"""
Unified Indicator Resolver Service

THE SINGLE ENTRY POINT for all indicator resolution across econ-data-mcp.

This service consolidates:
1. IndicatorLookup (FTS5 search over 330K+ indicators)
2. IndicatorTranslator (cross-provider translation, IMF codes, fuzzy matching)
3. CatalogService (concept definitions from YAML files)

Usage:
    from backend.services.indicator_resolver import get_indicator_resolver

    resolver = get_indicator_resolver()

    # Resolve an indicator query to provider-specific code
    result = resolver.resolve("US GDP growth", provider="FRED")

    # Find best provider for a concept
    provider, code, confidence = resolver.find_best_match("unemployment rate", country="US")

    # Translate between providers
    code = resolver.translate("NGDP_RPCH", from_provider="IMF", to_provider="FRED")

Author: econ-data-mcp Development Team
Date: 2025-12-27
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .indicator_lookup import IndicatorLookup, get_indicator_lookup
from .indicator_translator import IndicatorTranslator, get_indicator_translator
from .catalog_service import (
    find_concept_by_term,
    get_indicator_code,
    get_best_provider,
    is_provider_available,
)

logger = logging.getLogger(__name__)


@dataclass
class ResolvedIndicator:
    """Result of indicator resolution."""
    code: str
    provider: str
    name: str
    confidence: float
    source: str  # 'database', 'translator', 'catalog', or 'fallback'
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class IndicatorResolver:
    """
    Unified indicator resolution service.

    Resolution priority:
    1. Exact code match in database (highest confidence)
    2. FTS5 search in indicator database (330K+ indicators)
    3. IndicatorTranslator universal concepts
    4. CatalogService YAML definitions
    5. Provider-specific API search (fallback)

    This consolidates all indicator resolution logic into a single service,
    eliminating duplicate mappings across providers.
    """

    def __init__(
        self,
        lookup: Optional[IndicatorLookup] = None,
        translator: Optional[IndicatorTranslator] = None,
    ):
        self.lookup = lookup or get_indicator_lookup()
        self.translator = translator or get_indicator_translator()
        self._cache: Dict[str, ResolvedIndicator] = {}
        self._stop_words: Set[str] = {
            "the", "a", "an", "of", "for", "in", "to", "and", "or",
            "show", "get", "find", "data", "series", "indicator", "rate",
            "index", "value", "values", "percent", "percentage",
            "country", "countries", "from", "with", "by", "on", "at",
        }

    def resolve(
        self,
        query: str,
        provider: Optional[str] = None,
        country: Optional[str] = None,
        use_cache: bool = True,
    ) -> Optional[ResolvedIndicator]:
        """
        Resolve a natural language query or code to a provider-specific indicator.

        Args:
            query: Indicator name, code, or description
            provider: Target provider (FRED, WorldBank, IMF, etc.)
            country: Country context for provider selection
            use_cache: Whether to use cached results

        Returns:
            ResolvedIndicator with code, provider, and confidence, or None
        """
        if not query:
            return None

        # Check cache
        cache_key = f"{provider or 'any'}:{query.lower()}"
        if use_cache and cache_key in self._cache:
            logger.debug(f"Cache hit for indicator: {query}")
            return self._cache[cache_key]

        # Try resolution methods in priority order
        result = None

        # 1. Try exact code match in database
        if provider:
            exact = self.lookup.get(provider, query.upper())
            if exact:
                result = ResolvedIndicator(
                    code=exact.get("code", query.upper()),
                    provider=provider,
                    name=exact.get("name", query),
                    confidence=1.0,
                    source="database",
                    metadata=exact,
                )

        # 2. INFRASTRUCTURE FIX: Check IndicatorTranslator BEFORE FTS5 search
        # This ensures curated universal concepts (like consumer_credit -> TOTALSL)
        # take priority over raw database matches (which may include discontinued series)
        if not result:
            try:
                translated = self.translator.translate_indicator(query, target_provider=provider or "FRED")
                if translated and translated[0]:
                    code, concept_name = translated
                    result = ResolvedIndicator(
                        code=code,
                        provider=provider or "FRED",
                        name=concept_name or query,
                        confidence=0.75,  # Good confidence for curated mappings
                        source="translator",
                    )
                    logger.debug(f"Translator match: {query} -> {code}")
            except Exception as e:
                logger.debug(f"Translator lookup failed: {e}")

        # 3. Try FTS5 search in database (fallback for terms not in translator)
        if not result:
            search_results = self.lookup.search(query, provider=provider, limit=5)
            if search_results:
                best, best_confidence = self._pick_best_search_result(query, search_results)
                if best and best_confidence >= 0.35:
                    result = ResolvedIndicator(
                        code=best.get("code"),
                        provider=best.get("provider", provider),
                        name=best.get("name", query),
                        confidence=best_confidence,
                        source="database",
                        metadata=best,
                    )
                elif best:
                    logger.info(
                        "Rejecting low-confidence FTS match for '%s': %s (conf=%.2f)",
                        query,
                        best.get("code"),
                        best_confidence,
                    )

        # 4. Try IndicatorTranslator again if FTS5 returned low confidence
        if result and result.confidence < 0.7 and result.source == "database":
            try:
                translated = self.translator.translate_indicator(query, target_provider=provider or "FRED")
                if translated and translated[0]:
                    code, concept_name = translated
                    # Use a reasonable confidence score for translator results
                    trans_confidence = 0.75
                    # Only use if better than current result
                    if not result or trans_confidence > result.confidence:
                        result = ResolvedIndicator(
                            code=code,
                            provider=provider or "FRED",
                            name=concept_name or query,
                            confidence=trans_confidence,
                            source="translator",
                        )
            except Exception as e:
                logger.debug(f"IndicatorTranslator failed: {e}")

        # 4. Try CatalogService
        if not result or (result and result.confidence < 0.6):
            concept = find_concept_by_term(query)
            if concept:
                if provider and is_provider_available(concept, provider):
                    code = get_indicator_code(concept, provider)
                    if code:
                        result = ResolvedIndicator(
                            code=code,
                            provider=provider,
                            name=concept.replace("_", " ").title(),
                            confidence=0.85,
                            source="catalog",
                        )
                elif not provider:
                    # Find best provider for concept
                    best_provider, code, confidence = get_best_provider(
                        concept, [country] if country else None
                    )
                    if best_provider and code:
                        result = ResolvedIndicator(
                            code=code,
                            provider=best_provider,
                            name=concept.replace("_", " ").title(),
                            confidence=confidence,
                            source="catalog",
                        )

        # Cache successful result
        if result and use_cache:
            self._cache[cache_key] = result

        return result

    def find_best_match(
        self,
        query: str,
        country: Optional[str] = None,
        preferred_providers: Optional[List[str]] = None,
    ) -> Optional[Tuple[str, str, float]]:
        """
        Find the best provider and indicator code for a query.

        Args:
            query: Natural language query
            country: Country for routing decisions
            preferred_providers: List of preferred providers to try first

        Returns:
            Tuple of (provider, code, confidence) or None
        """
        # Try preferred providers first
        if preferred_providers:
            for provider in preferred_providers:
                result = self.resolve(query, provider=provider, country=country)
                if result and result.confidence >= 0.7:
                    return (result.provider, result.code, result.confidence)

        # Try without provider restriction
        result = self.resolve(query, country=country)
        if result:
            return (result.provider, result.code, result.confidence)

        # Fall back to indicator lookup's find_best_provider
        best = self.lookup.find_best_provider(query, country, preferred_providers)
        if best:
            return best

        return None

    def translate(
        self,
        code: str,
        from_provider: Optional[str] = None,
        to_provider: str = None,
    ) -> Optional[str]:
        """
        Translate an indicator code from one provider to another.

        Args:
            code: Source indicator code
            from_provider: Source provider (optional, will auto-detect)
            to_provider: Target provider

        Returns:
            Translated indicator code or None
        """
        if not to_provider:
            return None

        # Try translator first
        try:
            result = self.translator.translate_indicator(code, target_provider=to_provider)
            if result and result[0]:
                translated_code, _ = result
                return translated_code
        except Exception as e:
            logger.debug(f"IndicatorTranslator translation failed: {e}")

        # Try finding concept via catalog service and getting target provider's code
        try:
            concept = find_concept_by_term(code)
            if concept:
                target_code = get_indicator_code(concept, to_provider)
                if target_code:
                    return target_code
        except Exception as e:
            logger.debug(f"Concept lookup failed: {e}")

        return None

    def get_alternatives(
        self,
        indicator: str,
        provider: str,
        limit: int = 5,
    ) -> List[ResolvedIndicator]:
        """
        Get alternative indicators for fallback when primary fails.

        This is the INFRASTRUCTURE mechanism for handling archived/unavailable
        indicators - it provides alternatives that can be tried automatically.

        Args:
            indicator: Primary indicator name or code
            provider: Target provider
            limit: Maximum alternatives to return

        Returns:
            List of alternative ResolvedIndicator options
        """
        alternatives = []

        # Search for related indicators
        search_results = self.lookup.search(indicator, provider=provider, limit=limit + 1)

        for result in search_results:
            code = result.get("code")
            if code and code.upper() != indicator.upper():
                alternatives.append(ResolvedIndicator(
                    code=code,
                    provider=provider,
                    name=result.get("name", indicator),
                    confidence=result.get("score", 0.7),
                    source="database",
                    metadata=result,
                ))
                if len(alternatives) >= limit:
                    break

        return alternatives

    def clear_cache(self):
        """Clear the resolution cache."""
        self._cache.clear()
        logger.info("Indicator resolution cache cleared")

    def _tokenize_terms(self, text: str) -> Set[str]:
        """Tokenize text into normalized terms for lexical matching."""
        if not text:
            return set()
        terms = set(re.findall(r"[a-z0-9]+", text.lower()))
        return {t for t in terms if len(t) > 1 and t not in self._stop_words}

    def _score_search_match(self, query: str, candidate: Dict[str, Any], rank_index: int = 0) -> float:
        """
        Score a candidate search result on a 0-1 relevance scale.

        This avoids using raw FTS ranking scores directly, which are unbounded and
        not true confidence probabilities.
        """
        query_text = (query or "").strip().lower()
        code = str(candidate.get("code") or "")
        name = str(candidate.get("name") or "")
        description = str(candidate.get("description") or "")

        # Exact code queries should resolve with maximum confidence.
        if code and query_text == code.lower():
            return 1.0

        query_terms = self._tokenize_terms(query_text)
        candidate_terms = self._tokenize_terms(f"{name} {code} {description}")

        if not query_terms:
            return 0.0

        overlap_count = len(query_terms & candidate_terms)
        overlap_ratio = overlap_count / max(len(query_terms), 1)

        phrase_bonus = 0.0
        name_lower = name.lower()
        if query_text and query_text in name_lower:
            phrase_bonus += 0.2
        if query_text and code and query_text in code.lower():
            phrase_bonus += 0.15

        # Slightly favor higher-ranked FTS results while keeping lexical fit primary.
        rank_bonus = max(0.0, 0.1 - (rank_index * 0.02))

        confidence = 0.1 + (0.75 * overlap_ratio) + phrase_bonus + rank_bonus

        # Strongly penalize candidates with zero lexical overlap.
        if overlap_count == 0 and (query_text not in name_lower) and (query_text not in code.lower()):
            confidence *= 0.2

        return max(0.0, min(1.0, confidence))

    def _pick_best_search_result(
        self,
        query: str,
        search_results: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], float]:
        """Select best search result using lexical confidence scoring."""
        best_result: Optional[Dict[str, Any]] = None
        best_confidence = 0.0

        for idx, candidate in enumerate(search_results):
            confidence = self._score_search_match(query, candidate, rank_index=idx)
            if confidence > best_confidence:
                best_result = candidate
                best_confidence = confidence

        return best_result, best_confidence


# Singleton instance
_resolver_instance: Optional[IndicatorResolver] = None


def get_indicator_resolver() -> IndicatorResolver:
    """Get the singleton IndicatorResolver instance."""
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = IndicatorResolver()
        logger.info("IndicatorResolver initialized")
    return _resolver_instance


def resolve_indicator(
    query: str,
    provider: Optional[str] = None,
    country: Optional[str] = None,
) -> Optional[ResolvedIndicator]:
    """
    Convenience function to resolve an indicator.

    Args:
        query: Indicator name, code, or description
        provider: Target provider
        country: Country context

    Returns:
        ResolvedIndicator or None
    """
    return get_indicator_resolver().resolve(query, provider, country)
