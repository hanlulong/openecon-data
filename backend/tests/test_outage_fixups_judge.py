"""Outage fix-ups, query layer: who may ACCEPT a cross-provider fallback, and
what an outage turn says.

Production 2026-11-08: during a Comtrade outage "China exports to US 2024"
was answered with World Bank TX.VAL.TECH.MF.ZS (high-tech exports as % of
manufactured exports), accepted by the keyword/cue gate alone and served
with no disclosure.

Contract pinned here:
- acceptance: the structural country checks and the keyword gate may REJECT;
  only the model judge (semantic_match_judge.judge_fallback_result) may
  ACCEPT, and only a "match" at confidence >= MATCH_CONFIDENCE_THRESHOLD.
  "mismatch" / "uncertain" / low confidence reject (a no-verdict primary then
  ends in the honest outage answer); an UNAVAILABLE judge (None: no LLM,
  failure, timeout, too little request time) keeps the keyword decision;
- the judge goes through the parse path's reasoning-model handling
  (structured_completion_kwargs), is bounded by a fixed cap clamped by the
  request budget, and skips an open local-LLM circuit;
- a served fallback carries a localized substitution note, without mutating
  the (possibly cached) series;
- the standard pipeline classifies only the FETCH's own exception as the
  provider outcome; the delta path serves a stale cached copy before the
  outage answer; the outage wording uses display names, sane waits, a
  localized generic name, every failed leg's provider, and no "timeouts or
  server errors" for a quota refusal.
"""
from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.services import data_fetcher as data_fetcher_module
from backend.services import query as query_module
from backend.services import semantic_match_judge as smj
from backend.services.cache import cache_service
from backend.services.openrouter import structured_completion_kwargs
from backend.services.query import PostFetchProcessingError, QueryService
from backend.services.query_pipeline import ParseRouteResult, ValidationResult
from backend.services.semantic_match_judge import SemanticMatchJudgment
from backend.utils import request_budget as rb
from backend.utils.request_budget import in_final_provider_stage, request_budget
from backend.utils.retry import (
    DataNotAvailableError,
    ProviderCoolingDown,
    ProviderUnavailable,
    RequestBudgetExhausted,
    aggregate_failure,
    is_no_verdict,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _series(source="World Bank", sid="TX.VAL.TECH.MF.ZS",
            indicator="High-technology exports (% of manufactured exports)",
            country="China", notes=None):
    return NormalizedData(
        metadata=Metadata(source=source, indicator=indicator, country=country,
                          frequency="annual", unit="%", seriesId=sid, description=indicator,
                          notes=notes),
        data=[{"date": "2023-01-01", "value": 23.1}],
    )


def _trade_intent(language=None, **params):
    base = {"reporter": "China", "partner": "United States", "flow": "EXPORTS",
            "startDate": "2024-01-01", "endDate": "2024-12-31"}
    base.update(params)
    return ParsedIntent(
        apiProvider="COMTRADE", indicators=["exports"], parameters=base,
        clarificationNeeded=False, originalQuery="China exports to US 2024", language=language,
    )


def _fred_intent(language=None, original="US unemployment rate", **params):
    base = {"country": "US", "indicator": "UNRATE"}
    base.update(params)
    return ParsedIntent(
        apiProvider="FRED", indicators=["unemployment rate"], parameters=base,
        clarificationNeeded=False, originalQuery=original, language=language,
    )


def _judgment(decision, confidence, reason="r"):
    return SemanticMatchJudgment(decision=decision, confidence=confidence, reason=reason)


@pytest.fixture
def service():
    cache_service.clear()
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    svc.settings.use_outcome_decision_stage = False
    svc.settings.use_post_fetch_semantic_judge = False
    svc.settings.use_staged_state_commit = False
    return svc


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock seen ONLY by request_budget (asyncio keeps real time)."""
    state = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(rb, "time", SimpleNamespace(monotonic=lambda: state.now))
    return state


def _fallback_patches(service, fetch, *, providers=("WORLDBANK",), relevant=True):
    return [
        patch.object(service, "_fetch_data", new=fetch),
        patch.object(service, "_get_fallback_providers", return_value=list(providers)),
        patch.object(service, "_select_indicator_query_for_resolution", return_value="exports"),
        patch.object(service, "_is_fallback_relevant", return_value=relevant),
    ]


def _run_fallback(service, intent, primary, fetch, judge, **kwargs):
    patches = _fallback_patches(service, fetch, **kwargs)
    patches.append(patch.object(query_module, "_smj_judge_fallback_result", new=judge))
    for p in patches:
        p.start()
    try:
        return asyncio.run(service._try_with_fallback(intent, primary))
    finally:
        for p in reversed(patches):
            p.stop()


# ---------------------------------------------------------------------------
# Item 1: the judge is the acceptance authority
# ---------------------------------------------------------------------------


def test_judge_match_serves_the_fallback_with_a_substitution_note(service):
    original = _series(source="World Bank", sid="SL.UEM.TOTL.ZS",
                       indicator="Unemployment, total (% of total labor force)", country="United States")
    judge = AsyncMock(return_value=_judgment("match", 0.95))
    intent = _fred_intent()
    result = _run_fallback(service, intent, ProviderUnavailable("down"),
                           AsyncMock(return_value=[original]), judge)
    assert result and result[0].metadata.seriesId == "SL.UEM.TOTL.ZS"
    judge.assert_awaited_once()
    kwargs = judge.await_args.kwargs
    assert kwargs["original_query"] == "US unemployment rate"
    assert "exports" in kwargs["original_indicators"] and "unemployment rate" in kwargs["original_indicators"]
    assert 0 < kwargs["timeout_s"] <= smj.FALLBACK_JUDGE_TIMEOUT_SECONDS
    # Disclosed, on a COPY: the fetched (cacheable) series is untouched.
    assert result[0].metadata.notes[0] == (
        "FRED could not serve this request, so this data comes from World Bank instead."
    )
    assert original.metadata.notes is None
    assert intent.apiProvider == "WORLDBANK"


@pytest.mark.parametrize("judgment", [
    _judgment("mismatch", 0.96),
    _judgment("uncertain", 0.9),
    _judgment("match", smj.MATCH_CONFIDENCE_THRESHOLD - 0.01),
], ids=["mismatch", "uncertain", "low_confidence_match"])
def test_judge_rejection_refuses_the_fallback(service, judgment):
    primary = ProviderUnavailable("comtrade down")
    intent = _trade_intent()
    with pytest.raises(ProviderUnavailable) as info:
        _run_fallback(service, intent, primary, AsyncMock(return_value=[_series()]),
                      AsyncMock(return_value=judgment))
    assert info.value is primary
    assert intent.apiProvider == "COMTRADE"  # nothing restamped


def test_threshold_match_is_accepted(service):
    result = _run_fallback(service, _trade_intent(), ProviderUnavailable("down"),
                           AsyncMock(return_value=[_series()]),
                           AsyncMock(return_value=_judgment("match", smj.MATCH_CONFIDENCE_THRESHOLD)))
    assert result


def test_unavailable_judge_keeps_the_keyword_acceptance(service):
    result = _run_fallback(service, _trade_intent(), ProviderUnavailable("down"),
                           AsyncMock(return_value=[_series()]), AsyncMock(return_value=None))
    assert result and result[0].metadata.seriesId == "TX.VAL.TECH.MF.ZS"


def test_keyword_rejection_never_reaches_the_judge(service):
    judge = AsyncMock(return_value=_judgment("match", 0.99))
    with pytest.raises(ProviderUnavailable):
        _run_fallback(service, _trade_intent(), ProviderUnavailable("down"),
                      AsyncMock(return_value=[_series()]), judge, relevant=False)
    judge.assert_not_awaited()


def test_structural_country_mismatch_rejects_before_the_judge(service):
    """The real gate's country check still rejects on its own."""
    judge = AsyncMock(return_value=_judgment("match", 0.99))
    fetch = AsyncMock(return_value=[_series(country="Japan")])
    intent = _fred_intent()
    with patch.object(service, "_fetch_data", new=fetch), \
         patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
         patch.object(service, "_select_indicator_query_for_resolution", return_value="unemployment rate"), \
         patch.object(query_module, "_smj_judge_fallback_result", new=judge):
        with pytest.raises(ProviderUnavailable):
            asyncio.run(service._try_with_fallback(intent, ProviderUnavailable("down")))
    judge.assert_not_awaited()


def test_parallel_fallbacks_prefer_the_first_judged_match(service):
    def _by_provider(intent, execution_plan=None):
        if intent.apiProvider == "WORLDBANK":
            return [_series(sid="WB.BAD")]
        return [_series(source="IMF", sid="IMF.GOOD")]

    async def _judge(qs, *, fallback_result, **kwargs):
        sid = fallback_result[0].metadata.seriesId
        return _judgment("match", 0.9) if sid == "IMF.GOOD" else _judgment("mismatch", 0.9)

    result = _run_fallback(service, _trade_intent(), ProviderUnavailable("down"),
                           AsyncMock(side_effect=_by_provider), AsyncMock(side_effect=_judge),
                           providers=("WORLDBANK", "IMF"))
    assert result[0].metadata.seriesId == "IMF.GOOD"


def test_judge_not_run_when_too_little_request_time_is_left(service, clock):
    """The fallback fetch used up the time: the judge is unavailable (keyword
    decision kept) instead of starting an LLM call into the answer reserve."""
    judge = AsyncMock(return_value=_judgment("mismatch", 0.99))

    async def _slow_fetch(intent, execution_plan=None):
        clock.now += 27.5  # 30s budget, 1s answer reserve: 1.5s of I/O time left
        return [_series()]

    async def _go():
        with request_budget(30, answer_reserve_seconds=1, retry_reserve_seconds=5):
            return await service._try_with_fallback(_trade_intent(), ProviderUnavailable("down"))

    patches = _fallback_patches(service, AsyncMock(side_effect=_slow_fetch))
    patches.append(patch.object(query_module, "_smj_judge_fallback_result", new=judge))
    for p in patches:
        p.start()
    try:
        result = asyncio.run(_go())
    finally:
        for p in reversed(patches):
            p.stop()
    judge.assert_not_awaited()
    assert result


def test_judge_timeout_is_clamped_by_the_request_budget(service, clock):
    judge = AsyncMock(return_value=_judgment("match", 0.9))

    async def _fetch(intent, execution_plan=None):
        clock.now += 15.0  # 30s budget, 10s answer reserve: 5s of I/O time left
        return [_series()]

    async def _go():
        with request_budget(30, answer_reserve_seconds=10, retry_reserve_seconds=10):
            return await service._try_with_fallback(_trade_intent(), ProviderUnavailable("down"))

    patches = _fallback_patches(service, AsyncMock(side_effect=_fetch))
    patches.append(patch.object(query_module, "_smj_judge_fallback_result", new=judge))
    for p in patches:
        p.start()
    try:
        assert asyncio.run(_go())
    finally:
        for p in reversed(patches):
            p.stop()
    assert judge.await_args.kwargs["timeout_s"] == pytest.approx(5.0, abs=0.01)


class _SlowInstructor:
    def __init__(self, delay):
        self.delay = delay
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return _judgment("mismatch", 0.99)


def test_a_slow_real_judge_is_cut_off_and_keeps_the_keyword_decision(service, monkeypatch):
    """The REAL judge over a slow model: bounded by the cap, then unavailable."""
    monkeypatch.setattr(smj, "FALLBACK_JUDGE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(smj, "MIN_JUDGE_SECONDS", 0.05)
    slow = _SlowInstructor(delay=10)
    service.openrouter = SimpleNamespace(
        instructor_client=slow, instructor_model="m", llm_provider=None,
        settings=SimpleNamespace(llm_provider="vllm"),
    )
    started = time.perf_counter()
    result = _run_fallback(service, _trade_intent(), ProviderUnavailable("down"),
                           AsyncMock(return_value=[_series()]), smj.judge_fallback_result)
    elapsed = time.perf_counter() - started
    assert slow.calls == 1
    assert elapsed < 3.0
    assert result  # unavailable judge -> keyword gate's acceptance stands


def test_no_verdict_primary_with_a_rejected_fallback_ends_in_the_outage_answer(service):
    """The production case end to end on the delta path: Comtrade times out,
    World Bank offers the high-tech share, the judge says mismatch."""
    intent = _trade_intent()

    async def _fetch(fetch_intent, execution_plan=None):
        if fetch_intent.apiProvider == "COMTRADE":
            raise ProviderUnavailable("Connection failed after 3 HTTP attempts")
        return [_series()]

    valid = ValidationResult(is_multi_indicator=False, is_valid=True, validation_error=None,
                             suggestions=None, is_confident=True, confidence_reason=None)
    parse = ParseRouteResult(intent=intent, explicit_provider=None, routed_provider="COMTRADE",
                             validation_warning=None)
    with patch.object(service.pipeline, "validate_intent", return_value=valid), \
         patch.object(service, "_fetch_data", new=AsyncMock(side_effect=_fetch)), \
         patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
         patch.object(service, "_select_indicator_query_for_resolution", return_value="exports"), \
         patch.object(service, "_is_fallback_relevant", return_value=True), \
         patch.object(service, "_get_stale_from_cache", new=AsyncMock(return_value=None)), \
         patch.object(query_module, "_smj_judge_fallback_result",
                      new=AsyncMock(return_value=_judgment("mismatch", 0.96))):
        response = asyncio.run(service._execute_resolved_intent(
            "China exports to US 2024", "conv-judge", intent, parse,
            skip_prefetch_clarification=True, skip_post_fetch_clarification=True,
        ))
    assert response.error == "provider_unavailable"
    assert not response.data
    assert "UN Comtrade" in response.message


# --- the judge module itself (fake LLM objects) -----------------------------


class _RecordingInstructor:
    def __init__(self, reply):
        self.reply = reply
        self.kwargs = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.kwargs = kwargs
        return self.reply


def _qs(instructor=None, llm_provider=None, llm="vllm"):
    return SimpleNamespace(openrouter=SimpleNamespace(
        instructor_client=instructor, instructor_model="gpt-oss-120b" if instructor else None,
        llm_provider=llm_provider, settings=SimpleNamespace(llm_provider=llm),
    ))


def _judge_call(qs, **kwargs):
    return asyncio.run(smj.judge_fallback_result(
        qs, original_indicators=["exports"], fallback_result=[_series()],
        original_query="China exports to US 2024", original_concept="exports", **kwargs,
    ))


@pytest.mark.parametrize("llm,max_tokens,extra_body", [
    ("vllm", 1220, {"reasoning_effort": "low"}),
    ("openrouter", 220, {}),
])
def test_judge_uses_the_parse_paths_reasoning_budget(llm, max_tokens, extra_body):
    instructor = _RecordingInstructor(_judgment("mismatch", 0.9))
    judgment = _judge_call(_qs(instructor, llm=llm))
    assert judgment.decision == "mismatch"
    assert instructor.kwargs["max_tokens"] == max_tokens
    assert instructor.kwargs["extra_body"] == extra_body
    assert instructor.kwargs["response_model"] is SemanticMatchJudgment


def test_parse_keeps_its_exact_completion_budget():
    """The extracted helper keeps the parse's numbers byte-identical."""
    assert structured_completion_kwargs(SimpleNamespace(llm_provider="vllm"), 500) == {
        "max_tokens": 1500, "extra_body": {"reasoning_effort": "low"},
    }
    assert structured_completion_kwargs(SimpleNamespace(llm_provider="openrouter"), 500) == {
        "max_tokens": 500, "extra_body": {},
    }


def test_manual_path_states_the_json_contract_and_parses():
    class _Provider:
        def __init__(self):
            self.kwargs = None

        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return {"choices": [{"message": {"content":
                '{"decision": "match", "confidence": 0.91, "reason": "same metric"}'}}]}

    provider = _Provider()
    judgment = _judge_call(_qs(instructor=None, llm_provider=provider))
    assert judgment.decision == "match" and judgment.confidence == 0.91
    assert '"decision"' in provider.kwargs["system_prompt"]  # schema stated (observed: {"result": "match"})
    assert provider.kwargs["max_tokens"] == 1220


def test_non_judgment_reply_falls_through_to_unavailable():
    """A mocked client returning something that is not a judgment (as an
    AsyncMock does) is never read as a decision."""
    judgment = _judge_call(_qs(_RecordingInstructor(MagicMock()), llm_provider=None))
    assert judgment is None


def test_open_local_llm_circuit_makes_the_judge_unavailable_without_a_call(monkeypatch):
    from backend.services import openrouter as openrouter_module

    monkeypatch.setattr(openrouter_module, "_local_circuit_open_until", time.monotonic() + 60)
    instructor = _RecordingInstructor(_judgment("match", 0.99))
    assert _judge_call(_qs(instructor)) is None
    assert instructor.kwargs is None


def test_judge_timeout_returns_none_quickly():
    slow = _SlowInstructor(delay=10)
    started = time.perf_counter()
    assert _judge_call(_qs(slow), timeout_s=0.2) is None
    assert time.perf_counter() - started < 2.0


def test_bounded_judge_timeout(clock):
    assert smj.bounded_judge_timeout() == smj.FALLBACK_JUDGE_TIMEOUT_SECONDS  # no budget
    with request_budget(100, answer_reserve_seconds=10, retry_reserve_seconds=10):
        assert smj.bounded_judge_timeout() == smj.FALLBACK_JUDGE_TIMEOUT_SECONDS
    with request_budget(14, answer_reserve_seconds=10, retry_reserve_seconds=10):
        assert smj.bounded_judge_timeout() == pytest.approx(4.0)
    with request_budget(11, answer_reserve_seconds=10, retry_reserve_seconds=10):
        assert smj.bounded_judge_timeout() is None  # 1s < MIN_JUDGE_SECONDS


def test_resolved_indicator_wrapper_uses_the_shared_threshold(service):
    with patch.object(query_module, "_smj_judge_resolved_indicator",
                      new=AsyncMock(return_value=_judgment("match", smj.MATCH_CONFIDENCE_THRESHOLD))):
        assert asyncio.run(service._judge_resolved_indicator_match("FRED", "q", "UNRATE")) is True
    with patch.object(query_module, "_smj_judge_resolved_indicator",
                      new=AsyncMock(return_value=_judgment("match", 0.74))):
        assert asyncio.run(service._judge_resolved_indicator_match("FRED", "q", "UNRATE")) is False


# ---------------------------------------------------------------------------
# Item 2 follow-up: a fallback attempt's stage is decided by its caller only
# ---------------------------------------------------------------------------


def test_named_provider_as_a_non_last_fallback_is_not_marked_final(service):
    """User named World Bank, the primary was FRED: World Bank is a fallback
    with more fallbacks after it, so its fetch must NOT be the final stage even
    though its own intent would be 'explicitly named' (the caller decided)."""
    seen = {}

    async def _df_fetch(svc, intent, execution_plan=None):
        seen[intent.apiProvider] = in_final_provider_stage()
        raise DataNotAvailableError("no data")

    intent = _fred_intent(original="US unemployment rate from World Bank")
    with patch.object(query_module, "_df_fetch_data", new=AsyncMock(side_effect=_df_fetch)), \
         patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK", "IMF", "EUROSTAT"]), \
         patch.object(service, "_select_indicator_query_for_resolution", return_value="unemployment rate"):
        with pytest.raises(ProviderUnavailable):
            asyncio.run(service._try_with_fallback(intent, ProviderUnavailable("fred down")))
    assert seen == {"WORLDBANK": False, "IMF": False, "EUROSTAT": True}


# ---------------------------------------------------------------------------
# Item 4: only the fetch's own exception is the provider outcome
# ---------------------------------------------------------------------------


def test_post_fetch_timeout_is_not_told_as_an_outage(service):
    data = [_series(source="FRED", sid="UNRATE", indicator="Unemployment Rate", country="United States")]
    recover = AsyncMock(return_value=None)
    sentinel = QueryResponse(conversationId="c", clarificationNeeded=True)
    with patch.object(service, "_fetch_data", new=AsyncMock(return_value=data)), \
         patch.object(service, "_maybe_improve_country_coverage",
                      new=AsyncMock(side_effect=asyncio.TimeoutError())), \
         patch.object(service, "_try_with_fallback", new=AsyncMock(side_effect=DataNotAvailableError("x"))), \
         patch.object(service, "_maybe_recover_from_empty_data", new=recover), \
         patch.object(service, "_get_stale_from_cache", new=AsyncMock(return_value=None)), \
         patch.object(service, "_build_no_data_indicator_clarification", return_value=sentinel) as menu:
        response = asyncio.run(service._execute_standard_pipeline("US unemployment rate", "conv-pf", _fred_intent()))
    assert response is sentinel
    recover.assert_awaited_once()  # recovery is no longer skipped
    attempt_error = menu.call_args.kwargs["attempt_error"]
    assert isinstance(attempt_error, PostFetchProcessingError)
    assert isinstance(attempt_error.__cause__, asyncio.TimeoutError)
    assert not is_no_verdict(attempt_error)
    assert not isinstance(attempt_error, DataNotAvailableError)  # attempted code stays selectable


def test_post_fetch_timeout_reaches_the_menu_not_the_outage_text(service):
    data = [_series(source="FRED", sid="UNRATE", indicator="Unemployment Rate", country="United States")]
    with patch.object(service, "_fetch_data", new=AsyncMock(return_value=data)), \
         patch.object(service, "_verify_execution_result", new=AsyncMock(side_effect=asyncio.TimeoutError())), \
         patch.object(service, "_try_with_fallback", new=AsyncMock(side_effect=DataNotAvailableError("x"))), \
         patch.object(service, "_maybe_recover_from_empty_data", new=AsyncMock(return_value=None)), \
         patch.object(service, "_get_stale_from_cache", new=AsyncMock(return_value=None)), \
         patch.object(service, "_build_provider_outage_response",
                      side_effect=AssertionError("outage text")) as outage:
        asyncio.run(service._execute_standard_pipeline("US unemployment rate", "conv-pf2", _fred_intent()))
    outage.assert_not_called()


def test_fetch_timeout_is_still_the_outage(service):
    recover = AsyncMock(return_value=None)
    with patch.object(service, "_fetch_data", new=AsyncMock(side_effect=asyncio.TimeoutError())), \
         patch.object(service, "_try_with_fallback", new=AsyncMock(side_effect=asyncio.TimeoutError())), \
         patch.object(service, "_maybe_recover_from_empty_data", new=recover), \
         patch.object(service, "_get_stale_from_cache", new=AsyncMock(return_value=None)):
        response = asyncio.run(service._execute_standard_pipeline("US unemployment rate", "conv-ft", _fred_intent()))
    assert response.error == "provider_unavailable"
    recover.assert_not_awaited()


# ---------------------------------------------------------------------------
# Item 5: the delta path serves a stale cached copy before the outage answer
# ---------------------------------------------------------------------------


_VALID = ValidationResult(is_multi_indicator=False, is_valid=True, validation_error=None,
                          suggestions=None, is_confident=True, confidence_reason=None)


def _resolved(service, intent, fetch_error, stale, language=None):
    parse = ParseRouteResult(intent=intent, explicit_provider=None, routed_provider=intent.apiProvider,
                             validation_warning=None)
    stale_mock = AsyncMock(return_value=stale)
    persist = MagicMock()
    with patch.object(service.pipeline, "validate_intent", return_value=_VALID), \
         patch.object(service, "_fetch_data", new=AsyncMock(side_effect=fetch_error)), \
         patch.object(service, "_try_with_fallback", new=AsyncMock(side_effect=fetch_error)), \
         patch.object(service, "_maybe_recover_from_empty_data", new=AsyncMock(return_value=None)), \
         patch.object(service, "_verify_execution_result", new=AsyncMock(return_value=None)), \
         patch.object(service, "_get_stale_from_cache", new=stale_mock), \
         patch.object(service, "_persist_verified_conversation_state", new=persist):
        response = asyncio.run(service._execute_resolved_intent(
            "and imports?", "conv-stale", intent, parse,
            skip_prefetch_clarification=True, skip_post_fetch_clarification=True,
        ))
    return response, stale_mock, persist


@pytest.mark.parametrize("language,needle", [(None, "Showing cached data"), ("zh", "缓存数据")])
def test_delta_path_outage_serves_stale_cache(service, language, needle):
    stale = [_series(source="UN Comtrade", sid="TOTAL", indicator="Imports", country="China")]
    response, stale_mock, persist = _resolved(
        service, _trade_intent(language=language), ProviderUnavailable("down"), stale,
    )
    assert response.data and response.data[0].metadata.seriesId == "TOTAL"
    assert response.error is None
    assert needle in response.message and "UN Comtrade" in response.message
    stale_mock.assert_awaited_once()
    persist.assert_called_once()


def test_delta_path_outage_without_stale_cache_answers_honestly(service):
    response, stale_mock, persist = _resolved(service, _trade_intent(), ProviderUnavailable("down"), None)
    assert response.error == "provider_unavailable"
    stale_mock.assert_awaited_once()
    persist.assert_not_called()


def test_delta_path_verdict_does_not_consult_stale_cache(service):
    sentinel = QueryResponse(conversationId="c", clarificationNeeded=True)
    with patch.object(service, "_build_no_data_indicator_clarification", return_value=sentinel):
        response, stale_mock, _ = _resolved(
            service, _trade_intent(), DataNotAvailableError("no records"), [_series()],
        )
    assert response is sentinel
    stale_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Item 6: outage wording
# ---------------------------------------------------------------------------


def test_outage_names_fred_by_its_display_name(service):
    response = service._build_provider_outage_response("c", _fred_intent(), ProviderUnavailable("down"))
    assert "FRED" in response.message and "Federal Reserve" not in response.message
    assert response.message.startswith("⚠️ **No response from FRED right now**")


@pytest.mark.parametrize("retry_after,expected", [
    (1, "about 1 second."),
    (29.2, "about 30 seconds."),
    (59.5, "about 1 minute."),
    (600, "about 10 minutes."),
    (253402300799.0, "about 15 minutes."),  # year-9999 saturation, capped at the limiter cap
    (float("inf"), "about 15 minutes."),
])
def test_rate_limited_wait_is_rendered_sanely(service, retry_after, expected):
    response = service._build_provider_outage_response(
        "c", _trade_intent(), ProviderCoolingDown("429", retry_after_seconds=retry_after),
    )
    assert response.message.endswith(expected), response.message
    assert "1 seconds" not in response.message


def test_rate_limited_wait_is_localized(service):
    response = service._build_provider_outage_response(
        "c", _trade_intent(language="zh"), ProviderCoolingDown("429", retry_after_seconds=120),
    )
    assert "约 2 分钟后重试" in response.message


@pytest.mark.parametrize("retry_after", [None, float("nan"), 0, -5])
def test_quota_refusal_without_a_wait_is_not_told_as_server_errors(service, retry_after):
    response = service._build_provider_outage_response(
        "c", _trade_intent(), ProviderCoolingDown("quota 403", retry_after_seconds=retry_after),
    )
    assert "timeouts or server errors" not in response.message
    assert "rate-limited" in response.message and "request quota" in response.message
    assert response.error == "provider_unavailable"


def test_unknown_provider_name_is_localized(service):
    assert service._provider_display_names([None, ""], "zh") == "数据源"
    assert service._provider_display_names([], None) == "the data provider"
    intent = _trade_intent(language="zh")
    intent.apiProvider = ""
    response = service._build_provider_outage_response("c", intent, ProviderUnavailable("down"))
    assert "数据源" in response.message and "data provider" not in response.message
    response = service._build_provider_outage_response("c", _fred_intent().model_copy(update={"apiProvider": ""}),
                                                        RequestBudgetExhausted("deadline"))
    assert "from the data provider" in response.message


def test_aggregate_failure_carries_the_failed_providers_through_context():
    legs = [ProviderUnavailable("a"), ProviderCoolingDown("b", retry_after_seconds=5)]
    aggregated = aggregate_failure(legs, DataNotAvailableError("v"), context="ctx",
                                   sources=["FRED", "WORLDBANK"])
    assert isinstance(aggregated, ProviderCoolingDown)
    assert aggregated.providers == ("FRED", "WORLDBANK")
    assert aggregated.with_context("outer").providers == ("FRED", "WORLDBANK")
    assert ProviderUnavailable("x").providers == ()
    # Mixed outcomes keep the plain verdict.
    verdict = DataNotAvailableError("v")
    assert aggregate_failure([ProviderUnavailable("a"), []], verdict, context="c", sources=["A", "B"]) is verdict


def test_multi_indicator_all_outage_names_every_failed_provider(service):
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["unemployment rate", "GDP"],
        parameters={"country": "US", "startDate": "2020-01-01", "endDate": "2024-12-31"},
        clarificationNeeded=False, originalQuery="US unemployment rate and GDP",
    )
    with patch.object(data_fetcher_module, "fetch_data", new=AsyncMock(side_effect=ProviderUnavailable("down"))), \
         patch.object(service, "_select_routed_provider", new=AsyncMock(side_effect=["FRED", "WORLDBANK"])):
        with pytest.raises(ProviderUnavailable) as info:
            asyncio.run(data_fetcher_module.fetch_multi_indicator_data(service, intent))
    assert set(info.value.providers) == {"FRED", "WORLDBANK"}
    response = service._build_provider_outage_response("c", intent, info.value)
    assert "FRED and World Bank" in response.message or "World Bank and FRED" in response.message


def test_retry_wait_never_rounds_down():
    from backend.services.user_messages import format_retry_wait

    for seconds in (0.1, 1, 59, 59.5, 61, 3599, 3601):
        text = format_retry_wait(seconds)
        count = int(text.split()[1])
        unit = 1 if "second" in text else 60 if "minute" in text else 3600
        assert count * unit >= math.ceil(seconds), text
        assert ("1 seconds" not in text) and ("1 minutes" not in text or count != 1)
