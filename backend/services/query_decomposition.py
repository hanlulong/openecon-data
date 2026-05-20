"""Query decomposition helpers extracted from QueryService.

This module contains pure functions for decomposing aggregate queries
(e.g. "population by province") into entity-specific sub-queries.
"""

from __future__ import annotations

import logging
import re
from typing import List

from ..models import DataPoint, NormalizedData
from ..routing.country_resolver import CountryResolver
from ..utils.providers import normalize_provider_name
from .indicator_resolution import (
    is_ranking_query,
    is_exact_match_locked,
    extract_top_n_from_query,
    extract_target_year_from_query,
)
from .provider_strategy import collect_target_countries
from .relevance_scorer import extract_ranking_value

logger = logging.getLogger(__name__)


def generate_sub_query(original_query: str, entity: str, decomposition_type: str) -> str:
    """
    Generate a sub-query for a specific entity.

    Examples:
        - "population of canada by provinces" + "Ontario" -> "population of Ontario"
        - "GDP by each US state" + "California" -> "GDP of California"

    Args:
        original_query: Original user query
        entity: Entity name (e.g., "Ontario", "California")
        decomposition_type: Type of decomposition ("provinces", "states", etc.)

    Returns:
        Modified query for the specific entity
    """
    # Patterns to replace
    patterns = {
        "provinces": [
            (r"by\s+provinces?", f"for {entity}"),  # Match "by province" or "by provinces"
            (r"all\s+provinces?", entity),
            (r"each\s+provinces?", entity),
            (r"in\s+canada\s+by\s+provinces?", f"in {entity}"),  # Match "in canada by province(s)"
            (r"of\s+canada\s+by\s+provinces?", f"of {entity}"),
            (r"for\s+each\s+provinces?", f"for {entity}"),
        ],
        "states": [
            (r"by\s+states?", f"for {entity}"),
            (r"all\s+states", entity),
            (r"each\s+state", entity),
            (r"by\s+each\s+US\s+state", f"for {entity}"),
            (r"for\s+each\s+state", f"for {entity}"),
        ],
        "countries": [
            (r"by\s+countr(?:y|ies)", f"for {entity}"),
            (r"all\s+countries", entity),
            (r"each\s+country", entity),
            (r"for\s+each\s+country", f"for {entity}"),
        ],
        "regions": [
            (r"by\s+regions?", f"for {entity}"),
            (r"all\s+regions", entity),
            (r"each\s+region", entity),
            (r"for\s+each\s+region", f"for {entity}"),
        ],
    }

    sub_query = original_query
    if decomposition_type in patterns:
        for pattern, replacement in patterns[decomposition_type]:
            sub_query = re.sub(pattern, replacement, sub_query, flags=re.IGNORECASE)

    logger.debug("Generated sub-query for %s: '%s' -> '%s'", entity, original_query, sub_query)
    return sub_query


def apply_ranking_projection(query: str, data: List[NormalizedData]) -> List[NormalizedData]:
    """Transform ranking queries into sorted top-N datasets by latest/target-year value.

    Pure function equivalent of ``QueryService._apply_ranking_projection``.

    This improves UX for prompts like:

    - "Rank top 10 economies by GDP growth in 2023"
    - "Which ASEAN country has the highest import share of GDP since 2015"

    Args:
        query: The raw user query.
        data: List of normalised series to project.

    Returns:
        Projected (and possibly truncated) data list, or the original data
        when the query is not a ranking query or no rows could be ranked.
    """
    if not data or not is_ranking_query(query):
        return data

    target_year = extract_target_year_from_query(query)
    top_n = extract_top_n_from_query(query, default=10)
    query_lower = str(query or "").lower()
    descending = not any(term in query_lower for term in ("lowest", "smallest", "worst", "bottom"))

    ranking_rows: List[tuple[float, int, NormalizedData, DataPoint]] = []
    for index, series in enumerate(data):
        value, point = extract_ranking_value(series, target_year)
        if value is None or point is None:
            continue
        ranking_rows.append((value, index, series, point))

    if not ranking_rows:
        return data

    ranking_rows.sort(key=lambda item: (item[0], -item[1]), reverse=descending)
    selected_rows = ranking_rows[:top_n]

    projected: List[NormalizedData] = []
    for _value, _index, series, point in selected_rows:
        projected_series = series.model_copy(deep=True)
        projected_series.data = [point.model_copy(deep=True)]
        projected.append(projected_series)

    return projected or data


def maybe_expand_ranking_country_scope(
    query: str,
    provider: str,
    params: dict,
) -> dict:
    """
    Expand country scope for ranking queries that request top/highest/lowest
    results without enough country context.

    This keeps ranking in deterministic retrieval mode while avoiding single-
    country defaults for broad ranking prompts.
    """
    if not params:
        params = {}

    query_text = str(query or "").strip()
    if not query_text or not is_ranking_query(query_text):
        return params
    if is_exact_match_locked(params):
        return params
    if params.get("_ranking_scope_expanded"):
        return params

    existing_targets = collect_target_countries(params)
    if len(existing_targets) >= 2:
        return params

    expanded_countries: List[str] = []

    if len(existing_targets) == 1:
        region_expansion = CountryResolver.expand_region(existing_targets[0])
        if region_expansion and len(region_expansion) >= 2:
            expanded_countries = region_expansion
        else:
            return params
    else:
        expanded_countries = CountryResolver.expand_regions_in_query(query_text)
        # If region expansion didn't yield countries for a ranking query,
        # default to G20 — the LLM should provide specific countries, but
        # for ranking queries we need a sensible default set.
        if len(expanded_countries) < 2:
            expanded_countries = sorted(CountryResolver.G20_MEMBERS)

    if len(expanded_countries) < 2:
        return params

    normalized_provider = normalize_provider_name(provider)
    if normalized_provider == "EUROSTAT":
        expanded_countries = [
            country for country in expanded_countries
            if CountryResolver.is_eu_member(country)
        ]

    if len(expanded_countries) < 2:
        return params

    updated = dict(params)
    updated.pop("country", None)
    updated["countries"] = list(dict.fromkeys([str(country) for country in expanded_countries if country]))
    updated["_ranking_scope_expanded"] = True

    logger.info(
        "📈 Expanded ranking scope to %d countries for provider %s",
        len(updated.get("countries") or []),
        normalized_provider or provider,
    )
    return updated
