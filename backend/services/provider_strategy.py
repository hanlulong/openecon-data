"""Provider strategy helpers extracted from QueryService.

Pure functions that decide which provider is best for a given country/scope,
collect target countries from parameters, and check provider coverage.
These functions have no dependency on QueryService state.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..utils.providers import normalize_provider_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Geographic scope metadata
# ---------------------------------------------------------------------------
# Map from country-specific provider to the ISO2 codes it covers.
# "None" means global (covers everything) -- used as a sentinel.
PROVIDER_GEO_SCOPE: Dict[str, Optional[set]] = {
    "FRED": {"US"},
    "STATSCAN": {"CA"},
    # Eurostat and OECD are handled dynamically via CountryResolver
}


# ---------------------------------------------------------------------------
# Country collection
# ---------------------------------------------------------------------------

def collect_target_countries(parameters: Optional[dict]) -> List[str]:
    """Extract ordered country context from query parameters.

    Returns a de-duplicated list preserving insertion order.
    """
    if not parameters:
        return []

    countries: List[str] = []
    for key in ("countries", "reporters", "partner"):
        value = parameters.get(key)
        if isinstance(value, list):
            countries.extend(str(item) for item in value if item)
        elif value:
            countries.append(str(value))

    for key in ("country", "reporter"):
        value = parameters.get(key)
        if value:
            countries.append(str(value))

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(countries))


# ---------------------------------------------------------------------------
# Scope / comparison detection
# ---------------------------------------------------------------------------

def _has_comparison_markers(query: str) -> bool:
    """Detect comparison intent from query phrasing.

    Mirrors :func:`indicator_resolution.is_comparison_query` plus additional
    multi-country markers.
    """
    query_lower = str(query or "").lower()
    comparison_re = re.search(
        r"\b(compare|comparison|versus|vs|between|across|contrast)\b",
        query_lower,
    )
    return bool(
        comparison_re
        or "member countries" in query_lower
        or "by country" in query_lower
        or "country by country" in query_lower
        or "each country" in query_lower
    )


def provider_supports_requested_scope(
    provider: str,
    query: str,
    countries: Optional[List[str]],
) -> bool:
    """Check whether *provider* can handle the requested comparison scope.

    Returns ``False`` when, for example, OECD is asked to compare more than
    8 countries in a comparison-style query (which would hit rate limits).
    """
    if not countries:
        return True

    provider_upper = normalize_provider_name(provider)
    country_count = len([c for c in countries if c])

    if provider_upper == "OECD" and country_count > 8 and _has_comparison_markers(query):
        return False

    return True


# ---------------------------------------------------------------------------
# Single-country provider selection
# ---------------------------------------------------------------------------

def get_provider_for_single_country(
    iso2: str,
    concept_query: str,
    original_provider: str,
) -> Tuple[str, Optional[str]]:
    """Return ``(provider, indicator_code)`` best suited for *one* country.

    Uses the catalog to find the best provider that covers the given
    country, falling back to *original_provider* when the catalog has no
    opinion, and to WorldBank as the last resort (global coverage).
    """
    from .catalog_service import find_concept_by_term, get_best_provider
    from .provider_fallback import provider_covers_country_list

    concept = find_concept_by_term(concept_query)
    if concept:
        prov, code, conf = get_best_provider(concept, countries=[iso2])
        if prov and conf > 0.0:
            return normalize_provider_name(prov), code

    # If the original provider actually covers this country, keep it.
    if provider_covers_country_list(original_provider, [iso2]):
        return original_provider, None

    # Last resort: WorldBank has global coverage.
    return "WORLDBANK", None
