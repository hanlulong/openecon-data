"""A provider switch must drop the old provider's resolved code and dimensions.

base_indicator was resolved against the old provider's catalog and dimensions
are that provider's coordinate codes. The provider-change merge cleared the
resolved code and StatsCan IDs but left base_indicator/dimensions, so
materialize_intent dispatched the StatsCan vector + Sex filter to the new
provider ("Canada unemployment by sex" → "use Eurostat"). They're now cleared;
the semantic indicator persists for clean re-resolution.
"""

from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    merge_state,
)


def test_provider_switch_clears_provider_scoped_resolution():
    st = ConversationState()
    st.indicator = "unemployment rate"
    st.base_indicator = "UNEMPLOYMENT_RATE"
    st.provider = "STATSCAN"
    st.dimensions = {"Sex": "Females"}
    st.statscan_product_id = "14100375"

    merged = merge_state(st, FollowUpDelta(changed_provider="EUROSTAT", delta_type="provider_change"))

    assert merged.provider == "EUROSTAT"
    assert merged.indicator == "unemployment rate"  # semantic concept persists
    assert merged.base_indicator is None
    assert merged.dimensions is None
    assert merged.statscan_product_id is None
    assert merged.resolved_indicator_code is None
