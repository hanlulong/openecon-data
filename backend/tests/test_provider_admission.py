"""Offline regression coverage for atomic admission and HTTP cooldown boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tenacity import wait_none

from backend.providers import base as base_module
from backend.providers.base import BaseProvider, _get_breaker
from backend.providers import oecd as oecd_module
from backend.providers.oecd import OECDProvider
from backend.services import rate_limiter as rates
from backend.services.rate_limiter import ProviderRateLimiter, RateLimiterConfig, ProviderRateLimitWaitExceeded
from backend.utils.retry import DataNotAvailableError, parse_retry_after, retry_async


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
    if cancel_during == "admission":
        assert breaker.fail_counter == 1
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
async def test_io_cancellation_still_propagates_when_breaker_opens(method, state):
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
    task = asyncio.create_task(getattr(Provider(), f"_{method}_with_retry")(client, "https://example.invalid/fixture"))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert getattr(client, method).call_count == 1


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
