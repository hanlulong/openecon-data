"""Guard tests for carrying subnationalRegion + language through state (B, C).

subnational_region is topic-specific (carried while metric+geography are stable,
cleared on a change); language is sticky for the whole conversation.
"""
from __future__ import annotations

from backend.models import ParsedIntent
from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    extract_state_from_intent,
    materialize_intent,
    merge_new_state_with_previous,
    merge_state,
)


def _intent(**kw):
    base = dict(
        apiProvider="WorldBank", indicators=["GDP"], clarificationNeeded=False,
        parameters={"country": "China"}, originalQuery="北京GDP",
    )
    base.update(kw)
    intent = ParsedIntent(**{k: v for k, v in base.items() if k not in {"subnationalRegion", "language"}})
    intent.subnationalRegion = kw.get("subnationalRegion", "Beijing")
    intent.language = kw.get("language", "zh")
    return intent


def test_extract_captures_both_fields():
    st = extract_state_from_intent(_intent())
    assert st.subnational_region == "Beijing"
    assert st.language == "zh"


def test_materialize_round_trips_both_fields():
    st = ConversationState(indicator="GDP", country="China",
                           subnational_region="Beijing", language="zh")
    mi = materialize_intent(st)
    assert mi.subnationalRegion == "Beijing"
    assert mi.language == "zh"


def test_language_is_sticky_across_turns():
    prev = ConversationState(indicator="GDP", country="China", language="zh", turn_number=1)
    new = ConversationState(indicator="GDP", country="China")  # delta omitted language
    merged = merge_new_state_with_previous(new, prev)
    assert merged.language == "zh"


def test_region_survives_same_metric_and_geography():
    prev = ConversationState(indicator="GDP", country="China",
                             subnational_region="Beijing", turn_number=1)
    # Same country restated (even canonicalized) is NOT a geography change.
    assert merge_new_state_with_previous(
        ConversationState(indicator="GDP", country="China"), prev
    ).subnational_region == "Beijing"
    assert merge_new_state_with_previous(
        ConversationState(indicator="GDP", country="CN"), prev
    ).subnational_region == "Beijing"


def test_region_cleared_on_indicator_change():
    prev = ConversationState(indicator="GDP", country="China",
                             subnational_region="Beijing", turn_number=1)
    merged = merge_new_state_with_previous(
        ConversationState(indicator="inflation", country="China"), prev
    )
    assert merged.subnational_region is None


def test_region_cleared_on_geography_change():
    prev = ConversationState(indicator="GDP", country="China",
                             subnational_region="Beijing", turn_number=1)
    merged = merge_new_state_with_previous(
        ConversationState(indicator="GDP", country="United States"), prev
    )
    assert merged.subnational_region is None


def test_new_turn_region_wins_over_carry():
    prev = ConversationState(indicator="GDP", country="China",
                             subnational_region="Beijing", turn_number=1)
    merged = merge_new_state_with_previous(
        ConversationState(indicator="GDP", country="China", subnational_region="Shanghai"),
        prev,
    )
    assert merged.subnational_region == "Shanghai"


def test_delta_new_query_keeps_language_resets_region():
    cur = ConversationState(indicator="GDP", country="China",
                            subnational_region="Beijing", language="zh", turn_number=2)
    delta = FollowUpDelta(is_new_query=True, changed_indicator="inflation",
                          changed_country="Japan", raw_query="日本通胀")
    out = merge_state(cur, delta)
    assert out.language == "zh"
    assert out.subnational_region is None


def test_delta_indicator_change_clears_region_keeps_language():
    cur = ConversationState(indicator="GDP", country="China",
                            subnational_region="Beijing", language="zh", turn_number=2)
    out = merge_state(cur, FollowUpDelta(changed_indicator="inflation", raw_query="通胀"))
    assert out.subnational_region is None
    assert out.language == "zh"


def test_state_survives_model_validate_roundtrip():
    st = ConversationState(indicator="GDP", country="China",
                           subnational_region="Beijing", language="zh")
    restored = ConversationState.model_validate(st.model_dump())
    assert restored.subnational_region == "Beijing"
    assert restored.language == "zh"
