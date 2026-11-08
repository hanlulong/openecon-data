"""A provider that could not be reached must never read as "no data".

The shared provider request layer (BaseProvider._get_with_retry/_post_with_retry
and admit_provider_request) is the ONE place that knows whether the provider
answered. It types every no-verdict failure — exhausted 5xx retries,
connection/timeout/transport errors, an open availability breaker, a local
admission refusal, a 429 cooldown — as ``ProviderUnavailable`` (a
DataNotAvailableError subclass, so every existing handler and fallback keeps
working), while a 4xx answer stays a plain DataNotAvailableError verdict.
Consumers that treat a failed attempt as a fetch-validated dead end (the
no-data menu's attempted-code exclusion, the multi-indicator failure reason)
key on that type, never on message text. Provider re-wraps keep the type.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tenacity import wait_none

from backend.providers import base as base_module
from backend.providers.base import BaseProvider, _get_breaker
from backend.providers.comtrade import ComtradeProvider
from backend.services import data_fetcher as DF
from backend.services import rate_limiter as rates
from backend.services.rate_limiter import (
    ProviderRateLimiter,
    ProviderRateLimitWaitExceeded,
    RateLimiterConfig,
)
from backend.utils.retry import (
    DataNotAvailableError,
    ProviderCoolingDown,
    ProviderUnavailable,
    is_provider_unavailable,
)

_URL = "https://example.invalid/fixture"


class _Provider(BaseProvider):
    @property
    def provider_name(self):
        return "UNAVAILABLE_TEST"

    async def _fetch_data(self, **params):
        return []


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    monkeypatch.setattr(rates, "_global_rate_limiter", rates.GlobalRateLimiter())
    monkeypatch.setattr(base_module, "_provider_breakers", {})
    monkeypatch.setattr(base_module, "wait_exponential", lambda **kwargs: wait_none())
    rates.get_global_rate_limiter()._limiters["UNAVAILABLE_TEST"] = ProviderRateLimiter(
        RateLimiterConfig("UNAVAILABLE_TEST", min_delay_seconds=0, max_requests_per_minute=1000)
    )


def _response(status=200):
    return httpx.Response(status, json={"ok": True}, request=httpx.Request("GET", _URL))


def _client(side_effect):
    return SimpleNamespace(get=AsyncMock(side_effect=side_effect), post=AsyncMock(side_effect=side_effect))


def _raised(method, side_effect):
    async def _go():
        await getattr(_Provider(), f"_{method}_with_retry")(_client(side_effect), _URL)

    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(_go())
    return info.value


_NO_VERDICT = {
    "5xx_retries_exhausted": lambda: [_response(503)] * 3,
    "connect_error": lambda: httpx.ConnectError("connection refused"),
    "read_timeout": lambda: httpx.ReadTimeout("timed out"),
    "server_dropped_connection": lambda: httpx.RemoteProtocolError("server disconnected"),
}


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("failure", sorted(_NO_VERDICT))
def test_no_verdict_transport_failures_are_typed_unavailable(method, failure):
    error = _raised(method, _NO_VERDICT[failure]())
    assert isinstance(error, ProviderUnavailable)
    assert is_provider_unavailable(error)
    assert not isinstance(error, ProviderCoolingDown)


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_a_4xx_answer_stays_a_plain_verdict(status):
    error = _raised("get", [_response(status)])
    assert type(error) is DataNotAvailableError
    assert not is_provider_unavailable(error)


def test_open_breaker_is_typed_unavailable():
    _get_breaker("UNAVAILABLE_TEST").open()
    error = _raised("get", [_response(200)])
    assert "circuit breaker OPEN" in str(error)
    assert is_provider_unavailable(error)


def test_admission_budget_refusal_is_typed_unavailable(monkeypatch):
    monkeypatch.setattr(
        base_module, "acquire_provider",
        AsyncMock(side_effect=ProviderRateLimitWaitExceeded("wait budget exceeded")),
    )
    error = _raised("get", [_response(200)])
    assert is_provider_unavailable(error)
    assert not isinstance(error, ProviderCoolingDown)


def test_cooldown_is_a_no_verdict_failure_too():
    error = _raised("get", [httpx.Response(429, request=httpx.Request("GET", _URL))])
    assert isinstance(error, ProviderCoolingDown)
    assert is_provider_unavailable(error)


def test_with_context_keeps_the_no_verdict_class():
    plain = ProviderUnavailable("timed out").with_context("Comtrade API error for reporter DE")
    assert type(plain) is ProviderUnavailable and "timed out" in str(plain)
    cooling = ProviderCoolingDown("429", retry_after_seconds=7.0).with_context("ctx")
    assert type(cooling) is ProviderCoolingDown and cooling.retry_after_seconds == 7.0
    assert not is_provider_unavailable(None)
    assert not is_provider_unavailable(DataNotAvailableError("API returned 404"))
    assert not is_provider_unavailable(TimeoutError("not a provider verdict either way"))


# --------------------------------------------------------------------------
# Provider re-wraps keep the type (Comtrade: per-reporter and trade balance)
# --------------------------------------------------------------------------

def test_comtrade_reporter_rewrap_keeps_unavailable(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    monkeypatch.setattr(
        provider, "_get_with_retry",
        AsyncMock(side_effect=ProviderUnavailable("Failed after 3 HTTP attempts; retries exhausted: Server error (503)")),
    )
    with pytest.raises(ProviderUnavailable) as info:
        asyncio.run(provider._fetch_single_reporter_data(
            SimpleNamespace(), "DE", "0", "TOTAL", "X", "2023", "A",
        ))
    assert "Comtrade API error for reporter DE" in str(info.value)


def test_comtrade_reporter_rewrap_keeps_a_definitive_verdict_plain(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    monkeypatch.setattr(
        provider, "_get_with_retry", AsyncMock(side_effect=DataNotAvailableError("API returned 404")),
    )
    with pytest.raises(DataNotAvailableError) as info:
        asyncio.run(provider._fetch_single_reporter_data(
            SimpleNamespace(), "DE", "0", "TOTAL", "X", "2023", "A",
        ))
    assert not is_provider_unavailable(info.value)


@pytest.mark.parametrize(
    "raised,expected",
    [
        (ProviderUnavailable("connect failed"), ProviderUnavailable),
        (ProviderCoolingDown("429", retry_after_seconds=3.0), ProviderCoolingDown),
    ],
)
def test_comtrade_trade_balance_rewrap_keeps_the_class(monkeypatch, raised, expected):
    provider = ComtradeProvider(api_key="demo")

    async def failing(**_kwargs):
        raise raised

    monkeypatch.setattr(provider, "fetch_trade_data", failing)
    with pytest.raises(expected) as info:
        asyncio.run(provider.fetch_trade_balance(reporter="Canada", start_year=2020, end_year=2022))
    assert type(info.value) is expected
    assert "trade balance calculation" in str(info.value)


# --------------------------------------------------------------------------
# Multi-indicator failure reason: typed, never "no_data" for an outage
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "outcome,reason",
    [
        (ProviderUnavailable("BIS unavailable (transport failure, not a no-data verdict)"), "unavailable"),
        (ProviderCoolingDown("Rate limited (429)"), "unavailable"),
        (DataNotAvailableError("API returned 404"), "no_data"),
        ([], "no_data"),
        (asyncio.TimeoutError("indicator fetch timed out"), "timeout"),
        (ValueError("bug"), "error"),
    ],
)
def test_multi_indicator_failure_reason_is_typed(outcome, reason):
    assert DF._multi_indicator_failure_reason(outcome) == reason  # pylint: disable=protected-access


def test_partial_multi_indicator_fetch_reports_an_outage_as_unavailable():
    from backend.models import Metadata, NormalizedData, ParsedIntent

    intent = ParsedIntent(
        apiProvider="FRED", indicators=["unemployment rate", "CPI"],
        parameters={"country": "US"}, clarificationNeeded=False,
        originalQuery="US unemployment and CPI",
    )

    async def fetch(_svc, single_intent):
        if single_intent.indicators[0] == "CPI":
            raise ProviderUnavailable("FRED circuit breaker OPEN — provider is down, skipping (resets in 60s)")
        return [NormalizedData(
            metadata=Metadata(source="FRED", indicator="Unemployment Rate", country="US",
                              frequency="monthly", unit="Percent", seriesId="UNRATE"),
            data=[{"date": "2020-01-01", "value": 3.5}],
        )]

    svc = SimpleNamespace(
        _detect_explicit_provider=lambda *_a, **_k: "",
        _normalize_provider_alias=lambda x: x or None,
        _select_routed_provider=AsyncMock(return_value="FRED"),
    )
    token = DF.MULTI_INDICATOR_FETCH_REPORTS.set([])
    original = DF.fetch_data
    try:
        DF.fetch_data = fetch
        data = asyncio.run(DF.fetch_multi_indicator_data(svc, intent))
        reports = DF.MULTI_INDICATOR_FETCH_REPORTS.get()
    finally:
        DF.fetch_data = original
        DF.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    assert [series.metadata.seriesId for series in data] == ["UNRATE"]
    assert reports and reports[-1]["unserved"] == [{"indicator": "CPI", "reason": "unavailable"}]
