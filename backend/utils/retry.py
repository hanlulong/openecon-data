"""Retry utility for API calls with exponential backoff."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, TypeVar, Any
from functools import wraps

import httpx


logger = logging.getLogger(__name__)

T = TypeVar('T')


class DataNotAvailableError(Exception):
    """Raised when requested data is not available from the provider."""
    pass


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
                    # Rate limiting - use longer delay with exponential backoff
                    # Check for Retry-After header
                    retry_after = exc.response.headers.get('Retry-After')
                    if retry_after and retry_after.isdigit():
                        # Use Retry-After value, but ensure minimum 5 second delay
                        delay = max(float(retry_after), 5.0)
                    else:
                        # Use longer delay for rate limits (start at 5 seconds)
                        delay = max(delay, 5.0)

                    logger.warning(
                        f"Rate limit hit (429). Attempt {attempt}/{max_attempts}. "
                        f"Retrying after {delay:.1f}s..."
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(delay)
                        delay *= backoff_factor  # Exponential backoff for next attempt
                        continue  # Skip the normal retry logic below
                    else:
                        raise DataNotAvailableError(
                            f"Rate limit exceeded after {max_attempts} attempts. Please try again later."
                        ) from exc

            if attempt < max_attempts:
                # Add jitter to avoid thundering herd problem
                actual_delay = delay + random.uniform(0, jitter) if jitter > 0 else delay
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
