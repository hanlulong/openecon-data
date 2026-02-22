from __future__ import annotations

from backend.services.simplified_prompt import SimplifiedPrompt


def test_simplified_prompt_is_compact_and_extraction_focused() -> None:
    prompt = SimplifiedPrompt.generate()

    # Guardrail: keep prompt compact to reduce token overhead and policy drift.
    assert len(prompt.splitlines()) < 260
    assert "Return JSON only" in prompt
    assert "Do not do provider routing strategy" in prompt



def test_simplified_prompt_avoids_hardcoded_provider_routing_rules() -> None:
    prompt = SimplifiedPrompt.generate().lower()

    banned_phrases = [
        "oecd rate limiting",
        "provider selection hierarchy",
        "regional keyword mappings",
        "use sparingly",
    ]

    for phrase in banned_phrases:
        assert phrase not in prompt
