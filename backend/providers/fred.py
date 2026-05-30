from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_settings
from ..models import Metadata, NormalizedData
from ..routing.country_resolver import CountryResolver
from ..utils.retry import DataNotAvailableError
from ..services.http_pool import get_http_client
from .base import BaseProvider

logger = logging.getLogger(__name__)


_TIME_SCOPE_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_TIME_SCOPE_RELATIVE_RE = re.compile(
    r"\b(?:last|past|since|between|from|through|until|before|after|during)\b",
    flags=re.IGNORECASE,
)
_RECENCY_CUE_RE = re.compile(
    r"\b(?:latest|most recent|current|currently|today|yesterday|now)\b",
    flags=re.IGNORECASE,
)


def _query_has_explicit_time_scope(query: str) -> bool:
    """Detect whether the user explicitly constrained the requested time window."""
    query_text = str(query or "").strip()
    if not query_text:
        return False
    if _RECENCY_CUE_RE.search(query_text):
        return True
    if _TIME_SCOPE_YEAR_RE.search(query_text) and _TIME_SCOPE_RELATIVE_RE.search(query_text):
        return True
    if re.search(
        r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years|quarter|quarters)\b",
        query_text,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _parse_iso_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _country_aliases_for_iso2(iso2: str) -> list[str]:
    """Return human-readable aliases for an ISO2 code, longest first."""
    normalized = str(iso2 or "").strip().upper()
    if not normalized:
        return []
    aliases = [
        alias
        for alias, code in CountryResolver.COUNTRY_ALIASES.items()
        if code == normalized and len(alias) > 2
    ]
    return sorted(set(aliases), key=len, reverse=True)


def _country_label_for_iso2(iso2: str) -> str:
    aliases = _country_aliases_for_iso2(iso2)
    return aliases[0].title() if aliases else iso2.upper()


def _text_targets_country(text: str, iso2: str) -> bool:
    """Return whether provider-native text explicitly names a country."""
    text_lower = str(text or "").lower()
    if not text_lower:
        return False
    for alias in _country_aliases_for_iso2(iso2):
        if re.search(rf"\b{re.escape(alias.lower())}\b", text_lower):
            return True
    return False


def _infer_country_from_fred_info(info: Dict[str, Any]) -> str:
    """Infer country scope from FRED's provider-native series title.

    FRED contains both U.S. domestic series and international series published
    by upstream sources.  The provider API does not expose a normalized country
    field, but international series titles commonly carry explicit scope such as
    "Gross Domestic Product for Canada".  Treat that provider-native title text
    as mechanical country-scope metadata; otherwise preserve the historical U.S.
    default for domestic FRED series.
    """
    title = str(info.get("title") or "")
    for iso2 in sorted(set(CountryResolver.COUNTRY_ALIASES.values())):
        if _text_targets_country(title, iso2):
            return _country_label_for_iso2(iso2)
    return "US"


class FREDProvider(BaseProvider):
    """FRED (Federal Reserve Economic Data) provider.

    PHASE D: Now inherits from BaseProvider for:
    - Unified provider_name property
    - Standardized HTTP retry logic
    - Common error handling patterns
    """
    FREQUENCY_MAP: Dict[str, str] = {
        "Daily": "daily",
        "Weekly": "weekly",
        "Monthly": "monthly",
        "Quarterly": "quarterly",
        "Annual": "annual",
        "Semiannual": "semiannual",
    }

    @property
    def provider_name(self) -> str:
        """Return canonical provider name for logging and routing."""
        return "FRED"

    def __init__(self, api_key: Optional[str], metadata_search_service=None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)  # Initialize BaseProvider
        self.api_key = api_key
        if not self.api_key:
            # We mirror the JS behavior (warn instead of failing).
            print("⚠️  FRED API key not provided. Some features may be limited.")
        settings = get_settings()
        self.base_url = settings.fred_base_url.rstrip("/")
        self.metadata_search = metadata_search_service  # Optional: for future integration
        # Cache for dynamic series search results to avoid redundant API calls
        self._search_cache: Dict[str, str] = {}

    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Implementation of BaseProvider's abstract method.

        Routes to fetch_series with appropriate parameters.
        """
        return await self.fetch_series(params)

    async def _search_series_dynamic(self, search_text: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search FRED series using the series/search API endpoint.

        This enables dynamic discovery of ANY FRED series via the FRED API,
        allowing the system to find series for new indicators without code changes.

        Args:
            search_text: Natural language search terms (e.g., "gold price", "S&P 500")
            limit: Maximum number of results to return

        Returns:
            List of series metadata dicts with id, title, frequency, units, popularity
        """
        if not self.api_key:
            return []

        try:
            client = get_http_client()
            response = await self._get_with_retry(
                client,
                f"{self.base_url}/series/search",
                params={
                    "search_text": search_text,
                    "api_key": self.api_key,
                    "file_type": "json",
                    "limit": limit,
                    "order_by": "popularity",  # Prefer popular/authoritative series
                    "sort_order": "desc",
                },
                timeout=10.0,
            )
            data = response.json()

            series_list = data.get("seriess", [])
            logger.info(f"FRED dynamic search for '{search_text}' found {len(series_list)} series")

            return series_list

        except Exception as e:
            logger.warning(f"FRED series search failed for '{search_text}': {e}")
            return []

    def _rank_series_relevance(self, series: Dict[str, Any], search_terms: List[str]) -> float:
        """
        Rank a FRED series by relevance to search terms.

        This implements intelligent ranking to pick the BEST series match, not just
        the first match. Considers title match, popularity, and series characteristics.

        Args:
            series: FRED series metadata
            search_terms: List of normalized search terms

        Returns:
            Relevance score (higher is better)
        """
        score = 0.0
        title_lower = series.get("title", "").lower()
        series_id_lower = series.get("id", "").lower()

        # Exact title match bonus
        for term in search_terms:
            if term in title_lower:
                score += 10.0
            if term in series_id_lower:
                score += 5.0

        # Popularity bonus (FRED provides this)
        popularity = series.get("popularity", 0)
        if popularity > 0:
            score += min(popularity / 10, 5.0)  # Cap at 5 points

        # Prefer actual price/value series over indices or producer prices
        if "price" in title_lower and "producer" not in title_lower:
            score += 3.0
        if "spot" in title_lower or "fixing" in title_lower:
            score += 5.0  # Spot prices are usually what users want
        if "index" in title_lower and "price index" not in title_lower:
            score -= 2.0  # Slight penalty for generic indices
        if "ppi" in title_lower or "producer price" in title_lower:
            score -= 3.0  # Users asking for "gold price" don't want PPI

        # Prefer daily/monthly over annual for commodity prices
        frequency = series.get("frequency_short", "").lower()
        if frequency in ["d", "w", "m"]:
            score += 1.0

        # Penalize discontinued series
        if series.get("observation_end"):
            import datetime
            try:
                end_date = datetime.datetime.strptime(series["observation_end"], "%Y-%m-%d")
                days_old = (datetime.datetime.now() - end_date).days
                if days_old > 365:  # Discontinued over a year ago
                    score -= 5.0
            except (ValueError, TypeError):
                pass

        return score

    async def _find_best_series(self, indicator: str) -> Optional[str]:
        """
        Find the best FRED series ID for a natural language indicator using dynamic search.

        Resolution order:
        1. Check in-memory cache
        2. Search local indicator database (FTS5, 138K+ FRED series) - FAST
        3. Fall back to FRED API search - SLOWER, network call

        Args:
            indicator: Natural language indicator (e.g., "gold price", "S&P 500 index")

        Returns:
            Best matching FRED series ID, or None if no good match found
        """
        # Check cache first
        cache_key = indicator.upper().strip()
        if cache_key in self._search_cache:
            logger.info(f"Using cached FRED series for '{indicator}': {self._search_cache[cache_key]}")
            return self._search_cache[cache_key]

        # STEP 1: Search local indicator database (FTS5, instant)
        try:
            from ..services.indicator_database import get_indicator_lookup
            lookup = get_indicator_lookup()
            db_results = lookup.search(indicator, provider="FRED", limit=10)
            if db_results:
                # Rank results and pick best match
                best = db_results[0]  # Already ranked by relevance
                series_id = best.get("code")
                if series_id:
                    logger.info(f"Indicator database match for '{indicator}': {series_id} ({best.get('name', 'N/A')})")
                    self._search_cache[cache_key] = series_id
                    return series_id
        except Exception as e:
            logger.warning(f"Indicator database search failed: {e}")

        # STEP 2: Fall back to FRED API search (slower, network call)
        series_list = await self._search_series_dynamic(indicator, limit=20)

        if not series_list:
            return None

        # Normalize search terms for relevance ranking
        search_terms = [t.lower().strip() for t in re.split(r'[\s_-]+', indicator) if t]

        # Rank all series by relevance
        ranked = [(s, self._rank_series_relevance(s, search_terms)) for s in series_list]
        ranked.sort(key=lambda x: x[1], reverse=True)

        # Log top matches for debugging
        if ranked:
            top_3 = ranked[:3]
            logger.info(f"Top FRED series matches for '{indicator}':")
            for s, score in top_3:
                logger.info(f"  [{score:.1f}] {s['id']}: {s['title']}")

        # Return best match if score is reasonable
        if ranked and ranked[0][1] >= 5.0:
            best_series = ranked[0][0]["id"]
            # Cache the result
            self._search_cache[cache_key] = best_series
            logger.info(f"Dynamic FRED series discovery: '{indicator}' -> '{best_series}'")
            return best_series

        logger.warning(f"No good FRED series match for '{indicator}' (best score: {ranked[0][1] if ranked else 0:.1f})")
        return None

    # Index series that should get pc1 transformation when user asks for rate/change
    _INDEX_SERIES_FOR_RATE = {
        "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIACO",  # Price indices
        "CPALTT01USM657N",  # CPI all items
    }
    _RATE_KEYWORDS = {"inflation", "rate", "change", "growth", "percent", "pct", "%"}

    def _infer_transformation(self, indicator: str, series_code: str) -> Optional[str]:
        """Infer pc1 transformation when user asks for a rate but resolved to an index series."""
        if series_code not in self._INDEX_SERIES_FOR_RATE:
            return None
        indicator_lower = indicator.lower()
        if any(kw in indicator_lower for kw in self._RATE_KEYWORDS):
            return "pc1"
        return None

    def _series_id_with_transform(self, indicator: Optional[str], series_id: Optional[str]) -> tuple[str, Optional[str]]:
        """
        Parse an explicit series ID or detect a raw FRED code from indicator text.

        This method handles:
        1. Explicit series_id passthrough (with optional transformation suffix like ":pc1")
        2. Raw FRED code detection (short alphanumeric strings that look like series IDs)

        All natural-language indicator discovery is delegated to provider metadata
        search in _resolve_series_id_async().

        Args:
            indicator: Natural language indicator name (e.g., "GDP growth", "unemployment rate")
            series_id: Explicit FRED series ID (e.g., "GDP", "UNRATE")

        Returns:
            Tuple of (series_id, transformation) where transformation can be None or 'pc1', 'pch', etc.
            Returns (None, None) if no series ID can be determined from direct parsing.
        """
        # If explicit series ID provided, use it (check for transformation suffix)
        if series_id:
            if ":" in series_id:
                parts = series_id.split(":", 1)
                return parts[0], parts[1]
            return series_id, None

        if not indicator:
            raise ValueError("Series ID or indicator is required")

        # Backward-compatible explicit FRED code passthrough for code-like inputs.
        # Keep this strict to avoid treating parser-style tokens (with underscores)
        # as raw series IDs.
        candidate = indicator.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{1,25}", candidate):
            return candidate, None

        # No direct series ID detected - return None to signal that provider
        # metadata discovery should be tried in _resolve_series_id_async().
        return None, None

    async def _resolve_series_id_async(
        self, indicator: Optional[str], series_id: Optional[str]
    ) -> Tuple[str, Optional[str]]:
        """
        Async series ID resolution using database and dynamic search.

        Resolution priority:
        1. Explicit series_id passthrough (with optional transformation suffix)
        2. Raw FRED code detection (short alphanumeric strings)
        3. Provider metadata discovery (local FRED metadata index, then FRED API search)

        This keeps provider code/title passthrough mechanical while avoiding
        retired universal semantic shortcuts as final authority.

        Args:
            indicator: Natural language indicator name
            series_id: Explicit FRED series ID (takes precedence)

        Returns:
            Tuple of (series_id, transformation)

        Raises:
            DataNotAvailableError: If no matching series can be found
        """
        # Try explicit series_id / raw code detection first (synchronous, fast)
        result_series, transform = self._series_id_with_transform(indicator, series_id)

        if result_series is not None:
            return result_series, transform

        # Try provider metadata discovery (local metadata index, then FRED API).
        if indicator:
            logger.info(f"No database match for '{indicator}', attempting dynamic FRED series search...")
            dynamic_series = await self._find_best_series(indicator)
            if dynamic_series:
                logger.info(f"Dynamic discovery successful: '{indicator}' -> '{dynamic_series}'")
                return dynamic_series, None

        # Both approaches failed - provide helpful error
        indicator_lower = indicator.lower() if indicator else ""

        # Check if this is a precious metals/commodity spot price query
        precious_metals = ["gold", "silver", "platinum", "palladium"]
        if any(metal in indicator_lower for metal in precious_metals):
            raise DataNotAvailableError(
                f"FRED does not have spot prices for precious metals like gold or silver. "
                f"For commodity price INDICES (not spot prices), try: "
                f"'Producer Price Index' or 'PPI commodities' which maps to PPIACO. "
                f"For real-time gold/silver spot prices, use dedicated services like kitco.com or goldprice.org."
            )

        raise DataNotAvailableError(
            f"Unknown FRED indicator: '{indicator}'. "
            f"Dynamic search did not find a good match. "
            f"Please use a known indicator name (e.g., 'GDP', 'unemployment', 'inflation', 'housing starts') "
            f"or provide an explicit FRED series ID via the 'seriesId' parameter. "
            f"See https://fred.stlouisfed.org for available series."
        )

    def _series_id(self, indicator: Optional[str], series_id: Optional[str]) -> str:
        """Legacy synchronous method - returns just the series ID.

        Tries explicit series_id / raw code detection only. Natural-language
        discovery is async metadata search in fetch_series(); this synchronous
        compatibility method must not invoke retired semantic shortcuts.
        """
        series, _ = self._series_id_with_transform(indicator, series_id)
        if series is not None:
            return series

        raise DataNotAvailableError(
            f"Unknown FRED indicator: '{indicator}'. "
            f"Use fetch_series() for provider metadata discovery, or provide an explicit FRED series ID."
        )

    @staticmethod
    def _should_skip_default_window_for_exact_series(
        params: Dict[str, Any],
        info: Dict[str, Any],
    ) -> bool:
        """Avoid a doomed default-window observations call for stale exact FRED rows.

        Certification exact-title rows often omit an explicit date range.  The
        query pipeline supplies a friendly recent default (usually 5 years) so
        broad end-user queries do not ask clarifying questions.  Some exact
        FRED series are historical/discontinued, and the recent-window
        observations call can take tens of seconds before returning no data,
        only for data_fetcher to broaden and retry.  When the series metadata
        already proves the default window cannot intersect the series, skip the
        doomed call and fetch the available historical observations directly.

        Explicit user time scopes remain strict and are not broadened here.
        """
        if not (
            params.get("__exact_indicator_title_match")
            or params.get("__exact_provider_code_match")
        ):
            return False

        if _query_has_explicit_time_scope(str(params.get("__original_query") or "")):
            return False

        requested_start = _parse_iso_date(params.get("startDate"))
        requested_end = _parse_iso_date(params.get("endDate"))
        if not requested_start and not requested_end:
            return False

        series_start = _parse_iso_date(info.get("observation_start"))
        series_end = _parse_iso_date(info.get("observation_end"))

        if requested_start and series_end and series_end < requested_start:
            return True
        if requested_end and series_start and series_start > requested_end:
            return True
        return False

    def _map_frequency(self, fred_frequency: str) -> str:
        return self.FREQUENCY_MAP.get(fred_frequency, fred_frequency.lower())

    def _normalize_percentage_values(self, data: list[dict], series_id: str, unit: str) -> list[dict]:
        """Delegate to the shared SDMX percentage-normalizer.

        Phase 3.1 extraction: the previous in-place implementation was
        byte-identical to the IMF and Eurostat copies. The `unit` argument
        is preserved in the signature for caller compatibility but is
        unused — the normalizer is driven purely by the value distribution.
        """
        from ._sdmx import normalize_percentage_values as _shared
        del unit  # unused — kept for caller signature stability
        return _shared(data, label=series_id)

    async def fetch_series(
        self, params: Dict[str, Any]
    ) -> NormalizedData:
        # Use async metadata discovery with dynamic search fallback.
        target_series, transformation = await self._resolve_series_id_async(
            params.get("indicator"), params.get("seriesId")
        )

        # Use shared HTTP client pool with retry logic for transient failures
        client = get_http_client()
        info_response = await self._get_with_retry(
            client,
            f"{self.base_url}/series",
            params={
                "series_id": target_series,
                "api_key": self.api_key,
                "file_type": "json",
            },
            timeout=15.0,
        )
        info_payload = info_response.json()
        if not info_payload.get("seriess"):
            raise DataNotAvailableError(f"FRED series '{target_series}' not found. Please check the series ID or try a different indicator.")
        info = info_payload["seriess"][0]

        effective_params = dict(params)
        if self._should_skip_default_window_for_exact_series(params, info):
            logger.info(
                "Skipping default FRED time window for exact historical series: "
                "series=%s requested=%s..%s available=%s..%s",
                target_series,
                params.get("startDate"),
                params.get("endDate"),
                info.get("observation_start"),
                info.get("observation_end"),
            )
            for key in ("startDate", "endDate", "start_year", "end_year"):
                effective_params.pop(key, None)

        obs_params = {
            "series_id": target_series,
            "api_key": self.api_key,
            "file_type": "json",
        }
        if effective_params.get("startDate"):
            obs_params["observation_start"] = effective_params["startDate"]
        if effective_params.get("endDate"):
            obs_params["observation_end"] = effective_params["endDate"]

        # Add transformation if specified (e.g., 'pc1' for percent change from year ago)
        if transformation:
            obs_params["units"] = transformation

        obs_response = await self._get_with_retry(
            client,
            f"{self.base_url}/series/observations", params=obs_params, timeout=15.0
        )
        observations = obs_response.json().get("observations", [])

        if (
            (
                params.get("__exact_indicator_title_match")
                or params.get("__exact_provider_code_match")
            )
            and not _query_has_explicit_time_scope(str(params.get("__original_query") or ""))
            and not observations
            and (obs_params.get("observation_start") or obs_params.get("observation_end"))
        ):
            logger.info(
                "FRED exact historical series returned no rows in default window; "
                "retrying without default window: series=%s",
                target_series,
            )
            effective_params = dict(params)
            for key in ("startDate", "endDate", "start_year", "end_year"):
                effective_params.pop(key, None)
            obs_params = {
                "series_id": target_series,
                "api_key": self.api_key,
                "file_type": "json",
            }
            if transformation:
                obs_params["units"] = transformation
            obs_response = await self._get_with_retry(
                client,
                f"{self.base_url}/series/observations",
                params=obs_params,
                timeout=15.0,
            )
            observations = obs_response.json().get("observations", [])

        # Build API URL for metadata (without exposing actual API key)
        api_url_params = {
            "series_id": target_series,
            "file_type": "json",
        }
        if effective_params.get("startDate"):
            api_url_params["observation_start"] = effective_params["startDate"]
        if effective_params.get("endDate"):
            api_url_params["observation_end"] = effective_params["endDate"]

        query_string = "&".join(f"{key}={value}" for key, value in api_url_params.items())
        api_url = f"{self.base_url}/series/observations?{query_string}&api_key=***"

        unit = info.get("units", "")
        indicator_title = info["title"]

        # Override unit and title if transformation was applied
        if transformation == "pc1":
            unit = "Percent Change from Year Ago"
            indicator_title = f"{info['title']} (YoY % Change)"
        elif transformation == "pch":
            unit = "Percent Change"
            indicator_title = f"{info['title']} (% Change)"
        elif transformation == "log":
            unit = "Natural Log"
            indicator_title = f"{info['title']} (Log)"

        # Human-readable URL for data verification on FRED website
        source_url = f"https://fred.stlouisfed.org/series/{target_series}"

        # Extract enhanced metadata from FRED API response
        seasonal_adjustment = info.get("seasonal_adjustment", None)
        seasonal_adj_short = info.get("seasonal_adjustment_short", None)
        notes = info.get("notes", None)

        # Determine data type from series characteristics
        data_type = None
        title_lower = info["title"].lower()
        unit_lower = unit.lower()
        if "percent change" in title_lower or "growth rate" in title_lower:
            data_type = "Percent Change"
        elif "change" in title_lower:
            data_type = "Change"
        elif "index" in title_lower or "index" in unit_lower:
            data_type = "Index"
        elif "rate" in title_lower and "percent" in unit_lower:
            data_type = "Rate"
        else:
            data_type = "Level"

        # Determine price type (real vs nominal)
        price_type = None
        if "real" in title_lower or "chained" in title_lower or "constant" in title_lower:
            price_type = "Real (inflation-adjusted)"
        elif "nominal" in title_lower or "current" in title_lower:
            price_type = "Nominal (current prices)"

        # Parse notes into list (split by periods/semicolons for readability)
        notes_list = None
        if notes:
            # Truncate very long notes and split into sentences
            notes_text = notes[:500] if len(notes) > 500 else notes
            notes_list = [n.strip() for n in notes_text.split('.') if n.strip()][:3]

        metadata = Metadata(
            source="FRED",
            indicator=info["title"],
            country=_infer_country_from_fred_info(info),
            frequency=self._map_frequency(info["frequency"]),
            unit=unit,
            lastUpdated=info.get("last_updated", ""),
            seriesId=target_series,
            apiUrl=api_url,
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=seasonal_adjustment or seasonal_adj_short,
            dataType=data_type,
            priceType=price_type,
            description=info.get("notes", "")[:200] if info.get("notes") else None,
            notes=notes_list,
            startDate=info.get("observation_start"),
            endDate=info.get("observation_end"),
        )

        data_points = [
            {
                "date": obs.get("date", ""),
                "value": None if obs.get("value") in (None, ".", "") else float(obs["value"]),
            }
            for obs in observations
            if obs.get("date")  # Skip observations without dates
        ]

        # Normalize percentage values (FRED sometimes stores as decimals)
        if "percent" in unit.lower() or "rate" in unit.lower():
            data_points = self._normalize_percentage_values(data_points, target_series, unit)

        return NormalizedData(metadata=metadata, data=data_points)
