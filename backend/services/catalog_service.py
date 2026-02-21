"""
Unified Indicator Catalog Service

THE SINGLE SOURCE OF TRUTH for economic concept definitions.

This service loads concept definitions from YAML files and provides
a unified API for all indicator-related queries. Other modules
(indicator_synonyms.py, indicator_compatibility.py) should use this
service as their data source.

Key responsibilities:
1. Load and cache YAML concept definitions
2. Find concepts by name or synonym
3. Check if terms are excluded (false positives)
4. Get indicator codes for specific providers
5. Determine best provider for a concept
6. Provide fallback provider chains
"""
from typing import Dict, List, Optional, Any, Tuple, Set
import logging
from pathlib import Path
import yaml

# Use CountryResolver as single source of truth for country/region data
from ..routing.country_resolver import CountryResolver

logger = logging.getLogger(__name__)

# Cache for loaded catalog
_catalog_cache: Optional[Dict[str, Any]] = None

# DEPRECATED: Country sets moved to CountryResolver (backend/routing/country_resolver.py)
# These aliases are kept for backward compatibility but should not be used directly.
# Use CountryResolver.is_oecd_member() and CountryResolver.is_eu_member() instead.
OECD_MEMBERS: Set[str] = CountryResolver.OECD_MEMBERS
EU_MEMBERS: Set[str] = CountryResolver.EU_MEMBERS


def load_catalog() -> Dict[str, Any]:
    """
    Load all concept definitions from YAML files in the catalog directory.

    Returns:
        Dictionary mapping concept names to their definitions
    """
    global _catalog_cache

    if _catalog_cache is not None:
        return _catalog_cache

    catalog_dir = Path(__file__).parent.parent / "catalog" / "concepts"
    _catalog_cache = {}

    if not catalog_dir.exists():
        logger.warning(f"Catalog directory not found: {catalog_dir}")
        return _catalog_cache

    for yaml_file in catalog_dir.glob("*.yaml"):
        try:
            with open(yaml_file, "r") as f:
                concept_data = yaml.safe_load(f)
                if concept_data and "concept" in concept_data:
                    concept_name = concept_data["concept"]
                    _catalog_cache[concept_name] = concept_data
                    logger.debug(f"Loaded concept '{concept_name}' from {yaml_file.name}")
        except yaml.YAMLError as e:
            logger.error(f"Error parsing {yaml_file}: {e}")
        except Exception as e:
            logger.error(f"Error loading {yaml_file}: {e}")

    logger.info(f"Loaded {len(_catalog_cache)} concepts from catalog")
    return _catalog_cache


def reload_catalog() -> None:
    """Force reload of the catalog from disk."""
    global _catalog_cache
    _catalog_cache = None
    load_catalog()
    logger.info("Catalog reloaded")


def get_concept(concept_name: str) -> Optional[Dict[str, Any]]:
    """Get a concept definition by name."""
    catalog = load_catalog()
    return catalog.get(concept_name.lower().replace(" ", "_"))


def get_all_concepts() -> List[str]:
    """Get list of all concept names."""
    return list(load_catalog().keys())


def find_concept_by_term(term: str) -> Optional[str]:
    """
    Find the canonical concept name for a given term.

    Checks if the term matches any concept name or synonym.

    Args:
        term: A term that might be a synonym (e.g., "labor productivity")

    Returns:
        The canonical concept name (e.g., "productivity"), or None
    """
    catalog = load_catalog()
    term_lower = term.lower().strip()

    for concept_name, concept_data in catalog.items():
        # Check concept name
        if term_lower == concept_name.replace("_", " "):
            return concept_name

        # Check synonyms
        synonyms = concept_data.get("synonyms", {})
        primary_synonyms = synonyms.get("primary", [])
        secondary_synonyms = synonyms.get("secondary", [])

        all_synonyms = primary_synonyms + secondary_synonyms
        if term_lower in [s.lower() for s in all_synonyms]:
            return concept_name

    return None


def is_excluded_term(term: str, concept_name: str) -> bool:
    """
    Check if a term is explicitly excluded from a concept.

    Note: This is a simple substring check. For robust semantic matching,
    the LLM-based indicator selection in MetadataSearchService should be used.

    Args:
        term: The term to check (e.g., "production index")
        concept_name: The concept to check against (e.g., "productivity")

    Returns:
        True if the term contains an explicit exclusion phrase
    """
    concept = get_concept(concept_name)
    if not concept:
        return False

    exclusions = concept.get("explicit_exclusions", [])
    term_lower = term.lower()

    for exclusion in exclusions:
        if exclusion.lower() in term_lower:
            return True

    return False


def get_all_synonyms(concept_name: str) -> List[str]:
    """
    Get all synonyms for a concept, including primary and secondary.

    Args:
        concept_name: The economic concept

    Returns:
        List of all synonym terms (including concept name itself)
    """
    concept = get_concept(concept_name)
    if not concept:
        return []

    synonyms = concept.get("synonyms", {})
    primary = synonyms.get("primary", [])
    secondary = synonyms.get("secondary", [])

    return [concept_name.replace("_", " ")] + primary + secondary


def get_exclusions(concept_name: str) -> List[str]:
    """Get explicit exclusions for a concept."""
    concept = get_concept(concept_name)
    if not concept:
        return []
    return concept.get("explicit_exclusions", [])


def get_indicator_code(
    concept_name: str,
    provider: str,
    variant: str = "primary"
) -> Optional[str]:
    """
    Get the indicator code for a concept from a specific provider.

    Args:
        concept_name: The economic concept (e.g., "productivity")
        provider: The data provider (e.g., "WorldBank", "OECD")
        variant: The variant to use ("primary", "growth", etc.)

    Returns:
        The indicator code, or None if not available
    """
    concept = get_concept(concept_name)
    if not concept:
        return None

    # Check if provider is in not_available list
    not_available = concept.get("not_available", [])
    not_available_lower = {p.lower() for p in not_available}
    if provider.lower() in not_available_lower:
        return None

    providers = concept.get("providers", {})
    providers_lower = {p.lower(): p for p in providers.keys()}
    actual_provider = providers_lower.get(provider.lower())
    if not actual_provider:
        return None

    provider_info = providers.get(actual_provider, {})

    if not provider_info:
        return None

    # Get the variant (primary, growth, etc.)
    variant_info = provider_info.get(variant, {})
    if isinstance(variant_info, dict):
        return variant_info.get("code")

    return None


def get_provider_info(concept_name: str, provider: str) -> Optional[Dict[str, Any]]:
    """Get full provider information for a concept."""
    concept = get_concept(concept_name)
    if not concept:
        return None

    providers = concept.get("providers", {})
    providers_lower = {p.lower(): p for p in providers.keys()}
    actual_provider = providers_lower.get(provider.lower())
    if not actual_provider:
        return None
    return providers.get(actual_provider)


def _collect_codes_from_node(node: Any, seen: Set[str], out: List[str]) -> None:
    """Recursively collect indicator codes from nested provider metadata."""
    if isinstance(node, dict):
        code = node.get("code")
        if isinstance(code, str):
            candidate = code.strip()
            if candidate and candidate.lower() not in {"null", "none", "dynamic", "n/a"}:
                normalized = candidate.upper()
                if normalized not in seen:
                    seen.add(normalized)
                    out.append(candidate)

        for value in node.values():
            _collect_codes_from_node(value, seen, out)
        return

    if isinstance(node, list):
        for item in node:
            _collect_codes_from_node(item, seen, out)


def get_indicator_codes(concept_name: str, provider: str) -> List[str]:
    """
    Get all known indicator codes for a concept/provider pair.

    This includes primary and nested variant mappings (e.g., growth, alternates,
    sector-specific codes), while skipping placeholders like ``dynamic``.
    """
    provider_info = get_provider_info(concept_name, provider)
    if not provider_info:
        return []

    seen: Set[str] = set()
    codes: List[str] = []
    _collect_codes_from_node(provider_info, seen, codes)
    return codes


def is_indicator_code_for_concept(concept_name: str, provider: str, code: str) -> bool:
    """Check whether a provider/code mapping belongs to a catalog concept."""
    if not code:
        return False

    target = code.strip().upper()
    return any(c.strip().upper() == target for c in get_indicator_codes(concept_name, provider))


def find_concepts_by_code(provider: str, code: str) -> List[str]:
    """Find catalog concepts that include a specific provider/code mapping."""
    if not provider or not code:
        return []

    matches: List[str] = []
    for concept_name in get_all_concepts():
        if is_indicator_code_for_concept(concept_name, provider, code):
            matches.append(concept_name)
    return matches


def get_available_providers(concept_name: str) -> List[str]:
    """Get list of providers that have this concept available."""
    concept = get_concept(concept_name)
    if not concept:
        return []

    providers = concept.get("providers", {})
    not_available = concept.get("not_available", [])

    return [p for p in providers.keys() if p not in not_available]


def is_provider_available(concept_name: str, provider: str) -> bool:
    """Check if a provider has data for this concept.

    Uses case-insensitive matching for provider names.
    """
    concept = get_concept(concept_name)
    if not concept:
        return True  # Unknown concept, let provider try

    not_available = concept.get("not_available", [])
    # Case-insensitive check for not_available list
    not_available_lower = [p.lower() for p in not_available]
    if provider.lower() in not_available_lower:
        return False

    providers = concept.get("providers", {})
    # Case-insensitive check for providers dict
    providers_lower = {p.lower(): p for p in providers.keys()}
    return provider.lower() in providers_lower


def _check_coverage(coverage: Any, countries: Optional[List[str]]) -> bool:
    """Check if provider coverage includes the requested countries.

    Uses CountryResolver as the single source of truth for region membership.
    """
    if not countries:
        return True

    if coverage == "global":
        return True

    if coverage == "oecd_members":
        return all(CountryResolver.is_oecd_member(c) for c in countries)

    if coverage == "eu_members":
        return all(CountryResolver.is_eu_member(c) for c in countries)

    if isinstance(coverage, list):
        coverage_upper = {c.upper() for c in coverage}
        return all(c.upper() in coverage_upper for c in countries)

    return False


def get_best_provider(
    concept_name: str,
    countries: Optional[List[str]] = None,
    preferred_provider: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], float]:
    """
    Get the best provider for a concept based on coverage and confidence.

    Args:
        concept_name: The economic concept
        countries: List of countries to consider for coverage
        preferred_provider: Optional preferred provider to try first

    Returns:
        Tuple of (provider_name, indicator_code, confidence)
    """
    concept = get_concept(concept_name)
    if not concept:
        return None, None, 0.0

    providers = concept.get("providers", {})
    not_available = concept.get("not_available", [])

    # Create case-insensitive lookup maps
    providers_lower = {p.lower(): p for p in providers.keys()}
    not_available_lower = [p.lower() for p in not_available]

    # If preferred provider is available and covers countries, use it
    if preferred_provider:
        pref_lower = preferred_provider.lower()
        if pref_lower in providers_lower:
            actual_name = providers_lower[pref_lower]
            if pref_lower not in not_available_lower:
                primary = providers[actual_name].get("primary", {})
                if isinstance(primary, dict) and primary.get("code"):
                    coverage = primary.get("coverage", "global")
                    if _check_coverage(coverage, countries):
                        return (
                            actual_name,
                            primary["code"],
                            primary.get("confidence", 0.8)
                        )

    # Find best provider by confidence that covers the countries
    best_provider = None
    best_code = None
    best_confidence = 0.0

    for provider_name, provider_info in providers.items():
        if provider_name.lower() in not_available_lower:
            continue

        primary = provider_info.get("primary", {})
        if not isinstance(primary, dict):
            continue

        code = primary.get("code")
        if not code:
            continue

        coverage = primary.get("coverage", "global")
        if not _check_coverage(coverage, countries):
            continue

        confidence = primary.get("confidence", 0.8)
        if confidence > best_confidence:
            best_provider = provider_name
            best_code = code
            best_confidence = confidence

    return best_provider, best_code, best_confidence


def get_fallback_providers(
    concept_name: str,
    exclude_provider: Optional[str] = None
) -> List[Tuple[str, str, float]]:
    """
    Get fallback providers for a concept when the primary fails.

    Args:
        concept_name: The concept name
        exclude_provider: Provider to exclude (e.g., the one that failed)

    Returns:
        List of (provider, indicator_code, confidence) tuples in priority order
    """
    concept = get_concept(concept_name)
    if not concept:
        return []

    providers = concept.get("providers", {})
    not_available = concept.get("not_available", [])

    fallbacks = []
    for provider_name, provider_info in providers.items():
        if provider_name == exclude_provider:
            continue
        if provider_name in not_available:
            continue

        primary = provider_info.get("primary", {})
        if not isinstance(primary, dict):
            continue

        code = primary.get("code")
        if not code:
            continue

        confidence = primary.get("confidence", 0.8)
        fallbacks.append((provider_name, code, confidence))

    # Sort by confidence (highest first)
    fallbacks.sort(key=lambda x: x[2], reverse=True)
    return fallbacks


def validate_indicator_match(indicator_name: str, concept_name: str) -> Tuple[bool, str]:
    """
    Validate if an indicator name matches a concept.

    Uses explicit exclusions to prevent false positives. The validation is
    permissive by default - only rejecting known false positives.

    Logic:
    1. If indicator contains an explicit exclusion term -> REJECT
    2. If indicator contains a synonym -> ACCEPT with high confidence
    3. Otherwise -> ACCEPT (permissive - let search/LLM decide relevance)

    Args:
        indicator_name: The indicator name from search results
        concept_name: The concept to validate against

    Returns:
        Tuple of (is_valid, reason)
    """
    # First, reject explicit exclusions (known false positives)
    if is_excluded_term(indicator_name, concept_name):
        return False, f"'{indicator_name}' is an explicit exclusion for '{concept_name}'"

    # Check if any synonym is in the indicator name (high confidence match)
    synonyms = get_all_synonyms(concept_name)
    indicator_lower = indicator_name.lower()

    for synonym in synonyms:
        if synonym.lower() in indicator_lower:
            return True, f"Matches synonym '{synonym}'"

    # Permissive: accept if not an exclusion
    # This allows the search system to return relevant results that
    # might not exactly match synonyms but are still valid
    return True, "Accepted (not an explicit exclusion)"


# ============================================================================
# COMPATIBILITY LAYER - Functions for backward compatibility with old modules
# ============================================================================

def expand_indicator(indicator: str) -> Dict[str, Any]:
    """
    Expand user's indicator term to full concept with synonyms and exclusions.

    This provides backward compatibility with indicator_synonyms.py interface.

    Args:
        indicator: User's indicator term (e.g., "productivity", "gdp growth")

    Returns:
        Dict with concept, synonyms, NOT_synonyms, default_indicators
    """
    concept_name = find_concept_by_term(indicator)
    if not concept_name:
        concept_name = indicator.lower().replace(" ", "_")

    concept = get_concept(concept_name)
    if not concept:
        return {
            "concept": concept_name,
            "synonyms": [],
            "NOT_synonyms": [],
            "default_indicators": {}
        }

    # Build default_indicators from providers
    default_indicators = {}
    for provider, provider_info in concept.get("providers", {}).items():
        primary = provider_info.get("primary", {})
        if isinstance(primary, dict) and primary.get("code"):
            default_indicators[provider] = primary["code"]

    synonyms = concept.get("synonyms", {})
    return {
        "concept": concept_name,
        "synonyms": synonyms.get("primary", []) + synonyms.get("secondary", []),
        "NOT_synonyms": concept.get("explicit_exclusions", []),
        "default_indicators": default_indicators
    }


def is_false_positive(indicator_name: str, concept_info: Dict[str, Any]) -> bool:
    """
    Check if an indicator name is a known false positive for the concept.

    This provides backward compatibility with indicator_synonyms.py interface.
    """
    if not indicator_name or not concept_info.get("NOT_synonyms"):
        return False

    indicator_lower = indicator_name.lower()
    for exclusion in concept_info["NOT_synonyms"]:
        if exclusion.lower() in indicator_lower:
            return True

    return False


def get_default_indicator(concept: str, provider: str) -> Optional[str]:
    """
    Get the default indicator code for a concept and provider.

    This provides backward compatibility with indicator_synonyms.py interface.
    """
    return get_indicator_code(concept, provider, "primary")
