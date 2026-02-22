from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from ..models import CodeExecutionResult, GeneratedFile, NormalizedData, ParsedIntent, QueryResponse
from ..config import Settings
from ..services.cache import cache_service
from ..services.redis_cache import get_redis_cache
from ..services.conversation import conversation_manager
from ..services.openrouter import OpenRouterService
from ..services.query_complexity import QueryComplexityAnalyzer
from ..services.parameter_validator import ParameterValidator
from ..services.metadata_search import MetadataSearchService
from ..services.provider_router import ProviderRouter
from ..services.indicator_resolver import get_indicator_resolver, resolve_indicator
from ..services.query_pipeline import QueryPipeline
from ..routing.country_resolver import CountryResolver
from ..routing.unified_router import UnifiedRouter
from ..routing.hybrid_router import HybridRouter
from ..routing.semantic_provider_router import SemanticProviderRouter
from ..providers.fred import FREDProvider
from ..providers.worldbank import WorldBankProvider
from ..providers.comtrade import ComtradeProvider
from ..providers.statscan import StatsCanProvider
from ..providers.imf import IMFProvider
from ..providers.exchangerate import ExchangeRateProvider
from ..providers.bis import BISProvider
from ..providers.eurostat import EurostatProvider
from ..providers.oecd import OECDProvider
from ..providers.coingecko import CoinGeckoProvider
from ..utils.geographies import normalize_canadian_region_list
from ..utils.retry import retry_async, DataNotAvailableError
from ..services.rate_limiter import is_provider_circuit_open
from ..services.time_range_defaults import apply_default_time_range
from ..utils.processing_steps import (
    ProcessingTracker,
    activate_processing_tracker,
    get_processing_tracker,
    reset_processing_tracker,
)


logger = logging.getLogger(__name__)


# Provider name aliases to normalize LLM outputs to canonical names
PROVIDER_ALIASES = {
    # Comtrade variations
    "un comtrade": "COMTRADE",
    "un_comtrade": "COMTRADE",
    "comtrade": "COMTRADE",
    "un": "COMTRADE",
    # World Bank variations
    "world bank": "WORLDBANK",
    "worldbank": "WORLDBANK",
    "wb": "WORLDBANK",
    # Statistics Canada variations
    "statistics canada": "STATSCAN",
    "stats canada": "STATSCAN",
    "statcan": "STATSCAN",
    "statscan": "STATSCAN",
    # Exchange rate variations
    "exchangerate": "EXCHANGERATE",
    "exchange rate": "EXCHANGERATE",
    "exchangerate-api": "EXCHANGERATE",
    "exchange-rate": "EXCHANGERATE",
    "exchange rate api": "EXCHANGERATE",
    # FRED variations (handle LLM adding extra text)
    "fred": "FRED",
    "fred (federal reserve)": "FRED",
    "federal reserve": "FRED",
    # Other providers
    "imf": "IMF",
    "international monetary fund": "IMF",
    "bis": "BIS",
    "bank for international settlements": "BIS",
    "eurostat": "EUROSTAT",
    "oecd": "OECD",
    "coingecko": "COINGECKO",
    "coin gecko": "COINGECKO",
}


def normalize_provider_name(provider: str) -> str:
    """Normalize provider name to canonical form.

    Handles variations like 'UN COMTRADE', 'UN Comtrade', 'World Bank', etc.
    Returns uppercase canonical name like 'COMTRADE', 'WORLDBANK', etc.
    """
    if not provider:
        return provider

    # Try exact match first (case-insensitive)
    normalized = PROVIDER_ALIASES.get(provider.lower().strip())
    if normalized:
        return normalized

    # Fallback: just uppercase the original
    return provider.upper().strip()


def _filter_valid_data(data: List[NormalizedData]) -> List[NormalizedData]:
    """Filter None values from data list and return only valid entries.

    CRITICAL FIX: Parallel fetches can return [None, NormalizedData, None].
    This helper ensures safe access to data elements.

    Args:
        data: List that may contain None elements

    Returns:
        List with only valid NormalizedData objects
    """
    if not data:
        return []
    return [d for d in data if d is not None]


def _safe_get_source(data: List[NormalizedData]) -> str:
    """Safely get source from data list, handling None elements.

    Args:
        data: List that may contain None elements

    Returns:
        Source string or "UNKNOWN" if not available
    """
    valid = _filter_valid_data(data)
    if valid and valid[0].metadata:
        return valid[0].metadata.source or "UNKNOWN"
    return "UNKNOWN"


class QueryService:
    # Bump when cache semantics change so stale entries from old logic are not reused.
    CACHE_KEY_VERSION = "2026-02-21.2"
    MAX_FALLBACK_CACHE_ENTRIES = 1024

    def __init__(
        self,
        openrouter_key: str,
        fred_key: Optional[str],
        comtrade_key: Optional[str],
        coingecko_key: Optional[str] = None,
        settings: Optional[Settings] = None
    ) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()
        self.openrouter = OpenRouterService(openrouter_key, self.settings)

        # Initialize metadata search service if LLM provider is available
        metadata_search = None
        if self.openrouter.llm_provider:
            metadata_search = MetadataSearchService(self.openrouter.llm_provider)
            logger.info("✅ Metadata search service initialized with LLM provider")
        else:
            logger.warning("⚠️ Metadata search service not available (no LLM provider)")

        # Initialize providers with metadata search for intelligent discovery
        self.fred_provider = FREDProvider(fred_key)
        self.world_bank_provider = WorldBankProvider(metadata_search_service=metadata_search)
        self.comtrade_provider = ComtradeProvider(comtrade_key)
        self.statscan_provider = StatsCanProvider(metadata_search_service=metadata_search)
        self.imf_provider = IMFProvider(metadata_search_service=metadata_search)
        self.bis_provider = BISProvider(metadata_search_service=metadata_search)
        self.eurostat_provider = EurostatProvider(metadata_search_service=metadata_search)
        self.oecd_provider = OECDProvider(metadata_search_service=metadata_search)

        # ExchangeRate-API: Uses open access by default, API key optional
        self.exchangerate_provider = ExchangeRateProvider(self.settings.exchangerate_api_key)

        # CoinGecko: Cryptocurrency prices and market data
        self.coingecko_provider = CoinGeckoProvider(coingecko_key)

        # Semantic provider router (default): semantic-router + LiteLLM fallback.
        self.semantic_provider_router: Optional[SemanticProviderRouter] = None
        if self.settings.use_semantic_provider_router:
            self.semantic_provider_router = SemanticProviderRouter(settings=self.settings)
            logger.info("🧭 SemanticProviderRouter enabled (USE_SEMANTIC_PROVIDER_ROUTER=true)")

        # Optional hybrid router: deterministic candidates + LLM ranking.
        # Kept as fallback/legacy path when semantic provider router is disabled.
        self.hybrid_router: Optional[HybridRouter] = None
        if self.settings.use_hybrid_router and not self.settings.use_semantic_provider_router:
            self.hybrid_router = HybridRouter(llm_provider=self.openrouter.llm_provider)
            logger.info("🧠 HybridRouter enabled (USE_HYBRID_ROUTER=true)")

        # Deterministic baseline router (single source of routing truth).
        self.unified_router = UnifiedRouter()
        # Small in-memory cache to avoid repeated cross-provider fallback scans.
        self._fallback_provider_cache: "OrderedDict[Tuple[str, str, Tuple[str, ...]], List[str]]" = OrderedDict()
        # Shared parse/routing/validation stages used by multiple execution paths.
        self.pipeline = QueryPipeline(self)

    @staticmethod
    def _normalize_provider_alias(provider: Optional[str]) -> Optional[str]:
        """Normalize provider aliases to canonical provider names."""
        if not provider:
            return None
        return normalize_provider_name(provider)

    def _detect_explicit_provider(self, query: str) -> Optional[str]:
        """
        Detect if user explicitly requests a specific data provider.
        Returns provider name if found, None otherwise.

        This ensures user's explicit choice is always honored, regardless of LLM interpretation.
        """
        query_lower = query.lower()

        # Provider keywords with their variations
        provider_patterns = {
            "OECD": ["oecd", "from oecd", "using oecd", "via oecd", "according to oecd", "oecd data"],
            "FRED": ["fred", "from fred", "using fred", "via fred", "federal reserve", "st. louis fed", "stlouisfed"],
            "WORLDBANK": ["world bank", "worldbank", "from world bank", "using world bank", "world bank data"],
            "Comtrade": ["comtrade", "un comtrade", "from comtrade", "using comtrade", "united nations comtrade"],
            "StatsCan": ["statscan", "statistics canada", "stats canada", "from statscan", "using statscan"],
            "IMF": ["imf", "from imf", "using imf", "international monetary fund", "from the imf"],
            "BIS": ["bis", "from bis", "using bis", "bank for international settlements"],
            "Eurostat": ["eurostat", "from eurostat", "using eurostat", "eu statistics", "european statistics"],
            "ExchangeRate": ["exchangerate", "exchange rate api", "from exchangerate"],
            "CoinGecko": ["coingecko", "coin gecko", "from coingecko", "using coingecko"]
        }

        # Check each provider's patterns
        for provider, patterns in provider_patterns.items():
            for pattern in patterns:
                if pattern in query_lower:
                    return provider

    def _extract_country_from_query(self, query: str) -> Optional[str]:
        """
        Extract first country code from query using CountryResolver.

        Returns:
            ISO Alpha-2 country code if found, else None
        """
        countries = self._extract_countries_from_query(query)
        return countries[0] if countries else None

    def _extract_countries_from_query(self, query: str) -> List[str]:
        """
        Extract all country codes from query in appearance order.

        Returns:
            List of ISO Alpha-2 country codes.
        """
        countries = CountryResolver.detect_all_countries_in_query(query)
        if countries:
            logger.info("🌍 Fallback country extraction found countries: %s", countries)
        return countries

    def _apply_country_overrides(self, intent: ParsedIntent, query: str) -> None:
        """
        Apply geography overrides when query text clearly specifies country context
        but LLM output defaults to US/no country.

        Rules:
        - If query names 1 non-US country and intent defaults to US/no country -> set `country`.
        - If query names multiple countries and intent defaults to US/no country -> set `countries`.
        """
        if intent.parameters is None:
            intent.parameters = {}

        extracted_countries = self._extract_countries_from_query(query)
        if not extracted_countries:
            return

        current_country = str(intent.parameters.get("country", "") or "")
        current_countries_raw = intent.parameters.get("countries")
        current_countries = []
        if isinstance(current_countries_raw, list):
            current_countries = [str(c) for c in current_countries_raw if c is not None]

        def _is_us(value: str) -> bool:
            return value.strip().lower() in {"us", "usa", "united states", "america"}

        defaulted_to_us_or_empty = (
            (not current_country and not current_countries)
            or (_is_us(current_country) and not current_countries)
            or (len(current_countries) == 1 and _is_us(current_countries[0]))
        )

        if not defaulted_to_us_or_empty:
            return

        # Multi-country override (preserve query order from CountryResolver)
        if len(extracted_countries) > 1:
            non_us = [c for c in extracted_countries if c.upper() != "US"]
            if non_us:
                previous = current_country or (",".join(current_countries) if current_countries else "")
                intent.parameters.pop("country", None)
                intent.parameters["countries"] = extracted_countries
                logger.info(
                    "🌍 Country Override (multi): '%s' -> %s (query explicitly names multiple countries)",
                    previous,
                    extracted_countries,
                )
            return

        # Single-country override
        extracted_country = extracted_countries[0]
        if extracted_country.upper() != "US":
            previous = current_country or (current_countries[0] if current_countries else "")
            intent.parameters["country"] = extracted_country
            intent.parameters.pop("countries", None)
            logger.info(
                "🌍 Country Override: '%s' -> '%s' (query explicitly mentions non-US country)",
                previous,
                extracted_country,
            )

    async def _select_routed_provider(self, intent: ParsedIntent, query: str) -> str:
        """
        Select provider using deterministic router, optionally enhanced by
        SemanticProviderRouter (default) or HybridRouter (legacy fallback path).
        """
        params = intent.parameters or {}
        raw_countries = params.get("countries")
        countries = raw_countries if isinstance(raw_countries, list) else []
        routed_provider = normalize_provider_name(intent.apiProvider or "")
        deterministic_confidence = 0.0
        deterministic_match_type = "legacy"
        deterministic_decision = None
        try:
            deterministic_decision = self.unified_router.route(
                query=query,
                indicators=intent.indicators,
                country=params.get("country"),
                countries=countries,
                llm_provider=intent.apiProvider,
            )
            routed_provider = normalize_provider_name(deterministic_decision.provider)
            deterministic_confidence = float(deterministic_decision.confidence or 0.0)
            deterministic_match_type = str(deterministic_decision.match_type or "deterministic").lower()
            logger.info(
                "🧭 UnifiedRouter baseline: %s (conf=%.2f, type=%s)",
                routed_provider,
                deterministic_decision.confidence,
                deterministic_decision.match_type,
            )
        except Exception as exc:
            logger.warning(
                "UnifiedRouter baseline failed, falling back to legacy deterministic router: %s",
                exc,
            )
            routed_provider = ProviderRouter.route_provider(intent, query)

        routed_provider = ProviderRouter.correct_coingecko_misrouting(
            routed_provider,
            query,
            intent.indicators,
        )

        if self.semantic_provider_router:
            try:
                decision = await self.semantic_provider_router.route(
                    query=query,
                    indicators=intent.indicators,
                    country=params.get("country"),
                    countries=countries,
                    llm_provider_hint=intent.apiProvider,
                    baseline_decision=deterministic_decision,
                )
                semantic_provider = normalize_provider_name(decision.provider)
                semantic_provider = ProviderRouter.correct_coingecko_misrouting(
                    semantic_provider,
                    query,
                    intent.indicators,
                )
                semantic_confidence = float(getattr(decision, "confidence", 0.0) or 0.0)
                # Framework guardrail: preserve high-confidence deterministic decisions unless
                # semantic routing is materially stronger. This prevents low-similarity
                # semantic matches from overriding precise rule-based routing.
                if semantic_provider != routed_provider:
                    deterministic_locked = (
                        deterministic_confidence >= 0.88
                        and deterministic_match_type in {"explicit", "us_only", "indicator"}
                    )
                    semantic_materially_stronger = semantic_confidence >= (deterministic_confidence + 0.05)
                    if deterministic_locked and not semantic_materially_stronger:
                        logger.info(
                            "🧭 Semantic override skipped: keep %s (deterministic conf=%.2f, semantic conf=%.2f)",
                            routed_provider,
                            deterministic_confidence,
                            semantic_confidence,
                        )
                        return routed_provider
                if semantic_provider != routed_provider:
                    logger.info(
                        "🧭 Semantic routing override: %s -> %s (%s)",
                        routed_provider,
                        semantic_provider,
                        decision.reasoning,
                    )
                return semantic_provider
            except Exception as exc:
                logger.warning("Semantic provider routing failed, using deterministic provider: %s", exc)
                return routed_provider

        if not self.hybrid_router:
            return routed_provider

        try:
            decision = await self.hybrid_router.route(
                query=query,
                indicators=intent.indicators,
                country=params.get("country"),
                countries=countries,
                llm_provider_hint=intent.apiProvider,
            )
            hybrid_provider = normalize_provider_name(decision.provider)
            hybrid_provider = ProviderRouter.correct_coingecko_misrouting(
                hybrid_provider,
                query,
                intent.indicators,
            )
            if hybrid_provider != routed_provider:
                logger.info(
                    "🧠 Hybrid routing override: %s -> %s (%s)",
                    routed_provider,
                    hybrid_provider,
                    decision.reasoning,
                )
            return hybrid_provider
        except Exception as exc:
            logger.warning("Hybrid routing failed, using deterministic provider: %s", exc)
            return routed_provider

    def _tokenize_indicator_terms(self, text: str) -> set[str]:
        """Tokenize indicator text into comparable semantic terms."""
        if not text:
            return set()

        stop_words = {
            "the", "a", "an", "of", "for", "in", "to", "and", "or",
            "show", "get", "find", "data", "series", "indicator",
            "country", "countries", "from", "with", "by", "on", "at",
            "current", "constant", "annual", "monthly", "quarterly",
            "percent", "percentage", "ratio", "share", "rate", "index",
            "gdp", "value", "values",
        }
        geo_terms = {
            alias.strip().lower()
            for alias in CountryResolver.COUNTRY_ALIASES.keys()
            if alias and " " not in alias
        }

        raw_terms = set(re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")))
        terms: set[str] = set()
        for term in raw_terms:
            if len(term) <= 2 or term in stop_words or term in geo_terms:
                continue
            terms.add(term)
            if term.endswith("ies") and len(term) > 4:
                terms.add(term[:-3] + "y")
            elif term.endswith("s") and len(term) > 3:
                terms.add(term[:-1])
        return terms

    def _extract_indicator_cues(self, text: str) -> set[str]:
        """Extract high-signal semantic cues for intent/indicator consistency checks."""
        if not text:
            return set()

        text_lower = text.lower()
        cue_map = {
            "import": {"import", "imports"},
            "export": {"export", "exports"},
            "trade_balance": {"trade balance", "trade surplus", "trade deficit"},
            "debt": {"debt", "liability", "liabilities"},
            "debt_service": {"debt service", "debt service ratio", "dsr"},
            "debt_gdp_ratio": {
                "debt to gdp",
                "debt-to-gdp",
                "debt as % of gdp",
                "debt as percentage of gdp",
                "% of gdp debt",
                "gdp to debt ratio",
                "gdp/debt ratio",
            },
            "public_debt": {
                "government debt",
                "public debt",
                "sovereign debt",
                "national debt",
                "central government debt",
                "general government debt",
            },
            "household_debt": {"household debt"},
            "unemployment": {"unemployment", "jobless"},
            "inflation": {"inflation", "consumer price", "cpi"},
            "savings": {"saving", "savings"},
            "credit": {"credit", "lending", "loan"},
            "exchange_rate": {"exchange rate", "forex", "fx", "reer", "neer", "effective exchange rate"},
            "gdp": {"gdp", "gross domestic product"},
        }

        cues: set[str] = set()
        for cue, phrases in cue_map.items():
            if any(phrase in text_lower for phrase in phrases):
                cues.add(cue)
        return cues

    def _series_text_for_relevance(self, series: Any) -> str:
        """Build a comparable text blob from a series metadata payload."""
        metadata = None
        if series is not None and hasattr(series, "metadata"):
            metadata = getattr(series, "metadata", None)
        elif isinstance(series, dict):
            metadata = series.get("metadata")

        if metadata is None:
            return ""

        if hasattr(metadata, "model_dump"):
            meta_dict = metadata.model_dump()
        elif isinstance(metadata, dict):
            meta_dict = metadata
        else:
            meta_dict = {}

        return " ".join(
            str(meta_dict.get(key) or "")
            for key in ("indicator", "seriesId", "description", "source", "country", "unit")
        ).strip()

    def _score_series_relevance(self, query: str, series: Any) -> float:
        """Score semantic relevance of one returned series to the original query."""
        query_text = str(query or "").lower()
        series_text = self._series_text_for_relevance(series).lower()
        if not series_text:
            return -1.0

        score = 0.0
        query_cues = self._extract_indicator_cues(query_text)
        series_cues = self._extract_indicator_cues(series_text)

        if query_cues:
            cue_overlap = query_cues & series_cues
            score += float(len(cue_overlap)) * 2.5
            if not cue_overlap:
                score -= 2.0

        query_terms = self._tokenize_indicator_terms(query_text)
        series_terms = self._tokenize_indicator_terms(series_text)
        if query_terms and series_terms:
            lexical_overlap = len(query_terms & series_terms)
            score += min(2.5, lexical_overlap * 0.35)

        ratio_patterns = [
            "% of gdp",
            "as % of gdp",
            "as percent of gdp",
            "as percentage of gdp",
            "share of gdp",
            "to gdp ratio",
            "ratio to gdp",
            "as share of gdp",
        ]
        has_ratio_query = any(pattern in query_text for pattern in ratio_patterns)
        has_ratio_series = any(pattern in series_text for pattern in ratio_patterns)
        if has_ratio_query:
            if has_ratio_series:
                score += 2.5
            else:
                score -= 1.8

        # Penalize directional mismatches.
        if "import" in query_cues and "import" not in series_cues and "trade_balance" not in series_cues:
            score -= 2.2
        if "export" in query_cues and "export" not in series_cues and "trade_balance" not in series_cues:
            score -= 2.2
        if "trade_balance" in query_cues and "trade_balance" not in series_cues:
            score -= 2.2
        if "debt_service" in query_cues and "debt_service" not in series_cues:
            score -= 2.2
        if "debt_gdp_ratio" in query_cues:
            if "debt_gdp_ratio" in series_cues:
                score += 2.5
            else:
                score -= 2.8
            if "debt_service" in series_cues:
                score -= 3.0
        if "public_debt" in query_cues and "public_debt" not in series_cues:
            if ("household_debt" in series_cues) or ("credit" in series_cues) or ("debt_service" in series_cues):
                score -= 2.4
        if "credit" in query_cues and "credit" not in series_cues:
            score -= 1.8
        if "exchange_rate" in query_cues and "exchange_rate" not in series_cues:
            score -= 1.8

        # Generic GDP series should not dominate directional/ratio trade queries.
        if "gdp (current us$)" in series_text and ({"import", "export", "trade_balance"} & query_cues):
            score -= 3.0

        # Trade flow totals are usually not ratio indicators.
        if has_ratio_query and "total trade" in series_text:
            score -= 1.5

        return score

    def _rerank_data_by_query_relevance(self, query: str, data: List[Any]) -> List[Any]:
        """
        Reorder (and lightly filter) returned series by semantic relevance to query.

        This is a framework-level guardrail against agent over-decomposition where
        unrelated series can be returned before the intended concept.
        """
        if not data:
            return data

        scored: List[tuple[float, int, Any]] = []
        for idx, series in enumerate(data):
            scored.append((self._score_series_relevance(query, series), idx, series))

        scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        reranked = [item[2] for item in scored]

        top_score = scored[0][0] if scored else 0.0
        if top_score < 0.8:
            return reranked

        # Keep all strong matches; discard clearly irrelevant tail when we have good matches.
        filtered = [series for score, _, series in scored if score >= max(0.0, top_score - 3.0)]
        return filtered or reranked

    def _provider_supports_country_for_options(self, provider: str, country_iso2: Optional[str]) -> bool:
        """Lightweight country-coverage filter for clarification options."""
        if not country_iso2:
            return True

        provider_upper = normalize_provider_name(provider)
        iso2 = country_iso2.upper()

        if provider_upper == "EUROSTAT":
            return CountryResolver.is_eu_member(iso2)
        if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
            return iso2 == "CA"
        if provider_upper == "FRED":
            return iso2 == "US"
        if provider_upper == "BIS":
            return iso2 in BISProvider.BIS_SUPPORTED_COUNTRIES
        return True

    def _collect_indicator_choice_options(
        self,
        query: str,
        intent: ParsedIntent,
        max_options: int = 4,
    ) -> List[str]:
        """
        Build ranked indicator options across plausible providers for user clarification.
        """
        if not intent:
            return []

        raw_query = str(query or "").strip()
        indicator_query = self._select_indicator_query_for_resolution(intent) or raw_query
        if raw_query:
            raw_cues = self._extract_indicator_cues(raw_query)
            indicator_cues = self._extract_indicator_cues(indicator_query)
            high_signal_raw_cues = {cue for cue in raw_cues if cue not in {"gdp"}}
            if (
                ("debt_gdp_ratio" in raw_cues and "debt_gdp_ratio" not in indicator_cues)
                or (high_signal_raw_cues and not (high_signal_raw_cues & indicator_cues))
            ):
                indicator_query = raw_query
        if not indicator_query:
            return []

        target_countries = self._collect_target_countries(intent.parameters)
        target_country = target_countries[0] if target_countries else None
        target_iso2 = [
            iso2
            for iso2 in (self._normalize_country_to_iso2(country) for country in target_countries)
            if iso2
        ]

        primary_provider = normalize_provider_name(intent.apiProvider or "")
        fallback_candidates = self._get_fallback_providers(
            primary_provider,
            indicator_query,
            country=target_country,
            countries=target_countries,
        )

        provider_candidates = []
        for provider_name in [
            primary_provider,
            *fallback_candidates,
            "IMF",
            "WORLDBANK",
            "BIS",
            "OECD",
            "EUROSTAT",
            "FRED",
        ]:
            normalized = normalize_provider_name(provider_name)
            if normalized and normalized not in provider_candidates:
                provider_candidates.append(normalized)

        resolver = get_indicator_resolver()
        scored_options: List[tuple[float, str, str]] = []
        seen_codes: set[tuple[str, str]] = set()
        provider_labels = {
            "WORLDBANK": "WorldBank",
            "EUROSTAT": "Eurostat",
            "STATSCAN": "StatsCan",
            "COMTRADE": "Comtrade",
        }

        for provider_name in provider_candidates:
            # Skip providers that clearly don't cover the requested country context.
            if target_iso2 and not any(
                self._provider_supports_country_for_options(provider_name, iso2)
                for iso2 in target_iso2
            ):
                continue

            try:
                resolved = resolver.resolve(
                    indicator_query,
                    provider=provider_name,
                    country=target_country,
                    countries=target_countries or None,
                )
            except Exception:
                resolved = None

            if not resolved or not resolved.code or resolved.confidence < 0.55:
                continue

            code_key = (provider_name, str(resolved.code).upper())
            if code_key in seen_codes:
                continue
            seen_codes.add(code_key)

            synthetic_series = {
                "metadata": {
                    "indicator": resolved.name or "",
                    "seriesId": resolved.code,
                    "source": provider_name,
                }
            }
            relevance_score = self._score_series_relevance(query, synthetic_series)
            if relevance_score < -2.0:
                continue

            combined_score = float(resolved.confidence) + (0.12 * relevance_score)
            provider_label = provider_labels.get(provider_name, provider_name)
            option_name = str(resolved.name or resolved.code or "").replace("_", " ").strip()
            option_text = f"[{provider_label}] {option_name} ({resolved.code})"
            scored_options.append((combined_score, option_text, provider_name))

        scored_options.sort(key=lambda item: item[0], reverse=True)
        return [option for _, option, _ in scored_options[:max_options]]

    def _needs_indicator_clarification(
        self,
        query: str,
        data: List[Any],
        intent: Optional[ParsedIntent] = None,
    ) -> bool:
        """
        Determine whether returned data looks semantically uncertain for the query.
        """
        if not data:
            return False

        scored: List[tuple[float, Any]] = [
            (self._score_series_relevance(query, series), series)
            for series in data
        ]
        scored.sort(key=lambda item: item[0], reverse=True)

        top_score, top_series = scored[0]
        top_meta = getattr(top_series, "metadata", None)
        query_cues = self._extract_indicator_cues(query)
        top_series_cues = self._extract_indicator_cues(self._series_text_for_relevance(top_series))
        high_signal_query_cues = {cue for cue in query_cues if cue not in {"gdp"}}

        # Cross-check with provider-agnostic resolver for framework-level ambiguity detection.
        # If the best canonical match disagrees with returned series/provider, ask clarification.
        try:
            resolver = get_indicator_resolver()
            target_countries = self._collect_target_countries(intent.parameters) if intent else []
            target_country = target_countries[0] if target_countries else None
            canonical = resolver.resolve(
                query,
                country=target_country,
                countries=target_countries or None,
            )

            if canonical and canonical.confidence >= 0.8 and top_meta:
                top_provider = normalize_provider_name(getattr(top_meta, "source", "") or "")
                canonical_provider = normalize_provider_name(canonical.provider or "")
                top_indicator = str(getattr(top_meta, "indicator", "") or "")
                top_series_id = str(getattr(top_meta, "seriesId", "") or "")
                top_code = top_series_id or (
                    top_indicator if self._looks_like_provider_indicator_code(top_provider, top_indicator) else ""
                )
                canonical_code = str(canonical.code or "")

                def _codes_match(lhs: str, rhs: str) -> bool:
                    left = str(lhs or "").upper().strip()
                    right = str(rhs or "").upper().strip()
                    if not left or not right:
                        return False
                    if left == right:
                        return True
                    left_prefix = re.split(r"[_.]", left)[0]
                    right_prefix = re.split(r"[_.]", right)[0]
                    if len(left_prefix) >= 5 and left_prefix == right_prefix:
                        return True
                    return False

                if top_provider and canonical_provider and top_code and canonical_code:
                    if top_provider != canonical_provider and top_score < 2.0:
                        return True
                    if not _codes_match(top_code, canonical_code) and top_score < 2.0:
                        return True
                    # Canonical match aligns with returned series/provider.
                    if top_provider == canonical_provider and _codes_match(top_code, canonical_code):
                        return False
        except Exception:
            pass

        if "debt_gdp_ratio" in query_cues and "debt_gdp_ratio" not in top_series_cues:
            return True
        if (
            "public_debt" in query_cues
            and "public_debt" not in top_series_cues
            and ("debt_service" in top_series_cues or "credit" in top_series_cues)
        ):
            return True
        if high_signal_query_cues and not (high_signal_query_cues & top_series_cues) and top_score < 2.0:
            return True
        if top_score < 0.25:
            return True

        if len(scored) > 1:
            score_gap = scored[0][0] - scored[1][0]
            if score_gap < 0.2:
                second_series_cues = self._extract_indicator_cues(
                    self._series_text_for_relevance(scored[1][1])
                )
                if top_series_cues != second_series_cues:
                    return True

        return False

    def _build_uncertain_result_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        data: List[Any],
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """
        Return a clarification response with options when series selection is uncertain.
        """
        if not intent or not self._needs_indicator_clarification(query, data, intent):
            return None

        options = self._collect_indicator_choice_options(query, intent)
        if len(options) < 2:
            return None

        top_series = data[0] if data else None
        top_meta = getattr(top_series, "metadata", None) if top_series else None
        current_label = (
            f"{getattr(top_meta, 'indicator', 'Unknown indicator')} "
            f"from {getattr(top_meta, 'source', 'unknown source')}"
            if top_meta else "Unknown indicator"
        )

        clarification_questions = [
            "I found multiple plausible indicators and the current match is uncertain.",
            f"Current match: {current_label}",
            "Please choose one option:",
        ]
        clarification_questions.extend(
            f"{idx}. {option}" for idx, option in enumerate(options, start=1)
        )
        clarification_questions.append(
            "Reply with the option number (for example, 1) or the exact indicator text you want."
        )

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            processingSteps=processing_steps,
        )

    def _looks_like_provider_indicator_code(self, provider: str, indicator: str) -> bool:
        """Heuristic check for provider-native indicator code formats."""
        if not indicator:
            return False

        indicator_text = str(indicator).strip()
        if not indicator_text:
            return False

        provider_upper = normalize_provider_name(provider)
        code_upper = indicator_text.upper()

        if provider_upper in {"WORLDBANK", "WORLD BANK"}:
            # Examples: NE.IMP.GNFS.ZS, NY.GDP.MKTP.CD
            return bool(re.fullmatch(r"[A-Z]{2}\.[A-Z0-9]{2,}(?:\.[A-Z0-9]{2,}){1,4}", code_upper))

        if provider_upper == "BIS":
            # Examples: WS_CBPOL, WS_SPP, BIS.CBPOL
            return bool(
                code_upper.startswith("WS_")
                or re.fullmatch(r"BIS\.[A-Z0-9_]{3,}", code_upper)
            )

        if provider_upper == "IMF":
            # IMF codes are often uppercase with underscores/dots.
            return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_\.]{2,}", code_upper))

        if provider_upper == "FRED":
            # FRED series IDs are usually uppercase alphanumeric (no spaces).
            return bool(re.fullmatch(r"[A-Z0-9]{3,}", code_upper))

        if provider_upper == "EUROSTAT":
            return bool(re.fullmatch(r"[A-Z0-9_@\.]{4,}", code_upper))

        if provider_upper == "OECD":
            # Examples: DSD_...@DF_..., CPI, IRLT
            return bool(re.fullmatch(r"[A-Z0-9_@\.]{5,}", code_upper))

        if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
            return bool(re.fullmatch(r"[A-Z0-9_]{3,}", code_upper))

        return False

    def _is_resolved_indicator_plausible(
        self,
        provider: str,
        indicator_query: str,
        resolved_code: str,
    ) -> bool:
        """
        Lightweight semantic plausibility check for resolved provider codes.

        Prevents high-confidence but semantically off-target code matches from
        overriding clearer natural-language intent (especially for opaque FRED IDs).
        """
        provider_upper = normalize_provider_name(provider)
        query_cues = self._extract_indicator_cues(indicator_query or "")
        code_upper = str(resolved_code or "").upper()

        if not query_cues:
            return True

        if provider_upper == "FRED":
            domain_tokens = {
                "credit": ("CREDIT", "LOAN", "LEND", "TOTBKCR", "BUSLOANS", "REVOL", "NONREV", "TOTALSL"),
                "inflation": ("CPI", "PPI", "PCE", "DEFL", "INFL"),
                "exchange_rate": ("DEX", "EXCH", "XRU", "REER"),
                "trade_balance": ("BOP", "TRADE", "NETEXP"),
                "import": ("IMP", "IMPORT"),
                "export": ("EXP", "EXPORT"),
            }

            for cue, tokens in domain_tokens.items():
                if cue in query_cues and not any(token in code_upper for token in tokens):
                    return False

        if provider_upper == "BIS":
            # Guardrail: generic debt-to-GDP queries should not resolve to BIS debt-service series.
            if "debt_gdp_ratio" in query_cues:
                if code_upper == "WS_DSR":
                    return False
                # WS_TC is valid for BIS credit/household debt contexts, but not generic public debt.
                if code_upper == "WS_TC" and not (
                    query_cues & {"credit", "household_debt", "debt_service"}
                ):
                    return False

            if "debt_service" in query_cues and code_upper != "WS_DSR":
                return False

        return True

    def _indicator_resolution_threshold(self, indicator_query: str, resolved_source: str) -> float:
        """
        Dynamic acceptance threshold for resolver output.

        Long natural-language indicator prompts and directional trade queries tend to
        score lower in lexical systems; use a slightly lower threshold there while
        keeping strict defaults for weakly-signaled queries.
        """
        threshold = 0.70
        normalized_query = str(indicator_query or "").strip().lower()
        cue_set = self._extract_indicator_cues(normalized_query)

        if cue_set:
            threshold = 0.60
        if len(normalized_query.split()) >= 6:
            threshold = min(threshold, 0.62)
        if resolved_source in {"catalog", "translator"}:
            threshold = min(threshold, 0.60)

        return threshold

    def _select_indicator_query_for_resolution(self, intent: ParsedIntent) -> str:
        """
        Pick the best query string for indicator resolution.

        Uses LLM indicator text by default, but falls back to the original user
        query when semantic cues clearly mismatch.
        """
        if not intent.indicators:
            return ""

        indicator_query = str(intent.indicators[0] or "").strip()
        if not indicator_query:
            return ""

        original_query = str(intent.originalQuery or "").strip()
        if not original_query:
            return indicator_query

        ratio_patterns = [
            "% of gdp",
            "as % of gdp",
            "as percent of gdp",
            "as percentage of gdp",
            "share of gdp",
            "to gdp ratio",
            "ratio to gdp",
            "as share of gdp",
        ]
        original_lower = original_query.lower()
        indicator_lower = indicator_query.lower()
        has_ratio_original = any(pattern in original_lower for pattern in ratio_patterns)
        has_ratio_indicator = any(pattern in indicator_lower for pattern in ratio_patterns)
        if has_ratio_original and not has_ratio_indicator:
            logger.info(
                "🔎 Indicator dropped GDP-ratio context. Using original query for resolution."
            )
            return original_query

        original_cues = self._extract_indicator_cues(original_query)
        indicator_cues = self._extract_indicator_cues(indicator_query)
        if original_cues and not (original_cues & indicator_cues):
            logger.info(
                "🔎 Indicator cue mismatch (original=%s, parsed=%s). Using original query for resolution.",
                sorted(original_cues),
                sorted(indicator_cues),
            )
            return original_query

        original_terms = self._tokenize_indicator_terms(original_query)
        indicator_terms = self._tokenize_indicator_terms(indicator_query)
        if original_terms and indicator_terms:
            overlap = len(original_terms & indicator_terms) / max(len(original_terms), 1)
            if overlap < 0.15:
                logger.info(
                    "🔎 Low indicator-term overlap (%.2f). Using original query for resolution.",
                    overlap,
                )
                return original_query

        return indicator_query

    def _extract_exchange_rate_params(self, params: dict, intent: ParsedIntent) -> dict:
        """
        Extract currency pair information from query and populate params.

        CRITICAL: This must be called BEFORE cache lookup to ensure each unique
        currency pair has its own cache entry. Without this, different currency
        queries could share the same incorrect cached data.

        Args:
            params: Current query parameters
            intent: Parsed intent with originalQuery

        Returns:
            Updated params with baseCurrency and targetCurrency populated
        """
        import re

        # If params already has both currencies, use them
        if params.get("baseCurrency") and params.get("targetCurrency"):
            logger.info(f"💱 Currency params already set: {params.get('baseCurrency')} -> {params.get('targetCurrency')}")
            return params

        params = {**params}  # Create a copy to avoid mutation

        # Currency code mapping for common names/symbols
        currency_name_map = {
            "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD",
            "euro": "EUR", "euros": "EUR", "eur": "EUR",
            "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "sterling": "GBP", "british pound": "GBP",
            "yen": "JPY", "jpy": "JPY", "japanese yen": "JPY",
            "yuan": "CNY", "cny": "CNY", "renminbi": "CNY", "rmb": "CNY", "chinese yuan": "CNY",
            "franc": "CHF", "chf": "CHF", "swiss franc": "CHF",
            "rupee": "INR", "inr": "INR", "indian rupee": "INR",
            "won": "KRW", "krw": "KRW", "korean won": "KRW",
            "real": "BRL", "brl": "BRL", "brazilian real": "BRL",
            "ruble": "RUB", "rub": "RUB", "russian ruble": "RUB",
            "peso": "MXN", "mxn": "MXN", "mexican peso": "MXN",
            "rand": "ZAR", "zar": "ZAR", "south african rand": "ZAR",
            "lira": "TRY", "try": "TRY", "turkish lira": "TRY",
            "canadian dollar": "CAD", "cad": "CAD", "loonie": "CAD",
            "australian dollar": "AUD", "aud": "AUD", "aussie dollar": "AUD",
            "singapore dollar": "SGD", "sgd": "SGD",
            "hong kong dollar": "HKD", "hkd": "HKD",
            "new zealand dollar": "NZD", "nzd": "NZD", "kiwi dollar": "NZD",
        }

        query_text = (intent.originalQuery or "").upper()

        # Extract currency codes using various patterns
        base_currency = params.get("baseCurrency")
        target_currency = params.get("targetCurrency")

        # Pattern 1: "X to Y" (e.g., "USD to EUR", "JPY to USD")
        to_match = re.search(r'\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b', query_text)
        if to_match:
            base_currency = to_match.group(1)
            target_currency = to_match.group(2)
            logger.info(f"💱 Extracted from 'X to Y' pattern: {base_currency} -> {target_currency}")

        # Pattern 2: "X/Y" or "X-Y" (e.g., "USD/EUR", "EUR-GBP")
        if not base_currency or not target_currency:
            slash_match = re.search(r'\b([A-Z]{3})[/\-]([A-Z]{3})\b', query_text)
            if slash_match:
                base_currency = slash_match.group(1)
                target_currency = slash_match.group(2)
                logger.info(f"💱 Extracted from 'X/Y' pattern: {base_currency} -> {target_currency}")

        # Pattern 3: "X vs Y" (e.g., "USD vs EUR")
        if not base_currency or not target_currency:
            vs_match = re.search(r'\b([A-Z]{3})\s+VS\.?\s+([A-Z]{3})\b', query_text)
            if vs_match:
                base_currency = vs_match.group(1)
                target_currency = vs_match.group(2)
                logger.info(f"💱 Extracted from 'X vs Y' pattern: {base_currency} -> {target_currency}")

        # Pattern 4: Try to find any currency codes in the query
        if not base_currency or not target_currency:
            # Look for 3-letter currency codes
            all_codes = re.findall(r'\b([A-Z]{3})\b', query_text)
            # Filter to known currency codes
            valid_codes = {"USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD",
                          "INR", "KRW", "BRL", "MXN", "ZAR", "TRY", "SGD", "HKD",
                          "NZD", "SEK", "NOK", "DKK", "THB", "MYR", "TWD", "RUB"}
            found_codes = [c for c in all_codes if c in valid_codes]
            if len(found_codes) >= 2 and not base_currency:
                base_currency = found_codes[0]
                target_currency = found_codes[1]
                logger.info(f"💱 Extracted from code search: {base_currency} -> {target_currency}")
            elif len(found_codes) == 1:
                # Single currency found - treat as "X to USD" or "USD to X"
                code = found_codes[0]
                if code == "USD":
                    # Query is about USD, but we need a target
                    # Default to EUR as most common pair
                    base_currency = "USD"
                    target_currency = params.get("targetCurrency") or "EUR"
                else:
                    # Other currency to USD
                    base_currency = code
                    target_currency = "USD"
                logger.info(f"💱 Single code found: {base_currency} -> {target_currency}")

        # Pattern 5: Try common currency names in lowercase query
        if not base_currency or not target_currency:
            query_lower = (intent.originalQuery or "").lower()
            found_currencies = []
            for name, code in currency_name_map.items():
                if name in query_lower:
                    if code not in [c[1] for c in found_currencies]:
                        # Find position for ordering
                        pos = query_lower.find(name)
                        found_currencies.append((pos, code))
            # Sort by position in query
            found_currencies.sort(key=lambda x: x[0])
            if len(found_currencies) >= 2:
                base_currency = found_currencies[0][1]
                target_currency = found_currencies[1][1]
                logger.info(f"💱 Extracted from currency names: {base_currency} -> {target_currency}")
            elif len(found_currencies) == 1:
                code = found_currencies[0][1]
                if code == "USD":
                    base_currency = "USD"
                    target_currency = params.get("targetCurrency") or "EUR"
                else:
                    base_currency = code
                    target_currency = "USD"
                logger.info(f"💱 Single currency name found: {base_currency} -> {target_currency}")

        # Apply defaults if still not found
        if not base_currency:
            base_currency = "USD"
            logger.info("💱 Defaulting baseCurrency to USD")
        if not target_currency:
            # Default to EUR if base is USD, otherwise to USD
            target_currency = "EUR" if base_currency == "USD" else "USD"
            logger.info(f"💱 Defaulting targetCurrency to {target_currency}")

        params["baseCurrency"] = base_currency
        params["targetCurrency"] = target_currency

        return params

    def _build_cache_params(self, provider: str, params: dict) -> dict:
        """
        Build normalized cache parameters with explicit schema versioning.

        This decouples cache validity from implementation details and allows safe,
        global invalidation when routing/fetch semantics change.
        """
        cache_params = dict(params or {})
        cache_params["_cache_version"] = self.CACHE_KEY_VERSION
        cache_params["_provider"] = normalize_provider_name(provider)
        return cache_params

    def _serialize_cache_query(self, cache_params: dict) -> str:
        """Serialize cache params deterministically for Redis cache key input."""
        try:
            return json.dumps(cache_params, sort_keys=True, separators=(",", ":"), default=str)
        except Exception:
            # Keep a deterministic fallback for non-serializable values.
            return str(sorted(cache_params.items()))

    def _coerce_parsed_intent(self, raw_intent: Any, query: str) -> Optional[ParsedIntent]:
        """
        Convert parsed intent payloads (dict/model) to ParsedIntent and preserve original query.
        """
        if raw_intent is None:
            return None

        try:
            if isinstance(raw_intent, ParsedIntent):
                intent = raw_intent.model_copy(deep=True)
            elif isinstance(raw_intent, dict):
                intent = ParsedIntent.model_validate(raw_intent)
            else:
                return None
        except ValidationError:
            return None

        if not intent.originalQuery:
            intent.originalQuery = query
        return intent

    async def _get_from_cache(self, provider: str, params: dict):
        """
        Get data from cache (Redis first, then in-memory).

        Args:
            provider: Data provider name
            params: Query parameters

        Returns:
            Cached data if available, None otherwise
        """
        cache_params = self._build_cache_params(provider, params)

        # Try Redis cache first
        try:
            redis_cache = await get_redis_cache()
            query_key = self._serialize_cache_query(cache_params)
            cached_data = await redis_cache.get(provider, query_key, cache_params)
            if cached_data:
                logger.info(f"Redis cache hit for {provider}")
                return cached_data
        except Exception as e:
            logger.warning(f"Redis cache error: {e}, falling back to in-memory")

        # Fallback to in-memory cache
        cached_data = cache_service.get_data(provider, cache_params)
        if cached_data:
            logger.info(f"In-memory cache hit for {provider}")
            return cached_data

        return None

    async def _save_to_cache(self, provider: str, params: dict, data: list):
        """
        Save data to both Redis and in-memory cache.

        Args:
            provider: Data provider name
            params: Query parameters
            data: Data to cache
        """
        cache_params = self._build_cache_params(provider, params)

        # Save to Redis cache
        try:
            redis_cache = await get_redis_cache()
            query_key = self._serialize_cache_query(cache_params)
            await redis_cache.set(provider, query_key, data, cache_params)
            logger.debug(f"Saved to Redis cache: {provider}")
        except Exception as e:
            logger.warning(f"Failed to save to Redis: {e}")

        # Always save to in-memory cache as backup
        cache_service.cache_data(provider, cache_params, data)
        logger.debug(f"Saved to in-memory cache: {provider}")

    def _collect_target_countries(self, parameters: Optional[dict]) -> List[str]:
        """Extract ordered country context from query parameters."""
        if not parameters:
            return []

        countries: List[str] = []
        for key in ("countries", "reporters", "partner"):
            value = parameters.get(key)
            if isinstance(value, list):
                countries.extend(str(item) for item in value if item)
            elif value:
                countries.append(str(value))

        for key in ("country", "reporter"):
            value = parameters.get(key)
            if value:
                countries.append(str(value))

        # Preserve order while removing duplicates.
        return list(dict.fromkeys(countries))

    @staticmethod
    def _normalize_country_to_iso2(country: Optional[str]) -> Optional[str]:
        """Normalize country identifiers/names to ISO2 codes when possible."""
        if not country:
            return None

        country_text = str(country).strip()
        if not country_text:
            return None

        normalized = CountryResolver.normalize(country_text)
        if normalized:
            return normalized

        # Allow ISO3 inputs (e.g., GBR) and normalize to ISO2 when known.
        iso2 = CountryResolver.to_iso2(country_text.upper())
        if iso2:
            return iso2

        return None

    def _get_fallback_providers(
        self,
        primary_provider: str,
        indicator: Optional[str] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Get ordered list of fallback providers for a given primary provider.

        INFRASTRUCTURE FIX: This method now uses THREE sources for smarter fallbacks:
        1. IndicatorResolver database search (330K+ indicators) - HIGHEST priority
        2. Catalog-based fallbacks (YAML concept definitions)
        3. General fallback chains (provider relationships)

        The IndicatorResolver search finds which providers ACTUALLY have the indicator,
        rather than relying on static mappings.

        Args:
            primary_provider: The primary provider that failed
            indicator: Optional indicator name for smarter fallbacks
            country: Optional single-country context
            countries: Optional multi-country context

        Returns:
            List of fallback provider names to try in order
        """
        primary_upper = primary_provider.upper()
        cache_key: Optional[Tuple[str, str, Tuple[str, ...]]] = None
        if indicator:
            normalized_geo = tuple(
                sorted({
                    self._normalize_country_to_iso2(str(c)) or str(c).strip().upper()
                    for c in [*(countries or []), country]
                    if c
                })
            )
            cache_key = (
                primary_upper,
                str(indicator).strip().lower(),
                normalized_geo,
            )
            cached = self._fallback_provider_cache.get(cache_key)
            if cached:
                # LRU refresh on read.
                self._fallback_provider_cache.move_to_end(cache_key)
                return list(cached)

        fallback_list = []
        try:
            fallback_list = [
                normalize_provider_name(provider_name)
                for provider_name in self.unified_router.get_fallbacks(primary_upper)
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

        # INFRASTRUCTURE FIX: Use IndicatorResolver to find providers that have this indicator
        # This searches the 330K+ indicator database for actual matches
        if indicator:
            try:
                from .indicator_resolver import get_indicator_resolver
                resolver = get_indicator_resolver()

                # Search for this indicator across ALL providers
                all_providers = ["WORLDBANK", "IMF", "FRED", "EUROSTAT", "OECD", "BIS", "STATSCAN"]
                indicator_fallbacks = []

                for provider in all_providers:
                    if provider == primary_upper:
                        continue  # Skip the provider that failed

                    # Check if this provider has the indicator
                    resolved = resolver.resolve(
                        indicator,
                        provider=provider,
                        country=country,
                        countries=context_countries or None,
                    )
                    if resolved and resolved.confidence >= 0.6:
                        indicator_fallbacks.append((provider, resolved.confidence))
                        logger.debug(f"IndicatorResolver found '{indicator}' in {provider} (conf: {resolved.confidence:.2f})")

                # Sort by confidence and take providers
                if indicator_fallbacks:
                    indicator_fallbacks.sort(key=lambda x: x[1], reverse=True)
                    resolver_providers = [p for p, _ in indicator_fallbacks]
                    # Merge: resolver-based first, then general fallbacks
                    combined = resolver_providers + [p for p in fallback_list if p not in resolver_providers]
                    logger.info(f"🔍 Smart fallback for '{indicator}': {combined[:5]}")
                    result = combined[:5]  # Limit to 5 fallbacks
                    if cache_key:
                        self._fallback_provider_cache[cache_key] = result
                        self._fallback_provider_cache.move_to_end(cache_key)
                        while len(self._fallback_provider_cache) > self.MAX_FALLBACK_CACHE_ENTRIES:
                            self._fallback_provider_cache.popitem(last=False)
                    return result

            except Exception as e:
                logger.debug(f"IndicatorResolver fallback search failed: {e}")

        # Fallback to catalog-based compatibility
        if indicator:
            try:
                from .indicator_compatibility import get_fallback_providers as get_compat_fallbacks
                compat_fallbacks = get_compat_fallbacks(indicator, primary_upper)
                if compat_fallbacks:
                    compat_providers = [p for p, _, _ in compat_fallbacks]
                    combined = compat_providers + [p for p in fallback_list if p not in compat_providers]
                    logger.debug(f"Using catalog fallbacks for '{indicator}': {combined}")
                    if cache_key:
                        self._fallback_provider_cache[cache_key] = combined
                        self._fallback_provider_cache.move_to_end(cache_key)
                        while len(self._fallback_provider_cache) > self.MAX_FALLBACK_CACHE_ENTRIES:
                            self._fallback_provider_cache.popitem(last=False)
                    return combined
            except Exception as e:
                logger.debug(f"Could not get catalog-based fallbacks: {e}")

        if cache_key:
            self._fallback_provider_cache[cache_key] = fallback_list
            self._fallback_provider_cache.move_to_end(cache_key)
            while len(self._fallback_provider_cache) > self.MAX_FALLBACK_CACHE_ENTRIES:
                self._fallback_provider_cache.popitem(last=False)
        return fallback_list

    def _get_fallback_provider(self, primary_provider: str) -> Optional[str]:
        """
        Get the first fallback provider for a given primary provider.
        (Kept for backwards compatibility)

        Args:
            primary_provider: The primary provider that failed

        Returns:
            Fallback provider name or None if no fallback available
        """
        fallbacks = self._get_fallback_providers(primary_provider)
        return fallbacks[0] if fallbacks else None

    def _get_no_data_suggestions(self, provider: str, intent: ParsedIntent) -> str:
        """
        Generate helpful suggestions when no data is found.

        Args:
            provider: The provider that returned no data
            intent: The parsed intent with query details

        Returns:
            String with helpful suggestions for the user
        """
        provider_upper = normalize_provider_name(provider)
        suggestions = []

        # Provider-specific suggestions
        provider_suggestions = {
            "IMF": [
                "**Try alternative providers**: World Bank or OECD may have similar data.",
                "**Check country coverage**: IMF may not have data for all countries.",
                "**Historical data**: IMF primarily provides recent economic indicators."
            ],
            "BIS": [
                "**Try alternative providers**: World Bank or FRED may have property/credit data.",
                "**Check coverage**: BIS focuses on property prices, credit, and banking data.",
                "**Supported countries**: BIS covers ~60 major economies."
            ],
            "OECD": [
                "**Try alternative providers**: World Bank has broader country coverage.",
                "**OECD members only**: OECD data primarily covers member countries.",
                "**Check indicator name**: OECD uses specific indicator codes."
            ],
            "EUROSTAT": [
                "**EU countries only**: Eurostat covers EU member states.",
                "**Try World Bank**: For broader European or global data.",
                "**Check indicator**: Eurostat uses specific dataset codes."
            ],
            "COMTRADE": [
                "**Check country codes**: UN Comtrade uses ISO3 country codes.",
                "**Trade data availability**: Recent years may not be available yet.",
                "**Partner regions**: Some regions like 'Asia' or 'Africa' need individual countries."
            ],
            "STATSCAN": [
                "**Canada only**: Statistics Canada covers Canadian data.",
                "**Try World Bank**: For Canadian data with global comparison.",
                "**Check indicator**: StatsCan uses specific table/vector IDs."
            ],
            "WORLDBANK": [
                "**Check indicator code**: World Bank uses specific indicator codes (e.g., NY.GDP.MKTP.CD).",
                "**Regional data**: Try using region names like 'South Asia' or 'Sub-Saharan Africa'.",
                "**Data lag**: Some indicators have 1-2 year reporting delays."
            ],
            "FRED": [
                "**US data focus**: FRED primarily covers US economic data.",
                "**Try World Bank**: For non-US countries.",
                "**Series ID**: Check if the FRED series ID is correct."
            ],
            "COINGECKO": [
                "**Check coin ID**: Use correct cryptocurrency IDs (e.g., 'bitcoin', 'ethereum').",
                "**Historical data**: Some coins may have limited history.",
                "**Try alternative coins**: Check CoinGecko for available cryptocurrencies."
            ],
            "EXCHANGERATE": [
                "**Currency codes**: Use ISO currency codes (e.g., USD, EUR, GBP).",
                "**Supported currencies**: Covers 161 major currencies.",
                "**Try FRED**: For major currency pairs with longer history."
            ]
        }

        base_suggestions = provider_suggestions.get(provider_upper, [
            "**Try a different provider**: The data may be available from another source.",
            "**Check spelling**: Ensure country and indicator names are correct.",
            "**Simplify query**: Try a more specific or simpler query."
        ])

        suggestions.append("**Suggestions:**")
        for i, s in enumerate(base_suggestions[:3], 1):
            suggestions.append(f"{i}. {s}")

        # Add fallback provider hint
        fallbacks = self._get_fallback_providers(provider_upper)
        if fallbacks:
            suggestions.append(f"\n**Alternative providers to try**: {', '.join(fallbacks)}")

        return "\n".join(suggestions)

    def _is_fallback_relevant(
        self,
        original_indicators: List[str],
        fallback_result: List[NormalizedData],
        target_countries: Optional[List[str]] = None,
        original_query: Optional[str] = None,
    ) -> bool:
        """
        Check if fallback result is semantically related to the original query.

        This prevents returning completely unrelated data when fallback providers
        find something with vaguely similar keywords but different meaning.

        The check separates SUBJECT entities (corporations, government, households)
        from METRIC types (assets, debt, income). If the original query specifies
        a subject, the result must match that subject - not just any overlapping term.

        INFRASTRUCTURE FIX: Now also validates COUNTRY matching to prevent returning
        data for a different country than requested.

        Args:
            original_indicators: Original indicator names from user query
            fallback_result: Data returned from fallback provider
            target_countries: Optional countries the query is targeting

        Returns:
            True if fallback data is relevant, False otherwise
        """
        if not fallback_result or not original_indicators:
            return False

        # Country validation (generalized): enforce match for known ISO2 country contexts.
        requested_iso2 = {
            iso2
            for iso2 in (
                self._normalize_country_to_iso2(country)
                for country in (target_countries or [])
            )
            if iso2
        }
        if requested_iso2:
            saw_normalized_country = False
            matched_requested_country = False
            for data in fallback_result:
                if not data.metadata or not data.metadata.country:
                    continue

                result_country = data.metadata.country
                result_iso2 = self._normalize_country_to_iso2(result_country)
                if not result_iso2:
                    continue

                saw_normalized_country = True
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

        # Define subject entities (who/what the data is about)
        subject_entities = {
            'corporation', 'corporations', 'corporate', 'company', 'companies',
            'nonfinancial', 'nonfin', 'nfc',  # non-financial corporations
            'government', 'public', 'fiscal', 'general',
            'household', 'households', 'consumer', 'consumers',
            'bank', 'banks', 'banking', 'financial', 'mfi',
            'business', 'businesses', 'enterprise', 'enterprises',
            'private', 'sector'
        }

        # Define metric types (what is being measured)
        metric_types = {
            'assets', 'liabilities', 'debt', 'income', 'expenditure',
            'revenue', 'expense', 'expenses', 'balance', 'equity',
            'gdp', 'gnp', 'unemployment', 'inflation', 'cpi', 'ppi',
            'trade', 'exports', 'imports', 'deficit', 'surplus',
            'investment', 'consumption', 'savings', 'production',
            'employment', 'wages', 'salaries', 'output', 'growth'
        }

        # Metric qualifiers that change meaning
        metric_qualifiers = {
            'fixed', 'current', 'liquid', 'tangible', 'intangible',
            'gross', 'net', 'total', 'real', 'nominal'
        }

        # Extract key terms from text
        def extract_key_terms(text: str) -> set:
            stop_words = {
                'data', 'statistics', 'annual', 'quarterly', 'monthly',
                'index', 'rate', 'by', 'and', 'the', 'of', 'for', 'in', 'to',
                'a', 'an', 'all', 'from', 'with', 'as', 'at', 'show', 'plot',
                'get', 'find', 'display', 'chart', 'graph', 'value', 'values',
                'economic', 'activity', 'activities'
            }
            terms = set()
            for word in text.lower().replace('-', ' ').replace('_', ' ').split():
                clean = ''.join(c for c in word if c.isalnum())
                if len(clean) > 2 and clean not in stop_words:
                    terms.add(clean)
            return terms

        # Get terms from original indicators + query text (when available)
        # so generic parsed indicators like "trade" still preserve directionality
        # from the original user phrasing ("imports", "exports", etc.).
        original_text = " ".join(
            part for part in [
                ' '.join(original_indicators).lower(),
                str(original_query or "").lower(),
            ] if part
        )
        original_terms = extract_key_terms(original_text)

        if not original_terms:
            return True  # Can't validate, accept fallback

        # Extract subjects and metrics from original
        original_subjects = original_terms & subject_entities
        original_metrics = original_terms & metric_types
        original_qualifiers = original_terms & metric_qualifiers

        # Check each result for relevance
        for data in fallback_result:
            if not data.metadata:
                continue

            result_text = (data.metadata.indicator or "").lower()
            result_terms = extract_key_terms(result_text)

            # Extract subjects and metrics from result
            result_subjects = result_terms & subject_entities
            result_metrics = result_terms & metric_types
            result_qualifiers = result_terms & metric_qualifiers

            # CRITICAL CHECK 1: Subject entity matching
            # If original specifies a subject (e.g., corporations), result MUST have same subject
            if original_subjects:
                # Map related terms to canonical subjects
                def get_canonical_subject(terms: set) -> set:
                    canonical = set()
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

                orig_canonical = get_canonical_subject(original_subjects)
                result_canonical = get_canonical_subject(result_subjects)

                # If original has specific subject but result doesn't match, reject
                if orig_canonical and not (orig_canonical & result_canonical):
                    # Special case: if result has NO subject at all, it might be aggregate data
                    if result_subjects:
                        logger.warning(
                            f"Fallback rejected: original subject {orig_canonical} != result subject {result_canonical}"
                        )
                        return False
                    else:
                        # Result has no specific subject - might be too generic
                        logger.warning(
                            f"Fallback rejected: original has subject {orig_canonical} but result has no specific subject"
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
                            f"Fallback rejected: metrics don't match - original={original_metrics}, result={result_metrics}"
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
                                f"Fallback rejected: metric qualifiers differ significantly - "
                                f"original={original_qualifiers}, result={result_qualifiers}"
                            )
                            return False

            # If we get here, check general term overlap
            overlap = original_terms & result_terms
            min_required = max(1, len(original_terms) * 0.3)  # At least 30% overlap
            if len(overlap) >= min_required:
                logger.info(f"Fallback accepted: sufficient overlap - {overlap}")
                return True

        # Default: reject if no result passed the checks
        logger.warning("Fallback rejected: no result passed relevance checks")
        return False

    async def _try_with_fallback(self, intent: ParsedIntent, primary_error: Exception):
        """
        Try to fetch data from fallback providers when primary fails.

        Attempts multiple fallback providers in order until one succeeds.

        Args:
            intent: The parsed intent
            primary_error: The error from the primary provider

        Returns:
            Data from fallback provider

        Raises:
            Original error if all fallbacks fail
        """
        primary_provider = normalize_provider_name(intent.apiProvider)
        # Get indicator for smarter fallbacks
        indicator = intent.indicators[0] if intent.indicators else None
        target_countries = self._collect_target_countries(intent.parameters)
        target_country = target_countries[0] if target_countries else None
        fallback_providers = self._get_fallback_providers(
            primary_provider,
            indicator,
            country=target_country,
            countries=target_countries,
        )

        if not fallback_providers:
            raise primary_error

        last_error = primary_error
        for fallback_provider in fallback_providers:
            logger.warning(f"Attempting fallback from {primary_provider} to {fallback_provider}")

            fallback_params = dict(intent.parameters or {})
            # Remove provider-specific resolved indicator identifiers so fallback
            # providers can resolve indicator codes in their own namespace.
            fallback_params.pop("indicator", None)
            fallback_params.pop("seriesId", None)
            fallback_params.pop("series_id", None)
            fallback_params.pop("code", None)

            fallback_indicators = list(intent.indicators or [])
            fallback_indicator_query = self._select_indicator_query_for_resolution(intent)
            if fallback_indicator_query:
                if not fallback_indicators:
                    fallback_indicators = [fallback_indicator_query]
                elif len(fallback_indicators) == 1:
                    existing_indicator = str(fallback_indicators[0] or "").strip().lower()
                    current_param_indicator = str((intent.parameters or {}).get("indicator") or "").strip().lower()
                    if existing_indicator and current_param_indicator and existing_indicator == current_param_indicator:
                        fallback_indicators = [fallback_indicator_query]

            # Create a modified intent for the fallback provider
            fallback_intent = ParsedIntent(
                apiProvider=fallback_provider,
                indicators=fallback_indicators,
                parameters=fallback_params,
                clarificationNeeded=False,
                originalQuery=intent.originalQuery,
            )

            try:
                result = await self._fetch_data(fallback_intent)

                # Validate fallback result is semantically related to original query
                if result and self._is_fallback_relevant(
                    intent.indicators,
                    result,
                    target_countries,
                    intent.originalQuery,
                ):
                    logger.info(f"✅ Fallback to {fallback_provider} succeeded")
                    return result
                else:
                    logger.warning(
                        f"⚠️ Fallback to {fallback_provider} returned unrelated data, skipping"
                    )
                    continue  # Try next fallback
            except Exception as fallback_error:
                logger.warning(f"Fallback to {fallback_provider} failed: {fallback_error}")
                last_error = fallback_error
                continue  # Try next fallback

        # All fallbacks failed
        logger.error(f"All fallbacks failed for {primary_provider}")
        raise primary_error  # Raise original error

    async def process_query(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        auto_pro_mode: bool = True,
        use_orchestrator: bool = False,
        allow_orchestrator: bool = True,
    ) -> QueryResponse:
        # Check if there's already an active tracker (e.g., from streaming endpoint)
        existing_tracker = get_processing_tracker()
        if existing_tracker:
            # Use existing tracker (for streaming)
            tracker = existing_tracker
            tracker_token = None  # Don't reset the existing tracker
        else:
            # Create new tracker for non-streaming requests
            tracker = ProcessingTracker()
            tracker_token = activate_processing_tracker(tracker)
        try:
            conv_id = conversation_manager.get_or_create(conversation_id)
            history = conversation_manager.get_history(conv_id) if conversation_id else []

            # Check if LangChain orchestrator should be used
            from ..config import get_settings
            settings = get_settings()
            if allow_orchestrator and (use_orchestrator or settings.use_langchain_orchestrator):
                logger.info("🤖 Using LangChain orchestrator for intelligent query routing")
                return await self._execute_with_orchestrator(query, conv_id, tracker)

            # Early complexity detection (before LLM parsing)
            early_complexity = QueryComplexityAnalyzer.detect_complexity(query, intent=None)

            # If query REQUIRES Pro Mode, automatically switch
            if auto_pro_mode and early_complexity['pro_mode_required']:
                logger.info("🚀 Auto-switching to Pro Mode (detected: %s)", early_complexity['complexity_factors'])
                return await self._execute_pro_mode(query, conv_id)

            logger.info("Parsing query with LLM: %s", query)

            with tracker.track("parsing_query", "🤖 Understanding your question...") as update_parse_metadata:
                parse_result = await self.pipeline.parse_and_route(query, history)
                intent = parse_result.intent
                logger.debug("Parsed intent: %s", intent.model_dump())
                update_parse_metadata({
                    "provider": intent.apiProvider,
                    "indicators": intent.indicators,
                })

            conv_id = conversation_manager.add_message_safe(conv_id, "user", query, intent=intent)

            if intent.clarificationNeeded:
                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    clarificationNeeded=True,
                    clarificationQuestions=intent.clarificationQuestions,
                    processingSteps=tracker.to_list(),
                )

            if intent.needsDecomposition and intent.decompositionType == "provinces":
                intent.decompositionEntities = normalize_canadian_region_list(
                    intent.decompositionEntities,
                    fill_missing_territories=True
                )

            # Note: Query decomposition now uses batch methods when available (see _decompose_and_aggregate)
            # This avoids timeouts by making single API calls instead of 10-13 parallel requests

            # Ensure defaults are applied for decomposition queries before processing
            if intent.needsDecomposition and intent.decompositionEntities:
                if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
                    logger.info("📅 Applying default time periods to decomposition query...")
                    ParameterValidator.apply_default_time_periods(intent)

            # Check if query needs decomposition (e.g., "all provinces", "each state")
            if intent.needsDecomposition and intent.decompositionEntities:
                logger.info("🔄 Query decomposition detected: %s %s into %d entities",
                           intent.decompositionType, query, len(intent.decompositionEntities))

                # ALWAYS use batch method for decomposition queries (never Pro Mode)
                # The batch method is faster and more reliable than Pro Mode
                logger.info("🚀 Using batch method (Pro Mode disabled for decomposition)")

                # Decompose and aggregate using batch method
                data = await self._decompose_and_aggregate(query, intent, conv_id, tracker)

                conv_id = conversation_manager.add_message_safe(
                    conv_id,
                    "assistant",
                    f"Retrieved data for {len(intent.decompositionEntities)} {intent.decompositionType} from {intent.apiProvider}"
                )

                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    data=data,
                    clarificationNeeded=False,
                    processingSteps=tracker.to_list(),
                )

            # Apply default time periods BEFORE validation to prevent clarification requests
            # This is critical for reducing the 45% clarification rate on time period queries
            logger.info("📅 Applying default time periods to prevent clarification requests...")
            ParameterValidator.apply_default_time_periods(intent)

            validation = self.pipeline.validate_intent(intent)
            is_multi_indicator = validation.is_multi_indicator
            is_valid = validation.is_valid
            validation_error = validation.validation_error
            suggestions = validation.suggestions

            if not is_valid:
                logger.warning("Parameter validation failed: %s", validation_error)
                # Generate clarification questions
                clarification_qs = ParameterValidator.suggest_clarification(intent, validation_error)

                # Format error message with suggestions
                message_parts = [f"❌ **Cannot Process Query**", validation_error]
                if suggestions:
                    if suggestions.get('suggestion'):
                        message_parts.append(f"\n**💡 Suggestion**: {suggestions['suggestion']}")
                    if suggestions.get('common_indicators'):
                        message_parts.append(f"\n**Common indicators**: {', '.join(suggestions['common_indicators'])}")
                    if suggestions.get('example'):
                        message_parts.append(f"\n**Example**: {suggestions['example']}")

                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    clarificationNeeded=True,
                    clarificationQuestions=clarification_qs,
                    message="\n".join(message_parts),
                    processingSteps=tracker.to_list(),
                )

            is_confident = validation.is_confident
            confidence_reason = validation.confidence_reason
            if not is_confident:
                logger.warning("Low confidence in intent: %s", confidence_reason)
                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    clarificationNeeded=True,
                    clarificationQuestions=[
                        f"I'm not certain about this query: {confidence_reason}",
                        "Could you rephrase with more specific details?",
                        "Or would you like to use Pro Mode for a custom analysis?"
                    ],
                    message=f"⚠️ **Uncertain Query**\n{confidence_reason}\n\nPlease provide more details or use Pro Mode for better results.",
                    processingSteps=tracker.to_list(),
                )

            # Log any warnings from validation
            if suggestions and suggestions.get('warning'):
                logger.info("Validation warning: %s", suggestions['warning'])

            # Fetch data based on whether it's multi-indicator or not
            if is_multi_indicator:
                logger.info("📊 Multi-indicator query detected: %s indicators", len(intent.indicators))
                data = await self._fetch_multi_indicator_data(intent)
            else:
                # Fetch data with retry logic
                data = await retry_async(
                    lambda: self._fetch_data(intent),
                    max_attempts=3,
                    initial_delay=1.0,
                )

            # Check for empty data (silent failure case) and provide meaningful error
            if not data or (isinstance(data, list) and len(data) == 0):
                logger.warning(f"No data returned from {intent.apiProvider} for query: {query}")

                # Try to provide helpful context about why data might be missing
                provider_name = intent.apiProvider
                indicators = ", ".join(intent.indicators) if intent.indicators else "requested indicator"
                country = intent.parameters.get("country") or intent.parameters.get("countries", [""])[0] if intent.parameters else ""

                error_details = []
                error_details.append(f"No data found for **{indicators}**")
                if country:
                    error_details.append(f"for **{country}**")
                error_details.append(f"from **{provider_name}**.")

                # Add provider-specific suggestions
                suggestions = self._get_no_data_suggestions(provider_name, intent)

                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    data=None,
                    clarificationNeeded=False,
                    error="no_data_found",
                    message=f"⚠️ **No Data Available**\n\n{' '.join(error_details)}\n\n{suggestions}",
                    processingSteps=tracker.to_list(),
                )

            data = self._rerank_data_by_query_relevance(query, data)
            clarification_response = self._build_uncertain_result_clarification(
                conversation_id=conv_id,
                query=query,
                intent=intent,
                data=data,
                processing_steps=tracker.to_list(),
            )
            if clarification_response:
                return clarification_response

            conv_id = conversation_manager.add_message_safe(
                conv_id,
                "assistant",
                f"Retrieved {len(data)} data series from {intent.apiProvider}",
            )

            return QueryResponse(
                conversationId=conv_id,
                intent=intent,
                data=data,
                clarificationNeeded=False,
                processingSteps=tracker.to_list(),
            )
        except DataNotAvailableError as exc:
            logger.warning("Data not available from primary provider: %s", exc)

            # Try fallback providers before giving up
            if 'intent' in locals() and intent:
                try:
                    logger.info("🔄 Attempting fallback providers...")
                    fallback_data = await self._try_with_fallback(intent, exc)
                    if fallback_data:
                        logger.info("✅ Fallback succeeded!")
                        return QueryResponse(
                            conversationId=conv_id,
                            intent=intent,
                            data=fallback_data,
                            clarificationNeeded=False,
                            processingSteps=tracker.to_list(),
                        )
                except Exception as fallback_exc:
                    logger.warning("All fallback providers failed: %s", fallback_exc)

            # Format error message with helpful context
            formatted_message = QueryComplexityAnalyzer.format_error_message(
                str(exc), query, intent if 'intent' in locals() else None
            )
            return QueryResponse(
                conversationId=conv_id,
                clarificationNeeded=False,
                error="data_not_available",
                message=formatted_message,
                processingSteps=tracker.to_list(),
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Query processing error after retries")

            # Try fallback providers before giving up
            if 'intent' in locals() and intent:
                try:
                    logger.info("🔄 Attempting fallback providers after error...")
                    fallback_data = await self._try_with_fallback(intent, exc)
                    if fallback_data:
                        logger.info("✅ Fallback succeeded after error!")
                        return QueryResponse(
                            conversationId=conv_id,
                            intent=intent,
                            data=fallback_data,
                            clarificationNeeded=False,
                            processingSteps=tracker.to_list(),
                        )
                except Exception as fallback_exc:
                    logger.warning("All fallback providers failed: %s", fallback_exc)

            # Format error message with helpful context
            formatted_message = QueryComplexityAnalyzer.format_error_message(
                str(exc), query, intent if 'intent' in locals() else None
            )
            return QueryResponse(
                conversationId=conv_id,
                clarificationNeeded=False,
                error="processing_error",
                message=formatted_message,
                processingSteps=tracker.to_list(),
            )
        finally:
            # Only reset tracker if we created it (not using existing one)
            if tracker_token is not None:
                reset_processing_tracker(tracker_token)

    async def _fetch_multi_indicator_data(self, intent: ParsedIntent) -> List[NormalizedData]:
        """Fetch data for multiple indicators by making separate API calls for each"""
        import asyncio

        all_data = []

        # Ensure default time periods are applied to base intent first
        if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
            logger.info("📅 Applying default time periods to multi-indicator query...")
            ParameterValidator.apply_default_time_periods(intent)

        # Create separate intents for each indicator
        fetch_tasks = []
        for indicator in intent.indicators:
            # Create parameters for this indicator
            params = dict(intent.parameters) if intent.parameters else {}

            # For FRED provider, set indicator (let _series_id() handle normalization)
            if normalize_provider_name(intent.apiProvider) == "FRED":
                params["indicator"] = indicator

            # For StatsCan, set indicator field
            if normalize_provider_name(intent.apiProvider) == "STATSCAN":
                params["indicator"] = indicator

            # Create a new intent with single indicator
            single_intent = ParsedIntent(
                apiProvider=intent.apiProvider,
                indicators=[indicator],
                parameters=params,
                clarificationNeeded=False,
                confidence=intent.confidence,
                recommendedChartType=intent.recommendedChartType,
                originalQuery=intent.originalQuery,
            )

            # Create fetch task with retry
            task = retry_async(
                lambda i=single_intent: self._fetch_data(i),
                max_attempts=3,
                initial_delay=1.0,
            )
            fetch_tasks.append(task)

        # Fetch all indicators in parallel
        logger.info("🔄 Fetching %s indicators in parallel...", len(fetch_tasks))
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        # Collect successful results
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("Failed to fetch indicator %s: %s", intent.indicators[i], result)
                continue

            # Result is a list of NormalizedData
            if isinstance(result, list):
                all_data.extend(result)
            else:
                all_data.append(result)

        if not all_data:
            raise DataNotAvailableError(
                f"Could not fetch any of the requested indicators: {', '.join(intent.indicators)}"
            )

        logger.info("✅ Successfully fetched %s datasets for %s indicators", len(all_data), len(intent.indicators))
        return all_data

    async def _fetch_data(self, intent: ParsedIntent) -> List[NormalizedData]:
        logger.info(f"🔍 _fetch_data called: provider={intent.apiProvider}, indicators={intent.indicators}")

        provider = normalize_provider_name(intent.apiProvider)
        params = intent.parameters or {}
        tracker = get_processing_tracker()

        # PHASE B: Use IndicatorResolver as the unified entry point for indicator resolution
        # This replaces scattered resolution logic across providers
        resolver = get_indicator_resolver()

        # Resolve/validate indicator for providers that require normalized indicator codes.
        # IMPORTANT: run this even when params already has "indicator" because LLM-provided
        # values can be noisy (raw query text, wrong provider code, or invalid pseudo-codes).
        if provider in {"STATSCAN", "STATISTICS CANADA", "FRED", "IMF", "WORLDBANK", "EUROSTAT", "OECD", "BIS"}:
            existing_indicator = str(params.get("indicator") or "").strip()
            has_explicit_code = bool(
                existing_indicator
                and self._looks_like_provider_indicator_code(provider, existing_indicator)
            )
            if has_explicit_code:
                # Respect explicit provider-native series IDs from upstream parse/routing.
                logger.info(
                    "🔒 Keeping explicit %s indicator code: %s",
                    provider,
                    existing_indicator,
                )
                params = {**params, "indicator": existing_indicator}
                intent.parameters = params
            else:
                indicator_query = self._select_indicator_query_for_resolution(intent)
                if not indicator_query and intent.indicators:
                    indicator_query = str(intent.indicators[0] or "").strip()
                if not indicator_query:
                    indicator_query = existing_indicator

                if indicator_query:
                    country_context = params.get("country")
                    countries_context = params.get("countries") if isinstance(params.get("countries"), list) else None
                    original_query_text = str(intent.originalQuery or "").strip()
                    selected_original_override = (
                        bool(original_query_text)
                        and indicator_query == original_query_text
                        and bool(intent.indicators)
                        and indicator_query != str(intent.indicators[0] or "").strip()
                    )

                    resolved = resolver.resolve(
                        indicator_query,
                        provider=provider,
                        country=country_context,
                        countries=countries_context,
                    )

                    accepted_resolved = False
                    if resolved:
                        threshold = self._indicator_resolution_threshold(
                            indicator_query=indicator_query,
                            resolved_source=resolved.source,
                        )
                        accepted_resolved = resolved.confidence >= threshold
                        if accepted_resolved and not self._is_resolved_indicator_plausible(
                            provider=provider,
                            indicator_query=indicator_query,
                            resolved_code=resolved.code,
                        ):
                            accepted_resolved = False
                        logger.info(
                            "🔍 IndicatorResolver candidate: '%s' → '%s' (conf=%.2f, src=%s, threshold=%.2f, accepted=%s)",
                            indicator_query,
                            resolved.code,
                            resolved.confidence,
                            resolved.source,
                            threshold,
                            accepted_resolved,
                        )

                    if accepted_resolved and resolved:
                        params = {**params, "indicator": resolved.code}
                        # World Bank fetch path can iterate raw intent.indicators when multiple
                        # are present. If we intentionally overrode to original query for better
                        # semantic alignment, collapse to the resolved indicator to avoid
                        # reintroducing LLM-parsed mismatched indicators.
                        if provider in {"WORLDBANK", "WORLD BANK"} and selected_original_override and len(intent.indicators) > 1:
                            logger.info(
                                "🔎 Collapsing World Bank multi-indicator intent to resolved indicator '%s' after semantic override",
                                resolved.code,
                            )
                            intent.indicators = [resolved.code]
                    else:
                        params = {**params, "indicator": indicator_query}

                    intent.parameters = params  # ensure downstream consumers see indicator

        # Check catalog availability: if provider is in not_available list for this indicator,
        # proactively re-route to a better provider before wasting time on failed API calls
        # EXCEPTION: If user EXPLICITLY requested a provider (e.g., "from Eurostat"), respect their request
        indicator_term = (params.get("indicator") or (intent.indicators[0] if intent.indicators else ""))
        logger.info(f"📋 Catalog check: indicator='{indicator_term}', provider='{provider}'")

        # CRITICAL: Check if user explicitly requested this provider
        # If so, skip catalog override - user's explicit request has highest priority
        original_query = intent.originalQuery or ""
        explicit_provider_requested = normalize_provider_name(self._detect_explicit_provider(original_query) or "")
        if explicit_provider_requested and explicit_provider_requested == provider:
            logger.info(f"📋 Skipping catalog override - user explicitly requested {provider}")
        elif indicator_term and provider:
            try:
                from .catalog_service import find_concept_by_term, get_best_provider, is_provider_available
                concept = find_concept_by_term(indicator_term)
                logger.info(f"📋 Catalog concept: '{concept}' for term '{indicator_term}'")
                if concept and not is_provider_available(concept, provider):
                    # Provider is in not_available list - find alternative
                    countries_ctx = params.get("countries") if isinstance(params.get("countries"), list) else None
                    if not countries_ctx:
                        country = params.get("country") or params.get("region")
                        countries_ctx = [country] if country else None

                    alt_provider, alt_code, _ = get_best_provider(concept, countries_ctx)
                    if alt_provider and alt_provider.upper() != provider:
                        logger.info(
                            "📋 Catalog: %s not available for '%s', routing to %s",
                            provider,
                            indicator_term,
                            alt_provider,
                        )
                        intent.apiProvider = alt_provider
                        provider = normalize_provider_name(alt_provider)

                        if alt_code:
                            params = {**params, "indicator": alt_code}
                            intent.parameters = params
                            if not intent.indicators or len(intent.indicators) == 1:
                                intent.indicators = [alt_code]
                            logger.info(
                                "📋 Catalog remapped indicator for %s: %s -> %s",
                                provider,
                                indicator_term,
                                alt_code,
                            )
            except Exception as e:
                logger.warning(f"Catalog availability check failed: {e}")

        # Apply smart default time ranges based on provider
        # This ensures Comtrade gets 10 years, ExchangeRate/CoinGecko gets 3 months
        logger.info(f"🕐 Before defaults - provider={provider}, startDate={params.get('startDate')}, endDate={params.get('endDate')}")
        params = apply_default_time_range(provider, params)
        logger.info(f"🕐 After defaults - startDate={params.get('startDate')}, start_year={params.get('start_year')}")
        intent.parameters = params  # Update intent with defaults

        # CRITICAL FIX: For ExchangeRate queries, extract currency pairs BEFORE cache lookup
        # This ensures each unique currency pair has its own cache entry
        # Without this, "JPY to USD" and "GBP to USD" could share the same cache entry!
        if provider == "EXCHANGERATE":
            params = self._extract_exchange_rate_params(params, intent)
            intent.parameters = params
            logger.info(f"💱 ExchangeRate: Cache params after currency extraction: baseCurrency={params.get('baseCurrency')}, targetCurrency={params.get('targetCurrency')}")

        cached = await self._get_from_cache(provider, params)
        if cached:
            logger.info("Cache hit for %s", provider)
            result_list = cached if isinstance(cached, list) else [cached]
            if tracker:
                with tracker.track(
                    "cache_hit",
                    "⚡ Served instantly from cache",
                    {
                        "provider": provider,
                        "indicator_count": len(intent.indicators),
                    },
                ) as update_cache_metadata:
                    update_cache_metadata({
                        "series_count": len(result_list),
                        "cached": True,
                    })
                    return result_list
            return result_list

        logger.info("Cache miss for %s, fetching from API", provider)

        async def fetch_from_provider() -> List[NormalizedData]:
            # Use nonlocal to avoid UnboundLocalError when reassigning params
            nonlocal params

            if provider == "FRED":
                # Ensure params has indicator set (in case it wasn't set above)
                if not params.get("indicator") and intent.indicators:
                    params = {**params, "indicator": intent.indicators[0]}

                # Handle multiple indicators for FRED
                if len(intent.indicators) > 1:
                    # Fetch each series separately and combine results
                    all_series = []
                    for indicator in intent.indicators:
                        indicator_params = {**params, "indicator": indicator}
                        series = await self.fred_provider.fetch_series(indicator_params)
                        all_series.append(series)
                    return all_series
                else:
                    # Single indicator - fetch with params containing indicator
                    series = await self.fred_provider.fetch_series(params)
                    return [series]
            if provider in {"WORLDBANK", "WORLD BANK"}:
                resolved_indicator = params.get("indicator")
                # Handle multiple indicators for World Bank
                if len(intent.indicators) > 1:
                    all_data = []
                    indicators_to_fetch = intent.indicators
                    if resolved_indicator and len(intent.indicators) > 1:
                        # Prefer resolved indicator when available; it has passed resolver scoring.
                        indicators_to_fetch = [str(resolved_indicator)]

                    for indicator in indicators_to_fetch:
                        data = await self.world_bank_provider.fetch_indicator(
                            indicator=indicator,
                            country=params.get("country"),
                            countries=params.get("countries"),
                            start_date=params.get("startDate"),
                            end_date=params.get("endDate"),
                        )
                        all_data.extend(data if isinstance(data, list) else [data])
                    return all_data
                else:
                    indicator = str(resolved_indicator or (intent.indicators[0] if intent.indicators else ""))
                    return await self.world_bank_provider.fetch_indicator(
                        indicator=indicator,
                        country=params.get("country"),
                        countries=params.get("countries"),
                        start_date=params.get("startDate"),
                        end_date=params.get("endDate"),
                    )
            if provider == "COMTRADE":
                indicators = [indicator.lower() for indicator in intent.indicators]
                if any("balance" in indicator for indicator in indicators):
                    series = await self.comtrade_provider.fetch_trade_balance(
                        reporter=params.get("reporter") or params.get("country") or "US",
                        partner=params.get("partner"),
                        start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                        end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                        frequency=params.get("frequency", "annual"),
                    )
                    return [series]
                reporter_value = params.get("reporter") or params.get("country")
                reporters_value = params.get("reporters") or params.get("countries")
                # If an explicit reporter is present (common for bilateral queries),
                # ignore broad countries[] context to avoid duplicate/misaligned fan-out.
                if reporter_value:
                    reporters_value = None
                return await self.comtrade_provider.fetch_trade_data(
                    reporter=reporter_value,
                    reporters=reporters_value,
                    partner=params.get("partner"),
                    commodity=params.get("commodity"),
                    flow=params.get("flow"),
                    start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                    end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                    frequency=params.get("frequency", "annual"),
                )
            if provider in {"STATSCAN", "STATISTICS CANADA"}:
                # Check if this is a categorical query (with dimensions)
                # or an entity-based decomposition query
                dimensions = params.get("dimensions", {})
                entity = params.get("entity")
                indicator = params.get("indicator", intent.indicators[0] if intent.indicators else None)

                # Check for industry/breakdown parameter (e.g., "GDP goods-producing industries")
                industry = params.get("industry") or params.get("breakdown")
                if industry:
                    industry_lower = industry.lower()
                    # Check if this is actually a demographic breakdown (not industry)
                    # Demographic breakdowns should use coordinate-based queries
                    if any(demo in industry_lower for demo in ["age", "gender", "sex", "demographic"]):
                        logger.info(f"👥 Demographic breakdown detected: {industry}")
                        # Convert to coordinate-based indicator (e.g., EMPLOYMENT + age → EMPLOYMENT_BY_AGE)
                        combined_indicator = f"{indicator or 'EMPLOYMENT'}_BY_AGE"
                        demo_params = {
                            "indicator": combined_indicator,
                            "startDate": params.get("startDate"),
                            "endDate": params.get("endDate"),
                            "periods": params.get("periods", 240),
                        }
                        series = await self.statscan_provider.fetch_series(demo_params)
                        return [series]
                    else:
                        logger.info(f"🏭 Industry breakdown detected: {industry}")
                        breakdown_params = {
                            "indicator": indicator or "GDP",
                            "breakdown": industry,
                            "startDate": params.get("startDate"),
                            "endDate": params.get("endDate"),
                            "periods": params.get("periods", 240),
                        }
                        series = await self.statscan_provider.fetch_with_breakdown(breakdown_params)
                        return [series]

                # If entity is present (from decomposition), convert to dimension
                if entity and not dimensions:
                    dimensions = {"geography": entity}

                # Use categorical provider if dimensions are specified
                if dimensions:
                    # Build categorical data request
                    categorical_params = {
                        "productId": params.get("productId", "17100005"),
                        "indicator": indicator or "Population",
                        "periods": params.get("periods", 20),
                        "dimensions": dimensions
                    }
                    series = await self.statscan_provider.fetch_categorical_data(categorical_params)
                    return [series]
                else:
                    # Check if this is a hardcoded indicator or needs dynamic discovery
                    # Hardcoded indicators: GDP, UNEMPLOYMENT, CPI, HOUSING_STARTS, etc.
                    if indicator and indicator.upper() in self.statscan_provider.VECTOR_MAPPINGS:
                        # Use vector-based fetch for hardcoded indicators
                        series = await self.statscan_provider.fetch_series(params)
                        return [series]
                    elif indicator:
                        # Use dynamic discovery for non-hardcoded indicators
                        # (e.g., EMPLOYMENT, RETAIL_SALES, LABOUR_FORCE)
                        logger.info(f"🔍 Using dynamic discovery for StatsCan indicator: {indicator}")
                        dynamic_params = {
                            "indicator": indicator,
                            "geography": params.get("geography"),
                            "periods": params.get("periods", 240)
                        }
                        try:
                            result = await self.statscan_provider.fetch_dynamic_data(dynamic_params)
                            return [result]
                        except DataNotAvailableError:
                            # If dynamic discovery fails, fall back to vector-based fetch
                            # (which may raise a more specific error)
                            logger.warning(f"Dynamic discovery failed for {indicator}, trying vector fetch")
                            series = await self.statscan_provider.fetch_series(params)
                            return [series]
                    else:
                        # No indicator specified - error
                        raise DataNotAvailableError("No indicator specified for Statistics Canada query")

            if provider == "IMF":
                # Check if multiple countries are requested (batch query)
                countries_param = params.get("countries") or params.get("country")

                # Resolve countries/regions to list of country codes
                resolved_countries = []
                if isinstance(countries_param, list):
                    # Already a list - resolve each item (may be countries or regions)
                    for item in countries_param:
                        resolved_countries.extend(self.imf_provider._resolve_countries(item))
                elif isinstance(countries_param, str):
                    # Single string - could be country or region
                    resolved_countries = self.imf_provider._resolve_countries(countries_param)
                else:
                    # No country specified - default to USA
                    resolved_countries = ["USA"]

                # Remove duplicates while preserving order
                resolved_countries = list(dict.fromkeys(resolved_countries))

                logger.info(
                    "🌍 IMF query resolved to %d countries: %s (from params: %s)",
                    len(resolved_countries),
                    resolved_countries[:10] if len(resolved_countries) > 10 else resolved_countries,
                    countries_param,
                )

                if len(resolved_countries) > 1:
                    # Multiple countries - use batch method
                    logger.info("✅ Using IMF batch method for %d countries", len(resolved_countries))
                    all_data = []
                    for indicator in intent.indicators:
                        series_list = await self.imf_provider.fetch_batch_indicator(
                            indicator=indicator,
                            countries=resolved_countries,
                            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                        )
                        all_data.extend(series_list)
                    return all_data
                else:
                    # Single country - handle multiple indicators
                    country = resolved_countries[0]
                    if len(intent.indicators) > 1:
                        all_data = []
                        for indicator in intent.indicators:
                            series = await self.imf_provider.fetch_indicator(
                                indicator=indicator,
                                country=country,
                                start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                                end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                            )
                            all_data.append(series)
                        return all_data
                    else:
                        indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else ""))
                        series = await self.imf_provider.fetch_indicator(
                            indicator=indicator,
                            country=country,
                            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                        )
                        return [series]
            if provider in {"EXCHANGERATE", "EXCHANGE_RATE", "FX"}:
                logger.info(f"🔍 ExchangeRate Query Parameters:")
                logger.info(f"   - Full params: {params}")
                logger.info(f"   - baseCurrency: {params.get('baseCurrency', 'USD')}")
                logger.info(f"   - targetCurrency: {params.get('targetCurrency')}")
                logger.info(f"   - targetCurrencies: {params.get('targetCurrencies')}")
                logger.info(f"   - startDate: {params.get('startDate')}")
                logger.info(f"   - endDate: {params.get('endDate')}")

                # ExchangeRate-API free tier limitation:
                # Historical data is NOT available without a paid API key.
                # Only current rates are supported via the free tier.
                #
                # Check if the user is requesting historical data:
                # - If startDate is more than 7 days in the past, it's historical
                # - If endDate is before today, it's historical
                from datetime import datetime, timedelta

                has_historical_request = False
                start_date = params.get("startDate")
                end_date = params.get("endDate")

                # First check for time references in query text
                query_lower = (intent.originalQuery or "").lower()
                import re
                historical_patterns = [
                    r'\bfor\s+20\d{2}\b',           # "for 2023"
                    r'\b20\d{2}\s*-\s*20\d{2}\b',    # "2022-2023"
                    r'\blast\s+\d+\s+(month|year|day|week)s?\b',  # "last 6 months"
                    r'\bhistory\b',                 # "history"
                    r'\bhistorical\b',              # "historical"
                    r'\bfrom\s+20\d{2}\b',          # "from 2020"
                    r'\bsince\s+20\d{2}\b',         # "since 2020"
                ]
                for pattern in historical_patterns:
                    if re.search(pattern, query_lower):
                        has_historical_request = True
                        logger.info(f"   📅 Historical request detected from query text: '{pattern}'")
                        break

                if not has_historical_request and (start_date or end_date):
                    try:
                        today = datetime.now().date()
                        week_ago = today - timedelta(days=7)

                        # Check if start date is more than a week old (historical query)
                        if start_date:
                            start_dt = datetime.fromisoformat(start_date[:10]).date()
                            if start_dt < week_ago:
                                has_historical_request = True
                                logger.info(f"   📅 Historical request detected: startDate {start_date} is > 7 days ago")

                        # Check if end date is before today (historical query)
                        if end_date and not has_historical_request:
                            end_dt = datetime.fromisoformat(end_date[:10]).date()
                            yesterday = today - timedelta(days=1)
                            if end_dt < yesterday:
                                has_historical_request = True
                                logger.info(f"   📅 Historical request detected: endDate {end_date} is before yesterday")
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"   ⚠️ Could not parse dates: {e}")
                        # If we can't parse dates, assume it's not historical
                        pass

                if has_historical_request:
                    logger.warning("⚠️ ExchangeRate: Historical data requested - falling back to FRED")

                    # FRED has excellent historical exchange rate data
                    # Use FRED exchange rate series instead
                    base_currency = params.get("baseCurrency", "USD")
                    target_currency = params.get("targetCurrency")

                    # If target currency not in params, try to extract from query
                    if not target_currency:
                        query_upper = (intent.originalQuery or "").upper()
                        # Try to match patterns like "USD to EUR", "EUR/USD", etc.
                        import re
                        # Pattern: "X to Y" exchange rate
                        to_pattern = re.search(r'\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b', query_upper)
                        # Pattern: "X/Y" or "X vs Y"
                        slash_pattern = re.search(r'\b([A-Z]{3})[/\s](?:VS\s)?([A-Z]{3})\b', query_upper)

                        if to_pattern:
                            base_currency = to_pattern.group(1)
                            target_currency = to_pattern.group(2)
                            logger.info(f"   📝 Extracted from query: {base_currency} to {target_currency}")
                        elif slash_pattern:
                            base_currency = slash_pattern.group(1)
                            target_currency = slash_pattern.group(2)
                            logger.info(f"   📝 Extracted from query: {base_currency}/{target_currency}")

                    if target_currency:
                        # FRED exchange rate series mapping (USD-based)
                        fred_exchange_series = {
                            "EUR": "DEXUSEU",  # US Dollar to Euro
                            "GBP": "DEXUSUK",  # US Dollar to UK Pound
                            "JPY": "DEXJPUS",  # Japanese Yen to US Dollar
                            "CAD": "DEXCAUS",  # Canadian Dollar to US Dollar
                            "CHF": "DEXSZUS",  # Swiss Franc to US Dollar
                            "AUD": "DEXUSAL",  # US Dollar to Australian Dollar
                            "CNY": "DEXCHUS",  # Chinese Yuan to US Dollar
                            "MXN": "DEXMXUS",  # Mexican Peso to US Dollar
                            "INR": "DEXINUS",  # Indian Rupee to US Dollar
                            "BRL": "DEXBZUS",  # Brazilian Real to US Dollar
                            "KRW": "DEXKOUS",  # South Korean Won to US Dollar
                            "SEK": "DEXSDUS",  # Swedish Krona to US Dollar
                            "NOK": "DEXNOUS",  # Norwegian Krone to US Dollar
                            "DKK": "DEXDNUS",  # Danish Krone to US Dollar
                            "SGD": "DEXSIUS",  # Singapore Dollar to US Dollar
                            "HKD": "DEXHKUS",  # Hong Kong Dollar to US Dollar
                            "NZD": "DEXUSNZ",  # US Dollar to New Zealand Dollar
                            "ZAR": "DEXSFUS",  # South African Rand to US Dollar
                            "THB": "DEXTHUS",  # Thai Baht to US Dollar
                            "MYR": "DEXMAUS",  # Malaysian Ringgit to US Dollar
                            "TWD": "DEXTAUS",  # Taiwan Dollar to US Dollar
                        }

                        # Normalize currencies
                        target_upper = target_currency.upper()
                        base_upper = base_currency.upper()

                        # FRED series are USD-based, so we need to handle both directions:
                        # 1. If target is foreign (USD to EUR), look up target in series
                        # 2. If target is USD (EUR to USD), look up base in series
                        fred_series_id = None
                        if target_upper in fred_exchange_series and target_upper != "USD":
                            fred_series_id = fred_exchange_series[target_upper]
                            logger.info(f"   📈 Using FRED series {fred_series_id} for USD to {target_upper}")
                        elif base_upper in fred_exchange_series and target_upper == "USD":
                            # Reverse lookup: X to USD uses the same series as USD to X
                            fred_series_id = fred_exchange_series[base_upper]
                            logger.info(f"   📈 Using FRED series {fred_series_id} for {base_upper} to USD (will invert if needed)")
                        elif base_upper != "USD" and target_upper != "USD":
                            # Cross rate: e.g., CHF to EUR - try to find any series we have
                            if base_upper in fred_exchange_series:
                                fred_series_id = fred_exchange_series[base_upper]
                                logger.info(f"   📈 Using FRED series {fred_series_id} for {base_upper}/USD as proxy")
                            elif target_upper in fred_exchange_series:
                                fred_series_id = fred_exchange_series[target_upper]
                                logger.info(f"   📈 Using FRED series {fred_series_id} for USD/{target_upper} as proxy")

                        if fred_series_id:
                            try:
                                # FRED provider expects params dict with seriesId
                                fred_params = {
                                    "seriesId": fred_series_id,
                                    "startDate": params.get("startDate"),
                                    "endDate": params.get("endDate"),
                                }
                                series = await self.fred_provider.fetch_series(fred_params)
                                # Update metadata to indicate this is historical exchange rate data
                                series.metadata.indicator = f"{base_upper} to {target_upper} Exchange Rate"
                                series.metadata.source = "FRED (Federal Reserve)"
                                return [series]
                            except Exception as fred_error:
                                logger.warning(f"   ⚠️ FRED fallback failed: {fred_error}")
                                # Continue to original error if FRED fails

                    # If we couldn't use FRED, show the original error
                    raise DataNotAvailableError(
                        "Historical exchange rate data is not available with the free ExchangeRate API tier. "
                        "\n\n💡 **Alternatives:**\n"
                        "1. For **current rates**: Rephrase your query without time references (e.g., 'Current USD to EUR rate')\n"
                        "2. For **historical rates**: Use a paid ExchangeRate API key (https://www.exchangerate-api.com/)\n"
                        "3. For **Real Effective Exchange Rate** (REER) over time: Ask for 'REER' which uses IMF data\n\n"
                        "Note: Some bilateral exchange rates are available via FRED for major currency pairs."
                    )

                series = await self.exchangerate_provider.fetch_exchange_rate(
                    base_currency=params.get("baseCurrency", "USD"),
                    target_currency=params.get("targetCurrency"),
                    target_currencies=params.get("targetCurrencies"),
                )
                return [series]
            if provider == "BIS":
                indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else "POLICY_RATE"))
                # Add indicator to params for cache key differentiation
                params["indicator"] = indicator
                return await self.bis_provider.fetch_indicator(
                    indicator=indicator,
                    country=params.get("country"),
                    countries=params.get("countries"),
                    start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                    end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                    frequency=params.get("frequency", "M"),
                )
            if provider == "EUROSTAT":
                indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else "GDP"))
                # Add indicator to params for cache key differentiation
                params["indicator"] = indicator

                # Check for multi-country query (similar to OECD handling)
                country_param = params.get("country")
                countries_param = params.get("countries", [])

                # EU aggregate codes that should NOT expand
                EU_AGGREGATES = {"EU", "EU27", "EU27_2020", "EU28", "EA", "EA19", "EA20", "EUROZONE", "EURO_AREA"}

                # Check if this is a multi-country query
                is_multi_country = isinstance(countries_param, list) and len(countries_param) > 1

                # Also check if country_param is a region name (not an aggregate)
                if not is_multi_country and isinstance(country_param, str):
                    upper_country = country_param.upper().replace(" ", "_")
                    if upper_country not in EU_AGGREGATES:
                        # Use CountryResolver for region expansion (centralized source of truth)
                        from ..routing.country_resolver import CountryResolver

                        # Try to expand as a region (G7, BRICS, Nordic, ASEAN, etc.)
                        expanded = CountryResolver.expand_region(country_param)
                        if expanded:
                            countries_param = expanded
                            is_multi_country = True
                            logger.info(f"🌍 Expanded Eurostat region '{country_param}' to {len(expanded)} countries via CountryResolver")
                        else:
                            # Also check for sub-regional groupings not in CountryResolver
                            SUB_REGION_MAPPINGS = {
                                "BENELUX": ["BE", "NL", "LU"],
                                "BALTIC": ["EE", "LV", "LT"],
                                "DACH": ["DE", "AT", "CH"],
                                "IBERIAN": ["ES", "PT"],
                                "VISEGRAD": ["PL", "CZ", "SK", "HU"],
                                "V4": ["PL", "CZ", "SK", "HU"],
                            }
                            if upper_country in SUB_REGION_MAPPINGS:
                                countries_param = SUB_REGION_MAPPINGS[upper_country]
                                is_multi_country = True
                                logger.info(f"🌍 Expanded Eurostat sub-region '{country_param}' to: {countries_param}")

                if is_multi_country:
                    logger.info(f"🌍 Multi-country Eurostat query detected: {countries_param}")
                    series_list = []
                    for country in countries_param:
                        try:
                            series = await self.eurostat_provider.fetch_indicator(
                                indicator=indicator,
                                country=country,
                                start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                                end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                            )
                            series_list.append(series)
                        except Exception as e:
                            logger.warning(f"Failed to fetch {indicator} for {country}: {e}")
                            continue

                    if not series_list:
                        raise DataNotAvailableError(f"No Eurostat data available for {indicator} in any requested countries")

                    return series_list

                # Single country query (default to EU aggregate if not specified)
                single_country = country_param if country_param else "EU27_2020"
                series = await self.eurostat_provider.fetch_indicator(
                    indicator=indicator,
                    country=single_country,
                    start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                    end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                )
                return [series]
            if provider == "OECD":
                indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else "GDP"))
                # Add indicator to params for cache key differentiation
                params["indicator"] = indicator

                # NOTE: Pre-emptive circuit breaker check removed - it was too aggressive
                # The circuit breaker will still protect us by tracking 429 errors
                # and applying exponential backoff in the retry logic

                # Check if this is a multi-country query
                # IMPORTANT: For OECD queries, default to "OECD" aggregate, not "USA"
                # This handles queries like "OECD unemployment rate" correctly
                country_param = params.get("country")
                countries_param = params.get("countries", [])

                # Handle LLM parsing "OECD unemployment" as countries=["ALL_OECD"]
                # Convert this to country="OECD" for aggregate query
                if countries_param and len(countries_param) == 1:
                    c = countries_param[0].upper().replace(" ", "_")
                    if c in ("OECD", "ALL_OECD", "ALL_OECD_COUNTRIES", "OECD_COUNTRIES"):
                        logger.info(f"🌍 Converting countries=['{countries_param[0]}'] to OECD aggregate query")
                        country_param = "OECD"
                        countries_param = []  # Clear to prevent multi-country detection

                # If no country specified, use OECD aggregate
                if not country_param and not countries_param:
                    logger.info("🌍 No country specified for OECD query, using OECD aggregate")
                    country_param = "OECD"

                # Detect multi-country requests including region names (Nordic, G7, EU, etc.)
                # Use expand_countries() to check if a country param expands to multiple countries
                # BUT: "OECD" should NOT expand to all 38 countries - it's an aggregate
                expanded_countries = []
                if isinstance(country_param, str):
                    # Special handling: "OECD" is an aggregate, not a region to expand
                    if country_param.upper() in ("OECD", "OECD_AVERAGE"):
                        expanded_countries = ["OECD"]  # Keep as single aggregate
                    else:
                        expanded_countries = self.oecd_provider.expand_countries(country_param)

                is_multi_country = (
                    isinstance(countries_param, list) and len(countries_param) > 1
                ) or (
                    len(expanded_countries) > 1  # Region expands to multiple countries
                )

                if is_multi_country:
                    logger.info("🌍 Multi-country OECD query detected")
                    try:
                        countries = countries_param if countries_param else expanded_countries
                        series_list = await self.oecd_provider.fetch_multi_country(
                            indicator=indicator,
                            countries=countries,
                            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                        )
                        return series_list
                    except Exception as exc:
                        error_msg = str(exc).lower()
                        temporarily_unavailable = any(
                            token in error_msg
                            for token in ("rate limit", "429", "circuit", "timeout", "timed out", "temporarily unavailable")
                        )
                        if temporarily_unavailable:
                            logger.warning("OECD multi-country temporarily unavailable: %s", exc)
                            # Let centralized fallback policy choose alternative providers.
                            raise DataNotAvailableError(
                                f"OECD temporarily unavailable for multi-country request: {exc}"
                            ) from exc
                        raise

                try:
                    # Single country query (including OECD aggregate)
                    series = await self.oecd_provider.fetch_indicator(
                        indicator=indicator,
                        country=country_param,
                        start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                        end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                    )
                    return [series]
                except Exception as exc:
                    error_msg = str(exc).lower()
                    temporarily_unavailable = any(
                        token in error_msg
                        for token in ("rate limit", "429", "circuit", "timeout", "timed out", "temporarily unavailable")
                    )
                    if temporarily_unavailable:
                        logger.warning("OECD temporarily unavailable for %s: %s", country_param, exc)
                        # Let centralized fallback policy choose alternative providers.
                        raise DataNotAvailableError(
                            f"OECD temporarily unavailable for {country_param or 'OECD'}: {exc}"
                        ) from exc
                    raise
            if provider in {"COINGECKO", "COIN GECKO"}:
                logger.info(f"🔍 CoinGecko Query Parameters:")
                logger.info(f"   - Full params: {params}")
                logger.info(f"   - Indicators: {intent.indicators}")

                # Apply CoinGecko defaults for time period if not set
                # Check if query mentions time periods like "last X days", "past week", etc.
                query_lower = intent.originalQuery.lower() if intent.originalQuery else ""
                time_patterns = [
                    "last", "past", "previous", "recent", "historical",
                    "days", "weeks", "months", "year", "history"
                ]
                mentions_time = any(pattern in query_lower for pattern in time_patterns)

                # Extract days from query patterns like "last 30 days", "past 7 days"
                import re
                days_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+days?', query_lower)
                weeks_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+weeks?', query_lower)
                months_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+months?', query_lower)
                year_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+years?', query_lower)

                # Set days based on query pattern - prefer extracted days over LLM-generated dates
                # (LLM sometimes generates incorrect dates, especially for relative time periods)
                if not params.get("days"):
                    extracted_days = None
                    if days_match:
                        extracted_days = int(days_match.group(1))
                        logger.info(f"   📅 Extracted days from query: {extracted_days}")
                    elif weeks_match:
                        extracted_days = int(weeks_match.group(1)) * 7
                        logger.info(f"   📅 Extracted weeks from query, converted to days: {extracted_days}")
                    elif months_match:
                        extracted_days = int(months_match.group(1)) * 30
                        logger.info(f"   📅 Extracted months from query, converted to days: {extracted_days}")
                    elif year_match:
                        extracted_days = int(year_match.group(1)) * 365
                        logger.info(f"   📅 Extracted years from query, converted to days: {extracted_days}")
                    elif mentions_time:
                        # Default to 30 days for any time-related query
                        extracted_days = 30
                        logger.info(f"   📅 Time period mentioned, defaulting to 30 days")

                    if extracted_days:
                        params["days"] = extracted_days
                        # Clear startDate/endDate to use days instead (more reliable)
                        if params.get("startDate") or params.get("endDate"):
                            logger.info(f"   ⚠️ Clearing LLM-generated dates in favor of extracted days")
                            params.pop("startDate", None)
                            params.pop("endDate", None)

                # Determine query type based on indicators or params
                coin_ids = params.get("coinIds", [])
                vs_currency = params.get("vsCurrency", "usd")

                logger.info(f"   - Initial coin_ids: {coin_ids}")
                logger.info(f"   - vs_currency: {vs_currency}")
                logger.info(f"   - startDate: {params.get('startDate')}")
                logger.info(f"   - endDate: {params.get('endDate')}")
                logger.info(f"   - days: {params.get('days')}")

                # Map common cryptocurrency names to CoinGecko IDs
                # This mapping is applied ALWAYS (to both params-provided and indicator-derived coin_ids)
                coin_map = {
                    "bitcoin": "bitcoin", "btc": "bitcoin",
                    "ethereum": "ethereum", "eth": "ethereum",
                    "solana": "solana", "sol": "solana",
                    "cardano": "cardano", "ada": "cardano",
                    "polkadot": "polkadot", "dot": "polkadot",
                    "avalanche": "avalanche-2", "avax": "avalanche-2",  # CoinGecko uses avalanche-2
                    "polygon": "matic-network", "matic": "matic-network",
                    "chainlink": "chainlink", "link": "chainlink",
                    "uniswap": "uniswap", "uni": "uniswap",
                    "dogecoin": "dogecoin", "doge": "dogecoin",
                    "shiba": "shiba-inu", "shib": "shiba-inu",
                    "ripple": "ripple", "xrp": "ripple",
                    "binance": "binancecoin", "bnb": "binancecoin",
                    "litecoin": "litecoin", "ltc": "litecoin",
                    "tron": "tron", "trx": "tron",
                    "stellar": "stellar", "xlm": "stellar",
                    "cosmos": "cosmos", "atom": "cosmos",
                    "near": "near", "nearprotocol": "near",
                    "algorand": "algorand", "algo": "algorand",
                }

                # CRITICAL: Apply coin name mapping to params-provided coin_ids
                # This fixes issues where LLM provides "avalanche" but CoinGecko needs "avalanche-2"
                if coin_ids:
                    mapped_ids = []
                    for cid in coin_ids:
                        cid_lower = cid.lower()
                        mapped = coin_map.get(cid_lower, cid)  # Use mapped value or original
                        if mapped != cid:
                            logger.info(f"   🔄 Mapped coin ID: '{cid}' → '{mapped}'")
                        mapped_ids.append(mapped)
                    coin_ids = mapped_ids

                # Auto-detect coin IDs from indicators if not explicitly set
                if intent.indicators and not coin_ids:
                    for indicator in intent.indicators:
                        indicator_lower = indicator.lower().replace(" ", "")
                        for name, coin_id in coin_map.items():
                            if name in indicator_lower:
                                coin_ids.append(coin_id)
                                break

                    # If no coins matched, try to extract coin name from indicator text
                    # or default to bitcoin if no valid coin found
                    if not coin_ids:
                        # Try to detect coin name in indicator
                        found_coin = False
                        for indicator in intent.indicators:
                            ind_lower = indicator.lower()
                            for name, coin_id in coin_map.items():
                                if name in ind_lower:
                                    coin_ids.append(coin_id)
                                    found_coin = True
                                    break
                            if found_coin:
                                break
                        # Default to bitcoin if no specific coin found
                        if not coin_ids:
                            logger.info(f"   🪙 No specific coin found in indicators, defaulting to 'bitcoin'")
                            coin_ids = ["bitcoin"]

                logger.info(f"   - Resolved coin_ids: {coin_ids}")

                # Define indicator_lower for metric detection
                indicator_lower = " ".join(intent.indicators).lower() if intent.indicators else ""

                # Check if historical data is requested
                if params.get("startDate") or params.get("endDate") or params.get("days"):
                    logger.info(f"📈 Historical data request detected")

                    # Determine the metric for historical data
                    hist_metric = "price"  # Default
                    if any(term in indicator_lower for term in ["market cap", "market capitalization", "marketcap"]):
                        hist_metric = "market_cap"
                        logger.info(f"   📈 Historical market cap request detected")
                    elif any(term in indicator_lower for term in ["volume", "trading volume", "24h volume"]):
                        hist_metric = "volume"
                        logger.info(f"   📊 Historical volume request detected")

                    # Historical data range query
                    if params.get("startDate") and params.get("endDate"):
                        logger.info(f"   Using date range: {params['startDate']} to {params['endDate']}, metric: {hist_metric}")
                        series_list = []
                        for coin_id in coin_ids:
                            logger.info(f"   Fetching {hist_metric} data for {coin_id}...")
                            data = await self.coingecko_provider.get_historical_data_range(
                                coin_id=coin_id,
                                vs_currency=vs_currency,
                                from_date=params["startDate"],
                                to_date=params["endDate"],
                                metric=hist_metric,
                            )
                            logger.info(f"   ✅ Got {len(data)} series for {coin_id}")
                            series_list.extend(data)
                        logger.info(f"🎉 CoinGecko: Returning {len(series_list)} series")
                        return series_list
                    else:
                        # Historical data with days parameter
                        days = params.get("days", 30)
                        logger.info(f"   Using days parameter: {days}, metric: {hist_metric}")
                        series_list = []
                        for coin_id in coin_ids:
                            logger.info(f"   Fetching {hist_metric} data for {coin_id}...")
                            data = await self.coingecko_provider.get_historical_data(
                                coin_id=coin_id,
                                vs_currency=vs_currency,
                                metric=hist_metric,
                                days=days,
                            )
                            logger.info(f"   ✅ Got {len(data)} series for {coin_id}")
                            series_list.extend(data)
                        logger.info(f"🎉 CoinGecko: Returning {len(series_list)} series")
                        return series_list
                else:
                    # Current data (simple price endpoint)
                    # Determine which metric to extract based on indicators
                    metric = "price"  # Default
                    # indicator_lower is already defined above

                    # Check for ranking/top coins request
                    ranking_keywords = ["top", "top 10", "top 5", "top 20", "ranking", "rankings", "largest", "biggest"]
                    is_ranking_request = any(term in indicator_lower for term in ranking_keywords) or \
                                       any(term in query_lower for term in ranking_keywords)

                    if is_ranking_request and ("market cap" in indicator_lower or "market cap" in query_lower):
                        # Top N cryptocurrencies by market cap
                        logger.info(f"🏆 Top cryptocurrencies by market cap request")

                        # Extract N from query (e.g., "top 10")
                        top_n_match = re.search(r'top\s+(\d+)', query_lower)
                        per_page = int(top_n_match.group(1)) if top_n_match else 10

                        result = await self.coingecko_provider.get_market_data(
                            vs_currency=vs_currency,
                            order="market_cap_desc",
                            per_page=per_page,
                        )
                        logger.info(f"🎉 CoinGecko: Returning top {len(result)} cryptocurrencies")
                        return result

                    if any(term in indicator_lower for term in ["volume", "trading volume", "24h volume", "24-hour volume"]):
                        metric = "volume"
                        logger.info(f"📊 Volume request detected")
                    elif any(term in indicator_lower for term in ["market cap", "market capitalization", "marketcap"]):
                        metric = "market_cap"
                        logger.info(f"📈 Market cap request detected")
                    elif any(term in indicator_lower for term in ["24h change", "24 hour change", "price change", "change"]):
                        metric = "24h_change"
                        logger.info(f"📉 24h change request detected")

                    logger.info(f"💰 Current {metric} request for {len(coin_ids)} coins")
                    result = await self.coingecko_provider.get_simple_price(
                        coin_ids=coin_ids,
                        vs_currency=vs_currency,
                        metric=metric,
                    )
                    logger.info(f"🎉 CoinGecko: Returning {len(result)} {metric} points")
                    return result
            raise DataNotAvailableError(
                f"Provider {intent.apiProvider} is not yet implemented. Available providers: FRED, World Bank, Comtrade, StatsCan, IMF, ExchangeRate, BIS, Eurostat, OECD, CoinGecko"
            )

        if tracker:
            # Make message more specific based on provider
            provider_names = {
                "FRED": "Federal Reserve",
                "WORLDBANK": "World Bank",
                "COMTRADE": "UN Comtrade",
                "STATSCAN": "Statistics Canada",
                "BIS": "Bank for International Settlements",
                "EUROSTAT": "Eurostat",
                "OECD": "OECD",
                "COINGECKO": "CoinGecko",
            }
            provider_display = provider_names.get(provider, provider)
            fetch_message = f"📊 Retrieving data from {provider_display}..."

            with tracker.track(
                "fetching_data",
                fetch_message,
                {
                    "provider": provider,
                    "indicator_count": len(intent.indicators),
                },
            ) as update_fetch_metadata:
                result = await fetch_from_provider()
                update_fetch_metadata({
                    "series_count": len(result),
                    "cached": False,
                })
        else:
            result = await fetch_from_provider()

        if not result or (len(result) == 1 and not result[0].data):
            raise DataNotAvailableError(
                f"No data available from {provider} for the requested parameters. "
                f"The data may not exist or may not be available for the specified time period or location."
            )

        # Validate data before returning (fundamental data quality check)
        from backend.services.data_validator import get_data_validator
        validator = get_data_validator()
        for data_series in result:
            validation_result = validator.validate(data_series)
            validator.log_validation_results(data_series, validation_result)
            # Log warnings but don't reject data (users expect to see what API returns)
            if not validation_result.valid or validation_result.confidence < 0.5:
                logger.warning(
                    f"⚠️ Data quality concern for {data_series.metadata.indicator if data_series.metadata else 'UNKNOWN'}: "
                    f"confidence={validation_result.confidence:.2f}, issues={len(validation_result.issues)}"
                )

        await self._save_to_cache(provider, params, result if len(result) > 1 else result[0])
        return result

    async def _execute_with_orchestrator(
        self,
        query: str,
        conversation_id: str,
        tracker: Optional['ProcessingTracker'] = None
    ) -> QueryResponse:
        """
        Execute query using LangChain orchestrator for intelligent routing.

        Supports three modes:
        - LangGraph (USE_LANGGRAPH=true, default): State-persistent agent graph
        - ReAct Agent (USE_LANGCHAIN_REACT_AGENT=true): Multi-step reasoning with error recovery
        - Simple Orchestrator: Basic LLM-based routing

        Args:
            query: User's natural language query
            conversation_id: Conversation ID for context
            tracker: Optional processing tracker

        Returns:
            QueryResponse with orchestrator results
        """
        try:
            # Check mode: Deep Agents (for complex queries) > LangGraph > ReAct > Simple Orchestrator
            use_langgraph = os.getenv('USE_LANGGRAPH', 'true').lower() == 'true'
            use_react_agent = os.getenv('USE_LANGCHAIN_REACT_AGENT', 'false').lower() == 'true'
            use_deep_agents = os.getenv('USE_DEEP_AGENTS', 'true').lower() == 'true'

            # Get conversation history for context
            conversation_history = conversation_manager.get_messages(conversation_id)

            # Add current query to history
            updated_conversation_id = conversation_manager.add_message_safe(
                conversation_id,
                "user",
                query,
            )
            if updated_conversation_id != conversation_id:
                conversation_id = updated_conversation_id
                conversation_history = conversation_manager.get_messages(conversation_id)

            # Deep Agents mode - for complex multi-step queries with planning
            if use_deep_agents and self._should_use_deep_agents(query):
                logger.info("🚀 Using Deep Agents for complex query with planning and parallel execution")
                return await self._execute_with_deep_agents(
                    query, conversation_id, conversation_history, tracker
                )

            # LangGraph mode - state-persistent agent orchestration
            if use_langgraph and not use_react_agent:
                return await self._execute_with_langgraph(
                    query, conversation_id, conversation_history, tracker
                )

            if use_react_agent:
                # Use enhanced ReAct agent with multi-step reasoning
                from ..services.langchain_react_agent import create_react_agent

                logger.info("🤖 Using LangChain ReAct agent for intelligent query routing")

                if tracker:
                    with tracker.track(
                        "react_agent_execution",
                        "🧠 ReAct agent analyzing query...",
                        {
                            "conversation_id": conversation_id,
                            "history_length": len(conversation_history),
                        },
                    ):
                        agent = create_react_agent(
                            query_service=self,
                            conversation_id=conversation_id
                        )
                        result = await agent.execute(query, chat_history=conversation_history)
                else:
                    agent = create_react_agent(
                        query_service=self,
                        conversation_id=conversation_id
                    )
                    result = await agent.execute(query, chat_history=conversation_history)

                # Include reasoning log in response
                reasoning_log = result.get("reasoning_log", [])
                if reasoning_log:
                    logger.info(f"ReAct agent reasoning: {len(reasoning_log)} steps")
            else:
                # Use simple orchestrator (original implementation)
                from ..services.langchain_orchestrator import create_langchain_orchestrator

                if tracker:
                    with tracker.track(
                        "orchestrator_execution",
                        "🤖 Using intelligent query routing...",
                        {
                            "conversation_id": conversation_id,
                            "history_length": len(conversation_history),
                        },
                    ):
                        orchestrator = create_langchain_orchestrator(
                            query_service=self,
                            conversation_id=conversation_id
                        )
                        result = await orchestrator.execute(query, chat_history=conversation_history)
                else:
                    orchestrator = create_langchain_orchestrator(
                        query_service=self,
                        conversation_id=conversation_id
                    )
                    result = await orchestrator.execute(query, chat_history=conversation_history)

            # Convert orchestrator result to QueryResponse
            if result.get("success"):
                output = result.get("output", "")
                data = result.get("data")  # Get actual data from orchestrator
                query_type = result.get("query_type", "standard")

                # Add to conversation history
                conversation_id = conversation_manager.add_message_safe(
                    conversation_id,
                    "assistant",
                    f"LangChain Orchestrator: {output[:200]}..."
                )

                # Create response message (keep it clean without internal routing details)
                response_message = output

                # Check for empty data in orchestrator path
                if not data or (isinstance(data, list) and len(data) == 0):
                    # Get provider info from result
                    provider_name = result.get("provider") or result.get("api_provider") or "Unknown"
                    indicators_list = result.get("indicators", [])
                    indicators = ", ".join(indicators_list) if indicators_list else "requested indicator"
                    country = result.get("country") or ""

                    logger.warning(f"Orchestrator: No data returned from {provider_name}")

                    error_details = []
                    error_details.append(f"No data found for **{indicators}**")
                    if country:
                        error_details.append(f"for **{country}**")
                    error_details.append(f"from **{provider_name}**.")

                    suggestions = self._get_no_data_suggestions(provider_name, None)

                    return QueryResponse(
                        conversationId=conversation_id,
                        data=None,
                        clarificationNeeded=False,
                        error="no_data_found",
                        message=f"⚠️ **No Data Available**\n\n{' '.join(error_details)}\n\n{suggestions}",
                        processingSteps=tracker.to_list() if tracker else None,
                    )

                # Build response with data if available
                response = QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=False,
                    message=response_message,
                    processingSteps=tracker.to_list() if tracker else None,
                )

                # Add data if present
                if data:
                    response.data = data

                # Handle comparison/follow-up specific fields
                if result.get("merge_with_previous"):
                    # Frontend should merge this with previous chart
                    # CRITICAL FIX: Use safe helper to handle None elements in data list
                    valid_data = _filter_valid_data(data)
                    response.intent = ParsedIntent(
                        apiProvider=_safe_get_source(valid_data),
                        indicators=[d.metadata.indicator for d in valid_data if d.metadata] if valid_data else [],
                        parameters={"merge_with_previous": True},
                        clarificationNeeded=False,
                        recommendedChartType=result.get("chart_type", "line"),
                    )

                if result.get("legend_labels"):
                    # Include legend labels for multi-series
                    if not response.intent:
                        # CRITICAL FIX: Use safe helper to handle None elements
                        response.intent = ParsedIntent(
                            apiProvider=_safe_get_source(data),
                            indicators=[],
                            parameters={},
                            clarificationNeeded=False,
                        )
                    response.intent.parameters["legend_labels"] = result.get("legend_labels")

                if data and not response.intent:
                    valid_data = _filter_valid_data(data)
                    if valid_data:
                        response.intent = ParsedIntent(
                            apiProvider=_safe_get_source(valid_data),
                            indicators=[d.metadata.indicator for d in valid_data if d.metadata],
                            parameters={},
                            clarificationNeeded=False,
                            originalQuery=query,
                        )

                if data:
                    clarification_response = self._build_uncertain_result_clarification(
                        conversation_id=conversation_id,
                        query=query,
                        intent=response.intent,
                        data=data,
                        processing_steps=tracker.to_list() if tracker else None,
                    )
                    if clarification_response:
                        return clarification_response

                return response
            else:
                error_msg = result.get("error", "Unknown error")
                logger.error(f"Orchestrator execution failed: {error_msg}")

                return QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=False,
                    error="orchestrator_error",
                    message=f"❌ **Intelligent routing encountered an error**\n\n{error_msg}",
                    processingSteps=tracker.to_list() if tracker else None,
                )

        except Exception as exc:
            logger.exception("LangChain orchestrator error")
            # Fall back to standard processing
            logger.warning("Falling back to standard query processing")
            return await self._standard_query_processing(
                query,
                conversation_id,
                tracker,
                record_user_message=False,
            )

    def _should_use_deep_agents(self, query: str) -> bool:
        """
        Determine if a query should use Deep Agents for parallel processing.

        Uses QueryComplexityAnalyzer for comprehensive pattern detection.

        Deep Agents are used for:
        1. Multi-country comparison queries (3+ countries)
        2. Multi-indicator analysis queries
        3. Ranking/sorting queries across multiple entities
        4. Complex regional breakdowns
        5. Queries with "compare", "vs", "and" with multiple data points

        Returns:
            True if Deep Agents should be used
        """
        query_lower = query.lower()

        # Framework guardrail: keep single-metric retrieval queries on the
        # deterministic path. Deep planning is most useful for true multi-step
        # analysis, and can over-decompose straightforward ratio/flow requests.
        ratio_patterns = [
            "% of gdp", "as % of gdp", "as percent of gdp", "as percentage of gdp",
            "share of gdp", "to gdp ratio", "ratio to gdp", "as share of gdp",
        ]
        analysis_keywords = [
            "correlation", "regression", "causal", "simulate", "scenario",
            "what if", "decompose", "optimize", "compute", "calculate", "derive",
        ]
        has_ratio_query = any(pattern in query_lower for pattern in ratio_patterns)
        has_analysis_keyword = any(term in query_lower for term in analysis_keywords)
        query_cues = self._extract_indicator_cues(query_lower)

        if has_ratio_query and not has_analysis_keyword:
            logger.info("⏭️ Deep Agents skipped for single-metric ratio retrieval query")
            return False

        if ("trade" in query_lower or "import" in query_lower or "export" in query_lower) and not has_analysis_keyword:
            if not any(term in query_lower for term in ["correlation", "versus and", "decompose", "optimize"]):
                logger.info("⏭️ Deep Agents skipped for direct trade retrieval query")
                return False

        if any(term in query_lower for term in ["rank", "ranking", "top ", "highest", "lowest"]):
            # Ranking by a single indicator is a data retrieval + sort problem, not
            # necessarily a multi-agent planning problem.
            if len(query_cues) <= 2 and not has_analysis_keyword:
                logger.info("⏭️ Deep Agents skipped for single-indicator ranking query")
                return False

        # Use QueryComplexityAnalyzer for comprehensive detection
        complexity = QueryComplexityAnalyzer.detect_complexity(query)

        # Deep Agents for truly complex queries
        is_complex = False
        trigger_reason = []

        # Multi-country queries with 3+ countries
        if complexity.get('is_multi_country'):
            trigger_reason.append("multi-country")
            is_complex = True

        # Ranking/sorting queries across entities
        if complexity.get('is_ranking'):
            trigger_reason.append("ranking")
            is_complex = True

        # Multi-indicator analysis
        if complexity.get('is_multi_indicator'):
            trigger_reason.append("multi-indicator")
            is_complex = True

        # Complex calculations (correlation, aggregations)
        if complexity.get('is_calculation'):
            # Only for true calculations, not indicator names
            query_lower = query.lower()
            if any(w in query_lower for w in ['correlation', 'aggregate', 'combine', 'versus']):
                trigger_reason.append("calculation")
                is_complex = True

        # Fallback: simple keyword-based detection
        if not is_complex:
            query_lower = query.lower()
            comparison_keywords = ["compare", "vs", "versus", "both", "all countries"]
            has_comparison = any(kw in query_lower for kw in comparison_keywords)

            country_keywords = [
                "us", "usa", "uk", "germany", "france", "japan", "china",
                "canada", "india", "brazil", "eu", "europe", "italy", "spain",
                "australia", "mexico", "korea", "russia"
            ]
            country_count = sum(1 for c in country_keywords if c in query_lower.split())

            indicator_keywords = [
                "gdp", "unemployment", "inflation", "trade", "exports", "imports",
                "interest rate", "population", "debt"
            ]
            indicator_count = sum(1 for i in indicator_keywords if i in query_lower)

            is_complex = (
                (has_comparison and (country_count > 1 or indicator_count > 1)) or
                (country_count >= 3) or
                (indicator_count >= 2 and country_count >= 2)
            )
            if is_complex:
                trigger_reason.append(f"keywords({country_count}c/{indicator_count}i)")

        if is_complex:
            logger.info(f"🧠 Deep Agents triggered: {', '.join(trigger_reason)}")

        return is_complex

    async def _execute_with_deep_agents(
        self,
        query: str,
        conversation_id: str,
        conversation_history: list,
        tracker: Optional['ProcessingTracker'] = None
    ) -> QueryResponse:
        """
        Execute query using Deep Agents for parallel processing and planning.

        Uses LangChain Deep Agents (v0.3.1+) for:
        - Automatic task planning for complex queries
        - Parallel data fetching across multiple providers
        - Context management for long conversations

        Args:
            query: User's natural language query
            conversation_id: Conversation ID for context
            conversation_history: List of previous messages
            tracker: Optional processing tracker

        Returns:
            QueryResponse with results from parallel execution
        """
        from ..services.deep_agent_orchestrator import (
            DeepAgentOrchestrator,
            DeepAgentConfig,
        )

        try:
            if tracker:
                with tracker.track(
                    "deep_agent_execution",
                    "🧠 Deep Agent planning and executing parallel tasks...",
                    {"conversation_id": conversation_id},
                ):
                    config = DeepAgentConfig(
                        enable_planning=True,
                        enable_subagents=True,
                        max_concurrent_subagents=5,
                        planning_threshold=2,
                    )
                    deep_agent = DeepAgentOrchestrator(
                        query_service=self,
                        config=config,
                    )
                    result = await deep_agent.execute(
                        query=query,
                        conversation_id=conversation_id,
                    )
            else:
                config = DeepAgentConfig(
                    enable_planning=True,
                    enable_subagents=True,
                    max_concurrent_subagents=5,
                    planning_threshold=2,
                )
                deep_agent = DeepAgentOrchestrator(
                    query_service=self,
                    config=config,
                )
                result = await deep_agent.execute(
                    query=query,
                    conversation_id=conversation_id,
                )

            if result.get("success"):
                # Build response from Deep Agent result
                data = result.get("data", [])
                if result.get("results"):
                    # Parallel execution results
                    # CRITICAL FIX: Safely handle None items and None data
                    for item in result["results"]:
                        if item and item.get("result", {}).get("data"):
                            item_data = item["result"]["data"]
                            if isinstance(item_data, list):
                                # Filter None values from list
                                valid_items = [d for d in item_data if d is not None]
                                data.extend(valid_items)
                            elif item_data is not None:
                                data.append(item_data)

                # Filter any remaining None values
                data = _filter_valid_data(data)
                data = self._rerank_data_by_query_relevance(query, data)

                todos = result.get("todos", [])
                message = None
                if todos:
                    completed = sum(1 for t in todos if t.get("status") == "completed")
                    message = f"Completed {completed}/{len(todos)} planned tasks"

                # Add to conversation history
                conversation_id = conversation_manager.add_message_safe(
                    conversation_id,
                    "assistant",
                    message or f"Retrieved {len(data)} datasets"
                )

                # Build intent from data if not provided in result
                intent = result.get("intent")
                if not intent and data:
                    # Extract provider, indicators, and countries from data metadata
                    providers = set()
                    indicators = []
                    countries = []
                    for d in data:
                        if hasattr(d, 'metadata') and d.metadata:
                            if d.metadata.source:
                                providers.add(d.metadata.source)
                            if d.metadata.indicator:
                                indicators.append(d.metadata.indicator)
                            if d.metadata.country:
                                countries.append(d.metadata.country)

                    # Build ParsedIntent
                    intent = ParsedIntent(
                        apiProvider=list(providers)[0] if providers else "UNKNOWN",
                        indicators=indicators or ["data"],
                        parameters={"countries": countries} if countries else {},
                        clarificationNeeded=False,
                        recommendedChartType="line",
                    )

                clarification_response = self._build_uncertain_result_clarification(
                    conversation_id=conversation_id,
                    query=query,
                    intent=intent,
                    data=data,
                )
                if clarification_response:
                    return clarification_response

                return QueryResponse(
                    conversationId=conversation_id,
                    data=data if data else None,
                    intent=intent,
                    message=message,
                    clarificationNeeded=False,
                )
            else:
                error_msg = result.get("error", "Deep Agent execution failed")
                logger.error(f"Deep Agent error: {error_msg}")
                # Fall back to standard processing
                return await self._execute_with_langgraph(
                    query, conversation_id, conversation_history, tracker
                )

        except Exception as e:
            logger.exception("Deep Agent execution error, falling back to LangGraph")
            return await self._execute_with_langgraph(
                query, conversation_id, conversation_history, tracker
            )

    async def _execute_with_langgraph(
        self,
        query: str,
        conversation_id: str,
        conversation_history: list,
        tracker: Optional['ProcessingTracker'] = None
    ) -> QueryResponse:
        """
        Execute query using LangGraph agent graph with persistent state.

        This method:
        1. Retrieves existing conversation state (entity context, data references)
        2. Routes query through the agent graph (router → specialist agent)
        3. Persists updated state for follow-up queries
        4. Handles Pro Mode with full context from previous queries

        Args:
            query: User's natural language query
            conversation_id: Conversation ID for context
            conversation_history: List of previous messages
            tracker: Optional processing tracker

        Returns:
            QueryResponse with results
        """
        from backend.agents import get_agent_graph, set_query_service_provider
        from backend.memory.state_manager import get_state_manager
        from backend.memory.conversation_state import EntityContext
        from langchain_core.messages import HumanMessage, AIMessage

        logger.info("🔄 Using LangGraph agent orchestration")

        try:
            # Inject query-service provider to avoid backend.main import coupling in graph nodes.
            set_query_service_provider(lambda: self)

            # Get or create the agent graph
            graph = get_agent_graph()
            state_manager = get_state_manager()

            # Get existing conversation state
            existing_state = state_manager.get(conversation_id)

            # Build initial state
            entity_context = None
            data_refs = {}

            if existing_state:
                entity_context = existing_state.entity_context
                data_refs = existing_state.data_references

            # Convert conversation history to LangChain messages
            messages = []
            for msg in conversation_history[-10:]:  # Last 10 messages for context
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    role = msg.get("role", "user")
                else:
                    content = str(msg)
                    role = "user"

                if role == "user":
                    messages.append(HumanMessage(content=content))
                else:
                    messages.append(AIMessage(content=content))

            # Add current query
            messages.append(HumanMessage(content=query))

            # Build state
            # EntityContext requires entity_type and value - use None if not provided
            initial_state = {
                "messages": messages,
                "conversation_id": conversation_id,
                "entity_context": entity_context,  # Can be None, agent handles this
                "data_references": data_refs,
                "query_type": None,
                "resolved_context": {},
                "requires_pro_mode": False,
                "parsed_intent": None,
                "result": None,
                "code_execution": None,
                "is_pro_mode": False,
                "error": None,
                "processing_steps": [],
                "current_provider": None,
                "current_indicators": [],
            }

            # Configure with thread_id for persistence
            config = {"configurable": {"thread_id": conversation_id}}

            # Execute the graph
            if tracker:
                with tracker.track(
                    "langgraph_execution",
                    "🤖 Processing with intelligent agent routing...",
                    {"conversation_id": conversation_id},
                ):
                    result = await graph.ainvoke(initial_state, config)
            else:
                result = await graph.ainvoke(initial_state, config)

            # Update conversation state with results
            if result.get("entity_context") or result.get("data_references"):
                state_manager.update(
                    conversation_id,
                    entity_context=result.get("entity_context"),
                    data_references=result.get("data_references"),
                )

            # Handle errors - try fallback before giving up
            # Error can be at top-level or nested in result dict
            top_error = result.get("error")
            inner_result = result.get("result", {})
            nested_error = inner_result.get("error") if isinstance(inner_result, dict) else None
            error_msg = top_error or nested_error

            logger.info(f"🔍 LangGraph result: top_error={top_error}, nested_error={nested_error}, parsed_intent={result.get('parsed_intent')}")

            if error_msg:
                error_msg = str(error_msg)
                parsed_intent = result.get("parsed_intent")

                # Extract provider from parsed intent for fallback
                if parsed_intent:
                    try:
                        fallback_intent = self._coerce_parsed_intent(parsed_intent, query)
                        if not fallback_intent:
                            raise ValueError("Could not parse LangGraph fallback intent")

                        provider_name = fallback_intent.apiProvider or "Unknown"
                        logger.info(f"🔄 LangGraph error: Attempting fallback from {provider_name}...")
                        fallback_data = await self._try_with_fallback(
                            fallback_intent,
                            DataNotAvailableError(error_msg)
                        )
                        if fallback_data:
                            logger.info(f"✅ LangGraph error: Fallback succeeded!")
                            return QueryResponse(
                                conversationId=conversation_id,
                                intent=fallback_intent,
                                data=fallback_data,
                                clarificationNeeded=False,
                                processingSteps=tracker.to_list() if tracker else None,
                            )
                    except Exception as fallback_err:
                        logger.warning(f"LangGraph error: All fallbacks failed: {fallback_err}")

                # Check if this is a commodity/precious metals query - provide specific guidance
                query_lower = query.lower()
                if any(metal in query_lower for metal in ["gold", "silver", "platinum", "palladium"]):
                    commodity_error = (
                        "Gold and precious metal spot prices are not available through our current data providers. "
                        "For commodity price indices, try: 'Producer Price Index' or 'PPI commodities'. "
                        "For real-time spot prices, use dedicated services like kitco.com or goldprice.org."
                    )
                    return QueryResponse(
                        conversationId=conversation_id,
                        clarificationNeeded=False,
                        error=commodity_error,
                        message=f"❌ {commodity_error}",
                        processingSteps=tracker.to_list() if tracker else None,
                    )

                return QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=False,
                    error=error_msg,  # Use actual error instead of generic "langgraph_error"
                    message=f"❌ {error_msg}",
                    processingSteps=tracker.to_list() if tracker else None,
                )

            # Handle Pro Mode result
            if result.get("is_pro_mode") and result.get("code_execution"):
                code_exec = result["code_execution"]
                # Convert file dicts to GeneratedFile objects
                raw_files = code_exec.get("files", [])
                files = None
                if raw_files:
                    files = [
                        GeneratedFile(
                            url=f.get("url", "") if isinstance(f, dict) else f,
                            name=f.get("name", "") if isinstance(f, dict) else f.split("/")[-1],
                            type=f.get("type", "file") if isinstance(f, dict) else "file",
                        )
                        for f in raw_files
                    ]
                return QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=False,
                    codeExecution=CodeExecutionResult(
                        code=code_exec.get("code", ""),
                        output=code_exec.get("output", ""),
                        error=code_exec.get("error"),
                        files=files,
                    ),
                    isProMode=True,
                    processingSteps=tracker.to_list() if tracker else None,
                )

            # Handle standard data result
            query_result = result.get("result", {})
            data = query_result.get("data", [])
            if isinstance(data, list) and data:
                data = self._rerank_data_by_query_relevance(query, data)

            # Guardrail: if LangGraph returns data whose semantic cues do not
            # match high-signal cues from the original query (e.g., import vs debt),
            # retry through the standard deterministic path.
            if data:
                query_cues = self._extract_indicator_cues(query)
                result_cues: set[str] = set()
                for series in data:
                    indicator_name = (
                        series.metadata.indicator
                        if series and getattr(series, "metadata", None)
                        else ""
                    )
                    result_cues |= self._extract_indicator_cues(indicator_name)

                if query_cues and not (query_cues & result_cues):
                    logger.warning(
                        "LangGraph semantic cue mismatch (query=%s, result=%s). "
                        "Retrying via standard pipeline.",
                        sorted(query_cues),
                        sorted(result_cues),
                    )
                    return await self._standard_query_processing(
                        query,
                        conversation_id,
                        tracker,
                        record_user_message=False,
                    )

            # Check for empty data (silent failure case) - LangGraph specific
            if not data or (isinstance(data, list) and len(data) == 0):
                # Try to get provider from multiple sources in LangGraph result
                provider_name = "Unknown"
                indicators = "requested indicator"
                country = ""
                error_detail = None

                # Source 1: Check current_provider from state (set by data_node)
                if result.get("current_provider") and result["current_provider"] != "unknown":
                    provider_name = result["current_provider"]

                # Source 2: Check inner result dict (from data_agent)
                inner_result = result.get("result", {})
                if isinstance(inner_result, dict):
                    if inner_result.get("provider") and inner_result["provider"] != "unknown":
                        provider_name = inner_result["provider"]
                    if inner_result.get("error"):
                        error_detail = inner_result["error"]

                # Source 3: Check parsed_intent
                parsed_intent = result.get("parsed_intent")
                if parsed_intent:
                    if isinstance(parsed_intent, dict):
                        if provider_name == "Unknown":
                            provider_name = parsed_intent.get("apiProvider", "Unknown")
                        indicators_list = parsed_intent.get("indicators", [])
                        indicators = ", ".join(indicators_list) if indicators_list else "requested indicator"
                        params = parsed_intent.get("parameters", {})
                        country = params.get("country") or params.get("countries", [""])[0] if params else ""
                    elif hasattr(parsed_intent, "apiProvider"):
                        if provider_name == "Unknown":
                            provider_name = parsed_intent.apiProvider or "Unknown"
                        indicators = ", ".join(parsed_intent.indicators) if parsed_intent.indicators else "requested indicator"
                        params = parsed_intent.parameters or {}
                        country = params.get("country") or params.get("countries", [""])[0] if params else ""

                # Source 4: Check current_indicators from state
                if indicators == "requested indicator" and result.get("current_indicators"):
                    indicators = ", ".join(result["current_indicators"])

                logger.warning(f"LangGraph: No data returned from {provider_name} for query")

                # Try fallback providers before giving up (same as standard path)
                if parsed_intent and provider_name != "Unknown":
                    try:
                        fallback_intent = self._coerce_parsed_intent(parsed_intent, query)
                        if not fallback_intent:
                            raise ValueError("Could not parse LangGraph fallback intent")

                        logger.info(f"🔄 LangGraph: Attempting fallback from {provider_name}...")
                        fallback_data = await self._try_with_fallback(
                            fallback_intent,
                            DataNotAvailableError(f"No data from {provider_name}")
                        )
                        if fallback_data:
                            logger.info(f"✅ LangGraph: Fallback succeeded!")
                            # Return successful fallback data
                            return QueryResponse(
                                conversationId=conversation_id,
                                intent=fallback_intent,
                                data=fallback_data,
                                clarificationNeeded=False,
                                processingSteps=tracker.to_list() if tracker else None,
                            )
                    except Exception as fallback_err:
                        logger.warning(f"LangGraph: All fallbacks failed: {fallback_err}")

                error_details = []
                error_details.append(f"No data found for **{indicators}**")
                if country:
                    error_details.append(f"for **{country}**")
                error_details.append(f"from **{provider_name}**.")

                # Add specific error detail if available
                if error_detail:
                    error_details.append(f"\n\n**Reason:** {error_detail}")

                suggestions = self._get_no_data_suggestions(provider_name, parsed_intent)

                return QueryResponse(
                    conversationId=conversation_id,
                    intent=parsed_intent if isinstance(parsed_intent, ParsedIntent) else None,
                    data=None,
                    clarificationNeeded=False,
                    error="no_data_found",
                    message=f"⚠️ **No Data Available**\n\n{' '.join(error_details)}\n\n{suggestions}",
                    processingSteps=tracker.to_list() if tracker else None,
                )

            # Build response
            response = QueryResponse(
                conversationId=conversation_id,
                clarificationNeeded=False,
                processingSteps=tracker.to_list() if tracker else None,
            )

            if data:
                response.data = data

                # Build intent from result
                response_intent = self._coerce_parsed_intent(result.get("parsed_intent"), query)
                if not response_intent:
                    response_intent = self._coerce_parsed_intent(query_result.get("intent"), query)

                if response_intent:
                    response_intent.parameters = dict(response_intent.parameters or {})
                    response_intent.parameters.setdefault(
                        "merge_with_previous", query_result.get("merge_series", False)
                    )
                    if not response_intent.recommendedChartType and query_result.get("chart_type"):
                        response_intent.recommendedChartType = query_result.get("chart_type")
                    response.intent = response_intent
                elif data and len(data) > 0:
                    first_data = data[0]
                    response.intent = ParsedIntent(
                        apiProvider=first_data.metadata.source if first_data.metadata else "UNKNOWN",
                        indicators=[d.metadata.indicator for d in data if d.metadata],
                        parameters={
                            "merge_with_previous": query_result.get("merge_series", False),
                        },
                        clarificationNeeded=False,
                        recommendedChartType=query_result.get("chart_type", "line"),
                        originalQuery=query,
                    )

                clarification_response = self._build_uncertain_result_clarification(
                    conversation_id=conversation_id,
                    query=query,
                    intent=response.intent,
                    data=data,
                    processing_steps=tracker.to_list() if tracker else None,
                )
                if clarification_response:
                    return clarification_response

            # If research query, add message
            if result.get("query_type") == "research":
                response.message = query_result.get("message", "")

            # Add to conversation history
            conversation_id = conversation_manager.add_message_safe(
                conversation_id,
                "assistant",
                f"Query processed: {result.get('query_type', 'data_fetch')}"
            )
            response.conversationId = conversation_id

            return response

        except Exception as e:
            logger.exception(f"LangGraph execution error: {e}")
            # Fall back to standard processing
            logger.warning("Falling back to standard query processing")
            return await self._standard_query_processing(
                query,
                conversation_id,
                tracker,
                record_user_message=False,
            )

    async def _standard_query_processing(
        self,
        query: str,
        conversation_id: str,
        tracker: Optional['ProcessingTracker'] = None,
        record_user_message: bool = True,
    ) -> QueryResponse:
        """
        Standard query processing (without orchestrator).
        Used as fallback when orchestrator fails.
        """
        # This is the original process_query logic
        # For now, just parse and fetch normally
        history = conversation_manager.get_history(conversation_id) if conversation_id else []

        if tracker:
            with tracker.track("parsing_query", "🤖 Understanding your question..."):
                parse_result = await self.pipeline.parse_and_route(query, history)
                intent = parse_result.intent
        else:
            parse_result = await self.pipeline.parse_and_route(query, history)
            intent = parse_result.intent

        if record_user_message:
            conversation_id = conversation_manager.add_message_safe(
                conversation_id,
                "user",
                query,
                intent=intent,
            )

        if intent.clarificationNeeded:
            return QueryResponse(
                conversationId=conversation_id,
                intent=intent,
                clarificationNeeded=True,
                clarificationQuestions=intent.clarificationQuestions,
                processingSteps=tracker.to_list() if tracker else None,
            )

        # Fetch data
        data = await retry_async(
            lambda: self._fetch_data(intent),
            max_attempts=3,
            initial_delay=1.0,
        )
        data = self._rerank_data_by_query_relevance(query, data)
        clarification_response = self._build_uncertain_result_clarification(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            data=data,
            processing_steps=tracker.to_list() if tracker else None,
        )
        if clarification_response:
            return clarification_response

        conversation_id = conversation_manager.add_message_safe(
            conversation_id,
            "assistant",
            f"Retrieved {len(data)} data series from {intent.apiProvider}",
        )

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            data=data,
            clarificationNeeded=False,
            processingSteps=tracker.to_list() if tracker else None,
        )

    async def _execute_pro_mode(self, query: str, conversation_id: str) -> QueryResponse:
        """Execute query using Pro Mode (LangChain agent or Grok code generation)"""
        tracker = get_processing_tracker()
        if tracker:
            with tracker.track(
                "pro_mode_activation",
                "🚀 Switching to Pro Mode for advanced analysis...",
                {"conversation_id": conversation_id},
            ):
                pass

        # Check if LangChain Pro Mode is enabled (v1 or v2)
        use_langchain_v2 = os.getenv('USE_LANGCHAIN_PROMODE_V2', 'false').lower() == 'true'
        use_langchain_v1 = os.getenv('USE_LANGCHAIN_PROMODE', 'false').lower() == 'true'
        use_langchain = use_langchain_v2 or use_langchain_v1

        if use_langchain:
            # Use LangChain agent implementation (v2 if enabled, otherwise v1)
            try:
                if use_langchain_v2:
                    from ..services.langchain_promode_v2 import LangChainProModeV2 as LangChainProMode
                    logger.info(
                        "🤖 Using LangChain v2 agent for Pro Mode (conversation: %s)...",
                        conversation_id
                    )
                else:
                    from ..services.langchain_promode import LangChainProMode
                    logger.info(
                        "🤖 Using LangChain v1 agent for Pro Mode (conversation: %s)...",
                        conversation_id
                    )

                # Get conversation history for context
                conversation_history = conversation_manager.get_messages(conversation_id)

                # Add current query to history
                conversation_id = conversation_manager.add_message_safe(
                    conversation_id,
                    "user",
                    query,
                )

                # Create and execute LangChain agent
                if tracker:
                    with tracker.track(
                        "langchain_agent_execution",
                        "🤖 Executing LangChain agent...",
                        {
                            "conversation_id": conversation_id,
                            "history_length": len(conversation_history),
                        },
                    ):
                        agent = LangChainProMode(conversation_id)
                        result = await agent.execute(query, chat_history=conversation_history)
                else:
                    agent = LangChainProMode(conversation_id)
                    result = await agent.execute(query, chat_history=conversation_history)

                # Convert LangChain result to QueryResponse format
                if result.get("success"):
                    output = result.get("output", "")

                    # Add to conversation history
                    conversation_id = conversation_manager.add_message_safe(
                        conversation_id,
                        "assistant",
                        f"LangChain Pro Mode: {output[:200]}..."
                    )

                    # Create response message
                    response_message = f"✅ **Pro Mode (LangChain Agent)**\n\n{output}"

                    return QueryResponse(
                        conversationId=conversation_id,
                        clarificationNeeded=False,
                        message=response_message,
                        isProMode=True,
                        processingSteps=tracker.to_list() if tracker else None,
                    )
                else:
                    error_msg = result.get("error", "Unknown error")
                    logger.error(f"LangChain agent execution failed: {error_msg}")

                    return QueryResponse(
                        conversationId=conversation_id,
                        clarificationNeeded=False,
                        error="langchain_error",
                        message=f"❌ **Pro Mode (LangChain) encountered an error**\n\n{error_msg}",
                        isProMode=True,
                        processingSteps=tracker.to_list() if tracker else None,
                    )

            except Exception as exc:
                logger.exception("LangChain Pro Mode error")
                # Fall back to Grok if LangChain fails
                logger.warning("Falling back to Grok-based Pro Mode due to LangChain error")
                use_langchain = False

        # Use original Grok-based Pro Mode implementation
        if not use_langchain:
            try:
                from ..services.grok import get_grok_service
                from ..services.code_executor import get_code_executor
                from ..services.session_storage import get_session_storage

                grok_service = get_grok_service()
                code_executor = get_code_executor()
                session_storage = get_session_storage()

                conversation_history = conversation_manager.get_messages(conversation_id)

                session_id = conversation_id[:8]
                available_keys = session_storage.list_keys(session_id)

                available_data = {}
                if available_keys:
                    available_data["session_data_available"] = available_keys
                    available_data["note"] = "Use load_session(key) to access this data - it's already fetched and ready!"

                # Dynamically discover Statistics Canada metadata for categorical queries
                from ..services.statscan_metadata import get_statscan_metadata_service
                from ..services.query_complexity import QueryComplexityAnalyzer

                # Analyze query for categorical patterns
                analysis = QueryComplexityAnalyzer.detect_complexity(query, intent=None)

                # If query is categorical and mentions StatsCan indicators, discover metadata
                if 'categorical_breakdown' in analysis.get('complexity_factors', []):
                    logger.info(f"🔍 Categorical query detected, attempting dynamic metadata discovery...")

                    # Try to extract indicator from query
                    indicator_keywords = {
                        'unemployment': 'Labour Force',
                        'employment': 'Labour Force',
                        'population': 'Population',
                        'gdp': 'Gross domestic product',
                        'immigration': 'Immigration',
                        'wages': 'Wages',
                    }

                    indicator_found = None
                    query_lower = query.lower()
                    for keyword, search_term in indicator_keywords.items():
                        if keyword in query_lower:
                            indicator_found = search_term
                            break

                    if indicator_found:
                        metadata_service = get_statscan_metadata_service()
                        try:
                            # Discover metadata for the indicator
                            discovered = await metadata_service.discover_for_query(
                                indicator=indicator_found,
                                category=None  # Let it find all dimensions
                            )

                            if discovered:
                                logger.info(
                                    f"✅ Discovered StatsCan metadata: product {discovered['product_id']} "
                                    f"with {discovered['dimension_count']} dimensions"
                                )
                                available_data["statscan_metadata"] = {
                                    "product_id": discovered["product_id"],
                                    "product_title": discovered["product_title"],
                                    "dimensions": discovered["dimensions"],
                                    "cube_start_date": discovered.get("cube_start_date"),
                                    "cube_end_date": discovered.get("cube_end_date"),
                                    "note": (
                                        f"Discovered metadata for {discovered['product_title']}. "
                                        f"Use coordinate API with product_id={discovered['product_id']} "
                                        f"and dimension IDs from 'dimensions' dict."
                                    )
                                }
                            else:
                                logger.warning(f"No metadata discovered for '{indicator_found}'")
                        except Exception as e:
                            logger.exception(f"Error discovering StatsCan metadata: {e}")

                # If no metadata discovered, provide fallback vector IDs
                if "statscan_metadata" not in available_data:
                    available_data["statscan_vectors"] = {
                        "GDP": 65201210,
                        "UNEMPLOYMENT": 2062815,  # Overall unemployment rate, 15 years and over
                        "INFLATION": 41690973,
                        "CPI": 41690914,
                        "POPULATION": 1,
                        "HOUSING_STARTS": 50483,
                        "EMPLOYMENT_RATE": 14609,
                        "note": "These are VERIFIED vector IDs that work with Vector API (getDataFromVectorsAndLatestNPeriods). For categorical breakdowns, Pro Mode will discover appropriate dimensions."
                    }

                conversation_id = conversation_manager.add_message_safe(
                    conversation_id,
                    "user",
                    query,
                )

                logger.info(
                    "🤖 Generating code with Grok (auto-switched, conversation: %s, history: %d, session data: %s)...",
                    conversation_id,
                    len(conversation_history),
                    available_keys or "none",
                )
                logger.info(f"📋 available_data keys: {list(available_data.keys())}")
                if tracker:
                    with tracker.track(
                        "pro_mode_generate_code",
                        "🤖 Generating custom code...",
                        {
                            "conversation_id": conversation_id,
                            "history_length": len(conversation_history),
                        },
                    ):
                        generated_code = await grok_service.generate_code(
                            query=query,
                            conversation_history=conversation_history,
                            available_data=available_data,
                            session_id=session_id
                        )
                else:
                    generated_code = await grok_service.generate_code(
                        query=query,
                        conversation_history=conversation_history,
                        available_data=available_data,
                        session_id=session_id
                    )

                # Save discovered metadata to session storage BEFORE code execution
                # so the generated code can access it via load_session('statscan_metadata')
                if "statscan_metadata" in available_data:
                    from ..services.session_storage import get_session_storage
                    session_storage = get_session_storage()
                    session_storage.save(session_id, "statscan_metadata", available_data["statscan_metadata"])
                    logger.info("💾 Saved StatsCan metadata to session storage for code execution")

                logger.info("⚡ Executing generated code with session: %s...", session_id)
                if tracker:
                    with tracker.track(
                        "executing_code",
                        "⚡ Executing Python code...",
                        {"conversation_id": conversation_id},
                    ) as update_execution_metadata:
                        execution_result = await code_executor.execute_code(
                            generated_code,
                            session_id=session_id
                        )
                        update_execution_metadata({
                            "has_error": bool(execution_result.error),
                            "files": len(execution_result.files or []),
                        })
                else:
                    execution_result = await code_executor.execute_code(
                        generated_code,
                        session_id=session_id
                    )

                if execution_result.error:
                    response_message = (
                        f"✅ **Auto-switched to Pro Mode**\n\nCode generated but execution failed: {execution_result.error}"
                    )
                elif execution_result.files:
                    response_message = (
                        f"✅ **Auto-switched to Pro Mode**\n\nCode executed successfully. Generated {len(execution_result.files)} file(s)."
                    )
                else:
                    response_message = "✅ **Auto-switched to Pro Mode**\n\nCode executed successfully."

                conversation_id = conversation_manager.add_message_safe(
                    conversation_id,
                    "assistant",
                    f"Auto-switched to Pro Mode. Generated and executed code. Output: {execution_result.output[:200]}"
                )

                return QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=False,
                    message=response_message,
                    codeExecution=execution_result,
                    isProMode=True,
                    processingSteps=tracker.to_list() if tracker else None,
                )

            except Exception as exc:
                logger.exception("Pro Mode auto-switch error")
                return QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=False,
                    error="pro_mode_error",
                    message=f"❌ **Auto-switched to Pro Mode but encountered an error**\n\n{str(exc)}",
                    isProMode=True,
                    processingSteps=tracker.to_list() if tracker else None,
                )


    async def _decompose_and_aggregate(
        self,
        query: str,
        intent: ParsedIntent,
        conversation_id: str,
        tracker: Optional['ProcessingTracker'] = None
    ) -> List[NormalizedData]:
        """
        Decompose a query into sub-queries for each entity and aggregate results.

        For example: "population of canada by provinces" →
            - "population of Ontario"
            - "population of Quebec"
            - ... (for all 13 provinces)

        Args:
            query: Original user query
            intent: Parsed intent with decomposition fields populated
            conversation_id: Conversation ID
            tracker: Optional processing tracker

        Returns:
            List of NormalizedData objects (one per entity)
        """
        logger.info("🔄 Starting query decomposition for %d %s",
                   len(intent.decompositionEntities), intent.decompositionType)

        # Check if provider has batch method for efficient multi-entity queries
        # This avoids timeouts by making single API call instead of N parallel requests
        if intent.apiProvider == "StatsCan" and intent.decompositionType in ["provinces", "regions", "territories"]:
            if hasattr(self.statscan_provider, 'fetch_multi_province_data'):
                logger.info("🚀 Using batch method for %d %s (single API call)",
                           len(intent.decompositionEntities), intent.decompositionType)

                try:
                    # Convert indicator name to vector ID using StatsCan's _vector_id method
                    indicator_name = intent.indicators[0] if intent.indicators else "Population"
                    vector_id = await self.statscan_provider._vector_id(
                        indicator_name,
                        intent.parameters.get("vectorId")
                    )

                    # Build parameters for batch method
                    params = {
                        "productId": vector_id,  # Use resolved vector ID
                        "indicator": indicator_name,
                        "provinces": intent.decompositionEntities,
                        "periods": intent.parameters.get("periods", 20),
                        "dimensions": intent.parameters.get("dimensions", {})
                    }

                    # Call batch method - returns List[NormalizedData]
                    batch_results = await self.statscan_provider.fetch_multi_province_data(params)
                    logger.info("✅ Batch method completed: %d provinces returned", len(batch_results))
                    return batch_results
                except Exception as e:
                    logger.warning("⚠️ Batch method failed (%s), falling back to parallel decomposition", str(e))
                    # Continue with normal decomposition below

        # Generate sub-queries for each entity
        sub_queries = []
        for entity in intent.decompositionEntities:
            sub_query = self._generate_sub_query(query, entity, intent.decompositionType)
            sub_queries.append((entity, sub_query))

        logger.debug("Generated %d sub-queries: %s", len(sub_queries), [sq[1] for sq in sub_queries[:3]])

        # Execute sub-queries in parallel using asyncio.gather
        if tracker:
            with tracker.track("fetching_data", f"📥 Fetching data for {len(sub_queries)} {intent.decompositionType}..."):
                results = await asyncio.gather(*[
                    self._execute_sub_query(entity, sq, intent, conversation_id)
                    for entity, sq in sub_queries
                ], return_exceptions=True)
        else:
            results = await asyncio.gather(*[
                self._execute_sub_query(entity, sq, intent, conversation_id)
                for entity, sq in sub_queries
            ], return_exceptions=True)

        # Filter out failed queries and aggregate successful results
        aggregated_data = []
        failed_count = 0

        for i, result in enumerate(results):
            entity = sub_queries[i][0]

            if isinstance(result, Exception):
                logger.warning("Sub-query for %s failed: %s", entity, result)
                failed_count += 1
                continue

            if result:
                # Add entity name to metadata for identification
                for normalized_data in result:
                    # Store entity name in metadata.country or a custom field
                    if intent.decompositionType == "provinces":
                        normalized_data.metadata.country = entity
                    elif intent.decompositionType == "states":
                        normalized_data.metadata.country = entity
                    elif intent.decompositionType == "countries":
                        # Already has country in metadata
                        pass

                aggregated_data.extend(result)

        logger.info("✅ Query decomposition completed: %d/%d entities succeeded, %d failed",
                   len(aggregated_data), len(sub_queries), failed_count)

        if not aggregated_data:
            raise Exception(f"All sub-queries failed for {intent.decompositionType}")

        return aggregated_data

    def _generate_sub_query(self, original_query: str, entity: str, decomposition_type: str) -> str:
        """
        Generate a sub-query for a specific entity.

        Examples:
            - "population of canada by provinces" + "Ontario" → "population of Ontario"
            - "GDP by each US state" + "California" → "GDP of California"

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

        logger.debug("Generated sub-query for %s: '%s' → '%s'", entity, original_query, sub_query)
        return sub_query

    async def _execute_sub_query(
        self,
        entity: str,
        sub_query: str,
        original_intent: ParsedIntent,
        conversation_id: str
    ) -> Optional[List[NormalizedData]]:
        """
        Execute a single sub-query for an entity.

        Args:
            entity: Entity name (e.g., "Ontario")
            sub_query: Modified query for this entity
            original_intent: Original parsed intent (for provider/indicator info)
            conversation_id: Conversation ID

        Returns:
            List of NormalizedData objects or None if failed
        """
        try:
            # Create a modified intent for this entity
            sub_intent = ParsedIntent(
                apiProvider=original_intent.apiProvider,
                indicators=original_intent.indicators,
                parameters={
                    **original_intent.parameters,
                    "entity": entity  # Add entity to parameters for provider to use
                },
                clarificationNeeded=False,
                needsDecomposition=False,  # Don't re-decompose
            )

            # Fetch data using the existing fetch logic
            async def fetch_with_intent():
                return await self._fetch_data(sub_intent)

            data = await retry_async(
                fetch_with_intent,
                max_attempts=2,  # Fewer retries for sub-queries
                initial_delay=0.5,
            )

            return data

        except DataNotAvailableError:
            logger.warning("Data not available for %s", entity)
            return None
        except Exception as e:
            logger.error("Failed to execute sub-query for %s: %s", entity, e)
            return None
