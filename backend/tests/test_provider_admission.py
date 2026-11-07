"""Offline regression coverage for atomic admission and HTTP cooldown boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pybreaker
import pytest
from tenacity import wait_none

from backend.providers import base as base_module
from backend.providers.base import BaseProvider, _get_breaker
from backend.providers import oecd as oecd_module
from backend.providers.oecd import OECDProvider
from backend.services import rate_limiter as rates
from backend.services.rate_limiter import ProviderRateLimiter, RateLimiterConfig, ProviderRateLimitWaitExceeded
from backend.utils import retry as retry_module
from backend.utils.retry import DataNotAvailableError, ProviderCoolingDown, parse_retry_after, retry_async


class Provider(BaseProvider):
    @property
    def provider_name(self):
        return "ADMISSION_TEST"

    async def _fetch_data(self, **params):
        return []


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    monkeypatch.setattr(rates, "_global_rate_limiter", rates.GlobalRateLimiter())
    monkeypatch.setattr(base_module, "_provider_breakers", {})
    monkeypatch.setattr(base_module, "wait_exponential", lambda **kwargs: wait_none())
    monkeypatch.setattr(OECDProvider, "_DATAFLOW_STRUCTURE_CACHE", {})
    rates.get_global_rate_limiter()._limiters["ADMISSION_TEST"] = ProviderRateLimiter(
        RateLimiterConfig("ADMISSION_TEST", min_delay_seconds=0, max_requests_per_minute=100)
    )


def response(status=200, headers=None):
    return httpx.Response(status, headers=headers, json={"ok": True}, request=httpx.Request("GET", "https://example.invalid/fixture"))


def client_for(method, responses):
    client = SimpleNamespace()
    setattr(client, method, AsyncMock(side_effect=responses))
    return client


class Clock:
    def __init__(self):
        self.value = 10000.0
        self.sleeps = []
        self.after_sleep = None

    def time(self):
        return self.value

    monotonic = time

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds
        if self.after_sleep:
            self.after_sleep()


def fake_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rates, "time", clock)
    monkeypatch.setattr(rates, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return clock


@pytest.mark.asyncio
async def test_concurrent_reservations_are_spaced_and_counted_once():
    limiter = ProviderRateLimiter(RateLimiterConfig("T", min_delay_seconds=0.01, max_requests_per_minute=20))
    starts = []

    async def request():
        await limiter.acquire(max_wait_seconds=1)
        starts.append(asyncio.get_running_loop().time())

    await asyncio.gather(*(request() for _ in range(5)))
    assert len(limiter.minute_window) == 5
    assert all(b - a >= 0.009 for a, b in zip(starts, starts[1:]))


@pytest.mark.asyncio
async def test_total_wait_budget_is_not_reset_after_competing_wakeup(monkeypatch):
    clock = fake_clock(monkeypatch)
    limiter = ProviderRateLimiter(RateLimiterConfig("T", min_delay_seconds=3, max_requests_per_minute=20))
    limiter.record_request()
    clock.after_sleep = limiter.record_request  # Another caller wins the wakeup.
    with pytest.raises(ProviderRateLimitWaitExceeded):
        await limiter.acquire(max_wait_seconds=5)
    assert clock.sleeps == [3]
    assert len(limiter.minute_window) == 2  # Initial and competing request only.
    assert limiter._consecutive_429_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("window,seconds", [("minute", 60), ("hour", 3600)])
async def test_quota_is_rechecked_after_wakeup(monkeypatch, window, seconds):
    clock = fake_clock(monkeypatch)
    config = {f"max_requests_per_{window}": 1}
    limiter = ProviderRateLimiter(RateLimiterConfig("T", min_delay_seconds=0, **config))
    limiter.record_request()
    clock.after_sleep = limiter.record_request
    with pytest.raises(ProviderRateLimitWaitExceeded):
        await limiter.acquire(max_wait_seconds=seconds + 1)
    assert clock.sleeps == [seconds]


@pytest.mark.asyncio
async def test_cooldown_arriving_during_wait_prevents_reservation(monkeypatch):
    clock = fake_clock(monkeypatch)
    limiter = ProviderRateLimiter(RateLimiterConfig("T", min_delay_seconds=3, max_requests_per_minute=20))
    limiter.record_request()
    clock.after_sleep = lambda: limiter.record_rate_limit_error(120)
    with pytest.raises(ProviderRateLimitWaitExceeded, match="cooldown"):
        await limiter.acquire(max_wait_seconds=5)
    assert len(limiter.minute_window) == 1
    assert limiter._consecutive_429_count == 1


@pytest.mark.asyncio
async def test_zero_budget_and_invalid_budgets():
    limiter = ProviderRateLimiter(RateLimiterConfig("T", min_delay_seconds=10, max_requests_per_minute=20))
    await limiter.acquire(max_wait_seconds=0)
    with pytest.raises(ProviderRateLimitWaitExceeded):
        await limiter.acquire(max_wait_seconds=0)
    for budget in [-1, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            await limiter.acquire(max_wait_seconds=budget)
    assert len(limiter.minute_window) == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_reserves_nothing_and_does_not_block_next():
    limiter = ProviderRateLimiter(RateLimiterConfig("T", min_delay_seconds=0.03, max_requests_per_minute=20))
    await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(limiter.minute_window) == 1
    await limiter.acquire()
    assert len(limiter.minute_window) == 2


@pytest.mark.parametrize("header,expected", [(None, 60), ("bad", 60), ("NaN", 60), ("inf", 60), ("-1", 60), ("1.5", 60), ("120", 120), ("0", 0)])
def test_retry_after_parser(header, expected):
    assert parse_retry_after(header) == expected


def test_http_dates_and_extreme_header_are_safe():
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=120), usegmt=True), now=now) == 120
    assert parse_retry_after(format_datetime(now - timedelta(seconds=120), usegmt=True), now=now) == 0
    huge = parse_retry_after("9" * 5000, now=now)
    assert huge == 253402300799 - now.timestamp()
    assert parse_retry_after("0" * 5000 + "120", now=now) == 120


def test_shared_cooldown_never_shortens_and_late_success_is_not_recovery(monkeypatch):
    clock = fake_clock(monkeypatch)
    limiter = ProviderRateLimiter(RateLimiterConfig("T"))
    limiter.record_rate_limit_error(500)
    deadline = limiter._circuit_open_until
    clock.value += 1
    limiter.record_rate_limit_error(1)
    limiter.record_success()
    assert limiter._circuit_open_until == deadline
    assert limiter._consecutive_429_count == 2
    clock.value = deadline
    assert not limiter.is_circuit_open()
    limiter.record_success()
    assert limiter._consecutive_429_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("header", ["60", "120", "Wed, 01 Jan 2031 00:00:00 GMT", "bad", None, "9" * 5000])
async def test_429_sends_once_and_fresh_instance_respects_shared_cooldown(method, header):
    client = client_for(method, [response(429, {} if header is None else {"Retry-After": header}), response()])
    provider = Provider()
    with pytest.raises(DataNotAvailableError, match="Rate limited"):
        await getattr(provider, f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert provider.rate_limit_reset is not None
    limiter = rates.get_global_rate_limiter().get_limiter("ADMISSION_TEST")
    assert limiter._consecutive_429_count == 1
    assert limiter.get_circuit_status()["cooldown_remaining_seconds"] > 59
    breaker = _get_breaker("ADMISSION_TEST")
    assert breaker.fail_counter == 1
    with pytest.raises(DataNotAvailableError, match="rate-limited"):
        await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert getattr(client, method).call_count == 1
    assert breaker.fail_counter == 1
    assert len(limiter.minute_window) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("state", ["closed", "half-open"])
async def test_zero_http_local_refusal_preserves_breaker_state_and_counters(monkeypatch, method, state):
    breaker = _get_breaker("ADMISSION_TEST")
    breaker._state_storage.increment_counter()
    if state == "half-open":
        breaker.half_open()
        breaker._state_storage.increment_success_counter()
    before = (breaker.current_state, breaker.fail_counter, breaker._state_storage.success_counter)
    monkeypatch.setattr(base_module, "acquire_provider", AsyncMock(side_effect=ProviderRateLimitWaitExceeded("local refusal")))
    client = client_for(method, [response()])
    for _ in range(6):
        with pytest.raises(DataNotAvailableError, match="local refusal"):
            await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert getattr(client, method).call_count == 0
    assert (breaker.current_state, breaker.fail_counter, breaker._state_storage.success_counter) == before


@pytest.mark.asyncio
async def test_refusal_does_not_restore_stale_concurrent_breaker_outcome(monkeypatch):
    breaker = _get_breaker("ADMISSION_TEST")
    blocker, ready = asyncio.Event(), asyncio.Event()
    async def refuse(_provider):
        ready.set()
        await blocker.wait()
        raise ProviderRateLimitWaitExceeded("local refusal")
    monkeypatch.setattr(base_module, "acquire_provider", refuse)
    client = client_for("get", [response()])
    pending = asyncio.create_task(Provider()._get_with_retry(client, "https://example.invalid/fixture"))
    await ready.wait()
    with pytest.raises(httpx.ConnectError):
        with breaker.calling():
            raise httpx.ConnectError("concurrent genuine failure")
    blocker.set()
    with pytest.raises(DataNotAvailableError):
        await pending
    assert breaker.fail_counter == 1
    assert client.get.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_later_refusal_counts_prior_upstream_failure_once(monkeypatch, method):
    monkeypatch.setattr(base_module, "acquire_provider", AsyncMock(side_effect=[0, ProviderRateLimitWaitExceeded("local refusal")]))
    client = client_for(method, [response(500), response()])
    with pytest.raises(DataNotAvailableError, match="after 1 HTTP attempts.*local rate limit"):
        await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert getattr(client, method).call_count == 1
    assert _get_breaker("ADMISSION_TEST").fail_counter == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_retry_operation_counts_once_and_half_open_recovers(method):
    breaker = _get_breaker("ADMISSION_TEST")
    client = client_for(method, [response(500)] * 3)
    with pytest.raises(DataNotAvailableError):
        await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert getattr(client, method).call_count == 3
    assert breaker.fail_counter == 1
    breaker.half_open()
    client = client_for(method, [response(500), response()])
    await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert breaker.current_state == "closed"
    assert breaker.fail_counter == 0
    assert getattr(client, method).call_count == 2


@pytest.mark.asyncio
async def test_already_open_breaker_uses_no_reservation_and_expired_open_recovers():
    breaker = _get_breaker("ADMISSION_TEST")
    breaker.open()
    limiter = rates.get_global_rate_limiter().get_limiter("ADMISSION_TEST")
    client = client_for("get", [response()])
    with pytest.raises(DataNotAvailableError, match="circuit breaker OPEN"):
        await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    assert not limiter.minute_window
    breaker._state_storage.opened_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    assert breaker.current_state == "closed"
    assert len(limiter.minute_window) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("cancel_during", ["admission", "http"])
async def test_cancellation_propagates_and_never_retries(monkeypatch, method, cancel_during):
    ready = asyncio.Event()
    async def block(*args, **kwargs):
        ready.set()
        await asyncio.Event().wait()
    client = client_for(method, [response()])
    if cancel_during == "admission":
        monkeypatch.setattr(base_module, "acquire_provider", block)
    else:
        setattr(client, method, AsyncMock(side_effect=block))
    breaker = _get_breaker("ADMISSION_TEST")
    breaker._state_storage.increment_counter()
    task = asyncio.create_task(getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture"))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert getattr(client, method).call_count == (cancel_during == "http")
    assert breaker.fail_counter == 1  # cancellation is neutral in either phase
    if cancel_during == "admission":
        assert not rates.get_global_rate_limiter().get_limiter("ADMISSION_TEST").minute_window


@pytest.mark.asyncio
async def test_oecd_metadata_cache_hit_free_and_miss_admitted_once(monkeypatch):
    permit = AsyncMock(return_value=0)
    monkeypatch.setattr(oecd_module, "acquire_provider", permit)
    client = client_for("get", [response()])
    monkeypatch.setattr(oecd_module, "get_http_client", lambda: client)
    provider = OECDProvider(metadata_search_service=None)
    monkeypatch.setattr(provider, "_parse_oecd_dataflow_structure", lambda *args: {"dimensions": ["fixture"]})
    first = await provider._fetch_oecd_dataflow_structure_at_base("https://example.invalid", "A", "D", "1")
    second = await provider._fetch_oecd_dataflow_structure_at_base("https://example.invalid", "A", "D", "1")
    assert first is second
    permit.assert_awaited_once_with("OECD", max_wait_seconds=5.0)
    assert client.get.call_count == 1
    assert client.get.call_args.kwargs["timeout"] == 60


@pytest.mark.asyncio
async def test_oecd_every_retry_admitted_and_429_does_not_retry(monkeypatch):
    permit = AsyncMock(return_value=0)
    monkeypatch.setattr(oecd_module, "acquire_provider", permit)
    provider = OECDProvider(metadata_search_service=None)
    client = client_for("get", [response(500), response(), response(429, {"Retry-After": "120"})])
    await retry_async(lambda: provider._admitted_get(client, "https://example.invalid", timeout=50), initial_delay=0)
    assert permit.await_count == client.get.call_count == 2
    with pytest.raises(DataNotAvailableError, match="cooldown"):
        await retry_async(lambda: provider._admitted_get(client, "https://example.invalid", timeout=50), initial_delay=0)
    assert permit.await_count == client.get.call_count == 3
    assert rates.get_global_rate_limiter().get_limiter("OECD")._consecutive_429_count == 1
    assert all(call.kwargs == {"max_wait_seconds": 5.0} for call in permit.await_args_list)


@pytest.mark.asyncio
async def test_oecd_refusal_and_wait_cancellation_do_not_send_or_count_429(monkeypatch):
    provider = OECDProvider(metadata_search_service=None)
    client = client_for("get", [response()])
    monkeypatch.setattr(oecd_module, "acquire_provider", AsyncMock(side_effect=ProviderRateLimitWaitExceeded("local refusal")))
    with pytest.raises(DataNotAvailableError, match="temporarily rate-limited"):
        await provider._admitted_get(client, "https://example.invalid")
    ready = asyncio.Event()
    async def block(*args, **kwargs):
        ready.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(oecd_module, "acquire_provider", block)
    pending = asyncio.create_task(provider._admitted_get(client, "https://example.invalid"))
    await ready.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert client.get.call_count == 0
    assert rates.get_global_rate_limiter().get_limiter("OECD")._consecutive_429_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("state", ["near-open", "half-open"])
async def test_io_cancellation_propagates_and_is_breaker_neutral(method, state):
    # A caller cancelling (SSE disconnect, wait_for deadline, fan-out teardown)
    # is not a provider failure: it must neither trip a near-open breaker nor
    # re-open a half-open one.
    ready = asyncio.Event()
    async def blocked(*args, **kwargs):
        ready.set()
        await asyncio.Event().wait()
    client = SimpleNamespace(**{method: AsyncMock(side_effect=blocked)})
    breaker = _get_breaker("ADMISSION_TEST")
    for _ in range(4):
        breaker._state_storage.increment_counter()
    if state == "half-open":
        breaker.half_open()
    before = (breaker.current_state, breaker.fail_counter)
    task = asyncio.create_task(getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture"))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert getattr(client, method).call_count == 1
    assert (breaker.current_state, breaker.fail_counter) == before
    # The half-open trial is still available to the next real outcome.
    setattr(client, method, AsyncMock(return_value=response()))
    await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert breaker.current_state == "closed"
    assert breaker.fail_counter == 0


async def _cancel_while_in_http(method, count=1):
    ready = asyncio.Event()
    started = 0
    async def blocked(*args, **kwargs):
        nonlocal started
        started += 1
        if started == count:
            ready.set()
        await asyncio.Event().wait()
    client = SimpleNamespace(**{method: AsyncMock(side_effect=blocked)})
    tasks = [
        asyncio.create_task(getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture"))
        for _ in range(count)
    ]
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
    except asyncio.TimeoutError:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        pytest.fail(f"request was refused before reaching HTTP: {outcomes!r}")
    outer = asyncio.gather(*tasks)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_consecutive_http_cancellations_never_open_breaker(method):
    breaker = _get_breaker("ADMISSION_TEST")
    for _ in range(breaker.fail_max + 1):
        await _cancel_while_in_http(method)
    assert breaker.current_state == "closed"
    assert breaker.fail_counter == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_one_outer_cancel_of_same_provider_fanout_counts_nothing(method):
    breaker = _get_breaker("ADMISSION_TEST")
    client = await _cancel_while_in_http(method, count=breaker.fail_max)
    assert getattr(client, method).call_count == breaker.fail_max
    assert breaker.current_state == "closed"
    assert breaker.fail_counter == 0


async def _open_breaker_during_io(method, outcome):
    """Run one request whose I/O completes after a concurrent failure opened the breaker."""
    breaker = _get_breaker("ADMISSION_TEST")
    for _ in range(breaker.fail_max - 1):
        breaker._state_storage.increment_counter()
    in_io, release = asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        in_io.set()
        await release.wait()
        return outcome
    client = SimpleNamespace(**{method: AsyncMock(side_effect=slow)})
    pending = asyncio.create_task(getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture"))
    await in_io.wait()
    with pytest.raises(pybreaker.CircuitBreakerError):
        with breaker.calling():
            raise httpx.ConnectError("concurrent genuine failure")
    assert breaker.current_state == "open"
    release.set()
    return breaker, client, pending


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_success_is_kept_when_breaker_opened_during_io(method):
    breaker, client, pending = await _open_breaker_during_io(method, response())
    result = await pending
    assert result.status_code == 200
    assert breaker.current_state == "open"
    assert breaker.fail_counter == breaker.fail_max


@pytest.mark.asyncio
async def test_failure_when_breaker_opened_during_io_raises_its_own_error():
    breaker, client, pending = await _open_breaker_during_io("get", response(404))
    with pytest.raises(DataNotAvailableError, match="API returned 404"):
        await pending
    assert breaker.current_state == "open"
    assert client.get.call_count == 1


@pytest.mark.asyncio
async def test_failure_that_trips_breaker_reports_the_upstream_failure():
    breaker = _get_breaker("ADMISSION_TEST")
    for _ in range(breaker.fail_max - 1):
        breaker._state_storage.increment_counter()
    client = client_for("get", [response(500)] * 3)
    with pytest.raises(DataNotAvailableError, match="Failed after 3 HTTP attempts"):
        await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    assert breaker.current_state == "open"
    with pytest.raises(DataNotAvailableError, match="circuit breaker OPEN"):
        await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    assert client.get.call_count == 3


def test_breaker_open_check_degrades_when_private_state_is_unavailable():
    # Simulates a pybreaker release that renames its private storage.
    assert base_module._breaker_refuses_now(SimpleNamespace(current_state=pybreaker.STATE_OPEN)) is False
    breaker = _get_breaker("ADMISSION_TEST")
    breaker.open()
    assert base_module._breaker_refuses_now(breaker) is True


@pytest.mark.asyncio
async def test_breaker_bookkeeping_error_never_drops_fetched_data(monkeypatch):
    breaker = _get_breaker("ADMISSION_TEST")
    def broken_calling():
        raise AttributeError("incompatible pybreaker")
    monkeypatch.setattr(breaker, "calling", broken_calling)
    result = await Provider()._get_with_retry(client_for("get", [response()]), "https://example.invalid/fixture")
    assert result.status_code == 200


# --- Retry-After cap (limiter policy) -------------------------------------


def test_pathological_retry_after_is_capped_by_limiter(monkeypatch):
    fake_clock(monkeypatch)
    limiter = ProviderRateLimiter(RateLimiterConfig("T"))
    remaining = limiter.record_rate_limit_error(31536000)
    cap = rates.MAX_RETRY_AFTER_COOLDOWN_SECONDS
    assert remaining == cap
    assert limiter.get_circuit_status()["cooldown_remaining_seconds"] <= cap
    # The escalating tier still applies below the cap; a shorter header never shortens it.
    limiter2 = ProviderRateLimiter(RateLimiterConfig("T2"))
    assert limiter2.record_rate_limit_error(5) == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["31536000", "9" * 5000, "Fri, 31 Dec 9999 23:59:59 GMT"])
async def test_http_429_with_huge_retry_after_reopens_after_cap(monkeypatch, header):
    clock = fake_clock(monkeypatch)
    client = client_for("get", [response(429, {"Retry-After": header}), response()])
    with pytest.raises(ProviderCoolingDown) as caught:
        await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    cap = rates.MAX_RETRY_AFTER_COOLDOWN_SECONDS
    assert caught.value.retry_after_seconds == cap
    clock.value += cap + 1
    await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    assert client.get.call_count == 2


@pytest.mark.parametrize("raw,expected", [(None, 900.0), ("120", 120.0), ("bad", 900.0), ("-5", 900.0), ("inf", 900.0), ("0", 900.0)])
def test_retry_after_cap_env_override(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("PROVIDER_MAX_RETRY_AFTER_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PROVIDER_MAX_RETRY_AFTER_SECONDS", raw)
    assert rates._env_positive_seconds("PROVIDER_MAX_RETRY_AFTER_SECONDS", 900.0) == expected


# --- Typed cooldown signal -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_429_and_cooldown_refusal_raise_typed_cooling_down(method):
    client = client_for(method, [response(429, {"Retry-After": "120"}), response()])
    with pytest.raises(ProviderCoolingDown, match="Rate limited") as first:
        await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert first.value.retry_after_seconds == 120
    with pytest.raises(ProviderCoolingDown, match="cooldown is active") as refused:
        await getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture")
    assert 0 < refused.value.retry_after_seconds <= 120
    assert isinstance(refused.value, DataNotAvailableError)
    assert getattr(client, method).call_count == 1


@pytest.mark.asyncio
async def test_budget_refusal_is_not_reported_as_cooldown(monkeypatch):
    monkeypatch.setattr(base_module, "acquire_provider", AsyncMock(side_effect=ProviderRateLimitWaitExceeded("budget")))
    with pytest.raises(DataNotAvailableError, match="budget") as caught:
        await Provider()._get_with_retry(client_for("get", [response()]), "https://example.invalid/fixture")
    assert not isinstance(caught.value, ProviderCoolingDown)


@pytest.mark.asyncio
async def test_retry_async_429_is_terminal_without_sleeping(monkeypatch):
    sleeps = []
    async def record_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(retry_module.asyncio, "sleep", record_sleep)
    rate_limited = response(429, {"Retry-After": "99999"})
    calls = 0
    async def call():
        nonlocal calls
        calls += 1
        raise httpx.HTTPStatusError("429", request=rate_limited.request, response=rate_limited)
    with pytest.raises(ProviderCoolingDown) as caught:
        await retry_async(call, max_attempts=3, initial_delay=0)
    assert calls == 1
    assert sleeps == []
    assert caught.value.retry_after_seconds == 99999


# --- StatsCan streamed full-table download uses the shared contract -------


class _StreamClient:
    def __init__(self, status=200, headers=None, body=b""):
        self.calls = []
        self.status, self.headers, self.body = status, headers or {}, body

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url))
        client = self
        class _Ctx:
            async def __aenter__(self):
                return SimpleNamespace(
                    status_code=client.status,
                    headers=client.headers,
                    aiter_bytes=lambda: _aiter([client.body]),
                )
            async def __aexit__(self, *exc):
                return False
        return _Ctx()


async def _aiter(items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_statscan_stream_refuses_during_open_cooldown_without_sending(monkeypatch):
    from backend.providers import statscan as statscan_module
    client = _StreamClient()
    monkeypatch.setattr(statscan_module, "get_statscan_http_client", lambda: client)
    rates.get_global_rate_limiter().get_limiter("STATSCAN").record_rate_limit_error(120)
    with pytest.raises(ProviderCoolingDown, match="cooldown is active"):
        await statscan_module.StatsCanProvider()._download_full_table_csv_bundle("13100071")
    assert client.calls == []


@pytest.mark.asyncio
async def test_statscan_stream_429_opens_shared_cooldown_with_retry_after(monkeypatch):
    from backend.providers import statscan as statscan_module
    client = _StreamClient(status=429, headers={"Retry-After": "300"})
    monkeypatch.setattr(statscan_module, "get_statscan_http_client", lambda: client)
    with pytest.raises(ProviderCoolingDown, match="Rate limited"):
        await statscan_module.StatsCanProvider()._download_full_table_csv_bundle("13100071")
    limiter = rates.get_global_rate_limiter().get_limiter("STATSCAN")
    assert limiter.get_circuit_status()["cooldown_remaining_seconds"] > 299
    assert len(limiter.minute_window) == 1  # the one admitted request
    with pytest.raises(ProviderCoolingDown):
        await statscan_module.StatsCanProvider()._download_full_table_csv_bundle("13100071")
    assert len(client.calls) == 1


# --- Comtrade fan-out stops at the first cooldown --------------------------


@pytest.mark.asyncio
async def test_comtrade_multi_chunk_fetch_stops_at_first_429(monkeypatch):
    from backend.providers import comtrade as comtrade_module
    rates.get_global_rate_limiter()._limiters["COMTRADE"] = ProviderRateLimiter(
        RateLimiterConfig("COMTRADE", min_delay_seconds=0, max_requests_per_minute=100)
    )
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(comtrade_module, "get_http_client", lambda: http)
    provider = comtrade_module.ComtradeProvider("test-key-not-real")
    single = provider._fetch_single_reporter_data
    single_calls = 0
    async def counted(*args, **kwargs):
        nonlocal single_calls
        single_calls += 1
        return await single(*args, **kwargs)
    monkeypatch.setattr(provider, "_fetch_single_reporter_data", counted)
    try:
        with pytest.raises(ProviderCoolingDown):
            await provider.fetch_trade_data(
                reporter="Canada", partner="USA", flow="EXPORTS",
                start_year=2020, end_year=2023, frequency="monthly",
            )
    finally:
        await http.aclose()
    assert len(requests) == 1
    assert single_calls == 1


@pytest.mark.asyncio
async def test_comtrade_trade_balance_keeps_cooldown_type(monkeypatch):
    from backend.providers import comtrade as comtrade_module
    provider = comtrade_module.ComtradeProvider("test-key-not-real")
    fetch = AsyncMock(side_effect=ProviderCoolingDown("cooling", retry_after_seconds=60))
    monkeypatch.setattr(provider, "fetch_trade_data", fetch)
    with pytest.raises(ProviderCoolingDown, match="export data") as caught:
        await provider.fetch_trade_balance(reporter="Canada", start_year=2020, end_year=2023)
    assert caught.value.retry_after_seconds == 60
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_shared_cooldown_expiry_allows_new_instance_and_resets_streak(monkeypatch):
    clock = fake_clock(monkeypatch)
    client = client_for("get", [response(429, {"Retry-After": "120"}), response()])
    with pytest.raises(DataNotAvailableError, match="Rate limited"):
        await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    limiter = rates.get_global_rate_limiter().get_limiter("ADMISSION_TEST")
    clock.value += 121
    await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    assert client.get.call_count == 2
    assert limiter._consecutive_429_count == 0
    assert not limiter.is_circuit_open()


@pytest.mark.asyncio
async def test_breaker_opening_during_admission_does_not_send_or_overwrite_state(monkeypatch):
    breaker = _get_breaker("ADMISSION_TEST")
    for _ in range(4):
        breaker._state_storage.increment_counter()
    real_acquire = rates.acquire_provider
    async def concurrent_open(provider):
        await real_acquire(provider)
        try:
            with breaker.calling():
                raise httpx.ConnectError("concurrent genuine failure")
        except Exception:
            pass
    monkeypatch.setattr(base_module, "acquire_provider", concurrent_open)
    client = client_for("get", [response()])
    with pytest.raises(DataNotAvailableError, match="circuit breaker OPEN"):
        await Provider()._get_with_retry(client, "https://example.invalid/fixture")
    assert client.get.call_count == 0
    assert breaker.current_state == "open"
    assert breaker.fail_counter == 5
    assert len(rates.get_global_rate_limiter().get_limiter("ADMISSION_TEST").minute_window) == 1
