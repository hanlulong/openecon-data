"""Guard tests for four multi-round conversation-state fixes.

Each class corresponds to one framework fix in the multi-round delta/merge path.
These are deterministic unit tests over pure merge/gate/sanitizer logic — the
LLM-classification half of FIX 1a and FIX 4 (delta-extractor prompt rules) is
exercised through the delta extractor separately and cannot be asserted here
without a live model, so these cover the state-mutation and dispatch layers that
carry the correctness guarantee.

* FIX 1b — a REPLACE country switch on a geography-encoded provider (FRED,
  StatsCan, Comtrade, CoinGecko) invalidates the carried resolved code: the code
  is geography-specific, so reusing it would serve the OLD geography's data.
  Country-agnostic providers (World Bank, IMF, Eurostat, ...) keep the code.
* FIX 2 — the PHASE A "use carried indicator as-is" fast path must reflect the
  state: a bare label carried after a frequency change (or geography switch)
  cannot be dispatched as-is; a frequency change re-flags the indicator as
  needing resolution.
* FIX 3 — a persisted state carrying a foreign-namespace resolved code (stale
  pre-deploy Redis state) has that code cleared at read time.
* FIX 4 — an explicit language switch is applied; otherwise language is sticky.
"""
from __future__ import annotations

from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    merge_state,
)
from backend.services.data_fetcher import _carried_indicator_dispatchable_as_is
from backend.services.indicator_clarification import (
    looks_like_provider_indicator_code,
)
from backend.services.query import (
    _delta_reflects_frequency_change,
    _persisted_resolved_code_is_foreign_namespace,
)


class _FakeSvc:
    """Minimal stand-in exposing the code-shape predicate the gate calls."""

    def _looks_like_provider_indicator_code(self, provider: str, indicator: str) -> bool:
        return looks_like_provider_indicator_code(provider, indicator)


# ─── FIX 1b: country switch clears geography-encoded resolved codes ──

class TestCountrySwitchClearsGeographyEncodedCode:
    def test_fred_country_change_clears_resolved_code(self):
        state = ConversationState(
            indicator="unemployment rate",
            country="US",
            provider="FRED",
            subnational_region="Texas",
            resolved_indicator_code="TXUR",
            last_indicators_resolved=["TXUR"],
        )
        merged = merge_state(
            state, FollowUpDelta(changed_country="California", delta_type="country_change")
        )
        assert merged.resolved_indicator_code is None
        assert merged.last_indicators_resolved is None
        # The subnational annotation is also dropped by the existing Proposal-B
        # rule (a metric/geography change invalidates the old sub-region).
        assert merged.subnational_region is None

    def test_statscan_countries_change_clears_resolved_code(self):
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            resolved_indicator_code="14100287",
            last_indicators_resolved=["14100287"],
        )
        merged = merge_state(state, FollowUpDelta(changed_countries=["US"]))
        assert merged.resolved_indicator_code is None
        assert merged.last_indicators_resolved is None

    def test_worldbank_country_change_keeps_resolved_code(self):
        # World Bank codes are country-AGNOSTIC (country is a separate param), so
        # the SAME code is correct for the new country and must be preserved.
        state = ConversationState(
            indicator="GDP",
            country="DE",
            provider="WORLDBANK",
            resolved_indicator_code="NY.GDP.MKTP.CD",
            last_indicators_resolved=["NY.GDP.MKTP.CD"],
        )
        merged = merge_state(state, FollowUpDelta(changed_country="FR"))
        assert merged.resolved_indicator_code == "NY.GDP.MKTP.CD"
        assert merged.last_indicators_resolved == ["NY.GDP.MKTP.CD"]

    def test_imf_country_change_keeps_resolved_code(self):
        state = ConversationState(
            indicator="inflation",
            country="DE",
            provider="IMF",
            resolved_indicator_code="PCPIPCH",
            last_indicators_resolved=["PCPIPCH"],
        )
        merged = merge_state(state, FollowUpDelta(changed_country="JP"))
        assert merged.resolved_indicator_code == "PCPIPCH"
        assert merged.last_indicators_resolved == ["PCPIPCH"]

    def test_added_country_does_not_clear_resolved_code(self):
        # Only a REPLACE (changed_country/changed_countries) clears; ADD keeps
        # the snapshot until runtime validates scope (preserves the existing
        # test_geography_mutation_preserves_resolution_snapshot invariant).
        state = ConversationState(
            indicator="GDP growth rate",
            countries=["US"],
            provider="FRED",
            resolved_indicator_code="A191RL1Q225SBEA",
            last_indicators_resolved=["A191RL1Q225SBEA"],
        )
        merged = merge_state(state, FollowUpDelta(added_countries=["CA"]))
        assert merged.resolved_indicator_code == "A191RL1Q225SBEA"
        assert merged.last_indicators_resolved == ["A191RL1Q225SBEA"]


# ─── FIX 2: PHASE A state-reflecting dispatch gate ──────────────────

class TestPhaseADispatchGate:
    def setup_method(self):
        self.svc = _FakeSvc()

    def test_bare_label_no_code_falls_through_to_resolution(self):
        # After F1a nulls the resolved code on a frequency change, only the human
        # label ("GDP") survives with no resolved code present → must resolve.
        assert (
            _carried_indicator_dispatchable_as_is(
                self.svc, "FRED", "unemployment rate",
                indicator_changed=False, resolved_code_present=False,
            )
            is False
        )

    def test_code_shaped_label_dispatched_as_is(self):
        # A code-shaped carried indicator is dispatched as-is (preserved path).
        assert (
            _carried_indicator_dispatchable_as_is(
                self.svc, "WORLDBANK", "NY.GDP.MKTP.CD",
                indicator_changed=False, resolved_code_present=False,
            )
            is True
        )

    def test_resolved_code_present_dispatched_as_is(self):
        # A resolved code already in params is dispatched as-is regardless of the
        # indicator label's shape (verified prior-turn fetch).
        assert (
            _carried_indicator_dispatchable_as_is(
                self.svc, "EUROSTAT", "inflation",
                indicator_changed=False, resolved_code_present=True,
            )
            is True
        )

    def test_indicator_changed_always_resolves(self):
        assert (
            _carried_indicator_dispatchable_as_is(
                self.svc, "FRED", "CPIAUCSL",
                indicator_changed=True, resolved_code_present=True,
            )
            is False
        )

    def test_frequency_change_flags_indicator_for_resolution(self):
        state = ConversationState(indicator="GDP", provider="FRED", frequency="annual")
        assert _delta_reflects_frequency_change(
            FollowUpDelta(changed_frequency="quarterly"), state
        ) is True

    def test_same_frequency_does_not_flag(self):
        state = ConversationState(indicator="GDP", provider="FRED", frequency="annual")
        assert _delta_reflects_frequency_change(
            FollowUpDelta(changed_frequency="annual"), state
        ) is False

    def test_absent_frequency_does_not_flag(self):
        state = ConversationState(indicator="GDP", provider="FRED", frequency="annual")
        assert _delta_reflects_frequency_change(
            FollowUpDelta(changed_country="JP"), state
        ) is False

    def test_frequency_change_end_to_end_forces_resolution(self):
        # End-to-end frequency change: F1a nulls the resolved code, and the
        # disjunction helper flags the indicator as changed. The gate then sees
        # indicator_changed=True and forces resolution — this is the path that
        # actually protects a single-word, code-SHAPED carried label such as
        # "GDP" (which the code-shape heuristic alone would wave through as-is).
        state = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            frequency="annual",
            resolved_indicator_code="GDPCA",
            last_indicators_resolved=["GDPCA"],
        )
        delta = FollowUpDelta(changed_frequency="quarterly")
        merged = merge_state(state, delta)
        assert merged.resolved_indicator_code is None
        indicator_changed = _delta_reflects_frequency_change(delta, state)
        assert indicator_changed is True
        assert (
            _carried_indicator_dispatchable_as_is(
                self.svc, "FRED", merged.indicator or "",
                indicator_changed=indicator_changed, resolved_code_present=False,
            )
            is False
        )

    def test_multiword_bare_label_falls_through_without_disjunction(self):
        # For a MULTI-WORD label (has a space → never code-shaped), the gate
        # alone is sufficient: it falls through to resolution even when the
        # disjunction did not flag the indicator.
        assert (
            _carried_indicator_dispatchable_as_is(
                self.svc, "FRED", "unemployment rate",
                indicator_changed=False, resolved_code_present=False,
            )
            is False
        )


# ─── FIX 3: read-time namespace sanitizer ───────────────────────────

class TestForeignNamespaceSanitizer:
    def test_foreign_fred_code_on_worldbank_state_is_flagged(self):
        assert _persisted_resolved_code_is_foreign_namespace("WORLDBANK", "CPIAUCSL") is True

    def test_own_worldbank_code_is_kept(self):
        assert _persisted_resolved_code_is_foreign_namespace("WORLDBANK", "NY.GDP.MKTP.CD") is False

    def test_own_fred_code_is_kept(self):
        assert _persisted_resolved_code_is_foreign_namespace("FRED", "UNRATE") is False

    def test_own_imf_code_is_kept(self):
        assert _persisted_resolved_code_is_foreign_namespace("IMF", "PCPIPCH") is False

    def test_foreign_imf_code_on_worldbank_state_is_flagged(self):
        assert _persisted_resolved_code_is_foreign_namespace("WORLDBANK", "PCPIPCH") is True

    def test_empty_inputs_are_safe(self):
        assert _persisted_resolved_code_is_foreign_namespace("", "CPIAUCSL") is False
        assert _persisted_resolved_code_is_foreign_namespace("WORLDBANK", "") is False


# ─── FIX 4: language stickiness / explicit switch ───────────────────

class TestLanguageStickiness:
    def test_explicit_language_switch_applies(self):
        state = ConversationState(indicator="GDP", country="China", language="zh")
        assert merge_state(state, FollowUpDelta(changed_language="en")).language == "en"

    def test_absent_language_stays_sticky(self):
        state = ConversationState(indicator="GDP", country="China", language="zh")
        assert merge_state(state, FollowUpDelta(changed_country="JP")).language == "zh"

    def test_new_query_keeps_language_without_switch(self):
        state = ConversationState(indicator="GDP", country="China", language="zh")
        merged = merge_state(
            state, FollowUpDelta(is_new_query=True, changed_indicator="inflation")
        )
        assert merged.language == "zh"

    def test_new_query_honors_explicit_language_switch(self):
        state = ConversationState(indicator="GDP", country="China", language="zh")
        merged = merge_state(
            state,
            FollowUpDelta(
                is_new_query=True, changed_indicator="inflation", changed_language="en"
            ),
        )
        assert merged.language == "en"

    def test_pure_language_delta_survives_has_change_gate(self):
        # A delta whose only change is language must not be dropped as "no change".
        delta = FollowUpDelta(changed_language="en", query_type="parameter_delta")
        state = ConversationState(indicator="GDP", country="China", language="zh")
        assert merge_state(state, delta).language == "en"
