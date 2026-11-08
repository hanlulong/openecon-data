"""Retry utility for API calls with exponential backoff."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, Optional, TypeVar, Any
from functools import wraps

import httpx

from .request_budget import MIN_RETRY_ATTEMPT_SECONDS, budget_allows


logger = logging.getLogger(__name__)

T = TypeVar('T')


class DataNotAvailableError(Exception):
    """Raised when requested data is not available from the provider."""
    pass


class ProviderUnavailable(DataNotAvailableError):
    """The provider gave NO verdict on the request: it could not be reached.

    Raised by the shared provider request layer for exhausted 5xx retries,
    connection/timeout/transport errors, an open availability circuit breaker
    and a local admission refusal (plus ProviderCoolingDown below). Unlike a
    plain DataNotAvailableError (404, empty payload, invalid request), it says
    nothing about whether the series exists or has data, so consumers that
    treat a failed attempt as a fetch-validated dead end (the no-data menu's
    attempted-code exclusion, the multi-indicator failure reason) must not.
    Subclassing DataNotAvailableError keeps every existing handler and
    cross-provider fallback behaving unchanged.
    """

    def with_context(self, context: str) -> "ProviderUnavailable":
        """Return a copy of the SAME class whose message is prefixed with
        caller context (a re-wrap that must not demote it to a verdict)."""
        return type(self)(f"{context}: {self}")


class ProviderCoolingDown(ProviderUnavailable):
    """The provider refused (HTTP 429) or our shared limiter is in its cooldown.

    Every further request to the same provider would be refused locally until
    the cooldown expires, so fan-out loops should stop at the first one instead
    of queueing more zero-HTTP refusals. A cooldown is a transient refusal, so
    it is a ProviderUnavailable (and therefore still a DataNotAvailableError).
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

    def with_context(self, context: str) -> "ProviderCoolingDown":
        """Return a copy whose message is prefixed with caller context."""
        return ProviderCoolingDown(
            f"{context}: {self}", retry_after_seconds=self.retry_after_seconds
        )


def is_provider_unavailable(exc: BaseException | None) -> bool:
    """True when ``exc`` is a no-verdict provider failure (typed, never text).

    A plain DataNotAvailableError (or None, a clean empty attempt) is a
    verdict about the request; ProviderUnavailable and its subclasses are not.
    """
    return isinstance(exc, ProviderUnavailable)


class RequestBudgetExhausted(ProviderUnavailable):
    """We ran out of REQUEST time before the provider could be asked.

    Raised (with zero I/O) when a new provider operation would start inside
    the request deadline's answer reserve, or when a fetch-level budget was
    bound by the request deadline rather than by the provider. It is a
    no-verdict (a ProviderUnavailable, so every handler and fallback keeps
    working), it is NEVER recorded in the availability breaker, and it is NOT
    a claim that the provider is down. ``with_context`` is inherited and
    keeps this class.
    """


def is_no_verdict(exc: BaseException | None) -> bool:
    """True when ``exc`` says nothing about whether the requested data exists.

    Typed only: a ProviderUnavailable (and subclasses), or a raw transport /
    timeout exception that escaped provider wrapping. ``is_provider_unavailable``
    stays the narrower typed-provider check (a bare TimeoutError is not a
    provider verdict either way, but it is not a ProviderUnavailable).
    """
    return is_provider_unavailable(exc) or isinstance(
        exc, (httpx.TransportError, asyncio.TimeoutError)
    )


def variant_retry_allowed(last_error: BaseException | None) -> bool:
    """The single gate for every same-provider VARIANT re-probe.

    A variant re-probe exists to recover from a VERDICT (empty payload, 4xx,
    no rows): the alternate-code re-adjudication, the unwindowed window probe
    and broadened-window retry, the Comtrade M,X flow envelope and historical
    window, the Eurostat sparse-history retry, StatsCan dynamic->vector, the
    Comtrade adjudicated-HS retry, semantic recovery after a fetch error, and
    the World Bank fallback ladder. A no-verdict says nothing about the
    series, so the re-probe cannot help and only burns the request deadline
    against a provider that is not answering. ``None`` (a clean empty) and a
    plain DataNotAvailableError keep every existing re-probe.
    """
    return not is_no_verdict(last_error)


def aggregate_failure(
    outcomes: Iterable[Any],
    verdict: Optional[BaseException],
    *,
    context: str,
) -> Optional[BaseException]:
    """Combine one outcome per sub-request into the error a fan-out raises.

    ``outcomes``: an exception is a failed sub-request; anything else (None,
    data, ``[]``, a NormalizedData) means the provider ANSWERED it. When there
    is at least one outcome and EVERY one is a no-verdict, the provider never
    answered, so a typed no-verdict is returned instead of ``verdict``: the
    first ProviderCoolingDown (context-prefixed), else the first
    ProviderUnavailable (context-prefixed), else a ProviderUnavailable wrapping
    the first raw transport/timeout error. Otherwise ``verdict`` (possibly
    None) is returned unchanged.

    Deliberately conservative: mixed outcomes keep today's plain verdict.
    """
    items = list(outcomes)
    if not items or not all(
        isinstance(item, BaseException) and is_no_verdict(item) for item in items
    ):
        return verdict
    for item in items:
        if isinstance(item, ProviderCoolingDown):
            return item.with_context(context)
    for item in items:
        if isinstance(item, ProviderUnavailable):
            return item.with_context(context)
    raw = items[0]
    wrapped = ProviderUnavailable(f"{context}: {type(raw).__name__}: {raw}")
    wrapped.__cause__ = raw
    return wrapped


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float:
    """Parse Retry-After without exposing raw header text or overflowing dates.

    Unrepresentably large integer delays saturate at the last UTC second of
    year 9999. Missing or malformed headers retain the existing 60s default.
    """
    now = now or datetime.now(timezone.utc)
    max_delay = max(0.0, 253402300799.0 - now.timestamp())
    if not isinstance(value, str):
        return 60.0
    value = value.strip()
    if value.isascii() and value.isdigit():
        # Bound before integer conversion, including Python's digit-count limit.
        digits = value.lstrip("0") or "0"
        if len(digits) > 12:
            return max_delay
        return min(float(digits), max_delay)
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return min(max(0.0, deadline.timestamp() - now.timestamp()), max_delay)
    except (TypeError, ValueError, OverflowError, OSError):
        return 60.0


async def retry_async(
    func: Callable[..., T],
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.0,
    exceptions: tuple = (httpx.HTTPError, asyncio.TimeoutError),
) -> T:
    """
    Retry an async function with exponential backoff and optional jitter.

    Args:
        func: The async function to retry
        max_attempts: Maximum number of attempts (default: 3)
        initial_delay: Initial delay in seconds (default: 1.0)
        backoff_factor: Multiplier for delay between retries (default: 2.0)
        jitter: Random jitter range [0, jitter] added to delay to avoid thundering herd (default: 0.0)
        exceptions: Tuple of exceptions to catch and retry

    Returns:
        The result of the function call

    Raises:
        The last exception if all retries fail
    """
    delay = initial_delay
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except exceptions as exc:
            last_exception = exc

            # Check if it's a 404 or data not found - don't retry these
            if isinstance(exc, httpx.HTTPStatusError):
                error_text = exc.response.text

                # Detect HTML error pages and provide cleaner error messages
                if error_text.strip().startswith(('<?xml', '<!DOCTYPE', '<html')):
                    # This is an HTML/XML error page, not JSON
                    if exc.response.status_code == 404:
                        error_msg = "Data not found for the requested country/indicator combination"
                    elif exc.response.status_code == 400:
                        error_msg = "Invalid request - the API rejected this query. This may be due to rate limiting, an invalid country code, or temporarily unavailable data"
                    elif exc.response.status_code == 422:
                        error_msg = "Invalid request parameters"
                    elif exc.response.status_code == 429:
                        error_msg = "Rate limit exceeded - too many requests"
                    else:
                        error_msg = f"API request failed with status {exc.response.status_code}"
                else:
                    # Use the actual error text if it's not HTML
                    error_msg = error_text

                if exc.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"Data not available: {error_msg}"
                    ) from exc
                elif exc.response.status_code in (400, 422):
                    # Bad request - don't retry
                    raise DataNotAvailableError(
                        f"Invalid request: {error_msg}"
                    ) from exc
                elif exc.response.status_code == 429:
                    # Terminal: sleeping out an upstream Retry-After here would
                    # hold the request for an unbounded time (the header is
                    # provider-controlled) and retrying would spend quota into
                    # the cooldown. Shared cooldown policy lives in the
                    # provider admission path (BaseProvider + rate_limiter).
                    retry_after = parse_retry_after(exc.response.headers.get('Retry-After'))
                    logger.warning(
                        "Rate limit hit (429) on attempt %d/%d; not retrying "
                        "(Retry-After %.0fs)", attempt, max_attempts, retry_after,
                    )
                    raise ProviderCoolingDown(
                        "Rate limit exceeded (429). Please try again later.",
                        retry_after_seconds=retry_after,
                    ) from exc

            if attempt < max_attempts:
                # Add jitter to avoid thundering herd problem
                actual_delay = delay + random.uniform(0, jitter) if jitter > 0 else delay
                if not budget_allows(actual_delay + MIN_RETRY_ATTEMPT_SECONDS):
                    # The request deadline cannot fit the sleep plus a useful
                    # attempt: surface this failure now (deflation only).
                    logger.warning(
                        "Attempt %d/%d failed: %s. Not retrying: the request "
                        "deadline cannot fit another attempt", attempt, max_attempts, exc,
                    )
                    raise
                logger.warning(
                    f"Attempt {attempt}/{max_attempts} failed: {exc}. "
                    f"Retrying in {actual_delay:.1f}s..."
                )
                await asyncio.sleep(actual_delay)
                delay *= backoff_factor
            else:
                logger.error(
                    f"All {max_attempts} attempts failed. Last error: {exc}"
                )

    # If we get here, all retries failed
    raise last_exception  # type: ignore


def with_retry(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
):
    """
    Decorator to add retry logic to async functions.

    Usage:
        @with_retry(max_attempts=3)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            async def _call():
                return await func(*args, **kwargs)

            return await retry_async(
                _call,
                max_attempts=max_attempts,
                initial_delay=initial_delay,
                backoff_factor=backoff_factor,
            )

        return wrapper
    return decorator
