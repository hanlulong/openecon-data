"""Guard tests for conversation-state carry-forward invariants.

Covers two framework fixes in ``conversation_state_v2``:

* F3 — ``merge_new_state_with_previous`` must gate the ``dimensions`` carry on
  the same "indicator/provider unchanged" guard as every other
  provider-scoped field. Otherwise a StatsCan coordinate (e.g. ``Sex``) leaks
  onto a new indicator or provider.
* F1a — a frequency change is a resolution constraint: a code resolved at one
  frequency may not exist at another, so both the delta path (``merge_state``)
  and the non-delta path (``merge_new_state_with_previous``) must force
  re-resolution instead of carrying the stale resolved code forward.
"""
from __future__ import annotations

from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    merge_new_state_with_previous,
    merge_state,
)


# ─── F3: dimensions carry-forward guard ─────────────────────────────

class TestDimensionsCarryGuard:
    def test_dimensions_dropped_when_indicator_changes(self):
        previous = ConversationState(
            indicator="unemployment rate",
            provider="STATSCAN",
            dimensions={"Sex": "Male"},
        )
        new_state = ConversationState(indicator="GDP", provider="STATSCAN")
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.dimensions is None

    def test_dimensions_dropped_when_provider_changes(self):
        previous = ConversationState(
            indicator="unemployment rate",
            provider="STATSCAN",
            dimensions={"Sex": "Male"},
        )
        # Same human-readable indicator, different provider — the coordinate
        # codes are STATSCAN-namespaced and must not reach EUROSTAT.
        new_state = ConversationState(indicator="unemployment rate", provider="EUROSTAT")
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.dimensions is None

    def test_dimensions_carried_when_indicator_and_provider_unchanged(self):
        previous = ConversationState(
            indicator="unemployment rate",
            provider="STATSCAN",
            dimensions={"Sex": "Male"},
        )
        new_state = ConversationState(indicator="unemployment rate", provider="STATSCAN")
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.dimensions == {"Sex": "Male"}


# ─── F1a: frequency invalidates the resolved code ───────────────────

class TestFrequencyInvalidatesResolutionDeltaPath:
    def test_frequency_change_clears_resolved_code(self):
        state = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            frequency="annual",
            resolved_indicator_code="GDPCA",
            last_indicators_resolved=["GDPCA"],
        )
        delta = FollowUpDelta(changed_frequency="quarterly", delta_type="parameter_change")
        merged = merge_state(state, delta)
        assert merged.frequency == "quarterly"
        assert merged.resolved_indicator_code is None
        assert merged.last_indicators_resolved is None

    def test_same_frequency_keeps_resolved_code(self):
        state = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            frequency="annual",
            resolved_indicator_code="GDPCA",
            last_indicators_resolved=["GDPCA"],
        )
        # A follow-up that "changes" frequency to the same value is not a real
        # frequency change — the resolved code must survive.
        delta = FollowUpDelta(changed_frequency="annual", delta_type="parameter_change")
        merged = merge_state(state, delta)
        assert merged.resolved_indicator_code == "GDPCA"
        assert merged.last_indicators_resolved == ["GDPCA"]


class TestFrequencyInvalidatesResolutionNonDeltaPath:
    def test_frequency_change_prevents_code_carry(self):
        previous = ConversationState(
            indicator="GDP",
            provider="FRED",
            frequency="annual",
            resolved_indicator_code="GDPCA",
            last_indicators_resolved=["GDPCA"],
        )
        new_state = ConversationState(indicator="GDP", provider="FRED", frequency="quarterly")
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.resolved_indicator_code is None
        assert merged.last_indicators_resolved is None

    def test_same_frequency_carries_code(self):
        previous = ConversationState(
            indicator="GDP",
            provider="FRED",
            frequency="annual",
            resolved_indicator_code="GDPCA",
            last_indicators_resolved=["GDPCA"],
        )
        new_state = ConversationState(indicator="GDP", provider="FRED", frequency="annual")
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.resolved_indicator_code == "GDPCA"
        assert merged.last_indicators_resolved == ["GDPCA"]

    def test_no_new_frequency_still_carries_code(self):
        # A follow-up that does not mention frequency at all must not be treated
        # as a frequency change (frequency_changed requires a value on BOTH
        # sides), so the resolved code is preserved.
        previous = ConversationState(
            indicator="GDP",
            provider="FRED",
            frequency="annual",
            resolved_indicator_code="GDPCA",
        )
        new_state = ConversationState(indicator="GDP", provider="FRED")
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.resolved_indicator_code == "GDPCA"
