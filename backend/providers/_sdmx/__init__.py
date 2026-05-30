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

__all__ = ["normalize_percentage_values"]
