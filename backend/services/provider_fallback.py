"""Provider fallback logic for when a primary data provider fails.

Extracted from query.py to reduce file size and isolate the fallback
orchestration into a testable, reusable module.

This module provides:
- Structural fallback provider ordering from provider relationships
- Semantic relevance validation of fallback results
- User-facing "no data" suggestion text
- LRU cache for fallback provider lookups
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..models import NormalizedData, ParsedIntent
from ..routing.country_resolver import CountryResolver
from ..services.relevance_scorer import extract_indicator_cues

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider country coverage (standalone — no instance state needed)
# ---------------------------------------------------------------------------

def normalize_country_to_iso2(country: Optional[str]) -> Optional[str]:
    """Normalize country identifiers/names to ISO2 codes when possible."""
    if not country:
        return None

    country_text = str(country).strip()
    if not country_text:
        return None

    normalized = CountryResolver.normalize(country_text)
    if normalized:
        return normalized

    simplified = re.sub(r"[^a-z0-9]+", " ", country_text.lower()).strip()
    if simplified:
        normalized = CountryResolver.normalize(simplified)
        if normalized:
            return normalized

    worldbank_style_aliases = {
        "korea rep": "KR",
        "korea rep of": "KR",
        "iran isl rep": "IR",
        "egypt arab rep": "EG",
    }
    if simplified in worldbank_style_aliases:
        return worldbank_style_aliases[simplified]

    # Allow ISO3 inputs (e.g., GBR) and normalize to ISO2 when known.
    iso2 = CountryResolver.to_iso2(country_text.upper())
    if iso2:
        return iso2

    return None


def provider_covers_country_list(provider: str, countries: Optional[List[str]]) -> bool:
    """Check whether a provider can plausibly cover all requested countries."""
    if not countries:
        return True

    from .query import normalize_provider_name

    provider_upper = normalize_provider_name(provider)
    normalized_iso2 = [
        normalize_country_to_iso2(country) or str(country).upper()
        for country in countries
        if country
    ]
    if not normalized_iso2:
        return True

    if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
        return all(code == "CA" for code in normalized_iso2)
    if provider_upper == "FRED":
        return all(code == "US" for code in normalized_iso2)
    if provider_upper == "EUROSTAT":
        return all(
            code in {"EU", "EA", "EA19", "EA20", "EU27_2020"} or CountryResolver.is_eu_member(code)
            for code in normalized_iso2
        )
    if provider_upper == "OECD":
        return all(
            CountryResolver.is_oecd_member(code)
            for code in normalized_iso2
        )
    return True


# ---------------------------------------------------------------------------
# Fallback provider ordering
# ---------------------------------------------------------------------------

# Maximum entries in the LRU fallback-provider cache (per handler instance).
MAX_FALLBACK_CACHE_ENTRIES = 1024


def get_fallback_providers(
    primary_provider: str,
    unified_router: Any,
    fallback_cache: "OrderedDict[Tuple[str, Tuple[str, ...]], List[str]]",
    *,
    indicator: Optional[str] = None,
    country: Optional[str] = None,
    countries: Optional[List[str]] = None,
    max_cache_entries: int = MAX_FALLBACK_CACHE_ENTRIES,
) -> List[str]:
    """Return an ordered list of fallback providers for a given primary provider.

    Uses provider relationship fallbacks only. The optional indicator argument is
    retained for API compatibility and cache separation, but it must not trigger
    retired concept-shortcut semantic provider selection.

    Args:
        primary_provider: The primary provider that failed.
        unified_router: ``UnifiedRouter`` instance (provides ``get_fallbacks``).
        fallback_cache: Shared ``OrderedDict`` LRU cache (mutated in-place).
        indicator: Optional indicator name for smarter fallbacks.
        country: Optional single-country context.
        countries: Optional multi-country context.
        max_cache_entries: Maximum entries in the LRU cache.

    Returns:
        List of fallback provider names to try in order.
    """
    from .query import normalize_provider_name

    primary_upper = primary_provider.upper()
    normalized_geo = tuple(
        sorted({
            normalize_country_to_iso2(str(c)) or str(c).strip().upper()
            for c in [*(countries or []), country]
            if c
        })
    )
    cache_key: Optional[Tuple[str, Tuple[str, ...]]] = (primary_upper, normalized_geo)
    cached = fallback_cache.get(cache_key)
    if cached:
        # LRU refresh on read.
        fallback_cache.move_to_end(cache_key)
        return list(cached)

    fallback_list: List[str] = []
    try:
        fallback_list = [
            normalize_provider_name(provider_name)
            for provider_name in unified_router.get_fallbacks(primary_upper)
        ]
    except Exception as exc:
        logger.debug("UnifiedRouter fallback lookup failed for %s: %s", primary_upper, exc)

    # Ensure deterministic fallback list has no duplicates and excludes primary.
    fallback_list = [
        provider_name
        for provider_name in dict.fromkeys(fallback_list)
        if provider_name and provider_name != primary_upper
    ]

    context_countries = [str(c) for c in (countries or []) if c]
    if country and str(country) not in context_countries:
        context_countries.append(str(country))

    # --- Helper: persist *result* into the LRU cache. ---
    def _cache_and_return(result: List[str]) -> List[str]:
        if cache_key:
            fallback_cache[cache_key] = result
            fallback_cache.move_to_end(cache_key)
            while len(fallback_cache) > max_cache_entries:
                fallback_cache.popitem(last=False)
        return result

    if context_countries:
        fallback_list = [
            provider_name
            for provider_name in fallback_list
            if provider_covers_country_list(provider_name, context_countries)
        ]

    return _cache_and_return(fallback_list)


# ---------------------------------------------------------------------------
# No-data suggestion text
# ---------------------------------------------------------------------------

# Provider-specific suggestions (static data, no instance state).
_PROVIDER_SUGGESTIONS: Dict[str, List[str]] = {
    "IMF": [
        "**Try alternative providers**: World Bank or OECD may have similar data.",
        "**Check country coverage**: IMF may not have data for all countries.",
        "**Historical data**: IMF primarily provides recent economic indicators.",
    ],
    "BIS": [
        "**Try alternative providers**: World Bank or FRED may have property/credit data.",
        "**Check coverage**: BIS focuses on property prices, credit, and banking data.",
        "**Supported countries**: BIS covers ~60 major economies.",
    ],
    "OECD": [
        "**Try alternative providers**: World Bank has broader country coverage.",
        "**OECD members only**: OECD data primarily covers member countries.",
        "**Check indicator name**: OECD uses specific indicator codes.",
    ],
    "EUROSTAT": [
        "**EU countries only**: Eurostat covers EU member states.",
        "**Try World Bank**: For broader European or global data.",
        "**Check indicator**: Eurostat uses specific dataset codes.",
    ],
    "COMTRADE": [
        "**Check country codes**: UN Comtrade uses ISO3 country codes.",
        "**Trade data availability**: Recent years may not be available yet.",
        "**Partner regions**: Some regions like 'Asia' or 'Africa' need individual countries.",
    ],
    "STATSCAN": [
        "**Canada only**: Statistics Canada covers Canadian data.",
        "**Try World Bank**: For Canadian data with global comparison.",
        "**Check indicator**: StatsCan uses specific table/vector IDs.",
    ],
    "WORLDBANK": [
        "**Check indicator code**: World Bank uses specific indicator codes (e.g., NY.GDP.MKTP.CD).",
        "**Regional data**: Try using region names like 'South Asia' or 'Sub-Saharan Africa'.",
        "**Data lag**: Some indicators have 1-2 year reporting delays.",
    ],
    "FRED": [
        "**US data focus**: FRED primarily covers US economic data.",
        "**Try World Bank**: For non-US countries.",
        "**Series ID**: Check if the FRED series ID is correct.",
    ],
    "COINGECKO": [
        "**Check coin ID**: Use correct cryptocurrency IDs (e.g., 'bitcoin', 'ethereum').",
        "**Historical data**: Some coins may have limited history.",
        "**Try alternative coins**: Check CoinGecko for available cryptocurrencies.",
    ],
    "EXCHANGERATE": [
        "**Currency codes**: Use ISO currency codes (e.g., USD, EUR, GBP).",
        "**Supported currencies**: Covers 161 major currencies.",
        "**Try FRED**: For major currency pairs with longer history.",
    ],
}

_DEFAULT_SUGGESTIONS = [
    "**Try a different provider**: The data may be available from another source.",
    "**Check spelling**: Ensure country and indicator names are correct.",
    "**Simplify query**: Try a more specific or simpler query.",
]


def get_no_data_suggestions(
    provider: str,
    intent: ParsedIntent,
    *,
    fallback_providers_fn: Callable[..., List[str]],
) -> str:
    """Generate helpful suggestions when no data is found.

    Args:
        provider: The provider that returned no data.
        intent: The parsed intent with query details.
        fallback_providers_fn: Callable that returns fallback providers for
            a given primary provider (used to append alternative hint).

    Returns:
        String with helpful suggestions for the user.
    """
    from .query import normalize_provider_name

    provider_upper = normalize_provider_name(provider)
    suggestions: List[str] = []

    base_suggestions = _PROVIDER_SUGGESTIONS.get(provider_upper, _DEFAULT_SUGGESTIONS)

    suggestions.append("**Suggestions:**")
    for i, s in enumerate(base_suggestions[:3], 1):
        suggestions.append(f"{i}. {s}")

    # Add fallback provider hint
    fallbacks = fallback_providers_fn(provider_upper)
    if fallbacks:
        suggestions.append(f"\n**Alternative providers to try**: {', '.join(fallbacks)}")

    return "\n".join(suggestions)


# ---------------------------------------------------------------------------
# Fallback relevance validation
# ---------------------------------------------------------------------------

# Pre-computed geography terms for filtering (built once at module load).
_GEO_TERMS: set[str] = set()
for _alias in CountryResolver.COUNTRY_ALIASES.keys():
    for _token in re.findall(r"[a-z0-9]+", str(_alias or "").lower()):
        if len(_token) >= 2:
            _GEO_TERMS.add(_token)
for _code in CountryResolver.COUNTRY_ALIASES.values():
    _tok = str(_code or "").strip().lower()
    if _tok:
        _GEO_TERMS.add(_tok)

# Semantic category sets (module-level constants).
_SUBJECT_ENTITIES = frozenset({
    'corporation', 'corporations', 'corporate', 'company', 'companies',
    'nonfinancial', 'nonfin', 'nfc',  # non-financial corporations
    'government', 'public', 'fiscal', 'general',
    'household', 'households', 'consumer', 'consumers',
    'bank', 'banks', 'banking', 'financial', 'mfi',
    'business', 'businesses', 'enterprise', 'enterprises',
    'private', 'sector',
})

_METRIC_TYPES = frozenset({
    'assets', 'liabilities', 'debt', 'income', 'expenditure',
    'revenue', 'expense', 'expenses', 'balance', 'equity',
    'gdp', 'gnp', 'unemployment', 'inflation', 'cpi', 'ppi',
    'trade', 'exports', 'imports', 'deficit', 'surplus',
    'investment', 'consumption', 'savings', 'production',
    'employment', 'wages', 'salaries', 'output', 'growth',
})

_METRIC_QUALIFIERS = frozenset({
    'fixed', 'current', 'liquid', 'tangible', 'intangible',
    'gross', 'net', 'total', 'real', 'nominal',
})

_STOP_WORDS = frozenset({
    'data', 'statistics', 'annual', 'quarterly', 'monthly',
    'index', 'rate', 'by', 'and', 'the', 'of', 'for', 'in', 'to',
    'a', 'an', 'all', 'from', 'with', 'as', 'at', 'show', 'plot',
    'get', 'find', 'display', 'chart', 'graph', 'value', 'values',
    'economic', 'activity', 'activities',
    'trend', 'trends', 'historical', 'history', 'before', 'after',
    'between', 'versus', 'vs', 'across', 'compare', 'comparison',
    'contrast', 'since', 'last', 'past', 'latest',
})

_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _extract_key_terms(text: str) -> set[str]:
    """Tokenize *text* into semantically meaningful terms.

    Strips stop words, geography, pure digits, and year tokens.
    """
    terms: set[str] = set()
    for clean in re.findall(r"[a-z0-9]+", text.lower().replace('-', ' ').replace('_', ' ')):
        if not clean:
            continue
        if clean.isdigit():
            continue
        if _YEAR_RE.fullmatch(clean):
            continue
        if clean in _GEO_TERMS:
            continue
        if len(clean) > 2 and clean not in _STOP_WORDS:
            terms.add(clean)
    return terms


def _get_canonical_subject(terms: set[str]) -> set[str]:
    """Map subject entity terms to canonical categories."""
    canonical: set[str] = set()
    if terms & {'corporation', 'corporations', 'corporate', 'company', 'companies', 'nfc'}:
        canonical.add('corporation')
    if terms & {'government', 'public', 'fiscal', 'general'}:
        canonical.add('government')
    if terms & {'household', 'households', 'consumer', 'consumers'}:
        canonical.add('household')
    if terms & {'bank', 'banks', 'banking', 'mfi'}:
        canonical.add('bank')
    if terms & {'nonfinancial', 'nonfin'}:
        canonical.add('nonfinancial')
    if terms & {'financial'} and 'nonfinancial' not in terms and 'non' not in terms:
        canonical.add('financial')
    return canonical


def is_fallback_relevant(
    original_indicators: List[str],
    fallback_result: List[NormalizedData],
    target_countries: Optional[List[str]] = None,
    original_query: Optional[str] = None,
) -> bool:
    """Check if a fallback result is semantically related to the original query.

    Prevents returning completely unrelated data when fallback providers find
    something with vaguely similar keywords but different meaning.

    The check separates SUBJECT entities (corporations, government, households)
    from METRIC types (assets, debt, income).  If the original query specifies
    a subject, the result must match that subject -- not just any overlapping term.

    Also validates COUNTRY matching to prevent returning data for a different
    country than requested.

    Args:
        original_indicators: Original indicator names from user query.
        fallback_result: Data returned from fallback provider.
        target_countries: Optional countries the query is targeting.
        original_query: Optional raw user query text.

    Returns:
        ``True`` if fallback data is relevant, ``False`` otherwise.
    """
    if not fallback_result or not original_indicators:
        return False

    # Country validation (generalized): enforce match for known ISO2 country contexts.
    requested_iso2 = {
        iso2
        for iso2 in (
            normalize_country_to_iso2(country)
            for country in (target_countries or [])
        )
        if iso2
    }
    if requested_iso2:
        saw_normalized_country = False
        matched_requested_country = False
        returned_iso2: set[str] = set()
        for data in fallback_result:
            if not data.metadata or not data.metadata.country:
                continue

            result_country = data.metadata.country
            result_iso2 = normalize_country_to_iso2(result_country)
            if not result_iso2:
                continue

            saw_normalized_country = True
            returned_iso2.add(result_iso2)
            if result_iso2 in requested_iso2:
                matched_requested_country = True
                continue

            logger.warning(
                "Fallback rejected: country mismatch - requested=%s got=%s",
                sorted(requested_iso2),
                result_country,
            )
            return False

        if saw_normalized_country and not matched_requested_country:
            logger.warning(
                "Fallback rejected: none of the fallback result countries matched requested=%s",
                sorted(requested_iso2),
            )
            return False

        if len(requested_iso2) >= 2 and saw_normalized_country:
            missing_iso2 = requested_iso2 - returned_iso2
            if missing_iso2:
                logger.warning(
                    "Fallback rejected: incomplete country coverage requested=%s returned=%s missing=%s",
                    sorted(requested_iso2),
                    sorted(returned_iso2),
                    sorted(missing_iso2),
                )
                return False

    # Get terms from original indicators + query text (when available)
    # so generic parsed indicators like "trade" still preserve directionality
    # from the original user phrasing ("imports", "exports", etc.).
    original_text = " ".join(
        part for part in [
            ' '.join(original_indicators).lower(),
            str(original_query or "").lower(),
        ] if part
    )
    original_terms = _extract_key_terms(original_text)
    original_cues = extract_indicator_cues(original_text)
    high_signal_original_cues = {
        cue for cue in original_cues
        if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    }

    if not original_terms:
        return True  # Can't validate, accept fallback

    # High-signal cue guardrail: fallback must preserve at least one key cue
    # (for example, debt_gdp_ratio, bond_yield, import/export direction).
    if high_signal_original_cues:
        cue_overlap_found = False
        for data in fallback_result:
            if not data.metadata:
                continue
            candidate_text = " ".join(
                [
                    str(data.metadata.indicator or ""),
                    str(data.metadata.seriesId or ""),
                    str(data.metadata.description or ""),
                ]
            )
            candidate_cues = extract_indicator_cues(candidate_text)
            if high_signal_original_cues & candidate_cues:
                cue_overlap_found = True
                break
        if not cue_overlap_found:
            logger.warning(
                "Fallback rejected: high-signal cue mismatch original=%s",
                sorted(high_signal_original_cues),
            )
            return False

    # Extract subjects and metrics from original
    original_subjects = original_terms & _SUBJECT_ENTITIES
    original_metrics = original_terms & _METRIC_TYPES
    original_qualifiers = original_terms & _METRIC_QUALIFIERS

    # Check each result for relevance
    for data in fallback_result:
        if not data.metadata:
            continue

        result_text = (data.metadata.indicator or "").lower()
        result_text = " ".join(
            [
                result_text,
                str(data.metadata.seriesId or "").lower(),
                str(data.metadata.description or "").lower(),
            ]
        ).strip()
        result_terms = _extract_key_terms(result_text)

        # Extract subjects and metrics from result
        result_subjects = result_terms & _SUBJECT_ENTITIES
        result_metrics = result_terms & _METRIC_TYPES
        result_qualifiers = result_terms & _METRIC_QUALIFIERS

        # CRITICAL CHECK 1: Subject entity matching
        # If original specifies a subject (e.g., corporations), result MUST have same subject
        if original_subjects:
            orig_canonical = _get_canonical_subject(original_subjects)
            result_canonical = _get_canonical_subject(result_subjects)

            # If original has specific subject but result doesn't match, reject
            if orig_canonical and not (orig_canonical & result_canonical):
                # Special case: if result has NO subject at all, it might be aggregate data
                if result_subjects:
                    logger.warning(
                        "Fallback rejected: original subject %s != result subject %s",
                        orig_canonical, result_canonical,
                    )
                    return False
                else:
                    # Result has no specific subject - might be too generic
                    logger.warning(
                        "Fallback rejected: original has subject %s but result has no specific subject",
                        orig_canonical,
                    )
                    return False

        # CRITICAL CHECK 2: Metric type matching with qualifier awareness
        # "total assets" vs "fixed assets" are different concepts
        if original_metrics and result_metrics:
            overlap_metrics = original_metrics & result_metrics
            if not overlap_metrics:
                trade_family = {'trade', 'imports', 'exports', 'deficit', 'surplus', 'balance'}
                if not ((original_metrics & trade_family) and (result_metrics & trade_family)):
                    logger.warning(
                        "Fallback rejected: metrics don't match - original=%s, result=%s",
                        original_metrics, result_metrics,
                    )
                    return False
            # Preserve import/export direction when explicitly present.
            if 'imports' in original_metrics and 'imports' not in result_metrics and 'trade' not in result_metrics:
                logger.warning(
                    "Fallback rejected: requested imports but result metric set was %s",
                    result_metrics,
                )
                return False
            if 'exports' in original_metrics and 'exports' not in result_metrics and 'trade' not in result_metrics:
                logger.warning(
                    "Fallback rejected: requested exports but result metric set was %s",
                    result_metrics,
                )
                return False

            # If both have same metric but different qualifiers, be cautious
            # e.g., "total assets" vs "fixed assets"
            if original_qualifiers and result_qualifiers:
                if original_qualifiers != result_qualifiers:
                    # Different qualifiers might mean different things
                    # Check if it's a significant difference
                    significant_diff = {'fixed', 'current', 'tangible', 'intangible'}
                    if (original_qualifiers & significant_diff) != (result_qualifiers & significant_diff):
                        logger.warning(
                            "Fallback rejected: metric qualifiers differ significantly - "
                            "original=%s, result=%s",
                            original_qualifiers, result_qualifiers,
                        )
                        return False

        # If we get here, check general term overlap
        overlap = original_terms & result_terms
        min_required = max(1, len(original_terms) * 0.3)  # At least 30% overlap
        if len(overlap) >= min_required:
            logger.info("Fallback accepted: sufficient overlap - %s", overlap)
            return True

    # Default: reject if no result passed the checks
    logger.warning("Fallback rejected: no result passed relevance checks")
    return False
