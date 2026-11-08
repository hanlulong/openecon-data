"""A provider that never answered gets an HONEST answer, inside the deadline.

Production 2026-11-08: Comtrade's keyed API timed out; 'China exports to US
2024' hit the 120s 504 backstop, and the follow-up 'and imports?' answered
"I couldn't find data with the current indicator selection. Please choose the
indicator you intended" -- a verdict nobody gave.

Contract pinned here:
- every no-data-menu call site shares QueryService._build_no_data_indicator_
  clarification; on a NO-VERDICT attempt_error (typed: ProviderUnavailable /
  ProviderCoolingDown / RequestBudgetExhausted / raw transport or timeout) it
  answers "<provider> is not responding" (localized), stores no menu,
  persists no verified state and writes no cache;
- the delta follow-up path (_execute_resolved_intent) keeps the fetch error so
  it reaches that delegate; semantic recovery never re-probes after it;
- a provider_change follow-up never claims "<provider> doesn't have X" during
  an outage; cross-provider fallback still runs when time allows (correctness
  beats preference) and is skipped when the deadline cannot fit it;
- end to end (/api/query, /api/query/stream) a Comtrade outage answers 200
  with the honest message well before the deadline, with no M,X envelope,
  no window probe and no alternate-code selector round.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from tenacity import wait_none

import backend.main as m
from backend.models import Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.providers import base as base_module
from backend.providers import comtrade as comtrade_module
from backend.services import query as query_module
from backend.services import rate_limiter as rates
from backend.services.cache import cache_service
from backend.services.conversation import conversation_manager
from backend.services.query import QueryService
from backend.services.query_pipeline import ParseRouteResult, ValidationResult
from backend.services.rate_limiter import ProviderRateLimiter, RateLimiterConfig
from backend.services.user_messages import _MESSAGES, get_message
from backend.utils.request_budget import request_budget
from backend.utils.retry import (
    DataNotAvailableError,
    ProviderCoolingDown,
    ProviderUnavailable,
    RequestBudgetExhausted,
)


def _comtrade_intent(language=None, **params):
    base = {
        "reporter": "US", "flow": "EXPORTS", "commodity": "15", "indicator": "15",
        "startDate": "2024-01-01", "endDate": "2024-12-31",
        "__decision_source": "llm_pick", "__time_scope_authority": "user",
    }
    base.update(params)
    return ParsedIntent(
        apiProvider="COMTRADE", indicators=["vegetable oil exports"], parameters=base,
        clarificationNeeded=False, originalQuery="US exports of vegetable oil 2024",
        language=language,
    )


@pytest.fixture
def service():
    cache_service.clear()
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    svc.settings.use_outcome_decision_stage = False
    svc.settings.use_post_fetch_semantic_judge = False
    svc.settings.use_staged_state_commit = False
    return svc


# ---------------------------------------------------------------------------
# The localized catalog
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message_id", [
    "provider_unavailable", "provider_rate_limited", "provider_rate_limited_later",
    "request_deadline_exhausted", "stale_cache_served",
])
def test_outage_messages_exist_in_en_and_zh(message_id):
    assert {"en", "zh"} <= set(_MESSAGES[message_id])
    for language in ("en", "zh"):
        text = get_message(
            message_id, language, provider="UN Comtrade", Provider="UN Comtrade",
            wait="about 30 seconds",
        )
        assert "UN Comtrade" in text and "{" not in text
    # A missing placeholder never raises (the get_message contract).
    assert get_message(message_id, "zh")


# ---------------------------------------------------------------------------
# The shared delegate
# ---------------------------------------------------------------------------


def _menu_spies(svc):
    collect = patch.object(svc, "_collect_indicator_choice_options", side_effect=AssertionError("menu built"))
    store = patch("backend.services.indicator_clarification.store_pending_indicator_options",
                  side_effect=AssertionError("menu stored"))
    return collect, store


def test_outage_answer_replaces_the_no_data_menu(service):
    collect, store = _menu_spies(service)
    with collect, store:
        response = service._build_no_data_indicator_clarification(
            "conv-o", "US exports of vegetable oil", _comtrade_intent(),
            attempt_error=ProviderUnavailable("Connection failed after 3 HTTP attempts"),
        )
    assert response.error == "provider_unavailable"
    assert response.clarificationNeeded is False
    assert response.clarificationOptions is None and response.data is None
    assert "UN Comtrade" in response.message
    assert "couldn't find data" not in response.message


def test_outage_answer_is_localized(service):
    response = service._build_no_data_indicator_clarification(
        "conv-z", "美国植物油出口", _comtrade_intent(language="zh"),
        attempt_error=ProviderUnavailable("timed out"),
    )
    assert "暂时无法访问" in response.message and "UN Comtrade" in response.message
    assert "未找到" not in response.message


def test_rate_limited_and_deadline_variants(service):
    cooling = service._build_no_data_indicator_clarification(
        "c", "q", _comtrade_intent(), attempt_error=ProviderCoolingDown("429", retry_after_seconds=29.2),
    )
    assert cooling.error == "provider_unavailable" and "30" in cooling.message
    deadline = service._build_no_data_indicator_clarification(
        "c", "q", _comtrade_intent(), attempt_error=RequestBudgetExhausted("deadline"),
    )
    assert deadline.error == "request_timeout" and "ran out of time" in deadline.message
    raw = service._build_no_data_indicator_clarification(
        "c", "q", _comtrade_intent(), attempt_error=asyncio.TimeoutError(),
    )
    assert raw.error == "provider_unavailable"


@pytest.mark.parametrize("attempt_error", [None, DataNotAvailableError("API returned 404")])
def test_verdicts_still_get_the_menu_builder(service, attempt_error):
    sentinel = QueryResponse(conversationId="c", clarificationNeeded=True)
    with patch.object(query_module, "_ic_build_no_data_indicator_clarification", return_value=sentinel) as builder:
        response = service._build_no_data_indicator_clarification(
            "c", "q", _comtrade_intent(), attempt_error=attempt_error,
        )
    assert response is sentinel
    assert builder.call_args.kwargs["attempt_error"] is attempt_error


def test_provider_change_follow_up_never_claims_no_data_during_an_outage(service):
    intent = _comtrade_intent()
    intent.isFollowUp = True
    intent.followUpType = "provider_change"
    with patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK"]):
        assert service._build_provider_change_unavailable_response(
            "c", intent, None, attempt_error=ProviderUnavailable("down"),
        ) is None
        unchanged = service._build_provider_change_unavailable_response("c", intent, None)
    assert unchanged is not None and "doesn't appear to have" in unchanged.message


# ---------------------------------------------------------------------------
# The delta follow-up path and the standard pipeline
# ---------------------------------------------------------------------------


def _parse(intent):
    return ParseRouteResult(intent=intent, explicit_provider=None, routed_provider=intent.apiProvider,
                            validation_warning=None)


_VALID = ValidationResult(is_multi_indicator=False, is_valid=True, validation_error=None,
                          suggestions=None, is_confident=True, confidence_reason=None)


def test_resolved_intent_outage_answers_honestly_without_recovery(service):
    intent = _comtrade_intent()
    recover = AsyncMock(return_value=None)
    persist = MagicMock()
    with patch.object(service.pipeline, "validate_intent", return_value=_VALID), \
         patch.object(service, "_fetch_data", new=AsyncMock(side_effect=ProviderUnavailable("down"))), \
         patch.object(service, "_try_with_fallback", new=AsyncMock(side_effect=ProviderUnavailable("down"))), \
         patch.object(service, "_maybe_recover_from_empty_data", new=recover), \
         patch.object(service, "_persist_verified_conversation_state", new=persist):
        response = asyncio.run(service._execute_resolved_intent(
            "and imports?", "conv-delta", intent, _parse(intent),
            skip_prefetch_clarification=True, skip_post_fetch_clarification=True,
        ))
    assert response.error == "provider_unavailable"
    recover.assert_not_awaited()
    persist.assert_not_called()
    assert not conversation_manager.get_pending_indicator_options(response.conversationId)


def test_resolved_intent_verdict_keeps_recovery_and_passes_the_error(service):
    intent = _comtrade_intent()
    verdict = DataNotAvailableError("UN Comtrade returned no records")
    recover = AsyncMock(return_value=None)
    sentinel = QueryResponse(conversationId="c", clarificationNeeded=True)
    with patch.object(service.pipeline, "validate_intent", return_value=_VALID), \
         patch.object(service, "_fetch_data", new=AsyncMock(side_effect=verdict)), \
         patch.object(service, "_try_with_fallback", new=AsyncMock(side_effect=verdict)), \
         patch.object(service, "_maybe_recover_from_empty_data", new=recover), \
         patch.object(service, "_build_no_data_indicator_clarification", return_value=sentinel) as menu:
        response = asyncio.run(service._execute_resolved_intent(
            "and imports?", "conv-delta2", intent, _parse(intent),
            skip_prefetch_clarification=True, skip_post_fetch_clarification=True,
        ))
    assert response is sentinel
    recover.assert_awaited_once()
    assert menu.call_args.kwargs["attempt_error"] is verdict


def test_standard_pipeline_skips_semantic_recovery_after_an_outage(service):
    intent = _comtrade_intent()
    recover = AsyncMock(return_value=None)
    with patch.object(service, "_fetch_data", new=AsyncMock(side_effect=ProviderUnavailable("down"))), \
         patch.object(service, "_try_with_fallback", new=AsyncMock(side_effect=ProviderUnavailable("down"))), \
         patch.object(service, "_maybe_recover_from_empty_data", new=recover), \
         patch.object(service, "_get_stale_from_cache", new=AsyncMock(return_value=None)):
        response = asyncio.run(service._execute_standard_pipeline("q", "conv-std", intent))
    recover.assert_not_awaited()
    assert response.error == "provider_unavailable"


# ---------------------------------------------------------------------------
# Cross-provider fallback and the request deadline
# ---------------------------------------------------------------------------


def _wb_series():
    return NormalizedData(
        metadata=Metadata(source="World Bank", indicator="Exports", country="United States",
                          frequency="annual", unit="USD", seriesId="NE.EXP.GNFS.CD"),
        data=[{"date": "2024-01-01", "value": 3.1e12}],
    )


def test_fallback_is_skipped_when_the_deadline_cannot_fit_it(service):
    primary = ProviderUnavailable("down")
    fetch = AsyncMock(return_value=[_wb_series()])

    async def _go():
        with request_budget(5, answer_reserve_seconds=1, retry_reserve_seconds=1):
            return await service._try_with_fallback(_comtrade_intent(), primary)

    with patch.object(service, "_fetch_data", new=fetch), \
         patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK"]):
        with pytest.raises(ProviderUnavailable) as info:
            asyncio.run(_go())
    assert info.value is primary
    fetch.assert_not_awaited()


def test_fallback_without_a_budget_is_unchanged(service):
    fetch = AsyncMock(return_value=[_wb_series()])
    with patch.object(service, "_fetch_data", new=fetch), \
         patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
         patch.object(service, "_is_fallback_relevant", return_value=True):
        result = asyncio.run(service._try_with_fallback(_comtrade_intent(), ProviderUnavailable("down")))
    assert result and fetch.await_count == 1


def test_provider_locked_intent_still_skips_fallback(service):
    fetch = AsyncMock(return_value=[_wb_series()])
    intent = _comtrade_intent(__provider_locked=True)
    with patch.object(service, "_fetch_data", new=fetch), \
         patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
         patch.object(query_module, "_ir_is_provider_locked", return_value=True):
        with pytest.raises(ProviderUnavailable):
            asyncio.run(service._try_with_fallback(intent, ProviderUnavailable("down")))
    fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# End to end through backend.main.app (simulated Comtrade outage)
# ---------------------------------------------------------------------------


class _FakeSupabase:
    async def record_anonymous_query(self, *args, **kwargs):
        return 1


def _comtrade_row():
    return {
        "period": "2024", "flowDesc": "Export", "cmdCode": "15",
        "cmdDesc": "Animal, vegetable or microbial fats and oils", "primaryValue": 1.5e9,
        "reporterDesc": "USA", "partnerDesc": "World",
    }


@pytest.fixture
def outage(monkeypatch):
    """Comtrade's keyed API times out on every request; LLM seams are counted."""
    monkeypatch.setattr(m, "get_supabase_service", lambda: _FakeSupabase())
    monkeypatch.setattr(m, "schedule_query_log_to_supabase", lambda **kwargs: None)
    monkeypatch.setattr(m.settings, "anon_identity_mode", "shadow")
    monkeypatch.setattr(m.settings, "query_timeout_seconds", 12)
    monkeypatch.setattr(m.settings, "query_answer_reserve_seconds", 2.0)
    monkeypatch.setattr(m.settings, "provider_retry_reserve_seconds", 4.0)
    monkeypatch.setattr(m, "_SSE_STREAM_TIMEOUT_SECONDS", 12)

    monkeypatch.setattr(comtrade_module, "REQUEST_TIMEOUT", 3.0)
    monkeypatch.setattr(comtrade_module, "GLOBAL_REQUEST_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(base_module, "wait_exponential", lambda **kwargs: wait_none())
    monkeypatch.setattr(base_module, "_provider_breakers", {})
    monkeypatch.setattr(rates, "_global_rate_limiter", rates.GlobalRateLimiter())
    rates.get_global_rate_limiter()._limiters["COMTRADE"] = ProviderRateLimiter(
        RateLimiterConfig("COMTRADE", min_delay_seconds=0, max_requests_per_minute=10_000)
    )

    log: list[dict] = []
    state = {"healthy": False}

    async def handler(request: httpx.Request):
        log.append(dict(request.url.params))
        if state["healthy"]:
            return httpx.Response(200, json={"data": [_comtrade_row()]})
        await asyncio.sleep(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(
        comtrade_module, "get_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    svc = m.query_service
    monkeypatch.setattr(query_module, "_intent_cache", {})
    for flag in ("use_outcome_decision_stage", "use_post_fetch_semantic_judge", "use_staged_state_commit"):
        monkeypatch.setattr(svc.settings, flag, False)
    holder = {"intent": _comtrade_intent()}

    async def _parse_and_route(*args, **kwargs):
        return _parse(holder["intent"].model_copy(deep=True))

    monkeypatch.setattr(svc.pipeline, "parse_and_route", _parse_and_route)
    monkeypatch.setattr(svc.pipeline, "validate_intent", lambda intent: _VALID)
    post_parse = AsyncMock(return_value=None)
    monkeypatch.setattr(svc, "_build_post_parse_clarification", post_parse)
    monkeypatch.setattr(svc, "_get_from_cache", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_get_stale_from_cache", AsyncMock(return_value=None))
    fallbacks = {"providers": []}
    monkeypatch.setattr(svc, "_get_fallback_providers", lambda *a, **k: list(fallbacks["providers"]))

    original_save = svc._save_to_cache
    saved: list[str] = []

    async def _save(provider, params, data):
        saved.append(str(provider))
        return await original_save(provider, params, data)

    monkeypatch.setattr(svc, "_save_to_cache", _save)
    cache_writes: list[str] = []
    original_cache_data = cache_service.cache_data

    def _cache_data(provider, params, data, *args, **kwargs):
        cache_writes.append(str(provider))
        return original_cache_data(provider, params, data, *args, **kwargs)

    monkeypatch.setattr(cache_service, "cache_data", _cache_data)

    # Every selector / LLM seam that an alternate-code round or a recovery
    # would reach is counted.
    resolve_calls = []
    original_resolve = svc._resolve_indicator_for_fetch

    async def _resolve(*args, **kwargs):
        resolve_calls.append(args)
        return await original_resolve(*args, **kwargs)

    monkeypatch.setattr(svc, "_resolve_indicator_for_fetch", _resolve)
    recover = AsyncMock(return_value=None)
    monkeypatch.setattr(svc, "_maybe_recover_from_empty_data", recover)
    cache_service.clear()
    return SimpleNamespace(
        log=log, state=state, holder=holder, svc=svc, fallbacks=fallbacks, saved=saved,
        cache_writes=cache_writes, resolve_calls=resolve_calls, recover=recover, post_parse=post_parse,
    )


async def _post(path, body):
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client:
        started = time.monotonic()
        response = await client.post(path, json=body)
        return response, time.monotonic() - started


def _assert_outage_requests(log):
    assert 2 <= len(log) <= base_module.BaseProvider.MAX_RETRIES, log
    assert all(p["flowCode"] == "X" and p["period"] == "2024" for p in log), log
    assert len({p["cmdCode"] for p in log}) == 1


async def test_e2e_sync_outage_answers_honestly_before_the_deadline(outage):
    response, elapsed = await _post("/api/query", {"query": "US exports of vegetable oil 2024", "sessionId": "s-o1"})
    print(f"\n[e2e sync] elapsed={elapsed:.2f}s requests={outage.log}")
    assert response.status_code == 200, response.text
    assert elapsed < 10
    body = response.json()
    assert body["error"] == "provider_unavailable"
    assert "UN Comtrade" in body["message"]
    assert body["clarificationNeeded"] is False
    assert "couldn't find data" not in body["message"]
    _assert_outage_requests(outage.log)
    assert len(outage.resolve_calls) <= 1      # no alternate-code selector round
    assert "COMTRADE" not in {p.upper() for p in outage.saved}
    assert not any("COMTRADE" in p.upper() for p in outage.cache_writes)


async def test_e2e_zh_outage_is_localized(outage):
    outage.holder["intent"] = _comtrade_intent(language="zh")
    response, elapsed = await _post("/api/query", {"query": "美国2024年植物油出口", "sessionId": "s-o2"})
    body = response.json()
    print(f"\n[e2e zh] elapsed={elapsed:.2f}s message={body.get('message')!r}")
    assert response.status_code == 200
    assert elapsed < 10
    assert "暂时无法访问" in body["message"] and "UN Comtrade" in body["message"]


async def test_e2e_fallback_still_answers_when_time_allows(outage, monkeypatch):
    outage.fallbacks["providers"] = ["WORLDBANK"]
    monkeypatch.setattr(m.settings, "query_timeout_seconds", 30)
    original_fetch = outage.svc._fetch_data
    wb_calls = []

    async def _fetch(intent, execution_plan=None):
        if str(intent.apiProvider).upper() == "WORLDBANK":
            wb_calls.append(intent)
            return [_wb_series()]
        return await original_fetch(intent, execution_plan=execution_plan)

    monkeypatch.setattr(outage.svc, "_fetch_data", _fetch)
    monkeypatch.setattr(outage.svc, "_is_fallback_relevant", lambda *a, **k: True)
    response, elapsed = await _post("/api/query", {"query": "US exports 2024", "sessionId": "s-o3"})
    body = response.json()
    print(f"\n[e2e fallback] elapsed={elapsed:.2f}s error={body.get('error')!r}")
    assert response.status_code == 200
    assert wb_calls
    assert body.get("data") and body.get("error") in (None, "")
    assert body["intent"]["apiProvider"].upper() in {"WORLDBANK", "WORLD BANK"}
    assert elapsed < 20


async def test_e2e_explicit_provider_gets_the_honest_answer_not_a_fallback(outage, monkeypatch):
    outage.fallbacks["providers"] = ["WORLDBANK"]
    outage.holder["intent"] = _comtrade_intent()
    outage.holder["intent"].originalQuery = "US exports of vegetable oil 2024 from UN Comtrade"
    original_fetch = outage.svc._fetch_data
    wb_calls = []

    async def _fetch(intent, execution_plan=None):
        if str(intent.apiProvider).upper() == "WORLDBANK":
            wb_calls.append(intent)
            return [_wb_series()]
        return await original_fetch(intent, execution_plan=execution_plan)

    monkeypatch.setattr(outage.svc, "_fetch_data", _fetch)
    monkeypatch.setattr(outage.svc, "_detect_explicit_provider", lambda q: "COMTRADE")
    response, elapsed = await _post(
        "/api/query", {"query": "US exports of vegetable oil 2024 from UN Comtrade", "sessionId": "s-o4"},
    )
    body = response.json()
    assert response.status_code == 200 and elapsed < 10
    assert wb_calls == []
    assert body["error"] == "provider_unavailable"


async def test_e2e_stream_outage_emits_the_honest_error_event(outage):
    response, elapsed = await _post("/api/query/stream", {"query": "US exports of vegetable oil 2024", "sessionId": "s-o5"})
    text = response.text
    print(f"\n[e2e stream] elapsed={elapsed:.2f}s requests={len(outage.log)}")
    assert response.status_code == 200
    assert elapsed < 11
    error_events = [
        json.loads(block.split("data: ", 1)[1])
        for block in text.split("\n\n") if block.startswith("event: error")
    ]
    assert error_events and error_events[0]["error"] == "provider_unavailable"
    assert "UN Comtrade" in error_events[0]["message"]
    assert "event: done" in text
    assert "Query timed out" not in text
    _assert_outage_requests(outage.log)


async def test_e2e_outage_is_never_cached_as_empty(outage):
    first, _ = await _post("/api/query", {"query": "US exports of vegetable oil 2024", "sessionId": "s-o6"})
    assert first.json()["error"] == "provider_unavailable"
    outage.state["healthy"] = True
    second, _ = await _post("/api/query", {"query": "US exports of vegetable oil 2024", "sessionId": "s-o6b"})
    body = second.json()
    assert second.status_code == 200
    assert body.get("data"), body
    assert body.get("error") in (None, "")
