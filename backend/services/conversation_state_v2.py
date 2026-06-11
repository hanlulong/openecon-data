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

from ..models import NormalizedData, ParsedIntent
from ..utils.providers import normalize_provider_name
from .query_parsing import infer_multi_concept_indicators_from_query

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

_DIMENSION_DECOMPOSITION_AXIS_ALIASES = {
    "age": "Age group",
    "age group": "Age group",
    "age-group": "Age group",
    "ages": "Age group",
    "gender": "Gender",
    "sex": "Gender",
    "genders": "Gender",
    "labour force characteristic": "Labour force characteristics",
    "labour force characteristics": "Labour force characteristics",
    "labor force characteristic": "Labour force characteristics",
    "labor force characteristics": "Labour force characteristics",
    "characteristic": "Labour force characteristics",
    "characteristics": "Labour force characteristics",
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


def _looks_like_provider_code(provider: str, value: Optional[str]) -> bool:
    """Best-effort provider-code detection for persisted answer members."""
    text = str(value or "").strip()
    if not provider or not text:
        return False
    return bool("." in text or "_" in text or text.isupper())


def _normalize_member_country_list(countries: Optional[List[str]]) -> Optional[List[str]]:
    if not countries:
        return None
    deduped = [str(country).strip() for country in countries if str(country or "").strip()]
    if not deduped:
        return None
    return list(dict.fromkeys(deduped))


def _answer_member_key(member: "AnswerSetMember") -> tuple[str, str, str, tuple[str, ...]]:
    provider = normalize_provider_name(member.provider or "")
    provider_code = str(member.provider_code or member.series_id or "").strip().upper()
    indicator_label = str(member.indicator_label or "").strip().lower()
    canonical_identifier = provider_code or indicator_label
    return (
        provider,
        canonical_identifier,
        str(member.country or "").strip().upper(),
        tuple(country.upper() for country in (member.countries or [])),
    )


def _extract_statscan_product_id_from_member(member: "AnswerSetMember") -> Optional[str]:
    if normalize_provider_name(member.provider or "") != "STATSCAN":
        return None
    for candidate in (member.series_id, member.provider_code):
        text = str(candidate or "").strip()
        if not text:
            continue
        head = text.split(":", 1)[0]
        digits = "".join(ch for ch in head if ch.isdigit())
        if len(digits) >= 8:
            return digits[:8]
    return None


def build_answer_set_members_from_data(
    data: Optional[List[NormalizedData]],
    *,
    intent: Optional[ParsedIntent] = None,
    turn_number: int = 0,
) -> List["AnswerSetMember"]:
    """Project successful response data into provider/country/member history."""
    if not data:
        return []

    params = (intent.parameters or {}) if intent is not None else {}
    semantic_indicator_label = str(params.get("__semantic_indicator_label") or "").strip()
    fallback_countries = _normalize_member_country_list(params.get("countries"))
    if not fallback_countries and params.get("country"):
        fallback_countries = [str(params.get("country")).strip()]

    members: List[AnswerSetMember] = []
    for dataset in data:
        metadata = getattr(dataset, "metadata", None)
        provider = normalize_provider_name(str(getattr(metadata, "source", "") or getattr(intent, "apiProvider", "") or ""))
        if not provider:
            continue

        series_id = str(getattr(metadata, "seriesId", "") or "").strip() or None
        indicator_text = str(getattr(metadata, "indicator", "") or "").strip()
        indicator_label = semantic_indicator_label or indicator_text or (
            str((intent.indicators or [""])[0]).strip() if intent is not None else ""
        )
        country = str(getattr(metadata, "country", "") or "").strip() or None
        countries = [country] if country else fallback_countries
        provider_code = series_id
        if provider_code is None and _looks_like_provider_code(provider, indicator_text):
            provider_code = indicator_text

        members.append(
            AnswerSetMember(
                provider=provider,
                indicator_label=indicator_label or None,
                provider_code=provider_code,
                series_id=series_id,
                country=country,
                countries=_normalize_member_country_list(countries),
                source_turn=turn_number or None,
            )
        )

    deduped: List[AnswerSetMember] = []
    seen: set[tuple[str, str, str, tuple[str, ...], str]] = set()
    for member in members:
        key = _answer_member_key(member)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(member)
    return deduped


def _canonical_dimension_decomposition_axis(
    raw_key: str,
    raw_value: str,
) -> Optional[str]:
    """Map a category-style dimension request to a canonical decomposition axis."""
    key_lower = str(raw_key or "").strip().lower()
    value_lower = str(raw_value or "").strip().lower()

    if key_lower in _GEOGRAPHY_DIMENSION_KEYS:
        return None

    key_axis = _DIMENSION_DECOMPOSITION_AXIS_ALIASES.get(key_lower)
    value_axis = _DIMENSION_DECOMPOSITION_AXIS_ALIASES.get(value_lower)
    if key_axis and value_axis and key_axis == value_axis:
        return key_axis

    category_values = {
        value_lower,
        value_lower.removeprefix("by ").strip(),
        value_lower.removesuffix("s").strip(),
    }
    if key_axis and key_lower in category_values:
        return key_axis
    if key_axis and any(value == key_lower for value in category_values):
        return key_axis
    if key_axis and value_axis == key_axis:
        return key_axis
    return None


def _dimensions_target_decomposition_axis(
    dimensions: Optional[Dict[str, str]],
    decomposition: Optional[Dict[str, Any]],
) -> bool:
    """Whether specific dimension filters should replace the current decomposition."""
    if not dimensions or not decomposition:
        return False

    decomp_type = str(decomposition.get("type") or "").strip().lower()
    decomp_axis_raw = str(decomposition.get("axis") or "").strip().lower()
    decomp_axis = str(_DIMENSION_DECOMPOSITION_AXIS_ALIASES.get(decomp_axis_raw, decomp_axis_raw)).strip().lower()
    for raw_key, raw_value in dimensions.items():
        key_lower = str(raw_key or "").strip().lower()
        value_lower = str(raw_value or "").strip().lower()
        if key_lower in _GEOGRAPHY_DIMENSION_KEYS:
            if decomp_type in {"provinces", "states", "regions", "countries"}:
                return True
            continue
        axis = _canonical_dimension_decomposition_axis(key_lower, value_lower)
        if axis is not None:
            continue
        if decomp_type == "dimension" and decomp_axis:
            candidate_axis = _DIMENSION_DECOMPOSITION_AXIS_ALIASES.get(key_lower, key_lower)
            candidate_axis_lower = str(candidate_axis).strip().lower()
            if candidate_axis_lower == decomp_axis:
                return True
            if decomp_axis in {"demographic", "demographics"} and candidate_axis_lower in {
                "age group",
                "gender",
                "labour force characteristics",
            }:
                return True
    return False


def _canonicalize_decomposition_payload(
    decomposition: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Normalize decomposition payloads into the canonical state contract."""
    if decomposition is None:
        return None

    payload = dict(decomposition)
    decomp_type = str(payload.get("type") or "").strip().lower()
    axis = str(payload.get("axis") or "").strip()
    axis_lower = axis.lower()

    if decomp_type in {"age", "age group", "age groups", "age_group", "age_groups", "ages"}:
        payload["type"] = "dimension"
        payload["axis"] = "Age group"
    elif decomp_type in {"sex", "gender", "genders"}:
        payload["type"] = "dimension"
        payload["axis"] = "Gender"
    elif decomp_type == "dimension" and axis:
        canonical_axis = _DIMENSION_DECOMPOSITION_AXIS_ALIASES.get(axis_lower)
        if canonical_axis:
            payload["axis"] = canonical_axis

    return payload


def _strip_dimensions_for_decomposition(
    dimensions: Optional[Dict[str, str]],
    decomposition: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Remove stale member filters that conflict with a new breakdown request."""
    if not dimensions or not decomposition:
        return dimensions

    decomp_type = str(decomposition.get("type") or "").strip().lower()
    decomp_axis_raw = str(decomposition.get("axis") or "").strip().lower()
    decomp_axis = str(_DIMENSION_DECOMPOSITION_AXIS_ALIASES.get(decomp_axis_raw, decomp_axis_raw)).strip().lower()

    cleaned: Dict[str, str] = {}
    for raw_key, raw_value in dimensions.items():
        key = str(raw_key or "").strip()
        key_lower = key.lower()
        value_lower = str(raw_value or "").strip().lower()

        if decomp_type in {"provinces", "states", "regions", "countries"}:
            if key_lower in _GEOGRAPHY_DIMENSION_KEYS:
                continue
            cleaned[key] = str(raw_value)
            continue

        if decomp_type == "dimension":
            canonical_axis = _canonical_dimension_decomposition_axis(key_lower, value_lower)
            if canonical_axis and canonical_axis.lower() == decomp_axis:
                continue
            candidate_axis = _DIMENSION_DECOMPOSITION_AXIS_ALIASES.get(key_lower, key_lower)
            if str(candidate_axis).strip().lower() == decomp_axis:
                continue

        cleaned[key] = str(raw_value)

    return cleaned or None


def update_answer_members_from_data(
    state: ConversationState,
    data: Optional[List[NormalizedData]],
    *,
    intent: Optional[ParsedIntent] = None,
    recent_limit: int = 24,
) -> ConversationState:
    """Persist current and recent answer members after a successful fetch."""
    current_members = build_answer_set_members_from_data(
        data,
        intent=intent,
        turn_number=state.turn_number,
    )
    if not current_members:
        return state

    state.active_answer_members = current_members

    recent_members = list(state.recent_answer_members or [])
    recent_by_key = {_answer_member_key(member): member for member in recent_members}
    ordered_keys = [_answer_member_key(member) for member in recent_members]
    for member in current_members:
        key = _answer_member_key(member)
        recent_by_key[key] = member
        if key in ordered_keys:
            ordered_keys = [existing for existing in ordered_keys if existing != key]
        ordered_keys.append(key)
    if recent_limit > 0:
        ordered_keys = ordered_keys[-recent_limit:]
    state.recent_answer_members = [recent_by_key[key] for key in ordered_keys]

    params = (intent.parameters or {}) if intent is not None else {}
    explicit_statscan_product = "".join(ch for ch in str(params.get("__statscan_product_id") or "") if ch.isdigit())[:8] or None
    latest_statscan_product = explicit_statscan_product
    if not latest_statscan_product:
        for member in current_members:
            latest_statscan_product = _extract_statscan_product_id_from_member(member)
            if latest_statscan_product:
                break
    if latest_statscan_product:
        if state.statscan_product_id != latest_statscan_product:
            state.statscan_cube_metadata = None
        state.statscan_product_id = latest_statscan_product

    latest_coin_ids = [
        str(member.series_id).strip().lower()
        for member in current_members
        if normalize_provider_name(member.provider or "") == "COINGECKO"
        and str(member.series_id or "").strip()
    ]
    if latest_coin_ids:
        state.coin_ids = list(dict.fromkeys(latest_coin_ids))
    return state


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
        else:
            dimension_axis = _canonical_dimension_decomposition_axis(key_lower, value_lower)
            if decomposition is None and dimension_axis:
                decomposition = {
                    "type": "dimension",
                    "entities": None,
                    "axis": dimension_axis,
                }
                continue

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
    frequency: Optional[str] = None

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
    active_answer_members: Optional[List["AnswerSetMember"]] = None
    recent_answer_members: Optional[List["AnswerSetMember"]] = None


class AnswerSetMember(BaseModel):
    """One verified series/member captured from a successful answer."""

    provider: str
    indicator_label: Optional[str] = None
    provider_code: Optional[str] = None
    series_id: Optional[str] = None
    country: Optional[str] = None
    countries: Optional[List[str]] = None
    source_turn: Optional[int] = None


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
    changed_frequency: Optional[str] = None
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

    # --- Telemetry / escalation (Phase 2.6 schema bump) ---
    # Populated by the LLM extractor; consumed in Phase 2.7 (Tier-3 escape
    # hatch). In this PR these fields are TELEMETRY ONLY — neither merge_state
    # nor any production caller reads them yet, so adding them is a
    # zero-behavior-change schema extension. The wiring (escalate to a full
    # state rewrite when confidence is low or needs_full_rewrite is true)
    # ships behind DELTA_FULL_REWRITE_ENABLED in a follow-up PR with
    # ≥7d shadow-mode validation per docs/DEEP_REVIEW_2026-05-30.md §6
    # invariant #8.
    delta_confidence: Optional[float] = None
    needs_full_rewrite: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_CHART_TYPES = {"line", "bar", "scatter", "table"}


def _normalize_chart_type(value: Optional[str]) -> Optional[str]:
    chart_type = str(value or "").strip().lower()
    if not chart_type:
        return None
    if chart_type in _ALLOWED_CHART_TYPES:
        return chart_type
    alias_map = {
        "line chart": "line",
        "bar chart": "bar",
        "scatter plot": "scatter",
        "scatterplot": "scatter",
        "table view": "table",
        # The LLM occasionally emits semantic display modes like "index" or
        # "level" into the chart slot. These are not valid chart types, but
        # mapping them to a stable line chart prevents hard failures while the
        # underlying series stays intact.
        "index": "line",
        "level": "line",
        "index level": "line",
    }
    return alias_map.get(chart_type)


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
            frequency=delta.changed_frequency,
            chart_type=_normalize_chart_type(delta.changed_chart_type),
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
        if str(delta.changed_indicator).strip().lower() in {"trade balance", "total trade"} and delta.changed_trade_flow is None:
            merged.trade_flow = None
        if not delta.is_dimension_modifier_change:
            merged.dimensions = None
            merged.statscan_cube_metadata = None  # Clear stale cube metadata when indicator changes
            # A StatsCan product ID is bound to the previous indicator's table.
            # Carrying it forward re-locks STATSCAN to the WRONG cube with
            # "verified" authority, so the new indicator silently returns the
            # old table's data (F4).  Drop it so the new indicator re-resolves.
            merged.statscan_product_id = None
        # Auto-detect crypto indicator switches and update coin_ids/provider
        _coin = _indicator_to_coin_id(delta.changed_indicator)
        if _coin:
            merged.coin_ids = [_coin]
            merged.provider = "COINGECKO"
            merged.routed_provider = "COINGECKO"

        if delta.changed_indicator:
            next_trade_reporter = delta.changed_trade_reporter or merged.trade_reporter
            if next_trade_reporter:
                merged.country = next_trade_reporter
                merged.countries = None

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

    if (merged.provider or merged.routed_provider or "").upper() == "COMTRADE" or merged.trade_reporter:
        if delta.changed_country:
            merged.trade_reporter = delta.changed_country
        elif delta.changed_countries:
            merged.trade_reporter = delta.changed_countries[0] if delta.changed_countries else None
        elif delta.removed_countries:
            if merged.country:
                merged.trade_reporter = merged.country
            elif merged.countries:
                merged.trade_reporter = merged.countries[0]
        elif delta.added_countries and not merged.trade_reporter:
            if merged.country:
                merged.trade_reporter = merged.country
            elif merged.countries:
                merged.trade_reporter = merged.countries[0]

    # --- Provider ---
    if delta.changed_provider:
        merged.provider = delta.changed_provider
        merged.provider_locked = True
        merged.resolved_indicator_code = None
        merged.last_indicators_resolved = None
        # StatsCan product IDs / cube metadata are provider-namespaced; a
        # provider switch invalidates them.  Drop them alongside the resolved
        # code so a leftover STATSCAN product can't re-lock the new provider
        # path with "verified" authority (F4 sibling case).
        merged.statscan_product_id = None
        merged.statscan_cube_metadata = None

    # --- Time ---
    if delta.changed_start_date:
        merged.start_date = delta.changed_start_date
    if delta.changed_end_date:
        merged.end_date = delta.changed_end_date
    if delta.changed_frequency:
        merged.frequency = delta.changed_frequency

    # --- Dimensions ---
    if delta.added_dimensions:
        remaining_dimensions, decomposition_from_dimensions, has_specific_geography_filter = (
            _interpret_dimension_breakdown(delta.added_dimensions)
        )
        if remaining_dimensions:
            merged.dimensions = {**(merged.dimensions or {}), **remaining_dimensions}
        if decomposition_from_dimensions and delta.changed_decomposition is None:
            merged.decomposition = decomposition_from_dimensions
        elif (
            (has_specific_geography_filter or _dimensions_target_decomposition_axis(remaining_dimensions, merged.decomposition))
            and merged.decomposition is not None
        ):
            merged.decomposition = None
    if delta.removed_dimensions:
        if merged.dimensions:
            for key in delta.removed_dimensions:
                merged.dimensions.pop(key, None)
            if not merged.dimensions:
                merged.dimensions = None

    # --- Chart type ---
    if delta.changed_chart_type:
        merged.chart_type = _normalize_chart_type(delta.changed_chart_type) or merged.chart_type

    # --- Decomposition ---
    if delta.changed_decomposition is not None:
        merged.decomposition = _canonicalize_decomposition_payload(delta.changed_decomposition)
        merged.dimensions = _strip_dimensions_for_decomposition(
            merged.dimensions,
            merged.decomposition,
        )

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
    if state.frequency:
        parameters["frequency"] = state.frequency

    # Trade
    provider_name = (state.provider or state.routed_provider or "").upper()
    if provider_name == "COMTRADE":
        reporter_scope: List[str] = []
        if state.countries:
            reporter_scope = list(state.countries)
        elif state.country:
            reporter_scope = [state.country]

        if reporter_scope:
            if len(reporter_scope) > 1:
                parameters["reporters"] = reporter_scope
            if state.trade_reporter and state.trade_reporter in reporter_scope:
                parameters["reporter"] = state.trade_reporter
            else:
                parameters["reporter"] = reporter_scope[0]
        elif state.trade_reporter:
            parameters["reporter"] = state.trade_reporter
    elif state.trade_reporter:
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
    if state.statscan_product_id:
        parameters["__statscan_product_id"] = state.statscan_product_id
        parameters["__statscan_product_authority"] = "verified_conversation_state"

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
    if state.indicator:
        parameters["__semantic_indicator_label"] = state.indicator

    # Provider: use explicit provider if set, otherwise default
    provider = state.provider or state.routed_provider or "WorldBank"
    if state.provider_locked:
        parameters["__semantic_provider_locked"] = True

    # Decomposition
    needs_decomp = state.decomposition is not None
    decomp_type = state.decomposition.get("type") if state.decomposition else None
    decomp_entities = state.decomposition.get("entities") if state.decomposition else None
    if state.decomposition and str(decomp_type or "").lower() == "dimension":
        axis = str(state.decomposition.get("axis") or "").strip()
        if axis:
            parameters["__statscan_decomposition_axis"] = axis

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
    frequency = params.get("frequency")

    original_query = str(intent.originalQuery or "").strip()

    # Indicator
    indicator: Optional[str] = None
    if intent.indicators:
        indicator = intent.indicators[0]
    semantic_indicator_label = str(params.get("__semantic_indicator_label") or "").strip()
    semantic_candidates = [semantic_indicator_label] if semantic_indicator_label else infer_multi_concept_indicators_from_query(original_query)
    if semantic_candidates:
        semantic_indicator = semantic_candidates[0]
        indicator_looks_like_code = bool(
            indicator
            and (
                "." in indicator
                or "_" in indicator
                or indicator.isupper()
            )
        )
        if (
            not indicator
            or indicator_looks_like_code
            or semantic_indicator.lower() != str(indicator or "").lower()
        ):
            indicator = semantic_indicator

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
    provider_name = normalize_provider_name(intent.apiProvider or "")

    explicit_statscan_product = str(params.get("__statscan_product_id") or "").strip()
    explicit_statscan_product_digits = (
        "".join(ch for ch in explicit_statscan_product if ch.isdigit())[:8] or None
    )
    if (
        provider_name == "STATSCAN"
        and explicit_statscan_product_digits
        and resolved_indicator_code
    ):
        resolved_digits = "".join(ch for ch in str(resolved_indicator_code) if ch.isdigit())[:8]
        if resolved_digits and resolved_digits != explicit_statscan_product_digits:
            resolved_indicator_code = explicit_statscan_product_digits

    if provider_name == "COMTRADE":
        raw_reporters = params.get("reporters")
        reporter_list: List[str] = []
        if isinstance(raw_reporters, list):
            reporter_list.extend(str(value) for value in raw_reporters if str(value or "").strip())
        if trade_reporter and str(trade_reporter).strip():
            reporter_value = str(trade_reporter).strip()
            if reporter_value not in reporter_list:
                reporter_list.insert(0, reporter_value)
        if reporter_list:
            reporter_list = list(dict.fromkeys(reporter_list))
            if len(reporter_list) == 1:
                country = reporter_list[0]
                countries = None
            else:
                countries = reporter_list
                country = None
        elif trade_reporter and str(trade_reporter).strip():
            country = str(trade_reporter).strip()
            countries = None

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
        decomposition = _canonicalize_decomposition_payload({
            "type": intent.decompositionType,
            "entities": intent.decompositionEntities,
        })
        decomposition_axis = str(params.get("__statscan_decomposition_axis") or "").strip()
        if decomposition is None:
            decomposition = {}
        if decomposition_axis:
            decomposition["axis"] = decomposition_axis
            decomposition = _canonicalize_decomposition_payload(decomposition)
    elif decomposition_from_dimensions:
        decomposition = decomposition_from_dimensions

    # Preserve an upstream-selected base indicator label for dimension
    # follow-ups.  Do not infer this from provider-local semantic maps.
    base_indicator: Optional[str] = None
    if indicator:
        if params.get("__base_indicator"):
            base_indicator = str(params["__base_indicator"])
        elif params.get("__semantic_indicator_label"):
            base_indicator = str(params["__semantic_indicator_label"])

    # Pre-resolve StatsCan product ID and cube metadata for dimension follow-ups.
    # Also try to read the provider's in-memory cube metadata cache (populated
    # during R1's data fetch). This avoids async API calls in the delta extractor.
    statscan_product_id: Optional[str] = None
    statscan_cube_metadata_val: Optional[Dict[str, Any]] = None
    if explicit_statscan_product:
        statscan_product_id = explicit_statscan_product_digits
    if not statscan_product_id:
        try:
            from ..providers.statscan import StatsCanProvider
            indicator_digits = "".join(ch for ch in str(indicator or "") if ch.isdigit())
            if len(indicator_digits) in {8, 10}:
                statscan_product_id = StatsCanProvider._normalize_metadata_product_id(indicator_digits)
            elif len(indicator_digits) >= 7:
                _cached_pid = StatsCanProvider.PRODUCT_ID_CACHE.get(int(indicator_digits))
                if _cached_pid:
                    statscan_product_id = StatsCanProvider._normalize_metadata_product_id(_cached_pid)
        except Exception:
            pass
    if statscan_product_id:
        try:
            from ..providers.statscan import StatsCanProvider
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

    if not statscan_product_id:
        for candidate in (
            params.get("indicator"),
            resolved_indicator_code,
            indicator,
        ):
            candidate_text = str(candidate or "").strip()
            if ":" in candidate_text:
                candidate_text = candidate_text.split(":", 1)[0]
            candidate_digits = "".join(ch for ch in candidate_text if ch.isdigit())
            if len(candidate_digits) >= 8:
                statscan_product_id = candidate_digits[:8]
                break

    return ConversationState(
        indicator=indicator,
        base_indicator=base_indicator,
        resolved_indicator_code=resolved_indicator_code,
        dimensions=dimensions,
        statscan_product_id=statscan_product_id,
        statscan_cube_metadata=statscan_cube_metadata_val,
        country=country,
        countries=countries,
        provider=provider_name,
        provider_locked=bool(params.get("__semantic_provider_locked")),
        routed_provider=provider_name,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
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
    # Provider-change detection runs BEFORE the provider carry-forward below
    # so we can correctly invalidate provider-specific resolved codes
    # (Phase 2.9 — fixes A#100). A resolved_indicator_code is by definition
    # provider-specific (FRED's "CPIAUCSL" is meaningless against WorldBank);
    # carrying it forward across a provider switch leaks the previous
    # provider's namespace and produces silent wrong-data answers.
    provider_changed = bool(
        new_state.provider
        and previous.provider
        and str(new_state.provider).strip().upper() != str(previous.provider).strip().upper()
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
    if not new_state.frequency and previous.frequency:
        new_state.frequency = previous.frequency

    # --- Indicator resolution ---
    # Carry forward the previous resolved code ONLY when (a) the indicator
    # name has not changed AND (b) the provider has not changed. A code is
    # always provider-specific, so a provider switch invalidates it even
    # when the human-readable indicator name is identical.
    if (
        not indicator_changed
        and not provider_changed
        and not new_state.resolved_indicator_code
        and previous.resolved_indicator_code
    ):
        new_state.resolved_indicator_code = previous.resolved_indicator_code
    if not indicator_changed and not new_state.base_indicator and previous.base_indicator:
        new_state.base_indicator = previous.base_indicator
    if (
        not indicator_changed
        and not provider_changed
        and not new_state.last_indicators_resolved
        and previous.last_indicators_resolved
    ):
        new_state.last_indicators_resolved = previous.last_indicators_resolved

    # --- StatsCanada dimension context ---
    # StatsCan product IDs and cube metadata are provider-specific. If the
    # provider switches away from StatsCan, drop them so they cannot leak
    # into a downstream FRED/WB/IMF query path. Phase 2.9 — invariant #3
    # preserved (statscan_cube_metadata still survives intra-StatsCan turns).
    if not new_state.dimensions and previous.dimensions:
        new_state.dimensions = previous.dimensions
    if (
        not indicator_changed
        and not provider_changed
        and not new_state.statscan_product_id
        and previous.statscan_product_id
    ):
        new_state.statscan_product_id = previous.statscan_product_id
    if (
        not indicator_changed
        and not provider_changed
        and not new_state.statscan_cube_metadata
        and previous.statscan_cube_metadata
    ):
        new_state.statscan_cube_metadata = previous.statscan_cube_metadata
    if not new_state.decomposition and previous.decomposition:
        _, _, has_specific_geography_filter = _interpret_dimension_breakdown(new_state.dimensions)
        if (
            not has_specific_geography_filter
            and not explicit_geo_in_new_state
            and not _dimensions_target_decomposition_axis(new_state.dimensions, previous.decomposition)
        ):
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

    # --- Answer-set provenance ---
    if not new_state.active_answer_members and previous.active_answer_members:
        new_state.active_answer_members = previous.active_answer_members
    if not new_state.recent_answer_members and previous.recent_answer_members:
        new_state.recent_answer_members = previous.recent_answer_members

    return new_state


# ---------------------------------------------------------------------------
# Provider-family classification (for topic-change gating)
# ---------------------------------------------------------------------------

# Providers grouped by the kind of geography/namespace they serve.  Two
# providers in the SAME family answer the same broad family of questions for
# the same kinds of geographies, so an indicator switch between them is a
# normal in-conversation refinement (e.g. FRED <-> WorldBank for US macro),
# NOT a true topic change.  A jump to a DIFFERENT family (e.g. macro -> crypto,
# macro -> trade) is a strong corroborating topic-change signal.
_PROVIDER_FAMILY: Dict[str, str] = {
    "FRED": "macro",
    "WORLDBANK": "macro",
    "IMF": "macro",
    "OECD": "macro",
    "EUROSTAT": "macro",
    "BIS": "macro",
    "STATSCAN": "macro",
    "COMTRADE": "trade",
    "COINGECKO": "crypto",
    "EXCHANGERATE": "fx",
    "ALPHAVANTAGE": "equity",
}


def _provider_family(provider: Optional[str]) -> Optional[str]:
    key = normalize_provider_name(str(provider or "")) if provider else ""
    if not key:
        return None
    return _PROVIDER_FAMILY.get(key.upper())


def reset_state_for_topic_change(
    new_state: ConversationState,
    previous: Optional[ConversationState],
    *,
    llm_new_query: bool,
) -> ConversationState:
    """Reset stale provider-scoped context on a TRUE topic change (F3).

    ``new_query`` is an LLM *label*, not ground truth — the LLM over-applies it
    to borderline indicator switches.  So this helper does NOT reset anything
    on the bare label.  It resets the provider-scoped/contextual fields a true
    topic change invalidates ONLY when there is a corroborating signal:

        provider-family change AND concept (indicator) change.

    When the gate fires, the fields that belong to the *previous* topic and
    were carried forward by ``merge_new_state_with_previous`` are dropped:
    ``provider_locked``, all ``trade_*`` fields, ``statscan_*`` artifacts,
    ``decomposition``, and the previous time window — UNLESS the new state set
    them explicitly (explicit new values always win).

    Sacred guard: a related indicator switch the LLM mislabels new_query
    (e.g. "US GDP 2010-2020" -> "show unemployment", same macro family) does
    NOT trip the gate, so dates + country + provider context are preserved.
    """
    if previous is None or not llm_new_query:
        return new_state

    prev_family = _provider_family(previous.provider or previous.routed_provider)
    new_family = _provider_family(new_state.provider or new_state.routed_provider)
    provider_family_changed = bool(
        prev_family and new_family and prev_family != new_family
    )

    concept_changed = bool(
        new_state.indicator
        and previous.indicator
        and str(new_state.indicator).strip().lower() != str(previous.indicator).strip().lower()
    )

    # Geography change is the second corroborating signal.  Compare canonical
    # ISO2 geography so "US"/"United States" don't register as a change.
    def _geo_key(state: ConversationState) -> tuple[str, ...]:
        raw = list(state.countries or ([state.country] if state.country else []))
        return tuple(sorted(_normalize_country_to_iso2(c) for c in raw if c))

    prev_geo = _geo_key(previous)
    new_geo = _geo_key(new_state)
    geography_changed = bool(prev_geo and new_geo and prev_geo != new_geo)

    # Corroborating topic-change signal: the indicator concept changed AND the
    # turn also moved to a different provider family OR a different geography.
    # A bare indicator switch within the same provider family AND same
    # geography (the LLM's most common new_query false-positive, e.g. "US GDP"
    # -> "show unemployment") is NOT a topic change and is left untouched.
    if not (concept_changed and (provider_family_changed or geography_changed)):
        return new_state

    logger.info(
        "Topic-change reset: family %s->%s, geo %s->%s, concept '%s'->'%s' — "
        "dropping stale provider-scoped context",
        prev_family, new_family, prev_geo, new_geo,
        previous.indicator, new_state.indicator,
    )

    # Provider lock from the previous topic must release so the new topic can
    # route freely.  The gate already requires a provider-FAMILY change, so the
    # new state's provider belongs to a different family than the locked one —
    # a carried-forward lock here is, by construction, stale.  Release it.  The
    # new provider value itself stays in new_state.provider (routing chose it);
    # only the pin is dropped so later turns and re-routing aren't constrained
    # by the previous topic's lock.
    new_state.provider_locked = False

    # Trade fields: drop unless the new topic explicitly re-introduced them.
    # (A true topic change away from trade should not carry COMTRADE scope.)
    if new_family != "trade":
        new_state.trade_flow = None
        new_state.trade_reporter = None
        new_state.trade_partner = None
        new_state.trade_commodity = None

    # StatsCan artifacts are namespaced to the prior table; drop on topic change
    # unless the new topic is itself StatsCan (provider explicitly set).
    if normalize_provider_name(str(new_state.provider or "")) != "STATSCAN":
        new_state.statscan_product_id = None
        new_state.statscan_cube_metadata = None

    # Decomposition belongs to the previous indicator's structure; drop it on a
    # concept change unless new_state explicitly carried a fresh decomposition.
    if previous.decomposition is not None and new_state.decomposition == previous.decomposition:
        new_state.decomposition = None
    new_state.dimensions = new_state.dimensions if new_state.dimensions != previous.dimensions else None

    # Time window from the previous topic: drop it unless the new state set
    # dates explicitly.  We can only tell "explicit" from "carried forward" by
    # comparing against previous — if identical to previous, it was carried.
    if previous.start_date is not None and new_state.start_date == previous.start_date:
        new_state.start_date = None
    if previous.end_date is not None and new_state.end_date == previous.end_date:
        new_state.end_date = None
    if previous.frequency is not None and new_state.frequency == previous.frequency:
        new_state.frequency = None

    return new_state


ConversationState.model_rebuild()
