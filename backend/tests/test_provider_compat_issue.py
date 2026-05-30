"""Test to verify the provider compatibility issue in merge_new_state_with_previous"""
import pytest
from services.conversation_state_v2 import ConversationState, merge_new_state_with_previous


class TestProviderCompatibilityIssue:
    def test_merge_new_state_with_previous_doesnt_validate_provider_change(self):
        """
        VULNERABILITY: When provider changes but indicator stays the same,
        merge_new_state_with_previous carries forward the old provider's 
        resolved code without validating compatibility.
        
        Turn 1: Query "GDP" with FRED -> resolves to "CPIAUCSL" (FRED code)
        Turn 2: User says "use WORLDBANK instead"
        
        Expected: resolved_indicator_code should be None (FRED code invalid for WORLDBANK)
        Actual: resolved_indicator_code="CPIAUCSL" (invalid code persists)
        """
        # Turn 1: FRED resolves GDP to CPIAUCSL
        previous = ConversationState(
            indicator="GDP",
            provider="FRED",
            resolved_indicator_code="CPIAUCSL",  # FRED-specific code
            country="US",
            turn_number=0,
        )
        
        # Turn 2: Provider changes to WORLDBANK (but indicator name stays)
        new_state = ConversationState(
            indicator="GDP",
            provider="WORLDBANK",  # Provider changed
            country="US",
            turn_number=1,
            # resolved_indicator_code is not set - it will be carried forward incorrectly
        )
        
        merged = merge_new_state_with_previous(new_state, previous)
        
        # PROBLEM: The FRED code is carried forward
        assert merged.resolved_indicator_code == "CPIAUCSL"
        assert merged.provider == "WORLDBANK"
        # This is WRONG - CPIAUCSL is not a valid WORLDBANK indicator code


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
