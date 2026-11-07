"""Time-window provenance (who set the active window) — shared, stamp-first.

``__time_scope_authority`` is written where provenance is unambiguous:
ParameterValidator.apply_default_time_periods on the first turn, and the
conversation-state time materialization (``materialize_time_scope``) on every
follow-up path. Every consumer that must distinguish a USER window from a
framework DEFAULT window (exact-match date stripping, the honesty filter, the
broadened no-data retries — in data_fetcher AND inside providers) reads the
stamp through this module instead of re-inferring it from raw query text,
which is language-dependent ("只看最近两年" carries no English cue).

Purely structural: reads a provenance field, never interprets text.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

TIME_BOUND_KEYS = ("startDate", "endDate", "start_year", "end_year")

# Process-local count of dated reads that found no provenance stamp.
_PROVENANCE_MISSING_COUNT = 0


def has_time_bounds(params: Optional[Mapping[str, Any]]) -> bool:
    return any((params or {}).get(key) for key in TIME_BOUND_KEYS)


def stamped_user_time_scope(params: Optional[Mapping[str, Any]]) -> Optional[bool]:
    """True/False from the recorded provenance stamp; None when unstamped.

    Callers fall back to their legacy text heuristic ONLY on None, and that
    fallback is counted (``provenance_missing_count``) and logged at DEBUG
    (``time_scope_provenance_missing``) when dates are present, so a producer
    that forgets to stamp is observable without flooding production logs —
    one request reads the stamp at several gates, and known-unstamped
    producers (provider default ranges, pre-stamp Redis states) hit it on
    every dated call.
    """
    global _PROVENANCE_MISSING_COUNT
    authority = str((params or {}).get("__time_scope_authority") or "").strip().lower()
    if authority == "user":
        return True
    if authority == "default":
        return False
    if has_time_bounds(params):
        _PROVENANCE_MISSING_COUNT += 1
        logger.debug(
            "time_scope_provenance_missing: dated params without "
            "__time_scope_authority; falling back to text heuristic"
        )
    return None


def provenance_missing_count() -> int:
    """Process-local count of dated reads that found no provenance stamp."""
    return _PROVENANCE_MISSING_COUNT
