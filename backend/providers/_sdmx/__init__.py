"""Opt-in composition utilities shared by SDMX-shaped providers.

This subpackage exists to give multiple providers a single canonical
implementation of helpers that were previously forked across
backend/providers/{fred,imf,eurostat,...}.py. Providers call into these
utilities by composition (not inheritance) — the per-provider classes
remain the source of behavior; the utilities are pure functions that
the provider methods delegate to.

See docs/DEEP_REVIEW_2026-05-30.md Phase 3.1 for the migration plan
and §6 invariant on no mandatory SDMXBaseProvider — composition only.
"""

from .normalizers import normalize_percentage_values
from .periods import (
    frequency_from_period,
    frequency_label,
    period_to_iso_date,
)

__all__ = [
    "normalize_percentage_values",
    "period_to_iso_date",
    "frequency_label",
    "frequency_from_period",
]
