"""An unavailable source must not turn an added country into a semantic failure."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.providers import worldbank as wb_module
from backend.providers.worldbank import WorldBankProvider
from backend.services import query as query_module
from backend.services.conversation_state_v2 import AnswerSetMember, ConversationState, FollowUpDelta
from backend.services.query import QueryService
from backend.services.http_pool import extended_timeout
from backend.utils.request_budget import request_budget
from backend.utils.retry import DataNotAvailableError, ProviderCoolingDown, ProviderUnavailable, RequestBudgetExhausted


def _series(country, source="World Bank", indicator="Inflation, consumer prices (annual %)"):
    return NormalizedData(
        metadata=Metadata(source=source, country=country, indicator=indicator,
                          frequency="annual", unit="percent", seriesId="FP.CPI.TOTL.ZG" if source == "World Bank" else "PCPIPCH"),
        data=[{"date": "2024-01-01", "value": 4.0}],
    )


def _state(locked=False):
    return ConversationState(
        indicator="inflation rate", provider="WORLDBANK", provider_locked=locked,
        countries=["PE", "CO", "BR"], start_date="2016-01-01", end_date="2025-12-31",
        time_scope_source="user", language="en", original_query="inflation Peru Colombia Brazil",
        active_answer_members=[AnswerSetMember(provider="WORLDBANK", indicator_label="inflation rate",
                                              provider_code="FP.CPI.TOTL.ZG", series_id="FP.CPI.TOTL.ZG", country=c)
                               for c in ["PE", "CO", "BR"]],
    )


@pytest.fixture
def service(monkeypatch):
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    svc._fallback_provider_cache.clear()
    monkeypatch.setattr(svc, "_select_indicator_query_for_resolution", lambda intent: "inflation rate")
    monkeypatch.setattr(svc, "_verify_execution_result", AsyncMock(return_value=None))
    saved = []
    monkeypatch.setattr(query_module.conversation_manager, "add_message_safe", lambda cid, *_a, **_kw: cid)
    monkeypatch.setattr(query_module.conversation_manager, "set_conversation_state", lambda cid, state: saved.append(state.model_copy(deep=True)))
    return svc, saved


def _collective(svc, state=None):
    return asyncio.run(svc._execute_collective_answer_member_delta(
        query="also Mexico", conversation_id="collective-outage-test", tracker=None,
        state=state or _state(), delta=FollowUpDelta(added_countries=["MX"], delta_type="add_country", raw_query="also Mexico"),
    ))


def test_inferred_source_does_not_lock_new_country_but_explicit_source_does(service):
    svc, _ = service
    for locked in [False, True]:
        st = _state(locked)
        intent = svc._build_collective_member_intent(
            "also Mexico", "MX inflation rate", st, st.active_answer_members[0],
            FollowUpDelta(added_countries=["MX"], delta_type="add_country"), "inflation rate",
        )
        assert bool(intent.parameters.get("__semantic_provider_locked")) is locked


def test_complete_collection_uses_judged_fallback_and_real_provenance(service, monkeypatch):
    svc, saved = service
    attempted = []

    async def fetch(intent):
        country = intent.parameters["country"]
        attempted.append((intent.apiProvider, country))
        if intent.apiProvider == "WORLDBANK" and country == "MX":
            raise ProviderUnavailable("upstream timeout")
        source = {"IMF": "IMF", "FRED": "FRED"}.get(intent.apiProvider, "World Bank")
        return [_series(country, source)]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    judge = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "_judge_fallback_match", judge)
    result = _collective(svc)
    assert not result.error
    assert {x.metadata.country for x in result.data} == {"PE", "CO", "BR", "MX"}
    assert result.intent.apiProvider == "MULTI"
    mexico = next(x for x in result.data if x.metadata.country == "MX")
    assert mexico.metadata.source == "IMF"
    assert any("World Bank" in note and "IMF" in note for note in mexico.metadata.notes)
    assert ("IMF", "MX") in attempted
    assert len([x for x in attempted if x == ("WORLDBANK", "MX")]) == 1
    assert saved[-1].provider is None
    assert {m.country for m in saved[-1].active_answer_members} == {"PE", "CO", "BR", "MX"}
    assert next(m for m in saved[-1].active_answer_members if m.country == "MX").provider == "IMF"
    assert saved[-1].time_scope_source == "user"
    assert saved[-1].start_date == "2016-01-01"
    assert judge.await_count >= 1


@pytest.mark.parametrize("failure,code", [(ProviderUnavailable("down"), "provider_unavailable"),
                                         (ProviderCoolingDown("cooldown", retry_after_seconds=30), "provider_unavailable"),
                                         (RequestBudgetExhausted("deadline"), "request_timeout")])
def test_unresolved_member_outage_keeps_prior_state_and_failure_type(service, monkeypatch, failure, code):
    svc, saved = service
    state = _state()
    before = state.model_dump()

    async def fetch(intent):
        if intent.parameters["country"] == "MX":
            raise failure
        return [_series(intent.parameters["country"])]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    result = _collective(svc, state)
    assert result.error == code
    assert "preserve the full collective" not in result.message
    assert not result.data
    assert not saved
    assert state.model_dump() == before


@pytest.mark.parametrize("judgment", [False, None, RuntimeError("judge offline")])
def test_unjudged_or_rejected_substitute_does_not_commit_collection(service, monkeypatch, judgment):
    svc, saved = service

    async def fetch(intent):
        if intent.apiProvider == "WORLDBANK" and intent.parameters["country"] == "MX":
            raise ProviderUnavailable("down")
        return [_series(intent.parameters["country"], "IMF" if intent.apiProvider == "IMF" else "FRED")]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    monkeypatch.setattr(svc, "_judge_fallback_match", AsyncMock(side_effect=judgment) if isinstance(judgment, Exception) else AsyncMock(return_value=judgment))
    result = _collective(svc)
    assert result.error == "provider_unavailable"
    assert not saved


def test_explicit_source_never_substituted(service, monkeypatch):
    svc, _ = service
    fetch = AsyncMock()
    monkeypatch.setattr(svc, "_fetch_data", fetch)
    intent = ParsedIntent(apiProvider="WORLDBANK", indicators=["inflation rate"],
                          parameters={"country": "MX"}, clarificationNeeded=False,
                          originalQuery="Mexico inflation from World Bank")
    with pytest.raises(ProviderUnavailable):
        asyncio.run(svc._try_with_fallback(intent, ProviderUnavailable("down")))
    fetch.assert_not_awaited()


def test_fallbacks_union_normalized_exclusions_and_strip_foreign_codes(service, monkeypatch):
    svc, _ = service
    seen = []

    async def fetch(intent):
        seen.append(intent)
        raise DataNotAvailableError("none")

    monkeypatch.setattr(svc, "_get_fallback_providers", lambda *_a, **_kw: ["World Bank", "IMF", "FRED", "FRED"])
    monkeypatch.setattr(svc, "_fetch_data", fetch)
    intent = ParsedIntent(apiProvider="WORLDBANK", indicators=["FP.CPI.TOTL.ZG"], clarificationNeeded=False,
                          originalQuery="Mexico inflation", parameters={"country": "MX", "indicator": "FP.CPI.TOTL.ZG",
                          "__fallback_excluded_providers": ["imf"], "__exact_provider_code_match": True})
    with pytest.raises(ProviderUnavailable):
        asyncio.run(svc._try_with_fallback(intent, ProviderUnavailable("down")))
    assert [x.apiProvider for x in seen] == ["FRED"]
    assert set(seen[0].parameters["__fallback_excluded_providers"]) == {"IMF", "WORLDBANK"}
    assert "indicator" not in seen[0].parameters
    assert "__exact_provider_code_match" not in seen[0].parameters
    assert seen[0].indicators == ["inflation rate"]


@pytest.mark.parametrize("parent_limited", [False, True])
def test_worldbank_total_budget_cancels_retry_sequence(monkeypatch, parent_limited):
    provider = WorldBankProvider()
    cancelled = []
    monkeypatch.setattr(wb_module, "effective_timeout", lambda _: 0.12)

    async def stalled(*_a, **_kw):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(provider, "_get_with_retry", stalled)

    async def run():
        if parent_limited:
            with request_budget(0.08, answer_reserve_seconds=0.02, retry_reserve_seconds=0.03):
                return await provider.fetch_indicator("FP.CPI.TOTL.ZG", country="MX")
        return await provider.fetch_indicator("FP.CPI.TOTL.ZG", country="MX")

    started = time.monotonic()
    with pytest.raises(RequestBudgetExhausted if parent_limited else ProviderUnavailable):
        asyncio.run(run())
    assert time.monotonic() - started < 1.0
    assert cancelled == [True]


def test_worldbank_external_cancellation_propagates(monkeypatch):
    provider = WorldBankProvider()
    entered = asyncio.Event()

    async def stalled(*_a, **_kw):
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(provider, "_get_with_retry", stalled)

    async def run():
        task = asyncio.create_task(provider.fetch_indicator("FP.CPI.TOTL.ZG", country="MX"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_worldbank_extended_timeout_cannot_raise_total_ceiling(monkeypatch):
    provider = WorldBankProvider()
    budgets = []
    real_timeout = asyncio.timeout

    def timed(seconds):
        budgets.append(seconds)
        return real_timeout(seconds)

    monkeypatch.setattr(wb_module.asyncio, "timeout", timed)
    monkeypatch.setattr(provider, "_fetch_indicator", AsyncMock(return_value=[]))

    async def run():
        with extended_timeout(120):
            return await provider.fetch_indicator("FP.CPI.TOTL.ZG", country="MX")

    asyncio.run(run())
    assert budgets == [30.0]


def test_single_remaining_country_uses_served_code_and_no_implicit_lock(service, monkeypatch):
    svc, saved = service

    async def fetch(intent):
        if intent.apiProvider == "WORLDBANK":
            raise ProviderUnavailable("down")
        return [_series(intent.parameters["country"], intent.apiProvider)]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    monkeypatch.setattr(svc, "_judge_fallback_match", AsyncMock(return_value=True))
    response = asyncio.run(svc._execute_collective_answer_member_delta(
        query="only Colombia", conversation_id="collective-outage-test", tracker=None,
        state=_state(), delta=FollowUpDelta(changed_country="CO", delta_type="country_change", raw_query="only Colombia"),
    ))
    assert not response.error
    assert response.intent.apiProvider == "IMF"
    assert response.intent.parameters["indicator"] == "PCPIPCH"
    assert saved[-1].provider == "IMF"
    assert not saved[-1].provider_locked
    assert saved[-1].active_answer_members[0].provider_code == "PCPIPCH"
    next_intent = svc._build_collective_member_intent(
        "also Mexico", "MX inflation rate", saved[-1], saved[-1].active_answer_members[0],
        FollowUpDelta(added_countries=["MX"], delta_type="add_country"), "inflation rate",
    )
    assert not next_intent.parameters.get("__semantic_provider_locked")


def test_carried_default_window_is_preserved_without_mutating_cached_series(service, monkeypatch):
    svc, saved = service
    state = _state()
    state.time_scope_source = "default"
    state.start_date = "2016-11-19"
    originals = {}

    async def fetch(intent):
        country = intent.parameters["country"]
        series = _series(country)
        series.data = [_series(country).data[0].model_copy(update={"date": f"{year}-01-01"}) for year in [1960, 2016, 2025, 2031]]
        originals[country] = series
        # The fetcher mutates its input; a fallback must still see the original scope.
        intent.parameters.pop("startDate", None)
        intent.parameters.pop("endDate", None)
        return [series]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    response = _collective(svc, state)
    assert not response.error
    assert all([p.date for p in s.data] == ["2016-01-01", "2025-01-01"] for s in response.data)
    assert all(len(s.data) == 4 for s in originals.values())
    assert saved[-1].time_scope_source == "default"
    assert response.intent.parameters["__time_scope_authority"] == "default"


def test_outside_window_member_never_commits_collective_success(service, monkeypatch):
    svc, saved = service

    async def fetch(intent):
        country = intent.parameters["country"]
        series = _series(country)
        if country == "MX":
            series.data[0].date = "2031-01-01"
        return [series]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    result = _collective(svc)
    assert result.error == "no_data_in_requested_window"
    assert not result.data
    assert not saved


def test_imf_weo_discloses_estimates_and_projections(monkeypatch):
    from backend.providers.imf import IMFProvider
    from backend.services.indicator_database import get_indicator_lookup

    provider = IMFProvider()
    monkeypatch.setattr(provider, "_resolve_indicator_code", AsyncMock(return_value=("TEST", "Consumer price inflation")))
    monkeypatch.setattr(get_indicator_lookup(), "get", lambda *_a: {"category": "WEO"})

    class Response:
        def json(self):
            return {"values": {"TEST": {"MEX": {"2025": 4.0, "2026": 3.5}}}}

    monkeypatch.setattr(provider, "_get_with_retry", AsyncMock(return_value=Response()))
    data = asyncio.run(provider.fetch_batch_indicator("TEST", countries=["MEX"]))
    assert any("estimates and projections" in note for note in data[0].metadata.notes)


@pytest.mark.parametrize("authority", ["default", "user"])
def test_verified_state_captures_executed_window_after_defaults(service, authority):
    svc, saved = service
    state = ConversationState(indicator="inflation", provider="WORLDBANK")
    intent = ParsedIntent(apiProvider="WORLDBANK", indicators=["inflation"], clarificationNeeded=False,
                          parameters={"startDate": "2016-11-19", "endDate": "2026-11-17",
                                      "__time_scope_authority": authority})
    svc._persist_verified_conversation_state("collective-outage-test", state, [_series("MX")], intent=intent)
    assert saved[-1].start_date == "2016-11-19"
    assert saved[-1].end_date == "2026-11-17"
    assert saved[-1].time_scope_source == authority


def test_absent_intent_or_time_does_not_erase_prior_user_window(service):
    svc, saved = service
    for intent in [None, ParsedIntent(apiProvider="WORLDBANK", indicators=["inflation"], clarificationNeeded=False)]:
        state = _state()
        svc._persist_verified_conversation_state("collective-outage-test", state, [_series("MX")], intent=intent)
        assert saved[-1].start_date == "2016-01-01"
        assert saved[-1].time_scope_source == "user"


def test_verified_exact_unwindowed_execution_clears_stale_default_dates(service):
    svc, saved = service
    state = _state()
    state.time_scope_source = "default"
    intent = ParsedIntent(apiProvider="WORLDBANK", indicators=["FP.CPI.TOTL.ZG"], clarificationNeeded=False,
                          parameters={"__exact_provider_code_match": True, "__time_scope_authority": "default"})
    svc._persist_verified_conversation_state("collective-outage-test", state, [_series("MX")], intent=intent)
    assert saved[-1].start_date is None
    assert saved[-1].end_date is None
    assert saved[-1].time_scope_source is None


def test_verified_year_bounds_are_preserved_mechanically(service):
    svc, saved = service
    intent = ParsedIntent(apiProvider="WORLDBANK", indicators=["inflation"], clarificationNeeded=False,
                          parameters={"start_year": 2020, "end_year": 2024, "__time_scope_authority": "user"})
    svc._persist_verified_conversation_state("collective-outage-test", _state(), [_series("MX")], intent=intent)
    assert saved[-1].start_date == "2020-01-01"
    assert saved[-1].end_date == "2024-12-31"
