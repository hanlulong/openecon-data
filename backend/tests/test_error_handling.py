"""Error handling, retry, fallback, and circuit breaker tests.

Covers critical error paths that were previously untested:
- HTTP 429 rate limiting → retry with backoff
- HTTP 5xx server errors → retry then fail gracefully
- HTTP 404/403 → immediate DataNotAvailableError (no retry)
- Connection timeouts → retry then DataNotAvailableError
- Circuit breaker open → instant fail without waiting
- Malformed JSON responses → DataNotAvailableError
- Provider fallback chains → next provider on failure
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pybreaker
import pytest

from backend.providers.base import BaseProvider, _get_breaker, _provider_breakers, _TransientHTTPError
from backend.utils.retry import DataNotAvailableError


# ─── Concrete test provider ─────────────────────────────────────────

class _TestProvider(BaseProvider):
    @property
    def provider_name(self) -> str:
        return "TEST"

    async def _fetch_data(self, **params):
        pass


def _fresh_provider() -> _TestProvider:
    """Create a provider with a fresh circuit breaker."""
    # Reset the TEST breaker so tests are isolated
    _provider_breakers.pop("TEST", None)
    # Also reset the rate_limiter state for TEST since cycle 28 added
    # rate_limiter integration in _get_with_retry.
    try:
        from backend.services.rate_limiter import get_global_rate_limiter
        limiter_mgr = get_global_rate_limiter()
        if "TEST" in limiter_mgr._limiters:
            del limiter_mgr._limiters["TEST"]
    except Exception:
        pass
    p = _TestProvider(timeout=5.0)
    return p


def _mock_response(status_code: int = 200, json_data=None, headers=None, text=""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ─── HTTP 429 Rate Limit Tests ───────────────────────────────────────

@pytest.mark.asyncio
async def test_429_rate_limit_raises_data_not_available():
    """HTTP 429 should retry and eventually raise DataNotAvailableError."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2  # Speed up test

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(429, headers={"Retry-After": "1"})

    with pytest.raises(DataNotAvailableError, match="retries"):
        await provider._get_with_retry(client, "https://api.example.com/data")


@pytest.mark.asyncio
async def test_429_sets_rate_limit_reset_time():
    """HTTP 429 with Retry-After header should record rate limit reset."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 1

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(429, headers={"Retry-After": "120"})

    with pytest.raises(DataNotAvailableError):
        await provider._get_with_retry(client, "https://api.example.com/data")

    assert provider.rate_limit_reset is not None
    assert provider.rate_limit_reset > datetime.now()


# ─── HTTP 5xx Server Error Tests ─────────────────────────────────────

@pytest.mark.asyncio
async def test_500_server_error_retries_then_fails():
    """HTTP 500 should be retried and then raise DataNotAvailableError."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(500, text="Internal Server Error")

    with pytest.raises(DataNotAvailableError, match="retries"):
        await provider._get_with_retry(client, "https://api.example.com/data")

    # Should have attempted MAX_RETRIES times
    assert client.get.call_count >= 2


@pytest.mark.asyncio
async def test_503_service_unavailable_retries():
    """HTTP 503 should be treated as transient and retried."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(503)

    with pytest.raises(DataNotAvailableError, match="retries"):
        await provider._get_with_retry(client, "https://api.example.com/data")


# ─── HTTP 404/403 Non-Retryable Tests ────────────────────────────────

@pytest.mark.asyncio
async def test_404_not_found_raises_immediately():
    """HTTP 404 should NOT be retried — immediate DataNotAvailableError."""
    provider = _fresh_provider()

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(404, text="Not Found")

    with pytest.raises(DataNotAvailableError, match="404"):
        await provider._get_with_retry(client, "https://api.example.com/data")

    # Should only be called ONCE (no retries for 404)
    assert client.get.call_count == 1


@pytest.mark.asyncio
async def test_403_forbidden_raises_immediately():
    """HTTP 403 should NOT be retried — immediate DataNotAvailableError."""
    provider = _fresh_provider()

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(403, text="Forbidden")

    with pytest.raises(DataNotAvailableError, match="403"):
        await provider._get_with_retry(client, "https://api.example.com/data")

    assert client.get.call_count == 1


# ─── Connection Error Tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_connection_error_retries_then_fails():
    """Connection errors should be retried then raise DataNotAvailableError."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(DataNotAvailableError, match="Connection failed"):
        await provider._get_with_retry(client, "https://api.example.com/data")


@pytest.mark.asyncio
async def test_timeout_retries_then_fails():
    """Timeouts should be retried then raise DataNotAvailableError."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = httpx.TimeoutException("Request timed out")

    with pytest.raises(DataNotAvailableError, match="Connection failed"):
        await provider._get_with_retry(client, "https://api.example.com/data")


# ─── Circuit Breaker Tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_consecutive_failures():
    """After fail_max consecutive failures, circuit opens and fails fast."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 1
    breaker = _get_breaker("TEST")
    assert breaker.current_state == "closed"

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = httpx.ConnectError("Down")

    # Trigger enough failures to open the breaker (fail_max=5)
    for _ in range(5):
        try:
            await provider._get_with_retry(client, "https://api.example.com/data")
        except DataNotAvailableError:
            pass

    # Circuit should now be open
    assert breaker.current_state == "open"


@pytest.mark.asyncio
async def test_circuit_breaker_open_fails_instantly():
    """When circuit is open, should fail immediately without making HTTP call."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 1
    breaker = _get_breaker("TEST")

    # Force circuit open by triggering fail_max failures
    fail_client = AsyncMock(spec=httpx.AsyncClient)
    fail_client.get.side_effect = httpx.ConnectError("Down")
    for _ in range(breaker.fail_max):
        try:
            await provider._get_with_retry(fail_client, "https://api.example.com/data")
        except DataNotAvailableError:
            pass

    assert breaker.current_state == "open"

    # Now a fresh call should fail immediately without making HTTP call
    fresh_client = AsyncMock(spec=httpx.AsyncClient)

    with pytest.raises(DataNotAvailableError, match="circuit breaker OPEN"):
        await provider._get_with_retry(fresh_client, "https://api.example.com/data")

    # HTTP client should NOT have been called
    fresh_client.get.assert_not_called()


# ─── Successful Response Tests ───────────────────────────────────────

@pytest.mark.asyncio
async def test_200_success_returns_response():
    """HTTP 200 should return the response directly."""
    provider = _fresh_provider()

    client = AsyncMock(spec=httpx.AsyncClient)
    expected_resp = _mock_response(200, json_data={"value": 42})
    client.get.return_value = expected_resp

    result = await provider._get_with_retry(client, "https://api.example.com/data")

    assert result.status_code == 200
    assert result.json() == {"value": 42}


@pytest.mark.asyncio
async def test_transient_failure_then_success():
    """A transient error followed by success should return the successful response."""
    provider = _fresh_provider()

    client = AsyncMock(spec=httpx.AsyncClient)
    success_resp = _mock_response(200, json_data={"ok": True})
    client.get.side_effect = [
        httpx.ConnectError("Temporary failure"),  # First attempt fails
        success_resp,  # Second attempt succeeds
    ]

    result = await provider._get_with_retry(client, "https://api.example.com/data")
    assert result.status_code == 200
    assert client.get.call_count == 2


# ─── JSON Parse Safety Tests ────────────────────────────────────────

def test_parse_json_safe_handles_valid_json():
    """Valid JSON response should be parsed correctly."""
    provider = _fresh_provider()
    resp = _mock_response(200, json_data={"gdp": 21000000000000})
    result = provider._parse_json_safe(resp)
    assert result == {"gdp": 21000000000000}


def test_parse_json_safe_raises_on_invalid_json():
    """Invalid JSON should raise DataNotAvailableError."""
    provider = _fresh_provider()
    resp = MagicMock(spec=httpx.Response)
    resp.json.side_effect = ValueError("Invalid JSON")

    with pytest.raises(DataNotAvailableError, match="parse"):
        provider._parse_json_safe(resp)


# ─── POST with Retry Tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_with_retry_500_retries():
    """POST requests should also retry on 5xx."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = _mock_response(500)

    with pytest.raises(DataNotAvailableError, match="retries"):
        await provider._post_with_retry(client, "https://api.example.com/data")


@pytest.mark.asyncio
async def test_post_with_retry_404_immediate_fail():
    """POST 404 should fail immediately."""
    provider = _fresh_provider()

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = _mock_response(404)

    with pytest.raises(DataNotAvailableError, match="404"):
        await provider._post_with_retry(client, "https://api.example.com/data")

    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_post_with_retry_429_retries_then_fails():
    """POST 429 should now retry + record rate-limit reset (parity with GET)."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = _mock_response(429, headers={"Retry-After": "1"})

    with pytest.raises(DataNotAvailableError, match="retries"):
        await provider._post_with_retry(client, "https://api.example.com/data")
    assert provider.rate_limit_reset is not None
    assert client.post.call_count == 2  # retried, not one-shot


@pytest.mark.asyncio
async def test_post_with_retry_raise_on_status_false_returns_4xx():
    """raise_on_status=False returns a non-2xx response for caller inspection."""
    provider = _fresh_provider()

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = _mock_response(404, text="not found")

    resp = await provider._post_with_retry(
        client, "https://api.example.com/data", raise_on_status=False
    )
    assert resp.status_code == 404  # returned, NOT raised
    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_get_with_retry_raise_on_status_false_returns_4xx():
    """The GET helper's opt-out mirrors POST: a 4xx is returned, not raised."""
    provider = _fresh_provider()

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _mock_response(404, text="not found")

    resp = await provider._get_with_retry(
        client, "https://api.example.com/data", raise_on_status=False
    )
    assert resp.status_code == 404
    assert client.get.call_count == 1


@pytest.mark.asyncio
async def test_post_with_retry_raise_on_status_false_still_retries_5xx():
    """raise_on_status=False still retries transient 5xx (only the terminal raise is suppressed)."""
    provider = _fresh_provider()
    provider.MAX_RETRIES = 2

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = _mock_response(503)

    with pytest.raises(DataNotAvailableError, match="retries"):
        await provider._post_with_retry(
            client, "https://api.example.com/data", raise_on_status=False
        )
    assert client.post.call_count == 2  # 5xx is transient regardless of raise_on_status


# ─── Provider Name Tests ────────────────────────────────────────────

def test_provider_name_used_for_circuit_breaker():
    """Each provider should have its own circuit breaker keyed by name."""
    _provider_breakers.pop("TEST", None)
    _provider_breakers.pop("OTHER", None)

    p1 = _fresh_provider()
    b1 = _get_breaker(p1.provider_name)

    class _OtherProvider(BaseProvider):
        @property
        def provider_name(self) -> str:
            return "OTHER"
        async def _fetch_data(self, **params):
            pass

    p2 = _OtherProvider()
    b2 = _get_breaker(p2.provider_name)

    assert b1 is not b2
    assert b1.name == "TEST"
    assert b2.name == "OTHER"

    # Cleanup
    _provider_breakers.pop("OTHER", None)


# ─── Country Code Normalization Tests ────────────────────────────────

def test_normalize_country_code_with_mapping():
    provider = _fresh_provider()
    mappings = {"UNITED_STATES": "US", "GERMANY": "DE"}
    assert provider._normalize_country_code("United States", mappings) == "US"
    assert provider._normalize_country_code("Germany", mappings) == "DE"


def test_normalize_country_code_fallback():
    provider = _fresh_provider()
    assert provider._normalize_country_code("Brazil", {}) == "BRAZIL"


def test_normalize_indicator_returns_none_for_unknown():
    provider = _fresh_provider()
    result = provider._normalize_indicator("nonexistent_indicator", {"GDP": "NY.GDP.MKTP.CD"})
    assert result is None


def test_normalize_indicator_finds_match():
    provider = _fresh_provider()
    result = provider._normalize_indicator("gdp", {"GDP": "NY.GDP.MKTP.CD"})
    assert result == "NY.GDP.MKTP.CD"
