"""Rate limiter for provider-specific API quotas.

This module prevents rate limit errors by tracking request counts and enforcing
delays between requests to stay within provider limits.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class RateLimiterConfig:
    """Configuration for a provider's rate limits."""

    def __init__(
        self,
        name: str,
        min_delay_seconds: float = 0.5,
        max_requests_per_minute: Optional[int] = None,
        max_requests_per_hour: Optional[int] = None,
    ):
        """
        Args:
            name: Provider name (e.g., "OECD", "FRED")
            min_delay_seconds: Minimum delay between requests in seconds
            max_requests_per_minute: Max requests allowed in 60-second window (or None for unlimited)
            max_requests_per_hour: Max requests allowed in 3600-second window (or None for unlimited)
        """
        self.name = name
        self.min_delay_seconds = min_delay_seconds
        self.max_requests_per_minute = max_requests_per_minute
        self.max_requests_per_hour = max_requests_per_hour


class ProviderRateLimiter:
    """Tracks rate limit for a single provider."""

    def __init__(self, config: RateLimiterConfig):
        self.config = config
        self.last_request_time: Optional[float] = None

        # Track request timestamps in sliding windows
        # These deques store timestamps (seconds since epoch) of recent requests
        self.minute_window: deque = deque(maxlen=config.max_requests_per_minute or 1000)
        self.hour_window: deque = deque(maxlen=config.max_requests_per_hour or 10000)

        # Circuit breaker state - tracks when we hit rate limits
        self._circuit_open_until: Optional[float] = None  # Timestamp when circuit breaker closes
        self._consecutive_429_count: int = 0  # Number of consecutive 429 errors

    def _cleanup_windows(self, current_time: float) -> None:
        """Remove timestamps outside sliding windows."""
        minute_cutoff = current_time - 60  # 1 minute ago
        hour_cutoff = current_time - 3600  # 1 hour ago

        # Clean minute window
        while self.minute_window and self.minute_window[0] < minute_cutoff:
            self.minute_window.popleft()

        # Clean hour window
        while self.hour_window and self.hour_window[0] < hour_cutoff:
            self.hour_window.popleft()

    def get_delay_until_ready(self) -> float:
        """
        Get the number of seconds to wait before making the next request.

        Returns:
            Delay in seconds (0 if ready now)
        """
        now = time.time()
        self._cleanup_windows(now)

        delays = []

        # Check minimum delay between requests
        if self.last_request_time is not None:
            time_since_last = now - self.last_request_time
            if time_since_last < self.config.min_delay_seconds:
                delays.append(self.config.min_delay_seconds - time_since_last)

        # Check per-minute limit
        if self.config.max_requests_per_minute is not None:
            if len(self.minute_window) >= self.config.max_requests_per_minute:
                # Window is full, wait until oldest request expires
                oldest = self.minute_window[0]
                wait_time = (oldest + 60) - now
                if wait_time > 0:
                    delays.append(wait_time)

        # Check per-hour limit
        if self.config.max_requests_per_hour is not None:
            if len(self.hour_window) >= self.config.max_requests_per_hour:
                # Window is full, wait until oldest request expires
                oldest = self.hour_window[0]
                wait_time = (oldest + 3600) - now
                if wait_time > 0:
                    delays.append(wait_time)

        # Return maximum delay needed
        return max(delays) if delays else 0

    async def wait_until_ready(self) -> float:
        """
        Wait until the next request can be made.

        Returns:
            Delay that was applied (in seconds)
        """
        import asyncio

        delay = self.get_delay_until_ready()
        if delay > 0:
            logger.info(
                f"🚦 {self.config.name} rate limit: waiting {delay:.1f}s before next request "
                f"(minute: {len(self.minute_window)}/{self.config.max_requests_per_minute}, "
                f"hour: {len(self.hour_window)}/{self.config.max_requests_per_hour})"
            )
            await asyncio.sleep(delay)

        return delay

    def record_request(self) -> None:
        """Record that a request was just made."""
        now = time.time()
        self.last_request_time = now

        if self.config.max_requests_per_minute is not None:
            self.minute_window.append(now)

        if self.config.max_requests_per_hour is not None:
            self.hour_window.append(now)

    def record_rate_limit_error(self) -> None:
        """Record that a 429 rate limit error was received.

        Opens the circuit breaker to prevent further requests until cooldown expires.
        """
        self._consecutive_429_count += 1
        now = time.time()

        # Calculate cooldown duration based on consecutive errors
        # 1st error: 60s, 2nd: 120s, 3rd: 300s (5min), 4th+: 600s (10min)
        cooldown_durations = [60, 120, 300, 600]
        cooldown_index = min(self._consecutive_429_count - 1, len(cooldown_durations) - 1)
        cooldown = cooldown_durations[cooldown_index]

        self._circuit_open_until = now + cooldown
        logger.warning(
            f"🚫 {self.config.name} circuit breaker OPEN: "
            f"Rate limit hit {self._consecutive_429_count} time(s). "
            f"Cooldown: {cooldown}s until {datetime.fromtimestamp(self._circuit_open_until).strftime('%H:%M:%S')}"
        )

    def record_success(self) -> None:
        """Record a successful request - resets circuit breaker."""
        if self._consecutive_429_count > 0:
            logger.info(f"✅ {self.config.name} circuit breaker CLOSED: Request succeeded after rate limiting")
        self._consecutive_429_count = 0
        self._circuit_open_until = None

    def is_circuit_open(self) -> bool:
        """Check if circuit breaker is open (provider should be skipped).

        Returns:
            True if circuit is open and provider should be skipped
        """
        if self._circuit_open_until is None:
            return False

        now = time.time()
        if now >= self._circuit_open_until:
            # Cooldown expired, close circuit and allow retry
            logger.info(f"⚡ {self.config.name} circuit breaker cooldown expired, allowing retry")
            self._circuit_open_until = None
            return False

        remaining = self._circuit_open_until - now
        logger.debug(f"🚫 {self.config.name} circuit open, {remaining:.0f}s remaining")
        return True

    def get_circuit_status(self) -> dict:
        """Get detailed circuit breaker status for debugging."""
        now = time.time()
        remaining = max(0, (self._circuit_open_until or now) - now)
        return {
            "name": self.config.name,
            "circuit_open": self.is_circuit_open(),
            "consecutive_429_count": self._consecutive_429_count,
            "cooldown_remaining_seconds": remaining if self._circuit_open_until else 0,
            "requests_in_minute": len(self.minute_window),
            "requests_in_hour": len(self.hour_window),
        }


class GlobalRateLimiter:
    """Manages rate limiters for all providers."""

    # Default rate limit configs for known providers
    # Based on observed API behavior and documentation
    DEFAULT_CONFIGS: Dict[str, RateLimiterConfig] = {
        "OECD": RateLimiterConfig(
            name="OECD",
            min_delay_seconds=3.0,  # 3 second minimum between OECD requests (OECD allows 20/minute = 3s per request)
            max_requests_per_minute=18,  # OECD official limit is 20/minute (using 18 for safety margin)
            max_requests_per_hour=50,  # OECD actual limit is 60/hour - use 50 for safety margin
        ),
        "STATSCAN": RateLimiterConfig(
            name="STATSCAN",
            min_delay_seconds=0.5,  # 500ms minimum between requests
            max_requests_per_minute=60,
            max_requests_per_hour=1000,
        ),
        "WORLDBANK": RateLimiterConfig(
            name="WORLDBANK",
            min_delay_seconds=0.2,
            max_requests_per_minute=100,
            max_requests_per_hour=2000,
        ),
        "FRED": RateLimiterConfig(
            name="FRED",
            min_delay_seconds=0.1,
            max_requests_per_minute=200,
            max_requests_per_hour=5000,
        ),
        "COMTRADE": RateLimiterConfig(
            name="COMTRADE",
            min_delay_seconds=0.5,
            max_requests_per_minute=60,
            max_requests_per_hour=1000,
        ),
        "IMF": RateLimiterConfig(
            name="IMF",
            min_delay_seconds=0.2,
            max_requests_per_minute=100,
            max_requests_per_hour=2000,
        ),
        "BIS": RateLimiterConfig(
            name="BIS",
            min_delay_seconds=0.2,
            max_requests_per_minute=100,
            max_requests_per_hour=2000,
        ),
        "EUROSTAT": RateLimiterConfig(
            name="EUROSTAT",
            min_delay_seconds=0.2,
            max_requests_per_minute=100,
            max_requests_per_hour=2000,
        ),
        "EXCHANGERATE": RateLimiterConfig(
            name="EXCHANGERATE",
            min_delay_seconds=0.1,
            max_requests_per_minute=300,
            max_requests_per_hour=10000,
        ),
        "COINGECKO": RateLimiterConfig(
            name="COINGECKO",
            min_delay_seconds=0.1,
            max_requests_per_minute=300,
            max_requests_per_hour=10000,
        ),
    }

    def __init__(self):
        self._limiters: Dict[str, ProviderRateLimiter] = {}

        # Initialize with default configs
        for name, config in self.DEFAULT_CONFIGS.items():
            self._limiters[name] = ProviderRateLimiter(config)

    def get_limiter(self, provider: str) -> ProviderRateLimiter:
        """Get or create a rate limiter for a provider."""
        provider_upper = provider.upper()

        if provider_upper not in self._limiters:
            # Create default limiter for unknown providers
            config = RateLimiterConfig(
                name=provider_upper,
                min_delay_seconds=0.1,  # Minimal default
            )
            self._limiters[provider_upper] = ProviderRateLimiter(config)

        return self._limiters[provider_upper]

    def set_config(self, provider: str, config: RateLimiterConfig) -> None:
        """Override rate limit config for a provider."""
        provider_upper = provider.upper()
        self._limiters[provider_upper] = ProviderRateLimiter(config)
        logger.info(
            f"Updated rate limit config for {provider_upper}: "
            f"min_delay={config.min_delay_seconds}s, "
            f"per_minute={config.max_requests_per_minute}, "
            f"per_hour={config.max_requests_per_hour}"
        )


# Global instance
_global_rate_limiter: Optional[GlobalRateLimiter] = None


def get_global_rate_limiter() -> GlobalRateLimiter:
    """Get the global rate limiter instance."""
    global _global_rate_limiter
    if _global_rate_limiter is None:
        _global_rate_limiter = GlobalRateLimiter()
    return _global_rate_limiter


async def wait_for_provider(provider: str) -> float:
    """
    Wait until it's safe to make a request to a provider.

    Args:
        provider: Provider name (e.g., "OECD")

    Returns:
        Delay that was applied in seconds
    """
    limiter = get_global_rate_limiter().get_limiter(provider)
    return await limiter.wait_until_ready()


def record_provider_request(provider: str) -> None:
    """Record that a request was made to a provider."""
    limiter = get_global_rate_limiter().get_limiter(provider)
    limiter.record_request()


def record_provider_rate_limit_error(provider: str) -> None:
    """Record that a 429 rate limit error was received from a provider."""
    limiter = get_global_rate_limiter().get_limiter(provider)
    limiter.record_rate_limit_error()


def record_provider_success(provider: str) -> None:
    """Record a successful request to a provider."""
    limiter = get_global_rate_limiter().get_limiter(provider)
    limiter.record_success()


def is_provider_circuit_open(provider: str) -> bool:
    """Check if a provider's circuit breaker is open (should skip).

    Args:
        provider: Provider name (e.g., "OECD")

    Returns:
        True if provider should be skipped due to rate limiting
    """
    limiter = get_global_rate_limiter().get_limiter(provider)
    return limiter.is_circuit_open()


def get_provider_circuit_status(provider: str) -> dict:
    """Get detailed circuit breaker status for a provider."""
    limiter = get_global_rate_limiter().get_limiter(provider)
    return limiter.get_circuit_status()
