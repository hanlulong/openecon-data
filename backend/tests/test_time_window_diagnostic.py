"""A broad diagnostic must preserve the user's bounded data/identity contract."""

import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import ExecutionPlan, Metadata, NormalizedData, ParsedIntent
from backend.services.query import QueryService
from backend.utils.retry import DataNotAvailableError


def series(points):
    return NormalizedData(
        metadata=Metadata(
            source="Eurostat", indicator="Cost per training hour", country="IT",
            frequency="annual", unit="EUR", seriesId="trng_cvt_20s",
        ),
        data=[{"date": date, "value": value} for date, value in points],
    )


def intent(authority="user", query="Italy cost per training hour from Eurostat 2021-2024"):
    return ParsedIntent(
        apiProvider="EUROSTAT", indicators=["TRNG_CVT_20S"],
        parameters={
            "country": "IT", "indicator": "TRNG_CVT_20S",
            "startDate": "2021-01-01", "endDate": "2024-12-31", "sex": "F",
            "__semantic_authority": "exact_user_input", "__time_scope_authority": authority,
        },
        originalQuery=query, clarificationNeeded=False,
    )


def service(responses):
    replies = iter(responses)
    calls = []

    async def fetch(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        result = next(replies)
        if isinstance(result, BaseException):
            raise result
        return result

    svc = SimpleNamespace(
        _MAX_ALTERNATE_CODE_RETRIES=2,
        _preflight_geographic_split=AsyncMock(return_value=None),
        _maybe_expand_ranking_country_scope=lambda query, provider, params: params,
        _resolve_indicator_for_fetch=AsyncMock(side_effect=lambda provider, request, params: params),
        _get_from_cache=AsyncMock(return_value=None),
        _save_to_cache=AsyncMock(),
        _normalize_bis_metadata_labels=lambda result: None,
        eurostat_provider=SimpleNamespace(fetch_indicator=AsyncMock(side_effect=fetch)),
    )
    return svc, calls


@pytest.mark.parametrize("first", ["raised", "empty"])
def test_diagnostic_recovers_only_in_window_data_with_original_identity_and_cache(first):
    broad = series([("2019-01-01", 9), ("2023-01-01", 23), ("2025-01-01", 25)])
    original_data = broad.model_dump()
    initial = DataNotAvailableError("isolated missing window") if first == "raised" else series([])
    svc, calls = service([initial, broad])
    request = intent()
    plan = ExecutionPlan(provider="EUROSTAT", candidate_id="EUROSTAT:TRNG_CVT_20S", fetch_strategy="provider_dispatch", params={})
    result = asyncio.run(QueryService._fetch_data(svc, request, execution_plan=plan))
    assert [(point.date, point.value) for point in result[0].data] == [("2023-01-01", 23)]
    assert broad.model_dump() == original_data, "Do not modify diagnostic/cached provider objects"
    assert len(calls) == 2
    assert [(c["start_year"], c["end_year"]) for c in calls] == [(2021, 2024), (None, None)]
    assert all(c["indicator"] == "trng_cvt_20s" and c["country"] == "IT" and c["filters"] == {"sex": "F"} for c in calls)
    assert request.parameters["startDate"] == "2021-01-01"
    assert request.parameters["endDate"] == "2024-12-31"
    assert plan.provider_request["start_year"] == 2021 and plan.provider_request["end_year"] == 2024
    svc._resolve_indicator_for_fetch.assert_awaited_once()
    svc._save_to_cache.assert_awaited_once()
    lookup = svc._get_from_cache.await_args.args
    saved = svc._save_to_cache.await_args.args
    assert saved[:2] == lookup
    saved_series = saved[2][0] if isinstance(saved[2], list) else saved[2]
    assert [(p.date, p.value) for p in saved_series.data] == [("2023-01-01", 23)]


@pytest.mark.parametrize("first", ["raised", "empty"])
@pytest.mark.parametrize("diagnostic", ["failed", "empty", "unknown", "outside_unknown", "null", "outside_null"])
def test_inconclusive_diagnostic_does_not_claim_window_absence_or_retry_it(first, diagnostic):
    probes = {
        "failed": DataNotAvailableError("isolated diagnostic failed"),
        "empty": series([]),
        "unknown": series([("latest", 1)]),
        "outside_unknown": series([("2019-01-01", 1), ("latest", 2)]),
        "null": series([("2023-01-01", None)]),
        "outside_null": series([("2019-01-01", None)]),
    }
    initial = DataNotAvailableError("isolated initial failure") if first == "raised" else series([])
    svc, calls = service([initial, probes[diagnostic]])
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert "no_data_in_requested_window" not in str(failure.value)
    assert len(calls) == 2, "Only one diagnostic is permitted for a failed bounded request"
    svc._save_to_cache.assert_not_awaited()


@pytest.mark.parametrize("first", ["raised", "empty"])
def test_older_only_diagnostic_marks_window_absence_without_alternate_retry(first):
    initial = DataNotAvailableError("isolated initial failure") if first == "raised" else series([])
    svc, calls = service([initial, series([("2019-01-01", 1)])])
    with pytest.raises(DataNotAvailableError, match="no_data_in_requested_window.*latest_available=2019-01-01"):
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert len(calls) == 2
    svc._save_to_cache.assert_not_awaited()


def test_inherited_user_bounds_recover_without_current_turn_time_words():
    svc, calls = service([DataNotAvailableError("empty"), series([("2022-01-01", 1)])])
    result = asyncio.run(QueryService._fetch_data(svc, intent(query="Compare with Italy")))
    assert [point.date for point in result[0].data] == ["2022-01-01"]
    assert [(c["start_year"], c["end_year"]) for c in calls] == [(2021, 2024), (None, None)]


def test_recovery_excludes_unknown_missing_nonfinite_points_but_keeps_zero():
    broad = series([
        ("2020-01-01", 10), ("2023-01-01", 0), ("latest", 5),
        ("2022-01-01", None), ("2024-01-01", float("nan")),
    ])
    svc, calls = service([DataNotAvailableError("empty"), broad])
    result = asyncio.run(QueryService._fetch_data(svc, intent()))
    assert [(p.date, p.value) for p in result[0].data] == [("2023-01-01", 0)]
    assert len(broad.data) == 5 and len(calls) == 2


@pytest.mark.parametrize("points", [
    [("2019-01-01", 1), ("2023-01-01", float("inf"))],
    [("2019-01-01", 1), ("2023-01-01", float("nan"))],
])
def test_nonfinite_diagnostic_makes_outside_only_claim_inconclusive(points):
    broad = series(points)
    # Normal construction sanitizes nonfinite values; cached/mutated points
    # may bypass that validator, so exercise the diagnostic's own guard.
    broad.data[-1].value = points[-1][1]
    svc, _ = service([DataNotAvailableError("empty"), broad])
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert "no_data_in_requested_window" not in str(failure.value)
    svc._save_to_cache.assert_not_awaited()


@pytest.mark.parametrize("date", ["2023garbage", "2023-13-01", "2023-02-30", "2023-Q5", "2023-M13", "0000"])
def test_malformed_dates_cannot_prove_recovery_or_outside_only_coverage(date):
    svc, _ = service([DataNotAvailableError("empty"), series([("2019-01-01", 1), (date, 2)])])
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert "no_data_in_requested_window" not in str(failure.value)
    svc._save_to_cache.assert_not_awaited()


@pytest.mark.parametrize("date", ["2023", "2023-05", "2023-M5", "2023-Q2", "2023Q2", "2024-02-29", "2023-05-01T12:30:00Z"])
def test_valid_normalized_period_shapes_recover_at_existing_year_granularity(date):
    svc, _ = service([DataNotAvailableError("empty"), series([(date, 2)])])
    result = asyncio.run(QueryService._fetch_data(svc, intent()))
    assert [(p.date, p.value) for p in result[0].data] == [(date, 2)]


@pytest.mark.parametrize("value", ["bad", [], {}])
def test_malformed_raw_values_do_not_turn_a_diagnostic_into_server_error(value):
    broad = series([("2019-01-01", 1)])
    broad.data.append({"date": "2023-01-01", "value": value})
    svc, _ = service([DataNotAvailableError("empty"), broad])
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert "no_data_in_requested_window" not in str(failure.value)
    svc._save_to_cache.assert_not_awaited()


@pytest.mark.parametrize("diagnostic_error", [TimeoutError("isolated timeout"), RuntimeError("isolated failure")])
def test_failed_diagnostic_preserves_original_no_data_exception(diagnostic_error):
    initial = DataNotAvailableError("original bounded request has no data")
    svc, calls = service([initial, diagnostic_error])
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert failure.value is initial
    assert len(calls) == 2


@pytest.mark.parametrize("phase", ["primary", "diagnostic"])
def test_cancellation_propagates_without_cache_or_additional_probe(phase):
    responses = [asyncio.CancelledError()]
    if phase == "diagnostic":
        responses.insert(0, DataNotAvailableError("empty"))
    svc, calls = service(responses)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert len(calls) == (1 if phase == "primary" else 2)
    svc._save_to_cache.assert_not_awaited()


def test_non_no_data_primary_error_does_not_trigger_a_diagnostic():
    error = RuntimeError("isolated transport failure")
    svc, calls = service([error])
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert failure.value is error and len(calls) == 1


def test_existing_window_marker_is_not_probed_again():
    error = DataNotAvailableError("no_data_in_requested_window: existing provider marker")
    svc, calls = service([error])
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(QueryService._fetch_data(svc, intent()))
    assert failure.value is error and len(calls) == 1


def test_implicit_default_eurostat_sparse_history_retry_is_preserved():
    request = intent(authority="default", query="Italy cost per training hour")
    current_year = datetime.utcnow().year
    request.parameters["startDate"] = f"{current_year - 5}-01-01"
    request.parameters["endDate"] = f"{current_year}-12-31"
    svc, calls = service([DataNotAvailableError("no recent data"), series([("2019-01-01", 1)])])
    result = asyncio.run(QueryService._fetch_data(svc, request))
    assert [p.date for p in result[0].data] == ["2019-01-01"]
    assert [(c["start_year"], c["end_year"]) for c in calls] == [(current_year - 5, current_year), (1990, current_year)]


def test_unbounded_no_data_does_not_probe_without_any_user_bounds():
    request = intent()
    request.parameters.pop("startDate")
    request.parameters.pop("endDate")
    svc, calls = service([DataNotAvailableError("no data")])
    with pytest.raises(DataNotAvailableError):
        asyncio.run(QueryService._fetch_data(svc, request))
    assert len(calls) == 1


def test_recovered_data_passes_normal_validation_before_bounded_cache(monkeypatch):
    from backend.services import data_validator

    validation_events = []
    validator = SimpleNamespace(
        validate=lambda result: validation_events.append(result.model_dump()) or SimpleNamespace(valid=True, confidence=1),
        log_validation_results=lambda data, result: None,
    )
    monkeypatch.setattr(data_validator, "get_data_validator", lambda: validator)
    svc, _ = service([DataNotAvailableError("empty"), series([("2019-01-01", 1), ("2023-01-01", 3)])])
    result = asyncio.run(QueryService._fetch_data(svc, intent()))
    assert validation_events == [result[0].model_dump()]
    assert [p["date"] for p in validation_events[0]["data"]] == ["2023-01-01"]
    svc._save_to_cache.assert_awaited_once()
