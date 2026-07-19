from __future__ import annotations

from backend.services.simplified_prompt import SimplifiedPrompt


def test_simplified_prompt_is_compact_and_extraction_focused() -> None:
    prompt = SimplifiedPrompt.generate()

    # Guardrail: keep prompt compact to reduce token overhead and policy drift.
    # Bumped 260 -> 280 for the multilingual additions (English-canonical
    # indicators rule, subnationalRegion extraction, language detection);
    # 280 -> 290 for the FX currency-pair extraction rule (baseCurrency/
    # targetCurrency from the user's wording in any language);
    # 290 -> 295 for frequency-aware provider routing (FRED international
    # monthly series vs annual-only WorldBank — the WB silent-substitution
    # class found in 2026-07-16 real-user analytics);
    # 295 -> 300 for the sub-region->whole-country scope-reset rule (a carried
    # subnationalRegion discarded correct national data in live browser
    # testing 2026-07-17: "Ontario unemployment" then "加拿大失业率");
    # 300 -> 305 for the colloquial-headline-release rule ("jobs numbers" is
    # NOT ambiguous — parse-level clarification hit the battery + real users);
    # 305 -> 315 for the ChinaMacro provider entry + China routing rule
    # (2026-07-19 — new provider closing the China high-frequency gap,
    # user-approved; the #1 real-user failure class).
    assert len(prompt.splitlines()) < 315
    assert "Return JSON only" in prompt
    assert "Select apiProvider using the PROVIDER CAPABILITIES" in prompt
    # Provider matrix should always be included
    assert "PROVIDER CAPABILITIES" in prompt
    assert "FRED" in prompt
    assert "WorldBank" in prompt


def test_simplified_prompt_routes_government_fiscal_ratios_to_imf() -> None:
    """Concept-class routing: general-government debt/fiscal ratios are IMF-first for
    all countries (incl. the US), so "US debt to GDP" resolves to IMF's general
    government gross debt series rather than FRED's household-debt verbatim matches.
    FRED is reserved for explicit US federal-specific series or household/private debt."""
    prompt = SimplifiedPrompt.generate()

    assert "GENERAL-GOVERNMENT DEBT AGGREGATES" in prompt
    assert "IMF for ALL countries, INCLUDING the US" in prompt
    assert "federal debt held by the public" in prompt
    assert "household/private debt" in prompt


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
    # in the system prompt for LLM-driven routing — cycle 36; to 330 after
    # adding relative-period date examples "last N months"/YTD; to 355 after
    # the multilingual additions — subnationalRegion + language + English
    # canonical indicators; to 370 after the FX currency-pair rule and the
    # scope-reset + colloquial-headline rules (2026-07-17); to 380 with them —
    # keep-region-in-indicator subnational examples; to 390 for the ChinaMacro
    # provider matrix entry (2026-07-19 — new provider closing the China
    # high-frequency coverage gap, user-approved); to 395 for the
    # English-China capture lines (analytics 07-19 evening: ~51 China-macro
    # failures/36h were ENGLISH-phrased and routed FRED/WB — rule 10 is now
    # language-explicit and FRED's entry hands China off))
    assert len(prompt.splitlines()) < 395


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
