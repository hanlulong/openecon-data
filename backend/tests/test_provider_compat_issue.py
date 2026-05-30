"""Provider-compatibility regression test for merge_new_state_with_previous.

Originally this file documented an open bug: when the provider changed
between turns (e.g. FRED → WorldBank) but the indicator name stayed the
same, the previous provider's resolved_indicator_code carried forward and
leaked into the new provider's namespace. Phase 2.9 in
docs/DEEP_REVIEW_2026-05-30.md fixed that. This file now serves as a
regression guard so the leak does not return.
"""
import pytest
from backend.services.conversation_state_v2 import ConversationState, merge_new_state_with_previous


class TestProviderCompatibilityRegression:
    def test_provider_change_clears_resolved_indicator_code(self):
        """A FRED-specific code must not survive a switch to WorldBank.

        Turn 1: "GDP" with FRED → resolves to a FRED-native code.
        Turn 2: User says "use WorldBank instead" — even though the
                human-readable indicator label is identical, the resolved
                code is by definition provider-specific and is now invalid.
        Expected: merged.resolved_indicator_code is None so the next fetch
                  re-resolves through WorldBank's namespace.
        """
        previous = ConversationState(
            indicator="GDP",
            provider="FRED",
            resolved_indicator_code="CPIAUCSL",
            country="US",
            turn_number=0,
        )
        new_state = ConversationState(
            indicator="GDP",
            provider="WORLDBANK",
            country="US",
            turn_number=1,
        )
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.resolved_indicator_code is None
        assert merged.provider == "WORLDBANK"

    def test_same_provider_preserves_resolved_indicator_code(self):
        """The cross-provider invalidation must NOT fire when the provider stays the same.

        Turn 1: "GDP" with FRED → resolves to a FRED-native code.
        Turn 2: User asks for the same on a longer time range — provider unchanged.
        Expected: merged.resolved_indicator_code is preserved so the next fetch
                  skips re-resolution.
        """
        previous = ConversationState(
            indicator="GDP",
            provider="FRED",
            resolved_indicator_code="GDPC1",
            country="US",
            turn_number=0,
        )
        new_state = ConversationState(
            indicator="GDP",
            provider="FRED",
            country="US",
            turn_number=1,
        )
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.resolved_indicator_code == "GDPC1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
