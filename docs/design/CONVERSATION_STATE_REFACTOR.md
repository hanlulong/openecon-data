# Conversation State Refactor: FollowUpDelta + Merge Architecture

**Status:** DESIGN (not yet implemented)
**Date:** 2026-04-03
**Author:** Design phase analysis

---

## Problem Statement

The current multi-round conversation system reconstructs intent from scratch on each follow-up, using a fragile combination of:

1. **Deterministic regex handlers** (`_build_intent_from_contextual_follow_up`, `_build_intent_from_indicator_switch`) that detect specific follow-up patterns by string matching.
2. **LLM-based follow-up detection** via `conversation_context` injected into the system prompt, which produces `isFollowUp`, `followUpType`, and `resolvedQuery` fields.
3. **Flat `last_intent` storage** (`ParsedIntent`) that captures the previous turn's *execution plan* but not a clean representation of the conversation's accumulated *semantic state*.

### What Goes Wrong

- **Country follow-ups** require the `_INDICATOR_SWITCH_TERMS` set (40+ hardcoded terms) to detect indicator switches. New indicators not in this set silently fail.
- **Indicator switch** detection scans for known terms in a set, so "what about productivity" fails if "productivity" is not in `_INDICATOR_SWITCH_TERMS`.
- **State is encoded in `ParsedIntent`**, which is an execution-plan model (provider, indicators, parameters, routing flags). Extracting "what was the country?" requires digging into `parameters.country` / `parameters.countries` / `parameters.reporter` via `_collect_target_countries`.
- **Dimension modifiers** (CPI food -> CPI energy) require special-case code in `_build_intent_from_indicator_switch` with StatsCan-specific logic.
- **Time changes** are handled *only* by the LLM (no deterministic handler), so they depend entirely on prompt quality and LLM reliability.
- **Additive vs. replacement** country changes ("add France" vs. "show only US") are detected by keyword sets in `_build_intent_from_contextual_follow_up`.
- **Conversation context** is rebuilt from `last_intent` fields into a flat dict on every query (lines 3367-3427 in query.py), duplicated in the orchestrator path (lines 3958-3994).
- **Provider switches** require the LLM to produce `resolvedQuery` with full context; there is no deterministic handler.

### The Core Issue

There is no **single source of truth** for accumulated conversation state. The `last_intent` is an execution artifact, not a semantic state representation. Each follow-up handler independently reconstructs context from different fields of `ParsedIntent.parameters`, leading to:
- Duplicated context-extraction logic across 3+ code paths
- Fragile keyword-matching for follow-up type classification
- No clean way to "merge a change into existing state"

---

## Proposed Architecture

### Overview

Replace the "reconstruct from scratch" approach with a three-phase pipeline:

```
User Query
    |
    v
[1] Classify & Extract Delta  -->  FollowUpDelta
    |
    v
[2] Merge Delta into State    -->  ConversationState (updated)
    |
    v
[3] Materialize Intent        -->  ParsedIntent (for execution)
```

**Phase 1 (Delta Extraction):** Determine what changed relative to the previous turn. This is done by deterministic handlers for structural patterns, with LLM fallback for ambiguous cases.

**Phase 2 (Merge):** Apply the delta to the current `ConversationState` using deterministic, testable rules. This is a pure function with no side effects.

**Phase 3 (Materialization):** Convert the merged `ConversationState` into a `ParsedIntent` that the existing execution pipeline can consume. This replaces all the ad-hoc intent construction in the current handlers.

---

## Step 1: New Models

### ConversationState

```python
class ConversationState(BaseModel):
    """Structured state persisted across turns. Single source of truth.

    Unlike ParsedIntent (which is an execution plan with provider-specific
    parameters), ConversationState captures the user's semantic intent in
    a provider-agnostic way. It accumulates across turns.
    """
    # --- Core semantic fields ---
    indicator: Optional[str] = None          # Natural-language indicator: "unemployment rate"
    base_indicator: Optional[str] = None     # Canonical key: "UNEMPLOYMENT_RATE" (for vector/DB lookups)
    country: Optional[str] = None            # Single country ISO2: "CA"
    countries: Optional[List[str]] = None    # Multi-country ISO2 list: ["US", "DE", "FR"]
    provider: Optional[str] = None           # Explicit provider if user requested: "FRED", "IMF"
    start_date: Optional[str] = None         # "2020-01-01"
    end_date: Optional[str] = None           # "2024-12-31"

    # --- Dimension modifiers (StatsCan, Eurostat sub-categories) ---
    dimensions: Optional[Dict[str, str]] = None  # {"sex": "Female", "age": "youth", "product": "food"}

    # --- Chart / display preferences ---
    chart_type: Optional[str] = None         # "line", "bar", "scatter", "table"

    # --- Decomposition state ---
    decomposition: Optional[Dict[str, Any]] = None  # {"type": "provinces", "entities": ["Ontario"]}

    # --- Trade-specific fields ---
    trade_flow: Optional[str] = None         # "IMPORT", "EXPORT", "BOTH"
    trade_reporter: Optional[str] = None     # Reporter country
    trade_partner: Optional[str] = None      # Partner country
    trade_commodity: Optional[str] = None    # HS code or commodity name

    # --- Crypto-specific fields ---
    coin_ids: Optional[List[str]] = None     # ["bitcoin", "ethereum"]
    vs_currency: Optional[str] = None        # "usd"

    # --- Provenance ---
    original_query: Optional[str] = None     # Last raw user query text
    turn_number: int = 0                     # How many turns into the conversation
    routed_provider: Optional[str] = None    # Provider actually used (post-routing, may differ from explicit)
    last_indicators_resolved: Optional[List[str]] = None  # Provider-specific codes used last fetch
```

**Key design decisions:**
- **Provider-agnostic:** No provider-specific indicator codes in state (those live in `last_indicators_resolved` for reference only).
- **Flat structure:** No nested execution parameters. Each semantic concept gets its own field.
- **Dimensions as a dict:** Allows arbitrary sub-categories without hardcoding product types.
- **Trade fields separated:** Trade queries have distinct semantics (reporter/partner/flow) that don't map to the generic country/indicator model.

### FollowUpDelta

```python
class FollowUpDelta(BaseModel):
    """What changed relative to the previous turn.

    Produced by delta extraction (Phase 1). Every field is Optional;
    only populated fields represent changes. The merge function applies
    these changes to the current ConversationState.
    """
    # --- What changed ---
    changed_indicator: Optional[str] = None       # New indicator (natural language)
    changed_country: Optional[str] = None         # New single country
    changed_countries: Optional[List[str]] = None # New country list
    added_countries: Optional[List[str]] = None   # Countries to ADD to existing list
    removed_countries: Optional[List[str]] = None # Countries to REMOVE
    changed_provider: Optional[str] = None        # New explicit provider
    changed_start_date: Optional[str] = None      # New start date
    changed_end_date: Optional[str] = None        # New end date
    added_dimensions: Optional[Dict[str, str]] = None    # Dimensions to add/update
    removed_dimensions: Optional[List[str]] = None       # Dimension keys to remove
    changed_chart_type: Optional[str] = None
    changed_decomposition: Optional[Dict[str, Any]] = None
    changed_trade_flow: Optional[str] = None
    changed_trade_reporter: Optional[str] = None
    changed_trade_partner: Optional[str] = None
    changed_trade_commodity: Optional[str] = None

    # --- Meta ---
    is_new_query: bool = False              # True = ignore all previous state, start fresh
    is_dimension_modifier_change: bool = False  # True = indicator base stays, only modifier changes
    raw_query: Optional[str] = None         # The user's raw follow-up text

    # --- Classification (for logging/debugging) ---
    delta_type: Optional[str] = None        # "country_change", "indicator_switch", "time_change",
                                            # "provider_change", "dimension_change", "pronoun_reuse",
                                            # "clarification_answer", "additive_country", "new_query"
```

**Key design decisions:**
- **Additive vs. replacement countries are separate fields:** `changed_countries` replaces the list, `added_countries` appends. No ambiguity.
- **`is_dimension_modifier_change`:** Explicitly flags the "CPI food -> CPI energy" pattern so merge logic preserves the base indicator.
- **`delta_type` is informational:** Used for logging and debugging, not for branching logic. The merge function reads the actual changed fields.

---

## Step 2: The Merge Function

```python
def merge_state(current: ConversationState, delta: FollowUpDelta) -> ConversationState:
    """Deterministic merge: apply delta to current state.

    Rules:
    1. If delta.is_new_query, return a fresh ConversationState with only
       the delta's fields populated.
    2. Otherwise, overlay changed fields onto current state.
    3. When indicator changes (and it's NOT a dimension modifier change),
       clear dimensions (they are indicator-specific).
    4. When country changes, clear decomposition (province breakdowns
       are country-specific).
    5. Added/removed countries are merged into the existing list.
    6. Added/removed dimensions are merged into the existing dict.
    """
    if delta.is_new_query:
        # Fresh start: only populate from delta
        new_state = ConversationState(
            indicator=delta.changed_indicator,
            country=delta.changed_country,
            countries=delta.changed_countries,
            provider=delta.changed_provider,
            start_date=delta.changed_start_date,
            end_date=delta.changed_end_date,
            chart_type=delta.changed_chart_type,
            decomposition=delta.changed_decomposition,
            trade_flow=delta.changed_trade_flow,
            trade_reporter=delta.changed_trade_reporter,
            trade_partner=delta.changed_trade_partner,
            trade_commodity=delta.changed_trade_commodity,
            original_query=delta.raw_query,
            turn_number=current.turn_number + 1,
        )
        if delta.added_dimensions:
            new_state.dimensions = dict(delta.added_dimensions)
        return new_state

    merged = current.model_copy(deep=True)
    merged.turn_number = current.turn_number + 1
    merged.original_query = delta.raw_query or current.original_query

    # --- Indicator ---
    if delta.changed_indicator:
        merged.indicator = delta.changed_indicator
        merged.base_indicator = None  # Will be re-resolved during materialization
        merged.last_indicators_resolved = None  # Invalidate cached codes
        if not delta.is_dimension_modifier_change:
            # Full indicator change: clear dimensions (they belong to old indicator)
            merged.dimensions = None

    # --- Country ---
    if delta.changed_country:
        merged.country = delta.changed_country
        merged.countries = None  # Single-country mode
        merged.decomposition = None  # Province breakdowns are country-specific
    if delta.changed_countries:
        merged.countries = delta.changed_countries
        merged.country = None  # Multi-country mode
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
    if delta.removed_countries:
        existing = merged.countries or ([merged.country] if merged.country else [])
        remaining = [c for c in existing if c not in delta.removed_countries]
        if len(remaining) == 1:
            merged.country = remaining[0]
            merged.countries = None
        elif len(remaining) > 1:
            merged.countries = remaining
            merged.country = None
        # If empty after removal, keep the last state (edge case)

    # --- Provider ---
    if delta.changed_provider:
        merged.provider = delta.changed_provider
        merged.last_indicators_resolved = None  # Different provider, different codes

    # --- Time ---
    if delta.changed_start_date:
        merged.start_date = delta.changed_start_date
    if delta.changed_end_date:
        merged.end_date = delta.changed_end_date

    # --- Dimensions ---
    if delta.added_dimensions:
        merged.dimensions = {**(merged.dimensions or {}), **delta.added_dimensions}
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
```

### Merge Rules Summary

| Delta Field | Effect on State | Side Effects |
|---|---|---|
| `changed_indicator` | Replace `indicator`, clear `base_indicator` | Clear `dimensions` (unless `is_dimension_modifier_change`) |
| `changed_country` | Replace `country`, clear `countries` | Clear `decomposition` |
| `changed_countries` | Replace `countries`, clear `country` | Clear `decomposition` |
| `added_countries` | Append to existing country list | None |
| `removed_countries` | Remove from existing country list | None |
| `changed_provider` | Replace `provider` | Clear `last_indicators_resolved` |
| `changed_start_date` | Replace `start_date` | None |
| `changed_end_date` | Replace `end_date` | None |
| `added_dimensions` | Merge into `dimensions` dict | None |
| `removed_dimensions` | Remove keys from `dimensions` | None |
| `is_new_query=True` | Discard all state, build from delta only | Everything reset |

---

## Step 3: Integration Plan

### 3.1 Where ConversationState Gets Stored

**Replace `last_intent` in `ConversationContext` with `state`:**

```python
@dataclass
class ConversationContext:
    id: str
    messages: List[ConversationMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: Optional[ConversationState] = None         # NEW: replaces last_intent
    last_intent: Optional[ParsedIntent] = None        # KEEP: for backward compat during migration
    pending_indicator_options: Optional[Dict[str, Any]] = None
    pending_semantic_clarification: Optional[Dict[str, Any]] = None
```

**Migration strategy:**
- Phase 1: Store both `state` and `last_intent`. Build `state` from `last_intent` on read if `state` is missing (backward compat with existing Redis data).
- Phase 2: Remove `last_intent` once all code paths use `state`.

**ConversationManager API changes:**

```python
# New methods
def get_state(self, conversation_id: str) -> Optional[ConversationState]: ...
def update_state(self, conversation_id: str, state: ConversationState) -> None: ...

# Existing method signature change
def add_message(self, conversation_id, role, content,
                intent=None, state=None) -> None:  # Add state parameter
```

### 3.2 How FollowUpDelta Gets Produced

Delta extraction uses a **layered approach** -- deterministic handlers for common structural patterns, with LLM fallback for ambiguous cases.

#### Layer 1: Structural Handlers (Deterministic, No LLM)

These handle follow-ups that can be reliably detected by structure:

```python
class DeltaExtractor:
    """Extract FollowUpDelta from a query given conversation state."""

    def extract(self, query: str, state: ConversationState) -> Optional[FollowUpDelta]:
        """Try deterministic extraction. Returns None if ambiguous (needs LLM)."""

        # Priority order (most specific first):
        delta = self._try_clarification_answer(query, state)
        if delta: return delta

        delta = self._try_numeric_option_selection(query, state)
        if delta: return delta

        delta = self._try_country_only_follow_up(query, state)
        if delta: return delta

        delta = self._try_indicator_switch(query, state)
        if delta: return delta

        delta = self._try_provider_change(query, state)
        if delta: return delta

        # Not structurally recognizable -- needs LLM
        return None
```

**Key improvements over current code:**
- `_try_indicator_switch` no longer needs `_INDICATOR_SWITCH_TERMS`. Instead, it checks: "Does this short query contain words that are NOT countries, NOT filler words, and NOT in the current indicator?" If so, those words ARE the new indicator.
- `_try_country_only_follow_up` reuses the existing `_looks_like_country_follow_up` logic but populates a `FollowUpDelta` instead of building a full `ParsedIntent`.
- `_try_provider_change` detects "from FRED", "use IMF", "switch to Eurostat" patterns.
- Each handler returns a `FollowUpDelta` (not a `ParsedIntent`), keeping extraction separate from execution.

#### Layer 2: LLM Delta Extraction (Fallback)

When deterministic handlers return `None`, the LLM is called with conversation state context. The LLM prompt is modified to return a delta structure instead of a full intent.

**Option A (Recommended): Two-field LLM output**

The LLM produces `isFollowUp`, `followUpType`, and `resolvedQuery` (as today), and a new `delta` object:

```json
{
  "isFollowUp": true,
  "followUpType": "time_change",
  "resolvedQuery": "GDP in Canada last 20 years",
  "delta": {
    "changed_start_date": "2006-01-01",
    "changed_end_date": null
  },
  ... // full intent fields for fresh queries
}
```

**Option B (Simpler, phase 1): Use resolvedQuery as before**

Keep the current LLM output schema. When `isFollowUp=true`, diff `resolvedQuery` against `state` to produce a `FollowUpDelta`. This avoids changing the LLM prompt schema immediately.

**Recommendation:** Start with Option B to reduce risk. The `resolvedQuery` + state diffing approach works today and avoids prompt regression. Migrate to Option A in a later phase once the merge pipeline is proven stable.

#### Layer 3: New Query Detection

If neither deterministic nor LLM follow-up detection triggers, the query is treated as `is_new_query=True`:

```python
def _classify_as_new_or_followup(self, query: str, state: ConversationState, llm_intent: ParsedIntent) -> FollowUpDelta:
    """Final classification: if LLM says not a follow-up, return new-query delta."""
    if not llm_intent.isFollowUp:
        return FollowUpDelta(
            is_new_query=True,
            raw_query=query,
            delta_type="new_query",
            # Populate from LLM parse:
            changed_indicator=llm_intent.indicators[0] if llm_intent.indicators else None,
            changed_country=llm_intent.parameters.get("country"),
            changed_countries=llm_intent.parameters.get("countries"),
            changed_provider=llm_intent.apiProvider if llm_intent.apiProvider != "WorldBank" else None,
            changed_start_date=llm_intent.parameters.get("startDate"),
            changed_end_date=llm_intent.parameters.get("endDate"),
        )
    # LLM detected follow-up: build delta by diffing resolvedQuery parse vs state
    return self._diff_llm_follow_up(llm_intent, state)
```

### 3.3 How Merged State Becomes a ParsedIntent

A new **materialization** function converts `ConversationState` into `ParsedIntent`:

```python
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

    # Indicator
    indicators = [state.indicator] if state.indicator else ["unknown"]

    # Provider: use explicit provider if set, otherwise default (router will override)
    provider = state.provider or state.routed_provider or "WorldBank"

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
    )
```

### 3.4 What Happens to Existing Handlers

| Current Code | Replacement | Notes |
|---|---|---|
| `_build_intent_from_contextual_follow_up` | `DeltaExtractor._try_country_only_follow_up` | Returns `FollowUpDelta` instead of `(query, intent, parse_result)` |
| `_build_intent_from_indicator_switch` | `DeltaExtractor._try_indicator_switch` | Eliminates `_INDICATOR_SWITCH_TERMS` hardcoded set |
| `_INDICATOR_SWITCH_TERMS` (40-term set) | Removed | Indicator detection done by exclusion (not-country, not-filler = new indicator) |
| `_build_contextual_follow_up_query` | `materialize_intent` + router | Query text reconstructed from state if needed for logging |
| `_looks_like_country_follow_up` | `DeltaExtractor._try_country_only_follow_up` | Same logic, different output type |
| `_extract_concept_from_indicator` | Not needed | State stores natural-language indicator, not provider codes |
| `_collect_target_countries` | `state.country` / `state.countries` | Direct field access, no parameter digging |
| Conversation context dict construction (lines 3367-3427 + 3958-3994) | `_build_llm_context_from_state(state)` | Single function, no duplication |
| `conversation_manager.get_last_intent()` calls | `conversation_manager.get_state()` | Same call sites, different return type |

### 3.5 How the LLM Prompt Changes

**Minimal change in Phase 1.** The prompt structure stays the same, but the conversation context section is built from `ConversationState` instead of `last_intent`:

```python
@classmethod
def _follow_up_section_from_state(cls, state: ConversationState) -> str:
    """Build follow-up context from ConversationState (replaces dict-based version)."""
    country_str = ", ".join(state.countries) if state.countries else (state.country or "not specified")
    return f"""
--- CONVERSATION CONTEXT (follow-up detection) ---

The user's previous query was: "{state.original_query}"
Previous intent details:
- Indicator: {state.indicator or "not specified"}
- Country/countries: {country_str}
- Provider: {state.provider or state.routed_provider or "not specified"}
- Time period: {state.start_date or "not specified"} to {state.end_date or "not specified"}
- Dimensions: {state.dimensions or "none"}
- Turn number: {state.turn_number}

... (same follow-up detection instructions as current prompt)
"""
```

**Phase 2 (future):** Add `delta` field to LLM output schema for direct delta extraction.

### 3.6 New Query Processing Flow

```python
async def process_query(self, query, conversation_id=None, ...):
    conv_id = conversation_manager.get_or_create(conversation_id)

    # --- Phase 0: Pending clarification resolution (unchanged) ---
    pending_choice = await self._try_resolve_pending_indicator_choice(query, conv_id, tracker)
    if pending_choice:
        return pending_choice

    # --- Phase 1: Get current state ---
    current_state = conversation_manager.get_state(conv_id) or ConversationState()

    # --- Phase 2: Try deterministic delta extraction ---
    delta_extractor = DeltaExtractor(self)
    delta = delta_extractor.extract(query, current_state)

    if delta is not None:
        # Deterministic follow-up detected
        merged_state = merge_state(current_state, delta)
        intent = materialize_intent(merged_state)

        # Route the materialized intent
        intent = await self._route_and_validate(intent, query)

        # Update state with routing result
        merged_state.routed_provider = intent.apiProvider
        conversation_manager.update_state(conv_id, merged_state)

        return await self._execute_resolved_intent(
            query=merged_state.original_query or query,
            conversation_id=conv_id,
            intent=intent,
            parse_result=...,
            tracker=tracker,
            skip_prefetch_clarification=True,
        )

    # --- Phase 3: LLM parsing (same as today) ---
    conversation_context = _build_llm_context_from_state(current_state)
    parse_result = await self.pipeline.parse_and_route(query, history, conversation_context)
    intent = parse_result.intent

    # --- Phase 4: Build delta from LLM result ---
    if intent.isFollowUp:
        delta = _diff_llm_follow_up(intent, current_state)
    else:
        delta = FollowUpDelta(is_new_query=True, raw_query=query, ...)
        # Populate delta from fresh LLM parse

    # --- Phase 5: Merge and update state ---
    merged_state = merge_state(current_state, delta)
    # Re-materialize if needed, or use intent directly for fresh queries
    merged_state.routed_provider = intent.apiProvider
    merged_state.last_indicators_resolved = intent.indicators
    conversation_manager.update_state(conv_id, merged_state)

    # --- Phase 6: Execute (same as today) ---
    return await self._execute_intent(intent, conv_id, tracker, ...)
```

---

## Step 4: Files That Need Changes

### Core Changes

| File | Methods/Sections | Estimated Lines Changed | Description |
|---|---|---|---|
| `backend/models.py` | New classes | +80 | Add `ConversationState` and `FollowUpDelta` models |
| `backend/services/conversation.py` | `ConversationContext`, serialization, `ConversationManager` | ~120 | Add `state` field, serialization, `get_state()`/`update_state()` methods |
| `backend/services/query.py` | `process_query`, follow-up handlers | ~300 (remove ~350, add ~250) | Replace handler methods with delta extraction + merge pipeline |
| `backend/services/simplified_prompt.py` | `_follow_up_section` | ~30 | Build context from `ConversationState` instead of dict |
| `backend/services/query_pipeline.py` | `parse_and_route` | ~10 | Accept `ConversationState` optionally |

### New Files

| File | Lines | Description |
|---|---|---|
| `backend/services/conversation_state.py` | ~250 | `ConversationState`, `FollowUpDelta`, `merge_state()`, `materialize_intent()` |
| `backend/services/delta_extractor.py` | ~300 | `DeltaExtractor` class with all deterministic handlers |

### Files to Update (Minor)

| File | Change | Lines |
|---|---|---|
| `backend/services/openrouter.py` | Pass `ConversationState` to prompt generator | ~5 |
| `scripts/test_multiround.py` | Add state-verification checks | ~50 |

### Files to Remove Code From

| File | What Gets Removed | Lines Saved |
|---|---|---|
| `backend/services/query.py` | `_build_intent_from_contextual_follow_up` (~120 lines) | ~120 |
| `backend/services/query.py` | `_build_intent_from_indicator_switch` (~180 lines) | ~180 |
| `backend/services/query.py` | `_INDICATOR_SWITCH_TERMS` set | ~20 |
| `backend/services/query.py` | `_build_contextual_follow_up_query` | ~15 |
| `backend/services/query.py` | `_looks_like_country_follow_up` | ~45 |
| `backend/services/query.py` | `_extract_concept_from_indicator` | ~50 |
| `backend/services/query.py` | Duplicate conversation_context construction (both pipeline + orchestrator) | ~80 |

**Net change estimate:** Remove ~510 lines from query.py, add ~550 lines across new files, ~160 lines of modifications. Total code roughly neutral, but complexity is significantly reduced (fewer code paths, single merge function, no hardcoded term sets).

---

## Step 5: Migration Plan

### Phase 1: Dual-Write (Low Risk)

1. Add `ConversationState` and `FollowUpDelta` models to `backend/services/conversation_state.py`.
2. Add `state` field to `ConversationContext` alongside existing `last_intent`.
3. Build `ConversationState` from `ParsedIntent` after each successful query (populate both `state` and `last_intent`).
4. Add `get_state()` / `update_state()` to `ConversationManager`.
5. Write unit tests for `merge_state()` with all follow-up patterns from `test_multiround.py`.
6. **No behavior change.** Existing handlers still execute. State is built but not consumed.

### Phase 2: Delta Extraction + Merge (Medium Risk)

1. Implement `DeltaExtractor` with deterministic handlers.
2. In `process_query`, before calling existing handlers, try `DeltaExtractor.extract()`.
3. If delta extraction succeeds, use `merge_state` + `materialize_intent` path.
4. If delta extraction returns `None`, fall through to existing handlers (unchanged).
5. Run `test_multiround.py` to verify delta path matches existing behavior.
6. Add logging to compare delta-path results vs. existing handler results.

### Phase 3: Remove Old Handlers (After Validation)

1. Once Phase 2 is validated across 100+ multi-round test scenarios:
   - Remove `_build_intent_from_contextual_follow_up`
   - Remove `_build_intent_from_indicator_switch`
   - Remove `_INDICATOR_SWITCH_TERMS`
   - Remove `_build_contextual_follow_up_query`
   - Remove `_looks_like_country_follow_up`
   - Remove `_extract_concept_from_indicator`
2. Consolidate conversation_context construction into `_build_llm_context_from_state()`.
3. Remove `last_intent` from `ConversationContext` (use `state` only).
4. Update Redis serialization to handle migration from old format.

### Phase 4: LLM Delta Output (Future)

1. Modify LLM prompt to output `delta` fields directly.
2. Remove `resolvedQuery`-based diffing.
3. Reduce LLM token usage (delta is smaller than full intent).

---

## Testing Strategy

### Unit Tests for merge_state

```python
# Country change: preserve indicator + time
state = ConversationState(indicator="GDP", country="US", start_date="2020-01-01")
delta = FollowUpDelta(changed_country="DE", delta_type="country_change")
merged = merge_state(state, delta)
assert merged.indicator == "GDP"
assert merged.country == "DE"
assert merged.start_date == "2020-01-01"
assert merged.decomposition is None  # Cleared by country change

# Indicator switch: preserve country + time, clear dimensions
state = ConversationState(indicator="CPI", country="CA", dimensions={"product": "food"})
delta = FollowUpDelta(changed_indicator="unemployment rate", delta_type="indicator_switch")
merged = merge_state(state, delta)
assert merged.indicator == "unemployment rate"
assert merged.country == "CA"
assert merged.dimensions is None  # Cleared by indicator change

# Dimension modifier: preserve base indicator + country
state = ConversationState(indicator="CPI", country="CA", dimensions={"product": "food"})
delta = FollowUpDelta(
    added_dimensions={"product": "energy"},
    is_dimension_modifier_change=True,
    delta_type="dimension_change"
)
merged = merge_state(state, delta)
assert merged.indicator == "CPI"  # Preserved
assert merged.dimensions == {"product": "energy"}  # Updated

# Additive country
state = ConversationState(indicator="GDP", countries=["US", "DE"])
delta = FollowUpDelta(added_countries=["FR"], delta_type="additive_country")
merged = merge_state(state, delta)
assert merged.countries == ["US", "DE", "FR"]

# New query: clean slate
state = ConversationState(indicator="GDP", country="US", start_date="2020-01-01")
delta = FollowUpDelta(is_new_query=True, changed_indicator="inflation", changed_country="JP")
merged = merge_state(state, delta)
assert merged.indicator == "inflation"
assert merged.country == "JP"
assert merged.start_date is None  # Not carried over
```

### Integration Tests

Extend `scripts/test_multiround.py` with:
- **Scenario 6:** Dimension modifier follow-up (CPI food -> CPI energy)
- **Scenario 7:** Additive country ("add France" to existing US+DE comparison)
- **Scenario 8:** 3-turn chain (GDP US -> same for Germany -> last 20 years)
- **Scenario 9:** New query mid-conversation (GDP US -> completely unrelated inflation Japan)
- **Scenario 10:** Provider change preserving all other state

---

## Risk Assessment

| Risk | Mitigation |
|---|---|
| LLM prompt regression when changing context format | Phase 1 dual-write means no prompt changes initially |
| Redis backward compatibility with stored `last_intent` | Migration function builds `state` from `last_intent` on read |
| Deterministic handlers missing edge cases | Existing handlers run in parallel during Phase 2 for comparison |
| `merge_state` logic errors | Exhaustive unit tests covering all delta combinations |
| Performance impact of extra state serialization | `ConversationState` is ~500 bytes JSON, negligible vs. LLM calls |
| Breaking existing test suite | Phase 2 is additive only; old paths are not removed until Phase 3 |

---

## Summary

The FollowUpDelta + Merge architecture replaces the current "reconstruct intent from scratch" approach with a clean three-phase pipeline:

1. **Extract delta** (what changed) using deterministic handlers + LLM fallback
2. **Merge delta** into accumulated `ConversationState` using a pure, testable function
3. **Materialize** the merged state into a `ParsedIntent` for execution

This eliminates the 40-term `_INDICATOR_SWITCH_TERMS` hardcoded set, removes 500+ lines of ad-hoc intent construction, consolidates duplicated conversation context building, and provides a single source of truth for conversation state. The phased migration plan ensures zero regression risk during the transition.
