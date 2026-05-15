from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import logging
import asyncio
import re
import weakref

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import DataPoint, Metadata, NormalizedData
from .comtrade_metadata import (
    COUNTRY_CODE_MAPPINGS,
    HSReferenceAmbiguityError,
    HS_CODE_MAPPINGS,
    resolve_hs_reference_code,
)
# Country/region group definitions consolidated in CountryResolver (single source of truth).
# Previously imported: EU27_COUNTRY_CODES, REGION_EXPANSIONS, G7_COUNTRY_CODES,
# BRICS_COUNTRY_CODES, ASEAN_COUNTRY_CODES, NORDIC_COUNTRY_CODES
from .base import BaseProvider

logger = logging.getLogger(__name__)

# Retry configuration for rate limiting.
# Worst-case latency = MAX_RETRIES * REQUEST_TIMEOUT + sum(backoff delays).
# With MAX_RETRIES=3, TIMEOUT=30s, BASE=1.5:  3*30 + (1.5+3) = ~94.5s upper bound
# for a single reporter (vs. old 5*60 + 30 = 330s).  The fetch-level wall-clock
# budget below must be at least this large, or a valid Comtrade query can be
# cancelled before the retry policy has a chance to recover from a transient
# ReadTimeout/502.
MAX_RETRIES = 3
RETRY_DELAY_BASE = 1.5
REQUEST_TIMEOUT = 30.0  # Per-request timeout in seconds (Comtrade responds in 1-10s when healthy)
RATE_LIMIT_STATUS = 429
GLOBAL_REQUEST_CONCURRENCY = 1
GLOBAL_REQUEST_MIN_INTERVAL_SECONDS = 1.25
TRANSIENT_HTTP_STATUSES = {RATE_LIMIT_STATUS, 500, 502, 503, 504}
RETRY_BACKOFF_BUDGET = sum(RETRY_DELAY_BASE * (2 ** attempt) for attempt in range(MAX_RETRIES - 1))
SINGLE_REPORTER_RETRY_TIME_BUDGET = (
    (MAX_RETRIES * REQUEST_TIMEOUT)
    + RETRY_BACKOFF_BUDGET
    + GLOBAL_REQUEST_MIN_INTERVAL_SECONDS
    + 5.0
)
COMTRADE_OVERALL_TIME_BUDGET_CAP = max(
    REQUEST_TIMEOUT * 6,
    SINGLE_REPORTER_RETRY_TIME_BUDGET * 2,
)

_GLOBAL_REQUEST_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_GLOBAL_REQUEST_LAST_STARTED: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, float
] = weakref.WeakKeyDictionary()


def _comtrade_overall_time_budget(task_count: int) -> float:
    """Return the fetch-level budget without cutting off a single retry cycle.

    Comtrade fetches can fan out over reporter/partner/period chunks, but every
    outbound request is serialized by the provider-level subscription-key gate.
    The budget therefore needs to scale for the common one- and two-task cases
    while still capping broader fan-out so production requests do not stall for
    unbounded minutes during an upstream outage.
    """
    normalized_task_count = max(1, task_count)
    scaled_budget = SINGLE_REPORTER_RETRY_TIME_BUDGET * normalized_task_count
    return max(
        SINGLE_REPORTER_RETRY_TIME_BUDGET,
        min(COMTRADE_OVERALL_TIME_BUDGET_CAP, scaled_budget),
    )


def _global_request_semaphore() -> asyncio.Semaphore:
    """Return a process-local, per-event-loop Comtrade request semaphore.

    UN Comtrade applies short-window rate limits at the subscription-key level,
    so bounding concurrency inside each logical fetch is not enough when two
    same-loop user/certification sessions hit Comtrade at the same time.
    """
    loop = asyncio.get_running_loop()
    semaphore = _GLOBAL_REQUEST_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(GLOBAL_REQUEST_CONCURRENCY)
        _GLOBAL_REQUEST_SEMAPHORES[loop] = semaphore
    return semaphore


async def _respect_global_request_spacing() -> None:
    """Pace Comtrade calls on the current event loop.

    The process-wide semaphore prevents overlapping requests, but certification
    runs can still start serial requests back-to-back quickly enough to trip
    Comtrade's short-window 429 guard.  Space request starts while the global
    semaphore is held so concurrent sessions share one provider-level cadence.
    """
    loop = asyncio.get_running_loop()
    last_started = _GLOBAL_REQUEST_LAST_STARTED.get(loop)
    now = loop.time()
    if last_started is not None:
        wait_seconds = GLOBAL_REQUEST_MIN_INTERVAL_SECONDS - (now - last_started)
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
            now = loop.time()
    _GLOBAL_REQUEST_LAST_STARTED[loop] = now


class ComtradeProvider(BaseProvider):
    COMMODITY_MAPPINGS: Dict[str, str] = {
        "ALL": "TOTAL",
        "TOTAL": "TOTAL",
        # Oil and Petroleum Products (HS Chapter 27)
        "OIL": "27",
        "OILS": "27",
        "PETROLEUM": "27",
        "PETROLEUM_OIL": "27",
        "PETROLEUM_OILS": "27",
        "MINERAL_FUEL": "27",
        "MINERAL_FUELS": "27",
        "CRUDE": "2709",
        "CRUDE_OIL": "2709",
        "CRUDE_PETROLEUM": "2709",
        "NATURAL_GAS": "2711",
        "COAL": "2701",
        "PHARMACEUTICALS": "30",
        "MEDICINES": "30",
        "DRUGS": "30",
        "CLOTHING": "62",
        "APPAREL": "62",
        "GARMENTS": "62",
        "KNIT_CLOTHING": "61",
        "SHIRTS": "6205",
        "T-SHIRTS": "6109",
        "DRESSES": "6204",
        "TROUSERS": "6203",
        "PANTS": "6203",
        "FOOTWEAR": "64",
        "SHOES": "64",
        "BOOTS": "6401",
        "SNEAKERS": "6404",
        "ATHLETIC_SHOES": "640411",
        "LEATHER_SHOES": "640340",
        "MACHINERY": "84",
        "COMPUTERS": "8471",
        "LAPTOPS": "847130",
        "PRINTERS": "8443",
        "AIR_CONDITIONERS": "8415",
        "REFRIGERATORS": "8418",
        "ELECTRONICS": "85",
        "ELECTRICAL_EQUIPMENT": "85",
        "SMARTPHONES": "851712",
        "PHONES": "8517",
        "TELEVISIONS": "8528",
        "TVS": "8528",
        "SEMICONDUCTORS": "8542",
        "SEMICONDUCTOR": "8542",
        "CHIPS": "8542",
        "BATTERIES": "8506",
        "VEHICLES": "87",
        "CARS": "8703",
        "AUTOMOBILES": "8703",
        "TRUCKS": "8704",
        "MOTORCYCLES": "8711",
        "BICYCLES": "8712",
        "ELECTRIC_VEHICLES": "870380",
        "AIRCRAFT": "88",
        "AIRPLANES": "8802",
        "HELICOPTERS": "8802",
        "MEDICAL_INSTRUMENTS": "90",
        "OPTICAL_INSTRUMENTS": "90",
        "FURNITURE": "94",
        "CHAIRS": "9401",
        "TABLES": "9403",
        "TOYS": "95",
        "GAMES": "95",
        "FOOD": "AG2",
        "AGRICULTURE": "AG2",
        "WHEAT": "1001",
        "RICE": "1006",
        "CORN": "1005",
        "SOYBEANS": "1201",
        "COFFEE": "0901",
        "TEA": "0902",
        "MEAT": "02",
        "FISH": "03",
        "DAIRY": "04",
        "FRUIT": "08",
        "FRUITS": "08",
        "VEGETABLE": "07",
        "VEGETABLES": "07",
        "TEXTILE": "50",
        "TEXTILES": "50",
        "COTTON": "52",
        "FABRIC": "54",
        "IRON": "72",
        "STEEL": "72",
        "ALUMINUM": "76",
        "COPPER": "74",
        "GOLD": "7108",
        "SILVER": "7106",
        "RARE_EARTH": "2805",
        "RARE_EARTH_ELEMENTS": "2805",
        "RARE_EARTH_METALS": "2805",
        "RARE_EARTHS": "2805",
        "PLASTIC": "39",
        "PLASTICS": "39",
        "RUBBER": "40",
        "WOOD": "44",
        "PAPER": "48",
        "CHEMICAL": "28",
        "CHEMICALS": "28",
        "ORGANIC_CHEMICAL": "29",
        "ORGANIC_CHEMICALS": "29",
        # Beverages
        "WINE": "2204",
        "WINES": "2204",
        "BEER": "2203",
        "SPIRITS": "2208",
        "LIQUOR": "2208",
        "BEVERAGES": "22",
        # Flowers and plants
        "FLOWERS": "0603",
        "FLOWER": "0603",
        "CUT_FLOWERS": "0603",
        "PLANTS": "06",
        "LIVE_PLANTS": "06",
        # Minerals
        "IRON_ORE": "2601",
        "ORES": "26",
        "MINERALS": "25",
        # Fashion and apparel (expanded)
        "FASHION": "62",
        "FASHION_TEXTILES": "62",
        "LEATHER": "41",
        "LEATHER_GOODS": "42",
        "BAGS": "4202",
        "HANDBAGS": "420221",
        "JEWELRY": "7113",
        "WATCHES": "9101",
        "SUNGLASSES": "900410",
        "PERFUME": "3303",
        "COSMETICS": "33",
        # Auto parts
        "AUTO_PARTS": "8708",
        "CAR_PARTS": "8708",
        "VEHICLE_PARTS": "8708",
    }

    # Country mappings imported from comtrade_metadata module

    FLOW_MAPPINGS: Dict[str, str] = {
        "EXPORT": "X",
        "EXPORTS": "X",
        "IMPORT": "M",
        "IMPORTS": "M",
        "BOTH": "M,X",
    }

    @property
    def provider_name(self) -> str:
        return "Comtrade"

    def __init__(self, api_key: Optional[str], timeout: float = REQUEST_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        settings = get_settings()
        self.base_url = settings.comtrade_base_url.rstrip("/")
        self.api_key = api_key

    async def _fetch_data(self, **params) -> List[NormalizedData]:
        """Implement BaseProvider interface by routing to fetch_trade_data."""
        return await self.fetch_trade_data(
            reporter=params.get("reporter"),
            reporters=params.get("reporters"),
            partner=params.get("partner"),
            commodity=params.get("commodity"),
            flow=params.get("flow"),
            start_year=params.get("start_year"),
            end_year=params.get("end_year"),
            frequency=params.get("frequency", "annual"),
        )

    @staticmethod
    def _looks_like_specific_hs_heading(commodity: str) -> bool:
        text = str(commodity or "").strip()
        meaningful_tokens = [
            token
            for token in re.findall(r"[A-Za-z0-9]+", text)
            if len(token) > 1
        ]
        return bool((";" in text or "," in text) and len(meaningful_tokens) >= 6)

    @staticmethod
    def _strip_hs_code_prefix(title: str) -> str:
        return re.sub(r"^\s*\d{2,6}\s*[-:]\s*", "", str(title or "")).strip()

    @staticmethod
    def _candidate_title_similarity(query: str, candidate_name: str) -> float:
        try:
            from rapidfuzz import fuzz

            return float(fuzz.ratio(query, ComtradeProvider._strip_hs_code_prefix(candidate_name)))
        except Exception:
            query_tokens = set(re.findall(r"[a-z0-9]+", str(query or "").lower()))
            candidate_tokens = set(
                re.findall(
                    r"[a-z0-9]+",
                    ComtradeProvider._strip_hs_code_prefix(candidate_name).lower(),
                )
            )
            if not query_tokens or not candidate_tokens:
                return 0.0
            return 100.0 * len(query_tokens & candidate_tokens) / max(len(query_tokens), len(candidate_tokens))

    @staticmethod
    def _resolve_catalog_commodity_code(commodity: str) -> Optional[str]:
        """Resolve literal HS heading/subheading titles from provider catalog evidence."""

        from ..utils.retry import DataNotAvailableError

        try:
            reference_code = resolve_hs_reference_code(commodity)
        except HSReferenceAmbiguityError as exc:
            raise DataNotAvailableError(
                "comtrade_hs_subheading_ambiguous: provider HS reference contains multiple indistinguishable HS headings"
            ) from exc
        if reference_code:
            return reference_code

        candidates: list[dict[str, Any]] = []
        seen_codes: set[str] = set()

        def add_candidate(candidate: dict[str, Any]) -> None:
            candidate_code = str(candidate.get("code") or "").strip()
            if not (candidate_code.isdigit() and 2 <= len(candidate_code) <= 6):
                return
            if candidate_code in seen_codes:
                return
            seen_codes.add(candidate_code)
            candidates.append(candidate)

        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
            for candidate in lookup.search(commodity, provider="Comtrade", limit=8):
                add_candidate(candidate)
        except Exception:
            pass

        if not candidates and ComtradeProvider._looks_like_specific_hs_heading(commodity):
            try:
                from ..services.indicator_selector import IndicatorSelector

                selector = IndicatorSelector()
                retrieved, _scores = selector._get_candidates_with_scores(  # pylint: disable=protected-access
                    commodity,
                    "Comtrade",
                    top_k=8,
                )
                for code, name in retrieved:
                    add_candidate({"code": code, "name": name, "provider": "Comtrade"})
            except Exception:
                pass

        scored: list[tuple[float, dict[str, Any]]] = [
            (
                ComtradeProvider._candidate_title_similarity(
                    commodity,
                    str(candidate.get("name") or ""),
                ),
                candidate,
            )
            for candidate in candidates
        ]
        scored = [item for item in scored if item[0] >= 90.0]
        if not scored:
            if ComtradeProvider._looks_like_specific_hs_heading(commodity):
                raise DataNotAvailableError(
                    "comtrade_hs_subheading_unresolved: specific HS heading text did not resolve to a provider catalog code"
                )
            return None
        scored.sort(key=lambda item: (-item[0], str(item[1].get("code") or "")))
        if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) <= 1.0:
            top_code = str(scored[0][1].get("code") or "")
            second_code = str(scored[1][1].get("code") or "")
            if len(top_code) == len(second_code):
                raise DataNotAvailableError(
                    "comtrade_hs_subheading_ambiguous: provider catalog contains multiple indistinguishable HS headings"
                )
        return str(scored[0][1].get("code") or "").strip() or None

    @staticmethod
    def _commodity_code(commodity: Optional[str]) -> str:
        """Convert commodity name/HS code to Comtrade commodity code.

        Handles multiple input formats:
        - Numeric codes: "8703", "2709"
        - HS prefixed codes: "HS 8703", "HS8703", "HS 30", "HS2709"
        - Chapter references: "HS chapter 30", "chapter 84"
        - Text names: "automobiles", "wheat", "machinery"
        """
        if not commodity:
            return "TOTAL"
        commodity = commodity.strip()
        commodity = re.sub(
            r"^(?:exports?|imports?|re-exports?|re-imports?)\s+of\s+",
            "",
            commodity,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")

        # Tier 1: Direct numeric code (highest priority)
        if commodity.isdigit() and 2 <= len(commodity) <= 6:
            return commodity

        # Tier 2: HS-prefixed codes - strip "HS" prefix and extract numeric part
        # Handles: "HS 8703", "HS8703", "HS 30", "HS2709", "HS chapter 30"
        upper_commodity = commodity.upper()
        if upper_commodity.startswith("HS"):
            # Remove "HS" prefix
            rest = commodity[2:].strip()
            # Handle "HS chapter 30" format
            if rest.upper().startswith("CHAPTER"):
                rest = rest[7:].strip()  # Remove "chapter"
            # Extract numeric part
            numeric_part = ''.join(c for c in rest if c.isdigit())
            if numeric_part and 2 <= len(numeric_part) <= 6:
                return numeric_part

        # Tier 3: "Chapter XX" without HS prefix
        if upper_commodity.startswith("CHAPTER"):
            rest = commodity[7:].strip()
            numeric_part = ''.join(c for c in rest if c.isdigit())
            if numeric_part and 2 <= len(numeric_part) <= 4:
                return numeric_part

        catalog_code = ComtradeProvider._resolve_catalog_commodity_code(commodity)
        if catalog_code:
            return catalog_code

        key = upper_commodity.replace(" ", "_")
        # Tier 4: Check local COMMODITY_MAPPINGS (custom/specific mappings)
        code = ComtradeProvider.COMMODITY_MAPPINGS.get(key)
        if code:
            return code

        # Tier 5: Fallback to comprehensive HS code mappings
        code = HS_CODE_MAPPINGS.get(key)
        if code:
            return code

        # Tier 7: Partial match - find commodity containing this term
        for mapping_key, mapping_code in ComtradeProvider.COMMODITY_MAPPINGS.items():
            if key in mapping_key or mapping_key in key:
                return mapping_code

        # Default to TOTAL if no mapping found
        return "TOTAL"

    @staticmethod
    def _country_code(country: str) -> str:
        """Convert country name/code to UN Comtrade numeric code.

        Returns None for invalid regional codes that cannot be resolved.
        Valid regions (like "EU") are converted to their Comtrade codes.
        Invalid regions (like "Middle East", "Asia", "Africa") return None
        to signal that queries with these partners should be decomposed.

        Special handling:
        - "EU27_2020" returns list of individual EU member countries
        - Taiwan can use code 158 (standard) or 490 (alternative for some contexts)
        """
        # Already a UN numeric country code
        if country and str(country).isdigit():
            return str(country)

        key = country.upper().replace(" ", "_")
        code = COUNTRY_CODE_MAPPINGS.get(key, None)

        # If not found, return None instead of the original input
        # This prevents invalid codes like "Middle+East" or "AS" from being sent to API
        if code is None:
            return None

        return code

    @staticmethod
    def _flow_code(flow: Optional[str]) -> str:
        if not flow:
            return "M,X"
        key = flow.upper()
        if key in {"M", "X", "M,X"}:
            return key
        return ComtradeProvider.FLOW_MAPPINGS.get(key, "M,X")

    @staticmethod
    def _generate_period_values(start_year: int, end_year: int, frequency: str) -> List[str]:
        """Generate period values based on frequency.

        Args:
            start_year: Start year
            end_year: End year (inclusive)
            frequency: "annual", "monthly", "quarterly"

        Returns:
            List of period strings (e.g., ["2015", "2016", ...] for annual)
        """
        if frequency.lower() in ["annual", "yearly", "a", "y"]:
            return [str(year) for year in range(start_year, end_year + 1)]

        elif frequency.lower() in ["monthly", "month", "m"]:
            periods = []
            for year in range(start_year, end_year + 1):
                for month in range(1, 13):
                    periods.append(f"{year}{month:02d}")
            return periods

        elif frequency.lower() in ["quarterly", "quarter", "q"]:
            periods = []
            for year in range(start_year, end_year + 1):
                for quarter in range(1, 5):
                    periods.append(f"{year}{quarter}")
            return periods

        else:
            return [str(year) for year in range(start_year, end_year + 1)]

    @staticmethod
    def _chunk_period_values(period_values: List[str], max_periods: int = 12) -> List[str]:
        """Chunk period values into API-safe comma-separated batches.

        UN Comtrade returns HTTP 400 when the request includes too many periods.
        Keep chunking centralized so all query shapes use the same guardrail.
        """
        if not period_values:
            return []
        size = max(1, int(max_periods))
        return [
            ",".join(period_values[i:i + size])
            for i in range(0, len(period_values), size)
        ]

    @staticmethod
    def _merge_series_segments(series_list: List[NormalizedData]) -> List[NormalizedData]:
        """Merge segmented Comtrade responses into one series per flow/country."""
        if not series_list:
            return []

        merged: Dict[Tuple[str, str, str, str, str], NormalizedData] = {}
        for series in series_list:
            if not series or not series.metadata:
                continue

            meta = series.metadata
            key = (
                str(meta.source or ""),
                str(meta.indicator or ""),
                str(meta.country or ""),
                str(meta.frequency or ""),
                str(meta.unit or ""),
            )

            if key not in merged:
                merged[key] = NormalizedData(
                    metadata=meta.model_copy(deep=True),
                    data=list(series.data or []),
                )
                continue

            existing = merged[key]
            existing.data.extend(series.data or [])
            if not existing.metadata.apiUrl and meta.apiUrl:
                existing.metadata.apiUrl = meta.apiUrl
            if not existing.metadata.sourceUrl and meta.sourceUrl:
                existing.metadata.sourceUrl = meta.sourceUrl

        # Deduplicate points by date and keep sorted chronology.
        for series in merged.values():
            dedup: Dict[str, Dict[str, Optional[float]]] = {}
            for point in series.data or []:
                if isinstance(point, dict):
                    date = point.get("date")
                    value = point.get("value")
                else:
                    date = getattr(point, "date", None)
                    value = getattr(point, "value", None)

                if not date:
                    continue
                date_str = str(date)
                existing = dedup.get(date_str)
                if existing is None:
                    dedup[date_str] = {"date": date_str, "value": value}
                    continue
                existing_value = existing.get("value")
                if existing_value is None and value is not None:
                    dedup[date_str] = {"date": date_str, "value": value}
                elif value is not None and existing_value is not None and value > existing_value:
                    dedup[date_str] = {"date": date_str, "value": value}

            ordered = sorted(dedup.values(), key=lambda x: x["date"])
            series.data = [
                DataPoint(date=item["date"], value=item.get("value"))
                for item in ordered
            ]
            if series.data:
                series.metadata.startDate = series.data[0].date
                series.metadata.endDate = series.data[-1].date

        return list(merged.values())

    async def _fetch_single_reporter_data(
        self,
        client: httpx.AsyncClient,
        reporter_raw: str,
        partner_code: Optional[str],
        commodity_code: str,
        flow_code: str,
        period_param: str,
        freq_code: str,
    ) -> List[NormalizedData]:
        """Fetch trade data for a single reporter country with retry logic.

        Helper method to enable parallel fetching of multiple reporters.
        Implements exponential backoff for rate limiting (HTTP 429).

        Args:
            client: httpx AsyncClient instance
            reporter_raw: Reporter country name or code
            partner_code: Partner country code (can be None for world total)
            commodity_code: Commodity code
            flow_code: Trade flow code
            period_param: Comma-separated period string
            freq_code: Frequency code (A/M/Q)

        Returns:
            List of NormalizedData objects (one per flow type)
        """
        from ..utils.retry import DataNotAvailableError

        reporter_code = self._country_code(reporter_raw)

        # Check if reporter is a known non-reporting territory
        # Taiwan (158) does not report to UN Comtrade due to political status
        # Data about Taiwan trade must be obtained from partner perspective
        # (e.g., China's exports TO Taiwan, Japan's imports FROM Taiwan)
        NON_REPORTING_TERRITORIES = {
            "158": "Taiwan",  # Taiwan - use code 490 in partner queries
            "490": "Taiwan",  # Alternative Taiwan code
        }

        if reporter_code in NON_REPORTING_TERRITORIES:
            territory_name = NON_REPORTING_TERRITORIES[reporter_code]
            raise DataNotAvailableError(
                f"{territory_name} does not report trade data to UN Comtrade. "
                f"To get {territory_name} trade data, use partner perspective: "
                f"query partner imports FROM {territory_name} or partner exports TO {territory_name}."
            )

        # Handle invalid regional codes that cannot be resolved
        # (e.g., "Middle East", "Asia", "Africa" without specific country codes)
        if partner_code is None:
            # Only happens for regional codes that aren't mapped
            # These should be decomposed by the query service
            raise DataNotAvailableError(
                f"Cannot resolve partner code for reporter '{reporter_raw}' — "
                f"region may need to be decomposed into individual countries."
            )

        params = {
            "typeCode": "C",
            "freqCode": freq_code,
            "clCode": "HS",
            "reporterCode": reporter_code,
            "period": period_param,
            "partnerCode": partner_code,
            "cmdCode": commodity_code,
            "flowCode": flow_code,
            "format": "json",
        }

        if self.api_key:
            params["subscription-key"] = self.api_key

        # Use correct URL path based on frequency
        url_path = f"{self.base_url}/C/{freq_code}/HS"

        records: list[dict[str, Any]] = []

        # Implement exponential backoff retry for rate limiting and transient
        # upstream outages. UN Comtrade occasionally returns HTTP 500 during
        # otherwise valid bilateral lookups under load; treat that like a
        # retryable provider transient rather than immediately falling into
        # cross-provider fallback for a locked Comtrade conversation.
        #
        # The same endpoint can also transiently return an empty successful
        # payload for broad TOTAL bilateral flows that normally have data.  Do
        # a bounded retry for those broad trade-chain calls, but do not retry
        # narrow HS subheading empties because those are often genuine
        # commodity/data-availability gaps and must remain claim blockers.
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(url_path, params=params, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
                records = payload.get("data") or []
                if (
                    records
                    or commodity_code != "TOTAL"
                    or attempt >= MAX_RETRIES - 1
                ):
                    break
                delay = RETRY_DELAY_BASE * (2 ** attempt)
                logger.warning(
                    "Empty Comtrade TOTAL response for %s/%s, attempt %d/%d. "
                    "Retrying in %ss...",
                    reporter_raw,
                    partner_code,
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            except (httpx.ReadTimeout, httpx.TimeoutException, httpx.RequestError) as e:
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(
                        "Transient Comtrade error for %s (%s), attempt %d/%d. Retrying in %ss...",
                        reporter_raw,
                        type(e).__name__,
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    f"Comtrade API error for reporter {reporter_raw}: {type(e).__name__}: {str(e)}"
                )
                raise DataNotAvailableError(
                    f"Comtrade API error for reporter {reporter_raw}: {type(e).__name__}: {str(e)}"
                ) from e
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if status_code in TRANSIENT_HTTP_STATUSES and attempt < MAX_RETRIES - 1:
                    # Rate-limited or transient upstream error: wait and retry
                    # with exponential backoff.
                    delay = RETRY_DELAY_BASE * (2 ** attempt)
                    if status_code == RATE_LIMIT_STATUS:
                        logger.warning(
                            f"Rate limited (429) for {reporter_raw}, attempt {attempt + 1}/{MAX_RETRIES}. "
                            f"Waiting {delay}s before retry..."
                        )
                    else:
                        logger.warning(
                            "Transient Comtrade HTTP %s for %s, attempt %d/%d. "
                            "Waiting %ss before retry...",
                            status_code,
                            reporter_raw,
                            attempt + 1,
                            MAX_RETRIES,
                            delay,
                        )
                    await asyncio.sleep(delay)
                    continue
                else:
                    # Not a rate limit error, or final retry exhausted
                    response_text = (e.response.text or "").strip()[:300]
                    retry_after = e.response.headers.get("Retry-After")
                    quota_exhausted = (
                        status_code == 403
                        and (
                            "quota" in response_text.lower()
                            or retry_after is not None
                        )
                    )
                    if quota_exhausted:
                        detail = f"Comtrade API quota exhausted for reporter {reporter_raw}: HTTP 403"
                        if retry_after:
                            detail += f"; retry_after_seconds={retry_after}"
                        if response_text:
                            detail += f"; provider_message={response_text}"
                        logger.warning(detail)
                        raise DataNotAvailableError(detail) from e
                    logger.error(
                        f"Comtrade API error for reporter {reporter_raw}: "
                        f"HTTP {status_code}: {str(e)}"
                    )
                    raise DataNotAvailableError(
                        f"Comtrade API error for reporter {reporter_raw}: "
                        f"HTTP {status_code}"
                    ) from e
            except DataNotAvailableError:
                raise  # Let DataNotAvailableError propagate directly
            except Exception as e:
                # Other errors (network, JSON parsing, etc.)
                logger.error(
                    f"Comtrade API error for reporter {reporter_raw}: {type(e).__name__}: {str(e)}"
                )
                raise DataNotAvailableError(
                    f"Comtrade API error for reporter {reporter_raw}: {type(e).__name__}: {str(e)}"
                ) from e

        if not records:
            return []  # Return empty if no data

        # IMPROVED DEDUPLICATION: Include cmdCode in key to prevent non-total values
        # from overwriting total values when querying for TOTAL trade
        dedup_map: Dict[tuple, dict] = {}
        for record in records:
            # Use (period, flowDesc, cmdCode) as composite key
            # This prevents a specific HS chapter value from overwriting TOTAL
            key = (
                record.get("period"),
                record.get("flowDesc", "Trade"),
                record.get("cmdCode", "TOTAL")
            )
            # Keep maximum value for same key (handles data revisions)
            if key in dedup_map:
                existing_val = dedup_map[key].get("primaryValue") or 0
                new_val = record.get("primaryValue") or 0
                if new_val > existing_val:
                    dedup_map[key] = record
            else:
                dedup_map[key] = record

        # When querying for TOTAL, filter to only include TOTAL records
        # This prevents component HS chapter values from being mixed in
        if commodity_code == "TOTAL":
            total_records = [r for r in dedup_map.values()
                            if r.get("cmdCode", "").upper() in ("TOTAL", "AG2", "")]
            # If no TOTAL records found, fall back to all records
            if total_records:
                dedup_map = {
                    (r.get("period"), r.get("flowDesc", "Trade"), r.get("cmdCode", "TOTAL")): r
                    for r in total_records
                }
            else:
                logger.warning(f"No TOTAL records found, using all {len(dedup_map)} records")

        # Group deduplicated records by flow
        grouped: Dict[str, List[dict]] = defaultdict(list)
        for record in dedup_map.values():
            grouped[record.get("flowDesc", "Trade")].append(record)

        # Build API URL string safely
        try:
            url_with_params = response.request.url.copy_with(params=params)
            api_url = str(url_with_params)
        except Exception:
            api_url = str(response.request.url)

        if "subscription-key" in params:
            api_url = api_url.replace(params["subscription-key"], "YOUR_KEY")

        # Build results for this reporter
        results = []
        for flow_desc, flow_records in grouped.items():
            flow_records.sort(key=lambda x: x["period"])
            first = flow_records[0]

            flow_name = flow_desc or ("Exports" if "X" in flow_code else "Imports")
            commodity_name = first.get("cmdDesc") or ("Total Trade" if commodity_code == "TOTAL" else commodity_code)
            reporter_name = first.get("reporterDesc") or reporter_raw

            # Create data points and deduplicate by date
            # When multiple records exist for same period, keep the maximum value (assumes it's the total)
            data_points_map = {}
            for item in flow_records:
                date_str = f"{item['period']}-01-01"
                new_value = item.get("primaryValue") or 0

                # If date already exists, keep the maximum value
                if date_str in data_points_map:
                    existing_value = data_points_map[date_str]["value"] or 0
                    if new_value > existing_value:
                        data_points_map[date_str] = {"date": date_str, "value": new_value}
                else:
                    data_points_map[date_str] = {"date": date_str, "value": new_value}

            # Convert to list and sort by date
            data_points = sorted(data_points_map.values(), key=lambda x: x["date"])

            # DATA VALIDATION: Detect suspiciously low values for major trade flows
            # Major economies (US, China, Germany, Japan, UK, France, etc.) typically have
            # trade flows in billions of dollars, not thousands
            major_traders = {"CHN", "USA", "DEU", "JPN", "GBR", "FRA", "ITA", "NLD", "KOR", "CAN", "MEX"}
            reporter_is_major = reporter_code in major_traders or reporter_raw.upper() in [
                "CHINA", "UNITED STATES", "GERMANY", "JAPAN", "UNITED KINGDOM", "UK",
                "FRANCE", "ITALY", "NETHERLANDS", "SOUTH KOREA", "CANADA", "MEXICO"
            ]

            if reporter_is_major and commodity_code == "TOTAL":
                values = [p["value"] for p in data_points if p["value"] is not None and p["value"] > 0]
                if values:
                    max_val = max(values)
                    min_val = min(values)

                    # For major traders, total trade should be at least $1 billion
                    if max_val < 1e9:
                        logger.warning(
                            f"⚠️ COMTRADE DATA QUALITY: All values for {reporter_name} total {flow_name} "
                            f"are below $1B (max=${max_val:,.0f}). Data may be incomplete or in wrong units."
                        )

                    # Check for suspicious outliers (value >1000x smaller than max)
                    if min_val > 0 and max_val / min_val > 1000:
                        logger.warning(
                            f"⚠️ COMTRADE DATA QUALITY: Large value range for {reporter_name} total {flow_name}. "
                            f"Min=${min_val:,.0f}, Max=${max_val:,.0f}. Some data points may be incorrect."
                        )

            # Map frequency code to readable string
            freq_name = "monthly" if freq_code == "M" else "quarterly" if freq_code == "Q" else "annual"

            # Human-readable URL for data verification on UN Comtrade website
            source_url = "https://comtradeplus.un.org/TradeFlow"

            # Extract start and end dates from data points
            start_date = data_points[0]["date"] if data_points else None
            end_date = data_points[-1]["date"] if data_points else None

            results.append(
                NormalizedData(
                    metadata=Metadata(
                        source="UN Comtrade",
                        indicator=f"{flow_name} - {commodity_name}",
                        country=reporter_name,
                        frequency=freq_name,
                        unit="US Dollars",
                        lastUpdated=datetime.now(timezone.utc).isoformat(),
                        apiUrl=api_url,
                        sourceUrl=source_url,
                        seasonalAdjustment=None,
                        dataType="Level",
                        priceType="Nominal (current prices)",
                        description=f"{flow_name} - {commodity_name}",
                        notes=None,
                        startDate=start_date,
                        endDate=end_date,
                    ),
                    data=data_points,
                )
            )

        return results

    @staticmethod
    def _split_location_input(value: str) -> List[str]:
        """
        Split comma/semicolon-separated country input into individual tokens.

        Handles parser outputs like "Germany, Netherlands" without forcing users
        to resend the query as an explicit list.
        """
        if not value:
            return []
        chunks = re.split(r"[;,]", str(value))
        return [chunk.strip() for chunk in chunks if chunk and chunk.strip()]

    async def fetch_trade_data(
        self,
        reporter: Optional[str] = None,
        reporters: Optional[List[str]] = None,
        partner: Optional[str | List[str]] = None,
        commodity: Optional[str] = None,
        flow: Optional[str] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        frequency: str = "annual",
    ) -> List[NormalizedData]:
        """Fetch trade data from UN Comtrade API.

        Args:
            reporter: Single reporter country (for backwards compatibility)
            reporters: List of reporter countries (for multi-country queries)
            partner: Partner country code or name (None for world total)
            commodity: Commodity code or name
            flow: Trade flow (IMPORT, EXPORT, BOTH)
            start_year: Start year for data
            end_year: End year for data
            frequency: Data frequency (annual, monthly, quarterly)

        Returns:
            List of NormalizedData objects (one per reporter if multiple reporters)

        Raises:
            DataNotAvailableError: If partner is an invalid regional code that cannot be resolved
        """
        from ..utils.retry import DataNotAvailableError

        # Support both single reporter and multiple reporters
        reporter_list = reporters or [reporter or "US"]
        normalized_reporters: List[str] = []
        for reporter_item in reporter_list:
            split_items = self._split_location_input(str(reporter_item))
            if split_items:
                normalized_reporters.extend(split_items)
            else:
                normalized_reporters.append(str(reporter_item))
        reporter_list = normalized_reporters or [reporter or "US"]

        # Expand region names to individual countries using CountryResolver
        from ..routing.country_resolver import CountryResolver

        expanded_reporters = []
        for r in reporter_list:
            r_upper = r.upper().replace(" ", "_").replace("-", "_")

            # First, try CountryResolver (single source of truth for standard regions)
            expanded = CountryResolver.get_region_expansion(r_upper, format="un_numeric")
            if expanded:
                # Convert int codes to string format for Comtrade API
                region_codes = [str(code) for code in expanded]
                logger.info(f"🌍 Expanding Comtrade region '{r}' via CountryResolver → {len(region_codes)} countries")
                expanded_reporters.extend(region_codes)
            # Try variant names
            elif "_COUNTRIES" in r_upper or "_NATIONS" in r_upper:
                variant = r_upper.replace("_COUNTRIES", "").replace("_NATIONS", "")
                expanded = CountryResolver.get_region_expansion(variant, format="un_numeric")
                if expanded:
                    region_codes = [str(code) for code in expanded]
                    logger.info(f"🌍 Matched region '{variant}' via CountryResolver → {len(region_codes)} countries")
                    expanded_reporters.extend(region_codes)
                else:
                    expanded_reporters.append(r)
            else:
                expanded_reporters.append(r)
        reporter_list = expanded_reporters if expanded_reporters else reporter_list

        partner_inputs: List[str] = []
        if isinstance(partner, list):
            for partner_item in partner:
                if not partner_item:
                    continue
                split_items = self._split_location_input(str(partner_item))
                if split_items:
                    partner_inputs.extend(split_items)
                else:
                    partner_inputs.append(str(partner_item))
        elif partner:
            split_items = self._split_location_input(str(partner))
            partner_inputs = split_items if split_items else [str(partner)]

        # Taiwan Special Handling: Taiwan (490) is a non-reporting territory
        # If Taiwan is the reporter, we need to flip to partner perspective
        # Taiwan exports = partner imports FROM Taiwan (490)
        # Taiwan imports = partner exports TO Taiwan (490)
        taiwan_query = False
        if len(reporter_list) == 1:
            reporter_code_check = self._country_code(reporter_list[0])
            if reporter_code_check in ["158", "490"]:
                taiwan_query = True
                logger.info(
                    "Detected Taiwan as reporter - will use partner perspective. "
                    "Taiwan exports → query major partners' imports FROM Taiwan (490). "
                    "Taiwan imports → query major partners' exports TO Taiwan (490)."
                )

                # If no partner specified, use major Taiwan trading partners
                if not partner_inputs:
                    # Major Taiwan trading partners: China, USA, Japan, South Korea, Hong Kong
                    logger.info(
                        "No partner specified for Taiwan query - querying major trading partners: "
                        "China, USA, Japan, South Korea, Hong Kong, Singapore"
                    )
                    partner_list_for_taiwan = ["China", "USA", "Japan", "South Korea", "Hong Kong", "Singapore"]
                    # Flip: Taiwan as reporter → partners as reporters, Taiwan (490) as partner
                    reporter_list = partner_list_for_taiwan
                    partner_inputs = ["Taiwan"]  # Will resolve to 490
                    # Flip flow direction
                    if flow:
                        flow_upper = flow.upper()
                        if "EXPORT" in flow_upper:
                            flow = "IMPORT"  # Taiwan exports = partner imports from Taiwan
                            logger.info("Flipped flow: Taiwan exports → partner imports FROM Taiwan")
                        elif "IMPORT" in flow_upper:
                            flow = "EXPORT"  # Taiwan imports = partner exports to Taiwan
                            logger.info("Flipped flow: Taiwan imports → partner exports TO Taiwan")
                else:
                    # Partner specified - flip reporter and partner.
                    # For multi-partner inputs, use the first partner for this transformation.
                    original_partner = partner_inputs[0]
                    partner_inputs = ["Taiwan"]  # Taiwan becomes partner (490)
                    reporter_list = [original_partner]  # Partner becomes reporter
                    # Flip flow direction
                    if flow:
                        flow_upper = flow.upper()
                        if "EXPORT" in flow_upper:
                            flow = "IMPORT"
                            logger.info(f"Flipped: Taiwan exports to {original_partner} → {original_partner} imports FROM Taiwan")
                        elif "IMPORT" in flow_upper:
                            flow = "EXPORT"
                            logger.info(f"Flipped: Taiwan imports from {original_partner} → {original_partner} exports TO Taiwan")

        # Handle partner country code resolution (supports lists and region expansion)
        partner_codes: List[str] = []
        if partner_inputs:
            for partner_item in partner_inputs:
                partner_raw = str(partner_item).strip()
                if not partner_raw:
                    continue

                partner_upper = partner_raw.upper().replace(" ", "_").replace("-", "_")

                # First, try CountryResolver region expansion.
                expanded_partners = CountryResolver.get_region_expansion(
                    partner_upper,
                    format="un_numeric",
                )
                if expanded_partners:
                    resolved_codes = [str(code) for code in expanded_partners]
                    logger.info(
                        "🌍 Expanding Comtrade partner region '%s' via CountryResolver → %d countries",
                        partner_raw,
                        len(resolved_codes),
                    )
                    partner_codes.extend(resolved_codes)
                    continue

                # Try common region variants.
                if "_COUNTRIES" in partner_upper or "_NATIONS" in partner_upper:
                    variant = partner_upper.replace("_COUNTRIES", "").replace("_NATIONS", "")
                    expanded_partners = CountryResolver.get_region_expansion(variant, format="un_numeric")
                    if expanded_partners:
                        resolved_codes = [str(code) for code in expanded_partners]
                        logger.info(
                            "🌍 Matched Comtrade partner region '%s' via CountryResolver → %d countries",
                            variant,
                            len(resolved_codes),
                        )
                        partner_codes.extend(resolved_codes)
                        continue

                partner_code = self._country_code(partner_raw)
                if partner_code is None:
                    raise DataNotAvailableError(
                        f"'{partner_raw}' is not a valid country or recognized region in UN Comtrade. "
                        f"Please specify individual countries. "
                        f"For regions like 'Middle East', please specify individual countries: "
                        f"UAE, Saudi Arabia, Qatar, Kuwait, Oman, Iraq, Iran, Israel, etc."
                    )

                if partner_code == "EU27_2020":
                    # Expand EU27_2020 via CountryResolver (single source of truth)
                    eu_codes = CountryResolver.get_region_expansion("EU27_2020", format="un_numeric")
                    if eu_codes:
                        eu_str_codes = [str(code) for code in eu_codes]
                        logger.info(
                            "Expanding EU partner query to %d individual EU member countries",
                            len(eu_str_codes),
                        )
                        partner_codes.extend(eu_str_codes)
                    else:
                        logger.warning("EU27_2020 expansion via CountryResolver returned nothing")
                        partner_codes.append(partner_code)
                else:
                    partner_codes.append(partner_code)

            if not partner_codes:
                raise DataNotAvailableError(
                    "No valid partner countries were resolved for the query."
                )
        else:
            partner_codes = ["0"]  # World total

        # Preserve order while removing duplicates.
        partner_codes = list(dict.fromkeys(partner_codes))

        commodity_code = self._commodity_code(commodity)
        flow_code = self._flow_code(flow)

        now = datetime.now(timezone.utc)
        implicit_default_period = start_year is None and end_year is None
        start = start_year or now.year - 5
        end = end_year or now.year - 1

        # Determine frequency code for API
        freq_lower = frequency.lower()
        if freq_lower in ["monthly", "month", "m"]:
            freq_code = "M"
        elif freq_lower in ["quarterly", "quarter", "q"]:
            freq_code = "Q"
        else:
            freq_code = "A"  # Annual is default

        # Guardrail: Comtrade often lags by ~1 year (or more for annual data).
        # Avoid invalid future periods that trigger HTTP 400 and fallback misrouting.
        if freq_code == "A":
            max_supported_year = now.year - 2
        elif freq_code == "Q":
            max_supported_year = now.year - 1
        else:
            max_supported_year = now.year

        if end > max_supported_year:
            logger.info(
                "Clamping Comtrade end year from %s to %s for %s frequency",
                end,
                max_supported_year,
                frequency,
            )
            end = max_supported_year

        if start > end:
            logger.info(
                "Adjusting Comtrade start year from %s to %s to keep a valid period range",
                start,
                end,
            )
            start = end

        # Generate and chunk period parameters.
        # API guardrail: UN Comtrade returns 400 when request period count is too large.
        period_values = self._generate_period_values(start, end, frequency)
        period_chunks = self._chunk_period_values(period_values, max_periods=12)
        if not period_chunks:
            return []

        # Use shared HTTP client pool for better performance (timeout passed per-request)
        client = get_http_client()
        # Fetch combinations with local bounded concurrency, then pass each
        # outbound Comtrade request through the process-local global gate below.
        # The local semaphore limits per-query fan-out; the global gate/spacing
        # protects the shared subscription key from cross-session 429 bursts.
        max_concurrent_requests = 2 if freq_code == "A" else 3
        semaphore = asyncio.Semaphore(max_concurrent_requests)

        async def _guarded_fetch(
            reporter_raw: str,
            partner_code: str,
            period_param: str,
            flow_code_arg: str = flow_code,
        ) -> List[NormalizedData]:
            async with semaphore:
                async with _global_request_semaphore():
                    await _respect_global_request_spacing()
                    return await self._fetch_single_reporter_data(
                        client,
                        reporter_raw,
                        partner_code,
                        commodity_code,
                        flow_code_arg,
                        period_param,
                        freq_code,
                    )

        async def _run_fetch_for_chunks(
            chunks: List[str],
            flow_code_arg: str,
        ) -> Tuple[List[NormalizedData], Optional[BaseException]]:
            tasks = [
                _guarded_fetch(reporter_raw, partner_code, period_param, flow_code_arg)
                for reporter_raw in reporter_list
                for partner_code in partner_codes
                for period_param in chunks
            ]
            if not tasks:
                return [], None

            # Overall time budget: cap total wall-clock time so a cascade of
            # 429 retries across many tasks cannot stall the pipeline for
            # minutes. The lower bound must still cover one full
            # single-reporter retry envelope, otherwise transient Comtrade
            # timeouts are surfaced as false data-not-available results before
            # the provider retry policy completes.
            overall_budget = _comtrade_overall_time_budget(len(tasks))
            try:
                results_list = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=overall_budget,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Comtrade overall time budget (%.0fs) exceeded for %d tasks; "
                    "returning partial results",
                    overall_budget,
                    len(tasks),
                )
                return [], None

            flattened: List[NormalizedData] = []
            newest_error: Optional[BaseException] = None
            for result in results_list:
                if isinstance(result, BaseException):
                    logger.debug("Comtrade sub-task failed: %s", result)
                    newest_error = result
                    continue
                flattened.extend(result)
            return flattened, newest_error

        all_results, last_error = await _run_fetch_for_chunks(period_chunks, flow_code)

        async def _retry_both_flow_envelope(chunks: List[str]) -> None:
            nonlocal all_results, last_error
            if all_results or flow_code not in {"X", "M"}:
                return
            # UN Comtrade's v1 endpoint can return an empty payload for a
            # narrow flow-specific world-total request while the equivalent
            # both-flow request returns the requested flow records.  This also
            # occurs for sparse long-tail HS subheadings, so retry once with
            # the provider-native both-flow envelope and keep only the
            # originally requested direction.
            logger.info(
                "Comtrade %s request returned no rows for %s; retrying with M,X flow envelope",
                flow_code,
                commodity_code,
            )
            fallback_results, fallback_error = await _run_fetch_for_chunks(chunks, "M,X")
            if fallback_error is not None:
                last_error = fallback_error
            desired_prefix = "export" if flow_code == "X" else "import"
            for series in fallback_results:
                indicator_text = str(getattr(series.metadata, "indicator", "") or "").lower()
                if indicator_text.startswith(desired_prefix):
                    all_results.append(series)

        await _retry_both_flow_envelope(period_chunks)

        if (
            not all_results
            and implicit_default_period
            and freq_code == "A"
            and commodity_code != "TOTAL"
            and start > 2002
        ):
            # Exact long-tail HS subheadings are often sparse or discontinued.
            # With no user-specified date, a recent friendly default can create
            # false negatives even though UN Comtrade has older observations.
            # Retry only the older years that were not already requested;
            # explicit date windows remain strict.
            historical_period_values = self._generate_period_values(
                2002,
                start - 1,
                frequency,
            )
            historical_chunks = self._chunk_period_values(
                historical_period_values,
                max_periods=12,
            )
            logger.info(
                "Comtrade found no recent rows for sparse HS %s; retrying historical annual window 2002-%s",
                commodity_code,
                start - 1,
            )
            historical_results, historical_error = await _run_fetch_for_chunks(
                historical_chunks,
                flow_code,
            )
            if historical_error is not None:
                last_error = historical_error
            all_results.extend(historical_results)
            await _retry_both_flow_envelope(historical_chunks)

        # When ALL sub-tasks failed, propagate the most informative error
        # so the caller gets a specific message (e.g. "Taiwan does not report")
        # instead of the generic "No data available" from the fallback layer.
        if not all_results and last_error is not None:
            if isinstance(last_error, DataNotAvailableError):
                raise last_error
            raise DataNotAvailableError(str(last_error)) from last_error

        return self._merge_series_segments(all_results)

    async def fetch_trade_balance(
        self,
        reporter: str,
        partner: Optional[str] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        frequency: str = "annual",
    ) -> NormalizedData:
        """Fetch trade balance by getting imports and exports separately, then calculating balance.

        Trade Balance = Exports - Imports

        Makes sequential requests with delay to avoid rate limiting.

        Args:
            reporter: Reporting country code or name
            partner: Partner country code or name (None for world total)
            start_year: Start year for data
            end_year: End year for data
            frequency: Data frequency ("annual", "monthly", "quarterly")

        Returns:
            NormalizedData with trade balance time series
        """
        # Import here to avoid circular dependency
        from ..utils.retry import DataNotAvailableError

        # Fetch exports and imports separately for robustness
        # Add delay between requests to avoid rate limiting
        try:
            exports_data = await self.fetch_trade_data(
                reporter=reporter,
                partner=partner,
                commodity=None,
                flow="EXPORTS",
                start_year=start_year,
                end_year=end_year,
                frequency=frequency,
            )
        except Exception as exc:
            raise DataNotAvailableError(
                f"Failed to fetch export data for trade balance calculation: {str(exc)}"
            ) from exc

        # Brief delay between exports and imports requests to reduce rate limit risk
        await asyncio.sleep(0.2)

        try:
            imports_data = await self.fetch_trade_data(
                reporter=reporter,
                partner=partner,
                commodity=None,
                flow="IMPORTS",
                start_year=start_year,
                end_year=end_year,
                frequency=frequency,
            )
        except Exception as exc:
            raise DataNotAvailableError(
                f"Failed to fetch import data for trade balance calculation: {str(exc)}"
            ) from exc

        # Check that we got data
        if not exports_data or not imports_data:
            raise DataNotAvailableError(
                f"No trade data available for {reporter}" + (f" with {partner}" if partner else " (world)")
            )

        # Extract the first series from each result (should only be one per flow)
        exports = exports_data[0] if exports_data else None
        imports = imports_data[0] if imports_data else None

        if not exports or not imports:
            raise DataNotAvailableError(
                "Missing import or export data for trade balance calculation"
            )

        # Check that we have actual data points
        if not exports.data or not imports.data:
            raise DataNotAvailableError(
                f"No data points available for {reporter}" + (f" with {partner}" if partner else " (world)")
            )

        # Create maps for easier lookup
        import_map = {point.date: point.value or 0 for point in imports.data}
        export_map = {point.date: point.value or 0 for point in exports.data}

        # Get all unique dates from both series
        all_dates = sorted(set(list(import_map.keys()) + list(export_map.keys())))

        # Calculate trade balance for all dates
        balance_points = []
        for date in all_dates:
            export_value = export_map.get(date, 0)
            import_value = import_map.get(date, 0)
            balance = export_value - import_value
            balance_points.append({"date": date, "value": balance})

        # Build partner description for metadata
        partner_desc = f" with {partner}" if partner else " (World)"

        # Extract start and end dates from balance data
        start_date = balance_points[0]["date"] if balance_points else None
        end_date = balance_points[-1]["date"] if balance_points else None

        return NormalizedData(
            metadata=Metadata(
                source="UN Comtrade",
                indicator=f"Trade Balance{partner_desc}",
                country=exports.metadata.country,
                frequency=exports.metadata.frequency,
                unit="US Dollars",
                lastUpdated=datetime.now(timezone.utc).isoformat(),
                apiUrl=exports.metadata.apiUrl,
                seasonalAdjustment=None,
                dataType="Level",
                priceType="Nominal (current prices)",
                description=f"Trade Balance{partner_desc}",
                notes=None,
                startDate=start_date,
                endDate=end_date,
            ),
            data=balance_points,
        )
