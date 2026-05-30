"""Shared normalizers for SDMX-shaped provider data.

Phase 3.1 of the deep review: the percentage-decimal-to-percent conversion
was forked three times across FRED / IMF / Eurostat with byte-equivalent
logic. This module hosts the canonical implementation; each provider's
_normalize_percentage_values method now delegates here.

Pure functions, no provider dependencies, no module-level state — safe to
import from anywhere.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Below this absolute value, a series of "percentage" values almost
# certainly comes back from the upstream API as decimals (0.025 means 2.5%
# rather than 2.5%). Above 1.5 the natural percent scale (e.g. debt/GDP at
# 60% or unemployment at 10%) is unambiguous, so we leave the values alone.
_DECIMAL_PERCENT_THRESHOLD = 1.5


def normalize_percentage_values(
    data: Sequence[Dict[str, Any]],
    *,
    label: str = "",
    threshold: float = _DECIMAL_PERCENT_THRESHOLD,
) -> List[Dict[str, Any]]:
    """If a series looks like it returned decimals, convert to percent.

    A series is treated as "decimal percentages" when every non-null value's
    absolute value is below `threshold` (default 1.5). In that case every
    non-null value is multiplied by 100. Null values are preserved. The
    input list is not mutated; a new list is returned only when the
    multiplication actually fires (otherwise the input is returned
    unchanged, matching the prior per-provider behavior).

    Args:
        data: sequence of {'date': ..., 'value': float|None} dicts.
        label: provider-side identifier (FRED series id, IMF indicator name,
            Eurostat dataset code) used only in the diagnostic log line.
        threshold: override the default 1.5 threshold; kept as a kwarg so
            unit tests can pin behavior without touching the constant.

    Returns:
        Either the input sequence unchanged (no normalization needed) or a
        freshly-built list with the same date keys and rescaled values.
    """
    if not data:
        return list(data)

    non_null_abs = [abs(d["value"]) for d in data if d.get("value") is not None]
    if not non_null_abs:
        return list(data)

    max_abs = max(non_null_abs)
    if max_abs >= threshold:
        return list(data)

    logger.info(
        "Normalizing percentage values for %s (max abs value: %s)",
        label or "<unlabeled>",
        max_abs,
    )
    return [
        {
            "date": d["date"],
            "value": (d["value"] * 100) if d.get("value") is not None else None,
        }
        for d in data
    ]
