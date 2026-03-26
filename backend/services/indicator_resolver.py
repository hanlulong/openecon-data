"""
Unified Indicator Resolver Service

THE SINGLE ENTRY POINT for all indicator resolution across OpenEcon Data.

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

Author: OpenEcon Data Development Team
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
    get_provider_info,
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
        self._cache_version = str(
            os.getenv("INDICATOR_RESOLVER_CACHE_VERSION", "2026-02-23.1")
        ).strip() or "2026-02-23.1"
        self._stop_words: Set[str] = {
            "the", "a", "an", "of", "for", "in", "to", "and", "or",
            "show", "get", "find", "data", "series", "indicator", "rate",
            "index", "value", "values", "percent", "percentage",
            "country", "countries", "from", "with", "by", "on", "at",
        }
        # Context terms often appear in many indicators and are weak discriminators.
        # Semantic discriminators — terms that fundamentally change the meaning
        # of an indicator. These are NOT noise; they flip between different datasets.
        # "growth" vs "level" = different data. "real" vs "nominal" = different measure.
        self._semantic_discriminators: Set[str] = {
            "growth", "real", "nominal", "level", "per capita",
            "constant", "current", "change", "rate",
            "ppp", "purchasing power",
        }
        # Context terms — genuinely low-signal words for FTS matching
        self._context_terms: Set[str] = {
            "gdp", "ratio", "share", "index", "value", "values",
            "annual", "quarterly", "monthly", "yearly",
            "levels", "total", "overall",
            "percent", "percentage",
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
        catalog_fallback: Optional[ResolvedIndicator] = None  # Demoted catalog result
        provider_translator_candidate: Optional[ResolvedIndicator] = None
        concept_query = self._normalize_query_for_concept_lookup(query)
        query_concept = find_concept_by_term(concept_query) or find_concept_by_term(query)
        preferred_catalog_codes: Set[str] = set()
        placeholder_catalog_code: Optional[str] = None
        if provider and query_concept:
            preferred_catalog_codes = {
                self._normalize_code(code)
                for code in get_indicator_codes(query_concept, provider)
                if code
            }
            if not preferred_catalog_codes:
                primary_catalog_code = get_indicator_code(query_concept, provider)
                normalized_primary_code = self._normalize_code(primary_catalog_code)
                if normalized_primary_code in {"DYNAMIC", "NONE", "N/A"}:
                    placeholder_catalog_code = str(primary_catalog_code)

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

        # 2. Capture provider-specific translator output as a scored candidate.
        # This keeps curated mappings in play without letting them preempt
        # stronger lexical/semantic matches from the database path.
        if not result and provider:
            provider_translator_candidate = self._build_translator_candidate(query, provider)

        # Provider-agnostic concept resolution should prefer an explicit catalog
        # concept hit over the translator's coarser inferred concept. This keeps
        # newer catalog concepts from collapsing into broader legacy concepts.
        if not result and not provider:
            try:
                concept_candidates: List[Tuple[str, str]] = []
                if query_concept:
                    concept_candidates.append((query_concept, "catalog"))

                inferred_concept = self.translator.infer_concept(query)
                if inferred_concept and all(inferred_concept != name for name, _ in concept_candidates):
                    concept_candidates.append((inferred_concept, "translator"))

                for concept_name, source in concept_candidates:
                    best_provider, best_code, best_confidence = get_best_provider(
                        concept_name,
                        context_countries or None,
                    )
                    if best_provider and best_code:
                        metadata = self._get_catalog_metadata(
                            concept_name=concept_name,
                            provider=best_provider,
                            code=best_code,
                        )
                        result = ResolvedIndicator(
                            code=best_code,
                            provider=best_provider,
                            name=metadata.get("name", concept_name or query),
                            confidence=(
                                max(0.80, min(0.96, float(best_confidence or 0.85)))
                                if source == "catalog"
                                else max(0.72, min(0.90, float(best_confidence or 0.75)))
                            ),
                            source=source,
                            metadata=metadata,
                        )
                        # Semantic verification: check resolved name matches
                        # query discriminators before accepting
                        query_lower = query.lower()
                        res_name_lower = (result.name or "").lower()
                        q_discs = {d for d in self._semantic_discriminators if d in query_lower}

                        def _dp(disc: str, text: str) -> bool:
                            if disc in text: return True
                            if disc == "rate" and ("%" in text or "percent" in text or "ratio" in text): return True
                            if disc == "growth" and ("annual %" in text or "percent change" in text): return True
                            if disc == "ppp" and ("purchasing power" in text or "international $" in text): return True
                            return False

                        missing = {d for d in q_discs if not _dp(d, res_name_lower) and not _dp(d, (result.code or "").lower())}
                        if missing:
                            logger.info(
                                "🔬 Provider-agnostic result '%s' missing discriminators %s — trying next candidate",
                                result.code, missing,
                            )
                            catalog_fallback = catalog_fallback or result
                            result = None
                            continue  # Try next concept candidate

                        logger.debug(
                            "Provider-agnostic %s concept match: %s -> %s:%s",
                            source,
                            query,
                            best_provider,
                            best_code,
                        )
                        break
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

        # 3a. Semantic verification — if catalog returned a code but query has
        # semantic discriminators that the resolved name doesn't match, demote
        # to allow FTS to find a better match.  This prevents "GDP growth rate"
        # resolving to a GDP level series just because the catalog maps
        # "gdp_growth" to a level code for that provider.
        if result and result.source in ("database", "catalog"):
            query_lower = query.lower()
            result_name_lower = (result.name or "").lower()
            query_discs = {d for d in self._semantic_discriminators if d in query_lower}
            # Check if discriminators are present in name/code, accounting
            # for equivalent expressions: "rate" = "% of", "growth" = "annual %"
            def _disc_present(disc: str, text: str) -> bool:
                if disc in text:
                    return True
                if disc == "rate" and ("%" in text or "percent" in text or "ratio" in text):
                    return True
                if disc == "growth" and ("annual %" in text or "percent change" in text):
                    return True
                if disc == "ppp" and ("purchasing power" in text or "international $" in text):
                    return True
                if disc == "purchasing power" and ("ppp" in text or "international $" in text):
                    return True
                return False

            missing_discs = {
                d for d in query_discs
                if not _disc_present(d, result_name_lower)
                and not _disc_present(d, (result.code or "").lower())
            }
            if missing_discs:
                logger.info(
                    "🔬 Catalog result '%s' (%s) missing semantic discriminators %s "
                    "from query '%s' — demoting to allow FTS fallback",
                    result.code, result.name[:40] if result.name else "?",
                    missing_discs, query,
                )
                # Keep as fallback but allow FTS to potentially override
                catalog_fallback = result
                result = None

        # 3b. Some concepts intentionally use dynamic provider-side discovery
        # (e.g., CoinGecko/Comtrade). Prefer this catalog signal over low-quality
        # lexical matches that may map to unrelated provider-specific IDs.
        if not result and provider and query_concept and placeholder_catalog_code:
            result = self._build_catalog_result(
                concept_name=query_concept,
                provider=provider,
                code=placeholder_catalog_code,
                confidence=0.82,
            )

        # 4. Try FTS5 search in database (fallback for terms not in translator/catalog)
        if not result:
            search_results = self.lookup.search(query, provider=provider, limit=10)
            search_results = self._fuse_semantic_candidates(
                query=query,
                provider=provider,
                search_results=search_results,
            )
            if provider_translator_candidate:
                search_results = self._merge_translator_candidate(
                    search_results,
                    provider_translator_candidate,
                )
            if search_results:
                best, best_confidence = self._pick_best_search_result(
                    query,
                    search_results,
                    concept_name=query_concept,
                    preferred_codes=preferred_catalog_codes if preferred_catalog_codes else None,
                    countries=context_countries,
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
                        translator_match = (
                            provider_translator_candidate is not None
                            and bool(best.get("_translator_candidate"))
                            and self._normalize_code(best.get("code"))
                            == self._normalize_code(provider_translator_candidate.code)
                        )
                        if translator_match:
                            translator_metadata = dict(best)
                            translator_metadata.pop("_translator_candidate", None)
                            result = ResolvedIndicator(
                                code=provider_translator_candidate.code,
                                provider=provider_translator_candidate.provider,
                                name=translator_metadata.get("name", provider_translator_candidate.name),
                                confidence=max(best_confidence, provider_translator_candidate.confidence),
                                source="translator",
                                metadata=translator_metadata or provider_translator_candidate.metadata,
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
                if provider and provider_translator_candidate:
                    if provider_translator_candidate.confidence > result.confidence:
                        result = provider_translator_candidate
                elif not provider:
                    translated = None
                    translated_provider = provider
                    concept_name = self.translator.infer_concept(query)
                    if concept_name:
                        translated_provider, translated_code, _ = get_best_provider(
                            concept_name,
                            context_countries or None,
                        )
                        if translated_provider and translated_code:
                            translated = (translated_code, concept_name)

                    if translated and translated[0]:
                        code, concept_name = translated
                        trans_confidence = 0.75
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
        if result and query_concept and preferred_catalog_codes:
            result_code = self._normalize_code(result.code)
            if result_code and result_code not in preferred_catalog_codes:
                # For known-concept/provider queries, prefer canonical catalog mappings unless
                # the off-catalog match is very strong.
                off_catalog_threshold = 0.78
                if result.source == "translator":
                    off_catalog_threshold = 0.90
                elif result.source == "database":
                    off_catalog_threshold = 0.70
                if result.confidence < off_catalog_threshold:
                    should_try_catalog = True

        if should_try_catalog and query_concept:
            if provider and is_provider_available(query_concept, provider):
                code = get_indicator_code(query_concept, provider)
                if code:
                    result = self._build_catalog_result(
                        concept_name=query_concept,
                        provider=provider,
                        code=code,
                        confidence=0.85,
                    )
            elif not provider:
                # Find best provider for concept
                best_provider, code, confidence = get_best_provider(
                    query_concept, context_countries or None
                )
                if best_provider and code:
                    result = self._build_catalog_result(
                        concept_name=query_concept,
                        provider=best_provider,
                        code=code,
                        confidence=confidence,
                    )

        if not result and provider_translator_candidate:
            result = provider_translator_candidate

        # Use demoted catalog result as final fallback (better than nothing)
        if not result and catalog_fallback:
            logger.info(
                "🔬 Using demoted catalog fallback: %s (%s)",
                catalog_fallback.code, catalog_fallback.name[:40] if catalog_fallback.name else "?",
            )
            result = catalog_fallback

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

    def _build_translator_candidate(
        self,
        query: str,
        provider: str,
    ) -> Optional[ResolvedIndicator]:
        """Resolve a provider-specific translator candidate without short-circuiting search."""
        try:
            translated = self.translator.translate_indicator(query, target_provider=provider)
            if translated and translated[0]:
                code, concept_name = translated
                catalog_concept = find_concept_by_term(concept_name) or concept_name
                metadata = self._get_catalog_metadata(
                    concept_name=catalog_concept,
                    provider=provider,
                    code=code,
                )
                logger.debug(f"Translator match: {query} -> {code}")
                return ResolvedIndicator(
                    code=code,
                    provider=metadata.get("provider", provider),
                    name=metadata.get("name", concept_name or query),
                    confidence=0.75,
                    source="translator",
                    metadata=metadata,
                )
        except Exception as e:
            logger.debug(f"Translator lookup failed: {e}")
        return None

    def _merge_translator_candidate(
        self,
        search_results: List[Dict[str, Any]],
        translator_candidate: ResolvedIndicator,
    ) -> List[Dict[str, Any]]:
        """Inject a translator suggestion into the scored search candidate set."""
        merged_results = [dict(candidate) for candidate in search_results]
        translator_code = self._normalize_code(translator_candidate.code)
        translator_provider = self._normalize_provider_key(translator_candidate.provider)

        for candidate in merged_results:
            if (
                self._normalize_code(candidate.get("code")) == translator_code
                and self._normalize_provider_key(candidate.get("provider")) == translator_provider
            ):
                candidate["_translator_candidate"] = True
                return merged_results

        metadata = self.lookup.get(translator_candidate.provider, translator_candidate.code) or {}
        if not metadata and translator_candidate.metadata:
            metadata = dict(translator_candidate.metadata)
        translator_search_candidate = dict(metadata)
        translator_search_candidate.setdefault("code", translator_candidate.code)
        translator_search_candidate.setdefault("provider", translator_candidate.provider)
        translator_search_candidate.setdefault("name", translator_candidate.name)
        translator_search_candidate.setdefault("description", "")
        translator_search_candidate["_translator_candidate"] = True
        merged_results.append(translator_search_candidate)
        return merged_results

    def _build_catalog_result(
        self,
        concept_name: str,
        provider: str,
        code: str,
        confidence: float,
    ) -> ResolvedIndicator:
        """Build a catalog result with DB metadata when available and YAML metadata otherwise."""
        metadata = self._get_catalog_metadata(
            concept_name=concept_name,
            provider=provider,
            code=code,
        )
        return ResolvedIndicator(
            code=metadata.get("code", code),
            provider=metadata.get("provider", provider),
            name=metadata.get("name", concept_name.replace("_", " ").title()),
            confidence=confidence,
            source="catalog",
            metadata=metadata,
        )

    def _get_catalog_metadata(
        self,
        concept_name: Optional[str],
        provider: str,
        code: Optional[str],
    ) -> Dict[str, Any]:
        """Get provider metadata from the DB first, then fall back to catalog YAML."""
        if provider and code:
            metadata = self.lookup.get(provider, code)
            if metadata:
                return dict(metadata)

        catalog_metadata = self._catalog_metadata_from_definition(
            concept_name=concept_name,
            provider=provider,
            code=code,
        )
        return catalog_metadata or {}

    def _catalog_metadata_from_definition(
        self,
        concept_name: Optional[str],
        provider: str,
        code: Optional[str],
    ) -> Dict[str, Any]:
        """Build provider metadata from catalog YAML when no DB row is available."""
        if not concept_name or not provider:
            return {}

        provider_info = get_provider_info(concept_name, provider)
        if not provider_info:
            return {}

        normalized_code = self._normalize_code(code)
        mapping = self._find_catalog_mapping(provider_info, normalized_code)
        if not mapping and isinstance(provider_info.get("primary"), dict):
            mapping = provider_info.get("primary")

        metadata = dict(mapping or {})
        if code:
            metadata.setdefault("code", code)
        metadata.setdefault("provider", provider)
        metadata.setdefault("name", concept_name.replace("_", " ").title())
        metadata["concept"] = concept_name
        return metadata

    def _find_catalog_mapping(
        self,
        node: Any,
        target_code: str,
    ) -> Optional[Dict[str, Any]]:
        """Recursively locate the catalog mapping that owns a specific provider code."""
        if isinstance(node, dict):
            node_code = self._normalize_code(node.get("code"))
            if target_code and node_code and node_code == target_code:
                return node
            for value in node.values():
                match = self._find_catalog_mapping(value, target_code)
                if match:
                    return match
            return None

        if isinstance(node, list):
            for item in node:
                match = self._find_catalog_mapping(item, target_code)
                if match:
                    return match

        return None

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
        return f"{self._cache_version}:{provider_key}:{country_key}:{query_key}"

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

    @staticmethod
    def _normalize_query_for_concept_lookup(query: str) -> str:
        """Normalize text form for catalog concept lookup."""
        normalized = re.sub(r"[_/]+", " ", str(query or "").strip().lower()).replace("-", " ")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    @staticmethod
    def _specialization_penalty(query_text: str, candidate_text: str) -> float:
        """Penalize subgroup-specific series when the query asked for a generic metric."""
        query_lower = str(query_text or "").lower()
        candidate_lower = str(candidate_text or "").lower()
        if not candidate_lower:
            return 0.0

        penalty = 0.0

        def query_mentions(*terms: str) -> bool:
            return any(term in query_lower for term in terms)

        def candidate_mentions(*terms: str) -> bool:
            return any(term in candidate_lower for term in terms)

        penalty += IndicatorResolver._labor_rate_specificity_penalty(query_lower, candidate_lower)

        if not query_mentions("student", "students", "school months", "summer months") and candidate_mentions(
            "student",
            "students",
            "school months",
            "summer months",
        ):
            penalty += 0.62

        if not query_mentions("education", "educational attainment", "school") and candidate_mentions(
            "educational attainment",
            "education",
        ):
            penalty += 0.42

        if not query_mentions("fiscal") and candidate_mentions("fiscal"):
            penalty += 0.38

        if not query_mentions("insurance", "employment insurance", "beneficiaries", "claimants") and candidate_mentions(
            "employment insurance",
            "insurance program",
            "insurance region",
            "beneficiaries",
            "claimants",
        ):
            penalty += 0.46

        if not query_mentions("indigenous") and candidate_mentions("indigenous"):
            penalty += 0.32

        if not query_mentions("gender", "sex", "women", "woman", "men", "man", "female", "male") and candidate_mentions(
            "gender",
            "sex",
            "women",
            "woman",
            "men",
            "man",
            "female",
            "male",
        ):
            penalty += 0.24

        if not query_mentions("urban", "rural", "metropolitan", "population centre", "health region") and candidate_mentions(
            "urban",
            "rural",
            "metropolitan",
            "population centre",
            "health region",
        ):
            penalty += 0.18

        if not query_mentions("industry", "industries", "sector", "sectors") and candidate_mentions(
            "industry",
            "industries",
            "sector",
            "sectors",
        ):
            penalty += 0.26

        if not query_mentions("firm size", "business size", "company size", "enterprise size") and candidate_mentions(
            "firm size",
            "business size",
            "enterprise size",
        ):
            penalty += 0.26

        if not query_mentions("province", "provinces", "territory", "territories", "regional", "region", "regions") and candidate_mentions(
            "province",
            "provinces",
            "territory",
            "territories",
            "regional",
            "regions",
        ):
            penalty += 0.20

        if not query_mentions("youth", "age", "aged", "years old", "15-24", "15 to 24", "25-64", "25 to 64") and (
            re.search(r"\baged?\b", candidate_lower)
            or re.search(r"\b\d{1,2}\s*(?:to|-)\s*\d{1,2}\b", candidate_lower)
            or re.search(r"\b\d{1,2}\s+years?\s+(?:and|or)\s+over\b", candidate_lower)
        ):
            penalty += 0.34

        return penalty

    @staticmethod
    def _labor_rate_specificity_penalty(query_text: str, candidate_text: str) -> float:
        """Penalize near-miss labor-rate candidates for generic labor queries."""
        query_lower = str(query_text or "").lower()
        candidate_lower = str(candidate_text or "").lower()
        if not query_lower or not candidate_lower:
            return 0.0

        requested_metric = ""
        if re.search(r"\bemployment\s+rate\b", query_lower):
            requested_metric = "employment"
        elif re.search(r"\bunemployment\s+rate\b", query_lower):
            requested_metric = "unemployment"
        elif re.search(r"\b(?:labou?r\s+force\s+)?participation\s+rate\b", query_lower):
            requested_metric = "participation"

        if not requested_metric:
            return 0.0

        def has_metric_rate(metric: str) -> bool:
            metric_pattern = {
                "employment": r"\bemployment(?:\s+\w+){0,2}\s+rates?\b",
                "unemployment": r"\bunemployment(?:\s+\w+){0,2}\s+rates?\b",
                "participation": r"\b(?:labou?r\s+force\s+)?participation(?:\s+\w+){0,2}\s+rates?\b",
            }[metric]
            return bool(re.search(metric_pattern, candidate_lower))

        penalty = 0.0
        if not has_metric_rate(requested_metric):
            penalty += 0.58

        for sibling_metric in ("employment", "unemployment", "participation"):
            if sibling_metric == requested_metric:
                continue
            if has_metric_rate(sibling_metric):
                penalty += 0.12

        return penalty

    @staticmethod
    def _single_directional_term(terms: Set[str]) -> str:
        """Return a strict single trade direction from query terms, if present."""
        has_import = bool({"import", "imports"} & terms)
        has_export = bool({"export", "exports"} & terms)
        if has_import and not has_export:
            return "import"
        if has_export and not has_import:
            return "export"
        return ""

    @staticmethod
    def _candidate_has_directional_signal(candidate_text_lower: str, direction: str) -> bool:
        """Detect import/export directional signal in names/descriptions/codes."""
        direction_norm = str(direction or "").strip().lower()
        if direction_norm == "import":
            return any(
                token in candidate_text_lower
                for token in ("import", "imports", ".imp.", "_imp", "imp_")
            )
        if direction_norm == "export":
            return any(
                token in candidate_text_lower
                for token in ("export", "exports", ".exp.", "_exp", "exp_")
            )
        return False

    def _country_specific_code_penalty(
        self,
        code: Optional[str],
        countries: Optional[List[str]],
    ) -> float:
        """
        Penalize country-prefixed series codes when they mismatch query country scope.

        Many provider namespaces (notably IMF) include series prefixed by one country
        code (for example `VNM_...`). These are often wrong for multi-country/global
        requests and should not outrank generic series.
        """
        normalized_code = self._normalize_code(code)
        if not normalized_code:
            return 0.0

        match = re.match(r"^([A-Z]{3})_", normalized_code)
        if not match:
            return 0.0

        code_iso3 = match.group(1)
        if not CountryResolver.to_iso2(code_iso3):
            return 0.0

        if not countries:
            return 0.30

        requested_iso3: Set[str] = set()
        for country in countries:
            country_text = str(country or "").strip()
            if not country_text:
                continue

            iso2 = CountryResolver.normalize(country_text)
            if not iso2 and re.fullmatch(r"[A-Z]{3}", country_text.upper()):
                iso2 = CountryResolver.to_iso2(country_text.upper())
            if not iso2:
                continue

            iso3 = CountryResolver.to_iso3(iso2)
            if iso3:
                requested_iso3.add(iso3)

        if not requested_iso3:
            return 0.0
        if code_iso3 in requested_iso3:
            return 0.0 if len(requested_iso3) == 1 else 0.35
        return 0.70

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
        candidate_text_lower = f"{name} {code} {description}".lower()

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

        # Semantic discriminator scoring — terms that fundamentally change meaning.
        # "growth" vs "level", "real" vs "nominal" = different datasets entirely.
        query_discriminators = query_terms & self._semantic_discriminators
        for disc in query_discriminators:
            if disc in candidate_terms:
                confidence += 0.15  # Boost for matching semantic constraint
            else:
                confidence -= 0.12  # Penalize for missing constraint

        # Real vs nominal mutual exclusion — these are incompatible measures.
        if "real" in query_terms and "nominal" in candidate_text_lower and "real" not in candidate_text_lower:
            confidence -= 0.30
        if "nominal" in query_terms and "real" in candidate_text_lower and "nominal" not in candidate_text_lower:
            confidence -= 0.28

        # Growth vs level distinction
        if "growth" in query_terms and "level" in candidate_text_lower and "growth" not in candidate_text_lower:
            confidence -= 0.25

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
        query_direction = self._single_directional_term(query_terms)
        if query_direction:
            opposite_direction = "export" if query_direction == "import" else "import"
            has_query_direction_signal = self._candidate_has_directional_signal(
                candidate_text_lower,
                query_direction,
            )
            has_opposite_direction_signal = self._candidate_has_directional_signal(
                candidate_text_lower,
                opposite_direction,
            )
            if has_opposite_direction_signal and not has_query_direction_signal:
                confidence -= 0.38
            elif not has_query_direction_signal:
                confidence -= 0.14
        # Mild penalty when core terms only appear in long descriptions.
        if core_query_terms and effective_core_overlap_count > 0 and effective_core_name_overlap_count == 0:
            confidence -= 0.06

        # Monetary aggregate disambiguation (M1/M2/M3 should map to matching aggregate series).
        for aggregate in ("m1", "m2", "m3"):
            query_has_aggregate = bool(re.search(rf"\b{aggregate}\b", query_text))
            candidate_has_aggregate = bool(re.search(rf"\b{aggregate}\b", candidate_text_lower))
            if query_has_aggregate and candidate_has_aggregate:
                confidence += 0.24
            elif query_has_aggregate and not candidate_has_aggregate:
                confidence -= 0.72

        # Employment-to-population queries require employment subject, not population-only series.
        query_has_employment_population_ratio = (
            ("employment" in query_terms)
            and ("population" in query_terms)
            and bool({"ratio", "rate"} & set(re.findall(r"[a-z0-9]+", query_text)))
        )
        if query_has_employment_population_ratio and "employment" not in candidate_terms:
            confidence -= 0.34

        # Bond yield/long-term rate disambiguation.
        bond_yield_query = any(
            phrase in query_text
            for phrase in (
                "bond yield",
                "treasury yield",
                "government bond",
                "sovereign yield",
                "10-year",
                "10 year",
                "long-term interest rate",
                "long term interest rate",
            )
        )
        if bond_yield_query and not any(
            token in candidate_text_lower
            for token in (
                "bond",
                "yield",
                "treasury",
                "10-year",
                "10 year",
                "long-term",
                "long term",
            )
        ):
            confidence -= 0.32
        if bond_yield_query and any(
            token in candidate_text_lower
            for token in (
                "federal funds",
                "policy rate",
                "benchmark rate",
                "overnight",
                "interbank",
            )
        ):
            confidence -= 0.36

        # Producer-price requests should not degrade to consumer inflation.
        producer_price_query = (
            ("producer" in query_terms and "price" in query_terms)
            or "ppi" in query_terms
        )
        if producer_price_query:
            has_producer_signal = (
                "producer" in candidate_terms
                or "ppi" in candidate_terms
                or "wholesale" in candidate_terms
                or "pppi" in candidate_text_lower
                or "pwpi" in candidate_text_lower
            )
            if has_producer_signal:
                confidence += 0.26
            else:
                confidence -= 0.62
                if any(
                    token in candidate_text_lower
                    for token in ("consumer price", "cpi", "hicp", "pcpipch")
                ):
                    confidence -= 0.18

        # GDP deflator requests should not degrade to CPI/HICP/PPI.
        gdp_deflator_query = bool(
            re.search(r"\bgdp\b.{0,20}\bdeflator\b", query_text)
            or re.search(r"\bdeflator\b.{0,20}\bgdp\b", query_text)
        )
        if gdp_deflator_query:
            has_deflator_signal = any(
                token in candidate_text_lower
                for token in (
                    "deflator",
                    "gdpdef",
                    "ngdp_d",
                    "ny.gdp.defl",
                )
            )
            if has_deflator_signal:
                confidence += 0.30
            else:
                confidence -= 0.48
                if any(
                    token in candidate_text_lower
                    for token in ("cpi", "consumer price", "hicp", "ppi")
                ):
                    confidence -= 0.16

        # HICP requests should prioritize HICP-specific datasets.
        hicp_query = any(
            phrase in query_text
            for phrase in (
                "hicp",
                "harmonized index",
                "harmonised index",
                "harmonized inflation",
                "harmonised inflation",
            )
        )
        if hicp_query:
            has_hicp_signal = any(
                token in candidate_text_lower
                for token in (
                    "hicp",
                    "prc_hicp",
                    "df_prices_hicp",
                    "harmonized index",
                    "harmonised index",
                )
            )
            if has_hicp_signal:
                confidence += 0.28
            else:
                confidence -= 0.46

        ratio_patterns = (
            "% of gdp",
            "as % of gdp",
            "as percent of gdp",
            "as percentage of gdp",
            "share of gdp",
            "to gdp ratio",
            "ratio to gdp",
            "as share of gdp",
        )
        has_ratio_query = any(pattern in query_text for pattern in ratio_patterns)
        directional_ratio_query = has_ratio_query and bool(
            {"import", "imports", "export", "exports"} & query_terms
        )
        if directional_ratio_query:
            has_ratio_signal = any(
                token in candidate_text_lower
                for token in (
                    "% of gdp",
                    "percent of gdp",
                    "share of gdp",
                    "as share of gdp",
                    ".zs",
                    "_ngdp",
                )
            )
            has_directional_signal = (
                self._candidate_has_directional_signal(candidate_text_lower, query_direction)
                if query_direction else True
            )
            has_directional_ratio_signal = has_ratio_signal and has_directional_signal
            if has_ratio_signal:
                confidence += 0.22 if has_directional_ratio_signal else -0.16
            else:
                confidence -= 0.44
                if any(
                    token in candidate_text_lower
                    for token in ("current us$", "constant us$", "million", "billion", "total trade")
                ):
                    confidence -= 0.24
            if has_ratio_signal and query_direction and not has_directional_ratio_signal:
                confidence -= 0.24

        # Trade-openness requests should prefer "exports + imports as % of GDP".
        trade_openness_query = (
            "trade openness" in query_text
            or (
                ("import" in query_terms or "imports" in query_terms)
                and ("export" in query_terms or "exports" in query_terms)
                and ("gdp" in query_terms)
            )
        )
        if trade_openness_query:
            has_openness_signal = any(
                token in candidate_text_lower
                for token in (
                    "trade (% of gdp)",
                    "trade openness",
                    "exports plus imports",
                    "imports plus exports",
                    "ne.trd.gnfs.zs",
                    "bfa_bop_b_xs_gdp_pt",
                )
            )
            if has_openness_signal:
                confidence += 0.24
            elif any(
                token in candidate_text_lower
                for token in ("import", "export", "trade balance")
            ):
                confidence -= 0.12
            else:
                confidence -= 0.30

        # Debt-to-GDP requests should not degrade into debt-service or debt-securities datasets.
        debt_gdp_query = any(
            phrase in query_text
            for phrase in (
                "debt to gdp",
                "debt-to-gdp",
                "debt as % of gdp",
                "debt as percentage of gdp",
                "debt (% of gdp)",
                "gdp to debt ratio",
                "gdp/debt ratio",
            )
        )
        if debt_gdp_query:
            if not any(
                token in candidate_text_lower
                for token in (
                    "gdp",
                    "% of gdp",
                    "percent of gdp",
                    "debt-to-gdp",
                    "debt to gdp",
                    "government debt",
                    "public debt",
                )
            ):
                confidence -= 0.38
            if any(
                token in candidate_text_lower
                for token in (
                    "debt service",
                    "dsr",
                    "debt securities",
                    "international debt securities",
                )
            ):
                confidence -= 0.45

        # Prefer active series unless the query explicitly asks for discontinued data.
        if "discontinued" in candidate_text_lower and "discontinued" not in query_text:
            confidence -= 0.42

        # Prefer actual/historical data over projections, forecasts, and estimates.
        # FOMC projections, IMF forecasts, survey expectations, etc. should not
        # outrank actual data series (e.g., GDPC1CTMLR vs A191RL1Q225SBEA).
        _projection_terms = (
            "fomc", "projection", "projections", "forecast", "forecasts",
            "outlook", "summary of economic projections", "central tendency",
            "median forecast", "consensus", "expected", "expectations",
            "survey of professional forecasters",
        )
        query_wants_projection = any(t in query_text for t in _projection_terms)
        candidate_is_projection = any(t in candidate_text_lower for t in _projection_terms)
        if candidate_is_projection and not query_wants_projection:
            confidence -= 0.55  # Strong penalty — projections should almost never win

        confidence -= self._specialization_penalty(query_text, candidate_text_lower)

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
        countries: Optional[List[str]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], float]:
        """Select best search result using lexical + concept-aware scoring."""
        best_result: Optional[Dict[str, Any]] = None
        best_confidence = 0.0
        best_tiebreak: tuple[float, float, int] = (-float("inf"), -float("inf"), -10**9)

        for idx, candidate in enumerate(search_results):
            confidence = self._score_search_match(query, candidate, rank_index=idx)
            confidence += self._score_concept_alignment(concept_name, candidate)

            if preferred_codes:
                candidate_code = self._normalize_code(candidate.get("code"))
                if candidate_code in preferred_codes:
                    confidence += 0.25
                else:
                    confidence -= 0.05

            country_penalty = self._country_specific_code_penalty(
                candidate.get("code"),
                countries,
            )
            if country_penalty > 0.0:
                confidence -= country_penalty

            confidence = max(0.0, min(1.0, confidence))
            candidate_text = " ".join(
                str(candidate.get(key) or "")
                for key in ("name", "description", "code")
            )
            specialization_penalty = self._specialization_penalty(query, candidate_text)
            semantic_similarity = float(candidate.get("_semantic_similarity") or 0.0)
            tiebreak = (-specialization_penalty, semantic_similarity, -idx)

            if confidence > best_confidence or (
                abs(confidence - best_confidence) <= 1e-9 and tiebreak > best_tiebreak
            ):
                best_result = candidate
                best_confidence = confidence
                best_tiebreak = tiebreak

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
