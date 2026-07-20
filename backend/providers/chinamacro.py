"""ChinaMacro provider — fresh Chinese headline macro series.

Closes the #1 real-user coverage gap (Chinese high-frequency domestic data:
PMI, money/credit aggregates, fresh CPI/PPI, activity data, 10Y CGB yield)
that no existing API provider carries at China's official release cadence
(FRED's China mirrors lag ~14 months; World Bank is annual; the NBS numeric
database WAF-blocks datacenter IPs; PBoC's robots.txt disallows bots).

Two-tier design:

  LIVE tier   — vendored public JSON endpoints, verified reachable from the
                production host (2026-07-19 research pass):
                * EastMoney datacenter (datacenter-web.eastmoney.com) — the
                  official NBS/PBoC figures republished as structured JSON,
                  fresh to the current release month, deep history. Robots:
                  allow-all. No auth.
                * EastMoney treasury-yield report (datacenter.eastmoney.com)
                  — daily ChinaBond CGB yields to T-1 (cross-checked against
                  yield.chinabond.com.cn official fixings).
                * MOFCOM shrzgm (data.mofcom.gov.cn, official) — social
                  financing increment; publishes with a ~2-3 month lag.
  CSV tier    — a curated, dated snapshot under backend/data/chinamacro/
                (seeded from the live endpoints, spot-verified against NBS
                press releases). Served ONLY when the live tier fails or
                mis-parses; responses disclose which tier served.

Schema-drift honesty: if an expected field is missing from a live payload the
provider logs a structured warning and falls back to the CSV snapshot — it
never serves misparsed values. The series registry below is DATA (id → source
+ field mapping); there are no per-series code branches.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import ssl
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..models import Metadata, NormalizedData
from ..services.http_pool import get_http_client
from ..utils.retry import DataNotAvailableError
from .base import BaseProvider

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "chinamacro"

EASTMONEY_V1_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_OLD_URL = "https://datacenter.eastmoney.com/api/data/get"
MOFCOM_SHRZGM_URL = "https://data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery"
# Fixed PUBLIC token baked into the treasury-yield report URL (vendored from
# the akshare project, which embeds the same constant). Not a secret.
EASTMONEY_TREASURY_TOKEN = "894050c76af8597a853f5b408b759f5d"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) econ-data-mcp/1.0"

# Cache TTLs: never hammer the upstream — one call per report per TTL.
MONTHLY_TTL_SECONDS = 6 * 3600
DAILY_TTL_SECONDS = 3600

# ---------------------------------------------------------------------------
# Series registry — DATA, the single source of truth for catalog + dispatch.
# Loaded from backend/data/chinamacro/registry.json (registry-as-data): each
# entry is a dict with keys id, name_en, name_zh, unit, frequency,
# source{kind, report, field}, source_org, synonyms, notes.
# source kinds: eastmoney_v1 (report+field), eastmoney_treasury (field),
# mofcom_shrzgm (field). Adding a series is a JSON edit — no code change.
# ---------------------------------------------------------------------------
REGISTRY_PATH = DATA_DIR / "registry.json"


def _load_registry() -> Tuple[Dict[str, Any], ...]:
    """Load the series registry from JSON at import (cached module-level)."""
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        entries = json.load(fh)
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"ChinaMacro registry at {REGISTRY_PATH} is empty or malformed")
    return tuple(entries)


SERIES_REGISTRY: Tuple[Dict[str, Any], ...] = _load_registry()

_REGISTRY_BY_ID: Dict[str, Dict[str, Any]] = {s["id"]: s for s in SERIES_REGISTRY}


class _SchemaDriftError(Exception):
    """A live payload no longer carries the field the registry expects."""


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).lower().strip()


def resolve_series(indicator: str) -> Optional[Dict[str, Any]]:
    """Resolve a series-id or free text (en/zh) to a registry entry.

    Exact id match first; otherwise best token-overlap over name/synonyms.
    Purely mechanical scoring — semantic resolution happens upstream in the
    indicator selector (indicators.db carries these series with synonyms).
    """
    text = str(indicator or "").strip()
    if not text:
        return None
    if text.upper() in _REGISTRY_BY_ID:
        return _REGISTRY_BY_ID[text.upper()]
    folded = _fold(text)
    tokens = set(folded.replace(",", " ").split())
    best, best_score = None, 0.0
    for series in SERIES_REGISTRY:
        hay = _fold(f"{series['name_en']} {series['name_zh']} {series['synonyms']}")
        score = 0.0
        # Whole-phrase hit (covers Chinese, which does not tokenize on spaces)
        if folded and folded in hay:
            score += 10.0
        for phrase in series["synonyms"].split(","):
            p = _fold(phrase)
            if p and (p in folded or folded in p):
                score += 5.0
        score += sum(1.0 for t in tokens if t and t in hay)
        if score > best_score:
            best, best_score = series, score
    return best if best_score >= 2.0 else None


# ---------------------------------------------------------------------------
# Live tier
# ---------------------------------------------------------------------------

_live_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_mofcom_client: Optional[httpx.AsyncClient] = None


def _mofcom_http_client() -> httpx.AsyncClient:
    """MOFCOM negotiates only legacy TLS ciphers (old Tomcat): lower the
    OpenSSL security level for THIS host only. Certificate checks stay on."""
    global _mofcom_client
    if _mofcom_client is None:
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        _mofcom_client = httpx.AsyncClient(verify=ctx, timeout=10.0)
    return _mofcom_client


class ChinaMacroProvider(BaseProvider):
    """Fresh Chinese headline macro data (live extraction + curated fallback)."""

    @property
    def provider_name(self) -> str:
        return "ChinaMacro"

    def __init__(self, timeout: float = 10.0) -> None:
        super().__init__(timeout=timeout)

    async def _fetch_data(self, **params) -> NormalizedData:
        return await self.fetch_indicator(
            indicator=params.get("indicator", ""),
            start_date=params.get("startDate"),
            end_date=params.get("endDate"),
        )

    # -- raw upstream fetches (one per source kind, cached per report) ------

    async def _rows_eastmoney_v1(
        self,
        report: str,
        sort_column: str = "REPORT_DATE",
        filter_expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # Cache per (report, filter): the RPT_INDUSTRY_INDEX report serves many
        # distinct series off ONE reportName, disambiguated by an INDICATOR_ID
        # filter — so the filter has to be part of the cache identity.
        cache_key = f"em_v1:{report}:{filter_expr or ''}"
        cached = _live_cache.get(cache_key)
        if cached and cached[0] > time.time():
            return cached[1]
        client = get_http_client()
        params: Dict[str, Any] = {
            "reportName": report,
            "columns": "ALL",
            "pageSize": 500,
            "sortColumns": sort_column,
            "sortTypes": "-1",
        }
        if filter_expr:
            params["filter"] = filter_expr
        response = await self._get_with_retry(
            client,
            EASTMONEY_V1_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        payload = response.json()
        if not payload.get("success") or not (payload.get("result") or {}).get("data"):
            raise DataNotAvailableError(f"EastMoney report {report} returned no data")
        rows = payload["result"]["data"]
        _live_cache[cache_key] = (time.time() + MONTHLY_TTL_SECONDS, rows)
        return rows

    async def _rows_eastmoney_treasury(self) -> List[Dict[str, Any]]:
        cache_key = "em_treasury"
        cached = _live_cache.get(cache_key)
        if cached and cached[0] > time.time():
            return cached[1]
        client = get_http_client()
        response = await self._get_with_retry(
            client,
            EASTMONEY_OLD_URL,
            params={
                "type": "RPTA_WEB_TREASURYYIELD",
                "sty": "ALL",
                "st": "SOLAR_DATE",
                "sr": "-1",
                "token": EASTMONEY_TREASURY_TOKEN,
                "p": 1,
                "ps": 500,
                "pageNo": 1,
                "pageNum": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        payload = response.json()
        rows = (payload.get("result") or {}).get("data") or []
        if not rows:
            raise DataNotAvailableError("EastMoney treasury-yield report returned no data")
        _live_cache[cache_key] = (time.time() + DAILY_TTL_SECONDS, rows)
        return rows

    async def _rows_mofcom_shrzgm(self) -> List[Dict[str, Any]]:
        cache_key = "mofcom_shrzgm"
        cached = _live_cache.get(cache_key)
        if cached and cached[0] > time.time():
            return cached[1]
        client = _mofcom_http_client()
        last_error: Optional[Exception] = None
        for _ in range(2):  # one retry, matching the live-tier contract
            try:
                response = await client.post(
                    MOFCOM_SHRZGM_URL, headers={"User-Agent": USER_AGENT}
                )
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list) or not rows:
                    raise DataNotAvailableError("MOFCOM shrzgm returned no data")
                _live_cache[cache_key] = (time.time() + MONTHLY_TTL_SECONDS, rows)
                return rows
            except Exception as exc:  # noqa: BLE001 — retried once, then raised
                last_error = exc
                await asyncio.sleep(0.5)
        raise DataNotAvailableError(f"MOFCOM shrzgm unavailable: {last_error}")

    # -- parsing (drift-safe) ----------------------------------------------

    @staticmethod
    def _parse_rows(
        rows: List[Dict[str, Any]], field: str, date_field: str, series_id: str
    ) -> List[Dict[str, Any]]:
        if rows and field not in rows[0]:
            raise _SchemaDriftError(
                f"{series_id}: expected field '{field}' missing from live payload "
                f"(have: {sorted(rows[0])[:12]})"
            )
        # Dedup by date: the RPT_INDUSTRY_INDEX report sometimes repeats the
        # newest row verbatim; one observation per date keeps charts clean.
        # For the single-value-per-date reports this is a no-op.
        by_date: Dict[str, Any] = {}
        for row in rows:
            raw_date = str(row.get(date_field) or "").strip()
            value = row.get(field)
            if not raw_date or value is None:
                continue
            if date_field == "date" and len(raw_date) == 6 and raw_date.isdigit():
                date = f"{raw_date[:4]}-{raw_date[4:]}-01"  # MOFCOM "202604"
            else:
                date = raw_date[:10]  # "2026-06-01 00:00:00" -> "2026-06-01"
            by_date[date] = value
        points = [{"date": d, "value": v} for d, v in by_date.items()]
        points.sort(key=lambda p: p["date"])
        return points

    async def _live_observations(self, series: Dict[str, Any]) -> List[Dict[str, Any]]:
        src = series["source"]
        kind = src["kind"]
        if kind == "eastmoney_v1":
            rows = await self._rows_eastmoney_v1(
                src["report"],
                sort_column=src.get("sort_column", "REPORT_DATE"),
                filter_expr=src.get("filter"),
            )
            return self._parse_rows(
                rows, src["field"], src.get("date_field", "REPORT_DATE"), series["id"]
            )
        if kind == "eastmoney_treasury":
            rows = await self._rows_eastmoney_treasury()
            return self._parse_rows(rows, src["field"], "SOLAR_DATE", series["id"])
        if kind == "mofcom_shrzgm":
            rows = await self._rows_mofcom_shrzgm()
            return self._parse_rows(rows, src["field"], "date", series["id"])
        raise DataNotAvailableError(f"{series['id']}: no live source configured")

    # -- CSV fallback tier ---------------------------------------------------

    _csv_cache: Optional[Tuple[float, Dict[str, List[Dict[str, Any]]], str]] = None

    @classmethod
    def _csv_observations(cls, series_id: str) -> Tuple[List[Dict[str, Any]], str]:
        """Return (points, as_of) from the curated snapshot. Raises when absent."""
        path = DATA_DIR / "observations.csv"
        if not path.exists():
            raise DataNotAvailableError(f"No curated ChinaMacro snapshot at {path}")
        mtime = path.stat().st_mtime
        if cls._csv_cache is None or cls._csv_cache[0] != mtime:
            by_series: Dict[str, List[Dict[str, Any]]] = {}
            with path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        value = float(row["value"])
                    except (TypeError, ValueError):
                        continue
                    by_series.setdefault(row["series_id"], []).append(
                        {"date": row["date"], "value": value}
                    )
            for points in by_series.values():
                points.sort(key=lambda p: p["date"])
            as_of = (DATA_DIR / "AS_OF").read_text().strip() if (DATA_DIR / "AS_OF").exists() else "unknown"
            cls._csv_cache = (mtime, by_series, as_of)
        points = cls._csv_cache[1].get(series_id)
        if not points:
            raise DataNotAvailableError(f"{series_id}: not in the curated snapshot")
        return points, cls._csv_cache[2]

    # -- public fetch --------------------------------------------------------

    async def fetch_indicator(
        self,
        indicator: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> NormalizedData:
        series = resolve_series(indicator)
        if series is None:
            raise DataNotAvailableError(
                f"ChinaMacro has no series matching '{indicator}'. "
                f"Available: {', '.join(sorted(_REGISTRY_BY_ID))}"
            )

        provenance: str
        try:
            points = await self._live_observations(series)
            provenance = f"live: {series['source_org']}"
        except _SchemaDriftError as drift:
            logger.warning(
                "chinamacro_schema_drift %s",
                json.dumps({"series": series["id"], "detail": str(drift)}),
            )
            points, as_of = self._csv_observations(series["id"])
            provenance = f"curated snapshot (as-of {as_of}); live schema drifted"
        except Exception as exc:  # noqa: BLE001 — any live failure falls back
            logger.warning(
                "chinamacro_live_unavailable %s",
                json.dumps({"series": series["id"], "error": str(exc)[:200]}),
            )
            points, as_of = self._csv_observations(series["id"])
            provenance = f"curated snapshot (as-of {as_of}); live source unavailable"

        if start_date:
            points = [p for p in points if p["date"] >= start_date[:10]]
        if end_date:
            points = [p for p in points if p["date"] <= end_date[:10]]
        if not points:
            raise DataNotAvailableError(
                f"{series['id']}: no observations in the requested window"
            )

        source_pages = {
            "eastmoney_v1": "https://data.eastmoney.com/cjsj/",
            "eastmoney_treasury": "https://data.eastmoney.com/cjsj/zmgzsyl.html",
            "mofcom_shrzgm": "https://data.mofcom.gov.cn/",
        }
        metadata = Metadata(
            source=f"ChinaMacro ({series['source_org']})",
            indicator=f"{series['name_en']} / {series['name_zh']}",
            country="China",
            frequency=series["frequency"],
            unit=series["unit"],
            lastUpdated=points[-1]["date"],
            seriesId=series["id"],
            apiUrl=f"chinamacro://{series['id']} [{provenance}]",
            sourceUrl=source_pages.get(series["source"]["kind"]),
            description=series["notes"] or None,
        )
        return NormalizedData(metadata=metadata, data=points)
