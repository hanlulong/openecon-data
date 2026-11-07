"""Base provider class with common HTTP retry and error handling logic."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone

import httpx
import pybreaker
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from ..models import NormalizedData
from ..services.http_pool import effective_timeout
from ..services.rate_limiter import (
    ProviderRateLimitWaitExceeded,
    acquire_provider,
    record_provider_rate_limit_error,
    record_provider_success,
)
from ..utils.retry import DataNotAvailableError, parse_retry_after

logger = logging.getLogger(__name__)

# Per-provider circuit breakers — shared across instances.
# When a provider fails 5 times consecutively, the circuit opens
# for 60 seconds, instantly failing subsequent calls instead of
# wasting time on timeouts.  This prevents cascading slowdowns
# when a provider API is down.
_provider_breakers: Dict[str, pybreaker.CircuitBreaker] = {}


def _is_client_request_error(exc: BaseException) -> bool:
    """True for deterministic 4xx responses (except 429).

    A 400/403/404 means *this request* was wrong (e.g. a nonexistent series id
    that indicator discovery probed) — not that the provider is down. Counting
    these toward the breaker lets a handful of long-tail discovery misses trip
    the breaker and black out every query to a healthy provider. Excluding them
    keeps the breaker measuring availability (transport errors, 5xx, and the
    429s we surface as _TransientHTTPError), not request validity.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return 400 <= status < 500 and status != 429
    return False


def _get_breaker(provider_name: str) -> pybreaker.CircuitBreaker:
    """Get or create a circuit breaker for a provider."""
    if provider_name not in _provider_breakers:
        _provider_breakers[provider_name] = pybreaker.CircuitBreaker(
            fail_max=5,
            reset_timeout=60,
            name=provider_name,
            exclude=[_is_client_request_error],
        )
    return _provider_breakers[provider_name]


class _TransientHTTPError(Exception):
    """Internal marker for HTTP errors that should trigger tenacity retry."""
    pass


class _RateLimitedHTTPError(Exception):
    """An actual 429 counts as an upstream failure but is not retried."""


class _RetryAdmissionRefused(Exception):
    """Stop retries without replacing the last genuine upstream outcome."""


class BaseProvider(ABC):
    """Base class for all data providers.

    Provides common functionality:
    - HTTP client management with timeout
    - Retry logic for transient failures
    - Common error handling and logging
    - Rate limiting awareness
    - Standardized provider identification

    All providers should inherit from this class and implement:
    - provider_name property (required)
    - _fetch_data method (abstract)
    """

    # Default timeout (seconds)
    DEFAULT_TIMEOUT = 30.0

    # Retry configuration
    MAX_RETRIES = 3
    RETRY_BACKOFF_FACTOR = 1.0

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        """Initialize base provider.

        Args:
            timeout: Request timeout in seconds
        """
        self.timeout = timeout
        self.last_request_time = None
        self.rate_limit_reset = None

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the canonical provider name (e.g., 'FRED', 'WorldBank', 'IMF').

        This is used for logging, routing, and metadata.
        """
        pass

    @abstractmethod
    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Fetch data from provider API. Must be implemented by subclasses.

        Args:
            **params: Provider-specific parameters

        Returns:
            Normalized data or list of normalized data
        """
        pass

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        raise_on_status: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Get request with automatic retry on transient failures (powered by tenacity).

        Retries on: HTTP 5xx (server error),
        connection errors, timeouts.
        HTTP 429 establishes a shared cooldown and fails promptly.
        Does NOT retry on: HTTP 404/403 (not found/forbidden), other client errors.

        Args:
            client: httpx AsyncClient
            url: Request URL
            raise_on_status: when True (default) call response.raise_for_status()
                so a 4xx surfaces as DataNotAvailableError. Set False for call
                sites that must INSPECT a non-2xx response to drive their own
                recovery (e.g. BIS retries without params on non-200, Eurostat's
                404/413 ladder) — they still get rate-limit gating, 429 cooldown/5xx
                transient retry, and circuit-breaker protection, just not the
                terminal raise.
            **kwargs: Additional httpx parameters

        Returns:
            HTTP response

        Raises:
            DataNotAvailableError: If all retries fail
        """
        req_timeout = effective_timeout(kwargs.pop("timeout", self.timeout))
        provider_key = (self.provider_name or "").upper()
        attempts = 0
        last_failure = None
        retry_refused = False
        cancellation = None

        @retry(
            stop=stop_after_attempt(self.MAX_RETRIES),
            wait=wait_exponential(multiplier=self.RETRY_BACKOFF_FACTOR, min=1, max=30),
            retry=retry_if_exception_type((
                httpx.ConnectError, httpx.TimeoutException, _TransientHTTPError,
            )),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        async def _do_get():
            nonlocal attempts, last_failure, retry_refused
            if attempts and provider_key:
                try:
                    await acquire_provider(provider_key)
                except ProviderRateLimitWaitExceeded as exc:
                    retry_refused = True
                    raise _RetryAdmissionRefused() from exc
            attempts += 1
            try:
                response = await client.get(url, **kwargs, timeout=req_timeout)
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    try:
                        self.rate_limit_reset = datetime.now() + timedelta(seconds=retry_after)
                    except OverflowError:
                        self.rate_limit_reset = datetime.max
                    if provider_key:
                        record_provider_rate_limit_error(provider_key, retry_after)
                    raise _RateLimitedHTTPError("Rate limited (429); upstream cooldown is active")
                if response.status_code >= 500:
                    raise _TransientHTTPError(f"Server error ({response.status_code})")
                if raise_on_status:
                    response.raise_for_status()
                if provider_key:
                    record_provider_success(provider_key)
                return response
            except (httpx.ConnectError, httpx.TimeoutException, _TransientHTTPError) as exc:
                last_failure = exc
                raise

        breaker = _get_breaker(self.provider_name)
        try:
            # Avoid reserving quota for an already-open availability breaker.
            # The calling() context remains authoritative if it opens while waiting.
            if breaker.current_state == pybreaker.STATE_OPEN:
                opened_at = breaker._state_storage.opened_at
                if opened_at and datetime.now(timezone.utc) < opened_at + timedelta(seconds=breaker.reset_timeout):
                    raise pybreaker.CircuitBreakerError("Availability cooldown is active")
            if provider_key:
                # Zero-HTTP local refusals never enter pybreaker: excluded errors
                # would incorrectly reset its counters or close half-open state.
                await acquire_provider(provider_key)
            # Keep one outcome for the entire retry operation, not each attempt.
            with breaker.calling():
                try:
                    return await _do_get()
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    raise
                except _RetryAdmissionRefused:
                    assert last_failure is not None
                    raise last_failure
        except ProviderRateLimitWaitExceeded as exc:
            raise DataNotAvailableError(str(exc)) from exc
        except pybreaker.CircuitBreakerError:
            if cancellation is not None:
                # pybreaker may wrap cancellation when it opens the circuit.
                raise cancellation
            raise DataNotAvailableError(
                f"{self.provider_name} circuit breaker OPEN — provider is down, skipping (resets in 60s)"
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (404, 403):
                raise DataNotAvailableError(f"API returned {status}: {exc.response.text[:200]}") from exc
            raise DataNotAvailableError(str(exc)) from exc
        except _RateLimitedHTTPError as exc:
            raise DataNotAvailableError(str(exc)) from exc
        except _TransientHTTPError as exc:
            detail = "; further retries blocked by local rate limit" if retry_refused else "; retries exhausted"
            raise DataNotAvailableError(f"Failed after {attempts} HTTP attempts{detail}: {exc}") from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            detail = "; further retries blocked by local rate limit" if retry_refused else "; retries exhausted"
            raise DataNotAvailableError(f"Connection failed after {attempts} HTTP attempts{detail}: {exc}") from exc
        except Exception as exc:
            raise DataNotAvailableError(f"Request failed: {exc}") from exc

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        raise_on_status: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Post request with retry, rate-limiting, and circuit-breaker protection.

        Full parity with _get_with_retry (it previously had retry + timeout only,
        missing the rate-limiter pre-flight, 429/Retry-After handling, success
        recording, and the pybreaker wrap — so the 12 POST call sites, 11 of them
        StatsCan, were entirely unprotected). The only difference from the GET
        helper is client.post vs client.get.

        Args:
            client: httpx AsyncClient
            url: Request URL
            raise_on_status: see _get_with_retry — set False to inspect a non-2xx
                response while keeping rate-limit/429/5xx/breaker protection.
            **kwargs: Additional httpx parameters (timeout=, json=, headers= …)

        Returns:
            HTTP response

        Raises:
            DataNotAvailableError: If all retries fail
        """
        req_timeout = effective_timeout(kwargs.pop("timeout", self.timeout))
        provider_key = (self.provider_name or "").upper()
        attempts = 0
        last_failure = None
        retry_refused = False
        cancellation = None

        @retry(
            stop=stop_after_attempt(self.MAX_RETRIES),
            wait=wait_exponential(multiplier=self.RETRY_BACKOFF_FACTOR, min=1, max=30),
            retry=retry_if_exception_type((
                httpx.ConnectError, httpx.TimeoutException, _TransientHTTPError,
            )),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        async def _do_post():
            nonlocal attempts, last_failure, retry_refused
            if attempts and provider_key:
                try:
                    await acquire_provider(provider_key)
                except ProviderRateLimitWaitExceeded as exc:
                    retry_refused = True
                    raise _RetryAdmissionRefused() from exc
            attempts += 1
            try:
                response = await client.post(url, **kwargs, timeout=req_timeout)
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    try:
                        self.rate_limit_reset = datetime.now() + timedelta(seconds=retry_after)
                    except OverflowError:
                        self.rate_limit_reset = datetime.max
                    if provider_key:
                        record_provider_rate_limit_error(provider_key, retry_after)
                    raise _RateLimitedHTTPError("Rate limited (429); upstream cooldown is active")
                if response.status_code >= 500:
                    raise _TransientHTTPError(f"Server error ({response.status_code})")
                if raise_on_status:
                    response.raise_for_status()
                if provider_key:
                    record_provider_success(provider_key)
                return response
            except (httpx.ConnectError, httpx.TimeoutException, _TransientHTTPError) as exc:
                last_failure = exc
                raise

        breaker = _get_breaker(self.provider_name)
        try:
            # Avoid reserving quota for an already-open availability breaker.
            # The calling() context remains authoritative if it opens while waiting.
            if breaker.current_state == pybreaker.STATE_OPEN:
                opened_at = breaker._state_storage.opened_at
                if opened_at and datetime.now(timezone.utc) < opened_at + timedelta(seconds=breaker.reset_timeout):
                    raise pybreaker.CircuitBreakerError("Availability cooldown is active")
            if provider_key:
                # Zero-HTTP local refusals never enter pybreaker: excluded errors
                # would incorrectly reset its counters or close half-open state.
                await acquire_provider(provider_key)
            # Keep one outcome for the entire retry operation, not each attempt.
            with breaker.calling():
                try:
                    return await _do_post()
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    raise
                except _RetryAdmissionRefused:
                    assert last_failure is not None
                    raise last_failure
        except ProviderRateLimitWaitExceeded as exc:
            raise DataNotAvailableError(str(exc)) from exc
        except pybreaker.CircuitBreakerError:
            if cancellation is not None:
                # pybreaker may wrap cancellation when it opens the circuit.
                raise cancellation
            raise DataNotAvailableError(
                f"{self.provider_name} circuit breaker OPEN — provider is down, skipping (resets in 60s)"
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (404, 403):
                raise DataNotAvailableError(f"API returned {status}: {exc.response.text[:200]}") from exc
            raise DataNotAvailableError(str(exc)) from exc
        except _RateLimitedHTTPError as exc:
            raise DataNotAvailableError(str(exc)) from exc
        except _TransientHTTPError as exc:
            detail = "; further retries blocked by local rate limit" if retry_refused else "; retries exhausted"
            raise DataNotAvailableError(f"Failed after {attempts} HTTP attempts{detail}: {exc}") from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            detail = "; further retries blocked by local rate limit" if retry_refused else "; retries exhausted"
            raise DataNotAvailableError(f"Connection failed after {attempts} HTTP attempts{detail}: {exc}") from exc
        except Exception as exc:
            raise DataNotAvailableError(f"Request failed: {exc}") from exc

    @staticmethod
    def _normalize_country_code(country: str, mappings: Dict[str, str]) -> str:
        """Normalize country code using provided mappings.

        Args:
            country: Country name or code
            mappings: Dictionary mapping various formats to standard code

        Returns:
            Normalized country code
        """
        key = country.upper().replace(" ", "_")
        return mappings.get(key, country.upper())

    @staticmethod
    def _normalize_indicator(indicator: str, mappings: Dict[str, str]) -> Optional[str]:
        """Normalize indicator using provided mappings.

        Args:
            indicator: Indicator name or code
            mappings: Dictionary mapping indicator names to codes

        Returns:
            Normalized indicator code or None if not found
        """
        if not indicator:
            return None
        key = indicator.upper().replace(" ", "_")
        return mappings.get(key)

    @staticmethod
    def _is_rate_limited() -> bool:
        """Check if provider is currently rate limited."""
        # Can be overridden by subclasses
        return False

    @staticmethod
    def _parse_json_safe(response: httpx.Response) -> Dict[str, Any]:
        """Safely parse JSON response with error handling.

        Args:
            response: HTTP response

        Returns:
            Parsed JSON dictionary

        Raises:
            DataNotAvailableError: If JSON parsing fails
        """
        try:
            return response.json()
        except Exception as e:
            raise DataNotAvailableError(f"Failed to parse response: {str(e)}")
