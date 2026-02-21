"""
Provider-Indicator Compatibility Service

WRAPPER MODULE - This module delegates to catalog_service.py which is the
single source of truth for economic concept definitions.

This module is kept for backward compatibility with existing code that imports
from indicator_compatibility. New code should import directly from catalog_service.

Provides:
1. Select the best provider for a given indicator concept
2. Consider country coverage when selecting providers
3. Provide fallback options when primary provider fails
"""
from typing import Dict, List, Optional, Tuple, Any
import logging

# Import everything from catalog_service
from .catalog_service import (
    find_concept_by_term,
    get_best_provider,
    get_fallback_providers as _get_fallback_providers,
    get_indicator_code,
    is_provider_available,
    load_catalog,
    _check_coverage,
)

logger = logging.getLogger(__name__)


def load_compatibility_matrix() -> Dict[str, Any]:
    """
    Load the provider-indicator compatibility matrix.

    This is now a wrapper around load_catalog() for backward compatibility.
    The catalog is the single source of truth.

    Returns:
        The compatibility matrix dictionary (built from catalog)
    """
    catalog = load_catalog()

    # Build compatibility matrix format from catalog
    matrix = {
        "_description": "Provider-Indicator Compatibility Matrix - Generated from YAML catalog",
        "_version": "2.0.0",
        "_source": "catalog/concepts/*.yaml"
    }

    for concept_name, concept_data in catalog.items():
        providers = concept_data.get("providers", {})
        not_available = concept_data.get("not_available", [])

        # Build available providers dict
        available = {}
        preferred_providers = []

        for provider_name, provider_info in providers.items():
            primary = provider_info.get("primary", {})
            if isinstance(primary, dict) and primary.get("code"):
                available[provider_name] = {
                    "indicator": primary["code"],
                    "name": primary.get("name", ""),
                    "confidence": primary.get("confidence", 0.8),
                    "coverage": primary.get("coverage", "global"),
                    "frequency": primary.get("frequency", "annual")
                }
                # Add to preferred list sorted by confidence
                preferred_providers.append((provider_name, primary.get("confidence", 0.8)))

        # Sort by confidence
        preferred_providers.sort(key=lambda x: x[1], reverse=True)

        matrix[concept_name] = {
            "preferred_providers": [p[0] for p in preferred_providers],
            "available": available,
            "not_available": not_available
        }

    return matrix


def get_best_provider_for_indicator(
    indicator: str,
    countries: Optional[List[str]] = None,
    preferred_provider: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], float]:
    """
    Get the best provider and indicator code for a user's request.

    This considers:
    1. The indicator concept (via synonym expansion)
    2. Country coverage requirements
    3. Provider preferences
    4. Confidence levels

    Args:
        indicator: The indicator term (e.g., "productivity", "gdp growth")
        countries: List of country codes/names being queried
        preferred_provider: Optional preferred provider (will be tried first)

    Returns:
        Tuple of (provider_name, indicator_code, confidence)
        Returns (None, None, 0.0) if no suitable provider found
    """
    # Find the canonical concept for this indicator
    concept = find_concept_by_term(indicator)
    if not concept:
        concept = indicator.lower().replace(" ", "_")

    return get_best_provider(concept, countries, preferred_provider)


def get_fallback_providers(
    indicator: str,
    exclude_provider: Optional[str] = None
) -> List[Tuple[str, str, float]]:
    """
    Get fallback providers for an indicator when the primary fails.

    Args:
        indicator: The indicator term
        exclude_provider: Provider to exclude (e.g., the one that failed)

    Returns:
        List of (provider, indicator_code, confidence) tuples in priority order
    """
    # Find the canonical concept for this indicator
    concept = find_concept_by_term(indicator)
    if not concept:
        concept = indicator.lower().replace(" ", "_")

    return _get_fallback_providers(concept, exclude_provider)


def is_indicator_available(indicator: str, provider: str) -> bool:
    """
    Check if an indicator is available from a specific provider.

    Args:
        indicator: The indicator term
        provider: The provider name

    Returns:
        True if available, False otherwise
    """
    # Find the canonical concept for this indicator
    concept = find_concept_by_term(indicator)
    if not concept:
        concept = indicator.lower().replace(" ", "_")

    return is_provider_available(concept, provider)


# Re-export get_indicator_code with indicator term handling
def get_indicator_code_for_term(indicator: str, provider: str) -> Optional[str]:
    """
    Get the specific indicator code for a provider.

    Args:
        indicator: The indicator term
        provider: The provider name

    Returns:
        The indicator code, or None if not available
    """
    # Find the canonical concept for this indicator
    concept = find_concept_by_term(indicator)
    if not concept:
        concept = indicator.lower().replace(" ", "_")

    return get_indicator_code(concept, provider)
