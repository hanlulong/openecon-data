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
from typing import Dict, Optional, TYPE_CHECKING

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
    # Removal words (country-level operations, not indicator terms)
    "remove", "exclude", "without", "drop", "delete", "minus",
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
# "change to 2020-2025", "2020-2025", "2020 to 2025" (with hyphen or dash)
_TIME_RANGE_RE = re.compile(
    r"\b(19\d{2}|20\d{2})\s*[-–—]\s*(19\d{2}|20\d{2})\b",
)

# Additive / replacement / removal markers
_ADDITIVE_MARKERS = {"compare", "add", "also", "include", "plus", "too", "well"}
_REPLACEMENT_MARKERS = {"only", "just", "filter", "keep"}
_REMOVAL_MARKERS = {"remove", "exclude", "without", "drop", "delete", "minus"}


class DeltaExtractor:
    """Extract FollowUpDelta from a query given conversation state.

    Two-tier extraction:
    1. Fast deterministic regex handlers for structurally unambiguous patterns
    2. LLM-based extraction for everything else (natural language, compound changes)
    """

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
            return None

        query_text = str(query or "").strip()
        if not query_text:
            return None

        # Fast structural handlers — only for unambiguous patterns.
        # Dimension and indicator changes are handled by the LLM (Tier 2)
        # because regex can't distinguish "seniors" (age dimension) from
        # "seniors" (indicator switch), or "trade balance" (indicator) from
        # "wholesale trade" (GDP dimension).
        delta = self._try_country_only_follow_up(query_text, state)
        if delta:
            return delta

        delta = self._try_time_change(query_text, state)
        if delta:
            return delta

        delta = self._try_provider_change(query_text, state)
        if delta:
            return delta

        # Dimension modifier and indicator switch are now handled by
        # LLM delta extraction (extract_with_llm) for better accuracy.
        # The regex handlers remain as code but are bypassed here.
        return None

    async def extract_with_llm(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """LLM-based delta extraction for queries the regex handlers can't parse.

        Uses structured output (Instructor + Pydantic) to have the LLM
        populate only the changed fields in FollowUpDelta.
        """
        if not state.indicator:
            return None

        query_text = str(query or "").strip()
        if not query_text:
            return None

        # Get the Instructor client from the query service's openrouter
        openrouter = getattr(self._qs, "openrouter", None)
        if openrouter is None:
            logger.debug("LLM delta: no openrouter available")
            return None

        instructor_client = getattr(openrouter, "instructor_client", None)
        instructor_model = getattr(openrouter, "instructor_model", None)
        if instructor_client is None or instructor_model is None:
            logger.debug("LLM delta: instructor client not available")
            return None

        # Build state description for the prompt
        state_lines = []
        if state.indicator:
            state_lines.append(f"  Indicator: {state.indicator}")
        if state.country:
            state_lines.append(f"  Country: {state.country}")
        if state.countries:
            state_lines.append(f"  Countries: {', '.join(state.countries)}")
        if state.provider or state.routed_provider:
            state_lines.append(f"  Provider: {state.provider or state.routed_provider}")
        if state.start_date:
            state_lines.append(f"  Start date: {state.start_date}")
        if state.end_date:
            state_lines.append(f"  End date: {state.end_date}")
        if state.dimensions:
            state_lines.append(f"  Dimensions: {state.dimensions}")
        if state.chart_type:
            state_lines.append(f"  Chart type: {state.chart_type}")
        if state.trade_flow:
            state_lines.append(f"  Trade flow: {state.trade_flow}")
        if state.trade_reporter:
            state_lines.append(f"  Trade reporter: {state.trade_reporter}")
        if state.trade_partner:
            state_lines.append(f"  Trade partner: {state.trade_partner}")
        state_text = "\n".join(state_lines) if state_lines else "  (empty)"

        # Add available dimension members if StatsCan cube metadata is cached.
        # Show exact member names so the LLM can use them directly.
        # We include top-level members PLUS any members matching the query
        # to avoid truncation hiding valid dimension values (e.g., "Energy"
        # at index 344 of 359 in CPI's "Products and product groups").
        dimension_context = ""
        if state.statscan_cube_metadata:
            dims = state.statscan_cube_metadata.get("dimension", [])
            if dims:
                dim_lines = ["\nAvailable dimensions for this indicator (use EXACT member names):"]
                _query_tokens = {t.lower() for t in query_text.split() if t.lower() not in _FILLER_WORDS and len(t) > 2}
                for dim in dims:
                    name = dim.get("dimensionNameEn", "?")
                    all_members = [m.get("memberNameEn", "?") for m in (dim.get("member") or [])]
                    if not all_members:
                        continue
                    # Always show top-level members (first 20)
                    shown = list(all_members[:20])
                    # Add any members that fuzzy-match query tokens (case-insensitive substring)
                    if _query_tokens:
                        for member in all_members[20:]:
                            member_lower = member.lower()
                            if any(tok in member_lower for tok in _query_tokens):
                                if member not in shown:
                                    shown.append(member)
                    # Cap at 50 to keep prompt reasonable
                    if len(shown) > 50:
                        shown = shown[:50]
                    truncated = " (truncated, more available)" if len(all_members) > len(shown) else ""
                    dim_lines.append(f"  {name}: {', '.join(shown)}{truncated}")
                dimension_context = "\n".join(dim_lines)

        system_prompt = f"""You are a delta extractor for an economic data query system.

Given the user's CURRENT conversation state and their NEW follow-up message,
determine ONLY what changed. Output a FollowUpDelta JSON where you populate
ONLY the fields that the user wants to modify. Leave everything else null.

STEP 1 — CLASSIFY the query into one of these types:
- "parameter_delta": Simple modification (country, time, indicator, dimension, provider, chart type)
- "pro_mode": Complex analysis needing code execution (correlation, regression, scatter plot, forecast, statistics)
- "new_query": Completely unrelated new topic
- "clarification_answer": Answering a system question (picking options, "yes", "the first one")
- "informational": Question about the system ("what sources do you have?")

Set the query_type field accordingly.

STEP 2 — If query_type is "parameter_delta", populate the changed fields:
1. Only populate fields the user explicitly wants to change.
2. For countries:
   - changed_country: REPLACE current country with a new one
   - changed_countries: REPLACE with multiple countries
   - added_countries: "add", "also", "compare with", "include" → ADDITIVE
   - removed_countries: "remove", "exclude", "without" → SUBTRACTIVE
3. For dimensions (sub-categories like sex, age group, province, product type):
   - added_dimensions: dict of {{dimension_name: value}} where value is either:
     a) A SPECIFIC member name to FILTER by (e.g., "Ontario", "Females", "15 to 24 years")
     b) The CATEGORY name when the user wants to see ALL items (e.g., "province", "age group")
   - IMPORTANT: When the user says "by province" / "show by province" / "break down by province"
     WITHOUT naming a specific province, the value MUST be "province" (the category), NOT a
     specific province like "Ontario". This means "show data for ALL provinces".
   - Similarly: "by age group" → value="age group", "by sex" → value="sex" (show all).
   - But "show Ontario" / "for Ontario" → value="Ontario" (specific filter).
   - "show youth" → age_group = "15 to 24 years" (use exact member name if available below).
     "show seniors" / "55+" → age_group = "55 years and over".
   - When available dimension members are listed below, use the EXACT member name.
   - is_dimension_modifier_change: true when changing dimensions.
4. If the query is completely unrelated to prior context, set is_new_query=true, query_type="new_query".
5. Set delta_type to one of: country_change, additive_country, time_change, indicator_switch, provider_change, dimension_change, chart_change, new_query, compound_change
6. For time changes: use ISO format dates (YYYY-MM-DD). "last N years" = start_date N years before today.
7. "Compare X and Y" or "Compare with Y" when X is already shown → ADDITIVE for geography/countries.
8. "What about X" / "show X instead" where X is a different economic concept → changed_indicator (replaces).
9. "break it down by X" / "filter by X" / "by sex" / "by age" / "show by province" → dimension_change.
   - CRITICAL: "show by province" / "by province" / "break down by province" (no specific province named)
     → added_dimensions = {{"Geography": "province"}} (category name, NOT a specific province like "Ontario")
   - "show for Ontario" → added_dimensions = {{"Geography": "Ontario"}} (specific filter)
10. "also show X" / "add X" / "include X" / "and also X" where X is a DIFFERENT indicator → added_indicators (list). This ADDS to the existing indicators, not replaces. Example: after "US GDP", "also show inflation" → added_indicators=["inflation"].

IMPORTANT DISTINCTIONS:
- "Show unemployment instead" → changed_indicator (replaces current)
- "Also show unemployment" → added_indicators (adds alongside current)
- "Correlate unemployment with GDP" → pro_mode (needs computation)
- "Show as bar chart" → parameter_delta (chart type)
- "Show me a scatter plot" → pro_mode (needs code)
- "Show by province" → dimension_change with Geography="province" (ALL provinces, not one specific)
- "Show for Ontario" → dimension_change with Geography="Ontario" (specific province filter)

CRITICAL RULE — DIMENSION vs INDICATOR SWITCH vs COUNTRY CHANGE:
If available dimension members are listed below, and the user's query term matches
or closely relates to one of those members, this is a DIMENSION CHANGE (added_dimensions),
NOT an indicator switch or country change.

Examples:
- CPI has "Products and product groups" with "Food", "Energy" → "show energy" = added_dimensions
- Unemployment has "Geography" with "Ontario", "Quebec" → "show for Ontario" = added_dimensions with Geography=Ontario, NOT changed_country
- Unemployment has "Geography" with provinces → "show by province" = added_dimensions with Geography="province" (ALL provinces)
- Unemployment has "Sex" with "Males", "Females" → "show female" = added_dimensions with Sex value

Canadian provinces (Ontario, Quebec, BC, Alberta, etc.) appearing in Geography dimension members
should ALWAYS be added_dimensions, NEVER changed_country. They are sub-national filters, not countries.

CURRENT STATE:
{state_text}
{dimension_context}

Output the query_type and any changed fields as JSON."""

        try:
            delta = await instructor_client.chat.completions.create(
                model=instructor_model,
                response_model=FollowUpDelta,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query_text},
                ],
                temperature=0.1,
                max_tokens=2000,  # Reasoning models need 3-5x headroom for JSON output
            )

            # For non-parameter_delta classifications (pro_mode, new_query,
            # informational, clarification_answer), return the delta even
            # with no changed fields — the query_type is the important signal.
            if delta.query_type and delta.query_type != "parameter_delta":
                delta.raw_query = query_text
                logger.info("LLM classification: query_type=%s for: %s",
                            delta.query_type, query_text[:50])
                return delta

            # For parameter_delta: at least one field must be non-None AND non-empty.
            # LLM may return empty strings/dicts/lists which are truthy but meaningless.
            def _has_value(val: object) -> bool:
                if val is None:
                    return False
                if isinstance(val, (str, dict, list)) and not val:
                    return False
                return True

            has_change = any(
                _has_value(getattr(delta, f))
                for f in [
                    "changed_indicator", "added_indicators",
                    "changed_country", "changed_countries",
                    "added_countries", "removed_countries", "changed_provider",
                    "changed_start_date", "changed_end_date",
                    "added_dimensions", "removed_dimensions",
                    "changed_chart_type", "changed_trade_flow",
                    "changed_trade_reporter", "changed_trade_partner",
                    "changed_trade_commodity",
                ]
            ) or delta.is_new_query

            if not has_change:
                logger.info("LLM delta: no changes detected, returning None")
                return None

            delta.raw_query = query_text
            logger.info(
                "LLM Delta: type=%s, changes=%s",
                delta.delta_type,
                {k: v for k, v in delta.model_dump(exclude_none=True).items()
                 if k not in ("raw_query", "delta_type", "is_new_query",
                              "is_dimension_modifier_change")},
            )
            return delta

        except Exception as exc:
            logger.warning("LLM delta extraction failed: %s", exc)
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

        # Determine additive vs. removal vs. replacement
        query_words = set(query_lower.split())
        is_removal = bool(query_words & _REMOVAL_MARKERS)
        is_additive = bool(query_words & _ADDITIVE_MARKERS) and not bool(
            query_words & _REPLACEMENT_MARKERS
        ) and not is_removal

        if is_removal:
            logger.info(
                "Delta: removal country follow-up %s",
                target_countries,
            )
            return FollowUpDelta(
                removed_countries=target_countries,
                raw_query=query,
                delta_type="country_change",
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

        # Use cached cube metadata from conversation state (pre-populated
        # after first successful StatsCan query — no async API call needed).
        # Fall back to async fetch in a thread if cache is missing.
        cube_metadata = state.statscan_cube_metadata
        if not cube_metadata:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
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
                    product_id, exc,
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
            "past", "recent", "latest", "change", "switch", "update", "set",
            "period", "range", "time", "dates", "date", "timeframe",
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

        # Try "YYYY-YYYY" / "YYYY–YYYY" (range with hyphen/dash)
        m = _TIME_RANGE_RE.search(query)
        if m:
            start_year = m.group(1)
            end_year = m.group(2)
            logger.info("Delta: time change → %s-%s", start_year, end_year)
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
