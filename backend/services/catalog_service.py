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
import re
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
    if not term_lower:
        return None

    # 1) Exact match pass (strict, highest precision)
    for concept_name, concept_data in catalog.items():
        if term_lower == concept_name.replace("_", " "):
            return concept_name

        synonyms = concept_data.get("synonyms", {})
        primary_synonyms = synonyms.get("primary", [])
        secondary_synonyms = synonyms.get("secondary", [])
        all_synonyms = [s.lower() for s in (primary_synonyms + secondary_synonyms)]
        if term_lower in all_synonyms:
            return concept_name

    # 2) Semantic phrase/token pass for longer natural-language terms.
    # Keeps precision by requiring meaningful overlap with known synonyms,
    # while also handling typo/plural variants in a generic way.
    term_corrections = {
        "ration": "ratio",
        "exprot": "export",
        "exprt": "export",
        "improt": "import",
        "imprt": "import",
        "savngs": "savings",
    }

    def _tokenize(text: str) -> Set[str]:
        raw_tokens = re.findall(r"[a-z0-9]+", str(text or "").lower().replace("_", " "))
        tokens: Set[str] = set()
        for token in raw_tokens:
            token = term_corrections.get(token, token)
            if len(token) <= 1:
                continue
            tokens.add(token)
            if token.endswith("ies") and len(token) > 4:
                tokens.add(token[:-3] + "y")
            elif token.endswith("s") and len(token) > 3:
                tokens.add(token[:-1])
        return tokens

    term_tokens = _tokenize(term_lower)
    if not term_tokens:
        return None

    term_has_import = bool({"import", "imports"} & term_tokens)
    term_has_export = bool({"export", "exports"} & term_tokens)

    best_concept: Optional[str] = None
    best_score = 0.0

    for concept_name, concept_data in catalog.items():
        if is_excluded_term(term_lower, concept_name):
            continue

        synonyms = concept_data.get("synonyms", {})
        candidates = [concept_name.replace("_", " ")]
        candidates.extend(synonyms.get("primary", []))
        candidates.extend(synonyms.get("secondary", []))

        concept_score = 0.0
        for candidate in candidates:
            candidate_lower = str(candidate or "").strip().lower()
            if not candidate_lower:
                continue

            candidate_tokens = _tokenize(candidate_lower)

            # Direct phrase containment is a strong signal.
            if len(candidate_lower) >= 4 and candidate_lower in term_lower:
                phrase_bonus = min(0.03, 0.01 * max(len(candidate_tokens) - 1, 0))
                concept_score = max(concept_score, 0.95 + phrase_bonus)
                continue

            if not candidate_tokens:
                continue

            candidate_score = 0.0
            overlap = len(term_tokens & candidate_tokens) / max(len(candidate_tokens), 1)
            if len(candidate_tokens) >= 2 and overlap >= 0.60:
                candidate_score = max(candidate_score, min(0.92, 0.56 + 0.40 * overlap))
            elif len(candidate_tokens) == 1 and overlap >= 1.0:
                candidate_score = max(candidate_score, 0.84)

            candidate_has_import = bool({"import", "imports"} & candidate_tokens)
            candidate_has_export = bool({"export", "exports"} & candidate_tokens)

            # Directional guardrails: import/export intent should strongly favor
            # same-direction concepts and penalize opposite/neutral concepts.
            if term_has_import:
                if candidate_has_import:
                    candidate_score += 0.10
                elif candidate_has_export:
                    candidate_score -= 0.35
                else:
                    candidate_score -= 0.12
            if term_has_export:
                if candidate_has_export:
                    candidate_score += 0.10
                elif candidate_has_import:
                    candidate_score -= 0.35
                else:
                    candidate_score -= 0.12

            concept_score = max(concept_score, candidate_score)

        if concept_score > best_score:
            best_score = concept_score
            best_concept = concept_name

    if best_score >= 0.72:
        return best_concept

    return None


def is_excluded_term(term: str, concept_name: str) -> bool:
    """
    Check if a term is explicitly excluded from a concept.

    Internal helper for find_concept_by_term — kept until the broader
    catalog runtime-authority migration (see docs/DEEP_REVIEW_2026-05-30.md
    Phase 1.2 deferred work). Not exported.
    """
    concept = get_concept(concept_name)
    if not concept:
        return False

    exclusions = concept.get("explicit_exclusions", [])
    # Normalize underscores to spaces so "maternal_mortality" matches
    # the exclusion "maternal mortality". LLM outputs often use
    # underscores as word separators.
    term_lower = term.lower().replace("_", " ")

    for exclusion in exclusions:
        exclusion_text = str(exclusion or "").strip().lower()
        if not exclusion_text:
            continue

        # Boundary-aware phrase match avoids false positives where exclusion
        # text is only a substring of the intended concept (e.g., employment
        # inside unemployment).
        escaped = re.escape(exclusion_text).replace(r"\ ", r"[\s\-]+")
        if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", term_lower):
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


def is_provider_explicitly_excluded(concept_name: str, provider: str) -> bool:
    """Check if a provider is explicitly listed in not_available for a concept.

    Unlike ``is_provider_available``, this returns True **only** when the
    provider appears in the concept's ``not_available`` list.  A provider that
    is simply absent from the ``providers`` dict (but not excluded) returns
    False -- meaning the catalog is incomplete for that provider, not that the
    provider definitely lacks the data.

    This distinction matters for large-catalog providers like StatsCan (40K+
    tables): the catalog may not yet have a StatsCan entry for every concept,
    but that doesn't mean StatsCan lacks the data.
    """
    concept = get_concept(concept_name)
    if not concept:
        return False  # Unknown concept -- nothing is excluded
    not_available = concept.get("not_available", [])
    not_available_lower = [p.lower() for p in not_available]
    return provider.lower() in not_available_lower


def _check_coverage(coverage: Any, countries: Optional[List[str]]) -> bool:
    """Check if provider coverage includes the requested countries.

    Uses CountryResolver as the single source of truth for region membership.
    """
    if not countries:
        return True

    def _to_iso2(country: str) -> Optional[str]:
        if not country:
            return None
        normalized = CountryResolver.normalize(country)
        if normalized:
            return normalized
        return CountryResolver.to_iso2(str(country).upper())

    if isinstance(coverage, list):
        coverage_upper = {
            (CountryResolver.normalize(c) or str(c).upper())
            for c in coverage
            if c
        }
        return all(
            (CountryResolver.normalize(c) or str(c).upper()) in coverage_upper
            for c in countries
        )

    if isinstance(coverage, str):
        # Strip inline comments in YAML values like "OECD  # ...".
        coverage_normalized = coverage.split("#", 1)[0].strip().lower()
    else:
        coverage_normalized = str(coverage or "").strip().lower()

    if coverage_normalized in {"global", "partial_global"}:
        return True

    if coverage_normalized in {"oecd_members", "oecd", "oecd_plus"}:
        return all(CountryResolver.is_oecd_member(c) for c in countries)

    if coverage_normalized in {"eu_members", "eu"}:
        return all(CountryResolver.is_eu_member(c) for c in countries)

    if coverage_normalized in {"us_only", "us"}:
        return all((_to_iso2(c) or str(c).upper()) == "US" for c in countries)

    if coverage_normalized == "44_countries":
        try:
            from ..providers.bis import BISProvider
            return all((_to_iso2(c) or str(c).upper()) in BISProvider.BIS_SUPPORTED_COUNTRIES for c in countries)
        except Exception:
            # Fail open for coverage hints when BIS module is unavailable.
            return True

    return False


def _coverage_preference_bonus(coverage: Any, countries: Optional[List[str]]) -> float:
    """
    Apply a ranking bonus based on coverage specificity.

    When country context IS provided, prefer country-specific providers
    (e.g., StatsCan for Canada, FRED for US, Eurostat for EU members)
    over global providers. Country-specific sources typically offer
    higher frequency, more timely data, and better coverage.

    Without geography, prefer global providers as safer defaults.
    """
    if countries:
        # When we know the target country, prefer specific providers
        if isinstance(coverage, list):
            normalized_coverage = {
                (CountryResolver.normalize(c) or str(c).upper())
                for c in coverage if c
            }
            normalized_countries = {
                (CountryResolver.normalize(c) or str(c).upper())
                for c in countries if c
            }
            # Exact country match: provider is specialized for this country
            if normalized_coverage and normalized_countries <= normalized_coverage:
                return 0.10  # Strong bonus for country-specific providers
        elif isinstance(coverage, str):
            cov_lower = coverage.split("#", 1)[0].strip().lower()
            if cov_lower in {"global", "partial_global"}:
                return 0.0  # No bonus for global when country is known
            if cov_lower in {"eu_members", "eurozone"} and countries:
                from ..routing.country_resolver import CountryResolver as CR
                if all(CR.is_eu_member(c) for c in countries if c):
                    return 0.06  # Bonus for EU-specific when querying EU countries
        return 0.0

    if isinstance(coverage, list):
        normalized_items = {
            (CountryResolver.normalize(c) or str(c).upper())
            for c in coverage
            if c
        }
        coverage_count = len(normalized_items)
        if coverage_count <= 1:
            return -0.08
        if coverage_count <= 5:
            return -0.04
        return 0.0

    if isinstance(coverage, str):
        coverage_normalized = coverage.split("#", 1)[0].strip().lower()
    else:
        coverage_normalized = str(coverage or "").strip().lower()

    if coverage_normalized in {"global", "partial_global"}:
        return 0.08
    if coverage_normalized == "44_countries":
        return 0.04
    if coverage_normalized in {"us_only", "us"}:
        return -0.08

    return 0.0


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
    best_score = float("-inf")

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
        ranking_score = confidence + _coverage_preference_bonus(coverage, countries)
        if ranking_score > best_score:
            best_provider = provider_name
            best_code = code
            best_confidence = confidence
            best_score = ranking_score

    return best_provider, best_code, best_confidence


def get_variant_for_query(
    concept_name: str,
    provider: str,
    query: str,
    countries: Optional[List[str]] = None,
) -> Tuple[Optional[str], float]:
    """
    Select the best catalog variant for a provider based on query discriminators.

    When the user asks for e.g. "GDP per capita PPP", the primary indicator
    (GDP per capita, current US$) doesn't match. This function looks through
    named variants (ppp, growth, ppp_constant, etc.) in the catalog YAML
    and returns the variant whose name/key best matches the query.

    Args:
        concept_name: The catalog concept (e.g., "gdp_per_capita")
        provider: The provider name (e.g., "WorldBank")
        query: The original user query (e.g., "GDP per capita PPP India")
        countries: Optional country context for coverage checking

    Returns:
        Tuple of (indicator_code, confidence) or (None, 0.0) if no variant matches
    """
    concept = get_concept(concept_name)
    if not concept:
        return None, 0.0

    providers = concept.get("providers", {})
    not_available = concept.get("not_available", [])

    # Case-insensitive provider lookup
    providers_lower = {p.lower(): p for p in providers.keys()}
    not_available_lower = [p.lower() for p in not_available]

    prov_lower = provider.lower()
    if prov_lower in not_available_lower or prov_lower not in providers_lower:
        return None, 0.0

    actual_name = providers_lower[prov_lower]
    provider_info = providers[actual_name]
    if not isinstance(provider_info, dict):
        return None, 0.0

    query_lower = query.lower()

    # Discriminator keywords to match against variant keys and names.
    # Maps query terms to variant key patterns.
    _VARIANT_DISCRIMINATORS = {
        "ppp": ["ppp"],
        "purchasing power": ["ppp"],
        "constant": ["constant", "real"],
        "real": ["constant", "real"],
        "nominal": ["nominal", "current"],
        "growth": ["growth"],
        "per capita": ["per_capita", "percapita"],
        "net": ["net"],
        "gross": ["gross"],
        "total": ["total"],
        # Frequency discriminators (cycle 27 fix): when user specifies a
        # frequency, the variant lookup should match it.
        "monthly": ["monthly", "month", "_m", "_m_"],
        "quarterly": ["quarterly", "quarter", "_q", "_q_"],
        "weekly": ["weekly", "week", "_w", "_w_"],
        "daily": ["daily", "day", "_d", "_d_"],
        "annual": ["annual", "annually", "yearly", "_a", "_a_"],
        # Inflation variant discriminators (cycle 29 fix):
        # "core CPI", "core PCE", etc. should hit specific variants.
        # Note: "all items" intentionally NOT a pattern for "headline"
        # because the core CPI series name ("Core CPI: All Items Less Food
        # and Energy") contains it, causing wrong matches.
        "core": ["core", "less food and energy", "excluding food"],
        "pce": ["pce", "personal consumption expenditures"],
        "headline": ["headline"],
        "brent": ["brent"],
        "wti": ["wti", "west texas intermediate"],
        "west texas intermediate": ["wti", "west texas intermediate"],
    }

    # Get primary variant info to exclude discriminators that already
    # appear in the primary. This prevents "per capita" from triggering
    # variant selection when the primary is already "GDP per capita".
    primary_info = provider_info.get("primary", {})
    primary_name_lower = ""
    primary_key_lower = "primary"
    if isinstance(primary_info, dict):
        primary_name_lower = (primary_info.get("name") or "").lower()

    # Find which discriminators appear in the query but NOT in the primary
    query_discs = []
    for disc_term, variant_keys in _VARIANT_DISCRIMINATORS.items():
        if disc_term not in query_lower:
            continue
        # Skip discriminators already satisfied by the primary variant
        primary_combined = f"{primary_key_lower} {primary_name_lower}"
        disc_in_primary = (
            disc_term in primary_name_lower
            or any(pat in primary_combined for pat in variant_keys)
        )
        if disc_in_primary:
            continue
        query_discs.append((disc_term, variant_keys))

    if not query_discs:
        return None, 0.0

    # Score each variant (skip "primary" and non-dict entries like "alternatives")
    best_code = None
    best_confidence = 0.0
    best_match_count = 0

    for variant_key, variant_data in provider_info.items():
        if variant_key == "primary":
            continue
        if not isinstance(variant_data, dict):
            continue
        code = variant_data.get("code")
        if not code:
            continue

        # Check coverage
        coverage = variant_data.get("coverage", "global")
        if not _check_coverage(coverage, countries):
            continue

        # Score: how many query discriminators does this variant match?
        variant_key_lower = variant_key.lower()
        variant_name_lower = (variant_data.get("name") or "").lower()
        combined = f"{variant_key_lower} {variant_name_lower}"

        match_count = 0
        for disc_term, variant_patterns in query_discs:
            for pattern in variant_patterns:
                if pattern in combined or disc_term in variant_name_lower:
                    match_count += 1
                    break

        if match_count > best_match_count:
            best_match_count = match_count
            best_code = code
            best_confidence = variant_data.get("confidence", 0.85)

    if best_code:
        logger.info(
            "📋 Catalog variant match: concept=%s provider=%s variant_code=%s "
            "(matched %d/%d discriminators)",
            concept_name, provider, best_code, best_match_count, len(query_discs),
        )

    return best_code, best_confidence


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


def get_default_indicator(concept: str, provider: str) -> Optional[str]:
    """
    Get the default indicator code for a concept and provider.

    This provides backward compatibility with indicator_synonyms.py interface.
    """
    return get_indicator_code(concept, provider, "primary")
