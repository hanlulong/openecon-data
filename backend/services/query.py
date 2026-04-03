from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from ..models import (
    ClarificationOption,
    CodeExecutionResult,
    DataPoint,
    GeneratedFile,
    NormalizedData,
    ParsedIntent,
    QueryResponse,
)
from ..config import Settings
from ..services.cache import cache_service
from ..services.redis_cache import get_redis_cache
from ..services.conversation import conversation_manager
from ..services.openrouter import OpenRouterService
from ..services.query_complexity import QueryComplexityAnalyzer
from ..services.parameter_validator import ParameterValidator
from ..services.metadata_search import MetadataSearchService
from ..routing.unified_router import (
    route_provider as unified_route_provider,
    correct_coingecko_misrouting as unified_correct_coingecko_misrouting,
    validate_routing as unified_validate_routing,
)
from ..services.indicator_translator import IndicatorTranslator
from ..services.indicator_resolver import get_indicator_resolver
from ..services.query_pipeline import ParseRouteResult, QueryPipeline, ValidationResult
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
# rate_limiter import removed — was unused
from ..services.time_range_defaults import apply_default_time_range
# SemanticClarifier removed in Phase 2 LLM refactor — the LLM prompt's
# clarificationNeeded field + ambiguity policy now handles broad-concept
# detection.  See simplified_prompt.py.
from ..utils.processing_steps import (
    ProcessingTracker,
    activate_processing_tracker,
    get_processing_tracker,
    reset_processing_tracker,
)
from ..services.relevance_scorer import (
    tokenize_indicator_terms as _rs_tokenize_indicator_terms,
    extract_indicator_cues as _rs_extract_indicator_cues,
    single_directional_cue as _rs_single_directional_cue,
    has_directional_conflict as _rs_has_directional_conflict,
    specific_cues_compatible as _rs_specific_cues_compatible,
    series_text_for_relevance as _rs_series_text_for_relevance,
    specialization_mismatch_penalty as _rs_specialization_mismatch_penalty,
    labor_rate_specificity_penalty as _rs_labor_rate_specificity_penalty,
    score_series_relevance as _rs_score_series_relevance,
    rerank_data_by_query_relevance as _rs_rerank_data_by_query_relevance,
    extract_ranking_value as _rs_extract_ranking_value,
)
from ..services.provider_fallback import (
    get_fallback_providers as _pf_get_fallback_providers,
    get_no_data_suggestions as _pf_get_no_data_suggestions,
    is_fallback_relevant as _pf_is_fallback_relevant,
    normalize_country_to_iso2 as _pf_normalize_country_to_iso2,
    provider_covers_country_list as _pf_provider_covers_country_list,
)
from ..services.indicator_resolution import (
    _effective_original_query as _ir_effective_original_query,
    code_semantic_hint as _ir_code_semantic_hint,
    score_resolved_indicator_relevance as _ir_score_resolved_indicator_relevance,
    minimum_resolved_relevance_threshold as _ir_minimum_resolved_relevance_threshold,
    is_placeholder_indicator_code as _ir_is_placeholder_indicator_code,
    is_resolved_indicator_plausible as _ir_is_resolved_indicator_plausible,
    extract_series_provider_and_code as _ir_extract_series_provider_and_code,
    has_implausible_top_series as _ir_has_implausible_top_series,
    normalize_bis_metadata_labels as _ir_normalize_bis_metadata_labels,
    apply_concept_provider_override as _ir_apply_concept_provider_override,
    indicator_resolution_threshold as _ir_indicator_resolution_threshold,
    apply_catalog_availability_override as _ir_apply_catalog_availability_override,
    resolve_indicator_for_fetch as _ir_resolve_indicator_for_fetch,
    select_indicator_query_for_resolution as _ir_select_indicator_query_for_resolution,
    is_ranking_query as _ir_is_ranking_query,
    is_comparison_query as _ir_is_comparison_query,
    is_temporal_split_query as _ir_is_temporal_split_query,
    extract_top_n_from_query as _ir_extract_top_n_from_query,
    extract_target_year_from_query as _ir_extract_target_year_from_query,
    build_distilled_indicator_query as _ir_build_distilled_indicator_query,
)
from ..services.indicator_clarification import (
    format_indicator_option_name as _ic_format_indicator_option_name,
    dedupe_indicator_choice_options as _ic_dedupe_indicator_choice_options,
    parse_indicator_option as _ic_parse_indicator_option,
    store_pending_indicator_options as _ic_store_pending_indicator_options,
    store_pending_semantic_clarification as _ic_store_pending_semantic_clarification,
    build_clarification_options as _ic_build_clarification_options,
    indicator_option_label_key as _ic_indicator_option_label_key,
    has_materially_distinct_indicator_options as _ic_has_materially_distinct_indicator_options,
    match_structured_clarification_option as _ic_match_structured_clarification_option,
    match_indicator_choice_option as _ic_match_indicator_choice_option,
    try_resolve_pending_indicator_choice as _ic_try_resolve_pending_indicator_choice,
    maybe_recover_from_uncertain_match as _ic_maybe_recover_from_uncertain_match,
    provider_supports_country_for_options as _ic_provider_supports_country_for_options,
    provider_supports_requested_scope as _ic_provider_supports_requested_scope,
    apply_indicator_option_to_intent as _ic_apply_indicator_option_to_intent,
    provider_can_execute_indicator_option as _ic_provider_can_execute_indicator_option,
    build_no_reliable_indicator_match_response as _ic_build_no_reliable_indicator_match_response,
    get_direct_provider_indicator_translation as _ic_get_direct_provider_indicator_translation,
    collect_indicator_choice_options as _ic_collect_indicator_choice_options,
    infer_query_concept_groups as _ic_infer_query_concept_groups,
    build_multi_concept_query_clarification as _ic_build_multi_concept_query_clarification,
    is_simple_single_country_query as _ic_is_simple_single_country_query,
    looks_informational as _ic_looks_informational,
    handle_informational_intent as _ic_handle_informational_intent,
    format_informational_results as _ic_format_informational_results,
    verify_semantic_discriminators as _ic_verify_semantic_discriminators,
    humanize_region_name as _ic_humanize_region_name,
    has_explicit_group_scope as _ic_has_explicit_group_scope,
    rewrite_group_scope_query as _ic_rewrite_group_scope_query,
    build_group_scope_clarification as _ic_build_group_scope_clarification,
    filter_viable_indicator_choice_options as _ic_filter_viable_indicator_choice_options,
    build_failed_indicator_choice_response as _ic_build_failed_indicator_choice_response,
    build_prefetch_indicator_choice_clarification as _ic_build_prefetch_indicator_choice_clarification,
    build_post_parse_clarification as _ic_build_post_parse_clarification,
    build_invalid_intent_response as _ic_build_invalid_intent_response,
    build_low_confidence_intent_response as _ic_build_low_confidence_intent_response,
    needs_indicator_clarification as _ic_needs_indicator_clarification,
    build_uncertain_result_clarification as _ic_build_uncertain_result_clarification,
    build_indicator_mismatch_hint as _ic_build_indicator_mismatch_hint,
    build_no_data_indicator_clarification as _ic_build_no_data_indicator_clarification,
    looks_like_provider_indicator_code as _ic_looks_like_provider_indicator_code,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (deduplicated from multiple call-sites)
# ---------------------------------------------------------------------------

# Year extraction – matches 4-digit years in the 1900–2099 range.
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# Currency pair parsing – "USD TO EUR", "EUR/GBP", "JPY-USD", "USD VS EUR"
_CURRENCY_TO_RE = re.compile(r"\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b")
_CURRENCY_SLASH_RE = re.compile(r"\b([A-Z]{3})[/\-]([A-Z]{3})\b")
_CURRENCY_VS_RE = re.compile(r"\b([A-Z]{3})\s+VS\.?\s+([A-Z]{3})\b")
_CURRENCY_CODE_RE = re.compile(r"\b([A-Z]{3})\b")

# Top-N detection – "top 10", "top 5", etc.
_TOP_N_RE = re.compile(r"\btop\s+(\d{1,3})\b")

# Option label parsing – strips leading "[PROVIDER] " prefix and trailing "(CODE)".
_OPTION_PROVIDER_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
_OPTION_TRAILING_PARENS_RE = re.compile(r"\s*\([^()]+\)\s*$")
_OPTION_TRAILING_PARENS_ALT_RE = re.compile(r"\([^()]*\)\s*$")
_OPTION_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Intent cache — avoids re-parsing identical queries via LLM (saves 4-6s)
# ---------------------------------------------------------------------------
_intent_cache: Dict[str, Tuple[Any, float]] = {}  # hash -> (ParseRouteResult, timestamp)
_INTENT_CACHE_TTL = 300  # 5 minutes
_INTENT_CACHE_MAX_SIZE = 200  # evict oldest when exceeded


def _intent_cache_key(query: str) -> str:
    """Deterministic hash for a query string (case-insensitive, stripped)."""
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()


def _get_cached_parse_result(query_hash: str) -> Optional[Any]:
    """Return cached ParseRouteResult if still fresh, else None."""
    entry = _intent_cache.get(query_hash)
    if entry is not None:
        result, ts = entry
        if time.time() - ts < _INTENT_CACHE_TTL:
            return result
        # Stale — remove
        del _intent_cache[query_hash]
    return None


def _put_cached_parse_result(query_hash: str, result: Any) -> None:
    """Store a ParseRouteResult in the intent cache."""
    # Simple size cap: drop oldest entries when over limit
    if len(_intent_cache) >= _INTENT_CACHE_MAX_SIZE:
        oldest_key = min(_intent_cache, key=lambda k: _intent_cache[k][1])
        del _intent_cache[oldest_key]
    _intent_cache[query_hash] = (result, time.time())


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
    # Special sentinel for catalog concepts with no available provider
    "not_available": "NOT_AVAILABLE",
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


def _coerce_generated_file(file_item: Any) -> Optional[GeneratedFile]:
    """Normalize generated file payloads to GeneratedFile objects."""
    if file_item is None:
        return None
    if isinstance(file_item, GeneratedFile):
        return file_item

    if isinstance(file_item, dict):
        return GeneratedFile(
            url=str(file_item.get("url", "") or ""),
            name=str(file_item.get("name", "") or ""),
            type=str(file_item.get("type", "file") or "file"),
        )

    # Handle objects with url/name/type attributes (including pydantic models).
    url = getattr(file_item, "url", None)
    name = getattr(file_item, "name", None)
    file_type = getattr(file_item, "type", None)
    if url is not None:
        resolved_url = str(url)
        resolved_name = str(name or resolved_url.rsplit("/", 1)[-1] or "file")
        resolved_type = str(file_type or "file")
        return GeneratedFile(url=resolved_url, name=resolved_name, type=resolved_type)

    if isinstance(file_item, str):
        resolved_url = file_item
        return GeneratedFile(
            url=resolved_url,
            name=resolved_url.rsplit("/", 1)[-1] or "file",
            type="file",
        )

    return None


class QueryService:
    # Bump when cache semantics change so stale entries from old logic are not reused.
    CACHE_KEY_VERSION = "2026-02-23.1"
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
        self.indicator_translator = IndicatorTranslator()
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
        from ..routing.unified_router import detect_explicit_provider_match
        match = detect_explicit_provider_match(query)
        return match[0] if match else None

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
        expanded_region_countries = CountryResolver.expand_regions_in_query(query)
        if not extracted_countries and not expanded_region_countries:
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

        # Region-based multi-country override: when query mentions a known
        # country group (G7, G20, BRICS, EU, ASEAN, etc.), always expand to
        # the full member list regardless of comparative language.  Previously
        # this was gated behind "comparative_markers" which caused queries
        # like "employment in G20" (without words like "compare") to miss the
        # expansion entirely.
        if len(expanded_region_countries) > 1:
            current_geo = current_countries[:] if current_countries else ([current_country] if current_country else [])
            normalized_current = [
                self._normalize_country_to_iso2(country) or str(country).upper()
                for country in current_geo
                if country
            ]
            normalized_target = [
                self._normalize_country_to_iso2(country) or str(country).upper()
                for country in expanded_region_countries
            ]
            if normalized_current != normalized_target:
                previous = current_country or (",".join(current_countries) if current_countries else "")
                intent.parameters.pop("country", None)
                intent.parameters["countries"] = expanded_region_countries
                logger.info(
                    "🌍 Region Override: '%s' -> %s (query specifies a country group)",
                    previous,
                    expanded_region_countries,
                )
                return

        # Multi-country override should apply whenever query explicitly names multiple
        # countries, even if parser already selected one non-US country.
        if len(extracted_countries) > 1:
            normalized_current = [
                self._normalize_country_to_iso2(country) or str(country).upper()
                for country in current_countries
                if country
            ]
            if current_country:
                normalized_current.append(
                    self._normalize_country_to_iso2(current_country) or str(current_country).upper()
                )
            normalized_current = list(dict.fromkeys(normalized_current))

            normalized_extracted = [
                self._normalize_country_to_iso2(country) or str(country).upper()
                for country in extracted_countries
            ]
            normalized_extracted = list(dict.fromkeys(normalized_extracted))

            if normalized_current != normalized_extracted:
                previous = current_country or (",".join(current_countries) if current_countries else "")
                intent.parameters.pop("country", None)
                intent.parameters["countries"] = extracted_countries
                logger.info(
                    "🌍 Country Override (multi): '%s' -> %s (query explicitly names multiple countries)",
                    previous,
                    extracted_countries,
                )
            return

        if not defaulted_to_us_or_empty:
            return

        # Single-country override
        if not extracted_countries:
            return
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

    @staticmethod
    def _extract_indicator_text_from_refined_query(refined_query: str) -> str:
        """Strip scope suffixes from a clarification-refined query."""
        text = str(refined_query or "").strip()
        if not text:
            return ""

        cleaned = re.sub(
            r"\s+(?:across|for)\s+.+$",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned or text

    def _normalize_country_targets(self, countries: List[str]) -> List[str]:
        """Normalize a list of country or region targets to deduplicated ISO2 codes when possible."""
        normalized: List[str] = []
        for country in countries:
            country_text = str(country or "").strip()
            if not country_text:
                continue
            normalized_country = self._normalize_country_to_iso2(country_text) or country_text.upper()
            if normalized_country not in normalized:
                normalized.append(normalized_country)
        return normalized

    def _looks_like_country_follow_up(
        self,
        query: str,
        target_countries: List[str],
    ) -> bool:
        """
        Detect short geography-only follow-ups such as "show only US" or "Japan".

        These should reuse the last intent instead of being reparsed as a brand new
        query with no indicator context.
        """
        query_text = str(query or "").strip()
        if not query_text or not target_countries:
            return False

        query_lower = query_text.lower()
        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        if not tokens:
            return False

        allowed_tokens = {
            "show", "only", "just", "keep", "filter", "now", "instead",
            "use", "plot", "display", "me", "the", "for", "in", "to",
            "add", "also", "include", "plus", "and", "with", "compare",
            "what", "about", "how", "same", "but", "too", "well", "as",
        }
        geography_tokens = {country.lower() for country in target_countries}
        for country in target_countries:
            if country.upper() == "US":
                geography_tokens.update({"united", "states", "usa", "us", "america"})
            iso3 = CountryResolver.to_iso3(country)
            if iso3:
                geography_tokens.add(iso3.lower())
            # Add common country name tokens so "Add Germany" matches ["DE"]
            for alias, code in CountryResolver.COUNTRY_ALIASES.items():
                if code == country.upper():
                    for token in alias.split():
                        geography_tokens.add(token.lower())

        non_geography_tokens = [
            token for token in tokens
            if token not in allowed_tokens and token not in geography_tokens
        ]
        return len(non_geography_tokens) == 0

    async def _fetch_from_coingecko(
        self,
        intent: ParsedIntent,
        params: dict,
    ) -> list:
        """Fetch cryptocurrency data from CoinGecko.

        Handles:
        - Coin ID mapping (ticker symbols → CoinGecko IDs)
        - Time period extraction from query text
        - Historical data (date range or days)
        - Current price/market_cap/volume/24h_change
        - Top N rankings by market cap

        Returns list of NormalizedData.
        """
        import re

        logger.info(f"🔍 CoinGecko Query Parameters:")
        logger.info(f"   - Indicators: {intent.indicators}")

        query_lower = intent.originalQuery.lower() if intent.originalQuery else ""

        # Extract time periods from query text
        time_patterns = ["last", "past", "previous", "recent", "historical",
                         "days", "weeks", "months", "year", "history"]
        mentions_time = any(p in query_lower for p in time_patterns)

        days_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+days?', query_lower)
        weeks_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+weeks?', query_lower)
        months_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+months?', query_lower)
        year_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+years?', query_lower)

        if not params.get("days"):
            extracted_days = None
            if days_match:
                extracted_days = int(days_match.group(1))
            elif weeks_match:
                extracted_days = int(weeks_match.group(1)) * 7
            elif months_match:
                extracted_days = int(months_match.group(1)) * 30
            elif year_match:
                extracted_days = int(year_match.group(1)) * 365
            elif mentions_time:
                extracted_days = 30
            if extracted_days:
                params["days"] = extracted_days
                params.pop("startDate", None)
                params.pop("endDate", None)

        # Parse coin IDs from params
        raw_coin_ids = params.get("coinIds")
        if isinstance(raw_coin_ids, list):
            coin_ids = [str(cid).strip() for cid in raw_coin_ids if str(cid).strip()]
        elif isinstance(raw_coin_ids, str):
            coin_ids = [p.strip() for p in raw_coin_ids.split(",") if p.strip()]
        else:
            coin_ids = []

        # Sanitize vs_currency
        raw_vs = str(params.get("vsCurrency") or "usd").strip().lower()
        invalid_tokens = {"right", "now", "today", "current", "recent", "latest",
                          "trend", "performance", "history", "historical"}
        vs_currency = raw_vs if raw_vs not in invalid_tokens and re.fullmatch(r"[a-z]{3,10}", raw_vs) else "usd"
        params["vsCurrency"] = vs_currency

        # Coin name → CoinGecko ID mapping
        coin_map = {
            "bitcoin": "bitcoin", "btc": "bitcoin",
            "ethereum": "ethereum", "eth": "ethereum",
            "solana": "solana", "sol": "solana",
            "cardano": "cardano", "ada": "cardano",
            "polkadot": "polkadot", "dot": "polkadot",
            "avalanche": "avalanche-2", "avax": "avalanche-2",
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

        # Map provided coin IDs
        if coin_ids:
            coin_ids = [coin_map.get(c.lower(), c) for c in coin_ids]
        else:
            # Auto-detect from indicators
            for indicator in (intent.indicators or []):
                ind_lower = indicator.lower().replace(" ", "")
                for name, cid in coin_map.items():
                    if name in ind_lower:
                        coin_ids.append(cid)
                        break

            # Fallback: check query text
            if not coin_ids:
                for name, cid in coin_map.items():
                    if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", query_lower):
                        if cid not in coin_ids:
                            coin_ids.append(cid)
                if not coin_ids:
                    coin_ids = ["bitcoin"]

        logger.info(f"   - Resolved coins: {coin_ids}, vs={vs_currency}")

        indicator_lower = " ".join(intent.indicators).lower() if intent.indicators else ""
        metric_text = f"{indicator_lower} {query_lower}".strip()

        # Historical data request
        if params.get("startDate") or params.get("endDate") or params.get("days"):
            hist_metric = "price"
            if any(t in metric_text for t in ["market cap", "market capitalization", "marketcap"]):
                hist_metric = "market_cap"
            elif any(t in metric_text for t in ["volume", "trading volume", "24h volume"]):
                hist_metric = "volume"

            if params.get("startDate") and params.get("endDate"):
                series_list = []
                for coin_id in coin_ids:
                    data = await self.coingecko_provider.get_historical_data_range(
                        coin_id=coin_id, vs_currency=vs_currency,
                        from_date=params["startDate"], to_date=params["endDate"],
                        metric=hist_metric,
                    )
                    series_list.extend(data)
                return series_list
            else:
                days = params.get("days", 30)
                series_list = []
                for coin_id in coin_ids:
                    data = await self.coingecko_provider.get_historical_data(
                        coin_id=coin_id, vs_currency=vs_currency,
                        metric=hist_metric, days=days,
                    )
                    series_list.extend(data)
                return series_list

        # Current data
        ranking_keywords = ["top", "top 10", "top 5", "top 20", "ranking", "rankings", "largest", "biggest"]
        is_ranking = any(t in metric_text for t in ranking_keywords)

        if is_ranking and "market cap" in metric_text:
            top_n_match = _TOP_N_RE.search(query_lower)
            per_page = int(top_n_match.group(1)) if top_n_match else 10
            per_page = max(1, min(250, per_page))
            return await self.coingecko_provider.get_market_data(
                vs_currency=vs_currency, order="market_cap_desc", per_page=per_page,
            )

        metric = "price"
        if any(t in metric_text for t in ["volume", "trading volume", "24h volume", "24-hour volume"]):
            metric = "volume"
        elif any(t in metric_text for t in ["market cap", "market capitalization", "marketcap"]):
            metric = "market_cap"
        elif any(t in metric_text for t in ["24h change", "24 hour change", "price change", "change"]):
            metric = "24h_change"

        return await self.coingecko_provider.get_simple_price(
            coin_ids=coin_ids, vs_currency=vs_currency, metric=metric,
        )

    async def _fetch_exchange_rate_with_historical_fallback(
        self,
        intent: ParsedIntent,
        params: dict,
    ) -> list:
        """Fetch exchange rate data, falling back to FRED for historical requests.

        The ExchangeRate-API free tier only supports current rates.
        For historical data, we fall back to FRED which has daily exchange
        rate series for 21 major currency pairs.

        Returns list of NormalizedData.
        Raises DataNotAvailableError if neither source can serve the request.
        """
        import re
        from datetime import datetime, timedelta

        logger.info("🔍 ExchangeRate Query Parameters:")
        logger.info(f"   - baseCurrency: {params.get('baseCurrency', 'USD')}")
        logger.info(f"   - targetCurrency: {params.get('targetCurrency')}")
        logger.info(f"   - startDate: {params.get('startDate')}")

        # Detect if user is requesting historical data
        has_historical = False
        query_lower = (intent.originalQuery or "").lower()
        historical_patterns = [
            r'\bfor\s+20\d{2}\b',
            r'\b20\d{2}\s*-\s*20\d{2}\b',
            r'\blast\s+\d+\s+(month|year|day|week)s?\b',
            r'\bhistory\b', r'\bhistorical\b',
            r'\bfrom\s+20\d{2}\b', r'\bsince\s+20\d{2}\b',
        ]
        for pat in historical_patterns:
            if re.search(pat, query_lower):
                has_historical = True
                logger.info(f"   📅 Historical request detected: '{pat}'")
                break

        if not has_historical:
            start_date = params.get("startDate")
            end_date = params.get("endDate")
            if start_date or end_date:
                try:
                    today = datetime.now().date()
                    week_ago = today - timedelta(days=7)
                    if start_date:
                        start_dt = datetime.fromisoformat(start_date[:10]).date()
                        if start_dt < week_ago:
                            has_historical = True
                    if end_date and not has_historical:
                        end_dt = datetime.fromisoformat(end_date[:10]).date()
                        if end_dt < today - timedelta(days=1):
                            has_historical = True
                except (ValueError, AttributeError):
                    pass

        if has_historical:
            logger.warning("⚠️ ExchangeRate: Historical data requested — falling back to FRED")
            result = await self._fetch_historical_exchange_from_fred(intent, params)
            if result:
                return result
            raise DataNotAvailableError(
                "Historical exchange rate data is not available with the free ExchangeRate API tier. "
                "\n\n💡 **Alternatives:**\n"
                "1. For **current rates**: Rephrase without time references\n"
                "2. For **historical rates**: Use a paid ExchangeRate API key\n"
                "3. For **Real Effective Exchange Rate** (REER): Ask for 'REER' (uses IMF data)\n\n"
                "Note: Some bilateral exchange rates are available via FRED for major currency pairs."
            )

        # Current rate — use ExchangeRate-API
        series = await self.exchangerate_provider.fetch_exchange_rate(
            base_currency=params.get("baseCurrency", "USD"),
            target_currency=params.get("targetCurrency"),
            target_currencies=params.get("targetCurrencies"),
        )
        return [series]

    async def _fetch_historical_exchange_from_fred(
        self,
        intent: ParsedIntent,
        params: dict,
    ) -> Optional[list]:
        """Attempt to fetch historical exchange rate from FRED.

        FRED has daily exchange rate series for 21 major currency pairs.
        Returns list of NormalizedData on success, None if currency not supported.
        """
        base_currency = params.get("baseCurrency", "USD")
        target_currency = params.get("targetCurrency")

        if not target_currency:
            query_upper = (intent.originalQuery or "").upper()
            to_match = _CURRENCY_TO_RE.search(query_upper)
            slash_match = re.search(r'\b([A-Z]{3})[/\s](?:VS\s)?([A-Z]{3})\b', query_upper)
            if to_match:
                base_currency, target_currency = to_match.group(1), to_match.group(2)
            elif slash_match:
                base_currency, target_currency = slash_match.group(1), slash_match.group(2)

        if not target_currency:
            return None

        # FRED USD-based exchange rate series
        fred_fx = {
            "EUR": "DEXUSEU", "GBP": "DEXUSUK", "JPY": "DEXJPUS",
            "CAD": "DEXCAUS", "CHF": "DEXSZUS", "AUD": "DEXUSAL",
            "CNY": "DEXCHUS", "MXN": "DEXMXUS", "INR": "DEXINUS",
            "BRL": "DEXBZUS", "KRW": "DEXKOUS", "SEK": "DEXSDUS",
            "NOK": "DEXNOUS", "DKK": "DEXDNUS", "SGD": "DEXSIUS",
            "HKD": "DEXHKUS", "NZD": "DEXUSNZ", "ZAR": "DEXSFUS",
            "THB": "DEXTHUS", "MYR": "DEXMAUS", "TWD": "DEXTAUS",
        }

        target_upper = target_currency.upper()
        base_upper = base_currency.upper()

        fred_series_id = None
        if target_upper in fred_fx and target_upper != "USD":
            fred_series_id = fred_fx[target_upper]
        elif base_upper in fred_fx and target_upper == "USD":
            fred_series_id = fred_fx[base_upper]
        elif base_upper != "USD" and target_upper != "USD":
            fred_series_id = fred_fx.get(base_upper) or fred_fx.get(target_upper)

        if not fred_series_id:
            return None

        try:
            series = await self.fred_provider.fetch_series({
                "seriesId": fred_series_id,
                "startDate": params.get("startDate"),
                "endDate": params.get("endDate"),
            })
            series.metadata.indicator = f"{base_upper} to {target_upper} Exchange Rate"
            series.metadata.source = "FRED (Federal Reserve)"
            return [series]
        except Exception as e:
            logger.warning(f"FRED exchange rate fallback failed: {e}")
            return None

    def _extract_concept_from_indicator(
        self,
        indicators: List[str],
        original_query: Optional[str],
    ) -> Optional[str]:
        """Extract the human-readable concept from provider-specific indicator codes.

        When switching providers in a country follow-up (e.g., FRED→WorldBank),
        we need the concept term ("unemployment") instead of the provider code
        ("UNRATE") so the new provider's resolver can find the right indicator.

        Strategy: prefer the original query's indicator term; fall back to
        looking up the code's name in the indicator database.
        """
        if not indicators:
            return None

        code = str(indicators[0]).strip()

        # If the indicator is already a natural-language concept, keep it
        if " " in code or code.islower():
            return code

        # Try to extract concept from the original query
        if original_query:
            query_lower = str(original_query).lower()
            # Check if any known indicator switch term appears in the query
            for term in sorted(self._INDICATOR_SWITCH_TERMS, key=len, reverse=True):
                if term in query_lower:
                    return term

        # Fall back to looking up the code's name in the indicator database
        try:
            resolver = get_indicator_resolver()
            lookup = getattr(resolver, 'lookup', None)
            if lookup:
                for provider_name in ["FRED", "WORLDBANK", "IMF", "EUROSTAT"]:
                    meta = lookup.get(provider_name, code)
                    if meta and meta.get("name"):
                        # Extract first meaningful word from the name
                        name = meta["name"].lower()
                        for term in sorted(self._INDICATOR_SWITCH_TERMS, key=len, reverse=True):
                            if term in name:
                                return term
                        return name.split(",")[0].strip()[:50]
        except Exception:
            pass

        return code.lower()

    def _build_contextual_follow_up_query(
        self,
        last_intent: ParsedIntent,
        target_countries: List[str],
    ) -> Optional[str]:
        """Build a deterministic full query from the last intent plus new country scope."""
        indicators = [str(indicator).strip() for indicator in (last_intent.indicators or []) if str(indicator).strip()]
        if not indicators or not target_countries:
            return None

        indicator_text = " and ".join(indicators)
        if len(target_countries) == 1:
            return f"{indicator_text} in {target_countries[0]}"
        return f"{indicator_text} across {', '.join(target_countries)}"

    def _rewrite_query_with_country_targets(
        self,
        base_query: str,
        target_countries: List[str],
    ) -> Optional[str]:
        """Rewrite a prior query so only the geography scope changes."""
        query_text = str(base_query or "").strip()
        if not query_text or not target_countries:
            return None

        new_scope = (
            f"in {target_countries[0]}"
            if len(target_countries) == 1
            else f"across {', '.join(target_countries)}"
        )

        regions = CountryResolver.detect_regions_in_query(query_text)
        patterns: List[str] = [
            r"\bacross\s+[^,.;!?]+?\s+member countries\b",
            r"\bfor\s+the\s+[^,.;!?]+?\s+group(?:\s+as\s+a\s+whole)?\b",
        ]

        for region in regions:
            region_text = re.escape(str(region or "").strip())
            region_label = re.escape(self._humanize_region_name(region))
            patterns = [
                rf"\bacross\s+(?:the\s+)?{region_text}(?:\s+member countries)?\b",
                rf"\bacross\s+(?:the\s+)?{region_label}(?:\s+member countries)?\b",
                rf"\bfor\s+(?:the\s+)?{region_text}(?:\s+group(?:\s+as\s+a\s+whole)?)?\b",
                rf"\bfor\s+(?:the\s+)?{region_label}(?:\s+group(?:\s+as\s+a\s+whole)?)?\b",
                rf"\bin\s+(?:the\s+)?{region_text}\b",
                rf"\bin\s+(?:the\s+)?{region_label}\b",
            ] + patterns

        for pattern in patterns:
            if re.search(pattern, query_text, flags=re.IGNORECASE):
                rewritten = re.sub(pattern, new_scope, query_text, count=1, flags=re.IGNORECASE)
                return re.sub(r"\s{2,}", " ", rewritten).strip()

        distilled_indicator = self._build_distilled_indicator_query(query_text)
        if distilled_indicator:
            return f"{distilled_indicator} {new_scope}"
        return None

    # ── Structured context injection & follow-up detection ──────────────
    # NOTE: _build_conversation_context_prefix has been removed. Conversation
    # context is now injected into the LLM system prompt via
    # SimplifiedPrompt.generate(conversation_context=...) which provides
    # follow-up detection instructions alongside the context. This replaces
    # both the old context prefix injection AND the regex-based _detect_follow_up.

    # NOTE: Regex-based _detect_follow_up and its 5 compiled regex patterns
    # (_COUNTRY_CHANGE_RE, _WHAT_ABOUT_COUNTRY_RE, _TIME_CHANGE_RE,
    # _PROVIDER_CHANGE_RE, _PRONOUN_REUSE_RE) have been removed in favor of
    # LLM-based follow-up detection via dynamic prompt construction.
    # The LLM now receives conversation context in the system prompt and
    # returns isFollowUp, followUpType, and resolvedQuery fields directly.

    # Known indicator terms for detecting indicator-switch follow-ups
    _INDICATOR_SWITCH_TERMS = {
        "gdp", "gdp growth", "gdp per capita", "real gdp",
        "inflation", "cpi", "consumer prices",
        "unemployment", "employment", "labor force", "jobs",
        "trade balance", "trade openness", "exports", "imports", "trade",
        "population", "population growth",
        "debt", "government debt", "debt to gdp", "public debt",
        "interest rate", "policy rate", "federal funds rate",
        "exchange rate", "currency",
        "life expectancy", "fertility", "mortality",
        "savings", "investment", "fdi", "foreign direct investment",
        "current account", "fiscal balance", "budget deficit",
        "poverty", "inequality", "gini",
        "co2 emissions", "energy", "renewable energy",
        "money supply", "m2",
        "housing", "house prices", "housing starts",
        "bitcoin", "ethereum", "crypto",
        "gold", "oil", "commodity",
    }

    def _build_intent_from_indicator_switch(
        self,
        query: str,
        conversation_id: str,
    ) -> Optional[Tuple[str, ParsedIntent, ParseRouteResult]]:
        """
        Resolve indicator-switch follow-ups that keep the country but change the metric.

        Example:
        - previous: "GDP of Germany 2018-2023"
        - follow-up: "what about inflation"
        - rewritten query: "inflation in Germany 2018-2023"
        """
        last_intent = conversation_manager.get_last_intent(conversation_id)
        if not last_intent or last_intent.clarificationNeeded:
            return None

        query_lower = str(query or "").lower().strip()
        if not query_lower or len(query_lower) > 60:
            return None

        # Must mention a known indicator
        matched_indicator = None
        for term in sorted(self._INDICATOR_SWITCH_TERMS, key=len, reverse=True):
            if term in query_lower:
                matched_indicator = term
                break
        if not matched_indicator:
            return None

        # Check for switch marker OR bare indicator term (1-3 words, unambiguous).
        # Bare terms like "unemployment" or "inflation" are clear indicator switches
        # when conversation context exists. Ambiguous single words are excluded.
        switch_markers = {"what about", "show", "how about", "switch to", "instead", "now", "what is", "what's"}
        has_marker = any(marker in query_lower for marker in switch_markers)
        if not has_marker:
            # Allow bare indicator terms only if query is very short (1-3 words)
            # and the matched term is unambiguous
            word_count = len(query_lower.split())
            _ambiguous_bare_terms = {
                "trade", "energy", "jobs", "currency", "commodity",
                "crypto", "gold", "oil", "housing", "savings",
                "investment", "debt",
            }
            if word_count > 3 or matched_indicator in _ambiguous_bare_terms:
                return None

        # Must NOT mention a new country (that would be a country follow-up)
        extracted_countries = self._extract_countries_from_query(query)
        if extracted_countries:
            return None

        # Reuse country from prior intent
        prior_countries = self._collect_target_countries(last_intent.parameters)
        if not prior_countries:
            # Infer country from provider — FRED is US-only, StatsCan is CA-only
            prior_provider = normalize_provider_name(last_intent.apiProvider or "")
            if prior_provider == "FRED":
                prior_countries = ["US"]
            elif prior_provider in ("STATSCAN", "STATISTICS CANADA"):
                prior_countries = ["CA"]
            else:
                return None

        # Build new query: "indicator in country(ies)"
        if len(prior_countries) == 1:
            refined_query = f"{matched_indicator} in {prior_countries[0]}"
        else:
            refined_query = f"{matched_indicator} across {', '.join(prior_countries)}"

        # Preserve time period from prior intent
        params = dict(last_intent.parameters or {})
        start_date = params.get("startDate")
        end_date = params.get("endDate")
        if start_date and end_date:
            refined_query += f" {start_date[:4]}-{end_date[:4]}"

        logger.info(
            "🔄 Indicator switch follow-up: '%s' → '%s' (preserving countries=%s)",
            query, refined_query, prior_countries,
        )

        # Route the new query
        routing_decision = self.unified_router.route(
            query=refined_query,
            indicators=[matched_indicator],
            country=prior_countries[0] if len(prior_countries) == 1 else None,
            countries=prior_countries if len(prior_countries) > 1 else None,
        )
        api_provider = normalize_provider_name(routing_decision.provider)

        # Build new intent
        new_params: Dict[str, Any] = {}
        if start_date:
            new_params["startDate"] = start_date
        if end_date:
            new_params["endDate"] = end_date
        if len(prior_countries) == 1:
            new_params["country"] = prior_countries[0]
        else:
            new_params["countries"] = prior_countries

        intent = ParsedIntent(
            apiProvider=api_provider,
            indicators=[matched_indicator],
            parameters=new_params,
            clarificationNeeded=False,
            originalQuery=refined_query,
            confidence=0.90,
            isFollowUp=True,
            followUpType="indicator_switch",
            resolvedQuery=refined_query,
        )

        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider=api_provider,
            validation_warning=None,
        )

        return refined_query, intent, parse_result

    def _build_intent_from_contextual_follow_up(
        self,
        query: str,
        conversation_id: str,
    ) -> Optional[Tuple[str, ParsedIntent, ParseRouteResult]]:
        """
        Resolve short country-only follow-ups against the previous intent.

        Example:
        - previous: "employment rate across G20 member countries"
        - follow-up: "show only US"
        - rewritten query: "employment rate in US"
        """
        last_intent = conversation_manager.get_last_intent(conversation_id)
        if not last_intent or last_intent.clarificationNeeded:
            return None

        extracted_countries = self._extract_countries_from_query(query)
        expanded_region_countries = CountryResolver.expand_regions_in_query(query)
        target_countries = self._normalize_country_targets(extracted_countries or expanded_region_countries)
        if not self._looks_like_country_follow_up(query, target_countries):
            return None

        # Detect ADDITIVE follow-ups ("compare with", "add", "also include", "plus")
        # vs REPLACEMENT follow-ups ("show only", "just", "filter to")
        query_lower = str(query or "").lower()
        additive_markers = {"compare", "add", "also", "include", "plus", "too", "well"}
        # "and" excluded — too generic ("show only US and China" is replacement, not addition)
        replacement_markers = {"only", "just", "filter", "keep"}
        query_words = set(query_lower.split())
        is_additive = bool(query_words & additive_markers) and not bool(query_words & replacement_markers)

        # For additive follow-ups, merge new countries with prior countries
        if is_additive:
            prior_countries = self._collect_target_countries(last_intent.parameters)
            merged = list(dict.fromkeys(prior_countries + target_countries))  # Dedupe, preserve order
            if len(merged) > len(target_countries):
                target_countries = merged
                logger.info(
                    "🔗 Additive follow-up: merged %s with prior %s → %s",
                    target_countries[-len(target_countries):], prior_countries, merged,
                )

        refined_query = self._build_contextual_follow_up_query(last_intent, target_countries)
        if not refined_query:
            return None

        params = dict(last_intent.parameters or {})
        for key in ("country", "countries", "reporter", "reporters", "partner", "region"):
            params.pop(key, None)

        if len(target_countries) == 1:
            params["country"] = target_countries[0]
        else:
            params["countries"] = target_countries

        routing_decision = self.unified_router.route(
            query=refined_query,
            indicators=last_intent.indicators,
            country=params.get("country"),
            countries=params.get("countries"),
            llm_provider=last_intent.apiProvider,
        )
        api_provider = normalize_provider_name(routing_decision.provider)

        # Check provider coverage for ALL target countries (both singular and plural).
        # Handles: FRED (US-only) + "add France" → switch to WorldBank.
        all_target = params.get("countries") or ([params["country"]] if params.get("country") else [])
        provider_switched = False
        if all_target and not self._provider_covers_country_list(api_provider, all_target):
            logger.info(
                "🔄 Country follow-up: %s can't cover %s, switching to WORLDBANK",
                api_provider, all_target,
            )
            api_provider = "WORLDBANK"
            provider_switched = True

        intent = last_intent.model_copy(deep=True)
        intent.apiProvider = api_provider
        intent.parameters = params

        # When provider switches, reset indicator to concept-level term so
        # _resolve_indicator_for_fetch can find the right code for the new provider.
        # E.g., FRED/UNRATE → "unemployment" for WorldBank resolution.
        if provider_switched:
            concept_indicator = self._extract_concept_from_indicator(
                last_intent.indicators, last_intent.originalQuery
            )
            if concept_indicator:
                intent.indicators = [concept_indicator]
                params.pop("indicator", None)
                intent.parameters = params
                # Rebuild the refined query with the concept term
                refined_query = self._build_contextual_follow_up_query(intent, target_countries)
                logger.info(
                    "🔄 Reset indicator to concept '%s' after provider switch → query='%s'",
                    concept_indicator, refined_query,
                )
        intent.clarificationNeeded = False
        intent.clarificationQuestions = []
        intent.confidence = max(float(last_intent.confidence or 0.0), 0.95)
        intent.originalQuery = refined_query

        # Populate follow-up detection fields
        intent.isFollowUp = True
        intent.followUpType = "country_change"
        intent.resolvedQuery = refined_query

        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=self._normalize_provider_alias(self._detect_explicit_provider(refined_query)),
            routed_provider=api_provider,
            validation_warning=unified_validate_routing(
                api_provider,
                refined_query,
                intent,
            ),
        )
        return refined_query, intent, parse_result

    def _build_intent_from_semantic_clarification(
        self,
        pending: Dict[str, Any],
        selected_option: ClarificationOption,
        refined_query: str,
    ) -> Optional[ParsedIntent]:
        """
        Build a deterministic intent for clarification follow-ups when possible.

        This avoids sending an already-disambiguated reply back through the full
        LLM parse path.
        """
        kind = str(pending.get("kind") or "").strip()
        option_label = str(selected_option.label or "").strip()
        query_text = str(refined_query or "").strip()
        if not query_text:
            return None

        # "group as a whole" still requires more nuanced aggregate handling.
        if kind == "group_scope" and "compare member countries" not in option_label.lower():
            return None

        extracted_countries = self._extract_countries_from_query(query_text)
        expanded_region_countries = CountryResolver.expand_regions_in_query(query_text)
        params: Dict[str, Any] = {}

        if expanded_region_countries and (
            "member countries" in query_text.lower()
            or self._is_comparison_query(query_text)
        ):
            params["countries"] = expanded_region_countries
        elif len(extracted_countries) == 1:
            params["country"] = extracted_countries[0]
        elif len(extracted_countries) > 1:
            params["countries"] = extracted_countries

        indicator_text = option_label if kind != "group_scope" else self._extract_indicator_text_from_refined_query(query_text)
        indicator_text = str(indicator_text or "").strip() or self._extract_indicator_text_from_refined_query(query_text)
        if not indicator_text:
            return None

        routing_decision = self.unified_router.route(
            query=query_text,
            indicators=[indicator_text],
            country=params.get("country"),
            countries=params.get("countries"),
            llm_provider=None,
        )
        api_provider = normalize_provider_name(routing_decision.provider)
        if params.get("countries") and len(params["countries"]) > 1 and not self._provider_covers_country_list(api_provider, params["countries"]):
            api_provider = "WORLDBANK"

        intent = ParsedIntent(
            apiProvider=api_provider,
            indicators=[indicator_text],
            parameters=params,
            clarificationNeeded=False,
            confidence=0.95,
            recommendedChartType="line",
            originalQuery=query_text,
        )
        self._apply_country_overrides(intent, query_text)
        return intent

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
            # Short-circuit: catalog says no provider carries this concept
            if routed_provider == "NOT_AVAILABLE":
                intent.apiProvider = "not_available"
                return "NOT_AVAILABLE"
            # When routing was decided by catalog or coverage preference
            # (country-specific provider), mark the intent so downstream
            # prefetch clarification trusts the resolution.
            if deterministic_match_type in ("catalog", "country", "coverage"):
                params = dict(intent.parameters or {})
                params["__catalog_resolved"] = True
                intent.parameters = params
        except Exception as exc:
            logger.warning(
                "UnifiedRouter baseline failed, falling back to legacy deterministic router: %s",
                exc,
            )
            routed_provider = unified_route_provider(intent, query)

        routed_provider = unified_correct_coingecko_misrouting(
            routed_provider,
            query,
            intent.indicators,
        )
        explicit_provider_requested = normalize_provider_name(
            self._detect_explicit_provider(query or intent.originalQuery or "") or ""
        )
        if explicit_provider_requested:
            intent.apiProvider = explicit_provider_requested
            return explicit_provider_requested
        if countries and len(countries) > 1 and not self._provider_covers_country_list(routed_provider, countries):
            logger.info(
                "🧭 Coverage override: %s does not cover countries=%s, using WorldBank baseline",
                routed_provider,
                countries,
            )
            routed_provider = "WORLDBANK"
            deterministic_match_type = "coverage_override"
            deterministic_confidence = min(deterministic_confidence or 0.0, 0.78)

        params_before_override = dict(params)
        routed_provider_before_override = routed_provider
        routed_provider, params = self._apply_concept_provider_override(
            routed_provider,
            intent,
            params,
        )
        routed_provider = normalize_provider_name(routed_provider)
        intent.parameters = params
        if (
            routed_provider != routed_provider_before_override
            or params.get("indicator") != params_before_override.get("indicator")
        ):
            logger.info(
                "🧭 Catalog concept override locked provider selection: %s -> %s",
                routed_provider_before_override,
                routed_provider,
            )
            return routed_provider

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
                semantic_provider = unified_correct_coingecko_misrouting(
                    semantic_provider,
                    query,
                    intent.indicators,
                )
                if countries and len(countries) > 1 and not self._provider_covers_country_list(semantic_provider, countries):
                    logger.info(
                        "🧭 Semantic provider rejected by coverage: %s for countries=%s",
                        semantic_provider,
                        countries,
                    )
                    return routed_provider
                semantic_confidence = float(getattr(decision, "confidence", 0.0) or 0.0)
                # Framework guardrail: preserve high-confidence deterministic decisions unless
                # semantic routing is materially stronger. This prevents low-similarity
                # semantic matches from overriding precise rule-based routing.
                if semantic_provider != routed_provider:
                    deterministic_locked = (
                        deterministic_confidence >= 0.88
                        and deterministic_match_type in {"explicit", "us_only", "indicator", "catalog"}
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
            hybrid_provider = unified_correct_coingecko_misrouting(
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
        """Tokenize indicator text into comparable semantic terms.

        Delegates to :func:`relevance_scorer.tokenize_indicator_terms`.
        """
        return _rs_tokenize_indicator_terms(text)

    def _extract_indicator_cues(self, text: str) -> set[str]:
        """Extract high-signal semantic cues for intent/indicator consistency checks.

        Delegates to :func:`relevance_scorer.extract_indicator_cues`.
        """
        return _rs_extract_indicator_cues(text)

    @staticmethod
    def _single_directional_cue(cues: set[str]) -> str:
        """Delegates to :func:`relevance_scorer.single_directional_cue`."""
        return _rs_single_directional_cue(cues)

    @classmethod
    def _has_directional_conflict(
        cls,
        query_cues: set[str],
        candidate_cues: set[str],
    ) -> bool:
        """Delegates to :func:`relevance_scorer.has_directional_conflict`."""
        return _rs_has_directional_conflict(query_cues, candidate_cues)

    @classmethod
    def _specific_cues_compatible(
        cls,
        query_cues: set[str],
        candidate_cues: set[str],
    ) -> bool:
        """Delegates to :func:`relevance_scorer.specific_cues_compatible`."""
        return _rs_specific_cues_compatible(query_cues, candidate_cues)

    def _series_text_for_relevance(self, series: Any) -> str:
        """Delegates to :func:`relevance_scorer.series_text_for_relevance`."""
        return _rs_series_text_for_relevance(series)

    @staticmethod
    def _specialization_mismatch_penalty(query_text: str, candidate_text: str) -> float:
        """Delegates to :func:`relevance_scorer.specialization_mismatch_penalty`."""
        return _rs_specialization_mismatch_penalty(query_text, candidate_text)

    @staticmethod
    def _labor_rate_specificity_penalty(query_text: str, candidate_text: str) -> float:
        """Delegates to :func:`relevance_scorer.labor_rate_specificity_penalty`."""
        return _rs_labor_rate_specificity_penalty(query_text, candidate_text)

    def _score_series_relevance(self, query: str, series: Any) -> float:
        """Delegates to :func:`relevance_scorer.score_series_relevance`."""
        return _rs_score_series_relevance(query, series)

    def _rerank_data_by_query_relevance(self, query: str, data: List[Any]) -> List[Any]:
        """Delegates to :func:`relevance_scorer.rerank_data_by_query_relevance`."""
        return _rs_rerank_data_by_query_relevance(query, data)

    def _extract_ranking_value(
        self,
        series: NormalizedData,
        target_year: Optional[int],
    ) -> tuple[Optional[float], Optional[DataPoint]]:
        """Delegates to :func:`relevance_scorer.extract_ranking_value`."""
        return _rs_extract_ranking_value(series, target_year)

    def _apply_ranking_projection(self, query: str, data: List[NormalizedData]) -> List[NormalizedData]:
        """
        Transform ranking queries into sorted top-N datasets by latest/target-year value.

        This improves UX for prompts like:
        - "Rank top 10 economies by GDP growth in 2023"
        - "Which ASEAN country has the highest import share of GDP since 2015"
        """
        if not data or not self._is_ranking_query(query):
            return data

        target_year = self._extract_target_year_from_query(query)
        top_n = self._extract_top_n_from_query(query, default=10)
        query_lower = str(query or "").lower()
        descending = not any(term in query_lower for term in ("lowest", "smallest", "worst", "bottom"))

        ranking_rows: List[tuple[float, int, NormalizedData, DataPoint]] = []
        for index, series in enumerate(data):
            value, point = self._extract_ranking_value(series, target_year)
            if value is None or point is None:
                continue
            ranking_rows.append((value, index, series, point))

        if not ranking_rows:
            return data

        ranking_rows.sort(key=lambda item: (item[0], -item[1]), reverse=descending)
        selected_rows = ranking_rows[:top_n]

        projected: List[NormalizedData] = []
        for _value, _index, series, point in selected_rows:
            projected_series = series.model_copy(deep=True)
            projected_series.data = [point.model_copy(deep=True)]
            projected.append(projected_series)

        return projected or data

    async def _maybe_recover_from_empty_data(
        self,
        query: str,
        intent: Optional[ParsedIntent],
    ) -> Optional[List[NormalizedData]]:
        """
        Attempt semantic/ranking recovery when a primary fetch returns empty data.

        Recovery actions:
        - Distill noisy ranking/comparison phrasing to a stable metric phrase.
        - Expand region/group queries to explicit country lists.
        - Re-route provider for the recovered intent and retry once.
        """
        if not intent:
            return None

        params = dict(intent.parameters or {})
        if params.get("_semantic_recovery_attempted"):
            return None

        ranking_or_comparison = self._is_ranking_query(query) or self._is_comparison_query(query)
        distilled_indicator = self._build_distilled_indicator_query(query)
        if not ranking_or_comparison and not distilled_indicator:
            return None

        recovered_intent = intent.model_copy(deep=True)
        recovered_params = dict(recovered_intent.parameters or {})
        recovered_params["_semantic_recovery_attempted"] = True

        if distilled_indicator:
            recovered_intent.indicators = [distilled_indicator]
            recovered_params.pop("indicator", None)
            recovered_params.pop("seriesId", None)
            recovered_params.pop("series_id", None)
            recovered_params.pop("code", None)
            recovered_params["indicator"] = distilled_indicator

        if ranking_or_comparison:
            target_countries = self._collect_target_countries(recovered_params)
            if len(target_countries) < 2:
                expanded_regions = CountryResolver.expand_regions_in_query(query)
                explicit_countries = self._extract_countries_from_query(query)
                target_countries = explicit_countries or expanded_regions or target_countries
            if len(target_countries) < 2 and re.search(r"\b(economies|countries|nations)\b", str(query or "").lower()):
                target_countries = sorted(CountryResolver.G20_MEMBERS)
            if target_countries:
                recovered_params.pop("country", None)
                recovered_params["countries"] = list(dict.fromkeys([str(country) for country in target_countries if country]))

        recovered_intent.parameters = recovered_params

        try:
            rerouted_provider = await self._select_routed_provider(recovered_intent, distilled_indicator or query)
            recovered_intent.apiProvider = rerouted_provider
        except Exception as exc:
            logger.warning("Semantic recovery routing failed, keeping existing provider: %s", exc)

        try:
            recovered_data = await retry_async(
                lambda: self._fetch_data(recovered_intent),
                max_attempts=2,
                initial_delay=0.5,
            )
        except Exception as exc:
            logger.info("Semantic recovery fetch failed: %s", exc)
            return None

        if not recovered_data:
            return None

        recovered_data = self._rerank_data_by_query_relevance(query, recovered_data)
        if ranking_or_comparison:
            recovered_data = self._apply_ranking_projection(query, recovered_data)
        return recovered_data

    def _score_resolved_indicator_relevance(
        self, indicator_query: str, provider: str, resolved: Any,
    ) -> float:
        """Delegates to :func:`indicator_resolution.score_resolved_indicator_relevance`."""
        return _ir_score_resolved_indicator_relevance(self, indicator_query, provider, resolved)

    def _code_semantic_hint(self, provider: str, code: str) -> str:
        """Delegates to :func:`indicator_resolution.code_semantic_hint`."""
        return _ir_code_semantic_hint(provider, code)

    @staticmethod
    def _effective_original_query(intent) -> str:
        """Delegates to :func:`indicator_resolution._effective_original_query`."""
        return _ir_effective_original_query(intent)

    def _minimum_resolved_relevance_threshold(self, indicator_query: str) -> float:
        """Delegates to :func:`indicator_resolution.minimum_resolved_relevance_threshold`."""
        return _ir_minimum_resolved_relevance_threshold(indicator_query)

    @staticmethod
    def _is_placeholder_indicator_code(code: Optional[str]) -> bool:
        """Delegates to :func:`indicator_resolution.is_placeholder_indicator_code`."""
        return _ir_is_placeholder_indicator_code(code)

    def _format_indicator_option_name(
        self,
        provider: str,
        code: str,
        name: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Delegates to :func:`indicator_clarification.format_indicator_option_name`."""
        return _ic_format_indicator_option_name(self, provider, code, name, metadata)

    def _dedupe_indicator_choice_options(self, options: List[str]) -> List[str]:
        """Delegates to :func:`indicator_clarification.dedupe_indicator_choice_options`."""
        return _ic_dedupe_indicator_choice_options(self, options)

    @staticmethod
    def _parse_indicator_option(option: str) -> Optional[tuple[str, str]]:
        """Delegates to :func:`indicator_clarification.parse_indicator_option`."""
        return _ic_parse_indicator_option(option)

    def _store_pending_indicator_options(
        self,
        conversation_id: str,
        query: str,
        intent: ParsedIntent,
        options: List[str],
        question_lines: Optional[List[str]] = None,
    ) -> None:
        """Delegates to :func:`indicator_clarification.store_pending_indicator_options`."""
        return _ic_store_pending_indicator_options(self, conversation_id, query, intent, options, question_lines)

    def _store_pending_semantic_clarification(
        self,
        conversation_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """Delegates to :func:`indicator_clarification.store_pending_semantic_clarification`."""
        return _ic_store_pending_semantic_clarification(conversation_id, payload)

    def _build_clarification_options(
        self,
        options: Optional[List[str]],
    ) -> Optional[List[ClarificationOption]]:
        """Delegates to :func:`indicator_clarification.build_clarification_options`."""
        return _ic_build_clarification_options(self, options)

    @staticmethod
    def _indicator_option_label_key(option: str) -> Optional[str]:
        """Delegates to :func:`indicator_clarification.indicator_option_label_key`."""
        return _ic_indicator_option_label_key(option)

    def _has_materially_distinct_indicator_options(self, options: Optional[List[str]]) -> bool:
        """Delegates to :func:`indicator_clarification.has_materially_distinct_indicator_options`."""
        return _ic_has_materially_distinct_indicator_options(self, options)

    @staticmethod
    def _match_structured_clarification_option(
        user_query: str,
        options: List[ClarificationOption],
    ) -> Optional[ClarificationOption]:
        """Delegates to :func:`indicator_clarification.match_structured_clarification_option`."""
        return _ic_match_structured_clarification_option(user_query, options)

    def _match_indicator_choice_option(self, user_query: str, options: List[str]) -> Optional[str]:
        """Delegates to :func:`indicator_clarification.match_indicator_choice_option`."""
        return _ic_match_indicator_choice_option(user_query, options)

    async def _try_resolve_pending_indicator_choice(
        self,
        query: str,
        conversation_id: str,
        tracker: Optional['ProcessingTracker'] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.try_resolve_pending_indicator_choice`."""
        return await _ic_try_resolve_pending_indicator_choice(self, query, conversation_id, tracker)

    async def _maybe_recover_from_uncertain_match(
        self,
        query: str,
        intent: Optional[ParsedIntent],
        data: List[NormalizedData],
    ) -> Optional[List[NormalizedData]]:
        """Delegates to :func:`indicator_clarification.maybe_recover_from_uncertain_match`."""
        return await _ic_maybe_recover_from_uncertain_match(self, query, intent, data)

    def _provider_supports_country_for_options(self, provider: str, country_iso2: Optional[str]) -> bool:
        """Delegates to :func:`indicator_clarification.provider_supports_country_for_options`."""
        return _ic_provider_supports_country_for_options(provider, country_iso2)

    def _provider_covers_country_list(self, provider: str, countries: Optional[List[str]]) -> bool:
        """Check whether a provider can plausibly cover all requested countries.

        Delegates to :func:`provider_fallback.provider_covers_country_list`.
        """
        return _pf_provider_covers_country_list(provider, countries)

    def _provider_supports_requested_scope(
        self,
        provider: str,
        query: str,
        countries: Optional[List[str]],
    ) -> bool:
        """Delegates to :func:`indicator_clarification.provider_supports_requested_scope`."""
        return _ic_provider_supports_requested_scope(self, provider, query, countries)

    def _apply_indicator_option_to_intent(self, intent: ParsedIntent, option_text: str) -> bool:
        """Delegates to :func:`indicator_clarification.apply_indicator_option_to_intent`."""
        return _ic_apply_indicator_option_to_intent(intent, option_text)

    def _provider_can_execute_indicator_option(
        self,
        provider: str,
        code: str,
        option_name: Optional[str] = None,
    ) -> bool:
        """Delegates to :func:`indicator_clarification.provider_can_execute_indicator_option`."""
        return _ic_provider_can_execute_indicator_option(self, provider, code, option_name)

    def _build_no_reliable_indicator_match_response(
        self,
        conversation_id: str,
        intent: ParsedIntent,
        query: str,
        processing_steps: Optional[List[Any]] = None,
    ) -> QueryResponse:
        """Delegates to :func:`indicator_clarification.build_no_reliable_indicator_match_response`."""
        return _ic_build_no_reliable_indicator_match_response(conversation_id, intent, query, self, processing_steps)

    def _get_direct_provider_indicator_translation(
        self,
        provider: str,
        indicator_query: str,
    ) -> Optional[str]:
        """Delegates to :func:`indicator_clarification.get_direct_provider_indicator_translation`."""
        return _ic_get_direct_provider_indicator_translation(self, provider, indicator_query)

    def _collect_indicator_choice_options(
        self,
        query: str,
        intent: ParsedIntent,
        max_options: int = 3,
    ) -> List[str]:
        """Delegates to :func:`indicator_clarification.collect_indicator_choice_options`."""
        return _ic_collect_indicator_choice_options(self, query, intent, max_options)

    def _infer_query_concept_groups(self, query: str) -> set[str]:
        """Delegates to :func:`indicator_clarification.infer_query_concept_groups`."""
        return _ic_infer_query_concept_groups(query)

    def _build_multi_concept_query_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        is_multi_indicator: bool,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_multi_concept_query_clarification`."""
        return _ic_build_multi_concept_query_clarification(self, conversation_id, query, intent, is_multi_indicator, processing_steps)

    @staticmethod
    def _is_simple_single_country_query(query: str) -> bool:
        """Delegates to :func:`indicator_clarification.is_simple_single_country_query`."""
        return _ic_is_simple_single_country_query(query)

    @staticmethod
    def _looks_informational(query: str) -> bool:
        """Delegates to :func:`indicator_clarification.looks_informational`."""
        return _ic_looks_informational(query)

    def _handle_informational_intent(
        self,
        query: str,
        intent: ParsedIntent,
        conversation_id: str,
        tracker: Optional["ProcessingTracker"] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.handle_informational_intent`."""
        return _ic_handle_informational_intent(self, query, intent, conversation_id, tracker)

    def _format_informational_results(
        self,
        results: List[Dict[str, Any]],
        provider_filter: Optional[str],
        topic: Optional[str],
        original_query: str,
    ) -> str:
        """Delegates to :func:`indicator_clarification.format_informational_results`."""
        return _ic_format_informational_results(results, provider_filter, topic, original_query)

    def _verify_semantic_discriminators(
        self,
        original_query: str,
        code: str,
        series_name: str,
    ) -> bool:
        """Delegates to :func:`indicator_clarification.verify_semantic_discriminators`."""
        return _ic_verify_semantic_discriminators(original_query, code, series_name)

    @staticmethod
    def _humanize_region_name(region: str) -> str:
        """Delegates to :func:`indicator_clarification.humanize_region_name`."""
        return _ic_humanize_region_name(region)

    def _has_explicit_group_scope(self, query: str) -> bool:
        """Delegates to :func:`indicator_clarification.has_explicit_group_scope`."""
        return _ic_has_explicit_group_scope(self, query)

    def _rewrite_group_scope_query(
        self,
        query: str,
        region: str,
        scope: str,
    ) -> str:
        """Delegates to :func:`indicator_clarification.rewrite_group_scope_query`."""
        return _ic_rewrite_group_scope_query(query, region, scope)

    def _build_group_scope_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        is_multi_indicator: bool,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_group_scope_clarification`."""
        return _ic_build_group_scope_clarification(self, conversation_id, query, intent, is_multi_indicator, processing_steps)

    async def _filter_viable_indicator_choice_options(
        self,
        query: str,
        intent: ParsedIntent,
        options: List[str],
        max_options: int = 3,
    ) -> List[str]:
        """Delegates to :func:`indicator_clarification.filter_viable_indicator_choice_options`."""
        return await _ic_filter_viable_indicator_choice_options(self, query, intent, options, max_options)

    def _build_failed_indicator_choice_response(
        self,
        conversation_id: str,
        query: str,
        intent: ParsedIntent,
        options: List[str],
        selected_option: Optional[str],
        question_lines: Optional[List[str]],
        tracker: Optional['ProcessingTracker'] = None,
        error: Optional[str] = None,
    ) -> QueryResponse:
        """Delegates to :func:`indicator_clarification.build_failed_indicator_choice_response`."""
        return _ic_build_failed_indicator_choice_response(self, conversation_id, query, intent, options, selected_option, question_lines, tracker, error)

    async def _build_prefetch_indicator_choice_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        explicit_provider: Optional[str],
        is_multi_indicator: bool,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_prefetch_indicator_choice_clarification`."""
        return await _ic_build_prefetch_indicator_choice_clarification(self, conversation_id, query, intent, explicit_provider, is_multi_indicator, processing_steps)

    async def _build_post_parse_clarification(
        self,
        conversation_id: str,
        query: str,
        parse_result: ParseRouteResult,
        validation: ValidationResult,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_post_parse_clarification`."""
        return await _ic_build_post_parse_clarification(self, conversation_id, query, parse_result, validation, processing_steps)

    def _build_invalid_intent_response(
        self,
        conversation_id: str,
        intent: ParsedIntent,
        validation_error: Optional[str],
        suggestions: Optional[Dict[str, Any]],
        processing_steps: Optional[List[Any]] = None,
    ) -> QueryResponse:
        """Delegates to :func:`indicator_clarification.build_invalid_intent_response`."""
        return _ic_build_invalid_intent_response(conversation_id, intent, validation_error, suggestions, processing_steps)

    def _build_low_confidence_intent_response(
        self,
        conversation_id: str,
        intent: ParsedIntent,
        confidence_reason: Optional[str],
        processing_steps: Optional[List[Any]] = None,
    ) -> QueryResponse:
        """Delegates to :func:`indicator_clarification.build_low_confidence_intent_response`."""
        return _ic_build_low_confidence_intent_response(conversation_id, intent, confidence_reason, processing_steps)

    async def _execute_resolved_intent(
        self,
        query: str,
        conversation_id: str,
        intent: ParsedIntent,
        parse_result: ParseRouteResult,
        tracker: Optional['ProcessingTracker'] = None,
        skip_prefetch_clarification: bool = False,
    ) -> QueryResponse:
        """Run validation, clarification guardrails, and fetch for an already-built intent."""
        conv_id = conversation_manager.add_message_safe(conversation_id, "user", query, intent=intent)

        if intent.clarificationNeeded:
            conversation_manager.clear_pending_indicator_options(conv_id)
            conversation_manager.clear_pending_semantic_clarification(conv_id)
            return QueryResponse(
                conversationId=conv_id,
                intent=intent,
                clarificationNeeded=True,
                clarificationQuestions=intent.clarificationQuestions,
                processingSteps=tracker.to_list() if tracker else None,
            )

        if intent.needsDecomposition and intent.decompositionType == "provinces":
            intent.decompositionEntities = normalize_canadian_region_list(
                intent.decompositionEntities,
                fill_missing_territories=True
            )

        if intent.needsDecomposition and intent.decompositionEntities:
            if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
                logger.info("📅 Applying default time periods to decomposition query...")
                ParameterValidator.apply_default_time_periods(intent)

            logger.info("🔄 Query decomposition detected: %s %s into %d entities",
                       intent.decompositionType, query, len(intent.decompositionEntities))
            logger.info("🚀 Using batch method (Pro Mode disabled for decomposition)")

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
                processingSteps=tracker.to_list() if tracker else None,
            )

        logger.info("📅 Applying default time periods to prevent clarification requests...")
        ParameterValidator.apply_default_time_periods(intent)

        validation = self.pipeline.validate_intent(intent)
        if not validation.is_valid:
            logger.warning("Parameter validation failed: %s", validation.validation_error)
            return self._build_invalid_intent_response(
                conversation_id=conv_id,
                intent=intent,
                validation_error=validation.validation_error,
                suggestions=validation.suggestions,
                processing_steps=tracker.to_list() if tracker else None,
            )

        if not validation.is_confident:
            logger.warning("Low confidence in intent: %s", validation.confidence_reason)
            return self._build_low_confidence_intent_response(
                conversation_id=conv_id,
                intent=intent,
                confidence_reason=validation.confidence_reason,
                processing_steps=tracker.to_list() if tracker else None,
            )

        if validation.suggestions and validation.suggestions.get('warning'):
            logger.info("Validation warning: %s", validation.suggestions['warning'])

        if not skip_prefetch_clarification:
            parse_stage_clarification = await self._build_post_parse_clarification(
                conversation_id=conv_id,
                query=query,
                parse_result=parse_result,
                validation=validation,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if parse_stage_clarification:
                return parse_stage_clarification

        if validation.is_multi_indicator:
            logger.info("📊 Multi-indicator query detected: %s indicators", len(intent.indicators))
            data = await self._fetch_multi_indicator_data(intent)
        else:
            data = await retry_async(
                lambda: self._fetch_data(intent),
                max_attempts=3,
                initial_delay=1.0,
            )

        if not data or (isinstance(data, list) and len(data) == 0):
            logger.warning(f"No data returned from {intent.apiProvider} for query: {query}")

            try:
                logger.info("🔄 Empty result detected, attempting fallback providers...")
                fallback_data = await self._try_with_fallback(
                    intent,
                    DataNotAvailableError(
                        f"No data returned from {intent.apiProvider} for query: {query}"
                    ),
                )
                if fallback_data:
                    logger.info("✅ Fallback succeeded after empty primary response")
                    fallback_data = self._rerank_data_by_query_relevance(query, fallback_data)
                    fallback_data = self._apply_ranking_projection(query, fallback_data)
                    fallback_data, coverage_warning = await self._maybe_improve_country_coverage(
                        query,
                        intent,
                        fallback_data,
                    )
                    return QueryResponse(
                        conversationId=conv_id,
                        intent=intent,
                        data=fallback_data,
                        clarificationNeeded=False,
                        message=coverage_warning,
                        processingSteps=tracker.to_list() if tracker else None,
                    )
            except Exception as fallback_exc:
                logger.warning("Fallback after empty response failed: %s", fallback_exc)

            recovered_data = await self._maybe_recover_from_empty_data(query, intent)
            if recovered_data:
                logger.info("✅ Semantic recovery succeeded after empty primary response")
                recovered_data, coverage_warning = await self._maybe_improve_country_coverage(
                    query,
                    intent,
                    recovered_data,
                )
                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    data=recovered_data,
                    clarificationNeeded=False,
                    message=coverage_warning,
                    processingSteps=tracker.to_list() if tracker else None,
                )

            no_data_clarification = self._build_no_data_indicator_clarification(
                conversation_id=conv_id,
                query=query,
                intent=intent,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if no_data_clarification:
                return no_data_clarification

            provider_name = intent.apiProvider
            indicators = ", ".join(intent.indicators) if intent.indicators else "requested indicator"
            country = intent.parameters.get("country") or intent.parameters.get("countries", [""])[0] if intent.parameters else ""

            error_details = []
            error_details.append(f"No data found for **{indicators}**")
            if country:
                error_details.append(f"for **{country}**")
            error_details.append(f"from **{provider_name}**.")

            suggestions = self._get_no_data_suggestions(provider_name, intent)

            return QueryResponse(
                conversationId=conv_id,
                intent=intent,
                data=None,
                clarificationNeeded=False,
                error="no_data_found",
                message=f"⚠️ **No Data Available**\n\n{' '.join(error_details)}\n\n{suggestions}",
                processingSteps=tracker.to_list() if tracker else None,
            )

        data = self._rerank_data_by_query_relevance(query, data)
        data = self._apply_ranking_projection(query, data)
        recovered_uncertain_data = await self._maybe_recover_from_uncertain_match(
            query,
            intent,
            data,
        )
        if recovered_uncertain_data:
            data = recovered_uncertain_data
        data, coverage_warning = await self._maybe_improve_country_coverage(
            query,
            intent,
            data,
        )
        clarification_response = self._build_uncertain_result_clarification(
            conversation_id=conv_id,
            query=query,
            intent=intent,
            data=data,
            processing_steps=tracker.to_list() if tracker else None,
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
            message=coverage_warning,
            processingSteps=tracker.to_list() if tracker else None,
        )

    def _needs_indicator_clarification(
        self,
        query: str,
        data: List[Any],
        intent: Optional[ParsedIntent] = None,
    ) -> bool:
        """Delegates to :func:`indicator_clarification.needs_indicator_clarification`."""
        return _ic_needs_indicator_clarification(self, query, data, intent)

    def _build_uncertain_result_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        data: List[Any],
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_uncertain_result_clarification`."""
        return _ic_build_uncertain_result_clarification(self, conversation_id, query, intent, data, processing_steps)

    def _build_indicator_mismatch_hint(self, query: str, top_series: Any) -> Optional[str]:
        """Delegates to :func:`indicator_clarification.build_indicator_mismatch_hint`."""
        return _ic_build_indicator_mismatch_hint(query, top_series)

    def _build_no_data_indicator_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_no_data_indicator_clarification`."""
        return _ic_build_no_data_indicator_clarification(self, conversation_id, query, intent, processing_steps)

    def _looks_like_provider_indicator_code(self, provider: str, indicator: str) -> bool:
        """Delegates to :func:`indicator_clarification.looks_like_provider_indicator_code`."""
        return _ic_looks_like_provider_indicator_code(provider, indicator)

    def _is_resolved_indicator_plausible(
        self, provider: str, indicator_query: str,
        resolved_code: str, resolved_name: str = "",
    ) -> bool:
        """Delegates to :func:`indicator_resolution.is_resolved_indicator_plausible`."""
        return _ir_is_resolved_indicator_plausible(self, provider, indicator_query, resolved_code, resolved_name)

    def _extract_series_provider_and_code(self, series: Any) -> tuple[str, str]:
        """Delegates to :func:`indicator_resolution.extract_series_provider_and_code`."""
        return _ir_extract_series_provider_and_code(self, series)

    def _has_implausible_top_series(self, query: str, data: List[Any]) -> bool:
        """Delegates to :func:`indicator_resolution.has_implausible_top_series`."""
        return _ir_has_implausible_top_series(self, query, data)

    def _normalize_bis_metadata_labels(self, data: List[Any]) -> None:
        """Delegates to :func:`indicator_resolution.normalize_bis_metadata_labels`."""
        _ir_normalize_bis_metadata_labels(self, data)

    def _apply_concept_provider_override(
        self, provider: str, intent: ParsedIntent, params: dict,
    ) -> tuple[str, dict]:
        """Delegates to :func:`indicator_resolution.apply_concept_provider_override`."""
        return _ir_apply_concept_provider_override(self, provider, intent, params)

    def _indicator_resolution_threshold(self, indicator_query: str, resolved_source: str) -> float:
        """Delegates to :func:`indicator_resolution.indicator_resolution_threshold`."""
        return _ir_indicator_resolution_threshold(indicator_query, resolved_source)

    def _apply_catalog_availability_override(
        self, provider: str, intent: ParsedIntent, params: dict,
        fallback_excluded_providers: set,
    ) -> tuple[str, dict]:
        """Delegates to :func:`indicator_resolution.apply_catalog_availability_override`."""
        return _ir_apply_catalog_availability_override(self, provider, intent, params, fallback_excluded_providers)

    async def _resolve_indicator_for_fetch(
        self, provider: str, intent: ParsedIntent, params: dict,
    ) -> dict:
        """Delegates to :func:`indicator_resolution.resolve_indicator_for_fetch`."""
        return await _ir_resolve_indicator_for_fetch(
            self, provider, intent, params,
            _get_indicator_resolver=get_indicator_resolver,
        )

    def _select_indicator_query_for_resolution(self, intent: ParsedIntent) -> str:
        """Delegates to :func:`indicator_resolution.select_indicator_query_for_resolution`."""
        return _ir_select_indicator_query_for_resolution(self, intent)

    def _is_ranking_query(self, query: str) -> bool:
        """Delegates to :func:`indicator_resolution.is_ranking_query`."""
        return _ir_is_ranking_query(query)

    def _is_comparison_query(self, query: str) -> bool:
        """Delegates to :func:`indicator_resolution.is_comparison_query`."""
        return _ir_is_comparison_query(query)

    def _is_temporal_split_query(self, query: str) -> bool:
        """Delegates to :func:`indicator_resolution.is_temporal_split_query`."""
        return _ir_is_temporal_split_query(query)

    def _extract_top_n_from_query(self, query: str, default: int = 10) -> int:
        """Delegates to :func:`indicator_resolution.extract_top_n_from_query`."""
        return _ir_extract_top_n_from_query(query, default)

    def _extract_target_year_from_query(self, query: str) -> Optional[int]:
        """Delegates to :func:`indicator_resolution.extract_target_year_from_query`."""
        return _ir_extract_target_year_from_query(query)

    def _build_distilled_indicator_query(self, query: str) -> str:
        """Delegates to :func:`indicator_resolution.build_distilled_indicator_query`."""
        return _ir_build_distilled_indicator_query(self, query)

    def _infer_multi_concept_indicators_from_query(self, query: str) -> List[str]:
        """Infer explicit indicator list for comparison queries spanning concept families."""
        query_lower = str(query or "").lower()
        cues = self._extract_indicator_cues(query_lower)
        inferred: List[str] = []

        if "employment_population" in cues:
            inferred.append("employment to population ratio")
        elif "employment_rate" in cues:
            inferred.append("employment rate")
        elif "unemployment" in cues:
            inferred.append("unemployment rate")

        if "producer_price" in cues:
            inferred.append("producer price inflation")
        elif "inflation" in cues:
            inferred.append("HICP inflation" if "hicp" in query_lower else "inflation rate")

        if "debt_service" in cues:
            inferred.append("debt service ratio")
        elif "debt_gdp_ratio" in cues or "public_debt" in cues:
            inferred.append("government debt (% of GDP)")
        elif "credit" in cues:
            inferred.append("private sector credit to GDP")

        if "policy_rate" in cues:
            inferred.append("policy rate")
        elif "bond_yield" in cues:
            inferred.append("long-term interest rate")

        if "money_supply" in cues:
            inferred.append("money supply")

        if "reserves" in cues:
            inferred.append("foreign exchange reserves")
        elif "current_account" in cues:
            inferred.append("current account balance (% of GDP)")
        elif "real_effective_exchange_rate" in cues:
            inferred.append("real effective exchange rate")
        elif "exchange_rate" in cues:
            inferred.append("real effective exchange rate" if "reer" in query_lower else "exchange rate")

        if "trade_balance" in cues:
            inferred.append("trade balance (% of GDP)" if "gdp" in query_lower else "trade balance")
        elif "import" in cues:
            inferred.append("imports as % of GDP" if "gdp" in query_lower else "imports")
        elif "export" in cues:
            inferred.append("exports as % of GDP" if "gdp" in query_lower else "exports")

        # Preserve order and uniqueness.
        return list(dict.fromkeys([item for item in inferred if item]))

    def _maybe_expand_multi_concept_intent(self, query: str, intent: ParsedIntent) -> bool:
        """
        Auto-expand clearly comparative multi-concept queries into multi-indicator intent.

        This reduces unnecessary clarification loops for queries like
        "compare unemployment and inflation for G7 countries".
        """
        if not intent:
            return False
        if intent.indicators and len(intent.indicators) > 1:
            return False
        if not (self._is_comparison_query(query) or self._is_ranking_query(query)):
            return False

        inferred_indicators = self._infer_multi_concept_indicators_from_query(query)
        if len(inferred_indicators) < 2:
            return False

        target_countries = self._collect_target_countries(intent.parameters)
        if len(target_countries) < 2:
            extracted = self._extract_countries_from_query(query)
            expanded = CountryResolver.expand_regions_in_query(query)
            target_countries = extracted or expanded or target_countries
        if len(target_countries) < 2:
            return False

        params = dict(intent.parameters or {})
        params.pop("country", None)
        params["countries"] = list(dict.fromkeys([str(country) for country in target_countries if country]))
        params.pop("indicator", None)
        params.pop("seriesId", None)
        params.pop("series_id", None)
        params.pop("code", None)

        intent.parameters = params
        intent.indicators = inferred_indicators
        intent.clarificationNeeded = False
        intent.clarificationQuestions = []

        logger.info(
            "🧩 Auto-expanded multi-concept comparison query into indicators=%s countries=%s",
            inferred_indicators,
            params.get("countries"),
        )
        return True

    def _maybe_expand_ranking_country_scope(
        self,
        query: str,
        provider: str,
        params: dict,
    ) -> dict:
        """
        Expand country scope for ranking queries that request top/highest/lowest
        results without enough country context.

        This keeps ranking in deterministic retrieval mode while avoiding single-
        country defaults for broad ranking prompts.
        """
        if not params:
            params = {}

        query_text = str(query or "").strip()
        if not query_text or not self._is_ranking_query(query_text):
            return params
        if params.get("_ranking_scope_expanded"):
            return params

        existing_targets = self._collect_target_countries(params)
        if len(existing_targets) >= 2:
            return params

        expanded_countries: List[str] = []
        query_lower = query_text.lower()

        if len(existing_targets) == 1:
            region_expansion = CountryResolver.expand_region(existing_targets[0])
            if region_expansion and len(region_expansion) >= 2:
                expanded_countries = region_expansion
            else:
                return params
        else:
            expanded_countries = CountryResolver.expand_regions_in_query(query_text)
            if len(expanded_countries) < 2:
                if re.search(r"\b(economy|economies|countries|nations)\b", query_lower):
                    expanded_countries = sorted(CountryResolver.G20_MEMBERS)

        if len(expanded_countries) < 2:
            return params

        normalized_provider = normalize_provider_name(provider)
        if normalized_provider == "EUROSTAT":
            expanded_countries = [
                country for country in expanded_countries
                if CountryResolver.is_eu_member(country)
            ]

        if len(expanded_countries) < 2:
            return params

        updated = dict(params)
        updated.pop("country", None)
        updated["countries"] = list(dict.fromkeys([str(country) for country in expanded_countries if country]))
        updated["_ranking_scope_expanded"] = True

        logger.info(
            "📈 Expanded ranking scope to %d countries for provider %s",
            len(updated.get("countries", [])),
            normalized_provider or provider,
        )
        return updated

    def _maybe_resolve_region_clarification(self, query: str, intent: ParsedIntent) -> bool:
        """
        Resolve parser-issued geography clarification when query already names known regions.

        Example:
        - "energy importers versus exporters" -> expand both groups to countries
        """
        if not intent or not intent.clarificationNeeded:
            return False

        expanded_countries = CountryResolver.expand_regions_in_query(query)
        if len(expanded_countries) < 2:
            return False

        params = dict(intent.parameters or {})
        params.pop("country", None)
        params["countries"] = expanded_countries
        intent.parameters = params

        if not intent.indicators:
            distilled = self._build_distilled_indicator_query(query)
            if distilled:
                intent.indicators = [distilled]
        else:
            query_cues = self._extract_indicator_cues(query)
            if "current_account" in query_cues:
                intent.indicators = ["current account balance (% of GDP)"]
                params.pop("indicator", None)
                intent.parameters = params

        intent.clarificationNeeded = False
        intent.clarificationQuestions = []

        logger.info(
            "🌍 Resolved region-based clarification using expanded countries: %s",
            expanded_countries,
        )
        return True

    def _maybe_resolve_temporal_comparison_clarification(self, query: str, intent: ParsedIntent) -> bool:
        """
        Resolve parser-issued temporal split clarifications for before/after queries.

        Example:
        - "contrast trade balances before and after 2018"
        """
        if not intent or not intent.clarificationNeeded:
            return False

        query_text = str(query or "").strip()
        query_lower = query_text.lower()
        if "before" not in query_lower or "after" not in query_lower:
            return False

        years = [int(m) for m in _YEAR_RE.findall(query_lower)]
        if not years:
            return False
        split_year = max(years)

        clarification_blob = " ".join(str(item) for item in (intent.clarificationQuestions or [])).lower()
        if clarification_blob and not any(
            token in clarification_blob
            for token in ("before", "after", "period", "time range", "include the year", "from")
        ):
            return False

        params = dict(intent.parameters or {})
        if not params.get("startDate"):
            params["startDate"] = f"{max(1960, split_year - 10)}-01-01"
        if not params.get("endDate"):
            from datetime import datetime

            params["endDate"] = f"{max(split_year + 1, datetime.now().year)}-12-31"
        params["comparisonSplitYear"] = split_year
        intent.parameters = params

        distilled = self._build_distilled_indicator_query(query_text)
        if distilled:
            intent.indicators = [distilled]

        intent.clarificationNeeded = False
        intent.clarificationQuestions = []

        logger.info(
            "🕒 Resolved temporal comparison clarification using split year %s (%s to %s)",
            split_year,
            params.get("startDate"),
            params.get("endDate"),
        )
        return True

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
        to_match = _CURRENCY_TO_RE.search(query_text)
        if to_match:
            base_currency = to_match.group(1)
            target_currency = to_match.group(2)
            logger.info(f"💱 Extracted from 'X to Y' pattern: {base_currency} -> {target_currency}")

        # Pattern 2: "X/Y" or "X-Y" (e.g., "USD/EUR", "EUR-GBP")
        if not base_currency or not target_currency:
            slash_match = _CURRENCY_SLASH_RE.search(query_text)
            if slash_match:
                base_currency = slash_match.group(1)
                target_currency = slash_match.group(2)
                logger.info(f"💱 Extracted from 'X/Y' pattern: {base_currency} -> {target_currency}")

        # Pattern 3: "X vs Y" (e.g., "USD vs EUR")
        if not base_currency or not target_currency:
            vs_match = _CURRENCY_VS_RE.search(query_text)
            if vs_match:
                base_currency = vs_match.group(1)
                target_currency = vs_match.group(2)
                logger.info(f"💱 Extracted from 'X vs Y' pattern: {base_currency} -> {target_currency}")

        # Pattern 4: Try to find any currency codes in the query
        if not base_currency or not target_currency:
            # Look for 3-letter currency codes
            all_codes = _CURRENCY_CODE_RE.findall(query_text)
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

    async def _get_stale_from_cache(self, provider: str, params: dict):
        """Get stale (expired) cached data as fallback when provider is down.

        Returns data even if TTL has expired — a 1-hour-old GDP dataset
        is better than 'No Data Available' during a transient API outage.
        """
        cache_params = self._build_cache_params(provider, params)
        stale = cache_service.get_data_stale(provider, cache_params)
        if stale:
            logger.info(f"📦 Serving STALE cache for {provider} (provider may be down)")
        return stale

    async def _save_to_cache(self, provider: str, params: dict, data: list):
        """
        Save data to both Redis and in-memory cache.

        Never caches empty results — prevents cache-poisoning from
        transient API outages (e.g., WorldBank 502 caches empty response,
        then serves it even after the API recovers).

        Args:
            provider: Data provider name
            params: Query parameters
            data: Data to cache
        """
        if not data:
            logger.debug(f"Skipping cache save — empty data for {provider}")
            return
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
        """Normalize country identifiers/names to ISO2 codes when possible.

        Delegates to :func:`provider_fallback.normalize_country_to_iso2`.
        """
        return _pf_normalize_country_to_iso2(country)

    # ------------------------------------------------------------------
    # Pre-flight geographic split
    # ------------------------------------------------------------------
    # Providers like FRED (US-only), StatsCan (Canada-only), and Eurostat
    # (EU-only) cannot serve data for countries outside their scope.  When
    # a query targets multiple countries that span provider boundaries
    # (e.g. "PPI for US and Germany"), a single-provider fetch will either
    # fail or return partial data.
    #
    # This pre-flight check detects that situation *before* the fetch,
    # splits the query into per-provider sub-queries, fetches in parallel,
    # and merges the results.  It is a framework-level fix that benefits
    # every multi-country query — not a query-specific patch.

    # Map from country-specific provider to the ISO2 codes it covers.
    # "None" means global (covers everything) — used as a sentinel.
    _PROVIDER_GEO_SCOPE: Dict[str, Optional[set]] = {
        "FRED": {"US"},
        "STATSCAN": {"CA"},
        # Eurostat and OECD are handled dynamically via CountryResolver
    }

    def _get_provider_for_single_country(
        self,
        iso2: str,
        concept_query: str,
        original_provider: str,
    ) -> Tuple[str, Optional[str]]:
        """Return (provider, indicator_code) best suited for *one* country.

        Uses the catalog to find the best provider that covers the given
        country, falling back to the original provider when the catalog
        has no opinion.
        """
        from .catalog_service import find_concept_by_term, get_best_provider

        concept = find_concept_by_term(concept_query)
        if concept:
            prov, code, conf = get_best_provider(concept, countries=[iso2])
            if prov and conf > 0.0:
                return normalize_provider_name(prov), code

        # If the original provider actually covers this country, keep it.
        if self._provider_covers_country_list(original_provider, [iso2]):
            return original_provider, None

        # Last resort: WorldBank has global coverage.
        return "WORLDBANK", None

    async def _preflight_geographic_split(
        self,
        intent: ParsedIntent,
    ) -> Optional[List[NormalizedData]]:
        """Split a multi-country query across providers when no single provider covers all.

        Returns None when splitting is unnecessary (single country, or the
        current provider already covers everything).  Otherwise returns the
        merged result list.
        """
        params = intent.parameters or {}
        provider = normalize_provider_name(intent.apiProvider)

        # Collect all target countries as ISO2 codes.
        raw_countries = self._collect_target_countries(params)
        if len(raw_countries) < 2:
            return None  # Nothing to split

        iso2_map: "OrderedDict[str, str]" = OrderedDict()
        for raw in raw_countries:
            iso2 = self._normalize_country_to_iso2(raw)
            if iso2:
                iso2_map.setdefault(iso2, raw)
        if len(iso2_map) < 2:
            return None

        # If the current provider already covers all countries, no split needed.
        if self._provider_covers_country_list(provider, list(iso2_map.keys())):
            return None

        # Determine best provider for each country.
        concept_query = self._select_indicator_query_for_resolution(intent)
        if not concept_query:
            concept_query = " ".join(str(ind) for ind in intent.indicators if ind)

        # Group countries by their best provider.
        provider_groups: Dict[str, List[str]] = {}  # provider -> [iso2, ...]
        provider_codes: Dict[str, Optional[str]] = {}  # provider -> indicator code (if catalog knows)
        for iso2 in iso2_map:
            best_prov, best_code = self._get_provider_for_single_country(
                iso2, concept_query, provider,
            )
            provider_groups.setdefault(best_prov, []).append(iso2)
            # Keep the first code suggestion per provider.
            if best_prov not in provider_codes:
                provider_codes[best_prov] = best_code

        # If everything landed on the same provider, no split needed.
        if len(provider_groups) == 1:
            return None

        logger.info(
            "🌐 Geographic pre-flight split: query '%s' → %s",
            intent.originalQuery or concept_query,
            {prov: countries for prov, countries in provider_groups.items()},
        )

        # Build and execute per-provider sub-intents in parallel.
        async def _fetch_for_group(group_provider: str, group_iso2s: List[str]) -> List[NormalizedData]:
            sub_params = dict(params)
            # Replace multi-country params with the group's countries.
            sub_params.pop("country", None)
            if len(group_iso2s) == 1:
                sub_params["country"] = group_iso2s[0]
                sub_params.pop("countries", None)
            else:
                sub_params["countries"] = group_iso2s
                sub_params.pop("country", None)

            # Use catalog-resolved code for this provider when available.
            catalog_code = provider_codes.get(group_provider)
            if catalog_code:
                sub_params["indicator"] = catalog_code
            else:
                # Remove prior provider-specific indicator so resolver picks fresh.
                sub_params.pop("indicator", None)
                sub_params.pop("seriesId", None)
                sub_params.pop("series_id", None)
                sub_params.pop("code", None)

            # Recursion guard: mark this sub-intent so _fetch_data doesn't
            # attempt another geographic split on it.
            sub_params["__geo_split_child"] = True

            sub_intent = ParsedIntent(
                apiProvider=group_provider,
                indicators=[catalog_code] if catalog_code else list(intent.indicators or []),
                parameters=sub_params,
                clarificationNeeded=False,
                originalQuery=intent.originalQuery,
            )

            try:
                return await self._fetch_data(sub_intent)
            except Exception as exc:
                logger.warning(
                    "🌐 Geographic split: fetch from %s for %s failed: %s",
                    group_provider, group_iso2s, exc,
                )
                return []

        tasks = [
            _fetch_for_group(prov, countries)
            for prov, countries in provider_groups.items()
        ]
        results = await asyncio.gather(*tasks)

        merged: List[NormalizedData] = []
        for result_list in results:
            if result_list:
                merged.extend(result_list)

        if not merged:
            return None  # All sub-fetches failed; let caller try fallback

        logger.info(
            "🌐 Geographic split: merged %d series from %d providers",
            len(merged), len(provider_groups),
        )
        return merged

    def _assess_country_coverage(
        self,
        intent: Optional[ParsedIntent],
        data: Optional[List[NormalizedData]],
    ) -> Optional[Dict[str, Any]]:
        """
        Assess whether a multi-country request is fully represented in result data.

        Returns None when coverage checks do not apply (for example single-country
        queries), otherwise returns a dict with coverage details.
        """
        if not intent or not data:
            return None

        requested_countries = self._collect_target_countries(intent.parameters)
        if len(requested_countries) < 2:
            return None

        requested_map: "OrderedDict[str, str]" = OrderedDict()
        for raw_country in requested_countries:
            normalized_iso2 = self._normalize_country_to_iso2(raw_country)
            if not normalized_iso2:
                continue
            requested_map.setdefault(normalized_iso2, str(raw_country))

        if len(requested_map) < 2:
            return None

        returned_map: "OrderedDict[str, str]" = OrderedDict()
        for series in data:
            metadata = getattr(series, "metadata", None) if series is not None else None
            if not metadata:
                continue
            result_country = getattr(metadata, "country", None)
            normalized_iso2 = self._normalize_country_to_iso2(result_country)
            if not normalized_iso2:
                continue
            returned_map.setdefault(normalized_iso2, str(result_country))

        missing_iso2 = [iso2 for iso2 in requested_map.keys() if iso2 not in returned_map]
        covered_iso2 = [iso2 for iso2 in requested_map.keys() if iso2 in returned_map]
        coverage_ratio = len(covered_iso2) / max(len(requested_map), 1)

        return {
            "requested_iso2": list(requested_map.keys()),
            "requested_display": list(requested_map.values()),
            "returned_iso2": list(returned_map.keys()),
            "returned_display": list(returned_map.values()),
            "missing_iso2": missing_iso2,
            "missing_display": [requested_map[iso2] for iso2 in missing_iso2],
            "covered_count": len(covered_iso2),
            "requested_count": len(requested_map),
            "coverage_ratio": coverage_ratio,
            "complete": len(missing_iso2) == 0,
        }

    def _build_country_coverage_warning_message(
        self,
        coverage: Dict[str, Any],
    ) -> str:
        """Create a concise user-facing warning for partial multi-country coverage."""
        missing_display = [str(item) for item in (coverage.get("missing_display") or []) if item]
        returned_display = [str(item) for item in (coverage.get("returned_display") or []) if item]

        if missing_display:
            missing_text = ", ".join(missing_display)
            if returned_display:
                available_text = ", ".join(returned_display)
                return (
                    "Data is only available for a subset of requested countries. "
                    f"Missing: {missing_text}. Available: {available_text}."
                )
            return (
                "Data is only available for a subset of requested countries. "
                f"Missing: {missing_text}."
            )

        return ""

    async def _maybe_improve_country_coverage(
        self,
        query: str,
        intent: Optional[ParsedIntent],
        data: Optional[List[NormalizedData]],
    ) -> tuple[List[NormalizedData], Optional[str]]:
        """
        Try to improve multi-country coverage via fallback providers, then return
        data plus optional warning when coverage remains partial.
        """
        current_data = list(data or [])
        if not intent or not current_data:
            return current_data, None

        initial_coverage = self._assess_country_coverage(intent, current_data)
        if not initial_coverage or initial_coverage.get("complete"):
            return current_data, None

        logger.warning(
            "Partial country coverage detected for query '%s': covered=%s/%s missing=%s",
            query,
            initial_coverage.get("covered_count"),
            initial_coverage.get("requested_count"),
            initial_coverage.get("missing_display"),
        )

        best_data = current_data
        best_coverage = initial_coverage

        try:
            fallback_data = await self._try_with_fallback(
                intent,
                DataNotAvailableError("Partial multi-country coverage from primary provider"),
            )
        except Exception as exc:
            logger.info("Coverage fallback attempt failed: %s", exc)
            fallback_data = None

        if fallback_data:
            fallback_data = self._rerank_data_by_query_relevance(query, fallback_data)
            fallback_data = self._apply_ranking_projection(query, fallback_data)
            fallback_coverage = self._assess_country_coverage(intent, fallback_data)
            if fallback_coverage:
                fallback_score = (
                    float(fallback_coverage.get("coverage_ratio", 0.0)),
                    int(fallback_coverage.get("covered_count", 0)),
                )
                best_score = (
                    float(best_coverage.get("coverage_ratio", 0.0)),
                    int(best_coverage.get("covered_count", 0)),
                )
                if fallback_score > best_score:
                    best_data = fallback_data
                    best_coverage = fallback_coverage
                    logger.info(
                        "Coverage fallback improved country coverage to %s/%s",
                        best_coverage.get("covered_count"),
                        best_coverage.get("requested_count"),
                    )
            elif fallback_data:
                # If fallback cannot be evaluated but has payload, keep original best.
                logger.debug("Coverage fallback returned data without country labels; keeping primary result")

        if best_coverage.get("complete"):
            return best_data, None

        warning_message = self._build_country_coverage_warning_message(best_coverage)
        return best_data, warning_message or None

    def _get_fallback_providers(
        self,
        primary_provider: str,
        indicator: Optional[str] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
    ) -> List[str]:
        """Get ordered list of fallback providers for a given primary provider.

        Delegates to :func:`provider_fallback.get_fallback_providers`.
        """
        return _pf_get_fallback_providers(
            primary_provider,
            self.unified_router,
            self._fallback_provider_cache,
            indicator=indicator,
            country=country,
            countries=countries,
            max_cache_entries=self.MAX_FALLBACK_CACHE_ENTRIES,
        )

    def _get_no_data_suggestions(self, provider: str, intent: ParsedIntent) -> str:
        """Generate helpful suggestions when no data is found.

        Delegates to :func:`provider_fallback.get_no_data_suggestions`.
        """
        return _pf_get_no_data_suggestions(
            provider,
            intent,
            fallback_providers_fn=self._get_fallback_providers,
        )

    def _is_fallback_relevant(
        self,
        original_indicators: List[str],
        fallback_result: List[NormalizedData],
        target_countries: Optional[List[str]] = None,
        original_query: Optional[str] = None,
    ) -> bool:
        """Check if fallback result is semantically related to the original query.

        Delegates to :func:`provider_fallback.is_fallback_relevant`.
        """
        return _pf_is_fallback_relevant(
            original_indicators, fallback_result, target_countries, original_query,
        )

    def _resolve_concept_for_fallback(
        self,
        intent: ParsedIntent,
        primary_provider: str,
    ) -> Optional[str]:
        """Resolve a catalog concept name from the intent for cross-provider fallback.

        Checks (in order):
        1. Stored ``__catalog_concept`` parameter (set during catalog resolution)
        2. Reverse lookup from provider-specific indicator code via catalog
        3. Forward lookup from the original query text via ``find_concept_by_term``

        Returns:
            Catalog concept name (e.g., ``"exports_pct_gdp"``) or ``None``.
        """
        try:
            from .catalog_service import find_concept_by_term, find_concepts_by_code

            # 1. Check stored concept from prior catalog resolution
            stored_concept = (intent.parameters or {}).get("__catalog_concept")
            if stored_concept:
                logger.debug("Fallback concept from __catalog_concept: %s", stored_concept)
                return str(stored_concept)

            # 2. Reverse lookup: provider code -> concept
            for ind in (intent.indicators or []):
                ind_str = str(ind or "").strip()
                if ind_str and self._looks_like_provider_indicator_code(primary_provider, ind_str):
                    concepts = find_concepts_by_code(primary_provider, ind_str)
                    if concepts:
                        logger.debug(
                            "Fallback concept via reverse code lookup (%s/%s): %s",
                            primary_provider, ind_str, concepts[0],
                        )
                        return concepts[0]

            # Also check the 'indicator' parameter which may hold the resolved code
            param_indicator = str((intent.parameters or {}).get("indicator", "")).strip()
            if param_indicator and self._looks_like_provider_indicator_code(primary_provider, param_indicator):
                concepts = find_concepts_by_code(primary_provider, param_indicator)
                if concepts:
                    logger.debug(
                        "Fallback concept via param indicator reverse lookup (%s/%s): %s",
                        primary_provider, param_indicator, concepts[0],
                    )
                    return concepts[0]

            # 3. Forward lookup: original query text -> concept
            original_query = str(intent.originalQuery or "").strip()
            if original_query:
                concept = find_concept_by_term(original_query)
                if concept:
                    logger.debug("Fallback concept via query text: %s", concept)
                    return concept

                # Try distilled query
                distilled = self._build_distilled_indicator_query(original_query)
                if distilled:
                    concept = find_concept_by_term(distilled)
                    if concept:
                        logger.debug("Fallback concept via distilled query: %s", concept)
                        return concept

        except Exception as exc:
            logger.debug("Concept resolution for fallback failed: %s", exc)

        return None

    def _resolve_indicator_for_fallback_provider(
        self,
        concept_name: Optional[str],
        fallback_provider: str,
        semantic_query: str,
        countries: Optional[list],
    ) -> list[str]:
        """Resolve the indicator for a specific fallback provider.

        Uses the catalog concept to get the correct provider-specific code.
        Falls back to the semantic query string when catalog lookup fails.

        Args:
            concept_name: Catalog concept (e.g., ``"exports_pct_gdp"``).
            fallback_provider: Target provider name.
            semantic_query: Human-readable indicator phrase for fallback.
            countries: Country context for coverage checks.

        Returns:
            List with one indicator string for the fallback provider.
        """
        if concept_name:
            try:
                from .catalog_service import get_best_provider

                provider_name, code, confidence = get_best_provider(
                    concept_name,
                    countries,
                    preferred_provider=fallback_provider,
                )
                provider_norm = normalize_provider_name(provider_name or "")
                if provider_norm == fallback_provider and code and confidence >= 0.5:
                    logger.info(
                        "📋 Fallback indicator resolved via catalog: concept='%s' -> %s/%s (conf=%.2f)",
                        concept_name, fallback_provider, code, confidence,
                    )
                    return [code]
            except Exception as exc:
                logger.debug("Catalog fallback indicator resolution failed: %s", exc)

        # Fall back to semantic query string
        if semantic_query:
            return [semantic_query]
        return []

    async def _try_with_fallback(self, intent: ParsedIntent, primary_error: Exception):
        """
        Try to fetch data from fallback providers when primary fails.

        Uses concept names (not provider-specific codes) for cross-provider
        indicator resolution. When falling back from provider A to provider B:
        1. Resolve the catalog concept from the original query/indicator
        2. Look up the correct indicator code for provider B via catalog
        3. Fall back to human-readable query text if catalog lookup fails
        4. NEVER pass provider A's codes (e.g., NE.EXP.GNFS.ZS) to provider B

        Args:
            intent: The parsed intent
            primary_error: The error from the primary provider

        Returns:
            Data from fallback provider

        Raises:
            Original error if all fallbacks fail
        """
        primary_provider = normalize_provider_name(intent.apiProvider)

        # Resolve the concept name for cross-provider fallback.
        concept_name = self._resolve_concept_for_fallback(intent, primary_provider)

        # Use semantic indicator query (or original query) for smarter fallbacks.
        # This is the human-readable phrase, never a provider-specific code.
        indicator = self._select_indicator_query_for_resolution(intent)
        if not indicator:
            indicator = self._effective_original_query(intent) or (
                intent.indicators[0] if intent.indicators else None
            )
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

        logger.info(
            "🔄 Cross-provider fallback: concept=%s, semantic_query='%s', providers=%s",
            concept_name, indicator, fallback_providers,
        )

        last_error = primary_error
        for fallback_provider in fallback_providers:
            logger.warning(f"Attempting fallback from {primary_provider} to {fallback_provider}")

            fallback_params = dict(intent.parameters or {})
            fallback_params["__fallback_excluded_providers"] = [primary_provider]
            # Remove provider-specific resolved indicator identifiers so fallback
            # providers can resolve indicator codes in their own namespace.
            fallback_params.pop("indicator", None)
            fallback_params.pop("seriesId", None)
            fallback_params.pop("series_id", None)
            fallback_params.pop("code", None)
            # Remove stale catalog state from the primary provider
            fallback_params.pop("__catalog_resolved", None)
            fallback_params.pop("__catalog_concept", None)

            # Resolve indicator for THIS specific fallback provider.
            # Uses catalog concept -> provider-specific code when available,
            # falls back to semantic query string otherwise.
            fallback_indicators = self._resolve_indicator_for_fallback_provider(
                concept_name,
                fallback_provider,
                indicator or "",
                target_countries,
            )
            if not fallback_indicators:
                # Last resort: use the semantic indicator query
                fallback_indicator_query = self._select_indicator_query_for_resolution(intent)
                if fallback_indicator_query:
                    fallback_indicators = [fallback_indicator_query]
                elif intent.indicators:
                    # Only use original indicators if they are NOT provider-specific codes
                    safe_indicators = [
                        ind for ind in intent.indicators
                        if not self._looks_like_provider_indicator_code(primary_provider, str(ind or ""))
                    ]
                    fallback_indicators = safe_indicators or [self._effective_original_query(intent) or indicator or ""]

            # Create a modified intent for the fallback provider.
            # For follow-ups, propagate resolvedQuery and isFollowUp so that
            # indicator resolution in the fallback path uses the resolved query
            # (e.g. "GDP per capita India") instead of the raw follow-up text.
            effective_oq = self._effective_original_query(intent)
            fallback_intent = ParsedIntent(
                apiProvider=fallback_provider,
                indicators=fallback_indicators,
                parameters=fallback_params,
                clarificationNeeded=False,
                originalQuery=effective_oq or intent.originalQuery,
                isFollowUp=intent.isFollowUp,
                followUpType=intent.followUpType,
                resolvedQuery=intent.resolvedQuery,
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
        auto_pro_mode: bool = False,
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
            history = conversation_manager.get_history(conv_id)

            # ── Pending indicator choice (numeric "1", "2" responses) ───
            # Keep this for structural resolution — no LLM needed for "pick option 2".
            pending_choice_response = await self._try_resolve_pending_indicator_choice(
                query=query,
                conversation_id=conv_id,
                tracker=tracker,
            )
            if pending_choice_response is not None:
                return pending_choice_response

            # ── Phase 4: LLM-based clarification resolution ───────────
            # Semantic clarifications and country follow-ups are now
            # handled by the LLM via enhanced conversation context.

            contextual_follow_up = self._build_intent_from_contextual_follow_up(
                query=query,
                conversation_id=conv_id,
            )
            if contextual_follow_up is not None:
                refined_query, contextual_intent, contextual_parse_result = contextual_follow_up
                return await self._execute_resolved_intent(
                    query=refined_query,
                    skip_prefetch_clarification=True,  # Trust contextual follow-up — intent was verified against prior query
                    conversation_id=conv_id,
                    intent=contextual_intent,
                    parse_result=contextual_parse_result,
                    tracker=tracker,
                )

            # Check for indicator-switch follow-ups ("what about inflation",
            # "show unemployment instead") — keeps country, changes metric.
            indicator_switch = self._build_intent_from_indicator_switch(
                query=query,
                conversation_id=conv_id,
            )
            if indicator_switch is not None:
                refined_query, switch_intent, switch_parse_result = indicator_switch
                return await self._execute_resolved_intent(
                    query=refined_query,
                    skip_prefetch_clarification=True,
                    conversation_id=conv_id,
                    intent=switch_intent,
                    parse_result=switch_parse_result,
                    tracker=tracker,
                )

            # CONSOLIDATED: Semantic ambiguity and group scope checks now run
            # ONLY after LLM parse (in _build_post_parse_clarification) where
            # they have full intent context.  Pre-parse checks with intent=None
            # were redundant and less accurate.

            # Check if LangChain orchestrator should be used
            from ..config import get_settings
            settings = get_settings()
            bypass_orchestrator = self._is_temporal_split_query(query)
            # Also bypass orchestrator for queries that look informational —
            # the orchestrator doesn't handle metadata queries.  Let them
            # flow to the LLM parse step where queryType is classified.
            if not bypass_orchestrator and self._looks_informational(query):
                bypass_orchestrator = True
                logger.info("⏭️ Bypassing orchestrator for possible informational query")
            # Simple single-country macro queries work better through the
            # deterministic pipeline with the UnifiedRouter, which has
            # reliable fallback logic.  The orchestrator is better for
            # complex/multi-step queries.
            if not bypass_orchestrator and self._is_simple_single_country_query(query):
                bypass_orchestrator = True
                logger.info("⏭️ Bypassing orchestrator for simple single-country query; deterministic pipeline is more reliable")
            # Phase 4: Bypass orchestrator when previous turn was a clarification.
            # The deterministic pipeline with LLM conversation context handles
            # clarification answers better (preserves country, indicator context).
            if not bypass_orchestrator:
                last_intent_for_orch = conversation_manager.get_last_intent(conv_id)
                if last_intent_for_orch and last_intent_for_orch.clarificationNeeded:
                    bypass_orchestrator = True
                    logger.info("⏭️ Bypassing orchestrator for clarification answer; LLM conversation context required")
            if allow_orchestrator and (use_orchestrator or settings.use_langchain_orchestrator) and not bypass_orchestrator:
                logger.info("🤖 Using LangChain orchestrator for intelligent query routing")
                return await self._execute_with_orchestrator(query, conv_id, tracker)
            if bypass_orchestrator:
                logger.info("⏭️ Bypassing orchestrator for temporal split query; using deterministic pipeline")

            # Early complexity detection (before LLM parsing)
            early_complexity = QueryComplexityAnalyzer.detect_complexity(query, intent=None)

            # If query REQUIRES Pro Mode, automatically switch
            if auto_pro_mode and early_complexity['pro_mode_required']:
                logger.info("🚀 Auto-switching to Pro Mode (detected: %s)", early_complexity['complexity_factors'])
                return await self._execute_pro_mode(query, conv_id)

            # ── LLM-based follow-up detection via dynamic prompt ─────
            # Build conversation_context for the LLM system prompt so it can
            # detect follow-ups natively (replacing brittle regex patterns).
            # The raw query is preserved as `original_raw_query` for downstream use.
            conversation_context = None
            last_intent = conversation_manager.get_last_intent(conv_id)
            if last_intent:
                li_params = last_intent.parameters or {}
                # Gather country/countries from prior intent
                prior_country = li_params.get("country", "")
                prior_countries_list = li_params.get("countries")
                if prior_countries_list and isinstance(prior_countries_list, list):
                    country_str = ", ".join(str(c) for c in prior_countries_list)
                elif prior_country:
                    country_str = str(prior_country)
                else:
                    country_str = "not specified"

                conversation_context = {
                    "indicator": ", ".join(last_intent.indicators) if last_intent.indicators else "not specified",
                    "country": country_str,
                    "provider": last_intent.apiProvider or "not specified",
                    "startDate": li_params.get("startDate", "not specified"),
                    "endDate": li_params.get("endDate", "not specified"),
                    "originalQuery": last_intent.originalQuery or "not specified",
                }

                # Phase 4: Include clarification context when previous turn was a clarification.
                # This lets the LLM see what was asked and resolve the user's answer with full context.
                if last_intent.clarificationNeeded:
                    # Check for pending semantic clarification details (group scope, etc.)
                    pending_ctx = conversation_manager.get_pending_clarification_context(conv_id)
                    if pending_ctx:
                        conversation_context["pendingClarification"] = True
                        conversation_context["clarificationQuestion"] = pending_ctx.get("question", "")
                        conversation_context["clarificationOptions"] = ", ".join(
                            str(opt) for opt in (pending_ctx.get("options") or [])
                        )
                        # Use the original query from the pending state if available
                        original_from_pending = pending_ctx.get("original_query", "")
                        if original_from_pending:
                            conversation_context["originalQuery"] = original_from_pending
                        logger.info(
                            "📎 Built clarification context for LLM resolution (pending: %s)",
                            pending_ctx.get("kind", "unknown"),
                        )
                    elif last_intent.clarificationQuestions:
                        # LLM-generated clarification (not stored in pending state)
                        conversation_context["pendingClarification"] = True
                        conversation_context["clarificationQuestion"] = " ".join(
                            last_intent.clarificationQuestions
                        )
                        conversation_context["clarificationOptions"] = ""
                        logger.info(
                            "📎 Built LLM-clarification context for resolution (questions: %s)",
                            last_intent.clarificationQuestions[:2],
                        )

                    # Clear pending state now — LLM will handle resolution
                    conversation_manager.clear_all_pending(conv_id)
                else:
                    logger.info(
                        "📎 Built conversation context for LLM follow-up detection (prior: %s / %s)",
                        last_intent.indicators, last_intent.apiProvider,
                    )

            logger.info("Parsing query with LLM: %s", query)

            # --- Intent-level caching (Optimization 2) ---
            # Cache parsed intents for identical queries to skip LLM re-parsing
            # (saves 4-6s on repeated queries). Only cache when there is no
            # conversation context — follow-ups need fresh parsing.
            _use_intent_cache = conversation_context is None
            _query_hash = _intent_cache_key(query) if _use_intent_cache else None
            _cached = _get_cached_parse_result(_query_hash) if _query_hash else None

            with tracker.track("parsing_query", "🤖 Understanding your question...") as update_parse_metadata:
                if _cached is not None:
                    import copy
                    parse_result = copy.deepcopy(_cached)
                    logger.info("⚡ Intent cache HIT for: %s (skipped LLM call)", query[:60])
                else:
                    parse_result = await self.pipeline.parse_and_route(
                        query, history, conversation_context=conversation_context,
                    )
                    # Cache the result for future identical queries (only without conversation context)
                    if _use_intent_cache and _query_hash and not parse_result.intent.clarificationNeeded:
                        _put_cached_parse_result(_query_hash, parse_result)
                        logger.info("⚡ Intent cache STORE for: %s", query[:60])

                intent = parse_result.intent
                # Ensure originalQuery stores the user's raw query, not the
                # context-enriched version sent to the LLM.
                intent.originalQuery = query

                # If LLM detected a follow-up with a resolvedQuery, use it as
                # the effective query for downstream processing.
                if intent.isFollowUp and intent.resolvedQuery:
                    logger.info(
                        "🔄 LLM follow-up detected (type=%s): '%s' → resolvedQuery='%s'",
                        intent.followUpType, query, intent.resolvedQuery,
                    )
                    query = intent.resolvedQuery
                    intent.originalQuery = query

                logger.debug("Parsed intent: %s", intent.model_dump())
                update_parse_metadata({
                    "provider": intent.apiProvider,
                    "indicators": intent.indicators,
                })

            # Framework: UnifiedRouter determines the provider (overrides LLM).
            # The LLM may guess wrong (e.g., NOT_AVAILABLE for gold price,
            # WorldBank for "from Eurostat"). UnifiedRouter is deterministic
            # and handles explicit mentions, country context, and catalog concepts.
            try:
                router_decision = self.unified_router.route(
                    query=query,
                    indicators=intent.indicators or [],
                    llm_provider=intent.apiProvider,
                    country=intent.parameters.get("country") if intent.parameters else None,
                )
                if router_decision and router_decision.provider:
                    routed = normalize_provider_name(router_decision.provider)
                    llm_prov = normalize_provider_name(intent.apiProvider or "")
                    if routed != llm_prov:
                        logger.info(
                            "🎯 UnifiedRouter override: LLM=%s → Router=%s (type=%s, conf=%.2f)",
                            intent.apiProvider, routed, router_decision.match_type, router_decision.confidence,
                        )
                        intent.apiProvider = routed
                    # Fix NOT_AVAILABLE — LLM says not available but router found a provider
                    if llm_prov in ("NOT_AVAILABLE", "NONE", "UNKNOWN", ""):
                        intent.apiProvider = routed
                        logger.info("🔧 Fixed NOT_AVAILABLE: router found %s", routed)
            except Exception as e:
                logger.debug("UnifiedRouter override failed: %s", e)

            # Framework enrichment: recover from avoidable parser clarifications and
            # auto-expand clear multi-concept comparisons to multi-indicator intents.
            self._maybe_resolve_region_clarification(query, intent)
            self._maybe_resolve_temporal_comparison_clarification(query, intent)
            self._maybe_expand_multi_concept_intent(query, intent)

            # Route informational queries — the LLM classified queryType as
            # part of intent extraction (same API call, zero cost).
            _qt = str(intent.queryType or "").strip().lower()
            # Fallback: if the heuristic detected informational but the LLM
            # didn't classify it as such, override.  The heuristic has high
            # precision (question word + metadata word) so false positives
            # are rare, while local LLMs sometimes miss the queryType field.
            if _qt != "informational" and self._looks_informational(query):
                logger.info("📖 Heuristic override: queryType %r → informational for: %s", _qt, query[:60])
                _qt = "informational"
                intent.queryType = "informational"
            if _qt == "informational":
                logger.info("📖 LLM classified query as informational: %s", query)
                informational_response = self._handle_informational_intent(
                    query=query,
                    intent=intent,
                    conversation_id=conv_id,
                    tracker=tracker,
                )
                if informational_response is not None:
                    conv_id = conversation_manager.add_message_safe(
                        conv_id, "user", query, intent=intent,
                    )
                    conversation_manager.add_message_safe(
                        conv_id, "assistant", informational_response.message or "",
                    )
                    return informational_response

            conv_id = conversation_manager.add_message_safe(conv_id, "user", query, intent=intent)

            if intent.clarificationNeeded:
                conversation_manager.clear_pending_indicator_options(conv_id)
                conversation_manager.clear_pending_semantic_clarification(conv_id)
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
                return self._build_invalid_intent_response(
                    conversation_id=conv_id,
                    intent=intent,
                    validation_error=validation_error,
                    suggestions=suggestions,
                    processing_steps=tracker.to_list(),
                )

            is_confident = validation.is_confident
            confidence_reason = validation.confidence_reason
            if not is_confident:
                logger.warning("Low confidence in intent: %s", confidence_reason)
                return self._build_low_confidence_intent_response(
                    conversation_id=conv_id,
                    intent=intent,
                    confidence_reason=confidence_reason,
                    processing_steps=tracker.to_list(),
                )

            # Log any warnings from validation
            if suggestions and suggestions.get('warning'):
                logger.info("Validation warning: %s", suggestions['warning'])

            parse_stage_clarification = await self._build_post_parse_clarification(
                conversation_id=conv_id,
                query=query,
                parse_result=parse_result,
                validation=validation,
                processing_steps=tracker.to_list(),
            )
            if parse_stage_clarification:
                return parse_stage_clarification

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

                # Try fallback providers before returning a hard no-data response.
                # Empty payloads are often provider-specific coverage gaps.
                try:
                    logger.info("🔄 Empty result detected, attempting fallback providers...")
                    fallback_data = await self._try_with_fallback(
                        intent,
                        DataNotAvailableError(
                            f"No data returned from {intent.apiProvider} for query: {query}"
                        ),
                    )
                    if fallback_data:
                        logger.info("✅ Fallback succeeded after empty primary response")
                        fallback_data = self._rerank_data_by_query_relevance(query, fallback_data)
                        fallback_data = self._apply_ranking_projection(query, fallback_data)
                        fallback_data, coverage_warning = await self._maybe_improve_country_coverage(
                            query,
                            intent,
                            fallback_data,
                        )
                        return QueryResponse(
                            conversationId=conv_id,
                            intent=intent,
                            data=fallback_data,
                            clarificationNeeded=False,
                            message=coverage_warning,
                            processingSteps=tracker.to_list(),
                        )
                except Exception as fallback_exc:
                    logger.warning("Fallback after empty response failed: %s", fallback_exc)

                # Semantic recovery pass before returning hard no-data.
                recovered_data = await self._maybe_recover_from_empty_data(query, intent)
                if recovered_data:
                    logger.info("✅ Semantic recovery succeeded after empty primary response")
                    recovered_data, coverage_warning = await self._maybe_improve_country_coverage(
                        query,
                        intent,
                        recovered_data,
                    )
                    return QueryResponse(
                        conversationId=conv_id,
                        intent=intent,
                        data=recovered_data,
                        clarificationNeeded=False,
                        message=coverage_warning,
                        processingSteps=tracker.to_list(),
                    )

                no_data_clarification = self._build_no_data_indicator_clarification(
                    conversation_id=conv_id,
                    query=query,
                    intent=intent,
                    processing_steps=tracker.to_list(),
                )
                if no_data_clarification:
                    return no_data_clarification

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
            data = self._apply_ranking_projection(query, data)
            recovered_uncertain_data = await self._maybe_recover_from_uncertain_match(
                query,
                intent,
                data,
            )
            if recovered_uncertain_data:
                data = recovered_uncertain_data
            data, coverage_warning = await self._maybe_improve_country_coverage(
                query,
                intent,
                data,
            )
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
                message=coverage_warning,
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
                        fallback_data = self._rerank_data_by_query_relevance(query, fallback_data)
                        fallback_data = self._apply_ranking_projection(query, fallback_data)
                        fallback_data, coverage_warning = await self._maybe_improve_country_coverage(
                            query,
                            intent,
                            fallback_data,
                        )
                        return QueryResponse(
                            conversationId=conv_id,
                            intent=intent,
                            data=fallback_data,
                            clarificationNeeded=False,
                            message=coverage_warning,
                            processingSteps=tracker.to_list(),
                        )
                except Exception as fallback_exc:
                    logger.warning("All fallback providers failed: %s", fallback_exc)

            # Last resort: serve stale (expired) cached data rather than returning nothing.
            # A 1-hour-old GDP dataset is better than "No Data Available" during an API outage.
            if "intent" in locals() and intent:
                stale_data = await self._get_stale_from_cache(
                    normalize_provider_name(intent.apiProvider), intent.parameters or {}
                )
                if stale_data:
                    stale_list = stale_data if isinstance(stale_data, list) else [stale_data]
                    return QueryResponse(
                        conversationId=conv_id,
                        intent=intent,
                        data=stale_list,
                        clarificationNeeded=False,
                        message="⚠️ The data provider is temporarily unavailable. Showing cached data (may not be the latest).",
                        processingSteps=tracker.to_list(),
                    )

            clarification_response = self._build_no_data_indicator_clarification(
                conversation_id=conv_id,
                query=query,
                intent=intent if "intent" in locals() else None,
                processing_steps=tracker.to_list(),
            )
            if clarification_response:
                return clarification_response

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
                        fallback_data = self._rerank_data_by_query_relevance(query, fallback_data)
                        fallback_data = self._apply_ranking_projection(query, fallback_data)
                        fallback_data, coverage_warning = await self._maybe_improve_country_coverage(
                            query,
                            intent,
                            fallback_data,
                        )
                        return QueryResponse(
                            conversationId=conv_id,
                            intent=intent,
                            data=fallback_data,
                            clarificationNeeded=False,
                            message=coverage_warning,
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
        explicit_provider = self._normalize_provider_alias(
            self._detect_explicit_provider(intent.originalQuery or "")
        )

        # Ensure default time periods are applied to base intent first
        if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
            logger.info("📅 Applying default time periods to multi-indicator query...")
            ParameterValidator.apply_default_time_periods(intent)

        # Create separate intents for each indicator
        fetch_tasks = []
        for indicator in intent.indicators:
            # Create parameters for this indicator
            params = dict(intent.parameters) if intent.parameters else {}

            # Always set the indicator param for each sub-intent so that
            # concept-override logic in _fetch_data does not clobber it with
            # a match from the original (multi-concept) query text.
            params["indicator"] = indicator
            # Remove stale catalog-resolved flag so the sub-intent goes
            # through proper indicator resolution for its specific indicator.
            params.pop("__catalog_resolved", None)
            params.pop("__catalog_concept", None)

            single_provider = normalize_provider_name(intent.apiProvider)
            if explicit_provider:
                single_provider = explicit_provider
            else:
                try:
                    routing_intent = ParsedIntent(
                        apiProvider=single_provider,
                        indicators=[indicator],
                        parameters=dict(params),
                        clarificationNeeded=False,
                        originalQuery=intent.originalQuery,
                    )
                    routed_provider = await self._select_routed_provider(
                        routing_intent,
                        f"{indicator} {intent.originalQuery or ''}".strip(),
                    )
                    if routed_provider:
                        single_provider = routed_provider
                except Exception as exc:
                    logger.debug(
                        "Multi-indicator provider routing failed for '%s': %s",
                        indicator,
                        exc,
                    )

            # Create a new intent with single indicator.
            # Use a narrowed originalQuery that focuses on this specific
            # indicator so that concept-override matching doesn't confuse
            # indicators (e.g., matching "inflation" when fetching "unemployment").
            countries_text = ""
            if params.get("countries"):
                countries_text = f" for {', '.join(str(c) for c in params['countries'][:3])}"
            narrowed_query = f"{indicator}{countries_text}"

            single_intent = ParsedIntent(
                apiProvider=single_provider,
                indicators=[indicator],
                parameters=params,
                clarificationNeeded=False,
                confidence=intent.confidence,
                recommendedChartType=intent.recommendedChartType,
                originalQuery=narrowed_query,
            )

            # Create fetch task with retry — use fewer attempts for multi-indicator
            # queries to avoid compounding latency across many parallel fetches
            task = retry_async(
                lambda i=single_intent: self._fetch_data(i),
                max_attempts=2,
                initial_delay=0.5,
            )
            fetch_tasks.append(task)

        # Fetch all indicators in parallel with a total timeout.
        # Multi-indicator + multi-country queries can generate many API calls;
        # cap total wall-clock time to avoid exceeding client timeouts.
        num_countries = len(
            intent.parameters.get("countries", []) if intent.parameters else []
        )
        # Scale timeout: base 45s, +5s per country beyond 3 (max 90s)
        total_timeout = min(90, 45 + max(0, num_countries - 3) * 5)
        logger.info(
            "🔄 Fetching %s indicators in parallel (timeout=%ds, countries=%d)...",
            len(fetch_tasks), total_timeout, num_countries,
        )

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*fetch_tasks, return_exceptions=True),
                timeout=total_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "⏰ Multi-indicator fetch timed out after %ds — returning partial results",
                total_timeout,
            )
            results = []

        # Collect successful results
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                indicator_name = intent.indicators[i] if i < len(intent.indicators) else "unknown"
                logger.warning("Failed to fetch indicator %s: %s", indicator_name, result)
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

        # Early exit: concept is not available from any provider (catalog said so)
        if provider == "NOT_AVAILABLE":
            indicator_text = intent.indicators[0] if intent.indicators else "this indicator"
            logger.info(f"📚 Not available: {indicator_text} — no provider carries this data")
            return [NormalizedData(
                metadata=Metadata(
                    source="Catalog",
                    indicator=f"{indicator_text} — Not Available",
                    frequency="N/A",
                    unit="N/A",
                    lastUpdated="",
                    description=(
                        f"'{indicator_text}' is not currently available through any of our "
                        f"data providers. This may be because the data has been discontinued, "
                        f"archived, or is only available through specialized sources."
                    ),
                ),
                data=[],
            )]
        params = intent.parameters or {}

        # ── Pre-flight geographic split ──────────────────────────────
        # If this is a multi-country query and the chosen provider cannot
        # cover all countries, split into per-provider sub-queries, fetch
        # in parallel, and merge.  The internal flag prevents recursion
        # (sub-intents created by the split set it to True).
        if not params.get("__geo_split_child"):
            split_result = await self._preflight_geographic_split(intent)
            if split_result is not None:
                return split_result
        # Strip the recursion guard before downstream code sees it.
        if "__geo_split_child" in params:
            params = {k: v for k, v in params.items() if k != "__geo_split_child"}
            intent.parameters = params

        fallback_excluded_providers = {
            normalize_provider_name(str(candidate))
            for candidate in (params.get("__fallback_excluded_providers") or [])
            if candidate
        }
        fallback_excluded_providers.discard("")
        tracker = get_processing_tracker()

        ranking_scope_query = str(intent.originalQuery or "").strip()
        if not ranking_scope_query and intent.indicators:
            ranking_scope_query = " ".join(str(indicator) for indicator in intent.indicators if indicator)
        params = self._maybe_expand_ranking_country_scope(ranking_scope_query, provider, params)
        intent.parameters = params

        provider, params = self._apply_concept_provider_override(provider, intent, params)
        intent.parameters = params

        # PHASE B: Resolve indicator code via unified resolution pipeline
        params = await self._resolve_indicator_for_fetch(provider, intent, params)

        # Check catalog availability and re-route if needed
        provider, params = self._apply_catalog_availability_override(
            provider, intent, params, fallback_excluded_providers
        )

        # Preserve catalog-resolved flag as a transient attribute on the intent
        # before stripping internal keys. Used by _build_alternative_series to
        # skip the FTS5 lookup for high-confidence catalog matches.
        if params.get("__catalog_resolved"):
            object.__setattr__(intent, "_catalog_resolved", True)

        internal_param_keys = {"__fallback_excluded_providers", "__catalog_resolved", "__catalog_concept", "__qualifier_checked", "__geo_split_child"}
        if any(key in params for key in internal_param_keys):
            params = {k: v for k, v in params.items() if k not in internal_param_keys}
            intent.parameters = params

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
            self._normalize_bis_metadata_labels(result_list)
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
                logger.info(f"🌍 WorldBank dispatch: indicator={resolved_indicator}, country={params.get('country')}, countries={params.get('countries')}, startDate={params.get('startDate')}")
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
                    wb_result = await self.world_bank_provider.fetch_indicator(
                        indicator=indicator,
                        country=params.get("country"),
                        countries=params.get("countries"),
                        start_date=params.get("startDate"),
                        end_date=params.get("endDate"),
                    )
                    if isinstance(wb_result, list):
                        logger.info(f"🌍 WorldBank returned: {len(wb_result)} series, data_pts={[len(r.data) for r in wb_result if r]}")
                    else:
                        logger.info(f"🌍 WorldBank returned: type={type(wb_result)}, data_pts={len(wb_result.data) if wb_result and wb_result.data else 0}")
                    return wb_result
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

                # --- Framework fix: resolve numeric table/product IDs back to known vectors ---
                # The LLM sometimes sets params.indicator to a StatsCan table ID (e.g. "14100330")
                # instead of a semantic name (e.g. "UNEMPLOYMENT_RATE"). When that happens the
                # hardcoded vector lookup is bypassed and we hit a potentially stale/annual table.
                # Fix: always check the human-readable intent.indicators against VECTOR_MAPPINGS
                # and COORDINATE_PRODUCT_MAPPINGS first, regardless of what the LLM put in params.
                resolved_indicator = indicator  # what we'll actually use
                indicator_key = indicator.upper().replace(" ", "_") if indicator else None
                _sc_vectors = self.statscan_provider.VECTOR_MAPPINGS
                _sc_coords = self.statscan_provider.COORDINATE_PRODUCT_MAPPINGS

                if indicator_key and indicator_key not in _sc_vectors and indicator_key not in _sc_coords:
                    # The params indicator didn't match any known mapping.
                    # Try each human-readable indicator from intent.indicators.
                    for hr_indicator in (intent.indicators or []):
                        hr_key = hr_indicator.upper().replace(" ", "_")
                        if hr_key in _sc_vectors or hr_key in _sc_coords:
                            logger.info(
                                f"StatsCan: LLM indicator '{indicator}' not in mappings; "
                                f"resolved from intent.indicators '{hr_indicator}' -> key '{hr_key}'"
                            )
                            resolved_indicator = hr_indicator
                            indicator_key = hr_key
                            # Also update params so downstream fetch_series sees the correct indicator
                            params = {**params, "indicator": hr_indicator}
                            break
                    else:
                        # None of the intent.indicators matched either.
                        # Try normalising common natural-language phrases:
                        # "unemployment rate" -> UNEMPLOYMENT_RATE, "gdp" -> GDP, etc.
                        for hr_indicator in (intent.indicators or []):
                            normalised_key = hr_indicator.upper().replace(" ", "_").replace("-", "_")
                            if normalised_key in _sc_vectors or normalised_key in _sc_coords:
                                logger.info(
                                    f"StatsCan: resolved via normalised intent indicator "
                                    f"'{hr_indicator}' -> '{normalised_key}'"
                                )
                                resolved_indicator = normalised_key
                                indicator_key = normalised_key
                                params = {**params, "indicator": normalised_key}
                                break

                indicator = resolved_indicator
                # --- End framework fix ---

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
                    if indicator and indicator.upper().replace(" ", "_") in self.statscan_provider.VECTOR_MAPPINGS:
                        # Use vector-based fetch for hardcoded indicators
                        series = await self.statscan_provider.fetch_series(params)
                        return [series]
                    elif indicator:
                        # Use dynamic discovery for non-hardcoded indicators
                        # (e.g., EMPLOYMENT, RETAIL_SALES, LABOUR_FORCE)
                        logger.info(f"🔍 Using dynamic discovery for StatsCan indicator: {indicator}")
                        dynamic_params = {
                            "indicator": indicator,
                            "indicatorLabel": str(intent.indicators[0] if intent.indicators else indicator),
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
                resolved_indicator = str(params.get("indicator") or "").strip()

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
                    indicators_to_fetch = list(intent.indicators or [])
                    if resolved_indicator:
                        indicators_to_fetch = [resolved_indicator]
                    if not indicators_to_fetch:
                        indicators_to_fetch = [resolved_indicator] if resolved_indicator else []

                    for indicator in indicators_to_fetch:
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
                        indicators_to_fetch = list(intent.indicators or [])
                        if resolved_indicator:
                            indicators_to_fetch = [resolved_indicator]
                        for indicator in indicators_to_fetch:
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
                return await self._fetch_exchange_rate_with_historical_fallback(intent, params)
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
                    # Fetch all countries in parallel with a concurrency limiter
                    eurostat_sem = asyncio.Semaphore(5)

                    async def _fetch_eurostat_country(country_code: str) -> Optional[NormalizedData]:
                        async with eurostat_sem:
                            try:
                                return await self.eurostat_provider.fetch_indicator(
                                    indicator=indicator,
                                    country=country_code,
                                    start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                                    end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                                )
                            except Exception as e:
                                logger.warning(f"Failed to fetch {indicator} for {country_code}: {e}")
                                return None

                    eurostat_results = await asyncio.gather(
                        *[_fetch_eurostat_country(c) for c in countries_param],
                        return_exceptions=True,
                    )
                    series_list = [
                        r for r in eurostat_results
                        if isinstance(r, NormalizedData)
                    ]

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
                return await self._fetch_from_coingecko(intent, params)
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
                provider_start = time.perf_counter()
                result = await fetch_from_provider()
                provider_elapsed = time.perf_counter() - provider_start
                logger.info(f"Provider {provider} fetch: {provider_elapsed:.2f}s")
                update_fetch_metadata({
                    "series_count": len(result),
                    "cached": False,
                    "fetch_time_ms": round(provider_elapsed * 1000, 1),
                })
        else:
            provider_start = time.perf_counter()
            result = await fetch_from_provider()
            provider_elapsed = time.perf_counter() - provider_start
            logger.info(f"Provider {provider} fetch: {provider_elapsed:.2f}s")

        if not result or (len(result) == 1 and not result[0].data):
            raise DataNotAvailableError(
                f"No data available from {provider} for the requested parameters. "
                f"The data may not exist or may not be available for the specified time period or location."
            )

        self._normalize_bis_metadata_labels(result)

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

        Supports two modes:
        - LangGraph (USE_LANGGRAPH=true, default): State-persistent agent graph
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
            use_deep_agents = os.getenv('USE_DEEP_AGENTS', 'true').lower() == 'true'

            # Get conversation history for context
            conversation_history = conversation_manager.get_messages(conversation_id)

            # Run the same post-parse clarification guardrails used by the
            # standard pipeline before agent orchestration takes over.
            if tracker:
                with tracker.track("parsing_query", "🤖 Understanding your question...") as update_parse_metadata:
                    parse_result = await self.pipeline.parse_and_route(query, conversation_history)
                    intent = parse_result.intent
                    update_parse_metadata({
                        "provider": intent.apiProvider,
                        "indicators": intent.indicators,
                    })
            else:
                parse_result = await self.pipeline.parse_and_route(query, conversation_history)
                intent = parse_result.intent

            self._maybe_resolve_region_clarification(query, intent)
            self._maybe_resolve_temporal_comparison_clarification(query, intent)
            self._maybe_expand_multi_concept_intent(query, intent)

            if intent.clarificationNeeded:
                # Store the clarification intent so Phase 4 LLM-based resolution
                # can build conversation context on the next turn.
                conversation_manager.add_message_safe(
                    conversation_id, "user", query, intent=intent,
                )
                conversation_manager.clear_pending_indicator_options(conversation_id)
                conversation_manager.clear_pending_semantic_clarification(conversation_id)
                return QueryResponse(
                    conversationId=conversation_id,
                    intent=intent,
                    clarificationNeeded=True,
                    clarificationQuestions=intent.clarificationQuestions,
                    processingSteps=tracker.to_list() if tracker else None,
                )

            ParameterValidator.apply_default_time_periods(intent)
            validation = self.pipeline.validate_intent(intent)
            if not validation.is_valid:
                logger.warning("Orchestrator pre-check validation failed: %s", validation.validation_error)
                return self._build_invalid_intent_response(
                    conversation_id=conversation_id,
                    intent=intent,
                    validation_error=validation.validation_error,
                    suggestions=validation.suggestions,
                    processing_steps=tracker.to_list() if tracker else None,
                )

            if not validation.is_confident:
                logger.warning("Orchestrator pre-check low confidence: %s", validation.confidence_reason)
                return self._build_low_confidence_intent_response(
                    conversation_id=conversation_id,
                    intent=intent,
                    confidence_reason=validation.confidence_reason,
                    processing_steps=tracker.to_list() if tracker else None,
                )

            parse_stage_clarification = await self._build_post_parse_clarification(
                conversation_id=conversation_id,
                query=query,
                parse_result=parse_result,
                validation=validation,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if parse_stage_clarification:
                return parse_stage_clarification

            # Add current query to history WITH intent so follow-up queries
            # can reference the last intent via get_last_intent().
            updated_conversation_id = conversation_manager.add_message_safe(
                conversation_id,
                "user",
                query,
                intent=intent,
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

            # Standard pipeline — the deterministic pipeline with IndicatorSelector
            # now handles indicator resolution for ALL 330K indicators.
            # LangGraph/orchestrator is bypassed in favor of the simpler, more
            # reliable direct pipeline that includes:
            # - IndicatorSelector (OpenAI embed → LLM pick)
            # - Resolver-first resolution order
            # - Qualifier preservation
            # - All provider routing
            #
            # The intent has already been parsed (line 9194), validated, and
            # clarified — just execute it through the standard fetch path.
            logger.info("📊 Using standard pipeline (IndicatorSelector + direct fetch)")
            return await self._execute_standard_pipeline(
                query, conversation_id, intent, tracker,
            )

        except Exception as e:
            logger.error(f"Orchestration error: {e}", exc_info=True)
            # Fallback: try standard pipeline directly
            fallback_intent = intent if 'intent' in locals() and intent else None
            try:
                logger.info("Orchestration fallback to standard pipeline")
                parse_result = await self.pipeline.parse_and_route(query, [])
                fallback_intent = parse_result.intent
                return await self._execute_standard_pipeline(
                    query, conversation_id, parse_result.intent, tracker,
                )
            except Exception as fallback_error:
                logger.warning("Orchestration fallback also failed: %s", fallback_error)
                return QueryResponse(
                    conversationId=conversation_id,
                    intent=fallback_intent,
                    clarificationNeeded=False,
                    error=f"Query failed: {str(e)[:200]}",
                    processingSteps=tracker.to_list() if tracker else None,
                )

    async def _execute_standard_pipeline(
        self,
        query: str,
        conversation_id: str,
        intent: ParsedIntent,
        tracker: Optional['ProcessingTracker'] = None,
    ) -> QueryResponse:
        """Execute query through the standard deterministic pipeline.

        This replaces the LangGraph/orchestrator path with the simpler,
        more reliable direct pipeline that includes IndicatorSelector
        for 330K indicator resolution.

        Includes fallback providers and stale cache recovery so that
        multi-country / multi-provider queries don't silently fail.
        """
        fetch_error: Optional[Exception] = None

        # Check for multi-indicator queries (e.g., "unemployment and inflation for G7")
        # and use the parallel multi-indicator fetch path.
        is_multi_indicator = bool(intent.indicators and len(intent.indicators) > 1)

        # Primary fetch attempt
        try:
            if is_multi_indicator:
                logger.info(
                    "📊 Standard pipeline: multi-indicator query (%d indicators)",
                    len(intent.indicators),
                )
                result = await self._fetch_multi_indicator_data(intent)
            else:
                result = await self._fetch_data(intent)
            if result:
                # _fetch_data may return QueryResponse or list of NormalizedData
                if isinstance(result, QueryResponse):
                    if not result.conversationId:
                        result.conversationId = conversation_id
                    # Ensure intent is always present in the response
                    if not result.intent:
                        result.intent = intent
                    # Add alternative series if not already present
                    if result.data and not result.alternativeSeries:
                        result.alternativeSeries = self._build_alternative_series(intent, result.data)
                    return result
                elif isinstance(result, list):
                    # Rerank and project before returning
                    result = self._rerank_data_by_query_relevance(query, result)
                    result = self._apply_ranking_projection(query, result)
                    result, coverage_warning = await self._maybe_improve_country_coverage(
                        query, intent, result,
                    )
                    alternatives = self._build_alternative_series(intent, result)
                    conversation_manager.add_message_safe(
                        conversation_id, "assistant",
                        f"Data fetched: {intent.apiProvider}",
                        intent=intent,
                    )
                    return QueryResponse(
                        conversationId=conversation_id,
                        intent=intent,
                        data=result,
                        clarificationNeeded=False,
                        message=coverage_warning,
                        alternativeSeries=alternatives,
                        processingSteps=tracker.to_list() if tracker else None,
                    )
        except Exception as e:
            fetch_error = e
            logger.warning(f"Standard pipeline primary fetch error: {e}", exc_info=True)

        # Fallback: try alternative providers before giving up
        try:
            logger.info("🔄 Standard pipeline: attempting fallback providers...")
            fallback_data = await self._try_with_fallback(intent, fetch_error or Exception("No data"))
            if fallback_data:
                logger.info("✅ Standard pipeline: fallback provider succeeded")
                fallback_data = self._rerank_data_by_query_relevance(query, fallback_data)
                fallback_data = self._apply_ranking_projection(query, fallback_data)
                fallback_data, coverage_warning = await self._maybe_improve_country_coverage(
                    query, intent, fallback_data,
                )
                return QueryResponse(
                    conversationId=conversation_id,
                    intent=intent,
                    data=fallback_data,
                    clarificationNeeded=False,
                    message=coverage_warning,
                    processingSteps=tracker.to_list() if tracker else None,
                )
        except Exception as fallback_exc:
            logger.warning("Standard pipeline fallback providers failed: %s", fallback_exc)

        # Semantic recovery pass
        try:
            recovered_data = await self._maybe_recover_from_empty_data(query, intent)
            if recovered_data:
                logger.info("✅ Standard pipeline: semantic recovery succeeded")
                recovered_data, coverage_warning = await self._maybe_improve_country_coverage(
                    query, intent, recovered_data,
                )
                return QueryResponse(
                    conversationId=conversation_id,
                    intent=intent,
                    data=recovered_data,
                    clarificationNeeded=False,
                    message=coverage_warning,
                    processingSteps=tracker.to_list() if tracker else None,
                )
        except Exception as recovery_exc:
            logger.warning("Standard pipeline semantic recovery failed: %s", recovery_exc)

        # Last resort: serve stale (expired) cached data rather than nothing
        try:
            stale_data = await self._get_stale_from_cache(
                normalize_provider_name(intent.apiProvider), intent.parameters or {}
            )
            if stale_data:
                stale_list = stale_data if isinstance(stale_data, list) else [stale_data]
                return QueryResponse(
                    conversationId=conversation_id,
                    intent=intent,
                    data=stale_list,
                    clarificationNeeded=False,
                    message="The data provider is temporarily unavailable. Showing cached data (may not be the latest).",
                    processingSteps=tracker.to_list() if tracker else None,
                )
        except Exception:
            pass

        # Build indicator-specific clarification if possible
        clarification_response = self._build_no_data_indicator_clarification(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            processing_steps=tracker.to_list() if tracker else None,
        )
        if clarification_response:
            return clarification_response

        # Final error response - ALWAYS include intent so frontend knows what was parsed
        provider_name = intent.apiProvider
        indicators = ", ".join(intent.indicators) if intent.indicators else "requested indicator"
        country = ""
        if intent.parameters:
            country = intent.parameters.get("country") or ""
            if not country:
                countries = intent.parameters.get("countries", [])
                if countries:
                    country = ", ".join(str(c) for c in countries)

        error_details = [f"No data found for **{indicators}**"]
        if country:
            error_details.append(f"for **{country}**")
        error_details.append(f"from **{provider_name}**.")
        suggestions = self._get_no_data_suggestions(provider_name, intent)

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=False,
            error="no_data_found",
            message=f"No Data Available\n\n{' '.join(error_details)}\n\n{suggestions}",
            processingSteps=tracker.to_list() if tracker else None,
        )

    # Legacy orchestrator code removed — replaced by _execute_standard_pipeline.
    # See git history for the old LangChain orchestrator implementation.

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
        high_signal_query_cues = {
            cue for cue in query_cues
            if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        }
        concept_groups = self._infer_query_concept_groups(query)

        if has_ratio_query and not has_analysis_keyword:
            logger.info("⏭️ Deep Agents skipped for single-metric ratio retrieval query")
            return False

        # Single-concept retrieval queries (even when ranking/comparison phrasing is
        # present) are better served by deterministic fetching + framework ranking.
        if (
            (self._is_ranking_query(query) or self._is_comparison_query(query))
            and len(concept_groups) <= 1
            and len(high_signal_query_cues) <= 2
            and not has_analysis_keyword
        ):
            logger.info(
                "⏭️ Deep Agents skipped for single-concept retrieval query (concepts=%s, cues=%s)",
                sorted(concept_groups),
                sorted(high_signal_query_cues),
            )
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

        # PERFORMANCE FIX: The standard pipeline already handles multi-country +
        # multi-indicator comparison queries efficiently:
        # - _maybe_expand_multi_concept_intent() detects comparison queries
        # - _fetch_multi_indicator_data() fetches indicators in parallel
        # - WorldBank/IMF batch country requests in a single API call
        # - BIS/Eurostat fetch countries in parallel with semaphore
        #
        # Deep Agents decomposes into N*M individual process_query() calls, each
        # requiring a full LLM parse (~5s), so a G7 x 2 indicators query = 14
        # LLM calls = 70+ seconds.  The standard pipeline avoids this entirely.
        #
        # Only use Deep Agents for queries that require multi-step analysis
        # (correlation, regression, forecasting, etc.).

        is_multi_country = complexity.get('is_multi_country', False)
        is_multi_indicator = complexity.get('is_multi_indicator', False)
        is_ranking = complexity.get('is_ranking', False)

        # Check if this is a pure data retrieval/comparison query vs analysis
        analysis_keywords = [
            "correlation", "correlate", "regression", "decompose",
            "optimize", "forecast", "predict", "simulate", "model",
            "causal", "elasticity", "sensitivity",
        ]
        needs_analysis = any(kw in query_lower for kw in analysis_keywords)

        # Standard comparison/data-fetch queries: the deterministic pipeline is
        # faster and more reliable.  Skip Deep Agents for these.
        if (is_multi_country or is_multi_indicator or is_ranking) and not needs_analysis:
            logger.info(
                "⏭️ Deep Agents skipped: multi-country/indicator comparison handled "
                "by standard pipeline (multi_country=%s, multi_indicator=%s, ranking=%s)",
                is_multi_country, is_multi_indicator, is_ranking,
            )
            return False

        # Deep Agents only for truly complex analytical queries
        is_complex = False
        trigger_reason = []

        if needs_analysis:
            trigger_reason.append("analysis")
            is_complex = True

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
                self._normalize_bis_metadata_labels(data)
                data = self._rerank_data_by_query_relevance(query, data)
                data = self._apply_ranking_projection(query, data)

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

                if intent and data:
                    recovered_uncertain_data = await self._maybe_recover_from_uncertain_match(
                        query,
                        intent,
                        data,
                    )
                    if recovered_uncertain_data:
                        data = recovered_uncertain_data

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
        tracker: Optional['ProcessingTracker'] = None,
        pre_resolved_intent: Optional[ParsedIntent] = None,
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
                "parsed_intent": pre_resolved_intent,  # Pass pre-resolved intent to avoid re-parsing
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
                code_output = str(code_exec.get("output", "") or "").strip()
                raw_files = code_exec.get("files", []) or []
                # Guardrail: accidental Pro Mode routing for retrieval queries can
                # return empty code output and no datasets. Retry deterministic path.
                if result.get("query_type") != "analysis" and not code_output and not raw_files:
                    logger.warning(
                        "LangGraph routed non-analysis query to Pro Mode without output. "
                        "Retrying via standard pipeline."
                    )
                    return await self._standard_query_processing(
                        query,
                        conversation_id,
                        tracker,
                        record_user_message=False,
                    )
                # Convert file dicts to GeneratedFile objects
                files = None
                if raw_files:
                    files = [gf for gf in (_coerce_generated_file(f) for f in raw_files) if gf is not None]
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
            logger.info(f"🔍 LangGraph query_result type={type(query_result)}, keys={list(query_result.keys()) if isinstance(query_result, dict) else 'NOT_DICT'}")
            data = query_result.get("data", [])
            logger.info(f"🔍 LangGraph data type={type(data)}, len={len(data) if isinstance(data, (list,tuple)) else 'NOT_LIST'}")
            if isinstance(data, list):
                self._normalize_bis_metadata_labels(data)
            if isinstance(data, list) and data:
                data = self._rerank_data_by_query_relevance(query, data)
                data = self._apply_ranking_projection(query, data)

            logger.info(f"🔍 LangGraph data after rerank: type={type(data)}, len={len(data) if isinstance(data, (list,tuple)) else 'N/A'}")
            # Guardrail: if LangGraph returns data whose semantic cues do not
            # match high-signal cues from the original query (e.g., import vs debt),
            # retry through the standard deterministic path.
            if data:
                query_cues = self._extract_indicator_cues(query)
                high_signal_query_cues = {
                    cue for cue in query_cues
                    if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
                }
                result_cues: set[str] = set()
                for series in data:
                    indicator_name = (
                        series.metadata.indicator
                        if series and getattr(series, "metadata", None)
                        else ""
                    )
                    result_cues |= self._extract_indicator_cues(indicator_name)

                if high_signal_query_cues and not (high_signal_query_cues & result_cues):
                    logger.warning(
                        "LangGraph semantic cue mismatch (high_signal_query=%s, result=%s). "
                        "Retrying via standard pipeline.",
                        sorted(high_signal_query_cues),
                        sorted(result_cues),
                    )
                    return await self._standard_query_processing(
                        query,
                        conversation_id,
                        tracker,
                        record_user_message=False,
                    )

                if self._has_implausible_top_series(query, data):
                    logger.warning(
                        "LangGraph top series failed plausibility guard for query '%s'. "
                        "Retrying via standard pipeline.",
                        query,
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
                coerced_intent = self._coerce_parsed_intent(parsed_intent, query)
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

                recovery_intent = coerced_intent
                if not recovery_intent:
                    recovery_intent = self._coerce_parsed_intent(parsed_intent, query)
                if recovery_intent:
                    recovered_data = await self._maybe_recover_from_empty_data(query, recovery_intent)
                    if recovered_data:
                        logger.info("✅ LangGraph: Semantic recovery succeeded")
                        return QueryResponse(
                            conversationId=conversation_id,
                            intent=recovery_intent,
                            data=recovered_data,
                            clarificationNeeded=False,
                            processingSteps=tracker.to_list() if tracker else None,
                        )

                # Try fallback providers before giving up (same as standard path)
                if coerced_intent and provider_name != "Unknown":
                    try:
                        fallback_intent = coerced_intent
                        if not fallback_intent:
                            raise ValueError("Could not parse LangGraph fallback intent")

                        logger.info(f"🔄 LangGraph: Attempting fallback from {provider_name}...")
                        fallback_data = await self._try_with_fallback(
                            fallback_intent,
                            DataNotAvailableError(f"No data from {provider_name}")
                        )
                        if fallback_data:
                            logger.info(f"✅ LangGraph: Fallback succeeded!")
                            fallback_data = self._rerank_data_by_query_relevance(query, fallback_data)
                            fallback_data = self._apply_ranking_projection(query, fallback_data)
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

                # If LangGraph could not produce usable routing context, retry deterministic path.
                if (
                    not coerced_intent
                    or provider_name == "Unknown"
                    or indicators == "requested indicator"
                ):
                    logger.warning(
                        "LangGraph returned empty/under-specified data response. "
                        "Retrying via standard pipeline."
                    )
                    return await self._standard_query_processing(
                        query,
                        conversation_id,
                        tracker,
                        record_user_message=False,
                    )

                no_data_clarification = self._build_no_data_indicator_clarification(
                    conversation_id=conversation_id,
                    query=query,
                    intent=coerced_intent,
                    processing_steps=tracker.to_list() if tracker else None,
                )
                if no_data_clarification:
                    return no_data_clarification

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
                    intent=coerced_intent,
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

                if response.intent:
                    recovered_uncertain_data = await self._maybe_recover_from_uncertain_match(
                        query,
                        response.intent,
                        data,
                    )
                    if recovered_uncertain_data:
                        data = recovered_uncertain_data
                        response.data = data

                clarification_response = self._build_uncertain_result_clarification(
                    conversation_id=conversation_id,
                    query=query,
                    intent=response.intent,
                    data=data,
                    processing_steps=tracker.to_list() if tracker else None,
                )
                if clarification_response:
                    logger.warning(f"⚠️ LangGraph: Uncertain result converted to clarification (data had {len(data)} series)")
                    return clarification_response

                logger.info(f"✅ LangGraph: Returning {len(data)} series successfully")

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

        self._maybe_resolve_region_clarification(query, intent)
        self._maybe_expand_multi_concept_intent(query, intent)

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

        multi_concept_clarification = self._build_multi_concept_query_clarification(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            is_multi_indicator=bool(intent.indicators and len(intent.indicators) > 1),
            processing_steps=tracker.to_list() if tracker else None,
        )
        if multi_concept_clarification:
            return multi_concept_clarification

        # Fetch data
        data = await retry_async(
            lambda: self._fetch_data(intent),
            max_attempts=3,
            initial_delay=1.0,
        )
        if not data:
            recovered_data = await self._maybe_recover_from_empty_data(query, intent)
            if recovered_data:
                data = recovered_data
        if not data:
            provider_name = intent.apiProvider or "Unknown"
            indicators = ", ".join(intent.indicators) if intent.indicators else "requested indicator"
            country = intent.parameters.get("country") or intent.parameters.get("countries", [""])[0] if intent.parameters else ""
            no_data_clarification = self._build_no_data_indicator_clarification(
                conversation_id=conversation_id,
                query=query,
                intent=intent,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if no_data_clarification:
                return no_data_clarification
            details = [f"No data found for **{indicators}**"]
            if country:
                details.append(f"for **{country}**")
            details.append(f"from **{provider_name}**.")
            return QueryResponse(
                conversationId=conversation_id,
                intent=intent,
                clarificationNeeded=False,
                error="no_data_found",
                message=f"⚠️ **No Data Available**\n\n{' '.join(details)}",
                processingSteps=tracker.to_list() if tracker else None,
            )

        data = self._rerank_data_by_query_relevance(query, data)
        data = self._apply_ranking_projection(query, data)
        recovered_uncertain_data = await self._maybe_recover_from_uncertain_match(
            query,
            intent,
            data,
        )
        if recovered_uncertain_data:
            data = recovered_uncertain_data
        data, coverage_warning = await self._maybe_improve_country_coverage(
            query,
            intent,
            data,
        )
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

        # Generate alternative series suggestions for user exploration
        alternatives = self._build_alternative_series(intent, data)

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            data=data,
            clarificationNeeded=False,
            message=coverage_warning,
            processingSteps=tracker.to_list() if tracker else None,
            alternativeSeries=alternatives,
        )

    def _build_alternative_series(
        self,
        intent: ParsedIntent,
        data: Any,
    ) -> Optional[list]:
        """Generate alternative indicator suggestions based on the returned data.

        Shows related indicators the user might also want to explore.
        E.g., after GDP (current US$), suggest GDP growth, GDP per capita, GDP PPP.

        Performance optimizations:
        1. Skip entirely for catalog-resolved indicators (high confidence).
           These are already the correct indicator — alternatives add latency
           without value for the majority of queries.
        2. Uses FTS5 full-text search instead of LIKE '%...%' scan.
           FTS5 is indexed and runs in <50ms vs 2-6s for LIKE on 330K rows.
        """
        from .indicator_database import IndicatorDatabase
        from ..models import AlternativeSeries

        try:
            if not data:
                return None

            # Optimization 1: Skip alternatives for catalog-resolved indicators.
            # When the indicator was resolved via the catalog (high confidence),
            # the user got exactly what they asked for — building alternatives
            # is wasted work (saves 50-200ms FTS5 time + DB connection overhead).
            if getattr(intent, "_catalog_resolved", False):
                logger.debug(
                    "Skipping alternative series — catalog-resolved indicator: %s",
                    (intent.parameters or {}).get("indicator", "?"),
                )
                return None

            # Get the indicator code from returned data
            first_data = data[0] if isinstance(data, list) else data
            meta = getattr(first_data, "metadata", None) if not isinstance(first_data, dict) else first_data.get("metadata")
            if not meta:
                return None
            series_id = str(getattr(meta, "seriesId", "") or "")
            provider = str(getattr(meta, "source", "") or "")
            indicator_name = str(getattr(meta, "indicator", "") or "")

            if not series_id or not provider:
                return None

            # Get the concept family — indicators with similar name prefix
            core = indicator_name.split(",")[0].split("(")[0].strip().lower()
            if len(core) < 2:
                return None

            normalized_provider = normalize_provider_name(provider)

            # Use FTS5 search instead of LIKE '%...%' scan.
            # FTS5 is indexed and runs in <50ms vs 2-6s for LIKE on 330K rows.
            db = IndicatorDatabase()
            conn = db._get_connection()
            cur = conn.cursor()

            # Build FTS5 query from core words (strip punctuation, use OR)
            fts_words = [w.strip() for w in core.split() if w.strip() and len(w.strip()) > 2]
            if not fts_words:
                return None

            # Quote each word for FTS5 safety and join with AND for relevance
            fts_query = " AND ".join([f'"{w}"' for w in fts_words[:4]])

            try:
                cur.execute(
                    """SELECT i.code, i.name FROM indicators_fts f
                    JOIN indicators i ON f.rowid = i.id
                    WHERE indicators_fts MATCH ? AND i.provider = ? AND i.code != ?
                    ORDER BY bm25(indicators_fts) LIMIT 5""",
                    (fts_query, normalized_provider.upper(), series_id),
                )
                rows = cur.fetchall()
            except Exception:
                # FTS5 fallback: if match fails, try simpler query
                simple_fts = " OR ".join([f'"{w}"' for w in fts_words[:3]])
                try:
                    cur.execute(
                        """SELECT i.code, i.name FROM indicators_fts f
                        JOIN indicators i ON f.rowid = i.id
                        WHERE indicators_fts MATCH ? AND i.provider = ? AND i.code != ?
                        ORDER BY bm25(indicators_fts) LIMIT 5""",
                        (simple_fts, normalized_provider.upper(), series_id),
                    )
                    rows = cur.fetchall()
                except Exception:
                    rows = []

            if not rows:
                return None

            alternatives = []
            for code, name in rows:
                alternatives.append(AlternativeSeries(
                    code=code,
                    name=name,
                    provider=normalized_provider,
                ))

            return alternatives if alternatives else None
        except Exception:
            return None

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

                    # Extract indicator terms directly from the query.
                    # The metadata service's discover_for_query handles resolution
                    # via its own KNOWN_PRODUCTS lookup + API search fallback.
                    # No hardcoded keyword-to-indicator mapping needed here.
                    query_lower = query.lower()
                    query_words = set(query_lower.split())
                    metadata_service = get_statscan_metadata_service()
                    known_keys = {k.lower() for k in metadata_service.KNOWN_PRODUCTS}
                    indicator_found = None
                    # Match multi-word known keys first, then single words
                    for key in sorted(known_keys, key=len, reverse=True):
                        if key in query_lower:
                            indicator_found = key
                            break

                    if indicator_found:
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
            sub_params = {
                **(original_intent.parameters or {}),
                "entity": entity,  # Preserve for providers that support entity directly
            }
            if original_intent.decompositionType == "countries":
                # Country decomposition must bind each sub-query to a single country.
                sub_params["country"] = entity
                sub_params.pop("countries", None)

            # Create a modified intent for this entity
            sub_intent = ParsedIntent(
                apiProvider=original_intent.apiProvider,
                indicators=original_intent.indicators,
                parameters=sub_params,
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
