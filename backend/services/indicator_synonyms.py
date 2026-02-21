"""
Indicator Synonym System

WRAPPER MODULE - This module delegates to catalog_service.py which is the
single source of truth for economic concept definitions.

This module is kept for backward compatibility with existing code that imports
from indicator_synonyms. New code should import directly from catalog_service.

Provides:
1. Expand user queries to catch more variations
2. Prevent false positives (e.g., "productivity" != "production index")
3. Provide default indicator codes when a concept is recognized
"""
from typing import Dict, List, Optional, Any
import logging

# Import everything from catalog_service
from .catalog_service import (
    expand_indicator,
    is_false_positive,
    get_default_indicator,
    find_concept_by_term,
    get_all_synonyms,
    get_exclusions,
    is_excluded_term,
    load_catalog,
)

logger = logging.getLogger(__name__)


# For backward compatibility, provide ECONOMIC_CONCEPT_SYNONYMS as a property
# that builds from the catalog on first access
_cached_synonyms: Optional[Dict[str, Dict[str, Any]]] = None


def _build_synonyms_dict() -> Dict[str, Dict[str, Any]]:
    """Build the legacy ECONOMIC_CONCEPT_SYNONYMS dict from the catalog."""
    global _cached_synonyms
    if _cached_synonyms is not None:
        return _cached_synonyms

    catalog = load_catalog()
    _cached_synonyms = {}

    for concept_name, concept_data in catalog.items():
        synonyms = concept_data.get("synonyms", {})
        primary = synonyms.get("primary", [])
        secondary = synonyms.get("secondary", [])

        # Build default_indicators from providers
        default_indicators = {}
        for provider, provider_info in concept_data.get("providers", {}).items():
            primary_info = provider_info.get("primary", {})
            if isinstance(primary_info, dict) and primary_info.get("code"):
                default_indicators[provider] = primary_info["code"]

        _cached_synonyms[concept_name] = {
            "synonyms": primary + secondary,
            "NOT_synonyms": concept_data.get("explicit_exclusions", []),
            "default_indicators": default_indicators
        }

    return _cached_synonyms


# Lazy-loaded property for backward compatibility
class _SynonymsProxy:
    """Proxy class that loads synonyms on first access."""

    def __getitem__(self, key):
        return _build_synonyms_dict()[key]

    def __contains__(self, key):
        return key in _build_synonyms_dict()

    def __iter__(self):
        return iter(_build_synonyms_dict())

    def items(self):
        return _build_synonyms_dict().items()

    def keys(self):
        return _build_synonyms_dict().keys()

    def values(self):
        return _build_synonyms_dict().values()

    def get(self, key, default=None):
        return _build_synonyms_dict().get(key, default)

    def __len__(self):
        return len(_build_synonyms_dict())


# This is accessed by other modules for backward compatibility
ECONOMIC_CONCEPT_SYNONYMS = _SynonymsProxy()
