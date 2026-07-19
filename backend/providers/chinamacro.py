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
# source kinds: eastmoney_v1 (report+field), eastmoney_treasury (field),
# mofcom_shrzgm (field), csv (curated snapshot only).
# ---------------------------------------------------------------------------
SERIES_REGISTRY: Tuple[Dict[str, Any], ...] = (
    {
        "id": "CN_PMI_MFG",
        "name_en": "China Manufacturing PMI (NBS, official)",
        "name_zh": "中国制造业采购经理指数",
        "unit": "index (50 = neutral)",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_PMI", "field": "MAKE_INDEX"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "manufacturing pmi, china pmi, official pmi, 制造业PMI, 采购经理指数, PMI指数",
        "notes": "Official NBS manufacturing PMI, released ~1st of the following month.",
    },
    {
        "id": "CN_PMI_NONMFG",
        "name_en": "China Non-Manufacturing PMI (NBS, official)",
        "name_zh": "中国非制造业商务活动指数",
        "unit": "index (50 = neutral)",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_PMI", "field": "NMAKE_INDEX"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "non-manufacturing pmi, services pmi china, 非制造业PMI, 服务业PMI, 商务活动指数",
        "notes": "Official NBS non-manufacturing business activity index.",
    },
    {
        "id": "CN_M2_YOY",
        "name_en": "China M2 Money Supply YoY Growth",
        "name_zh": "中国M2货币供应量同比增速",
        "unit": "% YoY",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_CURRENCY_SUPPLY", "field": "BASIC_CURRENCY_SAME"},
        "source_org": "PBoC via EastMoney datacenter",
        "synonyms": "m2 growth china, money supply growth, M2同比, 货币供应量增速, M2增速",
        "notes": "PBoC monthly financial statistics. FRED's China M2 mirror is frozen at 2018 — this series is current.",
    },
    {
        "id": "CN_M2_LEVEL",
        "name_en": "China M2 Money Supply (level)",
        "name_zh": "中国M2货币供应量",
        "unit": "CNY 100 million (亿元)",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_CURRENCY_SUPPLY", "field": "BASIC_CURRENCY"},
        "source_org": "PBoC via EastMoney datacenter",
        "synonyms": "m2 money supply china, m2 level, M2余额, 货币供应量, 广义货币",
        "notes": "Level in 亿元 (CNY 100M).",
    },
    {
        "id": "CN_M1_YOY",
        "name_en": "China M1 Money Supply YoY Growth",
        "name_zh": "中国M1货币供应量同比增速",
        "unit": "% YoY",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_CURRENCY_SUPPLY", "field": "CURRENCY_SAME"},
        "source_org": "PBoC via EastMoney datacenter",
        "synonyms": "m1 growth china, M1同比, M1增速, 狭义货币",
        "notes": "",
    },
    {
        "id": "CN_NEW_LOANS",
        "name_en": "China New RMB Loans (monthly)",
        "name_zh": "中国新增人民币贷款",
        "unit": "CNY 100 million (亿元)",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_RMB_LOAN", "field": "RMB_LOAN"},
        "source_org": "PBoC via EastMoney datacenter",
        "synonyms": "new loans china, rmb loans, bank lending china, 新增贷款, 人民币贷款, 信贷投放",
        "notes": "Monthly new RMB loan issuance.",
    },
    {
        "id": "CN_CPI_YOY",
        "name_en": "China CPI YoY (NBS, fresh monthly)",
        "name_zh": "中国居民消费价格指数同比",
        "unit": "% YoY",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_CPI", "field": "NATIONAL_SAME"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "china cpi monthly fresh, cpi inflation china current, CPI同比, 消费价格指数, 通胀率",
        "notes": "Fresh NBS release (~9-10th of following month); FRED's OECD-sourced mirror lags ~14 months.",
    },
    {
        "id": "CN_PPI_YOY",
        "name_en": "China PPI YoY (NBS, fresh monthly)",
        "name_zh": "中国工业生产者出厂价格指数同比",
        "unit": "% YoY",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_PPI", "field": "BASE_SAME"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "china ppi monthly, producer prices china, PPI同比, 工业品出厂价格, 生产者价格",
        "notes": "",
    },
    {
        "id": "CN_RETAIL_YOY",
        "name_en": "China Retail Sales YoY",
        "name_zh": "中国社会消费品零售总额同比",
        "unit": "% YoY",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_TOTAL_RETAIL", "field": "RETAIL_TOTAL_SAME"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "retail sales china, consumer spending china, 社会消费品零售总额, 零售销售, 消费增速",
        "notes": "NBS combines Jan-Feb into one release for this series; no separate January value exists.",
    },
    {
        "id": "CN_IP_YOY",
        "name_en": "China Industrial Production YoY (value added)",
        "name_zh": "中国规模以上工业增加值同比",
        "unit": "% YoY",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_INDUS_GROW", "field": "BASE_SAME"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "industrial production china, industrial output, 工业增加值, 工业生产, 工业产出",
        "notes": "NBS combines Jan-Feb into one release for this series.",
    },
    {
        "id": "CN_FAI_YOY",
        "name_en": "China Fixed Asset Investment YoY",
        "name_zh": "中国固定资产投资同比",
        "unit": "% YoY",
        "frequency": "monthly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_ASSET_INVEST", "field": "BASE_SAME"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "fixed asset investment china, fai china, capital investment, 固定资产投资, 投资增速",
        "notes": "As published on the EastMoney datacenter monthly row (BASE_SAME); the NBS headline print is the cumulative year-to-date rate — cite accordingly.",
    },
    {
        "id": "CN_GDP_CUM_YOY",
        "name_en": "China GDP Cumulative YoY (quarterly releases)",
        "name_zh": "中国国内生产总值累计同比",
        "unit": "% YoY (cumulative quarters)",
        "frequency": "quarterly",
        "source": {"kind": "eastmoney_v1", "report": "RPT_ECONOMY_GDP", "field": "SUM_SAME"},
        "source_org": "NBS via EastMoney datacenter",
        "synonyms": "china gdp growth quarterly, gdp yoy china, GDP增速, 经济增长率, 国内生产总值增速",
        "notes": "Each release covers cumulative quarters (Q1, Q1-2, Q1-3, full year); the value is the cumulative YoY rate.",
    },
    {
        "id": "CN_10Y_YIELD",
        "name_en": "China 10-Year Government Bond Yield",
        "name_zh": "中国10年期国债收益率",
        "unit": "% (yield to maturity)",
        "frequency": "daily",
        "source": {"kind": "eastmoney_treasury", "field": "EMM00166466"},
        "source_org": "ChinaBond via EastMoney datacenter",
        "synonyms": "china 10 year yield, cgb yield, chinese government bond, 10年期国债, 国债收益率, 中债收益率",
        "notes": "ChinaBond CGB yield curve 10Y point; authoritative fixings at yield.chinabond.com.cn (cross-checked within 1bp).",
    },
    {
        "id": "CN_SF_INCREMENT",
        "name_en": "China Social Financing Increment (monthly flow)",
        "name_zh": "中国社会融资规模增量",
        "unit": "CNY 100 million (亿元)",
        "frequency": "monthly",
        "source": {"kind": "mofcom_shrzgm", "field": "tiosfs"},
        "source_org": "PBoC data via MOFCOM open query",
        "synonyms": "social financing china, aggregate financing, total social financing, tsf, 社会融资规模, 社融增量, 社融",
        "notes": "MOFCOM republishes the PBoC series with a ~2-3 month lag; the latest 1-2 months may not be available yet.",
    },
)

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

    async def _rows_eastmoney_v1(self, report: str) -> List[Dict[str, Any]]:
        cache_key = f"em_v1:{report}"
        cached = _live_cache.get(cache_key)
        if cached and cached[0] > time.time():
            return cached[1]
        client = get_http_client()
        response = await self._get_with_retry(
            client,
            EASTMONEY_V1_URL,
            params={
                "reportName": report,
                "columns": "ALL",
                "pageSize": 500,
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
            },
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
        points: List[Dict[str, Any]] = []
        for row in rows:
            raw_date = str(row.get(date_field) or "").strip()
            value = row.get(field)
            if not raw_date or value is None:
                continue
            if date_field == "date" and len(raw_date) == 6 and raw_date.isdigit():
                date = f"{raw_date[:4]}-{raw_date[4:]}-01"  # MOFCOM "202604"
            else:
                date = raw_date[:10]  # "2026-06-01 00:00:00" -> "2026-06-01"
            points.append({"date": date, "value": value})
        points.sort(key=lambda p: p["date"])
        return points

    async def _live_observations(self, series: Dict[str, Any]) -> List[Dict[str, Any]]:
        src = series["source"]
        kind = src["kind"]
        if kind == "eastmoney_v1":
            rows = await self._rows_eastmoney_v1(src["report"])
            return self._parse_rows(rows, src["field"], "REPORT_DATE", series["id"])
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
