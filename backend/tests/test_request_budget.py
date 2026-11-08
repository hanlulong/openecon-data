"""Request-scoped deadline budget: deflation-only limits on provider retries.

The query endpoints cancel a request from outside at their deadline; before
this budget nothing below the endpoint knew that deadline, so a provider retry
could start with seconds left and the user got a 504 instead of an answer.

Contract pinned here (backend/utils/request_budget.py + the retry core in
backend/providers/base.py + retry_async):
- with NO budget active every helper answers "unlimited" and the retry core
  is byte-identical (same attempt count, same timeouts, same message);
- a nested budget only shortens; a closed budget reads as None (so detached
  tasks revert to unbounded behaviour);
- a new provider operation inside the answer reserve is refused with zero I/O
  (RequestBudgetExhausted, never recorded in the breaker);
- a RETRY starts only when it fits before the retry reserve, and only a
  retry's timeout is capped; the first attempt's timeout is never changed.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tenacity import wait_none

from backend.providers import base as base_module
from backend.providers.base import BaseProvider, _get_breaker
from backend.services import rate_limiter as rates
from backend.services.rate_limiter import ProviderRateLimiter, RateLimiterConfig
from backend.utils import request_budget as rb
from backend.utils import retry as retry_module
from backend.utils.request_budget import (
    MIN_FALLBACK_IO_SECONDS,
    MIN_RETRY_ATTEMPT_SECONDS,
    budget_allows,
    current_budget,
    end_request_budget,
    fallback_allowed,
    io_remaining,
    request_budget,
    retry_attempt_timeout,
    retry_fits,
    start_request_budget,
)
from backend.utils.retry import (
    DataNotAvailableError,
    ProviderCoolingDown,
    ProviderUnavailable,
    RequestBudgetExhausted,
    retry_async,
)

_URL = "https://example.invalid/budget"
_PROVIDER = "BUDGET_TEST"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def test_no_budget_means_unlimited():
    assert current_budget() is None
    assert io_remaining() is None
    assert budget_allows() is True
    assert budget_allows(10_000) is True
    assert retry_fits(5) is True
    assert fallback_allowed() is True
    assert retry_attempt_timeout(30.0) == 30.0


def test_budget_reserves_nesting_and_close():
    with request_budget(10, answer_reserve_seconds=2, retry_reserve_seconds=4) as outer:
        assert current_budget() is outer
        assert io_remaining() == pytest.approx(8, abs=0.2)
        with request_budget(100, answer_reserve_seconds=2, retry_reserve_seconds=4) as nested:
            # A nested budget may only shorten: the outer deadline wins.
            assert nested.deadline == pytest.approx(outer.deadline, abs=1e-6)
        with request_budget(1, answer_reserve_seconds=0, retry_reserve_seconds=0) as shorter:
            assert shorter.deadline < outer.deadline
            assert current_budget() is shorter
        assert current_budget() is outer
    assert current_budget() is None
    assert outer.closed is True


def test_retry_reserve_is_coerced_up_to_the_answer_reserve():
    with request_budget(10, answer_reserve_seconds=5, retry_reserve_seconds=1) as budget:
        assert budget.retry_reserve_s == 5
    with request_budget(10, answer_reserve_seconds=-3, retry_reserve_seconds=-1) as budget:
        assert budget.answer_reserve_s == 0 and budget.retry_reserve_s == 0


def test_retry_attempt_timeout_never_raises_the_requested_value():
    with request_budget(9, answer_reserve_seconds=1, retry_reserve_seconds=3):
        assert retry_attempt_timeout(5.0) == 5.0           # fits: unchanged
        assert retry_attempt_timeout(10.0) == pytest.approx(6.0, abs=0.1)
        assert retry_attempt_timeout(0.3) == 0.3           # never raised to the floor
    with request_budget(4, answer_reserve_seconds=1, retry_reserve_seconds=3.5):
        # cap (~0.5s) is below the floor: floor, but never above the request.
        assert retry_attempt_timeout(10.0) == MIN_RETRY_ATTEMPT_SECONDS


def test_fallback_needs_minimum_io_time():
    with request_budget(MIN_FALLBACK_IO_SECONDS + 5, answer_reserve_seconds=1, retry_reserve_seconds=1):
        assert fallback_allowed() is True
    with request_budget(MIN_FALLBACK_IO_SECONDS, answer_reserve_seconds=1, retry_reserve_seconds=1):
        assert fallback_allowed() is False


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------


async def test_children_see_the_same_budget_object():
    with request_budget(30, answer_reserve_seconds=1, retry_reserve_seconds=2) as budget:
        async def _child():
            return current_budget()

        gathered = await asyncio.gather(_child(), _child())
        task_seen = await asyncio.create_task(_child())
        thread_seen = await asyncio.to_thread(current_budget)
    assert gathered == [budget, budget]
    assert task_seen is budget
    assert thread_seen is budget


async def test_concurrent_requests_are_isolated():
    seen = {}

    async def _request(name, total):
        with request_budget(total, answer_reserve_seconds=0, retry_reserve_seconds=0) as budget:
            await asyncio.sleep(0.01)
            seen[name] = (budget, current_budget())

    await asyncio.gather(_request("a", 10), _request("b", 50))
    assert seen["a"][1] is seen["a"][0]
    assert seen["b"][1] is seen["b"][0]
    assert seen["a"][0] is not seen["b"][0]


async def test_detached_task_reverts_to_unbounded_once_the_request_ends():
    gate = asyncio.Event()
    observed = {}

    async def _detached():
        observed["before"] = current_budget()
        await gate.wait()
        observed["after"] = current_budget()

    budget, token = start_request_budget(30, answer_reserve_seconds=1, retry_reserve_seconds=2)
    task = asyncio.create_task(_detached())
    await asyncio.sleep(0)
    # The parent resets its token: the task's copied context still holds it.
    rb._CURRENT.reset(token)
    await asyncio.sleep(0)
    assert observed["before"] is budget
    end_request_budget(budget, None)  # closes the shared object
    gate.set()
    await task
    assert observed["after"] is None


def test_end_request_budget_swallows_a_foreign_token():
    budget, token = start_request_budget(5, answer_reserve_seconds=0, retry_reserve_seconds=0)
    end_request_budget(budget, token)
    end_request_budget(budget, token)  # second reset: ValueError/RuntimeError swallowed
    assert current_budget() is None


# ---------------------------------------------------------------------------
# BaseProvider retry core
# ---------------------------------------------------------------------------


class _Provider(BaseProvider):
    @property
    def provider_name(self):
        return _PROVIDER

    async def _fetch_data(self, **params):
        return []


@pytest.fixture(autouse=True)
def _isolated_limiter_and_breakers(monkeypatch):
    monkeypatch.setattr(rates, "_global_rate_limiter", rates.GlobalRateLimiter())
    monkeypatch.setattr(base_module, "_provider_breakers", {})
    monkeypatch.setattr(base_module, "wait_exponential", lambda **kwargs: wait_none())
    rates.get_global_rate_limiter()._limiters[_PROVIDER] = ProviderRateLimiter(
        RateLimiterConfig(_PROVIDER, min_delay_seconds=0, max_requests_per_minute=1000)
    )


def _ok():
    return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", _URL))


def _timeouts(client):
    return [call.kwargs["timeout"] for call in client.get.await_args_list]


def test_no_budget_retry_behaviour_is_unchanged():
    client = SimpleNamespace(get=AsyncMock(side_effect=httpx.ReadTimeout("timed out")))

    async def _go():
        await _Provider()._get_with_retry(client, _URL, timeout=7.0)

    with pytest.raises(ProviderUnavailable) as info:
        asyncio.run(_go())
    assert client.get.await_count == BaseProvider.MAX_RETRIES
    assert _timeouts(client) == [7.0] * BaseProvider.MAX_RETRIES
    assert "; retries exhausted" in str(info.value)
    assert not isinstance(info.value, RequestBudgetExhausted)


def test_no_io_inside_the_answer_reserve():
    client = SimpleNamespace(get=AsyncMock(return_value=_ok()))
    breaker = _get_breaker(_PROVIDER)
    before = breaker.fail_counter

    async def _go():
        with request_budget(2, answer_reserve_seconds=5, retry_reserve_seconds=5):
            await _Provider()._get_with_retry(client, _URL)

    with pytest.raises(RequestBudgetExhausted) as info:
        asyncio.run(_go())
    assert isinstance(info.value, ProviderUnavailable)
    assert isinstance(info.value, DataNotAvailableError)
    assert client.get.await_count == 0
    assert breaker.fail_counter == before


def test_retries_stop_at_the_request_deadline():
    async def _slow_timeout(url, **kwargs):
        await asyncio.sleep(kwargs["timeout"])
        raise httpx.ReadTimeout("timed out")

    client = SimpleNamespace(get=AsyncMock(side_effect=_slow_timeout))

    async def _go():
        with request_budget(8, answer_reserve_seconds=1, retry_reserve_seconds=3):
            await _Provider()._get_with_retry(client, _URL, timeout=2.0)

    started = time.monotonic()
    with pytest.raises(ProviderUnavailable) as info:
        asyncio.run(_go())
    elapsed = time.monotonic() - started
    assert not isinstance(info.value, RequestBudgetExhausted)
    assert "limited by the request deadline" in str(info.value)
    assert client.get.await_count == 2
    assert elapsed < 7
    # The attempts genuinely failed: still ONE breaker outcome.
    assert _get_breaker(_PROVIDER).fail_counter == 1


def test_only_a_retry_timeout_is_capped():
    client = SimpleNamespace(get=AsyncMock(side_effect=[httpx.ConnectError("refused"), _ok()]))

    async def _go():
        with request_budget(9, answer_reserve_seconds=1, retry_reserve_seconds=3):
            return await _Provider()._get_with_retry(client, _URL, timeout=10.0)

    response = asyncio.run(_go())
    assert response.status_code == 200
    first, second = _timeouts(client)
    assert first == 10.0                      # the first attempt is never clamped
    assert MIN_RETRY_ATTEMPT_SECONDS <= second <= 6.05


def test_healthy_first_call_under_a_budget_uses_the_requested_timeout():
    client = SimpleNamespace(get=AsyncMock(return_value=_ok()))

    async def _go():
        with request_budget(5, answer_reserve_seconds=1, retry_reserve_seconds=3):
            return await _Provider()._get_with_retry(client, _URL, timeout=30.0)

    assert asyncio.run(_go()).status_code == 200
    assert _timeouts(client) == [30.0]


def test_post_helper_shares_the_budget_contract():
    client = SimpleNamespace(post=AsyncMock(side_effect=[httpx.ConnectError("refused"), _ok()]))

    async def _go():
        with request_budget(9, answer_reserve_seconds=1, retry_reserve_seconds=3):
            return await _Provider()._post_with_retry(client, _URL, timeout=10.0)

    asyncio.run(_go())
    timeouts = [call.kwargs["timeout"] for call in client.post.await_args_list]
    assert timeouts[0] == 10.0 and timeouts[1] <= 6.05


# ---------------------------------------------------------------------------
# retry_async (whole-pipeline retry wrapper)
# ---------------------------------------------------------------------------


def test_retry_async_stops_when_the_sleep_cannot_fit(monkeypatch):
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    func = AsyncMock(side_effect=httpx.ConnectError("refused"))

    async def _go():
        with request_budget(3.5, answer_reserve_seconds=1, retry_reserve_seconds=1):
            await retry_async(func, max_attempts=3, initial_delay=1.0)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(_go())
    assert func.await_count == 1
    assert sleeps == []


def test_retry_async_without_a_budget_is_unchanged(monkeypatch):
    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    func = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(retry_async(func, max_attempts=3, initial_delay=1.0))
    assert func.await_count == 3


@pytest.mark.parametrize("status", [404, 400, 422, 429])
def test_retry_async_still_never_retries_terminal_statuses(status):
    request = httpx.Request("GET", _URL)
    error = httpx.HTTPStatusError(
        "status", request=request, response=httpx.Response(status, request=request, text="x"),
    )
    func = AsyncMock(side_effect=error)
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(retry_async(func, max_attempts=3, initial_delay=0.0))
    assert func.await_count == 1
    if status == 429:
        assert isinstance(info.value, ProviderCoolingDown)


def test_retry_async_never_retries_a_provider_unavailable():
    func = AsyncMock(side_effect=ProviderUnavailable("down"))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(retry_async(func, max_attempts=3, initial_delay=0.0))
    assert func.await_count == 1
