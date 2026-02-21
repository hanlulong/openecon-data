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
import os
import re
from dataclasses import dataclass
from collections import OrderedDict
from time import monotonic
from typing import Any, Dict, List, Optional, Set, Tuple

from .indicator_lookup import IndicatorLookup, get_indicator_lookup
from .indicator_translator import IndicatorTranslator, get_indicator_translator
from .vector_search import VECTOR_SEARCH_AVAILABLE, get_vector_search_service
from ..routing.country_resolver import CountryResolver
from .catalog_service import (
    find_concept_by_term,
    get_indicator_code,
    get_indicator_codes,
    get_all_synonyms,
    get_exclusions,
    get_best_provider,
    is_provider_available,
)

try:  # Optional dependency for typo-tolerant lexical matching
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - optional at runtime
    fuzz = None

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
        self._cache: OrderedDict[str, Tuple[float, ResolvedIndicator]] = OrderedDict()
        try:
            self._cache_max_entries = max(
                128,
                int(os.getenv("INDICATOR_RESOLVER_CACHE_SIZE", "4096")),
            )
        except ValueError:
            self._cache_max_entries = 4096
        try:
            self._cache_ttl_seconds = max(
                30,
                int(os.getenv("INDICATOR_RESOLVER_CACHE_TTL_SECONDS", "1800")),
            )
        except ValueError:
            self._cache_ttl_seconds = 1800
        self._stop_words: Set[str] = {
            "the", "a", "an", "of", "for", "in", "to", "and", "or",
            "show", "get", "find", "data", "series", "indicator", "rate",
            "index", "value", "values", "percent", "percentage",
            "country", "countries", "from", "with", "by", "on", "at",
        }
        # Context terms often appear in many indicators and are weak discriminators.
        self._context_terms: Set[str] = {
            "gdp", "ratio", "share", "rate", "index", "value", "values",
            "annual", "quarterly", "monthly", "yearly", "current", "constant",
            "real", "nominal", "level", "levels", "total", "overall", "change",
            "growth", "percent", "percentage",
        }
        self._directional_terms: Set[str] = {
            "export", "import", "saving", "debt", "inflation", "unemployment",
            "investment", "consumption", "productivity", "employment",
        }
        self._term_corrections: Dict[str, str] = {
            "ration": "ratio",
            "exprot": "export",
            "improt": "import",
            "savngs": "savings",
        }
        self._use_hybrid_rerank = os.getenv("USE_INDICATOR_HYBRID_RERANK", "true").strip().lower() not in {
            "0", "false", "no", "off",
        }
        try:
            self._rrf_k = max(1, int(os.getenv("INDICATOR_RRF_K", "60")))
        except ValueError:
            self._rrf_k = 60
        try:
            self._vector_candidate_limit = max(
                10,
                int(os.getenv("INDICATOR_VECTOR_CANDIDATES", "40")),
            )
        except ValueError:
            self._vector_candidate_limit = 40
        self._vector_search_service = None
        self._vector_search_checked = False
        # Remove country/location tokens from lexical matching so "in China and UK"
        # does not dilute indicator intent scoring.
        self._geo_terms: Set[str] = set()
        for alias in CountryResolver.COUNTRY_ALIASES.keys():
            term = str(alias).strip().lower()
            if not term or " " in term:
                continue
            if len(term) >= 3 or term in {"us", "uk", "eu", "uae"}:
                self._geo_terms.add(term)
        for iso_code in CountryResolver.COUNTRY_ALIASES.values():
            term = str(iso_code).strip().lower()
            if not term:
                continue
            if len(term) >= 3 or term in {"us", "gb", "eu"}:
                self._geo_terms.add(term)

    def resolve(
        self,
        query: str,
        provider: Optional[str] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        use_cache: bool = True,
    ) -> Optional[ResolvedIndicator]:
        """
        Resolve a natural language query or code to a provider-specific indicator.

        Args:
            query: Indicator name, code, or description
            provider: Target provider (FRED, WorldBank, IMF, etc.)
            country: Country context for provider selection
            countries: Multi-country context for provider selection
            use_cache: Whether to use cached results

        Returns:
            ResolvedIndicator with code, provider, and confidence, or None
        """
        if not query:
            return None

        context_countries = [str(c) for c in (countries or []) if c]
        if country and str(country) not in context_countries:
            context_countries.append(str(country))

        # Check cache
        cache_key = self._build_cache_key(
            provider=provider,
            query=query,
            countries=context_countries,
        )
        if use_cache:
            cached = self._get_cached_result(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit for indicator: {query}")
                return cached

        # Try resolution methods in priority order
        result = None
        query_concept = find_concept_by_term(query)
        preferred_catalog_codes: Set[str] = set()
        if provider and query_concept:
            preferred_catalog_codes = {
                self._normalize_code(code)
                for code in get_indicator_codes(query_concept, provider)
                if code
            }

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
        if not result and provider:
            try:
                translated = self.translator.translate_indicator(query, target_provider=provider)
                if translated and translated[0]:
                    code, concept_name = translated
                    result = ResolvedIndicator(
                        code=code,
                        provider=provider,
                        name=concept_name or query,
                        confidence=0.75,  # Good confidence for curated mappings
                        source="translator",
                    )
                    logger.debug(f"Translator match: {query} -> {code}")
            except Exception as e:
                logger.debug(f"Translator lookup failed: {e}")

        # Provider-agnostic translation path (prevents implicit FRED bias).
        if not result and not provider:
            try:
                concept_name = self.translator.infer_concept(query)
                if concept_name:
                    best_provider, best_code, best_confidence = get_best_provider(
                        concept_name,
                        context_countries or None,
                    )
                    if best_provider and best_code:
                        result = ResolvedIndicator(
                            code=best_code,
                            provider=best_provider,
                            name=concept_name or query,
                            confidence=max(0.72, min(0.90, float(best_confidence or 0.75))),
                            source="translator",
                        )
                        logger.debug(
                            "Translator concept match: %s -> %s:%s",
                            query,
                            best_provider,
                            best_code,
                        )
            except Exception as e:
                logger.debug(f"Provider-agnostic translator lookup failed: {e}")

        # 3. If query maps to a known catalog concept, try provider's catalog codes first.
        # This guards against high-ranked but semantically wrong FTS candidates.
        if not result and provider and query_concept and preferred_catalog_codes:
            result = self._resolve_via_catalog_codes(
                query=query,
                provider=provider,
                concept_name=query_concept,
                preferred_codes=preferred_catalog_codes,
            )

        # 4. Try FTS5 search in database (fallback for terms not in translator/catalog)
        if not result:
            search_results = self.lookup.search(query, provider=provider, limit=10)
            search_results = self._fuse_semantic_candidates(
                query=query,
                provider=provider,
                search_results=search_results,
            )
            if search_results:
                best, best_confidence = self._pick_best_search_result(
                    query,
                    search_results,
                    concept_name=query_concept,
                    preferred_codes=preferred_catalog_codes if preferred_catalog_codes else None,
                )
                best_code = self._normalize_code(best.get("code")) if best else ""
                best_is_catalog_code = bool(best_code and best_code in preferred_catalog_codes)
                if best and best_confidence >= 0.35:
                    # Reject off-catalog low-confidence matches for known concepts/providers.
                    # High-confidence off-catalog matches are still allowed (new series/variants).
                    if preferred_catalog_codes and not best_is_catalog_code and best_confidence < 0.70:
                        logger.info(
                            "Rejecting off-catalog low-confidence FTS match for '%s': %s (conf=%.2f)",
                            query,
                            best.get("code"),
                            best_confidence,
                        )
                    else:
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

        # 5. Try IndicatorTranslator again if FTS5 returned low confidence
        if result and result.confidence < 0.7 and result.source == "database":
            try:
                translated = None
                translated_provider = provider
                if provider:
                    translated = self.translator.translate_indicator(query, target_provider=provider)
                else:
                    concept_name = self.translator.infer_concept(query)
                    if concept_name:
                        translated_provider, translated_code, _ = get_best_provider(
                            concept_name,
                            context_countries or None,
                        )
                        if translated_provider and translated_code:
                            translated = (translated_code, concept_name)
                        else:
                            translated = None
                    else:
                        translated = None

                if translated and translated[0]:
                    code, concept_name = translated
                    trans_confidence = 0.75
                    if translated_provider is None:
                        translated_provider = provider

                    # Only use if better than current result
                    if translated_provider and trans_confidence > result.confidence:
                        result = ResolvedIndicator(
                            code=code,
                            provider=translated_provider,
                            name=concept_name or query,
                            confidence=trans_confidence,
                            source="translator",
                        )
            except Exception as e:
                logger.debug(f"IndicatorTranslator failed: {e}")

        # 6. Try CatalogService fallback
        should_try_catalog = (not result) or (result and result.confidence < 0.6)
        if result and query_concept and preferred_catalog_codes and result.source == "database":
            result_code = self._normalize_code(result.code)
            # If a known-concept/provider query resolved to an off-catalog code with only
            # moderate confidence, fall back to catalog canonical mapping.
            if result_code and result_code not in preferred_catalog_codes and result.confidence < 0.70:
                should_try_catalog = True

        if should_try_catalog and query_concept:
            if provider and is_provider_available(query_concept, provider):
                code = get_indicator_code(query_concept, provider)
                if code:
                    result = ResolvedIndicator(
                        code=code,
                        provider=provider,
                        name=query_concept.replace("_", " ").title(),
                        confidence=0.85,
                        source="catalog",
                    )
            elif not provider:
                # Find best provider for concept
                best_provider, code, confidence = get_best_provider(
                    query_concept, context_countries or None
                )
                if best_provider and code:
                    result = ResolvedIndicator(
                        code=code,
                        provider=best_provider,
                        name=query_concept.replace("_", " ").title(),
                        confidence=confidence,
                        source="catalog",
                    )

        # Cache successful result
        if result and use_cache:
            self._set_cached_result(cache_key, result)

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

    @staticmethod
    def _normalize_provider_key(provider: Optional[str]) -> str:
        """Normalize provider strings across DB/vector sources."""
        if not provider:
            return ""
        normalized = re.sub(r"[^A-Za-z0-9]+", "", str(provider).upper())
        aliases = {
            "WORLD BANK": "WORLDBANK",
            "WORLDBANK": "WORLDBANK",
            "STATSCAN": "STATSCAN",
            "STATISTICSCANADA": "STATSCAN",
            "EUROSTAT": "EUROSTAT",
            "COINGECKO": "COINGECKO",
            "EXCHANGERATE": "EXCHANGERATE",
        }
        return aliases.get(normalized, normalized)

    def _get_vector_service(self):
        """Lazily load vector search service if available and indexed."""
        if not self._use_hybrid_rerank or not VECTOR_SEARCH_AVAILABLE:
            return None
        if self._vector_search_checked:
            return self._vector_search_service

        self._vector_search_checked = True
        try:
            service = get_vector_search_service()
            if service and service.is_indexed():
                self._vector_search_service = service
            else:
                logger.info("Vector search service available but no index found for hybrid reranking")
        except Exception as exc:  # pragma: no cover - best-effort path
            logger.warning("Hybrid vector reranking disabled (vector service unavailable): %s", exc)
            self._vector_search_service = None
        return self._vector_search_service

    def _fuse_semantic_candidates(
        self,
        query: str,
        provider: Optional[str],
        search_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Fuse lexical and semantic candidate ranks using Reciprocal Rank Fusion (RRF).

        This is intentionally generic and non-provider-specific:
        - lexical candidates come from SQLite FTS
        - semantic candidates come from vector search
        - RRF combines both without hardcoded phrase patches
        """
        if not search_results:
            return search_results

        vector_service = self._get_vector_service()
        if not vector_service:
            return search_results

        provider_key = self._normalize_provider_key(provider)
        where_filter = {"provider": provider_key} if provider_key else None

        try:
            vector_results = vector_service.search(
                query,
                limit=self._vector_candidate_limit,
                where=where_filter,
            )
        except Exception as exc:  # pragma: no cover - best-effort path
            logger.debug("Vector search failed during hybrid rerank: %s", exc)
            return search_results

        if not vector_results:
            return search_results

        lexical_rank_by_code: Dict[str, int] = {}
        candidate_by_code: Dict[str, Dict[str, Any]] = {}
        for idx, candidate in enumerate(search_results, start=1):
            code = self._normalize_code(candidate.get("code"))
            if not code:
                continue
            lexical_rank_by_code[code] = idx
            candidate_by_code[code] = dict(candidate)

        semantic_rank_by_code: Dict[str, int] = {}
        semantic_similarity_by_code: Dict[str, float] = {}
        for idx, vector_candidate in enumerate(vector_results, start=1):
            code = self._normalize_code(getattr(vector_candidate, "code", ""))
            if not code:
                continue

            semantic_rank_by_code.setdefault(code, idx)
            similarity = max(0.0, min(1.0, float(getattr(vector_candidate, "similarity", 0.0))))
            semantic_similarity_by_code[code] = max(
                similarity,
                semantic_similarity_by_code.get(code, 0.0),
            )

            if code in candidate_by_code:
                continue

            # Promote vector-only candidates into ranking set when metadata exists.
            vector_provider = getattr(vector_candidate, "provider", None)
            metadata = self.lookup.get(vector_provider or provider or "", code)
            if not metadata:
                continue

            if provider_key:
                metadata_provider = self._normalize_provider_key(metadata.get("provider"))
                if metadata_provider and metadata_provider != provider_key:
                    continue

            candidate_by_code[code] = dict(metadata)

        if not semantic_rank_by_code:
            return search_results

        fused_candidates: List[Dict[str, Any]] = []
        for code, candidate in candidate_by_code.items():
            lexical_rank = lexical_rank_by_code.get(code)
            semantic_rank = semantic_rank_by_code.get(code)
            rrf_score = 0.0
            if lexical_rank is not None:
                rrf_score += 1.0 / (self._rrf_k + lexical_rank)
            if semantic_rank is not None:
                rrf_score += 1.0 / (self._rrf_k + semantic_rank)
            if rrf_score <= 0.0:
                continue

            candidate["_rrf_score"] = rrf_score
            candidate["_semantic_rank"] = semantic_rank
            candidate["_semantic_similarity"] = semantic_similarity_by_code.get(code, 0.0)
            fused_candidates.append(candidate)

        if not fused_candidates:
            return search_results

        fused_candidates.sort(key=lambda c: c.get("_rrf_score", 0.0), reverse=True)
        return fused_candidates

    @staticmethod
    def _normalize_country_token(country: str) -> str:
        """Normalize country/cache context tokens."""
        return str(country or "").strip().upper()

    def _build_cache_key(
        self,
        provider: Optional[str],
        query: str,
        countries: Optional[List[str]] = None,
    ) -> str:
        """Build a deterministic cache key that includes resolution context."""
        provider_key = self._normalize_provider_key(provider) or "ANY"
        country_tokens = sorted(
            {
                self._normalize_country_token(country)
                for country in (countries or [])
                if self._normalize_country_token(country)
            }
        )
        country_key = ",".join(country_tokens) if country_tokens else "-"
        query_key = str(query or "").strip().lower()
        return f"{provider_key}:{country_key}:{query_key}"

    def _get_cached_result(self, cache_key: str) -> Optional[ResolvedIndicator]:
        """Return cached resolution result if present and not expired."""
        cached = self._cache.get(cache_key)
        if not cached:
            return None

        cached_at, result = cached
        if (monotonic() - cached_at) > self._cache_ttl_seconds:
            self._cache.pop(cache_key, None)
            return None

        # LRU touch
        self._cache.move_to_end(cache_key)
        return result

    def _set_cached_result(self, cache_key: str, result: ResolvedIndicator) -> None:
        """Store resolution result in bounded TTL/LRU cache."""
        self._cache[cache_key] = (monotonic(), result)
        self._cache.move_to_end(cache_key)

        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)

    def clear_cache(self):
        """Clear the resolution cache."""
        self._cache.clear()
        logger.info("Indicator resolution cache cleared")

    def _tokenize_terms(self, text: str) -> Set[str]:
        """Tokenize text into normalized terms for lexical matching."""
        if not text:
            return set()
        raw_terms = set(re.findall(r"[a-z0-9]+", text.lower()))
        terms: Set[str] = set()
        for term in raw_terms:
            term = self._term_corrections.get(term, term)
            if len(term) <= 1 or term in self._stop_words:
                continue
            if term in self._geo_terms:
                continue
            terms.add(term)
            # Lightweight stemming for plural variants (imports/import, prices/price).
            if term.endswith("ies") and len(term) > 4:
                terms.add(term[:-3] + "y")
            elif term.endswith("s") and len(term) > 3:
                terms.add(term[:-1])
        return terms

    def _normalize_code(self, code: Optional[str]) -> str:
        """Normalize indicator code for case-insensitive comparisons."""
        if not code:
            return ""
        return str(code).strip().upper()

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
        name_terms = self._tokenize_terms(f"{name} {code}")

        if not query_terms:
            return 0.0

        overlap_count = len(query_terms & candidate_terms)
        overlap_ratio = overlap_count / max(len(query_terms), 1)

        # Emphasize non-generic query terms (e.g., export/import/debt) to avoid
        # generic GDP-ratio candidates outranking the actual target concept.
        core_query_terms = {term for term in query_terms if term not in self._context_terms}
        core_overlap_count = len(core_query_terms & candidate_terms)
        core_overlap_ratio = core_overlap_count / max(len(core_query_terms), 1)
        core_name_overlap_count = len(core_query_terms & name_terms)
        core_name_overlap_ratio = core_name_overlap_count / max(len(core_query_terms), 1)

        phrase_bonus = 0.0
        name_lower = name.lower()
        if query_text and query_text in name_lower:
            phrase_bonus += 0.2
        if query_text and code and query_text in code.lower():
            phrase_bonus += 0.15

        fuzzy_bonus = 0.0
        fuzzy_value = 0.0
        fuzzy_core_overlap_count = 0
        fuzzy_core_name_overlap_count = 0
        if fuzz is not None and query_text:
            try:
                fuzzy_value = fuzz.token_set_ratio(query_text, f"{name_lower} {description.lower()} {code.lower()}") / 100.0
                # Keep bounded influence so lexical overlap still dominates.
                fuzzy_bonus = max(0.0, min(0.14, 0.20 * fuzzy_value))

                # Approximate core-term overlap for typo-heavy queries (e.g., "imprts").
                for core_term in core_query_terms:
                    if core_term in candidate_terms:
                        fuzzy_core_overlap_count += 1
                    else:
                        best_candidate = max(
                            (fuzz.ratio(core_term, candidate_term) for candidate_term in candidate_terms),
                            default=0,
                        )
                        if best_candidate >= 86:
                            fuzzy_core_overlap_count += 1

                    if core_term in name_terms:
                        fuzzy_core_name_overlap_count += 1
                    else:
                        best_name_term = max(
                            (fuzz.ratio(core_term, name_term) for name_term in name_terms),
                            default=0,
                        )
                        if best_name_term >= 86:
                            fuzzy_core_name_overlap_count += 1
            except Exception:  # pragma: no cover - optional runtime dependency path
                fuzzy_bonus = 0.0

        effective_core_overlap_count = max(core_overlap_count, fuzzy_core_overlap_count)
        effective_core_name_overlap_count = max(core_name_overlap_count, fuzzy_core_name_overlap_count)
        effective_core_overlap_ratio = effective_core_overlap_count / max(len(core_query_terms), 1)
        effective_core_name_overlap_ratio = effective_core_name_overlap_count / max(len(core_query_terms), 1)

        # Slightly favor higher-ranked FTS results while keeping lexical fit primary.
        rank_bonus = max(0.0, 0.1 - (rank_index * 0.02))
        semantic_similarity = float(candidate.get("_semantic_similarity") or 0.0)
        semantic_bonus = max(0.0, min(0.12, semantic_similarity * 0.16))

        confidence = (
            0.05
            + (0.60 * overlap_ratio)
            + phrase_bonus
            + rank_bonus
            + (0.25 * effective_core_overlap_ratio)
            + (0.15 * effective_core_name_overlap_ratio)
            + fuzzy_bonus
            + semantic_bonus
        )

        # Strongly penalize candidates with zero lexical overlap.
        if overlap_count == 0 and (query_text not in name_lower) and (query_text not in code.lower()):
            confidence *= 0.2
        # Penalize candidates that miss all core terms in the query.
        if core_query_terms and effective_core_overlap_count == 0:
            confidence -= 0.25
        # Prefer candidates where directional intent terms (export/import/etc.) appear in name/code.
        directional_core = core_query_terms & self._directional_terms
        if not directional_core and core_query_terms and fuzz is not None:
            inferred_directionals: Set[str] = set()
            for core_term in core_query_terms:
                for directional in self._directional_terms:
                    if fuzz.ratio(core_term, directional) >= 85:
                        inferred_directionals.add(directional)
            directional_core = inferred_directionals
        if directional_core:
            candidate_directionals = name_terms & self._directional_terms
            if directional_core & name_terms:
                confidence += 0.10
            else:
                # Strong penalty: explicit directional intent (import/export/etc.)
                # should not resolve to generic ratio/trade candidates.
                confidence -= 0.28
                if candidate_directionals and not (candidate_directionals & directional_core):
                    confidence -= 0.10
        # Mild penalty when core terms only appear in long descriptions.
        if core_query_terms and effective_core_overlap_count > 0 and effective_core_name_overlap_count == 0:
            confidence -= 0.06

        # Guardrail: single-term lexical hits are often semantically ambiguous
        # (e.g., "custom indicator" matching "customs and trade ...").
        # Keep confidence moderate unless we have an exact code or direct phrase match.
        single_term_core_query = len(core_query_terms) <= 1
        has_exact_code_match = bool(code and query_text == code.lower())
        has_direct_phrase_match = bool(query_text and query_text in name_lower)
        if (
            single_term_core_query
            and not directional_core
            and not has_exact_code_match
            and not has_direct_phrase_match
        ):
            confidence = min(confidence, 0.64)

        return max(0.0, min(1.0, confidence))

    def _score_concept_alignment(self, concept_name: Optional[str], candidate: Dict[str, Any]) -> float:
        """
        Score how well a candidate aligns with a catalog concept.

        Returns a bounded adjustment in [-0.5, 0.4] to be added to lexical score.
        """
        if not concept_name:
            return 0.0

        code = str(candidate.get("code") or "")
        name = str(candidate.get("name") or "")
        description = str(candidate.get("description") or "")
        candidate_text = f"{name} {description} {code}".lower()

        # Hard penalty for explicit concept exclusions.
        for exclusion in get_exclusions(concept_name):
            exclusion_text = str(exclusion).strip().lower()
            if exclusion_text and exclusion_text in candidate_text:
                return -0.45

        synonyms = get_all_synonyms(concept_name)
        if not synonyms:
            synonyms = [concept_name.replace("_", " ")]

        phrase_hits = 0
        concept_terms: Set[str] = set()
        for synonym in synonyms:
            synonym_text = str(synonym).strip().lower()
            if not synonym_text:
                continue
            concept_terms.update(self._tokenize_terms(synonym_text))
            if len(synonym_text) >= 3 and synonym_text in candidate_text:
                phrase_hits += 1

        if not concept_terms:
            return 0.0

        candidate_terms = self._tokenize_terms(candidate_text)
        overlap = len(concept_terms & candidate_terms) / max(len(concept_terms), 1)

        score = 0.0
        if phrase_hits:
            score += min(0.25, 0.08 * phrase_hits)
        score += min(0.20, overlap * 0.30)

        if phrase_hits == 0 and overlap == 0:
            score -= 0.10

        return max(-0.5, min(0.4, score))

    def _resolve_via_catalog_codes(
        self,
        query: str,
        provider: str,
        concept_name: str,
        preferred_codes: Set[str],
    ) -> Optional[ResolvedIndicator]:
        """
        Resolve using known catalog mappings for a concept/provider pair.

        This keeps resolution general and deterministic by preferring known
        concept codes when they are available in the local indicator database.
        """
        best_metadata: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for code in sorted(preferred_codes):
            metadata = self.lookup.get(provider, code)
            if not metadata:
                continue

            score = self._score_search_match(query, metadata)
            score += self._score_concept_alignment(concept_name, metadata)
            score += 0.15  # boost known catalog mappings
            score = max(0.0, min(1.0, score))

            if score > best_score:
                best_score = score
                best_metadata = metadata

        if best_metadata and best_score >= 0.45:
            return ResolvedIndicator(
                code=best_metadata.get("code"),
                provider=best_metadata.get("provider", provider),
                name=best_metadata.get("name", query),
                confidence=best_score,
                source="catalog",
                metadata=best_metadata,
            )

        return None

    def _pick_best_search_result(
        self,
        query: str,
        search_results: List[Dict[str, Any]],
        concept_name: Optional[str] = None,
        preferred_codes: Optional[Set[str]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], float]:
        """Select best search result using lexical + concept-aware scoring."""
        best_result: Optional[Dict[str, Any]] = None
        best_confidence = 0.0

        for idx, candidate in enumerate(search_results):
            confidence = self._score_search_match(query, candidate, rank_index=idx)
            confidence += self._score_concept_alignment(concept_name, candidate)

            if preferred_codes:
                candidate_code = self._normalize_code(candidate.get("code"))
                if candidate_code in preferred_codes:
                    confidence += 0.25
                else:
                    confidence -= 0.05

            confidence = max(0.0, min(1.0, confidence))
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
    countries: Optional[List[str]] = None,
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
    return get_indicator_resolver().resolve(query, provider, country, countries)
