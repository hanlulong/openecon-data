"""A country-scope reset must not resurrect the previous turn's sub-region.

Live (browser, 2026-07-17): "Ontario unemployment rate" then 加拿大失业率
(Canada unemployment) — the parse correctly nulled subnationalRegion for the
national turn, but merge_new_state_with_previous carried Ontario back in
(same country, same indicator -> the carry saw "nothing changed"), and the
subnational fail-closed check then discarded correct national data. The parse
now classifies sub-region->whole-country as followUpType=country_change;
extract_state_from_intent stamps it as scope_reset, which gates the carry.
"""
from __future__ import annotations

from backend.models import ParsedIntent
from backend.services.conversation_state_v2 import (
    ConversationState,
    extract_state_from_intent,
    merge_new_state_with_previous,
)


def _prev_ontario() -> ConversationState:
    return ConversationState(
        indicator="unemployment rate",
        country="Canada",
        subnational_region="Ontario",
    )


def test_scope_reset_blocks_region_carry() -> None:
    new = ConversationState(
        indicator="unemployment rate", country="Canada", scope_reset=True,
    )
    merged = merge_new_state_with_previous(new, _prev_ontario())
    assert merged.subnational_region is None


def test_plain_followup_still_carries_region() -> None:
    # "show 2020-2024": no scope reset -> Ontario must survive.
    new = ConversationState(indicator="unemployment rate", country="Canada")
    merged = merge_new_state_with_previous(new, _prev_ontario())
    assert merged.subnational_region == "Ontario"


def test_extract_stamps_scope_reset_from_country_change() -> None:
    intent = ParsedIntent(
        apiProvider="StatsCan",
        indicators=["unemployment rate"],
        parameters={"country": "CA"},
        clarificationNeeded=False,
        isFollowUp=True,
        followUpType="country_change",
        originalQuery="加拿大失业率",
    )
    state = extract_state_from_intent(intent)
    assert state.scope_reset is True

    intent.followUpType = "time_change"
    assert extract_state_from_intent(intent).scope_reset is None
