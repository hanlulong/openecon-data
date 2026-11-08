"""A no-verdict failure never triggers a same-provider VARIANT re-probe.

Variant re-probes (alternate code, unwindowed window probe, broadened window,
Comtrade M,X envelope / historical window, Eurostat sparse history, StatsCan
dynamic->vector, Comtrade adjudicated-HS, the World Bank fallback ladder)
exist to recover from a VERDICT: the provider answered "empty / 4xx / no
rows". After a no-verdict (ProviderUnavailable: timeouts, 5xx, breaker,
cooldown, request deadline) the re-probe asks the same unanswering provider
again, proves nothing about the series, and burns the request deadline
(production 2026-11-08: Comtrade ReadTimeout x3 -> M,X envelope -> another
~90s -> 504).

The gate is ONE type-only predicate, ``variant_retry_allowed``; fan-outs that
swallow per-sub-request errors combine them with ``aggregate_failure`` so a
pure outage stays typed. Verdict paths keep every existing re-probe.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from tenacity import wait_none

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.providers import base as base_module
from backend.providers import comtrade as comtrade_module
from backend.providers import worldbank as worldbank_module
from backend.providers.comtrade import ComtradeProvider
from backend.providers.imf import IMFProvider
from backend.providers.worldbank import WorldBankProvider
from backend.services import data_fetcher as DF
from backend.services import rate_limiter as rates
from backend.services.query import QueryService
from backend.services.rate_limiter import ProviderRateLimiter, RateLimiterConfig
from backend.utils.request_budget import request_budget
from backend.utils.retry import (
    DataNotAvailableError,
    ProviderCoolingDown,
    ProviderUnavailable,
    RequestBudgetExhausted,
    aggregate_failure,
    is_no_verdict,
    is_provider_unavailable,
    variant_retry_allowed,
)


def _series(code="X1", country="US", source="FRED", points=(("2024-01-01", 1.0),)):
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=code, country=country,
            frequency="annual", unit="x", seriesId=code,
        ),
        data=[{"date": d, "value": v} for d, v in points],
    )


@pytest.fixture(autouse=True)
def _isolated_limiter_and_breakers(monkeypatch):
    monkeypatch.setattr(rates, "_global_rate_limiter", rates.GlobalRateLimiter())
    monkeypatch.setattr(base_module, "_provider_breakers", {})
    monkeypatch.setattr(base_module, "wait_exponential", lambda **kwargs: wait_none())
    limiters = rates.get_global_rate_limiter()._limiters
    for key in ("COMTRADE", "WORLDBANK"):
        limiters[key] = ProviderRateLimiter(
            RateLimiterConfig(key, min_delay_seconds=0, max_requests_per_minute=10_000)
        )


# ---------------------------------------------------------------------------
# The predicate and the aggregator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailable("down"),
        ProviderCoolingDown("429", retry_after_seconds=3),
        RequestBudgetExhausted("deadline"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
        asyncio.TimeoutError(),
    ],
    ids=["unavailable", "cooldown", "budget", "read_timeout", "connect_error", "asyncio_timeout"],
)
def test_no_verdicts_never_allow_a_variant_retry(error):
    assert is_no_verdict(error)
    assert variant_retry_allowed(error) is False


@pytest.mark.parametrize(
    "error", [None, DataNotAvailableError("API returned 404"), ValueError("bad payload")],
    ids=["clean_empty", "plain_verdict", "other_error"],
)
def test_verdicts_keep_variant_retries(error):
    assert variant_retry_allowed(error) is True


def test_is_provider_unavailable_is_unchanged_for_a_bare_timeout():
    assert is_provider_unavailable(TimeoutError()) is False
    assert issubclass(RequestBudgetExhausted, ProviderUnavailable)
    assert issubclass(ProviderUnavailable, DataNotAvailableError)


_VERDICT = DataNotAvailableError("No data found")


def test_aggregate_failure_matrix():
    pu = ProviderUnavailable("timed out")
    both = aggregate_failure([pu, ProviderUnavailable("503")], _VERDICT, context="ctx")
    assert type(both) is ProviderUnavailable and str(both).startswith("ctx: ")

    cooling = aggregate_failure([pu, ProviderCoolingDown("429", retry_after_seconds=7)], _VERDICT, context="c")
    assert isinstance(cooling, ProviderCoolingDown) and cooling.retry_after_seconds == 7

    for answered in (None, DataNotAvailableError("API returned 404"), _series(), []):
        assert aggregate_failure([answered, pu], _VERDICT, context="c") is _VERDICT
    assert aggregate_failure([], _VERDICT, context="c") is _VERDICT

    raw = httpx.ReadTimeout("read timed out")
    wrapped = aggregate_failure([raw], _VERDICT, context="Eurostat X")
    assert type(wrapped) is ProviderUnavailable and wrapped.__cause__ is raw
    assert "Eurostat X: ReadTimeout" in str(wrapped)

    assert is_provider_unavailable(aggregate_failure([pu], None, context="c"))
    assert aggregate_failure([None], None, context="c") is None
    budget = aggregate_failure([RequestBudgetExhausted("deadline")], _VERDICT, context="c")
    assert type(budget) is RequestBudgetExhausted


# ---------------------------------------------------------------------------
# query._fetch_data: the same-provider alternate-code retry
# ---------------------------------------------------------------------------


def _llm_pick_intent():
    return ParsedIntent(
        apiProvider="COMTRADE",
        indicators=["vegetable oil exports"],
        clarificationNeeded=False,
        parameters={"indicator": "15", "__decision_source": "llm_pick", "reporter": "US"},
    )


async def test_alternate_code_retry_never_runs_after_an_outage():
    svc = QueryService.__new__(QueryService)
    intent = _llm_pick_intent()
    fetch = AsyncMock(side_effect=ProviderUnavailable("Comtrade: retries exhausted"))
    with patch("backend.services.query._df_fetch_data", new=fetch):
        with pytest.raises(ProviderUnavailable):
            await svc._fetch_data(intent, None)
    assert fetch.await_count == 1
    params = intent.parameters
    assert "__exclude_indicator_codes" not in params
    assert params["indicator"] == "15"
    assert params["__decision_source"] == "llm_pick"


async def test_alternate_code_retry_still_runs_after_a_verdict():
    svc = QueryService.__new__(QueryService)
    intent = _llm_pick_intent()
    calls = []

    async def _fetch(_svc, it, execution_plan=None):
        calls.append(dict(it.parameters))
        if len(calls) == 1:
            raise DataNotAvailableError("UN Comtrade returned no records")
        return [_series("1507", source="UN Comtrade")]

    with patch("backend.services.query._df_fetch_data", side_effect=_fetch):
        data = await svc._fetch_data(intent, None)
    assert len(calls) == 2 and data
    assert calls[1].get("__exclude_indicator_codes") == ["15"]


# ---------------------------------------------------------------------------
# data_fetcher: unwindowed window probe / broadened retry
# ---------------------------------------------------------------------------


def _df_svc():
    return SimpleNamespace(
        _MAX_ALTERNATE_CODE_RETRIES=2,
        _preflight_geographic_split=AsyncMock(return_value=None),
        _maybe_expand_ranking_country_scope=lambda query, provider, params: params,
        _resolve_indicator_for_fetch=AsyncMock(side_effect=lambda provider, request, params: params),
        _get_from_cache=AsyncMock(return_value=None),
        _save_to_cache=AsyncMock(),
        _normalize_bis_metadata_labels=lambda result: None,
        _looks_like_provider_indicator_code=lambda provider, value: True,
    )


def _windowed_intent():
    return ParsedIntent(
        apiProvider="FRED", indicators=["GDPC1"],
        parameters={
            "indicator": "GDPC1", "country": "US", "__semantic_authority": "exact_user_input",
            "startDate": "2024-01-01", "endDate": "2024-12-31", "__time_scope_authority": "user",
        },
        originalQuery="US real GDP 2024", clarificationNeeded=False,
    )


@pytest.mark.parametrize(
    "first_error,expect_probe",
    [(ProviderUnavailable("FRED: retries exhausted"), False), (DataNotAvailableError("empty"), True)],
    ids=["outage_no_probe", "verdict_probes"],
)
def test_window_probe_only_after_a_verdict(monkeypatch, first_error, expect_probe):
    calls = []

    async def _dispatch(svc, intent, execution_plan):  # noqa: ARG001
        calls.append(dict(getattr(execution_plan, "params", None) or intent.parameters or {}))
        if len(calls) == 1:
            raise first_error
        return [_series("GDPC1", points=(("2023-01-01", 1.0),))]

    monkeypatch.setattr(DF, "fetch_from_provider_dispatch", _dispatch)
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(DF.fetch_data(_df_svc(), _windowed_intent()))
    if expect_probe:
        assert len(calls) == 2
        assert "startDate" not in calls[1]          # the unwindowed probe ran
    else:
        assert len(calls) == 1                      # no probe, no broadened retry
        assert type(info.value) is ProviderUnavailable
        assert all(call.get("startDate") == "2024-01-01" for call in calls)


# ---------------------------------------------------------------------------
# Comtrade (real fetch_trade_data over an httpx MockTransport)
# ---------------------------------------------------------------------------


def _comtrade_row(reporter_desc="USA", period="2024", flow="Export"):
    return {
        "period": period, "flowDesc": flow, "cmdCode": "15",
        "cmdDesc": "Animal, vegetable or microbial fats and oils", "primaryValue": 1.5e9,
        "reporterDesc": reporter_desc, "partnerDesc": "World",
    }


@pytest.fixture
def comtrade_upstream(monkeypatch):
    """Fake UN Comtrade: ``behaviour(params) -> 'timeout' | 'hang' | Response | rows``."""
    log: list[dict] = []
    state = {"behaviour": lambda params: "timeout"}

    async def handler(request: httpx.Request):
        params = dict(request.url.params)
        log.append(params)
        outcome = state["behaviour"](params)
        if outcome == "timeout":
            await asyncio.sleep(request.extensions["timeout"]["read"])
            raise httpx.ReadTimeout("timed out", request=request)
        if outcome == "hang":
            await asyncio.sleep(5)
            raise httpx.ReadTimeout("timed out", request=request)
        if isinstance(outcome, httpx.Response):
            return outcome
        return httpx.Response(200, json={"data": outcome})

    monkeypatch.setattr(
        comtrade_module, "get_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(comtrade_module, "REQUEST_TIMEOUT", 0.3)
    monkeypatch.setattr(comtrade_module, "GLOBAL_REQUEST_MIN_INTERVAL_SECONDS", 0.0)
    return SimpleNamespace(log=log, state=state)


def _comtrade_fetch(**kwargs):
    kwargs.setdefault("reporter", "US")
    kwargs.setdefault("commodity", "15")
    kwargs.setdefault("flow", "exports")
    return asyncio.run(ComtradeProvider(api_key="demo").fetch_trade_data(**kwargs))


def test_comtrade_outage_never_runs_the_flow_envelope(comtrade_upstream):
    with pytest.raises(ProviderUnavailable):
        _comtrade_fetch(start_year=2024, end_year=2024)
    log = comtrade_upstream.log
    assert len(log) == base_module.BaseProvider.MAX_RETRIES
    assert all(p["flowCode"] == "X" for p in log)
    assert not any(p["flowCode"] == "M,X" for p in log)


def test_comtrade_outage_never_runs_the_historical_window(comtrade_upstream):
    with pytest.raises(ProviderUnavailable):
        _comtrade_fetch()  # implicit default period
    years = {int(year) for p in comtrade_upstream.log for year in p["period"].split(",")}
    assert years and min(years) >= 2020  # nothing from the 2002-.. historical window
    assert all(p["flowCode"] == "X" for p in comtrade_upstream.log)


def test_comtrade_clean_empty_keeps_the_flow_envelope(comtrade_upstream):
    comtrade_upstream.state["behaviour"] = lambda params: []
    with pytest.raises(DataNotAvailableError) as info:
        _comtrade_fetch(start_year=2024, end_year=2024)
    envelope = [p for p in comtrade_upstream.log if p["flowCode"] == "M,X"]
    assert len(envelope) == 1
    assert "UN Comtrade returned no records" in str(info.value)
    assert not is_provider_unavailable(info.value)


def test_comtrade_fan_out_stops_at_the_first_outage(comtrade_upstream, monkeypatch):
    # US (842) answers, DE (276) times out, FR (251) must never be asked.
    comtrade_upstream.state["behaviour"] = lambda params: (
        [_comtrade_row()] if params["reporterCode"] == "842" else "timeout"
    )
    disclosed = {}
    original = ComtradeProvider._disclose_unreported_trailing_years  # a staticmethod

    def _spy(*args, **kwargs):
        disclosed.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(ComtradeProvider, "_disclose_unreported_trailing_years", staticmethod(_spy))
    result = _comtrade_fetch(reporter=None, reporters=["US", "DE", "FR"], start_year=2024, end_year=2024)
    reporters = [p["reporterCode"] for p in comtrade_upstream.log]
    assert "251" not in reporters
    assert reporters.count("276") == base_module.BaseProvider.MAX_RETRIES
    assert result and result[0].data
    assert disclosed.get("fetch_complete") is False


def test_comtrade_fan_out_all_timing_out_asks_only_the_first_reporter(comtrade_upstream):
    with pytest.raises(ProviderUnavailable):
        _comtrade_fetch(reporter=None, reporters=["US", "DE", "FR"], start_year=2024, end_year=2024)
    assert {p["reporterCode"] for p in comtrade_upstream.log} == {"842"}


def test_comtrade_overall_budget_is_typed_no_verdict(comtrade_upstream, monkeypatch):
    comtrade_upstream.state["behaviour"] = lambda params: "hang"
    monkeypatch.setattr(comtrade_module, "_comtrade_overall_time_budget", lambda n: 0.2)
    monkeypatch.setattr(comtrade_module, "REQUEST_TIMEOUT", 10.0)
    with pytest.raises(ProviderUnavailable) as info:
        _comtrade_fetch(start_year=2024, end_year=2024)
    assert type(info.value) is ProviderUnavailable
    assert "returned no records" not in str(info.value)
    assert not any(p["flowCode"] == "M,X" for p in comtrade_upstream.log)


def test_comtrade_overall_budget_bound_by_the_request_deadline(comtrade_upstream, monkeypatch):
    comtrade_upstream.state["behaviour"] = lambda params: "hang"
    monkeypatch.setattr(comtrade_module, "REQUEST_TIMEOUT", 10.0)

    async def _go():
        with request_budget(1.5, answer_reserve_seconds=1.0, retry_reserve_seconds=1.0):
            return await ComtradeProvider(api_key="demo").fetch_trade_data(
                reporter="US", commodity="15", flow="exports", start_year=2024, end_year=2024,
            )

    with pytest.raises(RequestBudgetExhausted):
        asyncio.run(_go())


def test_comtrade_quota_403_is_a_cooldown(comtrade_upstream):
    comtrade_upstream.state["behaviour"] = lambda params: httpx.Response(
        403, headers={"Retry-After": "30"}, text=json.dumps({"message": "Out of call volume quota"}),
    )
    with pytest.raises(ProviderCoolingDown) as info:
        _comtrade_fetch(start_year=2024, end_year=2024)
    assert info.value.retry_after_seconds == 30
    assert len(comtrade_upstream.log) == 1


# ---------------------------------------------------------------------------
# World Bank fetch_indicator outcome tracking
# ---------------------------------------------------------------------------


_EMPTY_WB = [{"page": 1, "pages": 1, "per_page": 1000, "total": 0}, None]


@pytest.fixture
def wb_upstream(monkeypatch):
    log: list[str] = []
    state = {"behaviour": lambda url: "connect_error"}

    async def handler(request: httpx.Request):
        log.append(str(request.url.path))
        outcome = state["behaviour"](str(request.url.path))
        if outcome == "connect_error":
            raise httpx.ConnectError("connection refused", request=request)
        if isinstance(outcome, int):
            return httpx.Response(outcome, json=[{"message": [{"value": "x"}]}])
        return httpx.Response(200, json=outcome)

    monkeypatch.setattr(
        worldbank_module, "get_http1_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = WorldBankProvider()
    monkeypatch.setattr(provider, "_indicator_source_id", lambda code: None)
    alternatives = AsyncMock(return_value=[])
    source_endpoint = AsyncMock(return_value=[])
    monkeypatch.setattr(provider, "_get_alternative_indicators", alternatives)
    monkeypatch.setattr(provider, "_fetch_source_series_endpoint", source_endpoint)
    return SimpleNamespace(
        provider=provider, log=log, state=state,
        alternatives=alternatives, source_endpoint=source_endpoint,
    )


def test_wb_outage_skips_the_verdict_ladder(wb_upstream, monkeypatch):
    provider = wb_upstream.provider
    # A natural label resolved to a code (not an exact request): the
    # alternatives ladder would normally run after a no-data result.
    monkeypatch.setattr(provider, "_resolve_indicator_code", AsyncMock(return_value="SP.POP.TOTL"))
    with pytest.raises(ProviderUnavailable) as info:
        asyncio.run(provider.fetch_indicator(indicator="population", country="IN"))
    assert "World Bank did not answer" in str(info.value)
    wb_upstream.alternatives.assert_not_awaited()
    wb_upstream.source_endpoint.assert_not_awaited()


_FOUR = ["US", "GB", "FR", "DE"]


def test_wb_multi_country_outage_is_typed(wb_upstream):
    with pytest.raises(ProviderUnavailable):
        asyncio.run(wb_upstream.provider.fetch_indicator(indicator="SP.POP.TOTL", countries=_FOUR))
    assert any(";" in path for path in wb_upstream.log)        # the batch was tried
    assert len(wb_upstream.log) > base_module.BaseProvider.MAX_RETRIES  # and per-country recovery ran


def test_wb_batch_outage_but_countries_answered_empty_is_a_verdict(wb_upstream):
    wb_upstream.state["behaviour"] = lambda path: "connect_error" if ";" in path else _EMPTY_WB
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(wb_upstream.provider.fetch_indicator(indicator="SP.POP.TOTL", countries=_FOUR))
    assert not is_provider_unavailable(info.value)
    assert "No data found" in str(info.value)


def test_wb_all_404_is_a_verdict(wb_upstream):
    wb_upstream.state["behaviour"] = lambda path: 404
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(wb_upstream.provider.fetch_indicator(indicator="SP.POP.TOTL", countries=_FOUR))
    assert not is_provider_unavailable(info.value)
    assert "No data found" in str(info.value)


# ---------------------------------------------------------------------------
# Eurostat multi-country aggregate and sparse-history retry (data_fetcher)
# ---------------------------------------------------------------------------


def _eurostat_intent(country="Benelux"):
    return ParsedIntent(
        apiProvider="EUROSTAT", indicators=["une_rt_m"], clarificationNeeded=False,
        parameters={"indicator": "une_rt_m", "country": country},
        originalQuery=f"{country} unemployment rate",
    )


def _eurostat_svc(side_effect):
    fetch = AsyncMock(side_effect=side_effect)
    return SimpleNamespace(eurostat_provider=SimpleNamespace(fetch_indicator=fetch)), fetch


_NOT_DISSEMINATED = DataNotAvailableError(
    "Eurostat dataset une_rt_m is not disseminated for this narrowed query (persistent 5xx)"
)


@pytest.mark.parametrize(
    "outcomes,expect_typed",
    [
        ({"BE": ProviderUnavailable("down"), "NL": ProviderUnavailable("down"),
          "LU": ProviderUnavailable("down")}, True),
        ({"BE": DataNotAvailableError("API returned 404"), "NL": ProviderUnavailable("down"),
          "LU": ProviderUnavailable("down")}, False),
        ({"BE": _NOT_DISSEMINATED, "NL": _NOT_DISSEMINATED, "LU": _NOT_DISSEMINATED}, False),
    ],
    ids=["all_outage", "one_answered_404", "deliberate_not_disseminated_verdict"],
)
def test_eurostat_multi_country_aggregate(outcomes, expect_typed):
    async def _fetch(**kwargs):
        raise outcomes[kwargs["country"]]

    svc, _fetch_mock = _eurostat_svc(_fetch)
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(DF._fetch_from_eurostat(svc, _eurostat_intent(), {"indicator": "une_rt_m", "country": "Benelux"}))
    assert is_provider_unavailable(info.value) is expect_typed
    if not expect_typed:
        assert "No Eurostat data available" in str(info.value)


@pytest.mark.parametrize(
    "error,expected_calls",
    [(ProviderUnavailable("down"), 1), (DataNotAvailableError("empty"), 2)],
    ids=["outage_no_history_retry", "verdict_history_retry"],
)
def test_eurostat_sparse_history_retry_only_after_a_verdict(error, expected_calls):
    from datetime import datetime

    svc, fetch = _eurostat_svc(error)
    plan = SimpleNamespace(provider_request={"dataset_code": "une_rt_m", "start_year": datetime.utcnow().year - 2})
    with pytest.raises(DataNotAvailableError):
        asyncio.run(DF._fetch_from_eurostat(svc, _eurostat_intent("DE"), {"indicator": "une_rt_m", "country": "DE"}, plan))
    assert fetch.await_count == expected_calls
    if expected_calls == 2:
        assert fetch.await_args_list[1].kwargs["start_year"] == 1990


# ---------------------------------------------------------------------------
# Multi-indicator aggregate
# ---------------------------------------------------------------------------


def _multi_svc():
    return SimpleNamespace(
        _detect_explicit_provider=lambda *_a, **_k: "",
        _normalize_provider_alias=lambda x: x or None,
        _select_routed_provider=AsyncMock(return_value="FRED"),
    )


def _multi_intent():
    return ParsedIntent(
        apiProvider="FRED", indicators=["GDP", "inflation"], parameters={"country": "BR"},
        clarificationNeeded=False, originalQuery="Brazil GDP and inflation",
    )


@pytest.mark.parametrize(
    "legs,expect_typed",
    [
        ({"GDP": ProviderUnavailable("down"), "inflation": ProviderUnavailable("down")}, True),
        ({"GDP": ProviderUnavailable("down"), "inflation": asyncio.TimeoutError("timed out")}, True),
        ({"GDP": DataNotAvailableError("no data"), "inflation": ProviderUnavailable("down")}, False),
    ],
    ids=["all_outage", "outage_and_timeout", "one_verdict"],
)
def test_multi_indicator_all_failed_aggregate(legs, expect_typed, monkeypatch):
    async def _fetch(_svc, single_intent):
        raise legs[single_intent.indicators[0]]

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(DF, "fetch_data", _fetch)
    monkeypatch.setattr("backend.utils.retry.asyncio.sleep", _no_sleep)
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(DF.fetch_multi_indicator_data(_multi_svc(), _multi_intent()))
    assert is_provider_unavailable(info.value) is expect_typed
    if not expect_typed:
        assert "Could not fetch any of the requested indicators" in str(info.value)


# ---------------------------------------------------------------------------
# IMF BOP lane, StatsCan dynamic->vector, Comtrade adjudicated-HS retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,typed", [(ProviderUnavailable("timed out"), True), (DataNotAvailableError("empty"), False)],
    ids=["outage_keeps_type", "verdict_keeps_message"],
)
def test_imf_bop_lane_keeps_the_no_verdict_type(error, typed):
    provider = IMFProvider.__new__(IMFProvider)
    provider._get_imf_dataflow_structure = AsyncMock(return_value={})
    provider._build_bop_sdmx_series_candidates = lambda **kwargs: ["candidate"]
    provider._fetch_sdmx_exact_indicator_family = AsyncMock(side_effect=error)
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(provider._fetch_bop_family(
            indicator_code="BCA", indicator_label="current account", countries=["US"],
            start_year=2020, end_year=2024,
        ))
    assert is_provider_unavailable(info.value) is typed
    if typed:
        assert str(info.value).startswith("IMF BOP public SDMX request for BCA did not complete")
    else:
        assert "returned no observations" in str(info.value)


def _statscan_intent():
    return ParsedIntent(
        apiProvider="STATSCAN", indicators=["number of households"],
        parameters={
            "country": "CA", "indicator": "17100159",
            "__semantic_indicator_label": "number of households",
            "startDate": "2017-01-01", "endDate": "2017-12-31", "periods": 240,
        },
        clarificationNeeded=False, originalQuery="number of households in Canada in 2017",
    )


@pytest.mark.parametrize(
    "error,vector_called", [(ProviderUnavailable("down"), False), (DataNotAvailableError("no table"), True)],
    ids=["outage_no_vector_fetch", "verdict_vector_fetch"],
)
def test_statscan_dynamic_to_vector_only_after_a_verdict(error, vector_called):
    fetch_series = AsyncMock(return_value=_series("v1", country="CA", source="Statistics Canada"))
    statscan_provider = SimpleNamespace(
        PRODUCT_ID_CACHE={},
        fetch_dynamic_data=AsyncMock(side_effect=error),
        fetch_series=fetch_series,
    )
    svc = SimpleNamespace(statscan_provider=statscan_provider)
    intent = _statscan_intent()
    if vector_called:
        asyncio.run(DF._fetch_from_statscan(svc, intent, dict(intent.parameters)))
        fetch_series.assert_awaited_once()
    else:
        with pytest.raises(ProviderUnavailable):
            asyncio.run(DF._fetch_from_statscan(svc, intent, dict(intent.parameters)))
        fetch_series.assert_not_awaited()
    statscan_provider.fetch_dynamic_data.assert_awaited_once()


def test_comtrade_adjudicated_hs_retry_never_follows_a_no_verdict():
    # Belt: even a no-verdict whose text carries a resolution marker is never
    # re-dispatched with the adjudicated code (the gate is the type).
    from backend.models import ExecutionPlan

    calls = []

    class _Comtrade:
        async def fetch_trade_data(self, commodity=None, **kwargs):
            calls.append(commodity)
            raise ProviderUnavailable(
                f"comtrade_commodity_unresolved: '{commodity}' (reference fetch timed out)"
            )

    svc = SimpleNamespace(
        comtrade_provider=_Comtrade(),
        _normalize_country_to_iso2=lambda value: {"india": "IN", "russia": "RU"}.get(str(value).lower(), value),
    )
    params = {
        "reporter": "India", "partner": "Russia", "flow": "IMPORT",
        "commodity": "petroleum oils crude", "indicator": "2709",
        "__semantic_authority": "llm_adjudication",
        "__semantic_indicator_label": "petroleum oils crude imports",
    }
    intent = ParsedIntent(
        apiProvider="COMTRADE", indicators=["imports"], parameters=params,
        clarificationNeeded=False, originalQuery="India imports from Russia",
    )
    plan = ExecutionPlan(provider="COMTRADE", candidate_id="t", fetch_strategy="provider_dispatch", params=params)
    with pytest.raises(ProviderUnavailable):
        asyncio.run(DF.fetch_from_provider_dispatch(svc, intent, plan))
    assert calls == ["petroleum oils crude"]
