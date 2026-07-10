"""Guard tests for the silent-empty-response chokepoint.

Historically ~14% of production traffic returned ``data=[]`` with no error, no
clarification, and no explanation — a blank result the user could not act on.
``_finalize_empty_data_response`` is the single response-assembly chokepoint
(applied to every ``process_query`` return, shared by the non-stream and SSE
paths): a completed ``data_fetch`` response with empty data and no error /
clarification / text answer gets a structured, scope-aware explanation. Every
other response class passes through untouched, and the transform is idempotent.
"""
from __future__ import annotations

from backend.models import Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.services.query import QueryService


def _svc() -> QueryService:
    return QueryService.__new__(QueryService)


def _intent(query_type: str = "data_fetch", **params) -> ParsedIntent:
    return ParsedIntent(
        apiProvider="FRED",
        indicators=["GDP"],
        parameters=params or {"country": "US", "startDate": "2000", "endDate": "2020"},
        clarificationNeeded=False,
        queryType=query_type,
    )


def _response(**kwargs) -> QueryResponse:
    base = dict(conversationId="conv", clarificationNeeded=False)
    base.update(kwargs)
    return QueryResponse(**base)


def _series() -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(source="FRED", indicator="GDP", frequency="annual", unit="Billions"),
        data=[{"date": "2020-01-01", "value": 100.0}],
    )


def test_empty_data_fetch_gets_explanatory_error():
    resp = _response(
        intent=_intent(country="Canada", startDate="2015", endDate="2020"),
        data=[],
    )
    out = _svc()._finalize_empty_data_response(resp)
    assert out.error == "no_data_found"
    assert out.message
    # Scope details are woven into the explanation.
    assert "GDP" in out.message
    assert "Canada" in out.message
    assert "FRED" in out.message


def test_none_data_data_fetch_gets_explanatory_error():
    resp = _response(intent=_intent(), data=None)
    out = _svc()._finalize_empty_data_response(resp)
    assert out.error == "no_data_found"


def test_clarification_response_untouched():
    resp = _response(
        intent=_intent(),
        data=[],
        clarificationNeeded=True,
        clarificationQuestions=["Which country?"],
    )
    out = _svc()._finalize_empty_data_response(resp)
    assert out.error is None


def test_informational_text_answer_untouched():
    resp = _response(
        intent=_intent(query_type="informational"),
        data=[],
        message="Here are the indicators World Bank publishes for GDP.",
    )
    out = _svc()._finalize_empty_data_response(resp)
    assert out.error is None
    assert out.message.startswith("Here are the indicators")


def test_existing_error_untouched():
    resp = _response(intent=_intent(), data=[], error="provider_timeout", message="Timed out")
    out = _svc()._finalize_empty_data_response(resp)
    assert out.error == "provider_timeout"
    assert out.message == "Timed out"


def test_normal_data_response_untouched():
    resp = _response(intent=_intent(), data=[_series()])
    out = _svc()._finalize_empty_data_response(resp)
    assert out.error is None
    assert out.message is None


def test_empty_data_with_existing_message_not_clobbered():
    # An empty data_fetch that already carries a text explanation is not the
    # silent-empty bug — leave its message alone and do not stamp an error.
    resp = _response(intent=_intent(), data=[], message="Partial coverage note.")
    out = _svc()._finalize_empty_data_response(resp)
    assert out.error is None
    assert out.message == "Partial coverage note."


def test_finalization_is_idempotent():
    resp = _response(intent=_intent(), data=[])
    svc = _svc()
    once = svc._finalize_empty_data_response(resp)
    twice = svc._finalize_empty_data_response(once)
    assert once.error == twice.error == "no_data_found"
    assert once.message == twice.message
