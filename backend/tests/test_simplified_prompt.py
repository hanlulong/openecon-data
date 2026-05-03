from __future__ import annotations

from backend.services.simplified_prompt import SimplifiedPrompt


def test_simplified_prompt_is_compact_and_extraction_focused() -> None:
    prompt = SimplifiedPrompt.generate()

    # Guardrail: keep prompt compact to reduce token overhead and policy drift.
    assert len(prompt.splitlines()) < 260
    assert "Return JSON only" in prompt
    assert "Select apiProvider using the PROVIDER CAPABILITIES" in prompt
    # Provider matrix should always be included
    assert "PROVIDER CAPABILITIES" in prompt
    assert "FRED" in prompt
    assert "WorldBank" in prompt


def test_simplified_prompt_with_conversation_context() -> None:
    ctx = {
        "indicator": "GDP",
        "country": "United States",
        "provider": "FRED",
        "startDate": "2020-01-01",
        "endDate": "2024-12-31",
        "originalQuery": "GDP in US from 2020 to 2024",
    }
    prompt = SimplifiedPrompt.generate(conversation_context=ctx)

    # Should include follow-up section
    assert "CONVERSATION CONTEXT" in prompt
    assert "isFollowUp" in prompt
    assert "followUpType" in prompt
    assert "resolvedQuery" in prompt
    assert "GDP" in prompt
    assert "United States" in prompt
    assert "FRED" in prompt

    # Guardrail: even with context, prompt should remain under limit
    # (Expanded from 300 to 320 after enriching provider selection rules
    # in the system prompt for LLM-driven routing — cycle 36)
    assert len(prompt.splitlines()) < 320


def test_simplified_prompt_without_context_has_no_follow_up_section() -> None:
    prompt = SimplifiedPrompt.generate()
    assert "CONVERSATION CONTEXT" not in prompt
    assert "Previous query" not in prompt


def test_simplified_prompt_with_clarification_context() -> None:
    """Phase 4: When pendingClarification is set, the prompt includes clarification-specific instructions."""
    ctx = {
        "indicator": "trade",
        "country": "China",
        "provider": "WorldBank",
        "startDate": "not specified",
        "endDate": "not specified",
        "originalQuery": "trade data China",
        "pendingClarification": True,
        "clarificationQuestion": "Do you want exports, imports, or trade balance?",
        "clarificationOptions": "exports, imports, trade balance",
    }
    prompt = SimplifiedPrompt.generate(conversation_context=ctx)

    # Should include clarification resolution instructions
    assert "clarification question" in prompt.lower()
    assert "exports, imports, trade balance" in prompt
    assert "clarification_answer" in prompt
    assert "China" in prompt
    # The clarification context section should appear after the base prompt
    assert "CONVERSATION CONTEXT" in prompt


def test_simplified_prompt_non_clarification_follow_up_has_normal_rules() -> None:
    """When no pending clarification, the prompt uses normal follow-up rules."""
    ctx = {
        "indicator": "GDP",
        "country": "US",
        "provider": "FRED",
        "startDate": "2020-01-01",
        "endDate": "2024-12-31",
        "originalQuery": "GDP in US",
    }
    prompt = SimplifiedPrompt.generate(conversation_context=ctx)

    # Should have normal follow-up rules
    assert "country_change" in prompt
    assert "indicator_switch" in prompt
    # Should NOT have clarification-specific instructions
    assert "clarification question" not in prompt.lower().split("conversation context")[0]


def test_simplified_prompt_avoids_hardcoded_provider_routing_rules() -> None:
    prompt = SimplifiedPrompt.generate().lower()

    banned_phrases = [
        "oecd rate limiting",
        "provider selection hierarchy",
        "regional keyword mappings",
        "use sparingly",
        "catalog",
    ]

    for phrase in banned_phrases:
        assert phrase not in prompt


def test_simplified_prompt_preserves_count_metrics_without_provider_codes() -> None:
    prompt = SimplifiedPrompt.generate()

    assert "never provider-native IDs/codes" in prompt
    assert "unless the user explicitly" in prompt
    assert "Do not convert count/number questions into financial stock concepts" in prompt
    assert "For any direct" in prompt
    assert "census/demographic counts" in prompt
