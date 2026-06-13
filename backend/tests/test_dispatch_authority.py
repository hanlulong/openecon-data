"""No-shortcut dispatch authority for state-carried indicator codes.

Regression for the "France GDP" → "what about Italy" class: a code that
reached conversation state via a verified fetch must keep dispatch authority
on indicator-unchanged delta turns; an indicator change revokes it.
"""
from __future__ import annotations

from backend.services.data_fetcher import _has_provider_map_authority


def test_verified_state_authority_requires_delta_and_unchanged_indicator():
    base = {"__semantic_authority": "verified_conversation_state"}

    assert _has_provider_map_authority(
        {**base, "__delta_resolved": True, "__delta_indicator_changed": False}
    )
    # Indicator changed → must re-resolve, no carried authority.
    assert not _has_provider_map_authority(
        {**base, "__delta_resolved": True, "__delta_indicator_changed": True}
    )
    # Not a delta turn → the stamp alone grants nothing.
    assert not _has_provider_map_authority(base)


def test_strong_authorities_unchanged():
    for authority in ("exact_user_input", "llm_adjudication", "post_fetch_semantic_judge"):
        assert _has_provider_map_authority({"__semantic_authority": authority})
    assert not _has_provider_map_authority({})
    assert not _has_provider_map_authority({"__semantic_authority": "something_else"})


def test_materialized_state_stamps_verified_authority():
    from backend.services.conversation_state_v2 import ConversationState, materialize_intent

    state = ConversationState(
        indicator="GDP",
        country="France",
        provider="EUROSTAT",
        resolved_indicator_code="TIPSNA10",
    )
    intent = materialize_intent(state)
    params = intent.parameters or {}
    assert params.get("__semantic_authority") == "verified_conversation_state"


def test_materialized_state_without_resolved_code_stamps_nothing():
    from backend.services.conversation_state_v2 import ConversationState, materialize_intent

    state = ConversationState(indicator="GDP", country="France", provider="EUROSTAT")
    intent = materialize_intent(state)
    params = intent.parameters or {}
    assert "__semantic_authority" not in params
