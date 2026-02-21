from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional

from ..models import ProcessingStep


_processing_tracker_var: ContextVar[Optional["ProcessingTracker"]] = ContextVar(
    "processing_tracker", default=None
)


# Standard processing step descriptions with emojis for user-friendly display
STEP_DESCRIPTIONS = {
    "parsing_query": "🔍 Understanding your question...",
    "searching_metadata": "📚 Searching for matching indicators...",
    "selecting_provider": "🎯 Selecting best data source...",
    "fetching_data": "📊 Retrieving data...",
    "validating_data": "✅ Validating data quality...",
    "normalizing_data": "📐 Normalizing data format...",
    "applying_fallback": "🔄 Trying alternative data source...",
    "finalizing_response": "✨ Preparing your results...",
    # Deep Agent steps
    "analyzing_complexity": "🧠 Analyzing query complexity...",
    "expanding_regions": "🌍 Expanding regions to countries...",
    "parallel_fetch": "⚡ Fetching data in parallel...",
    "merging_results": "🔗 Combining results...",
    # Error states
    "error": "❌ An error occurred",
    "timeout": "⏱️ Request timed out",
    "rate_limited": "🚦 Rate limit reached",
}


class ProcessingTracker:
    """Collects processing steps for a single query cycle.

    Provides real-time feedback to users about query processing progress,
    including data fetching, fallback attempts, and error handling.
    """

    def __init__(self, stream_callback: Optional[Callable[[ProcessingStep], None]] = None) -> None:
        self._steps: list[ProcessingStep] = []
        self._stream_callback = stream_callback
        self._fallback_attempts: List[str] = []
        self._original_provider: Optional[str] = None

    @contextmanager
    def track(
        self,
        step: str,
        description: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Callable[[Optional[Dict[str, Any]]], None]:
        """Context manager that measures duration and records a processing step.

        Yields a callable allowing callers to append metadata while the step is running.
        Sends "in-progress" event at start and "completed" event at end for real-time UI updates.
        """
        start = perf_counter()
        collected_meta: Optional[Dict[str, Any]] = dict(metadata) if metadata else None

        def update_metadata(extra: Optional[Dict[str, Any]]) -> None:
            nonlocal collected_meta
            if not extra:
                return
            if collected_meta is None:
                collected_meta = dict(extra)
            else:
                collected_meta.update(extra)

        # Send "in-progress" event at start (for real-time UI feedback)
        if self._stream_callback:
            in_progress_step = ProcessingStep(
                step=step,
                description=description,
                status="in-progress",
                duration_ms=None,
                metadata=collected_meta,
            )
            self._stream_callback(in_progress_step)

        try:
            yield update_metadata
        finally:
            duration_ms = (perf_counter() - start) * 1000
            processing_step = ProcessingStep(
                step=step,
                description=description,
                status="completed",
                duration_ms=duration_ms,
                metadata=collected_meta,
            )
            self._steps.append(processing_step)

            # Send "completed" event with duration
            if self._stream_callback:
                self._stream_callback(processing_step)

    def add_step(
        self,
        step: str,
        description: str,
        duration_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "completed",
    ) -> None:
        """Record a step manually without the context manager.

        Args:
            step: Step identifier (e.g., "parsing_query")
            description: Human-readable description
            duration_ms: Duration in milliseconds (optional, typically for completed steps)
            metadata: Additional step metadata
            status: Step status - "pending", "in-progress", "completed", or "error"
        """
        processing_step = ProcessingStep(
            step=step,
            description=description,
            status=status,
            duration_ms=duration_ms,
            metadata=metadata,
        )
        self._steps.append(processing_step)

        # Call streaming callback if available
        if self._stream_callback:
            self._stream_callback(processing_step)

    def to_list(self) -> list[ProcessingStep]:
        return list(self._steps)

    # === FALLBACK TRACKING ===

    def set_original_provider(self, provider: str) -> None:
        """Record the originally selected provider."""
        self._original_provider = provider

    def add_fallback_attempt(self, provider: str, reason: str = "") -> None:
        """Record a fallback attempt for transparency.

        This information is shown to users when a query fails,
        so they know which providers were tried.
        """
        self._fallback_attempts.append(provider)

        # Also emit a processing step for streaming
        description = STEP_DESCRIPTIONS.get("applying_fallback", "🔄 Trying alternative data source...")
        self.add_step(
            step="applying_fallback",
            description=f"{description.split('...')[0]} ({provider})...",
            status="in-progress",
            metadata={"provider": provider, "reason": reason},
        )

    def get_fallback_attempts(self) -> List[str]:
        """Get list of all fallback providers that were tried."""
        return list(self._fallback_attempts)

    def get_original_provider(self) -> Optional[str]:
        """Get the originally selected provider."""
        return self._original_provider

    # === CONVENIENCE METHODS ===

    def emit_step(
        self,
        step_type: str,
        status: str = "in-progress",
        metadata: Optional[Dict[str, Any]] = None,
        custom_description: Optional[str] = None,
    ) -> None:
        """Emit a standard processing step with predefined description.

        Args:
            step_type: Key from STEP_DESCRIPTIONS (e.g., "parsing_query")
            status: "pending", "in-progress", "completed", or "error"
            metadata: Additional context to include
            custom_description: Override the default description
        """
        description = custom_description or STEP_DESCRIPTIONS.get(
            step_type, f"Processing: {step_type}"
        )
        self.add_step(
            step=step_type,
            description=description,
            status=status,
            metadata=metadata,
        )

    def emit_error(
        self,
        error_type: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit an error step with user-friendly formatting.

        Args:
            error_type: Type of error (e.g., "timeout", "rate_limited", "error")
            message: Human-readable error message
            metadata: Additional error context
        """
        description = STEP_DESCRIPTIONS.get(error_type, "❌ An error occurred")
        full_metadata = {"error_message": message, **(metadata or {})}
        self.add_step(
            step=error_type,
            description=f"{description}: {message[:100]}",
            status="error",
            metadata=full_metadata,
        )

    def emit_fetch_start(
        self,
        provider: str,
        indicator: str,
        country: Optional[str] = None,
    ) -> None:
        """Emit a data fetch start step."""
        country_str = f" for {country}" if country else ""
        self.add_step(
            step="fetching_data",
            description=f"📊 Fetching {indicator}{country_str} from {provider}...",
            status="in-progress",
            metadata={"provider": provider, "indicator": indicator, "country": country},
        )

    def emit_fetch_complete(
        self,
        provider: str,
        data_points: int,
        cached: bool = False,
        duration_ms: Optional[float] = None,
    ) -> None:
        """Emit a data fetch completion step."""
        cache_str = " (cached)" if cached else ""
        self.add_step(
            step="fetching_data",
            description=f"📊 Retrieved {data_points} data points from {provider}{cache_str}",
            status="completed",
            duration_ms=duration_ms,
            metadata={"provider": provider, "data_points": data_points, "cached": cached},
        )


def activate_processing_tracker(tracker: ProcessingTracker) -> Token:
    """Activate tracker for current async context."""
    return _processing_tracker_var.set(tracker)


def get_processing_tracker() -> Optional[ProcessingTracker]:
    """Retrieve tracker for current async context, if any."""
    return _processing_tracker_var.get()


def reset_processing_tracker(token: Token) -> None:
    """Reset tracker context variable."""
    _processing_tracker_var.reset(token)
