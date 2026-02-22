from __future__ import annotations

from backend.agents.router_agent import RouterAgent
from backend.memory.conversation_state import ConversationState, DataReference, EntityContext, QueryType
from backend.memory.state_manager import ConversationStateManager


def _sample_ref(ref_id: str = "ref-1") -> DataReference:
    return DataReference(
        id=ref_id,
        query="imports as % of gdp in china",
        provider="IMF",
        indicator="imports as % of GDP",
        country="CN",
        countries=["CN"],
        time_range=("2010-01-01", "2024-12-31"),
        chart_type="line",
    )


def test_entity_context_tracks_dataset_fields() -> None:
    ctx = EntityContext()
    ref = _sample_ref("r1")

    ctx.add_dataset(ref)

    assert ctx.get_last_dataset() is ref
    assert "CN" in ctx.current_countries
    assert "imports as % of GDP" in ctx.current_indicators
    assert ctx.current_provider == "IMF"
    assert ctx.current_time_range == ("2010-01-01", "2024-12-31")


def test_conversation_state_coerces_dict_data_references() -> None:
    payload = {
        "id": "dict-ref",
        "provider": "WorldBank",
        "indicator": "exports as % of GDP",
        "country": "GB",
        "countries": ["GB", "CN"],
        "time_range": ("2012-01-01", "2023-12-31"),
    }

    state = ConversationState(conversation_id="c1", data_references={"dict-ref": payload})

    refs = state.get_all_data_references()
    assert len(refs) == 1
    assert refs[0].id == "dict-ref"
    assert refs[0].provider == "WorldBank"
    assert state.entity_context.get_last_dataset() is not None
    assert state.entity_context.current_provider == "WorldBank"


def test_conversation_state_add_reference_assigns_id_when_missing() -> None:
    state = ConversationState(conversation_id="c2")
    ref = DataReference(provider="FRED", indicator="GDP", country="US")

    state.add_data_reference(ref)

    refs = state.get_all_data_references()
    assert len(refs) == 1
    assert refs[0].id
    assert refs[0].id in state.get_data_references_map()


def test_state_manager_update_merges_data_references() -> None:
    manager = ConversationStateManager()
    conv_id = manager.get_or_create("conv-state-test")
    manager.update(
        conv_id,
        data_references={
            "a1": {
                "provider": "IMF",
                "indicator": "public debt",
                "country": "US",
            }
        },
    )

    state = manager.get(conv_id)
    assert state is not None
    assert len(state.get_all_data_references()) == 1
    assert state.entity_context.current_provider == "IMF"


def test_router_agent_follow_up_uses_last_dataset_context() -> None:
    state = ConversationState(conversation_id="c3")
    state.add_data_reference(_sample_ref("follow-up-ref"))

    router = RouterAgent()
    result = router.classify("plot it on the same graph", state)

    assert result.query_type == QueryType.FOLLOW_UP
    assert result.context.get("follow_up_mode") is True
    assert result.context.get("base_dataset") is not None
    assert result.context["base_dataset"].id == "follow-up-ref"
