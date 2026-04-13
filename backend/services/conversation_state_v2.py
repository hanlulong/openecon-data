"""FollowUpDelta + Merge conversation state architecture.

Phase 1: New models + state tracking.

ConversationState is the single source of truth for accumulated conversation
semantics.  FollowUpDelta captures what changed between turns.  merge_state()
applies a delta to produce a new ConversationState.  materialize_intent()
converts a ConversationState into a ParsedIntent for execution.

All functions are pure (no side effects) and fully unit-testable.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from ..models import ParsedIntent
from ..utils.providers import normalize_provider_name

logger = logging.getLogger(__name__)


_DECOMPOSITION_VALUE_TO_TYPE = {
    "province": "provinces",
    "provinces": "provinces",
    "state": "states",
    "states": "states",
    "region": "regions",
    "regions": "regions",
    "country": "countries",
    "countries": "countries",
}

_GEOGRAPHY_DIMENSION_KEYS = {
    "geography",
    "province",
    "provinces",
    "state",
    "states",
    "region",
    "regions",
    "country",
    "countries",
}

_CRYPTO_INDICATOR_TO_COIN_ID = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    "solana": "solana",
    "sol": "solana",
    "cardano": "cardano",
    "ada": "cardano",
    "ripple": "ripple",
    "xrp": "ripple",
}


def _normalize_country_to_iso2(name: str) -> str:
    """Normalize a country name/code to ISO2 for reliable comparison.

    Handles full names ('Germany'), ISO2 ('DE'), ISO3 ('DEU'), and common
    aliases.  Returns uppercase ISO2 code when possible, otherwise the
    original string lowercased (so comparisons still work for unknown names).
    """
    from ..routing.country_resolver import CountryResolver

    key = name.strip().lower()
    # Direct alias lookup (covers full names, ISO2, ISO3)
    iso2 = CountryResolver.COUNTRY_ALIASES.get(key)
    if iso2:
        return iso2.upper()
    # Already an ISO2 code (2-letter uppercase)?
    if len(name.strip()) == 2 and name.strip().isalpha():
        return name.strip().upper()
    return key


def _indicator_to_coin_id(indicator: Optional[str]) -> Optional[str]:
    """Return the CoinGecko asset id implied by an indicator label, if any."""
    indicator_text = str(indicator or "").lower().strip()
    if not indicator_text:
        return None
    parts = indicator_text.split()
    if parts and parts[-1] in {"price", "coin", "crypto", "token", "currency"}:
        parts = parts[:-1]
    indicator_key = " ".join(parts).strip()
    return _CRYPTO_INDICATOR_TO_COIN_ID.get(indicator_key)


def _interpret_dimension_breakdown(
    dimensions: Optional[Dict[str, str]],
) -> tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]], bool]:
    """Split dimension filters from first-class decomposition semantics.

    Returns:
        remaining_dimensions,
        decomposition_payload,
        has_specific_geography_filter
    """
    if not dimensions:
        return None, None, False

    remaining: Dict[str, str] = {}
    decomposition: Optional[Dict[str, Any]] = None
    has_specific_geography_filter = False

    for raw_key, raw_value in dimensions.items():
        key = str(raw_key or "").strip()
        value = str(raw_value or "").strip()
        key_lower = key.lower()
        value_lower = value.lower()

        if key_lower in _GEOGRAPHY_DIMENSION_KEYS:
            decomp_type = _DECOMPOSITION_VALUE_TO_TYPE.get(value_lower)
            if decomposition is None and decomp_type:
                decomposition = {
                    "type": decomp_type,
                    "entities": None,
                    "axis": key,
                }
                continue
            has_specific_geography_filter = True

        remaining[key] = value

    return (remaining or None), decomposition, has_specific_geography_filter


# ---------------------------------------------------------------------------
# ConversationState
# ---------------------------------------------------------------------------

class ConversationState(BaseModel):
    """Structured state persisted across turns.  Single source of truth.

    Unlike ParsedIntent (which is an execution plan with provider-specific
    parameters), ConversationState captures the user's semantic intent in
    a provider-agnostic way.  It accumulates across turns.
    """

    # --- Core semantic fields ---
    indicator: Optional[str] = None
    base_indicator: Optional[str] = None
    # Provider-specific resolved indicator code (e.g., "NY.GDP.PCAP.CD" for
    # WorldBank, "CPIAUCSL" for FRED).  Populated after a successful fetch so
    # that follow-up turns that keep the same indicator can skip re-resolution
    # and avoid drifting to a different code.
    resolved_indicator_code: Optional[str] = None
    country: Optional[str] = None
    countries: Optional[List[str]] = None
    provider: Optional[str] = None
    provider_locked: bool = False
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    # --- Dimension modifiers (StatsCan, Eurostat sub-categories) ---
    dimensions: Optional[Dict[str, str]] = None
    # Cached product ID and cube metadata for dimension-capable indicators.
    # Pre-populated after first successful StatsCan query so the delta
    # extractor can check dimension members without async API calls.
    statscan_product_id: Optional[str] = None
    statscan_cube_metadata: Optional[Dict[str, Any]] = None

    # --- Chart / display preferences ---
    chart_type: Optional[str] = None

    # --- Decomposition state ---
    decomposition: Optional[Dict[str, Any]] = None

    # --- Trade-specific fields ---
    trade_flow: Optional[str] = None
    trade_reporter: Optional[str] = None
    trade_partner: Optional[str] = None
    trade_commodity: Optional[str] = None

    # --- Crypto-specific fields ---
    coin_ids: Optional[List[str]] = None
    vs_currency: Optional[str] = None

    # --- Provenance ---
    original_query: Optional[str] = None
    turn_number: int = 0
    routed_provider: Optional[str] = None
    last_indicators_resolved: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# FollowUpDelta
# ---------------------------------------------------------------------------

class FollowUpDelta(BaseModel):
    """What changed relative to the previous turn.

    Produced by delta extraction (Phase 1).  Every field is Optional;
    only populated fields represent changes.  The merge function applies
    these changes to the current ConversationState.
    """

    # --- What changed ---
    changed_indicator: Optional[str] = None
    added_indicators: Optional[List[str]] = None
    changed_country: Optional[str] = None
    changed_countries: Optional[List[str]] = None
    added_countries: Optional[List[str]] = None
    removed_countries: Optional[List[str]] = None
    changed_provider: Optional[str] = None
    changed_start_date: Optional[str] = None
    changed_end_date: Optional[str] = None
    added_dimensions: Optional[Dict[str, str]] = None
    removed_dimensions: Optional[List[str]] = None
    changed_chart_type: Optional[str] = None
    changed_decomposition: Optional[Dict[str, Any]] = None
    changed_trade_flow: Optional[str] = None
    changed_trade_reporter: Optional[str] = None
    changed_trade_partner: Optional[str] = None
    changed_trade_commodity: Optional[str] = None

    # --- Meta ---
    is_new_query: bool = False
    is_dimension_modifier_change: bool = False
    raw_query: Optional[str] = None

    # --- Classification ---
    delta_type: Optional[str] = None
    # Combined classification: the LLM delta extractor also classifies
    # the query type in a single call, eliminating the need for a separate
    # classifier LLM call. Values: parameter_delta, pro_mode, new_query,
    # clarification_answer, informational.
    query_type: Optional[str] = None


# ---------------------------------------------------------------------------
# merge_state
# ---------------------------------------------------------------------------

def merge_state(current: ConversationState, delta: FollowUpDelta) -> ConversationState:
    """Deterministic merge: apply *delta* to *current* state.

    Rules
    -----
    1. ``is_new_query`` → fresh ConversationState populated only from delta.
    2. ``changed_indicator`` (not dimension modifier) → clear dimensions.
    3. ``changed_country`` / ``changed_countries`` → clear decomposition.
    4. ``added_countries`` / ``removed_countries`` → merge into existing list.
    5. ``added_dimensions`` / ``removed_dimensions`` → merge into existing dict.
    """

    if delta.is_new_query:
        initial_dimensions, decomposition_from_dimensions, _ = _interpret_dimension_breakdown(
            delta.added_dimensions
        )
        new_state = ConversationState(
            indicator=delta.changed_indicator,
            country=delta.changed_country,
            countries=delta.changed_countries,
            provider=delta.changed_provider,
            provider_locked=bool(delta.changed_provider),
            start_date=delta.changed_start_date,
            end_date=delta.changed_end_date,
            chart_type=delta.changed_chart_type,
            decomposition=delta.changed_decomposition or decomposition_from_dimensions,
            trade_flow=delta.changed_trade_flow,
            trade_reporter=delta.changed_trade_reporter,
            trade_partner=delta.changed_trade_partner,
            trade_commodity=delta.changed_trade_commodity,
            original_query=delta.raw_query,
            turn_number=current.turn_number + 1,
        )
        if initial_dimensions:
            new_state.dimensions = dict(initial_dimensions)
        return new_state

    merged = current.model_copy(deep=True)
    merged.turn_number = current.turn_number + 1
    merged.original_query = delta.raw_query or current.original_query

    # --- Indicator ---
    if delta.changed_indicator:
        merged.indicator = delta.changed_indicator
        merged.base_indicator = None
        merged.resolved_indicator_code = None
        merged.last_indicators_resolved = None
        merged.coin_ids = None
        if not delta.is_dimension_modifier_change:
            merged.dimensions = None
            merged.statscan_cube_metadata = None  # Clear stale cube metadata when indicator changes
        # Auto-detect crypto indicator switches and update coin_ids/provider
        _coin = _indicator_to_coin_id(delta.changed_indicator)
        if _coin:
            merged.coin_ids = [_coin]
            merged.provider = "COINGECKO"
            merged.routed_provider = "COINGECKO"

    # Additive indicators: "also show inflation" adds to existing indicators
    if delta.added_indicators:
        existing = merged.last_indicators_resolved or ([merged.indicator] if merged.indicator else [])
        merged_list = list(dict.fromkeys(existing + delta.added_indicators))
        merged.last_indicators_resolved = merged_list
        # Keep the first indicator as the primary
        if not merged.indicator and merged_list:
            merged.indicator = merged_list[0]
        crypto_coin_ids = list(merged.coin_ids or [])
        for indicator in delta.added_indicators:
            coin_id = _indicator_to_coin_id(indicator)
            if coin_id and coin_id not in crypto_coin_ids:
                crypto_coin_ids.append(coin_id)
        if crypto_coin_ids:
            merged.coin_ids = crypto_coin_ids
            merged.provider = "COINGECKO"
            merged.routed_provider = "COINGECKO"

    # --- Country ---
    if delta.changed_country:
        merged.country = delta.changed_country
        merged.countries = None
        merged.decomposition = None
    if delta.changed_countries:
        merged.countries = delta.changed_countries
        merged.country = None
        merged.decomposition = None

    # Additive: merge new countries into existing list
    if delta.added_countries:
        existing = merged.countries or ([merged.country] if merged.country else [])
        merged_list = list(dict.fromkeys(existing + delta.added_countries))
        if len(merged_list) == 1:
            merged.country = merged_list[0]
            merged.countries = None
        else:
            merged.countries = merged_list
            merged.country = None

    # Subtractive: remove countries from existing list
    # Uses ISO2 normalization so "Germany", "DE", "DEU" all match.
    if delta.removed_countries:
        existing = merged.countries or ([merged.country] if merged.country else [])
        removed_iso2 = {_normalize_country_to_iso2(c) for c in delta.removed_countries}
        remaining = [c for c in existing if _normalize_country_to_iso2(c) not in removed_iso2]
        if len(remaining) == 1:
            merged.country = remaining[0]
            merged.countries = None
        elif len(remaining) > 1:
            merged.countries = remaining
            merged.country = None
        elif len(remaining) == 0 and existing:
            # All countries removed — keep the last state to avoid empty result
            logger.warning("merge_state: all countries removed, keeping last state")
        # If empty after removal, keep the last state (edge case)

    # --- Provider ---
    if delta.changed_provider:
        merged.provider = delta.changed_provider
        merged.provider_locked = True
        merged.resolved_indicator_code = None
        merged.last_indicators_resolved = None

    # --- Time ---
    if delta.changed_start_date:
        merged.start_date = delta.changed_start_date
    if delta.changed_end_date:
        merged.end_date = delta.changed_end_date

    # --- Dimensions ---
    if delta.added_dimensions:
        remaining_dimensions, decomposition_from_dimensions, has_specific_geography_filter = (
            _interpret_dimension_breakdown(delta.added_dimensions)
        )
        if remaining_dimensions:
            merged.dimensions = {**(merged.dimensions or {}), **remaining_dimensions}
        if decomposition_from_dimensions and delta.changed_decomposition is None:
            merged.decomposition = decomposition_from_dimensions
        elif has_specific_geography_filter and merged.decomposition is not None:
            merged.decomposition = None
    if delta.removed_dimensions:
        if merged.dimensions:
            for key in delta.removed_dimensions:
                merged.dimensions.pop(key, None)
            if not merged.dimensions:
                merged.dimensions = None

    # --- Chart type ---
    if delta.changed_chart_type:
        merged.chart_type = delta.changed_chart_type

    # --- Decomposition ---
    if delta.changed_decomposition is not None:
        merged.decomposition = delta.changed_decomposition

    # --- Trade fields ---
    if delta.changed_trade_flow:
        merged.trade_flow = delta.changed_trade_flow
    if delta.changed_trade_reporter:
        merged.trade_reporter = delta.changed_trade_reporter
    if delta.changed_trade_partner:
        merged.trade_partner = delta.changed_trade_partner
    if delta.changed_trade_commodity:
        merged.trade_commodity = delta.changed_trade_commodity

    return merged


# ---------------------------------------------------------------------------
# materialize_intent
# ---------------------------------------------------------------------------

def materialize_intent(state: ConversationState) -> ParsedIntent:
    """Convert accumulated ConversationState into an executable ParsedIntent.

    This is the ONLY place where state -> intent conversion happens.
    All follow-up handlers feed into merge_state; only this function
    produces the ParsedIntent for execution.
    """

    parameters: Dict[str, Any] = {}

    # Geography
    if state.countries and len(state.countries) > 1:
        parameters["countries"] = state.countries
    elif state.country:
        parameters["country"] = state.country
    elif state.countries and len(state.countries) == 1:
        parameters["country"] = state.countries[0]

    # Time
    if state.start_date:
        parameters["startDate"] = state.start_date
    if state.end_date:
        parameters["endDate"] = state.end_date

    # Trade
    if state.trade_reporter:
        parameters["reporter"] = state.trade_reporter
    if state.trade_partner:
        parameters["partner"] = state.trade_partner
    if state.trade_flow:
        parameters["flow"] = state.trade_flow
    if state.trade_commodity:
        parameters["commodity"] = state.trade_commodity

    # Crypto
    if state.coin_ids:
        parameters["coinIds"] = state.coin_ids
    if state.vs_currency:
        parameters["vsCurrency"] = state.vs_currency
    if (
        not parameters.get("coinIds")
        and (state.provider or state.routed_provider or "").upper() == "COINGECKO"
    ):
        derived_coin_ids: list[str] = []
        for indicator in state.last_indicators_resolved or ([state.indicator] if state.indicator else []):
            coin_id = _indicator_to_coin_id(indicator)
            if coin_id and coin_id not in derived_coin_ids:
                derived_coin_ids.append(coin_id)
        if derived_coin_ids:
            parameters["coinIds"] = derived_coin_ids

    # Dimensions (Phase 3: pass through to data_fetcher for StatsCan etc.)
    if state.dimensions:
        parameters["__dimensions"] = state.dimensions
        # Include the base indicator key so fetch_with_dimensions knows
        # which table/vector to apply modifiers to (e.g., "UNEMPLOYMENT_RATE")
        if state.base_indicator:
            parameters["__base_indicator"] = state.base_indicator
            # Also set indicator in params so data_fetcher uses the vector key
            parameters["indicator"] = state.base_indicator

    # Indicator: use base_indicator (vector key like "UNEMPLOYMENT_RATE") when
    # dimensions are active, so the StatsCan provider routes correctly.
    # When a resolved_indicator_code is available (from a prior successful
    # fetch) and the indicator hasn't changed, use the resolved code to
    # prevent re-resolution drift (e.g., "GDP per capita" re-resolving to
    # total GDP instead of per-capita GDP).
    if state.dimensions and state.base_indicator:
        indicators = [state.base_indicator]
    elif state.last_indicators_resolved and len(state.last_indicators_resolved) > 1:
        # Multi-indicator: use the full list from prior turn
        indicators = list(state.last_indicators_resolved)
    elif state.resolved_indicator_code:
        indicators = [state.resolved_indicator_code]
    elif state.indicator:
        indicators = [state.indicator]
    else:
        indicators = ["unknown"]

    # Persist the resolved indicator code into parameters["indicator"] so that
    # the indicator resolution pipeline sees it on follow-up turns and does not
    # re-resolve (which causes drift).  Without this, materialize_intent only
    # set parameters["indicator"] for dimension queries, leaving it empty for
    # normal follow-ups and forcing an unnecessary re-resolution cycle.
    if state.resolved_indicator_code and "indicator" not in parameters:
        parameters["indicator"] = state.resolved_indicator_code

    # Provider: use explicit provider if set, otherwise default
    provider = state.provider or state.routed_provider or "WorldBank"
    if state.provider_locked:
        parameters["__semantic_provider_locked"] = True

    # Decomposition
    needs_decomp = state.decomposition is not None
    decomp_type = state.decomposition.get("type") if state.decomposition else None
    decomp_entities = state.decomposition.get("entities") if state.decomposition else None

    return ParsedIntent(
        apiProvider=provider,
        indicators=indicators,
        parameters=parameters,
        clarificationNeeded=False,
        confidence=0.9,
        recommendedChartType=state.chart_type or "line",
        originalQuery=state.original_query,
        needsDecomposition=needs_decomp,
        decompositionType=decomp_type,
        decompositionEntities=decomp_entities,
        isFollowUp=state.turn_number > 0,
    )


# ---------------------------------------------------------------------------
# extract_state_from_intent  (for dual-write migration)
# ---------------------------------------------------------------------------

def extract_state_from_intent(intent: ParsedIntent, statscan_provider=None) -> ConversationState:
    """Build a ConversationState from a ParsedIntent (backward-compat helper).

    Used during the dual-write migration phase: after every successful query
    that produces a ParsedIntent, we also build a ConversationState so that
    both ``last_intent`` and ``state`` are kept in sync.
    """

    params = intent.parameters or {}

    # Geography
    country: Optional[str] = None
    countries: Optional[List[str]] = None
    raw_countries = params.get("countries")
    raw_country = params.get("country")
    if raw_countries and isinstance(raw_countries, list) and len(raw_countries) > 1:
        countries = [str(c) for c in raw_countries]
    elif raw_countries and isinstance(raw_countries, list) and len(raw_countries) == 1:
        country = str(raw_countries[0])
    elif raw_country:
        country = str(raw_country)

    # Time
    start_date = params.get("startDate")
    end_date = params.get("endDate")

    # Indicator
    indicator: Optional[str] = None
    if intent.indicators:
        indicator = intent.indicators[0]

    # Resolved indicator code: the provider-specific code (e.g., "NY.GDP.PCAP.CD")
    # populated by the data_fetcher's resolution pipeline. This is stored
    # separately from ``indicator`` (which is the human-readable name) so that
    # follow-up turns can reuse the exact code without re-resolution drift.
    resolved_indicator_code: Optional[str] = None
    _params_indicator = params.get("indicator")
    if _params_indicator:
        # Always store the resolved code from the data fetch pipeline.
        # This is the provider-specific code (e.g., NY.GDP.PCAP.CD) that
        # should be reused on follow-up turns to prevent indicator drift.
        resolved_indicator_code = str(_params_indicator).strip() or None

    # Fallback: if params["indicator"] was not set (e.g., some providers or
    # code paths skip the resolution pipeline), but intent.indicators[0]
    # looks like a provider-specific code (contains dots, underscores, or is
    # all uppercase like "CPIAUCSL"), capture it as the resolved code.
    # This ensures the first turn ALWAYS persists the resolved code.
    if not resolved_indicator_code and indicator:
        _ind = indicator.strip()
        _looks_like_code = (
            "." in _ind           # e.g., NY.GDP.PCAP.CD, SL.UEM.TOTL.ZS
            or "_" in _ind        # e.g., UNEMPLOYMENT_RATE
            or _ind.isupper()     # e.g., CPIAUCSL, GDP, CPI
        )
        if _looks_like_code:
            resolved_indicator_code = _ind

    # Trade
    trade_flow = params.get("flow")
    trade_reporter = params.get("reporter")
    trade_partner = params.get("partner")
    trade_commodity = params.get("commodity")

    # Crypto
    coin_ids = params.get("coinIds")
    vs_currency = params.get("vsCurrency")

    # Dimensions (from delta/merge path via __dimensions)
    dimensions, decomposition_from_dimensions, _ = _interpret_dimension_breakdown(
        params.get("__dimensions")
    )

    # Decomposition
    decomposition: Optional[Dict[str, Any]] = None
    if intent.needsDecomposition and intent.decompositionType:
        decomposition = {
            "type": intent.decompositionType,
            "entities": intent.decompositionEntities,
        }
    elif decomposition_from_dimensions:
        decomposition = decomposition_from_dimensions

    # Infer base_indicator (vector mapping key) from the indicator name.
    # For StatsCan, the indicator resolved by the catalog is often the vector
    # key (e.g., "UNEMPLOYMENT_RATE", "CPI", "GDP"). Check if it matches a
    # known VECTOR_MAPPINGS or COORDINATE_PRODUCT_MAPPINGS key.
    base_indicator: Optional[str] = None
    if indicator:
        _key = indicator.upper().replace(" ", "_").replace("-", "_")
        # Check if this is already a vector key (e.g., from catalog resolution)
        try:
            from ..providers.statscan import StatsCanProvider
            if (_key in StatsCanProvider.VECTOR_MAPPINGS
                    or _key in StatsCanProvider.COORDINATE_PRODUCT_MAPPINGS):
                base_indicator = _key
            # Framework fix: if _key is a numeric table/product ID (e.g., "14100287"),
            # reverse-lookup through PRODUCT_ID_CACHE to find the vector mapping key.
            # This ensures base_indicator is set for dimension follow-ups.
            # Prefer the longest (most specific) key, e.g., "UNEMPLOYMENT_RATE" over
            # "UNEMPLOYMENT", since longer names carry more semantic precision for
            # downstream coordinate building.
            elif _key.isdigit():
                _numeric_id = int(_key)
                _cached_product = StatsCanProvider.PRODUCT_ID_CACHE.get(_numeric_id)
                if _cached_product:
                    _normalized_product = StatsCanProvider._normalize_metadata_product_id(_cached_product)
                    _candidates = []
                    for _vec_key, _vec_id in StatsCanProvider.VECTOR_MAPPINGS.items():
                        if _vec_id is None:
                            continue
                        _vec_product = StatsCanProvider.PRODUCT_ID_CACHE.get(_vec_id)
                        if _vec_product and StatsCanProvider._normalize_metadata_product_id(_vec_product) == _normalized_product:
                            _candidates.append(_vec_key)
                    if _candidates:
                        # Prefer the longest key (most specific, e.g., UNEMPLOYMENT_RATE > UNEMPLOYMENT)
                        base_indicator = max(_candidates, key=len)
        except Exception:
            pass
        # Also check if the params had __base_indicator from a prior delta path
        if not base_indicator and params.get("__base_indicator"):
            base_indicator = params["__base_indicator"]

    # Pre-resolve StatsCan product ID and cube metadata for dimension follow-ups.
    # Also try to read the provider's in-memory cube metadata cache (populated
    # during R1's data fetch). This avoids async API calls in the delta extractor.
    statscan_product_id: Optional[str] = None
    statscan_cube_metadata_val: Optional[Dict[str, Any]] = None
    if base_indicator:
        try:
            from ..providers.statscan import StatsCanProvider
            _vec = StatsCanProvider.VECTOR_MAPPINGS.get(base_indicator)
            _coord = StatsCanProvider.COORDINATE_PRODUCT_MAPPINGS.get(base_indicator)
            if _coord:
                statscan_product_id = str(_coord[0])[:8]
            elif _vec is not None:
                _cached_pid = StatsCanProvider.PRODUCT_ID_CACHE.get(_vec)
                if _cached_pid:
                    statscan_product_id = str(_cached_pid)[:8]
            # Try reading cube metadata from:
            # 1. Provider's in-memory cache (populated during fetch)
            # 2. Local metadata service file cache (always available)
            if statscan_product_id:
                try:
                    if statscan_provider:
                        _norm_pid = statscan_provider._normalize_metadata_product_id(statscan_product_id)
                        # Check in-memory cache first
                        _cached_cube = statscan_provider._cube_metadata_cache.get(_norm_pid)
                        if _cached_cube:
                            statscan_cube_metadata_val = _cached_cube
                        # Fall back to local file cache (sync, no API call)
                        elif hasattr(statscan_provider, '_statscan_metadata_service') and statscan_provider._statscan_metadata_service:
                            _local = statscan_provider._statscan_metadata_service.get_local_cube_metadata(_norm_pid)
                            if _local:
                                statscan_cube_metadata_val = _local
                                # Also populate the in-memory cache for next time
                                statscan_provider._cube_metadata_cache[_norm_pid] = _local
                except Exception:
                    pass
        except Exception:
            pass

    return ConversationState(
        indicator=indicator,
        base_indicator=base_indicator,
        resolved_indicator_code=resolved_indicator_code,
        dimensions=dimensions,
        statscan_product_id=statscan_product_id,
        statscan_cube_metadata=statscan_cube_metadata_val,
        country=country,
        countries=countries,
        provider=normalize_provider_name(intent.apiProvider or ""),
        provider_locked=bool(params.get("__semantic_provider_locked")),
        routed_provider=normalize_provider_name(intent.apiProvider or ""),
        start_date=start_date,
        end_date=end_date,
        original_query=intent.originalQuery,
        last_indicators_resolved=list(intent.indicators) if intent.indicators else None,
        trade_flow=trade_flow,
        trade_reporter=trade_reporter,
        trade_partner=trade_partner,
        trade_commodity=trade_commodity,
        coin_ids=coin_ids,
        vs_currency=vs_currency,
        decomposition=decomposition,
        chart_type=intent.recommendedChartType,
    )


# ---------------------------------------------------------------------------
# merge_new_state_with_previous  (context preservation helper)
# ---------------------------------------------------------------------------

def merge_new_state_with_previous(
    new_state: ConversationState,
    previous: Optional[ConversationState],
) -> ConversationState:
    """Carry forward accumulated context from *previous* into *new_state*.

    When a non-delta execution path builds a ConversationState from a
    ParsedIntent (via ``extract_state_from_intent``), the intent may lack
    fields that were established in earlier turns (e.g., the country was
    set in R1 but R2 only changes the indicator).  This function fills
    those gaps so that accumulated context is never silently dropped.

    Fields are carried forward **only when the new state has no value**
    for that field — explicit values in the new state always win.

    This is the single, authoritative place for "don't lose context"
    logic.  Every state-save location outside the delta path should call
    this instead of ad-hoc field-by-field merging.
    """
    if previous is None:
        return new_state

    new_state.turn_number = previous.turn_number + 1
    explicit_geo_in_new_state = bool(new_state.country or new_state.countries)
    indicator_changed = bool(
        new_state.indicator
        and previous.indicator
        and str(new_state.indicator).strip().lower() != str(previous.indicator).strip().lower()
    )

    # --- Geography ---
    _new_has_geo = new_state.country or new_state.countries
    _prev_has_geo = previous.country or previous.countries
    if not _new_has_geo and _prev_has_geo:
        new_state.country = previous.country
        new_state.countries = previous.countries

    # --- Provider ---
    if not new_state.provider and previous.provider:
        new_state.provider = previous.provider
    if not new_state.provider_locked and previous.provider_locked:
        new_state.provider_locked = True
    if not new_state.routed_provider and previous.routed_provider:
        new_state.routed_provider = previous.routed_provider

    # --- Time range ---
    if not new_state.start_date and previous.start_date:
        new_state.start_date = previous.start_date
    if not new_state.end_date and previous.end_date:
        new_state.end_date = previous.end_date

    # --- Indicator resolution ---
    if (
        not indicator_changed
        and not new_state.resolved_indicator_code
        and previous.resolved_indicator_code
    ):
        new_state.resolved_indicator_code = previous.resolved_indicator_code
    if not indicator_changed and not new_state.base_indicator and previous.base_indicator:
        new_state.base_indicator = previous.base_indicator
    if (
        not indicator_changed
        and not new_state.last_indicators_resolved
        and previous.last_indicators_resolved
    ):
        new_state.last_indicators_resolved = previous.last_indicators_resolved

    # --- StatsCanada dimension context ---
    if not new_state.dimensions and previous.dimensions:
        new_state.dimensions = previous.dimensions
    if not indicator_changed and not new_state.statscan_product_id and previous.statscan_product_id:
        new_state.statscan_product_id = previous.statscan_product_id
    if (
        not indicator_changed
        and not new_state.statscan_cube_metadata
        and previous.statscan_cube_metadata
    ):
        new_state.statscan_cube_metadata = previous.statscan_cube_metadata
    if not new_state.decomposition and previous.decomposition:
        _, _, has_specific_geography_filter = _interpret_dimension_breakdown(new_state.dimensions)
        if not has_specific_geography_filter and not explicit_geo_in_new_state:
            new_state.decomposition = previous.decomposition

    # --- Trade fields ---
    if not new_state.trade_flow and previous.trade_flow:
        new_state.trade_flow = previous.trade_flow
    if not new_state.trade_reporter and previous.trade_reporter:
        new_state.trade_reporter = previous.trade_reporter
    if not new_state.trade_partner and previous.trade_partner:
        new_state.trade_partner = previous.trade_partner
    if not new_state.trade_commodity and previous.trade_commodity:
        new_state.trade_commodity = previous.trade_commodity

    # --- Crypto fields ---
    if not indicator_changed and not new_state.coin_ids and previous.coin_ids:
        new_state.coin_ids = previous.coin_ids
    if not new_state.vs_currency and previous.vs_currency:
        new_state.vs_currency = previous.vs_currency

    # --- Display preferences ---
    if not new_state.chart_type and previous.chart_type:
        new_state.chart_type = previous.chart_type

    return new_state
