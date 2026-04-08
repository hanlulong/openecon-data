"""Geography validation helpers extracted from QueryService.

This module contains pure functions for assessing multi-country coverage
in query results and building user-facing warnings when coverage is
partial.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from ..models import NormalizedData, ParsedIntent
from .provider_fallback import normalize_country_to_iso2
from .provider_strategy import collect_target_countries

logger = logging.getLogger(__name__)


def assess_country_coverage(
    intent: Optional[ParsedIntent],
    data: Optional[List[NormalizedData]],
) -> Optional[Dict[str, Any]]:
    """
    Assess whether a multi-country request is fully represented in result data.

    Returns None when coverage checks do not apply (for example single-country
    queries), otherwise returns a dict with coverage details.
    """
    if not intent or not data:
        return None

    requested_countries = collect_target_countries(intent.parameters)
    if len(requested_countries) < 2:
        return None

    requested_map: "OrderedDict[str, str]" = OrderedDict()
    for raw_country in requested_countries:
        normalized_iso2 = normalize_country_to_iso2(raw_country)
        if not normalized_iso2:
            continue
        requested_map.setdefault(normalized_iso2, str(raw_country))

    if len(requested_map) < 2:
        return None

    returned_map: "OrderedDict[str, str]" = OrderedDict()
    for series in data:
        metadata = getattr(series, "metadata", None) if series is not None else None
        if not metadata:
            continue
        result_country = getattr(metadata, "country", None)
        normalized_iso2 = normalize_country_to_iso2(result_country)
        if not normalized_iso2:
            continue
        returned_map.setdefault(normalized_iso2, str(result_country))

    missing_iso2 = [iso2 for iso2 in requested_map.keys() if iso2 not in returned_map]
    covered_iso2 = [iso2 for iso2 in requested_map.keys() if iso2 in returned_map]
    coverage_ratio = len(covered_iso2) / max(len(requested_map), 1)

    return {
        "requested_iso2": list(requested_map.keys()),
        "requested_display": list(requested_map.values()),
        "returned_iso2": list(returned_map.keys()),
        "returned_display": list(returned_map.values()),
        "missing_iso2": missing_iso2,
        "missing_display": [requested_map[iso2] for iso2 in missing_iso2],
        "covered_count": len(covered_iso2),
        "requested_count": len(requested_map),
        "coverage_ratio": coverage_ratio,
        "complete": len(missing_iso2) == 0,
    }


def build_country_coverage_warning_message(
    coverage: Dict[str, Any],
) -> str:
    """Create a concise user-facing warning for partial multi-country coverage."""
    missing_display = [str(item) for item in (coverage.get("missing_display") or []) if item]
    returned_display = [str(item) for item in (coverage.get("returned_display") or []) if item]

    if missing_display:
        missing_text = ", ".join(missing_display)
        if returned_display:
            available_text = ", ".join(returned_display)
            return (
                "Data is only available for a subset of requested countries. "
                f"Missing: {missing_text}. Available: {available_text}."
            )
        return (
            "Data is only available for a subset of requested countries. "
            f"Missing: {missing_text}."
        )

    return ""
