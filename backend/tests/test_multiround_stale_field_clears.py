"""Provider/indicator switches must clear the old turn's provider-scoped fields.

M1: a provider switch left coin_ids (a CoinGecko-only id) in state.
M2: an indicator switch left decomposition ("by province"), so "Canada CPI by
    province" then "show GDP instead" returned GDP broken out by province.
Both mirror clears already present for sibling fields / country changes.
"""

from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    merge_state,
)


def test_indicator_switch_clears_decomposition():
    st = ConversationState()
    st.indicator = "CPI"
    st.country = "Canada"
    st.decomposition = {"type": "provinces"}
    merged = merge_state(st, FollowUpDelta(changed_indicator="GDP", delta_type="indicator_change"))
    assert merged.indicator == "GDP"
    assert merged.decomposition is None


def test_dimension_modifier_change_keeps_decomposition():
    # A refinement ("CPI" -> "CPI shelter") is not a full indicator switch.
    st = ConversationState()
    st.indicator = "CPI"
    st.decomposition = {"type": "provinces"}
    merged = merge_state(
        st,
        FollowUpDelta(
            changed_indicator="CPI shelter",
            is_dimension_modifier_change=True,
            delta_type="indicator_change",
        ),
    )
    assert merged.decomposition == {"type": "provinces"}


def test_provider_switch_clears_coin_ids():
    st = ConversationState()
    st.indicator = "bitcoin price"
    st.coin_ids = ["bitcoin"]
    st.provider = "COINGECKO"
    merged = merge_state(st, FollowUpDelta(changed_provider="FRED", delta_type="provider_change"))
    assert merged.provider == "FRED"
    assert merged.coin_ids is None
