"""Adding a country by an alias must not duplicate an existing member.

The additive country merge dedup'd on raw strings while the removal path
normalized to ISO2, so "GDP of US and Japan" then "also add the United States"
yielded ["US", "JP", "United States"] and plotted the US twice. The add path
now dedups by ISO2 identity too.
"""

from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    merge_state,
)


def _state(countries):
    st = ConversationState()
    st.countries = list(countries)
    return st


def test_alias_addition_does_not_duplicate():
    m = merge_state(_state(["US", "JP"]), FollowUpDelta(added_countries=["United States"], delta_type="country_change"))
    assert (m.countries or [m.country]) == ["US", "JP"]


def test_real_addition_preserved():
    m = merge_state(_state(["US", "JP"]), FollowUpDelta(added_countries=["Germany"], delta_type="country_change"))
    assert m.countries == ["US", "JP", "Germany"]


def test_iso_code_alias_not_duplicated():
    m = merge_state(_state(["Germany"]), FollowUpDelta(added_countries=["DE"], delta_type="country_change"))
    assert (m.countries or [m.country]) == ["Germany"]
