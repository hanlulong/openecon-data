"""A shared place name must not authorize a different geographic extent."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services import geographic_identity as geo
from backend.services.conversation_state_v2 import AnswerSetMember, ConversationState
from backend.services.delta_extractor import DeltaExtractor
from backend.services.provider_fallback import is_fallback_relevant
from backend.services.query_helpers import apply_country_overrides
from backend.utils.jurisdictions import ambiguous_country_alias, geographic_name_collisions, has_geographic_collision


def _intent(country="Georgia", region=None, language="en", query_type="data_fetch"):
    return ParsedIntent(apiProvider="WorldBank", indicators=["GDP"], parameters={"country": country},
                        originalQuery="Georgia GDP", subnationalRegion=region, language=language,
                        queryType=query_type, clarificationNeeded=False)


@pytest.fixture
def pending(monkeypatch):
    saved = []
    monkeypatch.setattr(geo.conversation_manager, "set_pending_semantic_clarification",
                        lambda cid, payload: saved.append((cid, payload)))
    return saved


def test_registry_derives_multiple_real_country_subdivision_collisions():
    assert {x["id"] for x in geographic_name_collisions("Georgia GDP")[0]["entities"]} == {
        "country:GE", "subdivision:US-GA",
    }
    assert has_geographic_collision("GDP in Luxembourg")
    assert has_geographic_collision("Niger GDP")
    assert has_geographic_collision("Georgia's neighboring Georgian economy")
    assert not has_geographic_collision("Canada GDP")
    assert not has_geographic_collision("California GDP")
    assert not has_geographic_collision("Georgian GDP")
    assert not has_geographic_collision("Guadeloupe GDP")
    assert not has_geographic_collision("Guam GDP")
    assert ambiguous_country_alias("Georgia", "GE")
    assert not ambiguous_country_alias("Georgia", "US")


async def test_bare_name_becomes_durable_choices_with_complete_requeries(monkeypatch, pending):
    judgment = geo.GeographicIdentityDecision(outcome="clarify", entity_ids=["country:GE", "subdivision:US-GA"])
    monkeypatch.setattr(geo, "_structured_judgment", AsyncMock(return_value=judgment))
    response = await geo.resolve_geographic_identity(SimpleNamespace(), "Georgia GDP since 2020", _intent(), "geo-c")
    assert response.clarificationNeeded
    assert len(response.clarificationOptions) == 2
    assert "country" in response.clarificationOptions[0].label
    assert "United States" in response.clarificationOptions[1].label
    assert all("since 2020" in x.query and "GDP" in x.query for x in response.clarificationOptions)
    assert all(x.value == x.query for x in response.clarificationOptions)
    assert pending[-1][1]["kind"] == "geography"
    assert pending[-1][1]["entities"]["2"]["id"] == "subdivision:US-GA"


@pytest.mark.parametrize("entity,country,region", [
    ("country:GE", "GE", None), ("subdivision:US-GA", "US", "Georgia"),
])
async def test_model_selected_scope_is_not_added_as_two_countries(monkeypatch, pending, entity, country, region):
    monkeypatch.setattr(geo, "_structured_judgment", AsyncMock(return_value=
                        geo.GeographicIdentityDecision(outcome="select", entity_ids=[entity])))
    intent = _intent()
    intent.parameters = {"countries": ["US", "GE"]}
    response = await geo.resolve_geographic_identity(SimpleNamespace(), "GDP of the US state of Georgia", intent, "c")
    assert response is None
    assert intent.parameters["country"] == country
    assert "countries" not in intent.parameters
    assert intent.subnationalRegion == region
    assert not pending


async def test_known_identity_is_stable_on_the_following_parse(monkeypatch, pending):
    judge = AsyncMock(side_effect=AssertionError("a bound geographic identity must not be guessed again"))
    monkeypatch.setattr(geo, "_structured_judgment", judge)
    intent = _intent(country="US", region="Georgia")
    intent.parameters["__geographic_entity"] = "subdivision:US-GA"
    assert await geo.resolve_geographic_identity(SimpleNamespace(), "Georgia GDP", intent, "c") is None
    judge.assert_not_awaited()


async def test_informational_question_does_not_become_a_geography_interrogation(monkeypatch, pending):
    judge = AsyncMock(side_effect=AssertionError("informational path owns this question"))
    monkeypatch.setattr(geo, "_structured_judgment", judge)
    intent = _intent(query_type="informational")
    assert await geo.resolve_geographic_identity(SimpleNamespace(), "What is Georgia?", intent, "c") is None
    judge.assert_not_awaited()


@pytest.mark.parametrize("decision", [None,
    geo.GeographicIdentityDecision(outcome="select", entity_ids=["invented"]),
    geo.GeographicIdentityDecision(outcome="clarify", entity_ids=["country:GE"]),
])
async def test_unverified_bare_identity_asks_registered_choices_without_choosing(monkeypatch, pending, decision):
    monkeypatch.setattr(geo, "_structured_judgment", AsyncMock(return_value=decision))
    response = await geo.resolve_geographic_identity(SimpleNamespace(), "Georgia GDP", _intent(language="zh"), "c")
    assert response.clarificationNeeded and not response.error
    assert len(response.clarificationOptions) == 2
    assert not response.data
    assert "地区" in response.clarificationQuestions[0]


@pytest.mark.parametrize("query,country,region", [
    ("GDP of the country of Georgia", "Georgia", None),
    ("GDP of the US state of Georgia", "United States", "Georgia"),
])
async def test_explicit_qualified_scope_uses_corroborated_identity_without_a_new_guess(monkeypatch, pending, query, country, region):
    judge = AsyncMock(side_effect=AssertionError("qualified identity is corroborated by parser and registry"))
    monkeypatch.setattr(geo, "_structured_judgment", judge)
    intent = _intent(country=country, region=region)
    assert await geo.resolve_geographic_identity(SimpleNamespace(), query, intent, "c") is None
    assert intent.parameters["country"] == ("GE" if region is None else "US")
    assert intent.subnationalRegion == region
    judge.assert_not_awaited()


def test_country_alias_fill_preserves_the_parent_of_a_parsed_subdivision():
    intent = _intent(country="United States", region="Georgia")
    apply_country_overrides(SimpleNamespace(), intent, "GDP of the US state of Georgia")
    assert intent.parameters == {"country": "United States"}


@pytest.mark.parametrize("query,slot,country", [
    ("also Mexico", "added_countries", "MX"),
    ("remove Mexico", "removed_countries", "MX"),
    ("compare with Georgia", "added_countries", "GE"),
])
def test_verified_country_collection_preserves_structural_country_role(query, slot, country):
    countries = ["PE", "CO", "BR"] + (["MX"] if slot == "removed_countries" else [])
    state = ConversationState(
        indicator="inflation rate", provider="WORLDBANK", countries=countries,
        active_answer_members=[AnswerSetMember(provider="WORLDBANK", country=code) for code in countries],
    )
    delta = DeltaExtractor(SimpleNamespace()).extract(query, state)

    assert getattr(delta, slot) == [country]
    assert geo.preserves_verified_country_collection(state, delta)


@pytest.mark.parametrize("query,override", [
    ("Georgia", {}),
    ("also the US state of Georgia", {}),
    ("also Mexico", {"subnational_region": "Georgia"}),
    ("also Mexico", {"decomposition": {"type": "provinces"}}),
    ("also Mexico", {"active_answer_members": []}),
    ("also Mexico", {"active_answer_members": [AnswerSetMember(provider="WORLDBANK")]}),
])
def test_unproven_or_explicitly_different_scope_requires_identity_adjudication(query, override):
    countries = ["PE", "CO", "BR"]
    state = ConversationState(
        indicator="inflation rate", provider="WORLDBANK", countries=countries,
        active_answer_members=[AnswerSetMember(provider="WORLDBANK", country=code) for code in countries],
    ).model_copy(update=override)
    delta = DeltaExtractor(SimpleNamespace()).extract(query, state)

    assert not geo.preserves_verified_country_collection(state, delta)


@pytest.mark.parametrize("stale_region", [None, "Georgia"])
async def test_country_identity_decision_keeps_an_independently_named_peer(monkeypatch, pending, stale_region):
    judge = AsyncMock(return_value=geo.GeographicIdentityDecision(outcome="select", entity_ids=["country:GE"]))
    monkeypatch.setattr(geo, "_structured_judgment", judge)
    intent = _intent(region=stale_region)
    intent.parameters = {"countries": ["US", "GE", "PE"]}
    query = "Compare GDP in the United States, Georgia and Peru"

    assert await geo.resolve_geographic_identity(SimpleNamespace(), query, intent, "c") is None

    judge.assert_awaited_once()
    assert set(intent.parameters["countries"]) == {"US", "GE", "PE"}
    assert intent.parameters["__geographic_entity"] == "country:GE"
    assert intent.subnationalRegion is None


@pytest.mark.parametrize("ids,accepted", [
    (["country:MX"], True),
    (["country:MX", "country:PE", "country:CO", "country:MX"], True),
    (["country:MX", "country:US"], False),  # Parsed, omitted from the request.
    (["country:MX", "country:BR"], False),  # Mentioned, absent from parsed scope.
    (["country:MX", "invented"], False),
    (["country:MX", "subdivision:US-GA"], False),
    (["country:MX", "subdivision:MX-MEX"], False),
    (["country:PE", "country:CO"], False),
])
async def test_model_can_select_the_collision_alongside_known_peer_ids(monkeypatch, pending, ids, accepted):
    judgment = geo.GeographicIdentityDecision(outcome="select", entity_ids=ids)
    monkeypatch.setattr(geo, "_structured_judgment", AsyncMock(return_value=judgment))
    intent = _intent(country="MX")
    countries = ["MX", "PE", "CO"]
    if "country:BR" not in ids:
        countries.append("BR")
    if "country:US" in ids:
        countries.append("US")
    intent.parameters = {"countries": countries}

    response = await geo.resolve_geographic_identity(
        SimpleNamespace(), "inflation in Peru, Colombia, Brazil and Mexico", intent, "c",
    )

    if accepted:
        assert response is None
        assert intent.parameters["__geographic_entity"] == "country:MX"
        assert set(intent.parameters["countries"]) == {"MX", "PE", "CO", "BR"}
    else:
        assert response.clarificationNeeded
        assert len(response.clarificationOptions) == 2


def test_unknown_returned_geography_is_not_proof_of_a_known_requested_country():
    def series(country):
        return NormalizedData(metadata=Metadata(source="FRED", indicator="Georgia GDP", country=country,
                             frequency="annual", unit="USD", seriesId="GANGSP"),
                             data=[{"date": "2025-01-01", "value": 1.0}])
    assert not is_fallback_relevant(["GDP"], [series(None)], ["GE"], "Georgia GDP")
    assert not is_fallback_relevant(["GDP"], [series("US")], ["GE"], "Georgia GDP")
    assert is_fallback_relevant(["GDP"], [series("GE")], ["GE"], "Georgia GDP")
