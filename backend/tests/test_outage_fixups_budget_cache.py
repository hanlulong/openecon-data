"""Outage fix-ups: the final provider stage, the post-admission deadline
re-check, and never caching an incomplete Comtrade fan-out.

Contract pinned here:

- ``final_provider_stage()`` (backend/utils/request_budget.py) is a TASK-LOCAL
  marker: inside it ``retry_fits`` / ``retry_attempt_timeout`` leave only the
  ANSWER reserve (no cross-provider fallback can follow), outside it they
  leave the RETRY reserve exactly as before; with no budget active nothing
  changes; the marker never leaks to sibling tasks or back to the caller.
- QueryService marks a fetch as the final stage only when no further provider
  attempt can follow it: the last scheduled cross-provider fallback, or the
  intent's own fetch when fallback is refused (provider-locked / explicitly
  named provider).
- BaseProvider._request_with_retry re-checks the deadline AFTER the admission
  wait (zero I/O, nothing recorded in the breaker) and, for a retry, after
  the retry admission (the last genuine failure stands, one breaker outcome).
- A Comtrade fan-out with an unanswered sub-request (no-verdict, or skipped
  after the first outage) is served as a partial answer but marked
  ``fetch_incomplete`` and never written to either cache tier; a sub-request
  that answered "no data" (plain verdict) is an answer, not incompleteness.
"""
from __future__ import annotations

import asyncio
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from tenacity import wait_none

from backend.models import Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.providers import base as base_module
from backend.providers import comtrade as comtrade_module
from backend.providers.base import BaseProvider, _get_breaker
from backend.providers.comtrade import ComtradeProvider
from backend.services import data_fetcher as DF
from backend.services import query as query_module
from backend.services import rate_limiter as rates
from backend.services.cache import cache_service
from backend.services.query import QueryService
from backend.services.rate_limiter import ProviderRateLimiter, RateLimiterConfig
from backend.utils import request_budget as rb
from backend.utils.request_budget import (
    MIN_RETRY_ATTEMPT_SECONDS,
    budget_allows,
    current_budget,
    final_provider_stage,
    in_final_provider_stage,
    io_remaining,
    request_budget,
    retry_attempt_timeout,
    retry_fits,
)
from backend.utils.retry import (
    DataNotAvailableError,
    ProviderUnavailable,
    RequestBudgetExhausted,
)

_URL = "https://example.invalid/stage"
_PROVIDER = "STAGE_TEST"
_MAX = BaseProvider.MAX_RETRIES


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_limiter_and_breakers(monkeypatch):
    monkeypatch.setattr(rates, "_global_rate_limiter", rates.GlobalRateLimiter())
    monkeypatch.setattr(base_module, "_provider_breakers", {})
    monkeypatch.setattr(base_module, "wait_exponential", lambda **kwargs: wait_none())
    limiters = rates.get_global_rate_limiter()._limiters
    for key in (_PROVIDER, "COMTRADE"):
        limiters[key] = ProviderRateLimiter(
            RateLimiterConfig(key, min_delay_seconds=0, max_requests_per_minute=10_000)
        )


@pytest.fixture
def clock(monkeypatch):
    """A frozen monotonic clock seen ONLY by request_budget (asyncio, the rate
    limiter and httpx keep the real one); tests advance ``clock.now``."""
    state = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(rb, "time", SimpleNamespace(monotonic=lambda: state.now))
    return state


@pytest.fixture
def service():
    cache_service.clear()
    return QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")


def _series(code="X1", country="US", source="FRED", points=(("2024-01-01", 1.0),)):
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=code, country=country,
            frequency="annual", unit="x", seriesId=code,
        ),
        data=[{"date": d, "value": v} for d, v in points],
    )


# ---------------------------------------------------------------------------
# ITEM 2 -- final_provider_stage primitives
# ---------------------------------------------------------------------------


def test_final_stage_checks_retries_against_the_answer_reserve(clock):
    with request_budget(30, answer_reserve_seconds=2, retry_reserve_seconds=20):
        clock.now += 1.0  # 29s left
        assert in_final_provider_stage() is False
        assert retry_fits(10) is False                                  # 29 - 10 - 20 < 2
        assert retry_attempt_timeout(30.0) == pytest.approx(9.0, abs=0.01)   # 29 - 20
        with final_provider_stage():
            assert in_final_provider_stage() is True
            assert retry_fits(10) is True                               # 29 - 10 - 2 >= 2
            assert retry_attempt_timeout(30.0) == pytest.approx(27.0, abs=0.01)  # 29 - 2
            assert retry_attempt_timeout(5.0) == 5.0                    # never above requested
        # Back to the retry reserve once the stage exits.
        assert in_final_provider_stage() is False
        assert retry_fits(10) is False
        assert retry_attempt_timeout(30.0) == pytest.approx(9.0, abs=0.01)


def test_final_stage_keeps_the_floor_and_never_raises_a_timeout(clock):
    with request_budget(30, answer_reserve_seconds=2, retry_reserve_seconds=20):
        clock.now += 8.5  # 21.5s left: the retry-reserve cap (1.5s) is below the floor
        assert retry_fits(0) is False
        assert retry_attempt_timeout(30.0) == MIN_RETRY_ATTEMPT_SECONDS
        assert retry_attempt_timeout(1.0) == 1.0
        with final_provider_stage():
            assert retry_fits(0) is True
            assert retry_attempt_timeout(30.0) == pytest.approx(19.5, abs=0.01)
            clock.now += 17.5  # 4s left: 4 - 2 == MIN exactly -> still fits
            assert retry_fits(0) is True
            clock.now += 1.0   # 3s left: the answer-reserve cap (1s) is below the floor
            assert retry_fits(0) is False
            assert retry_attempt_timeout(30.0) == MIN_RETRY_ATTEMPT_SECONDS
            assert retry_attempt_timeout(1.0) == 1.0   # never raised to the floor


def test_final_stage_never_touches_the_answer_reserve_refusal(clock):
    with request_budget(10, answer_reserve_seconds=3, retry_reserve_seconds=6):
        clock.now += 8.0  # 2s left, inside the 3s answer reserve
        outside = (io_remaining(), budget_allows(0.0))
        with final_provider_stage():
            inside = (io_remaining(), budget_allows(0.0))
    assert outside == inside
    assert inside[1] is False


@pytest.mark.parametrize("final", [False, True], ids=["outside", "inside"])
def test_no_budget_is_unlimited_inside_or_outside_the_stage(final):
    with final_provider_stage() if final else nullcontext():
        assert current_budget() is None
        assert in_final_provider_stage() is final
        assert retry_fits(10_000) is True
        assert retry_attempt_timeout(30.0) == 30.0
        assert retry_attempt_timeout(0.5) == 0.5
    assert in_final_provider_stage() is False


async def test_final_stage_is_task_local_under_one_shared_budget(clock):
    seen = {}
    with request_budget(30, answer_reserve_seconds=2, retry_reserve_seconds=20) as budget:
        clock.now += 1.0  # 29s left for both legs
        entered = []
        both_inside = asyncio.Event()

        async def _leg(name, final):
            with final_provider_stage() if final else nullcontext():
                entered.append(name)
                if len(entered) == 2:
                    both_inside.set()
                # Read only once BOTH legs are inside their own context, so a
                # leak from the final leg would be visible to the plain one.
                await both_inside.wait()
                seen[name] = {
                    "budget": current_budget() is budget,
                    "final": in_final_provider_stage(),
                    "fits": retry_fits(10),
                    "timeout": retry_attempt_timeout(30.0),
                }

        await asyncio.gather(_leg("final", True), _leg("plain", False))
        assert in_final_provider_stage() is False
    assert seen["final"] == {"budget": True, "final": True, "fits": True, "timeout": 27.0}
    assert seen["plain"] == {"budget": True, "final": False, "fits": False, "timeout": 9.0}


async def test_stage_resets_in_the_caller_and_is_inherited_by_spawned_work():
    release = asyncio.Event()

    async def _child():
        await release.wait()
        return in_final_provider_stage()

    async def _probe():
        return in_final_provider_stage()

    with final_provider_stage():
        spawned = asyncio.create_task(_child())
        gathered = await asyncio.gather(_probe(), _probe())
        in_thread = await asyncio.to_thread(in_final_provider_stage)
        with final_provider_stage():
            pass
        assert in_final_provider_stage() is True   # nested exit restores the outer marker
    assert in_final_provider_stage() is False
    release.set()
    # The task copied the context at creation: it stays final after the
    # parent's `with` exits.
    assert await spawned is True
    assert gathered == [True, True]
    assert in_thread is True

    with pytest.raises(RuntimeError):
        with final_provider_stage():
            raise RuntimeError("boom")
    assert in_final_provider_stage() is False


# ---------------------------------------------------------------------------
# ITEMS 2 + 3 -- BaseProvider retry core
# ---------------------------------------------------------------------------


class _Provider(BaseProvider):
    @property
    def provider_name(self):
        return _PROVIDER

    async def _fetch_data(self, **params):
        return []


class _Upstream:
    """Fake HTTP upstream over httpx.MockTransport; records each attempt's
    read timeout (request.extensions["timeout"], as the outage harness does)
    and optionally advances the budget clock per attempt."""

    def __init__(self, kind, *, clock=None, advance=0.0):
        self.kind = kind
        self.clock = clock
        self.advance = advance
        self.timeouts: list = []

    async def _handler(self, request: httpx.Request):
        self.timeouts.append(request.extensions["timeout"]["read"])
        if self.clock is not None:
            self.clock.now += self.advance
        if self.kind == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if self.kind == "502":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"ok": True})

    def run(self, *, budget=None, final=False, timeout=30.0):
        async def _go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self._handler)) as client:
                scope = (
                    request_budget(budget[0], answer_reserve_seconds=budget[1], retry_reserve_seconds=budget[2])
                    if budget else nullcontext()
                )
                with scope:
                    with final_provider_stage() if final else nullcontext():
                        return await _Provider()._get_with_retry(client, _URL, timeout=timeout)

        return asyncio.run(_go())


def _spy_breaker_outcomes(monkeypatch):
    outcomes: list = []
    original = base_module._record_breaker_outcome

    def _spy(breaker, failure):
        outcomes.append(failure)
        return original(breaker, failure)

    monkeypatch.setattr(base_module, "_record_breaker_outcome", _spy)
    return outcomes


# 11s left, answer reserve 1s, retry reserve 10s: 11 - 10 < MIN (no retry
# against the retry reserve), 11 - 1 >= MIN (retries fit the answer reserve).
_BETWEEN_RESERVES = (11, 1, 10)


@pytest.mark.parametrize("kind", ["timeout", "502"])
def test_retry_reserve_suppresses_the_retry_outside_the_final_stage(clock, kind):
    upstream = _Upstream(kind)
    with pytest.raises(ProviderUnavailable) as info:
        upstream.run(budget=_BETWEEN_RESERVES, final=False)
    assert upstream.timeouts == [30.0]
    assert not isinstance(info.value, RequestBudgetExhausted)
    assert "after 1 HTTP attempts; retries limited by the request deadline" in str(info.value)
    assert _get_breaker(_PROVIDER).fail_counter == 1


@pytest.mark.parametrize("kind", ["timeout", "502"])
def test_final_stage_retries_against_the_answer_reserve(clock, kind):
    upstream = _Upstream(kind)
    with pytest.raises(ProviderUnavailable) as info:
        upstream.run(budget=_BETWEEN_RESERVES, final=True)
    total, answer, _retry = _BETWEEN_RESERVES
    first, *retries = upstream.timeouts
    assert len(upstream.timeouts) == _MAX > 1
    assert first == 30.0                                   # a first attempt is never capped
    assert all(MIN_RETRY_ATTEMPT_SECONDS <= t <= total - answer for t in retries)
    assert retries == [pytest.approx(total - answer)] * (_MAX - 1)
    assert not isinstance(info.value, RequestBudgetExhausted)
    assert f"after {_MAX} HTTP attempts" in str(info.value)
    # The whole retry operation is still ONE breaker outcome.
    assert _get_breaker(_PROVIDER).fail_counter == 1


def test_final_stage_retries_still_stop_before_the_answer_reserve(clock):
    # Each attempt takes 5s: attempt 1 at 11s left (30s, uncapped), the retry
    # at 6s left is capped to 6 - 1 = 5s, then 1s left: 1 - 1 < MIN -> stop.
    upstream = _Upstream("timeout", clock=clock, advance=5.0)
    with pytest.raises(ProviderUnavailable) as info:
        upstream.run(budget=_BETWEEN_RESERVES, final=True)
    assert upstream.timeouts == [30.0, 5.0]
    assert "after 2 HTTP attempts; retries limited by the request deadline" in str(info.value)


@pytest.mark.parametrize("final", [False, True], ids=["outside", "inside"])
def test_admission_wait_that_exhausts_the_budget_refuses_with_zero_io(clock, monkeypatch, final):
    outcomes = _spy_breaker_outcomes(monkeypatch)
    admitted = []

    async def _slow_admission(provider_key):
        admitted.append(provider_key)
        clock.now += 9.0  # 10s budget, 2s answer reserve: 1s left -> no I/O time

    monkeypatch.setattr(base_module, "admit_provider_request", _slow_admission)
    upstream = _Upstream("ok")
    with pytest.raises(RequestBudgetExhausted) as info:
        upstream.run(budget=(10, 2, 2), final=final, timeout=5.0)
    assert admitted == [_PROVIDER]
    assert "while waiting for admission" in str(info.value)
    assert upstream.timeouts == []            # zero I/O
    assert outcomes == []                     # nothing replayed into the breaker
    breaker = _get_breaker(_PROVIDER)
    assert breaker.fail_counter == 0
    assert breaker.current_state == "closed"


def test_admission_wait_that_leaves_io_time_still_sends(clock, monkeypatch):
    outcomes = _spy_breaker_outcomes(monkeypatch)

    async def _admission(provider_key):
        clock.now += 7.0  # 3s left, 1s past the 2s answer reserve

    monkeypatch.setattr(base_module, "admit_provider_request", _admission)
    upstream = _Upstream("ok")
    response = upstream.run(budget=(10, 2, 2), timeout=5.0)
    assert response.status_code == 200
    assert upstream.timeouts == [5.0]         # the first attempt's timeout is never capped
    assert outcomes == [None]


def _acquire_that_waits_on_the_first_retry(clock, seconds):
    calls: list = []

    async def _acquire(provider_key, max_wait_seconds=None):
        calls.append(provider_key)
        # Call 1 is the first attempt's admission (admit_provider_request);
        # call 2 is the first RETRY's admission.
        if len(calls) == 2:
            clock.now += seconds
        return 0.0

    return calls, _acquire


def test_retry_admission_wait_past_the_retry_reserve_stops_without_sending(clock, monkeypatch):
    outcomes = _spy_breaker_outcomes(monkeypatch)
    # 30s budget, retry reserve 3s: the stop check passes (27s of room), then
    # the retry admission waits 27s -> 3s left, nothing left past the reserve.
    calls, acquire = _acquire_that_waits_on_the_first_retry(clock, 27.0)
    monkeypatch.setattr(base_module, "acquire_provider", acquire)
    upstream = _Upstream("timeout")
    with pytest.raises(ProviderUnavailable) as info:
        upstream.run(budget=(30, 1, 3), final=False)
    assert len(calls) == 2
    assert upstream.timeouts == [30.0]        # the retry was never sent
    assert not isinstance(info.value, RequestBudgetExhausted)
    message = str(info.value)
    assert "after 1 HTTP attempts; retries limited by the request deadline" in message
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)   # the genuine failure stands
    assert len(outcomes) == 1 and isinstance(outcomes[0], httpx.ReadTimeout)
    assert _get_breaker(_PROVIDER).fail_counter == 1


def test_same_retry_admission_wait_still_fits_in_the_final_stage(clock, monkeypatch):
    # Same wait (3s left), but only the 1s answer reserve must be left: the
    # retries run, each capped to 3 - 1 = 2s (the floor).
    calls, acquire = _acquire_that_waits_on_the_first_retry(clock, 27.0)
    monkeypatch.setattr(base_module, "acquire_provider", acquire)
    upstream = _Upstream("timeout")
    with pytest.raises(ProviderUnavailable) as info:
        upstream.run(budget=(30, 1, 3), final=True)
    assert upstream.timeouts == [30.0] + [MIN_RETRY_ATTEMPT_SECONDS] * (_MAX - 1)
    assert f"after {_MAX} HTTP attempts" in str(info.value)
    assert _get_breaker(_PROVIDER).fail_counter == 1


@pytest.mark.parametrize("final", [False, True], ids=["outside", "inside"])
def test_no_budget_retry_core_is_unchanged(monkeypatch, final):
    admitted: list = []

    async def _noop_admission(provider_key):
        admitted.append(("admit", provider_key))

    async def _noop_acquire(provider_key, max_wait_seconds=None):
        admitted.append(("retry", provider_key))
        return 0.0

    monkeypatch.setattr(base_module, "admit_provider_request", _noop_admission)
    monkeypatch.setattr(base_module, "acquire_provider", _noop_acquire)
    upstream = _Upstream("timeout")
    with pytest.raises(ProviderUnavailable) as info:
        upstream.run(final=final, timeout=7.0)
    assert upstream.timeouts == [7.0] * _MAX
    assert admitted == [("admit", _PROVIDER)] + [("retry", _PROVIDER)] * (_MAX - 1)
    assert "; retries exhausted" in str(info.value)
    assert not isinstance(info.value, RequestBudgetExhausted)


# ---------------------------------------------------------------------------
# ITEM 2 -- QueryService wiring: which fetch is the final stage
# ---------------------------------------------------------------------------


def _fallback_intent():
    return ParsedIntent(
        apiProvider="COMTRADE", indicators=["vegetable oil exports"],
        parameters={"reporter": "US", "flow": "EXPORTS", "commodity": "15", "indicator": "15"},
        clarificationNeeded=False, originalQuery="US exports of vegetable oil 2024",
    )


@pytest.mark.parametrize(
    "providers,expected",
    [
        (["WORLDBANK"], {"WORLDBANK": True}),
        (["WORLDBANK", "IMF"], {"WORLDBANK": True, "IMF": True}),
        (["WORLDBANK", "IMF", "EUROSTAT"], {"WORLDBANK": False, "IMF": False, "EUROSTAT": True}),
        (
            ["WORLDBANK", "IMF", "EUROSTAT", "BIS"],
            {"WORLDBANK": False, "IMF": False, "EUROSTAT": False, "BIS": True},
        ),
    ],
    ids=["single", "top_two_only", "top_two_plus_one", "top_two_plus_two"],
)
@pytest.mark.parametrize("failure", ["empty", "raises"])
def test_only_the_last_scheduled_fallback_fetch_is_the_final_stage(service, providers, expected, failure):
    stages: dict = {}

    async def _fetch(fb_intent, *args, **kwargs):
        stages[fb_intent.apiProvider] = in_final_provider_stage()
        if failure == "raises":
            raise DataNotAvailableError(f"{fb_intent.apiProvider}: no data")
        return []

    primary = ProviderUnavailable("x")
    after: dict = {}

    async def _go():
        try:
            return await service._try_with_fallback(_fallback_intent(), primary)
        finally:
            after["stage"] = in_final_provider_stage()

    with patch.object(service, "_get_fallback_providers", return_value=list(providers)), \
         patch.object(service, "_select_indicator_query_for_resolution", return_value="vegetable oil exports"), \
         patch.object(service, "_fetch_data", new=AsyncMock(side_effect=_fetch)), \
         patch.object(service, "_fetch_multi_indicator_data", new=AsyncMock(side_effect=AssertionError("multi"))):
        with pytest.raises(ProviderUnavailable) as info:
            asyncio.run(_go())
    assert info.value is primary
    assert stages == expected
    assert after["stage"] is False


def test_fallback_refused_when_the_user_named_the_primary_provider(service):
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["unemployment rate"], parameters={"country": "US"},
        clarificationNeeded=False, originalQuery="US unemployment rate from FRED",
    )
    primary = ProviderUnavailable("x")
    fetch = AsyncMock(return_value=[_series("UNRATE")])
    with patch.object(service, "_fetch_data", new=fetch), \
         patch.object(service, "_get_fallback_providers", return_value=["WORLDBANK"]):
        with pytest.raises(ProviderUnavailable) as info:
            asyncio.run(service._try_with_fallback(intent, primary))
    assert info.value is primary
    fetch.assert_not_awaited()


_OWN_STAGE_CASES = [
    ("FRED", "US unemployment rate", {"__semantic_provider_locked": True}, True),
    ("FRED", "US unemployment rate from FRED", {}, True),
    ("WORLDBANK", "US unemployment rate from FRED", {}, False),
    ("FRED", "US unemployment rate", {}, False),
]
_OWN_STAGE_IDS = ["provider_locked", "explicit_same_provider", "explicit_other_provider", "fallback_possible"]


def _own_intent(api_provider, query, params, indicators=("unemployment rate",)):
    return ParsedIntent(
        apiProvider=api_provider, indicators=list(indicators),
        parameters={"indicator": "UNRATE", "country": "US", **params},
        clarificationNeeded=False, originalQuery=query,
    )


@pytest.mark.parametrize("api_provider,query,params,expect_final", _OWN_STAGE_CASES, ids=_OWN_STAGE_IDS)
def test_fetch_data_runs_a_fetch_no_fallback_can_follow_as_the_final_stage(
    service, api_provider, query, params, expect_final,
):
    seen: list = []

    async def _df(_svc, intent, execution_plan=None):
        seen.append(in_final_provider_stage())
        return [_series("UNRATE")]

    intent = _own_intent(api_provider, query, params)

    async def _go():
        result = await service._fetch_data(intent)
        return result, in_final_provider_stage()

    with patch("backend.services.query._df_fetch_data", new=AsyncMock(side_effect=_df)) as df:
        result, after = asyncio.run(_go())
    assert service._cross_provider_fallback_refused(intent) is expect_final
    assert seen == [expect_final]
    assert df.await_count == 1                # the re-entry never fetches twice
    assert result and after is False


@pytest.mark.parametrize("api_provider,query,params,expect_final", _OWN_STAGE_CASES, ids=_OWN_STAGE_IDS)
def test_multi_indicator_fetch_no_fallback_can_follow_is_the_final_stage(
    service, api_provider, query, params, expect_final,
):
    seen: list = []

    async def _df_multi(_svc, intent):
        seen.append(in_final_provider_stage())
        return [_series("UNRATE"), _series("CPI")]

    intent = _own_intent(api_provider, query, params, indicators=("unemployment rate", "inflation"))

    async def _go():
        result = await service._fetch_multi_indicator_data(intent)
        return result, in_final_provider_stage()

    with patch("backend.services.query._df_fetch_multi_indicator_data", new=AsyncMock(side_effect=_df_multi)):
        result, after = asyncio.run(_go())
    assert seen == [expect_final]
    assert len(result) == 2 and after is False


def test_alternate_code_retry_of_a_locked_fetch_stays_in_the_final_stage(service):
    calls: list = []

    async def _df(_svc, intent, execution_plan=None):
        calls.append((in_final_provider_stage(), (intent.parameters or {}).get("indicator")))
        if len(calls) == 1:
            raise DataNotAvailableError("FRED returned no observations")
        return [_series("UNRATE2")]

    intent = _own_intent(
        "FRED", "US unemployment rate",
        {"__semantic_provider_locked": True, "__decision_source": "llm_pick"},
    )
    with patch("backend.services.query._df_fetch_data", new=AsyncMock(side_effect=_df)):
        result = asyncio.run(service._fetch_data(intent))
    assert result
    assert [stage for stage, _code in calls] == [True, True]
    assert calls[0][1] == "UNRATE" and calls[1][1] is None   # re-adjudicated, code excluded
    assert intent.parameters["__exclude_indicator_codes"] == ["UNRATE"]


# ---------------------------------------------------------------------------
# ITEM 7 -- NormalizedData.fetch_incomplete
# ---------------------------------------------------------------------------


def test_fetch_incomplete_marker_is_copied_but_never_serialized():
    series = _series()
    assert series.fetch_incomplete is False
    series.mark_fetch_incomplete()
    assert series.fetch_incomplete is True
    assert _series().fetch_incomplete is False                     # per instance
    assert series.model_copy().fetch_incomplete is True
    assert series.model_copy(deep=True).fetch_incomplete is True
    assert series.model_copy(update={"data": []}).fetch_incomplete is True
    assert copy.copy(series).fetch_incomplete is True
    assert copy.deepcopy(series).fetch_incomplete is True

    dumped = series.model_dump()
    assert "_fetch_incomplete" not in dumped and "fetch_incomplete" not in dumped
    as_json = series.model_dump_json()
    assert "fetch_incomplete" not in as_json
    assert "fetch_incomplete" not in QueryResponse(
        conversationId="c", clarificationNeeded=False, data=[series],
    ).model_dump_json()
    # A serialization round trip (e.g. Redis) never resurrects the marker.
    assert NormalizedData.model_validate(dumped).fetch_incomplete is False
    assert NormalizedData.model_validate_json(as_json).fetch_incomplete is False


# ---------------------------------------------------------------------------
# ITEM 7 -- Comtrade fan-out marking (real fetch_trade_data over MockTransport)
# ---------------------------------------------------------------------------


_REPORTER_DESC = {"842": "USA", "276": "Germany", "251": "France"}
_PARTNER_DESC = {"0": "World", "276": "Germany", "251": "France"}


def _comtrade_row(params, period=None):
    return {
        "period": period or params["period"].split(",")[0], "flowDesc": "Export", "cmdCode": "15",
        "cmdDesc": "Animal, vegetable or microbial fats and oils", "primaryValue": 1.5e9,
        "reporterDesc": _REPORTER_DESC.get(params["reporterCode"], params["reporterCode"]),
        "partnerDesc": _PARTNER_DESC.get(params["partnerCode"], params["partnerCode"]),
    }


@pytest.fixture
def comtrade_upstream(monkeypatch):
    """Fake UN Comtrade: ``behaviour(params) -> 'timeout' | Response | rows``.
    Failures are instant (no real sleeping)."""
    log: list = []
    state = {"behaviour": lambda params: [_comtrade_row(params)]}

    async def handler(request: httpx.Request):
        params = dict(request.url.params)
        log.append(params)
        outcome = state["behaviour"](params)
        if outcome == "timeout":
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


_NO_VERDICTS = {
    "503": lambda: httpx.Response(503, text="Service Unavailable"),
    "timeout": lambda: "timeout",
    "quota_403": lambda: httpx.Response(
        403, headers={"Retry-After": "30"}, text=json.dumps({"message": "Out of call volume quota"}),
    ),
}


@pytest.mark.parametrize("failure", list(_NO_VERDICTS))
def test_comtrade_partial_reporter_fan_out_is_marked_incomplete(comtrade_upstream, failure):
    # US answers, DE gives no verdict, FR is skipped after the stop.
    comtrade_upstream.state["behaviour"] = lambda params: (
        [_comtrade_row(params)] if params["reporterCode"] == "842" else _NO_VERDICTS[failure]()
    )
    result = _comtrade_fetch(reporter=None, reporters=["US", "DE", "FR"], start_year=2024, end_year=2024)
    asked = [p["reporterCode"] for p in comtrade_upstream.log]
    assert "276" in asked and "251" not in asked
    assert result and all(series.data for series in result)        # served as a partial answer
    assert all(series.fetch_incomplete for series in result)


def test_comtrade_partial_period_chunks_are_marked_incomplete(comtrade_upstream):
    # 2000-2024 is three 12-period chunks: the first answers, the second
    # 503s (no verdict), the third is skipped after the stop.
    comtrade_upstream.state["behaviour"] = lambda params: (
        [_comtrade_row(params, period="2005")]
        if "2000" in params["period"].split(",")
        else httpx.Response(503, text="Service Unavailable")
    )
    result = _comtrade_fetch(start_year=2000, end_year=2024)
    chunks = {p["period"] for p in comtrade_upstream.log}
    assert len(chunks) == 2
    assert result and all(series.data for series in result)
    assert all(series.fetch_incomplete for series in result)


def test_comtrade_partial_partner_fan_out_is_marked_incomplete(comtrade_upstream):
    comtrade_upstream.state["behaviour"] = lambda params: (
        [_comtrade_row(params)] if params["partnerCode"] == "276" else "timeout"
    )
    result = _comtrade_fetch(partner=["DE", "FR"], start_year=2024, end_year=2024)
    assert {p["partnerCode"] for p in comtrade_upstream.log} == {"276", "251"}
    assert result and all(series.fetch_incomplete for series in result)


def test_comtrade_complete_fan_out_is_not_marked(comtrade_upstream):
    result = _comtrade_fetch(reporter=None, reporters=["US", "DE"], start_year=2024, end_year=2024)
    assert {p["reporterCode"] for p in comtrade_upstream.log} == {"842", "276"}
    assert len(result) == 2
    assert not any(series.fetch_incomplete for series in result)


@pytest.mark.parametrize(
    "verdict",
    [lambda: httpx.Response(404, text="Not Found"), lambda: []],
    ids=["http_404", "empty_rows"],
)
def test_comtrade_verdict_sub_request_is_an_answer_not_incompleteness(comtrade_upstream, verdict):
    comtrade_upstream.state["behaviour"] = lambda params: (
        verdict() if params["reporterCode"] == "276" else [_comtrade_row(params)]
    )
    result = _comtrade_fetch(reporter=None, reporters=["US", "DE", "FR"], start_year=2024, end_year=2024)
    # A verdict never stops the fan-out, and the merged result is complete.
    assert {p["reporterCode"] for p in comtrade_upstream.log} == {"842", "276", "251"}
    assert len(result) == 2
    assert not any(series.fetch_incomplete for series in result)


class _NoSleepAsyncio:
    """comtrade's ``asyncio`` with an instant sleep (fetch_trade_balance's
    0.2s pacing between the two flow fetches)."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(_seconds, result=None):
        return result


@pytest.mark.parametrize(
    "exports_incomplete,imports_incomplete,expected",
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["exports_partial", "imports_partial", "both_complete"],
)
def test_trade_balance_inherits_the_incomplete_marker(
    monkeypatch, exports_incomplete, imports_incomplete, expected,
):
    provider = ComtradeProvider(api_key="demo")

    async def _fetch_trade_data(**kwargs):
        exports = kwargs["flow"] == "EXPORTS"
        series = NormalizedData(
            metadata=Metadata(
                source="UN Comtrade", indicator="Exports" if exports else "Imports",
                country="United States", frequency="annual", unit="US Dollars", seriesId="TOTAL",
            ),
            data=[{"date": "2024-01-01", "value": 10.0 if exports else 4.0}],
        )
        if (exports_incomplete if exports else imports_incomplete):
            series.mark_fetch_incomplete()
        return [series]

    monkeypatch.setattr(provider, "fetch_trade_data", _fetch_trade_data)
    monkeypatch.setattr(comtrade_module, "asyncio", _NoSleepAsyncio())
    balance = asyncio.run(provider.fetch_trade_balance(reporter="US", start_year=2024, end_year=2024))
    assert balance.data[0].value == 6.0
    assert balance.fetch_incomplete is expected


# ---------------------------------------------------------------------------
# ITEM 7 -- QueryService._save_to_cache never stores an incomplete fetch
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_spies(monkeypatch):
    redis = SimpleNamespace(set=AsyncMock(return_value=True), ttl_for_provider=lambda provider: 3600)
    monkeypatch.setattr(query_module, "get_redis_cache", AsyncMock(return_value=redis))
    memory = MagicMock()
    monkeypatch.setattr(cache_service, "cache_data", memory)
    return SimpleNamespace(redis=redis, memory=memory)


_CACHE_PARAMS = {"indicator": "15", "reporter": "US", "flow": "EXPORTS"}


def _marked(series):
    series.mark_fetch_incomplete()
    return series


@pytest.mark.parametrize(
    "build",
    [
        lambda: _marked(_series(source="UN Comtrade")),
        lambda: [_marked(_series("A", source="UN Comtrade")), _marked(_series("B", source="UN Comtrade"))],
        lambda: [_series("A", source="UN Comtrade"), _marked(_series("B", source="UN Comtrade"))],
    ],
    ids=["single_series", "all_marked", "one_of_two_marked"],
)
def test_save_to_cache_never_stores_an_incomplete_fetch(service, cache_spies, build):
    asyncio.run(service._save_to_cache("COMTRADE", dict(_CACHE_PARAMS), build()))
    cache_spies.redis.set.assert_not_awaited()
    cache_spies.memory.assert_not_called()


@pytest.mark.parametrize(
    "build",
    [lambda: _series(source="UN Comtrade"), lambda: [_series("A", source="UN Comtrade")]],
    ids=["single_series", "list"],
)
def test_save_to_cache_still_stores_a_complete_fetch(service, cache_spies, build):
    asyncio.run(service._save_to_cache("COMTRADE", dict(_CACHE_PARAMS), build()))
    cache_spies.redis.set.assert_awaited_once()
    cache_spies.memory.assert_called_once()


# ---------------------------------------------------------------------------
# ITEM 7 -- end to end: data_fetcher.fetch_data -> real Comtrade -> real save
# ---------------------------------------------------------------------------


def _df_svc(service):
    """The data_fetcher seams around the REAL Comtrade provider and the REAL
    QueryService._save_to_cache (indicator resolution is identity)."""
    return SimpleNamespace(
        _MAX_ALTERNATE_CODE_RETRIES=2,
        _preflight_geographic_split=AsyncMock(return_value=None),
        _maybe_expand_ranking_country_scope=lambda query, provider, params: params,
        _resolve_indicator_for_fetch=AsyncMock(side_effect=lambda provider, request, params: params),
        _get_from_cache=AsyncMock(return_value=None),
        _save_to_cache=service._save_to_cache,
        _normalize_bis_metadata_labels=lambda result: None,
        _looks_like_provider_indicator_code=lambda provider, value: True,
        _normalize_country_to_iso2=QueryService._normalize_country_to_iso2,
        comtrade_provider=ComtradeProvider(api_key="demo"),
    )


def _comtrade_window_intent():
    return ParsedIntent(
        apiProvider="COMTRADE", indicators=["vegetable oil exports"],
        parameters={
            "reporter": "US", "flow": "EXPORTS", "commodity": "15", "indicator": "15",
            "startDate": "2000-01-01", "endDate": "2024-12-31", "__time_scope_authority": "user",
        },
        clarificationNeeded=False, originalQuery="US exports of vegetable oil 2000-2024",
    )


@pytest.mark.parametrize("partial", [True, False], ids=["partial_not_cached", "complete_cached"])
def test_comtrade_fetch_through_data_fetcher_caches_only_a_complete_answer(
    service, comtrade_upstream, cache_spies, partial,
):
    def _behaviour(params):
        first_year = params["period"].split(",")[0]
        if partial and first_year != "2000":
            return httpx.Response(503, text="Service Unavailable")
        return [_comtrade_row(params, period=str(int(first_year) + 1))]

    comtrade_upstream.state["behaviour"] = _behaviour
    result = asyncio.run(DF.fetch_data(_df_svc(service), _comtrade_window_intent()))
    assert result and all(series.data for series in result)
    assert all(series.fetch_incomplete is partial for series in result)
    if partial:
        cache_spies.redis.set.assert_not_awaited()
        cache_spies.memory.assert_not_called()
    else:
        cache_spies.redis.set.assert_awaited_once()
        cache_spies.memory.assert_called_once()
