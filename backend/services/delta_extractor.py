"""Delta extraction for conversation follow-ups.

Phase 3: Context-aware deterministic detection of what changed between turns.

DeltaExtractor tries fast structural handlers (country-only change,
dimension modifier, indicator switch, time change) BEFORE the LLM.
If no deterministic match is found it returns None so the caller can
fall through to the LLM-based follow-up detection.

Phase 3 addition: the extractor is now *context-aware*.  When the current
ConversationState points to a StatsCan indicator with dimensional tables,
follow-up terms like "female", "shelter", "Ontario" are checked against the
table's actual dimension members BEFORE the indicator-switch handler runs.
This prevents dimension modifiers from being misclassified as new indicators.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, List, Optional, TYPE_CHECKING

from .conversation_state_v2 import ConversationState, FollowUpDelta
from ..routing.country_resolver import CountryResolver

if TYPE_CHECKING:
    from .query import QueryService

logger = logging.getLogger(__name__)


# Provider names for detection
_PROVIDER_NAMES = {
    "fred", "world bank", "worldbank", "imf", "eurostat", "oecd",
    "bis", "statscan", "statistics canada", "comtrade", "un comtrade",
    "coingecko", "exchangerate",
}

# Filler/stop words that are not semantically meaningful in follow-ups
_FILLER_WORDS = {
    "show", "me", "the", "a", "an", "for", "in", "of", "to", "and",
    "what", "about", "how", "is", "are", "was", "were", "do", "does",
    "can", "could", "would", "will", "shall", "let", "please", "now",
    "instead", "also", "too", "well", "same", "but", "only", "just",
    "keep", "filter", "display", "plot", "use", "from", "with",
    "compare", "add", "include", "plus", "get", "give", "tell",
    "look", "at", "up", "see", "that", "this", "it", "its", "my",
    # Time-related words (not indicator terms)
    "since", "last", "past", "recent", "latest", "between",
    "years", "year", "months", "month", "quarters", "quarter",
    "decades", "decade",
}

# Provider switch patterns
_PROVIDER_SWITCH_RE = re.compile(
    r"\b(?:from|use|switch\s+to|via|using|through)\s+"
    r"(fred|world\s*bank|imf|eurostat|oecd|bis|statscan|statistics\s+canada|comtrade|coingecko)\b",
    re.IGNORECASE,
)

# Time-related patterns
_TIME_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_TIME_LAST_N_RE = re.compile(
    r"\blast\s+(\d+)\s+(years?|months?|quarters?|decades?)\b",
    re.IGNORECASE,
)
_TIME_SINCE_RE = re.compile(r"\bsince\s+(19\d{2}|20\d{2})\b", re.IGNORECASE)
_TIME_FROM_TO_RE = re.compile(
    r"\bfrom\s+(19\d{2}|20\d{2})\s+to\s+(19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)

# Additive / replacement markers
_ADDITIVE_MARKERS = {"compare", "add", "also", "include", "plus", "too", "well"}
_REPLACEMENT_MARKERS = {"only", "just", "filter", "keep"}


class DeltaExtractor:
    """Extract FollowUpDelta from a query given conversation state."""

    def __init__(self, query_service: "QueryService") -> None:
        self._qs = query_service

    def extract(
        self,
        query: str,
        state: ConversationState,
        intent: Optional[object] = None,
    ) -> Optional[FollowUpDelta]:
        """Try deterministic extraction.  Returns ``None`` if ambiguous.

        Parameters
        ----------
        query : str
            The user's raw follow-up text.
        state : ConversationState
            Accumulated state from previous turns.
        intent : ParsedIntent, optional
            LLM-parsed intent (used for LLM-layer extraction in future phases).

        Returns
        -------
        FollowUpDelta or None
            A delta if a deterministic pattern was matched, else ``None``.
        """
        if not state.indicator:
            # No prior state to follow-up on
            return None

        query_text = str(query or "").strip()
        if not query_text:
            return None

        # Priority order (most specific first):
        delta = self._try_country_only_follow_up(query_text, state)
        if delta:
            return delta

        delta = self._try_time_change(query_text, state)
        if delta:
            return delta

        delta = self._try_provider_change(query_text, state)
        if delta:
            return delta

        # Phase 3: Context-aware dimension modifier detection.
        # Must run BEFORE indicator switch so that "show female" after
        # "unemployment rate" is recognised as a dimension change, not
        # an indicator switch to "female".
        delta = self._try_dimension_modifier(query_text, state)
        if delta:
            return delta

        delta = self._try_indicator_switch(query_text, state)
        if delta:
            return delta

        # Not structurally recognizable -- needs LLM
        return None

    # ------------------------------------------------------------------
    # Handler: Country-only follow-up
    # ------------------------------------------------------------------

    def _try_country_only_follow_up(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect "show only US", "Japan", "add France" style follow-ups."""
        extracted_countries = CountryResolver.detect_all_countries_in_query(query)
        expanded_regions = CountryResolver.expand_regions_in_query(query)
        target_countries = extracted_countries or expanded_regions
        if not target_countries:
            return None

        # Check that query is geography-only (no other meaningful tokens)
        query_lower = query.lower()
        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        if not tokens:
            return None

        # Build set of geography-related tokens
        geography_tokens: set = set()
        for country in target_countries:
            geography_tokens.add(country.lower())
            if country.upper() == "US":
                geography_tokens.update({"united", "states", "usa", "us", "america"})
            iso3 = CountryResolver.to_iso3(country)
            if iso3:
                geography_tokens.add(iso3.lower())
            for alias, code in CountryResolver.COUNTRY_ALIASES.items():
                if code == country.upper():
                    for token in alias.split():
                        geography_tokens.add(token.lower())

        non_geography_tokens = [
            t for t in tokens
            if t not in _FILLER_WORDS and t not in geography_tokens
        ]
        if non_geography_tokens:
            return None

        # Determine additive vs. replacement
        query_words = set(query_lower.split())
        is_additive = bool(query_words & _ADDITIVE_MARKERS) and not bool(
            query_words & _REPLACEMENT_MARKERS
        )

        if is_additive:
            logger.info(
                "Delta: additive country follow-up %s",
                target_countries,
            )
            return FollowUpDelta(
                added_countries=target_countries,
                raw_query=query,
                delta_type="additive_country",
            )

        # Replacement
        if len(target_countries) == 1:
            logger.info(
                "Delta: country change → %s",
                target_countries[0],
            )
            return FollowUpDelta(
                changed_country=target_countries[0],
                raw_query=query,
                delta_type="country_change",
            )
        else:
            logger.info(
                "Delta: countries change → %s",
                target_countries,
            )
            return FollowUpDelta(
                changed_countries=target_countries,
                raw_query=query,
                delta_type="country_change",
            )

    # ------------------------------------------------------------------
    # Handler: Indicator switch
    # ------------------------------------------------------------------

    def _try_indicator_switch(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect "what about inflation", "show unemployment" style follow-ups.

        Uses exclusion-based detection: if the query is short and all tokens
        are either filler words, country names (already handled above), or
        a new indicator term, then it is an indicator switch.  No hardcoded
        term set required.
        """
        query_lower = query.lower().strip()
        if not query_lower or len(query_lower) > 80:
            return None

        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        if not tokens or len(tokens) > 10:
            return None

        # Check for explicit switch markers
        switch_markers = {
            "what about", "show", "how about", "switch to",
            "instead", "what is", "what's",
        }
        has_marker = any(marker in query_lower for marker in switch_markers)

        # Must NOT mention a new country (that is a country follow-up)
        extracted_countries = CountryResolver.detect_all_countries_in_query(query)
        if extracted_countries:
            return None

        # Collect non-filler tokens — these are the candidate indicator
        candidate_tokens = [t for t in tokens if t not in _FILLER_WORDS]
        if not candidate_tokens:
            return None

        # The candidate indicator is the non-filler tokens joined
        candidate_indicator = " ".join(candidate_tokens)

        # If no switch marker, only allow very short queries (1-3 non-filler words)
        if not has_marker and len(candidate_tokens) > 3:
            return None

        # If the candidate indicator is the same as the current one, skip
        current_lower = (state.indicator or "").lower()
        if candidate_indicator == current_lower:
            return None

        # Avoid matching provider names as indicators
        if candidate_indicator in _PROVIDER_NAMES:
            return None

        logger.info(
            "Delta: indicator switch '%s' → '%s'",
            state.indicator,
            candidate_indicator,
        )
        return FollowUpDelta(
            changed_indicator=candidate_indicator,
            raw_query=query,
            delta_type="indicator_switch",
        )

    # ------------------------------------------------------------------
    # Handler: Provider change
    # ------------------------------------------------------------------

    def _try_provider_change(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect "from FRED", "use IMF", "switch to Eurostat" patterns."""
        m = _PROVIDER_SWITCH_RE.search(query)
        if not m:
            return None

        provider_raw = m.group(1).strip()
        # Normalize
        from .query import normalize_provider_name
        provider = normalize_provider_name(provider_raw)

        if provider == (state.provider or state.routed_provider or "").upper():
            return None  # Same provider, no change

        logger.info("Delta: provider change → %s", provider)
        return FollowUpDelta(
            changed_provider=provider,
            raw_query=query,
            delta_type="provider_change",
        )

    # ------------------------------------------------------------------
    # Handler: Context-aware dimension modifier  (Phase 3)
    # ------------------------------------------------------------------

    def _try_dimension_modifier(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect dimension modifiers for StatsCan indicators.

        When the conversation state points to a StatsCan indicator that has
        dimensional tables (e.g., unemployment rate with sex/age/geography
        dimensions, or CPI with product dimensions), this handler checks
        whether the follow-up query contains a term that matches one of
        those dimension members.

        If a match is found the delta is ``added_dimensions`` (not
        ``changed_indicator``), which prevents misclassification.

        This handler is synchronous but wraps an async call to
        ``_get_cube_metadata`` via ``asyncio`` so it integrates with the
        sync ``extract()`` method.
        """
        # Only applies when current state is StatsCan
        current_provider = (
            state.provider or state.routed_provider or ""
        ).upper()
        if current_provider not in {"STATSCAN", "STATISTICS CANADA"}:
            return None

        # Need a base indicator in state
        if not state.indicator:
            return None

        # Access the StatsCan provider via the QueryService
        statscan = getattr(self._qs, "statscan_provider", None)
        if statscan is None:
            return None

        # Resolve the indicator key
        indicator_key = (
            state.base_indicator
            or state.indicator.upper().replace(" ", "_").replace("-", "_")
        )

        # Check if this indicator is known (has a vector or coordinate mapping)
        _vec = statscan.VECTOR_MAPPINGS.get(indicator_key)
        _coord = statscan.COORDINATE_PRODUCT_MAPPINGS.get(indicator_key)
        if _vec is None and _coord is None:
            return None

        # Resolve product ID
        product_id: Optional[str] = None
        if _coord:
            product_id = statscan._normalize_metadata_product_id(_coord[0])
        elif _vec is not None:
            _cached = statscan.PRODUCT_ID_CACHE.get(_vec)
            if _cached:
                product_id = statscan._normalize_metadata_product_id(_cached)

        if not product_id:
            return None

        # Fetch cube metadata (async → sync bridge)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We are inside an already-running event loop (e.g., FastAPI).
                # Create a future and use run_coroutine_threadsafe if possible,
                # or use the nest_asyncio approach.  For simplicity we use a
                # new thread to avoid deadlocks.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    cube_metadata = pool.submit(
                        lambda: asyncio.run(
                            statscan._get_cube_metadata(product_id)
                        )
                    ).result(timeout=10)
            else:
                cube_metadata = loop.run_until_complete(
                    statscan._get_cube_metadata(product_id)
                )
        except Exception as exc:
            logger.debug(
                "Dimension modifier: failed to fetch cube metadata for %s: %s",
                product_id,
                exc,
            )
            return None

        if not cube_metadata:
            return None

        # Use the provider's extract_dimension_modifiers
        modifiers: Dict[str, str] = statscan.extract_dimension_modifiers(
            query_text=query,
            base_indicator=indicator_key,
            product_id=product_id,
            cube_metadata=cube_metadata,
        )

        if not modifiers:
            return None

        logger.info(
            "Delta: dimension modifier for %s → %s",
            indicator_key,
            modifiers,
        )
        return FollowUpDelta(
            added_dimensions=modifiers,
            is_dimension_modifier_change=True,
            raw_query=query,
            delta_type="dimension_change",
        )

    # ------------------------------------------------------------------
    # Handler: Time change
    # ------------------------------------------------------------------

    def _try_time_change(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect time-period follow-ups like "last 20 years", "since 2010"."""
        query_lower = query.lower().strip()
        if not query_lower:
            return None

        # Only match if query is very short and mostly about time
        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        non_filler = [t for t in tokens if t not in _FILLER_WORDS and t not in {
            "years", "year", "months", "month", "quarters", "quarter",
            "decades", "decade", "since", "last", "from", "to", "between",
            "past", "recent", "latest",
        }]

        # If there are non-filler, non-time tokens, this is not a pure time change
        if len(non_filler) > 1:
            return None

        # Try "from YYYY to YYYY"
        m = _TIME_FROM_TO_RE.search(query)
        if m:
            start_year = m.group(1)
            end_year = m.group(2)
            logger.info("Delta: time change → %s to %s", start_year, end_year)
            return FollowUpDelta(
                changed_start_date=f"{start_year}-01-01",
                changed_end_date=f"{end_year}-12-31",
                raw_query=query,
                delta_type="time_change",
            )

        # Try "since YYYY"
        m = _TIME_SINCE_RE.search(query)
        if m:
            start_year = m.group(1)
            logger.info("Delta: time change → since %s", start_year)
            return FollowUpDelta(
                changed_start_date=f"{start_year}-01-01",
                raw_query=query,
                delta_type="time_change",
            )

        # Try "last N years"
        m = _TIME_LAST_N_RE.search(query)
        if m:
            from datetime import datetime
            n = int(m.group(1))
            unit = m.group(2).lower().rstrip("s")
            now = datetime.now()
            if unit == "year":
                start_year = now.year - n
                logger.info("Delta: time change → last %d years", n)
                return FollowUpDelta(
                    changed_start_date=f"{start_year}-01-01",
                    changed_end_date=f"{now.year}-12-31",
                    raw_query=query,
                    delta_type="time_change",
                )

        return None
