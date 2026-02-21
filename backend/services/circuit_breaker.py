"""
Circuit Breaker Pattern Implementation

Prevents cascading failures by failing fast when services are unavailable.
Implements exponential backoff for recovery.

Benefits:
- Fail fast when providers are down (no wasted requests)
- Automatic recovery with exponential backoff
- Better error messages
- Improved system resilience
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable, TypeVar, Awaitable
from enum import Enum
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

T = TypeVar('T')


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Too many failures, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5  # Failures before opening
    recovery_timeout_seconds: int = 60  # Time before attempting recovery
    success_threshold: int = 2  # Successes in half-open before closing
    window_size_seconds: int = 300  # Time window for counting failures


class CircuitBreaker:
    """
    Circuit breaker for external API calls.

    Implements the circuit breaker pattern to prevent cascading failures:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests fail immediately
    - HALF_OPEN: Testing if service recovered, allowing limited requests

    Exponential backoff strategy:
    - Initial backoff: recovery_timeout
    - Max backoff: recovery_timeout * 8 (configurable)
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ):
        """
        Initialize circuit breaker.

        Args:
            name: Name for this breaker (e.g., "fred_api")
            config: Circuit breaker configuration
        """
        self.name = name
        self.config = config or CircuitBreakerConfig()

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[datetime] = None
        self._opened_at: Optional[datetime] = None
        self._window_start: Optional[datetime] = None
        self._lock = asyncio.Lock()  # Thread-safe state transitions

        logger.info(
            f"Circuit breaker '{name}' initialized: "
            f"failure_threshold={self.config.failure_threshold}, "
            f"recovery_timeout={self.config.recovery_timeout_seconds}s"
        )

    def _reset_window(self) -> None:
        """Reset failure window."""
        self._window_start = datetime.now(timezone.utc)
        self._failure_count = 0

    def _should_reset_window(self) -> bool:
        """Check if window should be reset."""
        if not self._window_start:
            return True
        elapsed = (datetime.now(timezone.utc) - self._window_start).total_seconds()
        return elapsed > self.config.window_size_seconds

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args,
        **kwargs
    ) -> T:
        """
        Execute function through circuit breaker.

        Thread-safe: uses asyncio.Lock to prevent race conditions.

        Args:
            func: Async function to call
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
        """
        # Check state with lock to prevent race conditions
        async with self._lock:
            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has elapsed
                if self._opened_at:
                    elapsed = (datetime.now(timezone.utc) - self._opened_at).total_seconds()
                    if elapsed >= self.config.recovery_timeout_seconds:
                        self._transition_to_half_open()
                        logger.info(f"Circuit breaker '{self.name}' transitioning to HALF_OPEN")
                    else:
                        raise CircuitBreakerOpenError(
                            f"Circuit breaker '{self.name}' is OPEN. "
                            f"Retrying in {self.config.recovery_timeout_seconds - elapsed:.0f}s"
                        )
                else:
                    raise CircuitBreakerOpenError(f"Circuit breaker '{self.name}' is OPEN")

        # Execute function outside lock to avoid holding lock during I/O
        try:
            result = await func(*args, **kwargs)

            # Record success with lock
            async with self._lock:
                self._on_success()
            return result

        except Exception as e:
            # Record failure with lock
            async with self._lock:
                self._on_failure()
            raise

    def _on_success(self) -> None:
        """Handle successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.config.success_threshold:
                self._transition_to_closed()
                logger.info(
                    f"Circuit breaker '{self.name}' recovered. "
                    f"Transitioning to CLOSED ({self._success_count} successes)"
                )
        elif self._state == CircuitState.CLOSED:
            # Reset failure window on success
            if self._should_reset_window():
                self._reset_window()
            # Always reset failure count in closed state
            self._failure_count = 0

    def _on_failure(self) -> None:
        """Handle failed call."""
        now = datetime.now(timezone.utc)
        self._last_failure_time = now

        # Reset window if timeout elapsed
        if self._should_reset_window():
            self._reset_window()

        self._failure_count += 1

        if self._state == CircuitState.CLOSED:
            if self._failure_count >= self.config.failure_threshold:
                self._transition_to_open()
                logger.warning(
                    f"Circuit breaker '{self.name}' OPEN. "
                    f"Failure threshold reached ({self._failure_count} failures). "
                    f"Rejecting requests for {self.config.recovery_timeout_seconds}s"
                )

        elif self._state == CircuitState.HALF_OPEN:
            self._transition_to_open()
            logger.warning(
                f"Circuit breaker '{self.name}' back to OPEN. "
                f"Service recovery failed."
            )

    def _transition_to_open(self) -> None:
        """Transition to open state."""
        self._state = CircuitState.OPEN
        self._opened_at = datetime.now(timezone.utc)
        self._success_count = 0

    def _transition_to_half_open(self) -> None:
        """Transition to half-open state."""
        self._state = CircuitState.HALF_OPEN
        self._success_count = 0

    def _transition_to_closed(self) -> None:
        """Transition to closed state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = None
        self._reset_window()

    def get_state(self) -> str:
        """Get current circuit state."""
        return self._state.value

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time.isoformat() if self._last_failure_time else None,
            "opened_at": self._opened_at.isoformat() if self._opened_at else None,
            "config": {
                "failure_threshold": self.config.failure_threshold,
                "recovery_timeout_seconds": self.config.recovery_timeout_seconds,
                "success_threshold": self.config.success_threshold,
            }
        }


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


class CircuitBreakerRegistry:
    """Registry of circuit breakers for multiple services."""

    _breakers: Dict[str, CircuitBreaker] = {}

    @classmethod
    def get_breaker(
        cls,
        name: str,
        config: Optional[CircuitBreakerConfig] = None
    ) -> CircuitBreaker:
        """Get or create circuit breaker."""
        if name not in cls._breakers:
            cls._breakers[name] = CircuitBreaker(name, config)
        return cls._breakers[name]

    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all breakers."""
        return {
            name: breaker.get_stats()
            for name, breaker in cls._breakers.items()
        }

    @classmethod
    def reset_all(cls) -> None:
        """Reset all circuit breakers."""
        for breaker in cls._breakers.values():
            breaker._transition_to_closed()
        logger.info(f"Reset {len(cls._breakers)} circuit breakers")


def get_circuit_breaker(name: str) -> CircuitBreaker:
    """Get or create a circuit breaker for a provider."""
    return CircuitBreakerRegistry.get_breaker(name)
