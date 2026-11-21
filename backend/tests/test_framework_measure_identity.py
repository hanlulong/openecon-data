from unittest.mock import AsyncMock

import httpx
import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.providers.statscan import StatsCanProvider
from backend.services import semantic_match_judge
from backend.services.conversation_state_v2 import AnswerSetMember, ConversationState, FollowUpDelta
from backend.services.query import QueryService
from backend.utils.retry import DataNotAvailableError, ProviderUnavailable, ResultVerificationUnavailable, aggregate_failure, variant_retry_allowed


def series(source, code, unit, *, country="Canada"):
    return NormalizedData(
        metadata=Metadata(source=source, seriesId=code, indicator="Economic measure",
                          country=country, unit=unit, frequency="quarterly"),
        data=[{"date": "2025-01-01", "value": 123.0}],
    )


def dimension(name, *labels):
    return {"dimensionNameEn": name, "member": [
        {"memberId": i, "memberNameEn": label} for i, label in enumerate(labels, 1)
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("measure,labels,pick", [
    ("GDP", ("Compensation of employees", "Gross domestic product at market prices"), 2),
    ("total wholesale sales", ("Wholesale sales, all industries", "Motor vehicles"), 1),
])
async def test_statscan_executes_model_chosen_coordinate_and_exposes_members(measure, labels, pick):
    provider = StatsCanProvider.__new__(StatsCanProvider)
    provider.semantic_coordinate_selector = AsyncMock(return_value=[2, pick])
    provider.fetch_by_coordinate = AsyncMock(return_value=series("Statistics Canada", "12345678:2.2", "Dollars"))
    cube = {"cubeTitleEn": "Contribution and total estimates", "dimension": [
        dimension("Geography", "Ontario", "Canada"), dimension("Estimates", *labels),
    ]}

    result = await provider._fetch_adjudicated_coordinate(
        "12345678", cube, measure, "Canada", periods=12, start_date=None, end_date=None,
    )

    evidence = provider.semantic_coordinate_selector.await_args.kwargs["axes"]
    assert evidence[0]["members"] == [{"id": 2, "label": "Canada"}]
    assert set(m["label"] for m in evidence[1]["members"]) == set(labels)
    assert provider.fetch_by_coordinate.await_args.args[0]["coordinate"].split(".")[:2] == ["2", str(pick)]
    assert f"Estimates: {labels[pick - 1]}" in result.metadata.description


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [None, [1, 1], [2, 99]])
async def test_statscan_rejects_unknown_or_out_of_scope_member_without_fetching(choice):
    provider = StatsCanProvider.__new__(StatsCanProvider)
    provider.semantic_coordinate_selector = AsyncMock(return_value=choice)
    provider.fetch_by_coordinate = AsyncMock()
    cube = {"dimension": [dimension("Geography", "Ontario", "Canada"), dimension("Measure", "Total")]}

    with pytest.raises(DataNotAvailableError):
        await provider._fetch_adjudicated_coordinate(
            "12345678", cube, "total retail sales", "Canada", periods=12, start_date=None, end_date=None,
        )
    provider.fetch_by_coordinate.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_measure_checks_changed_identity_with_prior_metadata(monkeypatch):
    previous = {"concept": "GDP per capita", "series": [{
        "provider": "FRED", "code": "PRIOR", "unit": "Current USD",
        "priceType": "Current prices", "title": "GDP per capita",
    }]}
    candidate = series("World Bank", "NEXT", "2010 USD")
    judge = AsyncMock(return_value=semantic_match_judge.SemanticMatchJudgment(decision="mismatch", confidence=0.99))
    monkeypatch.setattr(semantic_match_judge, "judge_fallback_result", judge)
    assert not await semantic_match_judge.verify_followup_measure(object(), previous, [candidate], query="what about Brazil")
    assert judge.await_args.kwargs["prior_measure"] == previous
    assert judge.await_args.kwargs["fallback_result"] == [candidate]


@pytest.mark.asyncio
async def test_retained_identity_with_changed_frequency_requires_a_verdict(monkeypatch):
    previous = {"concept": "employment", "series": [{
        "provider": "FRED", "code": "SHARED", "unit": "Persons", "frequency": "annual",
    }]}
    candidate = series("FRED", "SHARED", "Persons")
    judge = AsyncMock(return_value=None)
    monkeypatch.setattr(semantic_match_judge, "judge_fallback_result", judge)
    assert await semantic_match_judge.verify_followup_measure(object(), previous, [candidate], query="what about France") is None
    judge.assert_awaited_once()


@pytest.mark.parametrize("status,expected", [(401, ProviderUnavailable), (403, ProviderUnavailable), (404, DataNotAvailableError)])
def test_statscan_access_refusals_are_distinct_from_missing_data(status, expected):
    response = httpx.Response(status, request=httpx.Request("GET", "https://example.test/statscan"))
    with pytest.raises(expected) as exc:
        StatsCanProvider._raise_for_status_or_data_unavailable(response, "checking a published table")
    assert type(exc.value) is expected


def test_local_verification_failure_keeps_its_own_user_outcome():
    service = QueryService.__new__(QueryService)
    failure = ResultVerificationUnavailable("No model verdict")
    assert not variant_retry_allowed(failure)
    assert aggregate_failure([failure, ProviderUnavailable("Primary provider timed out")], None, context="multi") is failure
    intent = ParsedIntent(apiProvider="FRED", indicators=["employment"], clarificationNeeded=False)
    response = service._build_no_data_indicator_clarification("test", "what about France", intent, attempt_error=failure)
    assert response.error == "verification_unavailable"
    assert not response.clarificationNeeded
    assert "couldn't verify" in response.message


@pytest.mark.asyncio
async def test_provider_fallback_cannot_collapse_a_collection_to_first_measure():
    service = QueryService.__new__(QueryService)
    intent = ParsedIntent(apiProvider="WORLDBANK", indicators=["unemployment", "inflation"],
                          parameters={"country": "PE"}, clarificationNeeded=False)
    outage = DataNotAvailableError("Both measures could not be served")
    with pytest.raises(DataNotAvailableError) as error:
        await service._try_with_fallback(intent, outage)
    assert error.value is outage
    assert intent.indicators == ["unemployment", "inflation"]


def test_same_country_cube_breakdown_is_not_a_geographic_collection():
    service = QueryService.__new__(QueryService)
    state = ConversationState(indicator="unemployment rate", provider="STATSCAN", active_answer_members=[
        AnswerSetMember(provider="STATSCAN", country="CA", series_id="12345678:1.1"),
        AnswerSetMember(provider="STATSCAN", country="Canada", series_id="12345678:1.2"),
    ])
    assert not service._should_use_collective_answer_member_delta(state, FollowUpDelta(changed_indicator="GDP"))
