"""
Hybrid Router

Combines deterministic candidate generation with LLM ranking and hard guardrails.
The goal is flexibility for phrasing variation while preserving reliability.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .keyword_matcher import KeywordMatcher
from .unified_router import RoutingDecision, UnifiedRouter
from ..services.catalog_service import (
    find_concept_by_term,
    get_available_providers,
    is_provider_available,
)
from ..services.json_parser import parse_llm_json

logger = logging.getLogger(__name__)


class HybridRouter:
    """LLM-assisted provider router with deterministic constraints."""

    _PROVIDER_CANONICAL = {
        "WORLDBANK": "WorldBank",
        "WORLD BANK": "WorldBank",
        "FRED": "FRED",
        "IMF": "IMF",
        "BIS": "BIS",
        "EUROSTAT": "Eurostat",
        "OECD": "OECD",
        "COMTRADE": "Comtrade",
        "UNCOMTRADE": "Comtrade",
        "UN COMTRADE": "Comtrade",
        "STATSCAN": "StatsCan",
        "STATISTICSCANADA": "StatsCan",
        "STATISTICS CANADA": "StatsCan",
        "EXCHANGERATE": "ExchangeRate",
        "EXCHANGE RATE": "ExchangeRate",
        "COINGECKO": "CoinGecko",
        "COIN GECKO": "CoinGecko",
    }

    _PROVIDER_HINTS = {
        "WorldBank": "global development indicators and cross-country macro ratios",
        "FRED": "US economic time series",
        "IMF": "global macro aggregates, fiscal/current-account, forecasts/projections",
        "BIS": "property prices, credit, financial stability",
        "Eurostat": "EU country official statistics",
        "OECD": "OECD comparative statistics and policy indicators",
        "Comtrade": "bilateral trade flows by reporter/partner/commodity",
        "StatsCan": "official Canada statistics",
        "ExchangeRate": "FX rates and currency conversion",
        "CoinGecko": "crypto prices and market metrics",
    }

    def __init__(self, llm_provider: Any = None):
        self._llm_provider = llm_provider
        self._deterministic = UnifiedRouter()

    @staticmethod
    def _normalize_provider(provider: Optional[str]) -> Optional[str]:
        if not provider:
            return None
        cleaned = re.sub(r"[^A-Za-z ]+", "", str(provider)).strip().upper()
        if not cleaned:
            return None
        if cleaned in HybridRouter._PROVIDER_CANONICAL:
            return HybridRouter._PROVIDER_CANONICAL[cleaned]
        compact = cleaned.replace(" ", "")
        if compact in HybridRouter._PROVIDER_CANONICAL:
            return HybridRouter._PROVIDER_CANONICAL[compact]
        return None

    def _provider_scores(
        self,
        query: str,
        indicators: List[str],
        country: Optional[str],
        countries: List[str],
        llm_provider_hint: Optional[str],
        baseline: RoutingDecision,
    ) -> Dict[str, float]:
        scores: Dict[str, float] = {}

        def boost(provider: Optional[str], value: float) -> None:
            normalized = self._normalize_provider(provider)
            if not normalized:
                return
            scores[normalized] = scores.get(normalized, 0.0) + value

        boost(baseline.provider, 100.0)
        for idx, fb in enumerate(baseline.fallbacks):
            boost(fb, max(0.0, 60.0 - (idx * 7.0)))

        explicit = KeywordMatcher.detect_explicit_provider(query)
        if explicit and explicit.provider:
            boost(explicit.provider, 200.0)

        indicator_match = KeywordMatcher.detect_indicator_provider(query, indicators)
        if indicator_match and indicator_match.provider:
            boost(indicator_match.provider, 45.0)

        regional_match = KeywordMatcher.detect_regional_provider(query)
        if regional_match and regional_match.provider:
            boost(regional_match.provider, 35.0)

        boost(llm_provider_hint, 35.0)

        concepts = set()
        for term in indicators:
            concept = find_concept_by_term(term)
            if concept:
                concepts.add(concept)
        concept_from_query = find_concept_by_term(query)
        if concept_from_query:
            concepts.add(concept_from_query)

        for concept in concepts:
            for provider in get_available_providers(concept):
                boost(provider, 24.0)

        if countries and len(countries) > 1:
            boost("WorldBank", 15.0)
        if country and self._normalize_provider(baseline.provider) == "Eurostat":
            boost("Eurostat", 10.0)

        if not scores:
            scores["WorldBank"] = 1.0
        return scores

    def _build_candidates(
        self,
        query: str,
        indicators: List[str],
        country: Optional[str],
        countries: List[str],
        llm_provider_hint: Optional[str],
        baseline: RoutingDecision,
    ) -> List[str]:
        scored = self._provider_scores(
            query=query,
            indicators=indicators,
            country=country,
            countries=countries,
            llm_provider_hint=llm_provider_hint,
            baseline=baseline,
        )
        return [k for k, _ in sorted(scored.items(), key=lambda kv: kv[1], reverse=True)]

    @staticmethod
    def _resolve_country_context(country: Optional[str], countries: Optional[List[str]]) -> List[str]:
        if countries:
            return [str(c) for c in countries if c]
        if country:
            return [str(country)]
        return []

    def _build_prompt(
        self,
        query: str,
        indicators: List[str],
        country: Optional[str],
        countries: List[str],
        candidates: List[str],
    ) -> str:
        indicator_text = ", ".join(indicators) if indicators else "None"
        geo_items = self._resolve_country_context(country, countries)
        geo_text = ", ".join(geo_items) if geo_items else "None"

        candidate_lines = []
        for candidate in candidates[:8]:
            hint = self._PROVIDER_HINTS.get(candidate, "")
            candidate_lines.append(f"- {candidate}: {hint}")

        return (
            "Route this economic data query to the best provider from the candidate list.\n\n"
            f"Query: {query}\n"
            f"Indicators parsed: {indicator_text}\n"
            f"Country context: {geo_text}\n\n"
            "Candidate providers:\n"
            + "\n".join(candidate_lines)
            + "\n\nDecision rules:\n"
            "- Choose exactly ONE provider from candidates.\n"
            "- Prefer provider with strongest domain/country fit for the requested data.\n"
            "- Bilateral trade flows -> Comtrade.\n"
            "- Macro ratios (% of GDP) across countries -> WorldBank.\n"
            "- Forecasts/projections/global macro aggregates -> IMF.\n"
            "- EU official country statistics -> Eurostat.\n"
            "- Canada official statistics -> StatsCan.\n"
            "- Property prices -> BIS.\n"
            "- US-only macro series -> FRED.\n"
            "- FX rates -> ExchangeRate.\n"
            "- Crypto -> CoinGecko.\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  "provider": "CandidateProvider",\n'
            '  "confidence": 0.0,\n'
            '  "reasoning": "short reason",\n'
            '  "fallbacks": ["ProviderA", "ProviderB"],\n'
            '  "concept": "optional short concept name"\n'
            "}\n"
        )

    def _catalog_guardrail(
        self,
        selected_provider: str,
        indicators: List[str],
    ) -> bool:
        concepts = []
        for term in indicators:
            concept = find_concept_by_term(term)
            if concept:
                concepts.append(concept)

        if not concepts:
            return True

        # Reject provider if unavailable for all recognized concepts.
        return any(is_provider_available(concept, selected_provider) for concept in concepts)

    async def route(
        self,
        query: str,
        indicators: Optional[List[str]] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        llm_provider_hint: Optional[str] = None,
    ) -> RoutingDecision:
        indicators = indicators or []
        countries = countries or []

        baseline = self._deterministic.route(
            query=query,
            indicators=indicators,
            country=country,
            countries=countries,
            llm_provider=llm_provider_hint,
        )

        # If LLM provider is unavailable, deterministic routing is used.
        if not self._llm_provider:
            return baseline

        candidates = self._build_candidates(
            query=query,
            indicators=indicators,
            country=country,
            countries=countries,
            llm_provider_hint=llm_provider_hint,
            baseline=baseline,
        )
        if not candidates:
            return baseline

        try:
            choice = await parse_llm_json(
                self._llm_provider,
                prompt=self._build_prompt(query, indicators, country, countries, candidates),
                system_prompt=(
                    "You are a strict economic data provider router. "
                    "Choose only from provided candidates and output JSON only."
                ),
                max_tokens=220,
                max_retries=1,
            )
        except Exception as exc:
            logger.warning("HybridRouter LLM routing failed, using deterministic fallback: %s", exc)
            return baseline

        selected = self._normalize_provider(choice.get("provider"))
        if not selected or selected not in candidates:
            logger.info(
                "HybridRouter ignored out-of-candidate provider '%s'; fallback=%s",
                choice.get("provider"),
                baseline.provider,
            )
            return baseline

        explicit = KeywordMatcher.detect_explicit_provider(query)
        if explicit and explicit.provider:
            explicit_provider = self._normalize_provider(explicit.provider)
            if explicit_provider:
                selected = explicit_provider

        # Apply critical anti-misrouting guardrail.
        selected, _ = KeywordMatcher.correct_coingecko_misrouting(
            selected,
            query,
            indicators,
        )
        selected = self._normalize_provider(selected) or baseline.provider

        if not self._catalog_guardrail(selected, indicators):
            logger.info(
                "HybridRouter rejected catalog-incompatible provider '%s'; fallback=%s",
                selected,
                baseline.provider,
            )
            return baseline

        fallback_candidates = choice.get("fallbacks", [])
        if not isinstance(fallback_candidates, list):
            fallback_candidates = []
        normalized_fallbacks = []
        for fb in fallback_candidates:
            fb_name = self._normalize_provider(fb)
            if not fb_name or fb_name == selected or fb_name in normalized_fallbacks:
                continue
            if fb_name in candidates:
                normalized_fallbacks.append(fb_name)

        if not normalized_fallbacks:
            normalized_fallbacks = self._deterministic.get_fallbacks(selected)

        confidence = choice.get("confidence", baseline.confidence)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = baseline.confidence
        confidence = max(0.0, min(1.0, confidence))

        concept = str(choice.get("concept") or "").strip()
        matched_pattern = f"concept:{concept}" if concept else None

        return RoutingDecision(
            provider=selected,
            confidence=confidence,
            fallbacks=normalized_fallbacks,
            reasoning=str(choice.get("reasoning") or "LLM-ranked candidate selection"),
            match_type="hybrid_llm",
            matched_pattern=matched_pattern,
        )
