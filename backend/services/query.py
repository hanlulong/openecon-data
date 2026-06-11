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
    ExecutionPlan,
    GeneratedFile,
    NormalizedData,
    ParsedIntent,
    QueryResponse,
)
from ..config import Settings
from ..services.cache import cache_service
from ..services.redis_cache import get_redis_cache
from ..services.conversation import conversation_manager
from ..services.conversation_state_v2 import (
    extract_state_from_intent,
    materialize_intent,
    merge_new_state_with_previous,
    merge_state,
    reset_state_for_topic_change,
    update_answer_members_from_data,
)
from ..services.delta_extractor import DeltaExtractor
from ..services.openrouter import OpenRouterService
from ..services.query_complexity import QueryComplexityAnalyzer
from ..services.parameter_validator import ParameterValidator
from ..services.metadata_search import MetadataSearchService
from ..routing.unified_router import (
    route_provider as unified_route_provider,
    correct_coingecko_misrouting as unified_correct_coingecko_misrouting,
)
from ..services.query_pipeline import ParseRouteResult, QueryPipeline, ValidationResult
from ..routing.country_resolver import CountryResolver
from ..routing.unified_router import UnifiedRouter
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
from ..utils.geographies import CANADIAN_PROVINCES, normalize_canadian_region_list
from ..utils.imf_supportability import imf_exact_provider_surface_supportability_reason
from ..utils.providers import ALL_PROVIDERS
from ..utils.retry import retry_async, DataNotAvailableError
from ..services.http_pool import extended_timeout
from ..services.query_decomposition import (
    generate_sub_query,
    apply_ranking_projection as _qd_apply_ranking_projection,
    maybe_expand_ranking_country_scope as _qd_maybe_expand_ranking_country_scope,
)
from ..services.geography_validation import (
    assess_country_coverage as _gv_assess_country_coverage,
    build_country_coverage_warning_message as _gv_build_country_coverage_warning_message,
)
from ..services.query_parsing import (
    extract_indicator_text_from_refined_query,
    infer_multi_concept_indicators_from_query as _qp_infer_multi_concept_indicators_from_query,
)
# SemanticClarifier removed in Phase 2 LLM refactor — the LLM prompt's
# clarificationNeeded field + ambiguity policy now handles broad-concept
# detection.  See simplified_prompt.py.

_RAW_QUERY_CACHE_HASH_PROVIDERS = {"STATSCAN", "STATISTICS CANADA"}
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
from ..services.semantic_match_judge import (
    judge_execution_result as _smj_judge_execution_result,
    judge_resolved_indicator as _smj_judge_resolved_indicator,
)
from ..services.execution_planner import build_minimal_execution_plan as _ep_build_minimal_execution_plan
from ..services.indicator_resolution import (
    _effective_original_query as _ir_effective_original_query,
    build_exact_indicator_title_intent as _ir_build_exact_indicator_title_intent,
    code_semantic_hint as _ir_code_semantic_hint,
    is_provider_locked as _ir_is_provider_locked,
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
    apply_indicator_option_to_intent as _ic_apply_indicator_option_to_intent,
    provider_can_execute_indicator_option as _ic_provider_can_execute_indicator_option,
    build_no_reliable_indicator_match_response as _ic_build_no_reliable_indicator_match_response,
    collect_indicator_choice_options as _ic_collect_indicator_choice_options,
    infer_query_concept_groups as _ic_infer_query_concept_groups,
    build_multi_concept_query_clarification as _ic_build_multi_concept_query_clarification,
    is_simple_single_country_query as _ic_is_simple_single_country_query,
    looks_informational as _ic_looks_informational,
    handle_informational_intent as _ic_handle_informational_intent,
    _DefinitionSentinel as _ic_DefinitionSentinel,
    format_informational_results as _ic_format_informational_results,
    verify_semantic_discriminators as _ic_verify_semantic_discriminators,
    humanize_region_name as _ic_humanize_region_name,
    has_explicit_group_scope as _ic_has_explicit_group_scope,
    rewrite_group_scope_query as _ic_rewrite_group_scope_query,
    build_group_scope_clarification as _ic_build_group_scope_clarification,
    build_unspecified_geography_breakdown_clarification as _ic_build_unspecified_geography_breakdown_clarification,
    build_structured_semantic_clarification as _ic_build_structured_semantic_clarification,
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
from ..services.data_fetcher import (
    fetch_from_coingecko as _df_fetch_from_coingecko,
    fetch_exchange_rate_with_historical_fallback as _df_fetch_exchange_rate_with_historical_fallback,
    fetch_historical_exchange_from_fred as _df_fetch_historical_exchange_from_fred,
    extract_exchange_rate_params as _df_extract_exchange_rate_params,
    fetch_data as _df_fetch_data,
    fetch_multi_indicator_data as _df_fetch_multi_indicator_data,
    _has_statscan_mechanical_dimension_dispatch_authority as _df_has_statscan_mechanical_dimension_dispatch_authority,
    _statscan_periods_from_date_range as _df_statscan_periods_from_date_range,
)
from ..services.provider_strategy import (
    collect_target_countries as _ps_collect_target_countries,
    get_provider_for_single_country as _ps_get_provider_for_single_country,
    provider_supports_requested_scope as _ps_provider_supports_requested_scope,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (deduplicated from multiple call-sites)
# ---------------------------------------------------------------------------

# Year extraction – matches 4-digit years in the 1900–2099 range.
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


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


def _normalize_province_decomposition_entities(
    entities: Optional[List[str]],
) -> List[str]:
    """Normalize a Canadian province breakdown to the 10 provinces only."""
    province_set = set(CANADIAN_PROVINCES)
    normalized = normalize_canadian_region_list(entities)
    provinces_only = [region for region in normalized if region in province_set]
    return provinces_only or CANADIAN_PROVINCES.copy()


def _put_cached_parse_result(query_hash: str, result: Any) -> None:
    """Store a ParseRouteResult in the intent cache.

    Store a deep snapshot, not the live object. The caller keeps mutating
    `result.intent` after this returns (originalQuery, router overrides,
    provider locks, resolved codes, expanded country scopes), so caching the
    reference would let a later HIT replay the first request's post-execution
    state as if the parser had produced it — nondeterministic divergence on
    repeat queries. Reads already deepcopy on the way out; this freezes the
    entry on the way in so both ends of the cache are isolated.
    """
    import copy
    result = copy.deepcopy(result)
    # Simple size cap: drop oldest entries when over limit
    if len(_intent_cache) >= _INTENT_CACHE_MAX_SIZE:
        oldest_key = min(_intent_cache, key=lambda k: _intent_cache[k][1])
        del _intent_cache[oldest_key]
    _intent_cache[query_hash] = (result, time.time())


# Provider normalization — single source of truth
from ..utils.providers import normalize_provider_name


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
    CACHE_KEY_VERSION = "2026-05-16.1"
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

        # UnifiedRouter is the single source of routing truth. The legacy
        # SemanticProviderRouter (semantic-router + LiteLLM fallback) and
        # HybridRouter (deterministic candidates + LLM ranking) were removed
        # in the 2026-05-30 deep review — both were default-disabled, unused
        # in production, and added 765 LOC plus ~63MB of deps for no
        # measurable routing improvement (Workflow B Position B).
        self.unified_router = UnifiedRouter()
        # Small in-memory cache to avoid repeated cross-provider fallback scans.
        self._fallback_provider_cache: "OrderedDict[Tuple[str, Tuple[str, ...]], List[str]]" = OrderedDict()
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
        """Delegates to :func:`query_helpers.extract_countries_from_query`."""
        from .query_helpers import extract_countries_from_query as _qh_extract
        return _qh_extract(query)

    @staticmethod
    def _broad_exact_title_catalog_concept(query: str) -> Optional[str]:
        """Disabled compatibility shim for the old broad-concept shortcut."""
        return None

    @staticmethod
    def _explicit_year_range_from_text(text: str) -> tuple[str, str] | None:
        """Extract a mechanical year range embedded in exact-code query text."""
        years = [
            int(match.group(0))
            for match in re.finditer(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(text or ""))
        ]
        if len(years) < 2:
            return None
        start_year, end_year = years[0], years[1]
        if start_year > end_year:
            return None
        return f"{start_year:04d}-01-01", f"{end_year:04d}-12-31"

    @staticmethod
    def _explicit_code_residual_is_country_scope(text: str, token: str) -> bool:
        """Return True when removing an embedded code leaves only country scope.

        The explicit provider-code fast path is mechanical exact-input
        transport.  It should accept ``Germany SH.TBS.INCD from World Bank`` or
        ``United States PCPI_CP_01_BY2010_IX from IMF`` where the non-code
        remainder is just geography, but it must not promote abbreviations that
        appear inside units or prose (for example ``U.S. dollars``) into final
        provider-code authority.
        """
        if not token:
            return False
        residual = re.sub(
            rf"(?<![A-Za-z0-9_@.\-]){re.escape(str(token))}(?![A-Za-z0-9_@.\-])",
            " ",
            str(text or ""),
            count=1,
            flags=re.IGNORECASE,
        )
        residual = re.sub(r"\s+", " ", residual).strip(" ,;:")
        return not residual or bool(CountryResolver.normalize(residual))

    @staticmethod
    def _unsafe_dot_only_explicit_code_token(provider: str, candidate: str) -> bool:
        """Reject dotted abbreviation tokens that are not provider-native codes."""
        provider_upper = normalize_provider_name(provider)
        token = str(candidate or "").strip()
        if provider_upper != "IMF":
            return False
        # The local IMF executable catalog uses DataMapper-style identifiers
        # with letters/digits/underscores, not dot-delimited abbreviations. A
        # dot-only token such as "U.S." in "U.S. dollars" is unit/country prose,
        # not an explicit provider code.
        return "." in token and "_" not in token and not any(ch.isdigit() for ch in token)

    def _build_explicit_provider_code_intent(self, query: str) -> Optional[ParsedIntent]:
        """Fast path for raw queries that are just a provider code plus provider mention."""
        explicit_provider = normalize_provider_name(self._detect_explicit_provider(query) or "")
        if not explicit_provider:
            return None

        stripped_original = str(query or "").strip()
        provider_suffixes = [explicit_provider]
        if explicit_provider.upper() == "WORLDBANK":
            provider_suffixes.extend(["world bank", "worldbank"])
        if explicit_provider.upper() == "STATSCAN":
            provider_suffixes.extend(["statscan", "stats canada", "statistics canada"])
        for provider_suffix in dict.fromkeys(provider_suffixes):
            for verb in ("from", "use", "using", "via"):
                stripped_original = re.sub(
                    rf"\b{verb}\s+{re.escape(provider_suffix)}\b",
                    " ",
                    stripped_original,
                    flags=re.IGNORECASE,
                )
        stripped_original = re.sub(r"\s+", " ", stripped_original).strip(" ,;:")
        stripped = stripped_original.lower()
        if not stripped:
            return None

        candidate = stripped_original if explicit_provider in {"WORLDBANK", "COINGECKO"} else str(stripped_original).upper()
        fred_catalog_codes: list[str] = []
        worldbank_catalog_codes: list[str] = []
        coingecko_catalog_slugs: list[str] = []
        statscan_exact_product_id: str | None = None
        statscan_label: str | None = None
        if explicit_provider == "STATSCAN":
            product_match = re.match(r"^\s*(\d{8}|\d{10})(?:\b|(?=\D))(.*)$", stripped_original)
            if product_match:
                raw_product_id = product_match.group(1)
                statscan_exact_product_id = raw_product_id[:8]
                label = re.sub(r"\s+", " ", product_match.group(2) or "").strip(" ,;:-")
                statscan_label = label or statscan_exact_product_id
                candidate = statscan_exact_product_id
        if not self._looks_like_provider_indicator_code(explicit_provider, candidate):
            code_candidates = []
            for token in re.findall(
                r"(?<![A-Za-z0-9_@.\-])[A-Za-z][A-Za-z0-9_@.\-]{2,}(?![A-Za-z0-9_@.\-])",
                str(query or ""),
            ):
                normalized_token = token if explicit_provider in {"WORLDBANK", "COINGECKO"} else token.upper()
                if not ("_" in token or "." in token or "@" in token or "-" in token):
                    continue
                if self._unsafe_dot_only_explicit_code_token(explicit_provider, normalized_token):
                    continue
                if not self._looks_like_provider_indicator_code(explicit_provider, normalized_token):
                    continue
                if not self._explicit_code_residual_is_country_scope(stripped_original, token):
                    continue
                code_candidates.append(normalized_token)
            if explicit_provider == "IMF":
                code_candidates.extend(
                    self._imf_uppercase_catalog_code_tokens(str(query or ""), stripped)
                )
            if explicit_provider == "FRED":
                fred_catalog_codes = self._fred_uppercase_catalog_code_tokens(str(query or ""), stripped)
                code_candidates.extend(fred_catalog_codes)
            if explicit_provider == "COMTRADE":
                code_candidates.extend(
                    re.sub(r"^HS\s*", "", token.upper())
                    for token in re.findall(
                        r"(?<![A-Za-z0-9])HS\s*[0-9]{2,6}(?![A-Za-z0-9])",
                        str(query or ""),
                        flags=re.IGNORECASE,
                    )
                )
            if explicit_provider == "WORLDBANK":
                worldbank_catalog_codes = self._worldbank_uppercase_catalog_code_tokens(str(query or ""), stripped)
                code_candidates.extend(worldbank_catalog_codes)
            if explicit_provider == "COINGECKO":
                coingecko_catalog_slugs = self._coingecko_catalog_slug_tokens(
                    str(query or ""),
                    stripped_original,
                )
                code_candidates.extend(coingecko_catalog_slugs)
            if len(dict.fromkeys(code_candidates)) == 1:
                candidate = code_candidates[0]

        catalog_backed_worldbank_code = (
            explicit_provider == "WORLDBANK"
            and candidate in worldbank_catalog_codes
        )
        catalog_backed_fred_code = (
            explicit_provider == "FRED"
            and candidate in fred_catalog_codes
        )
        catalog_backed_coingecko_slug = (
            explicit_provider == "COINGECKO"
            and candidate in coingecko_catalog_slugs
        )
        if (
            not catalog_backed_worldbank_code
            and not catalog_backed_fred_code
            and not catalog_backed_coingecko_slug
            and self._unsafe_dot_only_explicit_code_token(explicit_provider, candidate)
        ):
            return None
        if (
            not catalog_backed_worldbank_code
            and not catalog_backed_fred_code
            and not catalog_backed_coingecko_slug
            and not self._looks_like_provider_indicator_code(explicit_provider, candidate)
        ):
            return None

        if explicit_provider == "WORLDBANK":
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{1,127}", candidate):
                return None
        elif explicit_provider == "COINGECKO":
            if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,127}", candidate):
                return None
        elif explicit_provider == "COMTRADE":
            candidate = re.sub(r"^HS", "", candidate.upper())
            if not re.fullmatch(r"[0-9]{2,6}", candidate):
                return None
        elif not re.fullmatch(r"[A-Z0-9_@.\-]{3,}", candidate):
            return None

        country_scope_text = re.sub(
            re.escape(str(candidate)),
            " ",
            stripped_original,
            count=1,
            flags=re.IGNORECASE,
        )
        countries = self._extract_countries_from_query(country_scope_text)
        params = {
            "indicator": candidate,
            "__semantic_indicator_label": statscan_label if statscan_exact_product_id else candidate,
            "__semantic_provider_locked": True,
            "__exact_provider_code_match": True,
        }
        if statscan_exact_product_id:
            params["__statscan_product_id"] = statscan_exact_product_id
        explicit_year_range = self._explicit_year_range_from_text(stripped_original)
        if explicit_year_range is not None:
            params["startDate"], params["endDate"] = explicit_year_range
        if len(countries) == 1:
            params["country"] = countries[0]
        elif len(countries) > 1:
            params["countries"] = countries
        if explicit_provider == "COMTRADE":
            query_lower = str(query or "").lower()
            if re.search(r"\b(?:re-?)?exports?\b", query_lower):
                params["flow"] = "EXPORT"
            elif re.search(r"\b(?:re-?)?imports?\b", query_lower):
                params["flow"] = "IMPORT"

        return ParsedIntent(
            apiProvider=explicit_provider,
            indicators=[candidate],
            parameters=params,
            clarificationNeeded=False,
            confidence=0.99,
            recommendedChartType="line",
            queryType="data_fetch",
            originalQuery=query,
            isFollowUp=False,
            followUpType=None,
            resolvedQuery=None,
            needsDecomposition=False,
            decompositionType=None,
            decompositionEntities=None,
            useProMode=False,
        )

    def _worldbank_uppercase_catalog_code_tokens(self, query: str, stripped_query: str = "") -> list[str]:
        """Return exact uppercase WorldBank catalog-code tokens embedded in a query.

        Some WorldBank public sources use short provider-native codes such as
        ``TOT`` that do not contain dots, underscores, or digits.  Accept those
        only when the original query contains the uppercase token and the token
        already exists as a WorldBank code in the local catalog.  This keeps the
        path mechanical and prevents lower-case natural-language words from
        being promoted to provider codes.
        """
        tokens = [
            match.group(0)
            for match in re.finditer(r"\b[A-Z][A-Z0-9]{1,15}\b", str(query or ""))
        ]
        if not tokens:
            return []

        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
        except Exception:
            return []

        matches: list[str] = []
        for token in tokens:
            if token in {"WORLD", "BANK", "WORLDBANK"}:
                continue
            residual = re.sub(
                rf"\b{re.escape(token.lower())}\b",
                " ",
                str(stripped_query or "").lower(),
            )
            residual = re.sub(r"\s+", " ", residual).strip(" ,;:")
            if residual and not CountryResolver.normalize(residual):
                continue
            try:
                metadata = lookup.get("WorldBank", token)
            except Exception:
                metadata = None
            if not metadata:
                continue
            exact_code = str(metadata.get("code") or token).strip()
            if exact_code:
                matches.append(exact_code)
        return matches

    def _fred_uppercase_catalog_code_tokens(self, query: str, stripped_query: str = "") -> list[str]:
        """Return exact uppercase FRED catalog-code tokens embedded in a query.

        Some FRED series IDs are long uppercase words that happen to end with an
        English suffix (for example ``PATENTUSMEUTILITY``).  Accept them only
        when the original token is already present in the local FRED catalog and
        the non-code remainder is empty or just a recognized country phrase.
        """
        tokens = [
            match.group(0)
            for match in re.finditer(r"\b[A-Z][A-Z0-9]{2,63}\b", str(query or ""))
        ]
        if not tokens:
            return []

        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
        except Exception:
            return []

        matches: list[str] = []
        for token in tokens:
            if token in {"FRED"}:
                continue
            residual = re.sub(
                rf"\b{re.escape(token.lower())}\b",
                " ",
                str(stripped_query or "").lower(),
            )
            residual = re.sub(r"\s+", " ", residual).strip(" ,;:")
            if residual and not CountryResolver.normalize(residual):
                continue
            try:
                metadata = lookup.get("FRED", token)
            except Exception:
                metadata = None
            if not metadata:
                continue
            exact_code = str(metadata.get("code") or token).strip()
            if exact_code:
                matches.append(exact_code)
        return matches

    def _coingecko_catalog_slug_tokens(self, query: str, stripped_query: str = "") -> list[str]:
        """Return exact CoinGecko slug tokens embedded in a provider-locked query.

        Lowercase CoinGecko ids can look like ordinary words (for example
        ``status``), so this path only accepts tokens already present as exact
        local CoinGecko catalog codes and only when the remaining text is
        generic crypto/price wording. This preserves mechanical exact-id
        transport without adding semantic coin-name selection.
        """
        tokens = [
            match.group(0).lower()
            for match in re.finditer(r"\b[a-zA-Z0-9][a-zA-Z0-9\-]{1,127}\b", str(query or ""))
        ]
        if not tokens:
            return []

        generic_residual_tokens = {
            "crypto",
            "cryptocurrency",
            "coin",
            "token",
            "price",
            "current",
            "latest",
            "usd",
        }
        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
        except Exception:
            return []

        matches: list[str] = []
        for token in tokens:
            if token in {"coingecko", "crypto", "cryptocurrency", "coin", "token", "price", "usd"}:
                continue
            residual = re.sub(
                rf"\b{re.escape(token)}\b",
                " ",
                str(stripped_query or "").lower(),
            )
            residual_words = {
                word
                for word in re.sub(r"[^a-z0-9]+", " ", residual).split()
                if word
            }
            if residual_words - generic_residual_tokens:
                continue
            try:
                metadata = lookup.get("CoinGecko", token)
            except Exception:
                metadata = None
            if not metadata:
                continue
            exact_code = str(metadata.get("code") or token).strip().lower()
            if exact_code:
                matches.append(exact_code)
        if matches:
            return matches

        residual_text = str(stripped_query or "")
        residual_words = [
            word
            for word in re.sub(r"[^a-z0-9]+", " ", residual_text.lower()).split()
            if word
        ]
        slug_request_tokens = {
            "crypto",
            "cryptocurrency",
            "price",
            "current",
            "latest",
            "usd",
        }
        asset_words = [
            word
            for word in residual_words
            if word not in slug_request_tokens
        ]
        raw_asset_words = [
            word
            for word in re.findall(r"[A-Za-z0-9]+", residual_text)
            if word.lower() not in slug_request_tokens
        ]
        # Mechanical slug transport only: require the complete provider-locked
        # asset phrase to be present with an explicit crypto/price wrapper, and
        # require that phrase to hyphen-join exactly to a local CoinGecko slug.
        # This covers generated phrases such as "Aevo Exchange" ->
        # "aevo-exchange" and "Aga Token" -> "aga-token" without making
        # "Aevo" imply the exchange slug or allowing partial/generic
        # "Exchange" lookups.
        if len(asset_words) >= 2 and len(asset_words) != len(residual_words):
            phrase = " ".join(asset_words)
            raw_phrase = " ".join(raw_asset_words).strip()
            try:
                exact_title_rows = lookup.exact_name_matches(
                    list(
                        dict.fromkeys(
                            candidate
                            for candidate in (
                                raw_phrase,
                                phrase,
                                phrase.title(),
                            )
                            if candidate
                        )
                    ),
                    provider="CoinGecko",
                    limit=20,
                )
            except Exception:
                exact_title_rows = []
            if exact_title_rows:
                return []
            candidate_slug = "-".join(asset_words)
            try:
                metadata = lookup.get("CoinGecko", candidate_slug)
            except Exception:
                metadata = None
            if metadata:
                exact_code = str(metadata.get("code") or candidate_slug).strip().lower()
                if exact_code == candidate_slug:
                    matches.append(exact_code)
        return matches

    def _imf_uppercase_catalog_code_tokens(self, query: str, stripped_query: str = "") -> list[str]:
        """Return exact uppercase IMF DataMapper-code tokens embedded in a query.

        Queries such as ``Nigeria TTT from IMF`` are still explicit provider-code
        requests even though the code has no underscore/digit namespace. Keep
        this narrow: only original uppercase alphanumeric tokens that already
        exist in the local IMF catalog and are served by executable DataMapper
        categories (WEO or regional outlook) are accepted. Natural-language
        words are intentionally not uppercased and reinterpreted as codes here.
        The non-code remainder must be empty or just a recognized country
        phrase, so natural titles like ``Nigeria Real Non-Oil GDP Growth from
        IMF`` do not get hijacked by the embedded ``GDP`` acronym.
        """
        tokens = [
            match.group(0)
            for match in re.finditer(r"\b[A-Z][A-Z0-9]{2,10}\b", str(query or ""))
        ]
        if not tokens:
            return []

        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
        except Exception:
            return []

        matches: list[str] = []
        for token in tokens:
            if token in {"IMF"}:
                continue
            residual = re.sub(
                rf"\b{re.escape(token.lower())}\b",
                " ",
                str(stripped_query or "").lower(),
            )
            residual = re.sub(r"\s+", " ", residual).strip(" ,;:")
            if residual and not CountryResolver.normalize(residual):
                continue
            try:
                metadata = lookup.get("IMF", token)
            except Exception:
                metadata = None
            if not metadata:
                continue
            category = str(metadata.get("category") or "").strip().upper()
            if category == "WEO" or category.endswith("REO"):
                matches.append(token)
        return matches

    def _build_exact_indicator_title_intent(self, query: str) -> Optional[ParsedIntent]:
        """Fast path for raw queries that already match a provider-native title."""
        return _ir_build_exact_indicator_title_intent(
            query,
            explicit_provider=self._detect_explicit_provider(query),
            countries=self._extract_countries_from_query(query),
            all_providers=list(ALL_PROVIDERS),
        )

    def _add_provider_transparency(
        self,
        result: QueryResponse,
        original_query: str,
    ) -> QueryResponse:
        """Delegates to :func:`query_helpers.add_provider_transparency`."""
        from .query_helpers import add_provider_transparency as _qh_transparency
        return _qh_transparency(self, result, original_query)

    def _apply_country_overrides(self, intent: ParsedIntent, query: str) -> None:
        """Delegates to :func:`query_helpers.apply_country_overrides`."""
        from .query_helpers import apply_country_overrides as _qh_apply
        _qh_apply(self, intent, query)

    @staticmethod
    def _extract_indicator_text_from_refined_query(refined_query: str) -> str:
        """Strip scope suffixes from a clarification-refined query."""
        return extract_indicator_text_from_refined_query(refined_query)

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
        """Delegates to :func:`query_helpers.looks_like_country_follow_up`."""
        from .query_helpers import looks_like_country_follow_up as _qh_follow_up
        return _qh_follow_up(query, target_countries)

    async def _fetch_from_coingecko(
        self,
        intent: ParsedIntent,
        params: dict,
    ) -> list:
        """Delegates to :func:`data_fetcher.fetch_from_coingecko`."""
        return await _df_fetch_from_coingecko(self.coingecko_provider, intent, params)

    async def _fetch_exchange_rate_with_historical_fallback(
        self,
        intent: ParsedIntent,
        params: dict,
    ) -> list:
        """Delegates to :func:`data_fetcher.fetch_exchange_rate_with_historical_fallback`."""
        return await _df_fetch_exchange_rate_with_historical_fallback(self, intent, params)

    async def _fetch_historical_exchange_from_fred(
        self,
        intent: ParsedIntent,
        params: dict,
    ) -> Optional[list]:
        """Delegates to :func:`data_fetcher.fetch_historical_exchange_from_fred`."""
        return await _df_fetch_historical_exchange_from_fred(self, intent, params)

    def _build_intent_from_semantic_clarification(
        self,
        pending: Dict[str, Any],
        selected_option: ClarificationOption,
        refined_query: str,
    ) -> Optional[ParsedIntent]:
        """Delegates to :func:`query_helpers.build_intent_from_semantic_clarification`."""
        from .query_helpers import build_intent_from_semantic_clarification as _qh_build_intent
        return _qh_build_intent(self, pending, selected_option, refined_query)

    async def _select_routed_provider(self, intent: ParsedIntent, query: str) -> str:
        """Select provider using the UnifiedRouter (single source of truth).

        Previously this method routed through two additional optional layers
        (SemanticProviderRouter + LiteLLM, then HybridRouter) — both removed
        in the 2026-05-30 deep review.
        """
        params = intent.parameters or {}
        raw_countries = params.get("countries")
        countries = raw_countries if isinstance(raw_countries, list) else []
        llm_provider = normalize_provider_name(intent.apiProvider or "")
        routed_provider = llm_provider
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
            deterministic_provider = normalize_provider_name(deterministic_decision.provider)
            deterministic_confidence = float(deterministic_decision.confidence or 0.0)
            deterministic_match_type = str(deterministic_decision.match_type or "deterministic").lower()
            logger.info(
                "🧭 UnifiedRouter baseline: %s (conf=%.2f, type=%s)",
                deterministic_provider,
                deterministic_decision.confidence,
                deterministic_decision.match_type,
            )
            # Short-circuit: router can still report unsupported structural cases.
            if deterministic_provider == "NOT_AVAILABLE":
                intent.apiProvider = "not_available"
                return "NOT_AVAILABLE"
            if getattr(deterministic_decision, "can_override_llm_provider", False):
                routed_provider = deterministic_provider
            elif getattr(deterministic_decision, "decision_source", "") == "llm_provider":
                routed_provider = deterministic_provider
            elif llm_provider in ("", "NONE", "UNKNOWN"):
                routed_provider = deterministic_provider
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
        if routed_provider == "NOT_AVAILABLE":
            intent.apiProvider = "not_available"
            return "NOT_AVAILABLE"
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

    def _rerank_data_by_query_relevance(
        self,
        query: str,
        data: List[Any],
        *,
        is_multi_indicator: bool = False,
    ) -> List[Any]:
        """Delegates to :func:`relevance_scorer.rerank_data_by_query_relevance`."""
        return _rs_rerank_data_by_query_relevance(query, data, is_multi_indicator=is_multi_indicator)

    def _extract_ranking_value(
        self,
        series: NormalizedData,
        target_year: Optional[int],
    ) -> tuple[Optional[float], Optional[DataPoint]]:
        """Delegates to :func:`relevance_scorer.extract_ranking_value`."""
        return _rs_extract_ranking_value(series, target_year)

    def _apply_ranking_projection(self, query: str, data: List[NormalizedData]) -> List[NormalizedData]:
        """Delegates to :func:`query_decomposition.apply_ranking_projection`."""
        return _qd_apply_ranking_projection(query, data)

    async def _maybe_recover_from_empty_data(
        self,
        query: str,
        intent: Optional[ParsedIntent],
    ) -> Optional[List[NormalizedData]]:
        """Delegates to :func:`query_helpers.maybe_recover_from_empty_data`."""
        from .query_helpers import maybe_recover_from_empty_data as _qh_recover
        return await _qh_recover(self, query, intent)

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

    def _build_structured_semantic_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_structured_semantic_clarification`."""
        return _ic_build_structured_semantic_clarification(
            self,
            conversation_id,
            query,
            intent,
            processing_steps,
        )

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
        """Delegates to :func:`provider_strategy.provider_supports_requested_scope`."""
        return _ps_provider_supports_requested_scope(provider, query, countries)

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

    def _build_unspecified_geography_breakdown_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent] = None,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Delegates to :func:`indicator_clarification.build_unspecified_geography_breakdown_clarification`."""
        return _ic_build_unspecified_geography_breakdown_clarification(
            conversation_id,
            query,
            intent,
            processing_steps,
        )

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

    def _build_verification_failed_response(
        self,
        conversation_id: str,
        intent: ParsedIntent,
        message: str,
        processing_steps: Optional[List[Any]] = None,
    ) -> QueryResponse:
        """Return a fail-closed response when fetched data cannot be verified."""
        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            data=None,
            clarificationNeeded=False,
            error="verification_failed",
            message=f"⚠️ **Couldn't verify the result**\n\n{message}",
            processingSteps=processing_steps,
        )

    async def _execute_resolved_intent(
        self,
        query: str,
        conversation_id: str,
        intent: ParsedIntent,
        parse_result: ParseRouteResult,
        tracker: Optional['ProcessingTracker'] = None,
        skip_prefetch_clarification: bool = False,
        skip_post_fetch_clarification: bool = False,
    ) -> QueryResponse:
        """Run validation, clarification guardrails, and fetch for an already-built intent."""
        _prev_good_intent = conversation_manager.get_last_intent(conversation_id)
        _prev_good_state = conversation_manager.get_conversation_state(conversation_id)
        conv_id = conversation_manager.add_message_safe(conversation_id, "user", query, intent=intent)
        pending_state = None

        # Dual-write: update ConversationState alongside last_intent.
        # Skip when called from the delta path (skip_post_fetch_clarification=True)
        # because the delta path already saved the precisely-merged state with
        # dimensions and cube metadata intact. Re-extracting from the intent
        # would lose that carefully-merged context.
        if not skip_post_fetch_clarification:
            try:
                _new_state = extract_state_from_intent(intent, statscan_provider=self.statscan_provider)
                _existing_state = conversation_manager.get_conversation_state(conv_id)
                _new_state = merge_new_state_with_previous(_new_state, _existing_state)
                # Pre-cache cube metadata for StatsCan dimension-capable indicators.
                # This allows the delta extractor to check dimensions synchronously
                # on the next follow-up (no async API call needed).
                if (_new_state.statscan_product_id
                        and _new_state.provider
                        and _new_state.provider.upper() in {"STATSCAN", "STATISTICS CANADA"}
                        and not _new_state.statscan_cube_metadata):
                    try:
                        _cube = await self.statscan_provider._get_cube_metadata(
                            _new_state.statscan_product_id
                        )
                        if _cube:
                            _new_state.statscan_cube_metadata = _cube
                    except Exception as _cube_exc:
                        logger.debug("Non-critical: statscan cube metadata fetch failed: %s", _cube_exc)
                pending_state = _new_state
                if not self._use_staged_state_commit():
                    conversation_manager.set_conversation_state(conv_id, _new_state)
            except Exception as _sw_err:
                logger.warning("Dual-write conversation_state failed: %s", _sw_err, exc_info=True)

        if intent.clarificationNeeded:
            semantic_clarification = self._build_structured_semantic_clarification(
                conversation_id=conv_id,
                query=query,
                intent=intent,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if semantic_clarification is not None:
                return semantic_clarification
            conversation_manager.clear_pending_indicator_options(conv_id)
            conversation_manager.clear_pending_semantic_clarification(conv_id)
            return QueryResponse(
                conversationId=conv_id,
                intent=intent,
                clarificationNeeded=True,
                clarificationQuestions=intent.clarificationQuestions,
                processingSteps=tracker.to_list() if tracker else None,
            )

        execution_plan = self._build_minimal_execution_plan(query, intent)

        if intent.needsDecomposition and intent.decompositionType == "provinces":
            intent.decompositionEntities = _normalize_province_decomposition_entities(
                intent.decompositionEntities
            )
        await self._maybe_expand_statscan_dimension_decomposition_entities(conv_id, intent)

        if intent.needsDecomposition and intent.decompositionEntities:
            if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
                logger.info("📅 Applying default time periods to decomposition query...")
                ParameterValidator.apply_default_time_periods(intent)

            logger.info("🔄 Query decomposition detected: %s %s into %d entities",
                       intent.decompositionType, query, len(intent.decompositionEntities))
            logger.info("🚀 Using batch method (Pro Mode disabled for decomposition)")

            data = await self._decompose_and_aggregate(query, intent, conv_id, tracker)
            verification_error = await self._verify_execution_result(
                query,
                intent,
                execution_plan,
                data,
            )
            if verification_error:
                if _prev_good_intent is not None:
                    conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                if _prev_good_state is not None:
                    conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                return self._build_verification_failed_response(
                    conversation_id=conv_id,
                    intent=intent,
                    message=verification_error,
                    processing_steps=tracker.to_list() if tracker else None,
                )
            self._persist_verified_conversation_state(
                conv_id,
                pending_state,
                data,
                intent=intent,
            )

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

        try:
            if validation.is_multi_indicator:
                logger.info("📊 Multi-indicator query detected: %s indicators", len(intent.indicators))
                data = await self._fetch_multi_indicator_data(intent)
            else:
                data = await retry_async(
                    lambda: self._fetch_data(intent, execution_plan=execution_plan),
                    max_attempts=3,
                    initial_delay=1.0,
                )
        except DataNotAvailableError as fetch_exc:
            logger.warning("Data not available in _execute_resolved_intent: %s", fetch_exc)
            data = []  # Treat as empty data — let fallback logic below handle it

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
                    verification_error = await self._verify_execution_result(
                        query,
                        intent,
                        execution_plan,
                        fallback_data,
                    )
                    if verification_error:
                        if _prev_good_intent is not None:
                            conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                        if _prev_good_state is not None:
                            conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                        return self._build_verification_failed_response(
                            conversation_id=conv_id,
                            intent=intent,
                            message=verification_error,
                            processing_steps=tracker.to_list() if tracker else None,
                        )
                    self._persist_verified_conversation_state(
                        conv_id,
                        pending_state,
                        fallback_data,
                        intent=intent,
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
                verification_error = await self._verify_execution_result(
                    query,
                    intent,
                    execution_plan,
                    recovered_data,
                )
                if verification_error:
                    if _prev_good_intent is not None:
                        conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                    if _prev_good_state is not None:
                        conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                    return self._build_verification_failed_response(
                        conversation_id=conv_id,
                        intent=intent,
                        message=verification_error,
                        processing_steps=tracker.to_list() if tracker else None,
                    )
                self._persist_verified_conversation_state(
                    conv_id,
                    pending_state,
                    recovered_data,
                    intent=intent,
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
            country = intent.parameters.get("country") or (intent.parameters.get("countries") or [""])[0] if intent.parameters else ""

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
        coverage_warning = None
        if not skip_post_fetch_clarification:
            recovered_uncertain_data = await self._maybe_recover_from_uncertain_match(
                query,
                intent,
                data,
            )
            if recovered_uncertain_data:
                data = recovered_uncertain_data
            clarification_response = self._build_uncertain_result_clarification(
                conversation_id=conv_id,
                query=query,
                intent=intent,
                data=data,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if clarification_response:
                return clarification_response
        else:
            logger.info("Skipping post-fetch clarification for delta-resolved intent")

        data, coverage_warning = await self._maybe_improve_country_coverage(
            query,
            intent,
            data,
        )

        verification_error = await self._verify_execution_result(
            query,
            intent,
            execution_plan,
            data,
        )
        if verification_error:
            if _prev_good_intent is not None:
                conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
            if _prev_good_state is not None:
                conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
            return self._build_verification_failed_response(
                conversation_id=conv_id,
                intent=intent,
                message=verification_error,
                processing_steps=tracker.to_list() if tracker else None,
            )
        self._persist_verified_conversation_state(
            conv_id,
            pending_state,
            data,
            intent=intent,
        )

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

    async def _judge_resolved_indicator_match(
        self,
        provider: str,
        indicator_query: str,
        resolved_code: str,
        resolved_name: str = "",
        resolved_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Use the shared model-backed resolved-indicator judge when available."""
        judgment = await _smj_judge_resolved_indicator(
            self,
            provider=provider,
            indicator_query=indicator_query,
            resolved_code=resolved_code,
            resolved_name=resolved_name,
            resolved_metadata=resolved_metadata,
        )
        if judgment is None:
            logger.warning(
                "Resolved-indicator judge unavailable or inconclusive for provider=%s code=%s",
                provider,
                resolved_code,
            )
            return False

        if judgment.decision != "match" or judgment.confidence < 0.75:
            logger.info(
                "Resolved-indicator judge rejected match: provider=%s code=%s decision=%s confidence=%.2f reason=%s",
                provider,
                resolved_code,
                judgment.decision,
                judgment.confidence,
                judgment.reason,
            )
            return False

        return True

    def _use_minimal_execution_plan(self) -> bool:
        return bool(getattr(self.settings, "use_minimal_execution_plan", False))

    def _use_post_fetch_semantic_judge(self) -> bool:
        return bool(getattr(self.settings, "use_post_fetch_semantic_judge", False))

    def _use_staged_state_commit(self) -> bool:
        return bool(getattr(self.settings, "use_staged_state_commit", False))

    def _build_minimal_execution_plan(
        self,
        query: str,
        intent: ParsedIntent,
    ) -> Optional[ExecutionPlan]:
        if not self._use_minimal_execution_plan():
            return None
        return _ep_build_minimal_execution_plan(query, intent)

    @staticmethod
    def _series_with_values_count(data: List[NormalizedData]) -> int:
        return sum(
            1
            for series in (data or [])
            if any(getattr(point, "value", None) is not None for point in (series.data or []))
        )

    def _returned_country_scope(self, data: List[NormalizedData]) -> list[str]:
        countries: list[str] = []
        for series in data or []:
            metadata = getattr(series, "metadata", None)
            country_text = str(getattr(metadata, "country", "") or "").strip()
            if not country_text:
                continue
            normalized = self._normalize_country_to_iso2(country_text) or country_text.upper()
            if normalized not in countries:
                countries.append(normalized)
        return countries

    def _verify_execution_plan_structure(
        self,
        execution_plan: ExecutionPlan,
        data: List[NormalizedData],
    ) -> Optional[str]:
        verification_checks = set(execution_plan.verification_checks or [])
        expected_shape = dict(execution_plan.expected_shape or {})

        min_series_count = int(expected_shape.get("min_series_count", 1) or 1)
        series_with_values = self._series_with_values_count(data)
        if series_with_values < min_series_count:
            return (
                f"Expected at least {min_series_count} populated series, "
                f"but received {series_with_values}."
            )

        if "decomposition_cardinality" in verification_checks and series_with_values < 2:
            return "Expected a decomposition result with multiple populated members, but the result collapsed to a single member."

        if "country_scope" in verification_checks:
            requested_countries = self._normalize_country_targets(
                list(expected_shape.get("requested_countries") or [])
            )
            returned_countries = self._returned_country_scope(data)
            if requested_countries and not returned_countries:
                return (
                    "The result could not verify country scope because the fetched series "
                    "did not include country metadata."
                )

            if requested_countries and returned_countries:
                unexpected_countries = [
                    country for country in returned_countries if country not in requested_countries
                ]
                if unexpected_countries:
                    return (
                        "Fetched country scope drifted beyond the requested set. "
                        f"Requested={requested_countries}, returned={returned_countries}."
                    )

                missing_countries = [
                    country for country in requested_countries if country not in returned_countries
                ]
                if missing_countries:
                    return (
                        "The result did not cover the full requested country scope. "
                        f"Missing={missing_countries}, returned={returned_countries}."
                    )

        return None

    async def _verify_execution_result(
        self,
        query: str,
        intent: ParsedIntent,
        execution_plan: Optional[ExecutionPlan],
        data: List[NormalizedData],
    ) -> Optional[str]:
        if not execution_plan or not data:
            return None

        structural_failure = self._verify_execution_plan_structure(execution_plan, data)
        if structural_failure:
            return structural_failure

        verification_query = str(
            intent.resolvedQuery or query or intent.originalQuery or ""
        ).strip()
        if self._has_implausible_top_series(verification_query, data):
            top_meta = getattr(data[0], "metadata", None) if data else None
            top_indicator = str(getattr(top_meta, "indicator", "") or "").strip()
            top_series_id = str(getattr(top_meta, "seriesId", "") or "").strip()
            rendered = top_indicator or top_series_id or "top fetched series"
            return (
                f"The fetched result is semantically implausible for the requested query. "
                f"Requested query: {verification_query}. Top series: {rendered}."
            )

        if not self._use_post_fetch_semantic_judge():
            return None

        judgment = await _smj_judge_execution_result(
            self,
            original_query=verification_query,
            execution_plan=execution_plan.model_dump(mode="json"),
            fetched_result=data,
        )
        if judgment is None:
            return "Fetched result could not be semantically verified."

        if judgment.decision == "pass" and judgment.confidence >= 0.7:
            return None

        failed_checks = ", ".join(judgment.failed_checks or [])
        detail = f"{judgment.reason or 'Fetched result could not be verified.'}"
        if failed_checks:
            detail += f" Failed checks: {failed_checks}."
        return detail

    def _commit_staged_conversation_state(
        self,
        conversation_id: str,
        state: Optional[Any],
    ) -> None:
        if state is None or not self._use_staged_state_commit():
            return
        conversation_manager.set_conversation_state(conversation_id, state)

    def _persist_verified_conversation_state(
        self,
        conversation_id: str,
        state: Optional[Any],
        data: Optional[List[NormalizedData]] = None,
        *,
        intent: Optional[ParsedIntent] = None,
    ) -> None:
        """Persist post-verification conversation state, including answer members.

        The standard path, delta path, and fallback paths all need the same
        guarantee: once a response is accepted as the current answer, the
        durable conversation state must reflect the exact returned member set.
        """
        if state is None:
            return
        params = (intent.parameters or {}) if intent is not None else {}
        semantic_indicator_label = str(params.get("__semantic_indicator_label") or "").strip()
        if semantic_indicator_label:
            state.indicator = semantic_indicator_label
        if intent is not None and intent.indicators:
            state.last_indicators_resolved = list(intent.indicators)
        resolved_indicator = str(params.get("indicator") or "").strip()
        if resolved_indicator:
            state.resolved_indicator_code = resolved_indicator
        provider_name = ""
        if data:
            data_providers = {
                normalize_provider_name(
                    getattr(getattr(series, "metadata", None), "source", "") or ""
                )
                for series in data
                if normalize_provider_name(
                    getattr(getattr(series, "metadata", None), "source", "") or ""
                )
            }
            if len(data_providers) == 1:
                provider_name = next(iter(data_providers))
        if not provider_name:
            provider_name = normalize_provider_name(
                getattr(intent, "apiProvider", "") or getattr(state, "provider", "") or getattr(state, "routed_provider", "") or ""
            )
        if provider_name:
            state.provider = provider_name
            state.routed_provider = provider_name
        if data:
            update_answer_members_from_data(
                state,
                data,
                intent=intent,
            )
        conversation_manager.set_conversation_state(conversation_id, state)

    @staticmethod
    def _member_country_list(member: Any) -> List[str]:
        countries = list(getattr(member, "countries", None) or [])
        if not countries and getattr(member, "country", None):
            countries = [getattr(member, "country")]
        return [str(country).strip() for country in countries if str(country or "").strip()]

    def _member_country_keys(self, member: Any) -> List[str]:
        keys: List[str] = []
        for country in self._member_country_list(member):
            iso2 = self._normalize_country_to_iso2(country)
            keys.append((iso2 or country).upper())
        return list(dict.fromkeys(keys))

    # Providers whose series codes are GEOGRAPHY-ENCODED: a single code names a
    # specific country (FRED "UNRATE" = US only, StatsCan vectors are bound to a
    # province/country).  For these, a collective share-key MUST include the
    # member's country so one member's code is never reused for a different
    # country (F6).  Country-AGNOSTIC providers (World Bank, IMF, Eurostat,
    # OECD, BIS) take the country as a separate parameter, so the SAME code
    # (e.g. "NY.GDP.PCAP.CD") is correct for every country and may be shared.
    _GEOGRAPHY_ENCODED_PROVIDERS = {"FRED", "STATSCAN", "COMTRADE", "COINGECKO"}

    def _collective_share_country_key(self, member: Any) -> str:
        """Canonical country component for a collective share-key (F6).

        Returns the member's canonical geography ONLY for geography-encoded
        providers (FRED/StatsCan/...); returns "" for country-agnostic
        providers so their codes can still be shared across countries.
        """
        provider = normalize_provider_name(str(getattr(member, "provider", "") or "")).upper()
        if provider not in self._GEOGRAPHY_ENCODED_PROVIDERS:
            return ""
        keys = self._member_country_keys(member)
        if not keys:
            return ""
        return "|".join(sorted(keys))

    def _current_collective_indicator_label(self, state: Any) -> str:
        labels = [
            str(getattr(member, "indicator_label", "") or "").strip()
            for member in (getattr(state, "active_answer_members", None) or [])
            if str(getattr(member, "indicator_label", "") or "").strip()
        ]
        if labels:
            return labels[0]
        return str(getattr(state, "indicator", "") or "").strip()

    def _distinct_member_scope_count(self, members: List[Any]) -> int:
        scope_keys = {
            key
            for member in members
            for key in self._member_country_keys(member)
        }
        return len(scope_keys)

    @staticmethod
    def _looks_like_collective_indicator_switch(raw_query: Optional[str]) -> bool:
        text = str(raw_query or "").strip().lower()
        if not text:
            return False
        return bool(
            re.search(r"\b(all|everything|everyone|entire set|whole set)\b", text)
            or "switch all" in text
            or "change all" in text
        )

    def _should_preserve_current_provider_for_delta(
        self,
        state: Optional[Any],
        delta: Optional[Any],
        delta_intent: Optional[ParsedIntent],
        query: str,
    ) -> bool:
        if state is None or delta is None or delta_intent is None:
            return False
        if delta.changed_provider is not None or delta.is_new_query:
            return False
        if (
            delta.changed_decomposition is not None
            or delta.added_dimensions is not None
            or delta.removed_dimensions is not None
        ):
            return False

        current_provider = normalize_provider_name(
            getattr(state, "provider", "") or getattr(state, "routed_provider", "") or ""
        )
        if not current_provider:
            return False

        if (
            getattr(delta, "delta_type", None) == "chart_change"
            and getattr(delta, "query_type", None) == "parameter_delta"
        ):
            return True

        params = delta_intent.parameters or {}
        semantic_label = str(
            params.get("__semantic_indicator_label")
            or getattr(delta, "changed_indicator", None)
            or (delta_intent.indicators[0] if delta_intent.indicators else "")
            or getattr(state, "indicator", "")
            or query
        ).strip()

        if current_provider == "COMTRADE":
            bilateral_trade_context = bool(
                getattr(state, "trade_reporter", None)
                or getattr(state, "trade_partner", None)
                or getattr(delta, "changed_trade_partner", None)
                or getattr(delta, "changed_trade_flow", None)
                or "trade" in semantic_label.lower()
                or "export" in semantic_label.lower()
                or "import" in semantic_label.lower()
            )
            if bilateral_trade_context:
                return True

        try:
            from .catalog_service import find_concept_by_term, is_provider_available

            concept = find_concept_by_term(semantic_label)
            if concept and is_provider_available(concept, current_provider):
                return True
        except Exception as exc:
            logger.debug("Delta provider preservation lookup failed: %s", exc)

        return False

    def _should_force_statscan_for_canada_decomposition_delta(
        self,
        state: Optional[Any],
        delta: Optional[Any],
    ) -> bool:
        """Force StatsCan for Canada-scoped decomposition follow-ups.

        Short delta queries like "Show by province" lose the explicit Canada
        mention, so generic re-routing often keeps the prior OECD provider.
        Structural Canada geography/dimension breakdowns should prefer
        Statistics Canada as the framework-level canonical provider.
        """
        if state is None or delta is None:
            return False
        if (
            delta.changed_decomposition is None
            and delta.added_dimensions is None
            and delta.removed_dimensions is None
        ):
            return False

        scope = []
        if getattr(state, "countries", None):
            scope.extend(list(getattr(state, "countries", None) or []))
        elif getattr(state, "country", None):
            scope.append(getattr(state, "country"))
        normalized_scope = {
            str(self._normalize_country_to_iso2(country) or country or "").upper()
            for country in scope
            if str(country or "").strip()
        }
        if normalized_scope != {"CA"}:
            return False

        decomposition = getattr(delta, "changed_decomposition", None) or {}
        decomp_type = str(decomposition.get("type") or "").strip().lower()
        axis = str(decomposition.get("axis") or "").strip().lower()
        if decomp_type in {"provinces", "regions", "states", "dimension"}:
            return True
        if axis in {"geography", "age group", "gender", "sex"}:
            return True
        return False

    def _should_use_collective_answer_member_delta(
        self,
        state: Optional[Any],
        delta: Optional[Any],
    ) -> bool:
        if state is None or delta is None:
            return False

        active_members = list(getattr(state, "active_answer_members", None) or [])
        recent_members = list(getattr(state, "recent_answer_members", None) or [])
        if max(len(active_members), len(recent_members)) < 2:
            return False

        geography_delta = bool(
            delta.changed_country is not None
            or delta.changed_countries is not None
            or delta.added_countries is not None
            or delta.removed_countries is not None
        )

        active_scope_count = len(active_members)
        if (
            delta.changed_indicator
            and not geography_delta
            and active_scope_count < 2
        ):
            recent_scope_count = self._distinct_member_scope_count(recent_members)
            if not (
                self._looks_like_collective_indicator_switch(getattr(delta, "raw_query", None))
                and recent_scope_count >= 2
            ):
                return False

        if any(
            getattr(state, field, None)
            for field in ("dimensions", "trade_flow", "trade_reporter", "trade_partner", "trade_commodity", "coin_ids")
        ):
            return False

        if getattr(state, "provider_locked", False) and not delta.changed_indicator:
            return False

        if delta.is_new_query or delta.changed_provider is not None or delta.added_indicators is not None:
            return False

        return bool(
            delta.changed_indicator
            or delta.changed_start_date
            or delta.changed_end_date
            or geography_delta
        )

    def _prefer_imf_like_provider_for_growth_members(
        self,
        members: List[Any],
        collective_indicator_label: str,
    ) -> List[Any]:
        """Prefer IMF-preserving members for GDP growth chains when available."""
        label = str(collective_indicator_label or "").strip().lower()
        if "growth" not in label or "gdp" not in label:
            return members

        imf_members = [
            member for member in members
            if normalize_provider_name(getattr(member, "provider", "") or "") == "IMF"
        ]
        if not imf_members:
            return members

        by_country: Dict[str, Any] = {}
        for member in imf_members:
            keys = self._member_country_keys(member)
            if not keys:
                continue
            for key in keys:
                by_country[key] = member

        rewritten: List[Any] = []
        template_member = max(
            imf_members,
            key=lambda member: int(getattr(member, "source_turn", 0) or 0),
        )
        for member in members:
            keys = self._member_country_keys(member)
            replacement = None
            for key in keys:
                if key in by_country:
                    replacement = by_country[key].model_copy(deep=True)
                    break
            if replacement is None:
                replacement = template_member.model_copy(deep=True)
                replacement.country = getattr(member, "country", None)
                replacement.countries = list(getattr(member, "countries", None) or ([replacement.country] if replacement.country else [])) or None
                replacement.indicator_label = collective_indicator_label
                replacement.provider_code = None
                replacement.series_id = None
            rewritten.append(replacement)
        return rewritten

    def _collective_member_query(
        self,
        member: Any,
        collective_indicator_label: str,
        fallback_query: str,
    ) -> str:
        member_countries = [
            self._normalize_country_to_iso2(country) or country
            for country in self._member_country_list(member)
        ]
        member_scope = ", ".join(member_countries) or str(getattr(member, "provider", "") or "member")
        return " ".join(
            part for part in [member_scope, collective_indicator_label] if str(part).strip()
        ).strip() or fallback_query

    def _select_collective_answer_members(
        self,
        state: Any,
        delta: Any,
        collective_indicator_label: str,
    ) -> List[Any]:
        active_members = [
            member.model_copy(deep=True)
            for member in (getattr(state, "active_answer_members", None) or [])
        ]
        recent_members = [
            member.model_copy(deep=True)
            for member in (getattr(state, "recent_answer_members", None) or [])
        ]
        base_members = active_members or recent_members
        if not base_members:
            return []

        normalized_collective_label = collective_indicator_label.strip().lower()

        def _member_label_score(member: Any) -> int:
            member_label = str(getattr(member, "indicator_label", "") or "").strip().lower()
            if not normalized_collective_label or not member_label:
                return 0
            if member_label == normalized_collective_label:
                return 3
            if normalized_collective_label in member_label or member_label in normalized_collective_label:
                return 2
            return 0

        def _member_template_score(member: Any, *, is_active_snapshot: bool) -> tuple[int, int, int, int]:
            return (
                _member_label_score(member),
                int(bool(is_active_snapshot)),
                int(getattr(member, "source_turn", 0) or 0),
                int(bool(getattr(member, "provider_code", None) or getattr(member, "series_id", None))),
            )

        def _coerce_member_for_country(country: str) -> Optional[Any]:
            key = (self._normalize_country_to_iso2(country) or str(country or "").strip()).upper()
            candidates: List[tuple[tuple[int, int, int, int], Any]] = []
            for member in active_members:
                if key in self._member_country_keys(member):
                    candidates.append((_member_template_score(member, is_active_snapshot=True), member))
            for member in recent_members:
                if key in self._member_country_keys(member):
                    candidates.append((_member_template_score(member, is_active_snapshot=False), member))
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                return candidates[0][1].model_copy(deep=True)
            if not base_members:
                return None
            fallback_seed = max(
                base_members,
                key=lambda member: _member_template_score(member, is_active_snapshot=(member in active_members)),
            )
            cloned = fallback_seed.model_copy(deep=True)
            cloned.country = str(country).strip()
            cloned.countries = [str(country).strip()]
            cloned.provider_code = None
            cloned.series_id = None
            return cloned

        geography_delta = bool(
            delta.changed_country is not None
            or delta.changed_countries is not None
            or delta.added_countries is not None
            or delta.removed_countries is not None
        )
        collective_indicator_switch = bool(
            delta.changed_indicator
            and not geography_delta
            and self._looks_like_collective_indicator_switch(getattr(delta, "raw_query", None))
            and self._distinct_member_scope_count(recent_members) >= 2
        )
        if delta.added_countries:
            seed_members = active_members or recent_members
        elif geography_delta:
            seed_members = active_members or recent_members
        elif collective_indicator_switch:
            seed_members = recent_members or active_members
        else:
            seed_members = active_members
        members = seed_members or [member.model_copy(deep=True) for member in recent_members]
        if delta.changed_country or delta.changed_countries:
            requested = []
            if delta.changed_country:
                requested.append(delta.changed_country)
            requested.extend(delta.changed_countries or [])
            members = [
                member
                for country in requested
                if (member := _coerce_member_for_country(str(country)))
            ]

        if delta.removed_countries:
            removed = {
                (self._normalize_country_to_iso2(country) or str(country or "").strip()).upper()
                for country in delta.removed_countries
            }
            members = [
                member
                for member in members
                if not removed.intersection(self._member_country_keys(member))
            ]

        if delta.added_countries:
            existing_keys = {
                key
                for member in members
                for key in self._member_country_keys(member)
            }
            for country in delta.added_countries:
                key = (self._normalize_country_to_iso2(country) or str(country or "").strip()).upper()
                if key in existing_keys:
                    continue
                member = _coerce_member_for_country(str(country))
                if member is not None:
                    if (
                        not delta.changed_indicator
                        and getattr(member, "provider_code", None) is None
                        and getattr(member, "series_id", None) is None
                    ):
                        provider_key = normalize_provider_name(str(getattr(member, "provider", "") or ""))
                        label_key = str(getattr(member, "indicator_label", "") or collective_indicator_label or "").strip().lower()
                        templates = [
                            candidate
                            for candidate in (active_members + recent_members)
                            if normalize_provider_name(str(getattr(candidate, "provider", "") or "")) == provider_key
                            and str(getattr(candidate, "indicator_label", "") or "").strip().lower() == label_key
                            and (
                                getattr(candidate, "provider_code", None)
                                or getattr(candidate, "series_id", None)
                            )
                        ]
                        if templates:
                            template = max(
                                templates,
                                key=lambda candidate: int(getattr(candidate, "source_turn", 0) or 0),
                            )
                            member.provider_code = str(
                                getattr(template, "provider_code", None)
                                or getattr(template, "series_id", None)
                            )
                            member.series_id = str(
                                getattr(template, "series_id", None)
                                or getattr(template, "provider_code", None)
                            )
                    members.append(member)
                    existing_keys.update(self._member_country_keys(member))

        if collective_indicator_label:
            normalized_label = collective_indicator_label.lower()
            for member in members:
                existing_label = str(getattr(member, "indicator_label", "") or "").strip().lower()
                if existing_label != normalized_label:
                    member.indicator_label = collective_indicator_label
                    member.provider_code = None
                    member.series_id = None

        members = self._prefer_imf_like_provider_for_growth_members(
            members,
            collective_indicator_label,
        )

        return members

    def _build_collective_member_intent(
        self,
        query: str,
        member_query: str,
        state: Any,
        member: Any,
        delta: Any,
        collective_indicator_label: str,
    ) -> ParsedIntent:
        countries = [
            self._normalize_country_to_iso2(country) or country
            for country in self._member_country_list(member)
        ]
        params: Dict[str, Any] = {
            "__delta_resolved": True,
        }
        if not delta.changed_indicator:
            params["__semantic_provider_locked"] = True
        if countries:
            if len(countries) == 1:
                params["country"] = countries[0]
            else:
                params["countries"] = countries

        start_date = delta.changed_start_date or getattr(state, "start_date", None)
        end_date = delta.changed_end_date or getattr(state, "end_date", None)
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date

        indicator_changed = bool(delta.changed_indicator or not getattr(member, "provider_code", None))
        params["__delta_indicator_changed"] = indicator_changed
        if collective_indicator_label:
            params["__semantic_indicator_label"] = collective_indicator_label

        indicator_token = (
            collective_indicator_label
            if indicator_changed
            else str(getattr(member, "provider_code", None) or getattr(member, "series_id", None) or collective_indicator_label or "unknown")
        )
        if not indicator_changed and indicator_token:
            params["indicator"] = indicator_token
            params["__exact_provider_code_match"] = True

        return ParsedIntent(
            apiProvider=str(getattr(member, "provider", "") or getattr(state, "provider", "") or "WorldBank"),
            indicators=[indicator_token],
            parameters=params,
            clarificationNeeded=False,
            confidence=0.9,
            originalQuery=query,
            resolvedQuery=member_query,
            isFollowUp=True,
            followUpType=delta.delta_type,
        )

    async def _execute_collective_answer_member_delta(
        self,
        query: str,
        conversation_id: str,
        tracker: Optional["ProcessingTracker"],
        state: Any,
        delta: Any,
    ) -> Optional[QueryResponse]:
        collective_indicator_label = str(
            delta.changed_indicator or self._current_collective_indicator_label(state)
        ).strip()
        target_members = self._select_collective_answer_members(
            state,
            delta,
            collective_indicator_label,
        )
        if not target_members:
            return None

        combined_data: List[NormalizedData] = []
        failed_members: List[str] = []
        # Share-key includes the member's canonical country so a geography-
        # encoded provider code (FRED/StatsCan) is never reused across
        # countries (F6).  Country-agnostic providers (WB/IMF) simply key per
        # country, which still resolves correctly.
        shared_resolution_codes: Dict[tuple[str, str, str], str] = {}
        shared_label_key = collective_indicator_label.lower()

        for member in target_members:
            member_query = self._collective_member_query(
                member,
                collective_indicator_label,
                query,
            )
            member_intent = self._build_collective_member_intent(
                query=query,
                member_query=member_query,
                state=state,
                member=member,
                delta=delta,
                collective_indicator_label=collective_indicator_label,
            )
            member_provider_key = normalize_provider_name(
                str(getattr(member, "provider", "") or member_intent.apiProvider or "")
            )
            member_country_key = self._collective_share_country_key(member)
            shared_code = shared_resolution_codes.get(
                (member_provider_key, member_country_key, shared_label_key)
            )
            if shared_code and delta.changed_indicator:
                # A collection-wide transform should resolve one provider's
                # target indicator once, then apply that provider-native code
                # consistently to the remaining preserved members.  This is
                # not semantic final authority: the first member already passed
                # normal retrieval/fetch/verification, and the reused value is
                # the provider-native series code returned by that success.
                member_intent.indicators = [shared_code]
                member_intent.parameters = {
                    **dict(member_intent.parameters or {}),
                    "indicator": shared_code,
                    "__delta_indicator_changed": False,
                    "__semantic_indicator_label": collective_indicator_label,
                    "__semantic_authority": "llm_adjudication",
                    "__exact_provider_code_match": True,
                    "__decision_source": "collective_shared_resolution",
                }
            member_countries = self._member_country_list(member)
            member_scope = ", ".join(member_countries) or str(getattr(member, "provider", "") or "member")

            async def _recover_member_with_fallback(primary_error: Exception) -> Optional[List[NormalizedData]]:
                try:
                    fallback_data = await self._try_with_fallback(member_intent, primary_error)
                except Exception as fallback_exc:
                    logger.warning(
                        "Collective delta fallback failed for %s via %s: %s",
                        member_scope,
                        member_intent.apiProvider,
                        fallback_exc,
                    )
                    return None

                if fallback_data:
                    fallback_provider = normalize_provider_name(
                        str(getattr(fallback_data[0].metadata, "source", "") or "")
                    ) if fallback_data else ""
                    if fallback_provider:
                        member_intent.apiProvider = fallback_provider
                    logger.info(
                        "Collective delta fallback succeeded for %s via %s",
                        member_scope,
                        member_intent.apiProvider,
                    )
                return fallback_data

            try:
                member_data = await retry_async(
                    lambda intent=member_intent: self._fetch_data(intent),
                    max_attempts=2,
                    initial_delay=0.3,
                )
            except Exception as exc:
                logger.warning(
                    "Collective delta fetch failed for %s via %s: %s",
                    member_scope,
                    member_intent.apiProvider,
                    exc,
                )
                member_data = await _recover_member_with_fallback(exc)
                if not member_data:
                    failed_members.append(member_scope)
                    continue

            if not member_data:
                member_data = await _recover_member_with_fallback(
                    DataNotAvailableError(
                        f"No data returned for preserved member {member_scope}"
                    )
                )
                if not member_data:
                    failed_members.append(member_scope)
                    continue

            member_data = self._rerank_data_by_query_relevance(member_query, member_data)
            if self._is_ranking_query(member_query):
                member_data = self._apply_ranking_projection(member_query, member_data)
            requires_fresh_semantic_verification = bool(
                delta.changed_indicator or not getattr(member, "provider_code", None)
            )
            if requires_fresh_semantic_verification:
                verification_error = await self._verify_execution_result(
                    member_query,
                    member_intent,
                    self._build_minimal_execution_plan(member_query, member_intent),
                    member_data,
                )
                if verification_error:
                    logger.warning(
                        "Collective delta verification failed for %s via %s: %s",
                        member_scope,
                        member_intent.apiProvider,
                        verification_error,
                    )
                    failed_members.append(member_scope)
                    continue

            if delta.changed_indicator and collective_indicator_label:
                returned_provider_key = normalize_provider_name(
                    str(getattr(getattr(member_data[0], "metadata", None), "source", "") or "")
                )
                resolution_provider_key = returned_provider_key or normalize_provider_name(
                    str(member_intent.apiProvider or member_provider_key or "")
                )
                returned_codes = {
                    str(getattr(getattr(series, "metadata", None), "seriesId", "") or "").strip()
                    for series in member_data
                    if str(getattr(getattr(series, "metadata", None), "seriesId", "") or "").strip()
                }
                if resolution_provider_key and len(returned_codes) == 1:
                    shared_resolution_codes.setdefault(
                        (resolution_provider_key, member_country_key, shared_label_key),
                        next(iter(returned_codes)),
                    )

            combined_data.extend(member_data)

        if failed_members:
            return QueryResponse(
                conversationId=conversation_id,
                clarificationNeeded=False,
                error="verification_failed",
                message=(
                    "⚠️ **Couldn't preserve the full collective result set**\n\n"
                    "This follow-up needs a collection-wide rewrite, but one or more preserved members "
                    f"could not be re-resolved safely: {', '.join(failed_members[:6])}."
                ),
                processingSteps=tracker.to_list() if tracker else None,
            )

        response_provider = (
            str(getattr(target_members[0], "provider", "") or "WorldBank")
            if len({str(getattr(member, "provider", "") or "") for member in target_members}) == 1
            else "MULTI"
        )
        response_countries = list(
            dict.fromkeys(
                country
                for member in target_members
                for country in self._member_country_list(member)
            )
        )
        response_params: Dict[str, Any] = {}
        if len(response_countries) == 1:
            response_params["country"] = response_countries[0]
        elif response_countries:
            response_params["countries"] = response_countries
        if len(target_members) == 1:
            member_code = str(
                getattr(target_members[0], "provider_code", None)
                or getattr(target_members[0], "series_id", None)
                or ""
            ).strip()
            if member_code and not delta.changed_indicator:
                response_params["indicator"] = member_code
                response_params["__delta_resolved"] = True
                response_params["__delta_indicator_changed"] = False
                response_params["__semantic_authority"] = "llm_adjudication"
                response_params["__exact_provider_code_match"] = True
                response_params["__decision_source"] = "collective_single_member_addition"
        if delta.changed_start_date or getattr(state, "start_date", None):
            response_params["startDate"] = delta.changed_start_date or getattr(state, "start_date", None)
        if delta.changed_end_date or getattr(state, "end_date", None):
            response_params["endDate"] = delta.changed_end_date or getattr(state, "end_date", None)
        if collective_indicator_label:
            response_params["__semantic_indicator_label"] = collective_indicator_label

        response_intent = ParsedIntent(
            apiProvider=response_provider,
            indicators=[collective_indicator_label or str(getattr(state, "indicator", "") or "unknown")],
            parameters=response_params,
            clarificationNeeded=False,
            confidence=0.9,
            originalQuery=query,
            isFollowUp=True,
            followUpType=delta.delta_type,
        )

        conv_id = conversation_manager.add_message_safe(
            conversation_id,
            "user",
            query,
            intent=response_intent,
        )

        updated_state = state.model_copy(deep=True)
        updated_state.turn_number = getattr(state, "turn_number", 0) + 1
        updated_state.original_query = query
        updated_state.start_date = delta.changed_start_date or getattr(state, "start_date", None)
        updated_state.end_date = delta.changed_end_date or getattr(state, "end_date", None)
        if collective_indicator_label:
            updated_state.indicator = collective_indicator_label
            updated_state.base_indicator = None
            updated_state.resolved_indicator_code = None
            updated_state.last_indicators_resolved = [collective_indicator_label]
        if len(response_countries) == 1:
            updated_state.country = response_countries[0]
            updated_state.countries = None
        elif response_countries:
            updated_state.country = None
            updated_state.countries = response_countries
        updated_state.provider = None if response_provider == "MULTI" else response_provider
        updated_state.routed_provider = updated_state.provider
        updated_state.provider_locked = (
            response_provider != "MULTI"
            and len(target_members) <= 1
        )
        update_answer_members_from_data(updated_state, combined_data, intent=response_intent)
        if delta.added_countries and combined_data:
            current_members = list(getattr(updated_state, "active_answer_members", None) or [])
            recent_members = list(getattr(updated_state, "recent_answer_members", None) or [])
            current_keys = {
                key
                for member in current_members
                for key in self._member_country_keys(member)
            }
            updated_recent = list(recent_members)
            for previous_member in (getattr(state, "active_answer_members", None) or []):
                previous_keys = self._member_country_keys(previous_member)
                if any(key in current_keys for key in previous_keys):
                    continue
                updated_recent.append(previous_member.model_copy(deep=True))
            if updated_recent:
                deduped_recent: Dict[tuple[str, str, str], Any] = {}
                order: List[tuple[str, str, str]] = []
                for member in updated_recent[-24:]:
                    provider_key = normalize_provider_name(str(getattr(member, "provider", "") or ""))
                    label_key = str(getattr(member, "indicator_label", "") or "").strip().lower()
                    country_key = ",".join(self._member_country_keys(member))
                    key = (provider_key, label_key, country_key)
                    deduped_recent[key] = member
                    if key in order:
                        order = [existing for existing in order if existing != key]
                    order.append(key)
                updated_state.recent_answer_members = [deduped_recent[key] for key in order[-24:]]
        conversation_manager.set_conversation_state(conv_id, updated_state)

        conv_id = conversation_manager.add_message_safe(
            conv_id,
            "assistant",
            f"Retrieved {len(combined_data)} data series across {len(updated_state.active_answer_members or [])} preserved result members",
        )

        return QueryResponse(
            conversationId=conv_id,
            intent=response_intent,
            data=combined_data,
            clarificationNeeded=False,
            processingSteps=tracker.to_list() if tracker else None,
            delta_state_saved=True,
        )

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
        return await _ir_resolve_indicator_for_fetch(self, provider, intent, params)

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
        """Delegates to :func:`query_parsing.infer_multi_concept_indicators_from_query`."""
        return _qp_infer_multi_concept_indicators_from_query(query)

    def _maybe_expand_multi_concept_intent(self, query: str, intent: ParsedIntent) -> bool:
        """Delegates to :func:`query_helpers.maybe_expand_multi_concept_intent`."""
        from .query_helpers import maybe_expand_multi_concept_intent as _qh_expand
        return _qh_expand(self, query, intent)

    def _maybe_expand_ranking_country_scope(
        self,
        query: str,
        provider: str,
        params: dict,
    ) -> dict:
        """Delegates to :func:`query_decomposition.maybe_expand_ranking_country_scope`."""
        return _qd_maybe_expand_ranking_country_scope(query, provider, params)

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
        """Delegates to :func:`data_fetcher.extract_exchange_rate_params`."""
        return _df_extract_exchange_rate_params(params, intent)

    def _build_cache_params(self, provider: str, params: dict) -> dict:
        """
        Build normalized cache parameters with explicit schema versioning.

        This decouples cache validity from implementation details and allows safe,
        global invalidation when routing/fetch semantics change.

        For providers that use dimension modifiers (e.g., StatsCan with "shelter",
        "male", "youth"), the originalQuery is included so different modifier
        queries don't share the same cache entry.
        """
        cache_params = dict(params or {})
        for internal_key in (
            "__semantic_authority",
            "__decision_source",
            "__indicator_selection_source",
            "__indicator_selection_status",
            "__indicator_rejection_reason",
            "__indicator_retry_query",
            "__exact_provider_code_match",
            "__semantic_provider_locked",
        ):
            cache_params.pop(internal_key, None)
        cache_params["_cache_version"] = self.CACHE_KEY_VERSION
        execution_plan_identity = params.get("__execution_plan_identity") if params else None
        if execution_plan_identity:
            cache_params["_provider"] = normalize_provider_name(
                execution_plan_identity.get("provider_request", {}).get("provider") or provider
            )
            cache_params["_plan_hash"] = hashlib.sha256(
                json.dumps(execution_plan_identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest()[:16]
            cache_params.pop("_query_hash", None)
            cache_params.pop("__execution_plan_identity", None)
            return cache_params

        cache_params["_provider"] = normalize_provider_name(provider)
        # Include original query hash only for providers that still depend on
        # raw query semantics at fetch time (notably StatsCan dimension
        # extraction). For ordinary follow-up refinements like chart/format
        # changes, hashing the raw follow-up text causes unnecessary cache
        # misses and re-fetches in multi-round sessions.
        normalized_provider = cache_params["_provider"]
        oq = params.get("__original_query") or params.get("originalQuery")
        if oq and normalized_provider in _RAW_QUERY_CACHE_HASH_PROVIDERS:
            cache_params["_query_hash"] = hashlib.sha256(
                oq.strip().lower().encode("utf-8")
            ).hexdigest()[:16]
        return cache_params

    def _serialize_cache_query(self, cache_params: dict) -> str:
        """Serialize cache params deterministically for Redis cache key input."""
        try:
            return json.dumps(cache_params, sort_keys=True, separators=(",", ":"), default=str)
        except Exception as _ser_exc:
            # Keep a deterministic fallback for non-serializable values.
            logger.debug("Cache key JSON serialization fallback: %s", _ser_exc)
            return str(sorted(cache_params.items()))

    def _cached_result_has_complete_country_coverage(
        self,
        params: Optional[dict],
        data: Any,
    ) -> bool:
        """Reject cached multi-country payloads that dropped requested countries.

        Partial responses can occur transiently when upstream providers timeout
        or return incomplete batches. If cached before post-fetch coverage
        checks run, they poison later multiround turns with silently truncated
        scopes. This helper enforces a framework-level invariant: explicit
        multi-country requests are cacheable only when all requested countries
        are present in the payload.
        """
        if not data:
            return False

        data_list = data if isinstance(data, list) else [data]
        try:
            coverage_intent = ParsedIntent(
                apiProvider="WORLDBANK",
                indicators=[str((params or {}).get("indicator") or "__cache_probe__")],
                parameters=dict(params or {}),
                clarificationNeeded=False,
            )
            coverage = self._assess_country_coverage(coverage_intent, data_list)
        except Exception as exc:
            logger.debug("Cache coverage assessment failed: %s", exc)
            return True

        if coverage and not coverage.get("complete", False):
            logger.info(
                "Skipping incomplete multi-country cache payload: requested=%s returned=%s missing=%s",
                coverage.get("requested_display"),
                coverage.get("returned_display"),
                coverage.get("missing_display"),
            )
            return False
        return True

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
            if cached_data and self._cached_result_has_complete_country_coverage(params, cached_data):
                logger.info(f"Redis cache hit for {provider}")
                try:
                    cache_service.cache_data(provider, cache_params, cached_data)
                except Exception as exc:
                    logger.debug("Failed to mirror Redis hit into in-memory cache: %s", exc)
                return cached_data
            if cached_data:
                logger.info(f"Redis cache entry rejected for {provider} due to incomplete country coverage")
        except Exception as e:
            logger.warning(f"Redis cache error: {e}, falling back to in-memory")

        # Fallback to in-memory cache
        cached_data = cache_service.get_data(provider, cache_params)
        if cached_data and self._cached_result_has_complete_country_coverage(params, cached_data):
            logger.info(f"In-memory cache hit for {provider}")
            return cached_data
        if cached_data:
            logger.info(f"In-memory cache entry rejected for {provider} due to incomplete country coverage")

        return None

    async def _get_stale_from_cache(self, provider: str, params: dict):
        """Get stale (expired) cached data as fallback when provider is down.

        Returns data even if TTL has expired — a 1-hour-old GDP dataset
        is better than 'No Data Available' during a transient API outage.
        """
        cache_params = self._build_cache_params(provider, params)
        stale = cache_service.get_data_stale(provider, cache_params)
        if stale and self._cached_result_has_complete_country_coverage(params, stale):
            logger.info(f"📦 Serving STALE cache for {provider} (provider may be down)")
            return stale
        if stale:
            logger.info(f"Stale cache entry rejected for {provider} due to incomplete country coverage")
        return None

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
        if not self._cached_result_has_complete_country_coverage(params, data):
            logger.info(f"Skipping cache save for {provider} — incomplete multi-country coverage")
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
        """Delegates to :func:`provider_strategy.collect_target_countries`."""
        return _ps_collect_target_countries(parameters)

    @staticmethod
    def _normalize_country_to_iso2(country: Optional[str]) -> Optional[str]:
        """Normalize country identifiers/names to ISO2 codes when possible.

        Delegates to :func:`provider_fallback.normalize_country_to_iso2`.
        """
        return _pf_normalize_country_to_iso2(country)

    @staticmethod
    def _extract_statscan_product_id(value: Optional[str]) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        head = text.split(":", 1)[0]
        digits = "".join(ch for ch in head if ch.isdigit())
        if len(digits) >= 8:
            return digits[:8]
        return None

    def _infer_statscan_product_id_for_followup(
        self,
        conversation_id: Optional[str],
        intent: Optional[ParsedIntent],
    ) -> Optional[str]:
        params = (intent.parameters or {}) if intent is not None else {}
        candidates = [
            params.get("productId"),
            params.get("__statscan_product_id"),
            params.get("indicator"),
        ]
        for indicator in (intent.indicators or []) if intent is not None else []:
            candidates.append(indicator)

        state = conversation_manager.get_conversation_state(conversation_id) if conversation_id else None
        if state is not None:
            candidates.append(getattr(state, "resolved_indicator_code", None))
            candidates.append(getattr(state, "statscan_product_id", None))
            for member in list(getattr(state, "active_answer_members", None) or []) + list(getattr(state, "recent_answer_members", None) or []):
                if normalize_provider_name(getattr(member, "provider", "") or "") != "STATSCAN":
                    continue
                candidates.append(getattr(member, "series_id", None))
                candidates.append(getattr(member, "provider_code", None))

        for candidate in candidates:
            product_id = self._extract_statscan_product_id(candidate)
            if product_id:
                return product_id
        return None

    def _should_preserve_statscan_followup_provider(
        self,
        state: Optional[Any],
        intent: Optional[ParsedIntent],
    ) -> bool:
        if state is None or intent is None:
            return False
        params = intent.parameters or {}
        has_dimension_or_decomposition_signal = bool(
            intent.needsDecomposition
            or params.get("__dimensions")
            or params.get("__statscan_decomposition_axis")
        )
        if not has_dimension_or_decomposition_signal:
            return False
        provider = normalize_provider_name(getattr(state, "provider", "") or getattr(state, "routed_provider", "") or "")
        statscan_member_present = any(
            normalize_provider_name(getattr(member, "provider", "") or "") == "STATSCAN"
            for member in list(getattr(state, "active_answer_members", None) or []) + list(getattr(state, "recent_answer_members", None) or [])
        )
        if (
            provider != "STATSCAN"
            and not statscan_member_present
            and not getattr(state, "statscan_product_id", None)
            and not params.get("__statscan_product_id")
        ):
            return False

        return True

    def _maybe_promote_statscan_axis_decomposition_from_query(
        self,
        query: str,
        intent: Optional[ParsedIntent],
    ) -> None:
        if intent is None:
            return
        if normalize_provider_name(intent.apiProvider or "") != "STATSCAN":
            return

        params = dict(intent.parameters or {})
        if params.get("__dimensions"):
            return
        if params.get("__exact_indicator_title_match") and params.get("__statscan_product_id"):
            # Literal StatsCan table titles often contain phrases such as
            # "by age group and sex" as provider table-scope metadata.  Do not
            # reinterpret those title words as a user-requested decomposition.
            # If the query is not just the literal provider title (for example
            # a follow-up/near-title user request that already carries an
            # explicit decomposition type), allow the provider-native axis
            # pathway below to fail closed on missing required dimensions.
            semantic_label = str(params.get("__semantic_indicator_label") or "").strip()
            normalized_query = re.sub(r"[^a-z0-9]+", " ", str(query or "").lower()).strip()
            normalized_label = re.sub(r"[^a-z0-9]+", " ", semantic_label.lower()).strip()
            exact_title_is_query_scope = (
                normalized_label
                and len(normalized_label) >= 8
                and normalized_label in normalized_query
            )
            current_decomp_type = str(intent.decompositionType or "").strip().lower()
            if exact_title_is_query_scope or current_decomp_type not in {"sex", "gender", "age", "age group", "age groups", "ages"}:
                return

        query_lower = query.lower()
        axis = None
        current_decomp_type = str(intent.decompositionType or "").strip().lower()
        if re.search(r"\bby\s+(sex|gender)\b", query_lower) or current_decomp_type in {"sex", "gender"}:
            axis = "Sex"
        elif re.search(r"\bby\s+(age|age group|age groups|ages)\b", query_lower) or current_decomp_type in {"age", "age group", "age groups", "ages"}:
            axis = "Age group"

        if not axis:
            return

        intent.needsDecomposition = True
        intent.decompositionType = "dimension"
        params["__statscan_decomposition_axis"] = axis
        params["__semantic_provider_locked"] = True
        can_refine_indicator_label = not (
            params.get("__exact_indicator_title_match")
            or params.get("__statscan_product_id")
        )
        if can_refine_indicator_label and intent.indicators:
            cleaned_indicators = []
            for indicator in intent.indicators:
                cleaned = str(indicator or "")
                if axis == "Sex":
                    cleaned = re.sub(
                        r"\bby\s+(?:sex|gender)\b(?!\s+(?:at|of|in|on)\b)",
                        " ",
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                    cleaned = re.sub(
                        r"\b(?:male|female|males|females)\b",
                        " ",
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                    cleaned = re.sub(
                        r"\b(?:sex|gender)\s*$",
                        " ",
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                elif axis == "Age group":
                    cleaned = re.sub(
                        r"\bby\s+(?:age(?:\s+groups?)?|ages)\b",
                        " ",
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                    cleaned = re.sub(
                        r"\b(?:age\s+groups?|ages)\s*$",
                        " ",
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                cleaned = re.sub(r"\bby\s*$", " ", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;-")
                if cleaned:
                    cleaned_indicators.append(cleaned)
            unique_cleaned = list(dict.fromkeys(cleaned_indicators))
            if len(unique_cleaned) == 1:
                params["__semantic_indicator_label"] = unique_cleaned[0]
                intent.indicators = [unique_cleaned[0]]
        intent.parameters = params
        if intent.clarificationNeeded:
            intent.clarificationNeeded = False
            intent.clarificationQuestions = None
        logger.info(
            "Promoted StatsCan axis decomposition from query: axis=%s query=%s",
            axis,
            query[:80],
        )

    def _canonicalize_statscan_dimension_axis_for_response(
        self,
        intent: Optional[ParsedIntent],
    ) -> None:
        """Expose a user-facing decomposition axis after provider execution.

        StatsCan's provider metadata often calls the underlying dimension
        ``Sex``.  The conversation-state contract canonicalizes this as
        ``Gender`` for follow-up reasoning and user-facing state.  Keep the
        provider-native axis through fetch execution, then canonicalize the
        returned intent so response metadata and persisted state agree.
        """
        if intent is None:
            return
        if normalize_provider_name(intent.apiProvider or "") != "STATSCAN":
            return
        if str(intent.decompositionType or "").strip().lower() != "dimension":
            return
        params = dict(intent.parameters or {})
        axis = str(params.get("__statscan_decomposition_axis") or "").strip().lower()
        if axis in {"sex", "gender"}:
            params["__statscan_decomposition_axis"] = "Gender"
            intent.parameters = params
        elif axis in {"age", "age group", "age groups", "ages"}:
            params["__statscan_decomposition_axis"] = "Age group"
            intent.parameters = params

    async def _maybe_expand_statscan_dimension_decomposition_entities(
        self,
        conversation_id: Optional[str],
        intent: Optional[ParsedIntent],
    ) -> None:
        if intent is None or not intent.needsDecomposition or intent.decompositionEntities:
            return
        if normalize_provider_name(intent.apiProvider or "") != "STATSCAN":
            return

        params = dict(intent.parameters or {})
        axis = str(params.get("__statscan_decomposition_axis") or "").strip()
        if not axis:
            decomp_type = str(intent.decompositionType or "").strip().lower()
            if decomp_type in {"age groups", "age_group", "age_groups", "ages"}:
                axis = "Age group"
            elif decomp_type in {"sex", "gender"}:
                axis = "Sex"
        if not axis:
            return

        axis_keyword = "age" if "age" in axis.lower() else "sex" if any(token in axis.lower() for token in ("sex", "gender")) else None
        if axis_keyword is None:
            return

        semantic_label = str(params.get("__semantic_indicator_label") or "").strip()
        candidate_product_ids: list[str] = []
        for candidate in [
            self._infer_statscan_product_id_for_followup(conversation_id, intent),
            params.get("__statscan_product_id"),
        ]:
            normalized = self._extract_statscan_product_id(candidate)
            if normalized and normalized not in candidate_product_ids:
                candidate_product_ids.append(normalized)

        # Do not discover a StatsCan product from the semantic label here.
        # Product/table authority must come from exact provider-native input,
        # verified conversation state, or retrieval + LLM adjudication.  This
        # helper may only enumerate axis members after such product evidence
        # is already present.

        members = []
        product_id = None
        for candidate_product_id in candidate_product_ids:
            try:
                candidate_members = await self.statscan_provider.get_dimension_members(candidate_product_id, axis_keyword)
            except Exception as exc:
                logger.warning(
                    "Failed to expand StatsCan decomposition axis %s for product %s: %s",
                    axis,
                    candidate_product_id,
                    exc,
                )
                continue
            if candidate_members:
                members = candidate_members
                product_id = candidate_product_id
                break

        if not product_id:
            return

        entities = []
        for _, name in members:
            label = str(name or "").strip()
            lowered = label.lower()
            if not label:
                continue
            if axis_keyword == "age" and (
                "total" in lowered
                or "all ages" in lowered
                or "15 years and over" in lowered
            ):
                continue
            if axis_keyword == "sex" and lowered in {"both sexes", "total", "total - gender", "all"}:
                continue
            entities.append(label)

        if entities:
            intent.decompositionEntities = entities
            params["__statscan_decomposition_axis"] = axis
            params["__statscan_product_id"] = product_id
            intent.parameters = params
            if str(intent.decompositionType or "").strip().lower() in {"age groups", "age_group", "ages", "sex", "gender"}:
                intent.decompositionType = "dimension"

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

    def _get_provider_for_single_country(
        self,
        iso2: str,
        concept_query: str,
        original_provider: str,
    ) -> Tuple[str, Optional[str]]:
        """Delegates to :func:`provider_strategy.get_provider_for_single_country`."""
        return _ps_get_provider_for_single_country(iso2, concept_query, original_provider)

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
        # When the indicator is a provider-specific code (e.g. CPIAUCSL for FRED),
        # use the human-readable concept for cross-provider catalog lookups.
        concept_query = self._select_indicator_query_for_resolution(intent)
        if not concept_query:
            concept_query = " ".join(str(ind) for ind in intent.indicators if ind)

        # Build a human-readable indicator description for sub-intents sent to
        # a different provider.  Provider-specific codes (CPIAUCSL, GDP, etc.)
        # are meaningless to other providers; we need a concept phrase like
        # "inflation" or "gross domestic product" for proper resolution.
        _human_indicator_query = concept_query
        _first_indicator = str(intent.indicators[0]) if intent.indicators else ""
        # Check if indicator looks like a code for ANY provider, not just
        # the current one (the indicator may have been resolved by a prior
        # provider, e.g. NY.GDP.PCAP.CD from WorldBank on a StatsCan-routed intent).
        _indicator_is_code = _first_indicator and any(
            self._looks_like_provider_indicator_code(p, _first_indicator)
            for p in ALL_PROVIDERS
        )
        if _indicator_is_code:
            # The indicator is a provider-specific code.  Try reverse-looking
            # up the catalog concept name (e.g. CPIAUCSL → "inflation",
            # NY.GDP.PCAP.CD → "gdp_per_capita").  Check all providers.
            from .catalog_service import find_concepts_by_code
            _reverse_concepts: List[str] = []
            for _p in ALL_PROVIDERS:
                _reverse_concepts = find_concepts_by_code(_p, _first_indicator)
                if _reverse_concepts:
                    break
            if _reverse_concepts:
                # Use the concept name as the human-readable description
                _human_indicator_query = _reverse_concepts[0].replace("_", " ")
                logger.info(
                    "🌐 Geographic split: reverse-mapped %s → concept '%s'",
                    _first_indicator, _human_indicator_query,
                )
            elif not _indicator_is_code or (concept_query and concept_query != _first_indicator):
                # concept_query is already distilled and different from the code
                _human_indicator_query = concept_query
            else:
                # Last resort: use the original query which might be "US inflation rate"
                _human_indicator_query = intent.originalQuery or concept_query

        # Group countries by their best provider.
        provider_groups: Dict[str, List[str]] = {}  # provider -> [iso2, ...]
        provider_codes: Dict[str, Optional[str]] = {}  # provider -> indicator code (if catalog knows)
        for iso2 in iso2_map:
            best_prov, best_code = self._get_provider_for_single_country(
                iso2, _human_indicator_query, provider,
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

            # ── Strip delta-resolution flags ────────────────────────────
            # The parent intent may carry __delta_resolved from the
            # conversation delta path.  Sub-intents dispatched to a
            # *different* provider need full indicator resolution (the
            # prior provider's code, e.g. CPIAUCSL, is meaningless to
            # WorldBank).  Even same-provider sub-intents should go
            # through normal resolution since the geographic context
            # changed.
            sub_params.pop("__delta_resolved", None)
            sub_params.pop("__delta_indicator_changed", None)

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

            # For sub-intents going to a different provider, use the
            # human-readable indicator description (not the provider-specific
            # code) so the resolution pipeline can find the right indicator.
            _sub_indicators: List[str]
            _sub_original_query: str
            if catalog_code:
                _sub_indicators = [catalog_code]
                _sub_original_query = _human_indicator_query
            elif group_provider != provider:
                # Different provider: use human-readable indicator for resolution
                _sub_indicators = [_human_indicator_query] if _human_indicator_query else list(intent.indicators or [])
                _sub_original_query = _human_indicator_query
            else:
                # Same provider: keep the original indicator codes
                _sub_indicators = list(intent.indicators or [])
                _sub_original_query = intent.originalQuery or _human_indicator_query

            sub_intent = ParsedIntent(
                apiProvider=group_provider,
                indicators=_sub_indicators,
                parameters=sub_params,
                clarificationNeeded=False,
                originalQuery=_sub_original_query,
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

        # When dispatching 3+ parallel provider fetches, extend the HTTP
        # timeout so that slow providers don't cause premature failures.
        # Single-provider queries keep the default 30s for fast responses.
        if len(tasks) >= 3:
            with extended_timeout(120.0):
                results = await asyncio.gather(*tasks)
        else:
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
        """Delegates to :func:`geography_validation.assess_country_coverage`."""
        return _gv_assess_country_coverage(intent, data)

    def _build_country_coverage_warning_message(
        self,
        coverage: Dict[str, Any],
    ) -> str:
        """Delegates to :func:`geography_validation.build_country_coverage_warning_message`."""
        return _gv_build_country_coverage_warning_message(coverage)

    async def _maybe_improve_country_coverage(
        self,
        query: str,
        intent: Optional[ParsedIntent],
        data: Optional[List[NormalizedData]],
    ) -> tuple[List[NormalizedData], Optional[str]]:
        """Delegates to :func:`query_helpers.maybe_improve_country_coverage`."""
        from .query_helpers import maybe_improve_country_coverage as _qh_coverage
        return await _qh_coverage(self, query, intent, data)

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

    def _build_provider_change_unavailable_response(
        self,
        conv_id: str,
        intent: ParsedIntent,
        tracker: Optional['ProcessingTracker'] = None,
        error_code: str = "data_not_available",
    ) -> Optional[QueryResponse]:
        """Build a helpful response when a provider_change follow-up fails.

        Returns None if the intent is not a provider_change follow-up.
        """
        if not intent or not intent.isFollowUp or intent.followUpType != "provider_change":
            return None

        provider_name = intent.apiProvider
        indicators = ", ".join(intent.indicators) if intent.indicators else "requested indicator"
        fallback_providers = self._get_fallback_providers(normalize_provider_name(provider_name))
        available_hint = ""
        if fallback_providers:
            available_hint = f" This data may be available from: **{', '.join(fallback_providers)}**."
        return QueryResponse(
            conversationId=conv_id,
            intent=intent,
            data=None,
            clarificationNeeded=False,
            error=error_code,
            message=(
                f"**{provider_name}** doesn't appear to have **{indicators}** data."
                f"{available_hint}"
                f"\n\nTry asking for this data without specifying a provider, "
                f"and the system will route to the best available source."
            ),
            processingSteps=tracker.to_list() if tracker else None,
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

    async def _try_with_fallback(self, intent: ParsedIntent, primary_error: Exception):
        """
        Try to fetch data from fallback providers when primary fails.

        Uses human-readable query text (not provider-specific codes) for
        cross-provider indicator resolution. When falling back from provider A
        to provider B, provider B must resolve candidates in its own namespace;
        catalog concept-to-code mappings are not allowed to choose final codes.

        Args:
            intent: The parsed intent
            primary_error: The error from the primary provider

        Returns:
            Data from fallback provider

        Raises:
            Original error if all fallbacks fail
        """
        primary_provider = normalize_provider_name(intent.apiProvider)

        explicit_provider_requested = normalize_provider_name(
            self._detect_explicit_provider(str(intent.originalQuery or "")) or ""
        )
        provider_locked = _ir_is_provider_locked(intent.parameters or {})
        if provider_locked or (
            explicit_provider_requested
            and explicit_provider_requested == primary_provider
        ):
            logger.info(
                "🔒 Skipping cross-provider fallback for provider-locked intent (%s)",
                primary_provider,
            )
            raise primary_error

        logger.info(
            "🔄 Cross-provider fallback uses semantic query, not catalog code mapping"
        )

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
            "🔄 Cross-provider fallback: semantic_query='%s', providers=%s",
            indicator, fallback_providers,
        )

        last_error = primary_error

        # Build fallback intents upfront so we can try the top providers in parallel.
        def _build_fallback_intent(fallback_provider: str) -> ParsedIntent:
            fb_params = dict(intent.parameters or {})
            fb_params["__fallback_excluded_providers"] = [primary_provider]
            # Remove provider-specific resolved indicator identifiers so fallback
            # providers can resolve indicator codes in their own namespace.
            for key in ("indicator", "seriesId", "series_id", "code"):
                fb_params.pop(key, None)

            # Keep fallback indicator evidence provider-neutral. The fallback
            # provider's own retrieval + LLM adjudication must choose any
            # provider-native code.
            fb_indicators = [indicator] if indicator else []
            if not fb_indicators:
                fb_indicator_query = self._select_indicator_query_for_resolution(intent)
                if fb_indicator_query:
                    fb_indicators = [fb_indicator_query]
                elif intent.indicators:
                    safe_indicators = [
                        ind for ind in intent.indicators
                        if not self._looks_like_provider_indicator_code(primary_provider, str(ind or ""))
                    ]
                    fb_indicators = safe_indicators or [self._effective_original_query(intent) or indicator or ""]

            effective_oq = self._effective_original_query(intent)
            return ParsedIntent(
                apiProvider=fallback_provider,
                indicators=fb_indicators,
                parameters=fb_params,
                clarificationNeeded=False,
                originalQuery=effective_oq or intent.originalQuery,
                isFollowUp=intent.isFollowUp,
                followUpType=intent.followUpType,
                resolvedQuery=intent.resolvedQuery,
            )

        async def _try_single_fallback(fallback_provider: str) -> Optional[list]:
            """Try a single fallback provider, return data or None."""
            logger.warning(f"Attempting fallback from {primary_provider} to {fallback_provider}")
            fb_intent = _build_fallback_intent(fallback_provider)
            try:
                result = await self._fetch_data(fb_intent)
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
                    return None
            except Exception as e:
                logger.warning(f"Fallback to {fallback_provider} failed: {e}")
                return None

        # --- Parallel fallback for top-2 providers ---
        # When 2+ fallback providers are available, try the top 2 in parallel
        # to reduce latency (especially when WB circuit breaker is open).
        if len(fallback_providers) >= 2:
            top_two = fallback_providers[:2]
            remaining = fallback_providers[2:]
            logger.info(f"🔄 Parallel fallback: trying {top_two} simultaneously")

            results = await asyncio.gather(
                _try_single_fallback(top_two[0]),
                _try_single_fallback(top_two[1]),
                return_exceptions=True,
            )

            # Prefer higher-priority provider (index 0) if both succeed
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(f"Parallel fallback {top_two[i]} raised: {result}")
                    last_error = result
                elif result is not None:
                    return result

            # Both top-2 failed — continue sequentially with remaining
            for fallback_provider in remaining:
                result = await _try_single_fallback(fallback_provider)
                if result is not None:
                    return result
        else:
            # Only 1 fallback provider — try it directly
            for fallback_provider in fallback_providers:
                result = await _try_single_fallback(fallback_provider)
                if result is not None:
                    return result

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
            conversation_manager.refresh_from_redis(conv_id)
            history = conversation_manager.get_history(conv_id)

            if normalize_provider_name(self._detect_explicit_provider(query) or "") == "IMF":
                early_supportability_reason = imf_exact_provider_surface_supportability_reason(query)
                if early_supportability_reason:
                    with tracker.track(
                        "provider_supportability",
                        "Checking provider supportability...",
                    ) as update_supportability_metadata:
                        update_supportability_metadata({
                            "provider": "IMF",
                            "supportability_reason": early_supportability_reason,
                        })
                    early_intent = ParsedIntent(
                        apiProvider="IMF",
                        indicators=[query],
                        parameters={"__semantic_provider_locked": True},
                        clarificationNeeded=False,
                        confidence=1.0,
                        recommendedChartType="line",
                        queryType="data_fetch",
                        originalQuery=query,
                        isFollowUp=False,
                        followUpType=None,
                        resolvedQuery=None,
                        needsDecomposition=False,
                        decompositionType=None,
                        decompositionEntities=None,
                        useProMode=False,
                    )
                    error_message = (
                        "IMF query targets a detailed IMF public-data surface that is "
                        "not yet executable by OpenEcon's production IMF dataset-family "
                        "routing. This is a fail-closed supportability block, not a "
                        "broad-proxy substitution. "
                        f"reason={early_supportability_reason}"
                    )
                    formatted_message = QueryComplexityAnalyzer.format_error_message(
                        error_message,
                        query,
                        early_intent,
                    )
                    conv_id = conversation_manager.add_message_safe(
                        conv_id,
                        "user",
                        query,
                        intent=early_intent,
                    )
                    conversation_manager.add_message_safe(conv_id, "assistant", formatted_message)
                    return QueryResponse(
                        conversationId=conv_id,
                        intent=early_intent,
                        clarificationNeeded=False,
                        error="data_not_available",
                        message=formatted_message,
                        processingSteps=tracker.to_list(),
                    )

            # ── Pending indicator choice (numeric "1", "2" responses) ───
            # Keep this for structural resolution — no LLM needed for "pick option 2".
            pending_choice_response = await self._try_resolve_pending_indicator_choice(
                query=query,
                conversation_id=conv_id,
                tracker=tracker,
            )
            if pending_choice_response is not None:
                return pending_choice_response

            pending_semantic = conversation_manager.get_pending_semantic_clarification(conv_id)
            if pending_semantic:
                structured_options = [
                    ClarificationOption.model_validate(option)
                    for option in (pending_semantic.get("options") or [])
                    if isinstance(option, dict)
                ]
                selected_option = self._match_structured_clarification_option(query, structured_options)
                if selected_option is not None:
                    refined_query = str(selected_option.value or query).strip()
                    semantic_intent = self._build_intent_from_semantic_clarification(
                        pending=pending_semantic,
                        selected_option=selected_option,
                        refined_query=refined_query,
                    )
                    conversation_manager.clear_pending_semantic_clarification(conv_id)
                    if semantic_intent is not None:
                        semantic_parse_result = ParseRouteResult(
                            intent=semantic_intent,
                            explicit_provider=semantic_intent.apiProvider,
                            routed_provider=semantic_intent.apiProvider,
                            validation_warning=None,
                        )
                        return await self._execute_resolved_intent(
                            query=refined_query,
                            conversation_id=conv_id,
                            intent=semantic_intent,
                            parse_result=semantic_parse_result,
                            tracker=tracker,
                            skip_prefetch_clarification=True,
                        )
                if str(pending_semantic.get("kind") or "").strip() == "group_scope":
                    pending_intent = conversation_manager.get_last_intent(conv_id)
                    follow_up_countries = self._extract_countries_from_query(query)
                    if pending_intent is not None and follow_up_countries:
                        narrowed_intent = pending_intent.model_copy(deep=True)
                        narrowed_intent.clarificationNeeded = False
                        narrowed_intent.clarificationQuestions = None
                        narrowed_params = dict(narrowed_intent.parameters or {})
                        if len(follow_up_countries) == 1:
                            narrowed_params["country"] = follow_up_countries[0]
                            narrowed_params.pop("countries", None)
                        else:
                            narrowed_params["countries"] = follow_up_countries
                            narrowed_params.pop("country", None)
                        narrowed_intent.parameters = narrowed_params
                        conversation_manager.clear_pending_semantic_clarification(conv_id)
                        narrowed_parse_result = ParseRouteResult(
                            intent=narrowed_intent,
                            explicit_provider=narrowed_intent.apiProvider,
                            routed_provider=narrowed_intent.apiProvider,
                            validation_warning=None,
                        )
                        return await self._execute_resolved_intent(
                            query=query,
                            conversation_id=conv_id,
                            intent=narrowed_intent,
                            parse_result=narrowed_parse_result,
                            tracker=tracker,
                            skip_prefetch_clarification=True,
                        )

            # ── Combined Classification + Delta Extraction ──────────────
            # Single LLM call classifies AND extracts delta in one shot.
            # The FollowUpDelta model includes a query_type field that the
            # LLM sets alongside the changed fields. This eliminates the
            # separate classifier call (~300ms saved per follow-up).
            _current_conv_state = conversation_manager.get_conversation_state(conv_id)
            _query_type = None
            _delta = None
            _llm_new_query = False
            if _current_conv_state is not None:
                _delta_extractor = DeltaExtractor(self)
                # Tier 1: fast regex for structural patterns (country, time, provider)
                _delta = _delta_extractor.extract(query, _current_conv_state)
                if _delta is not None:
                    _query_type = "parameter_delta"
                else:
                    # Tier 2: combined LLM classification + delta extraction
                    try:
                        _delta = await _delta_extractor.extract_with_llm(
                            query, _current_conv_state,
                        )
                        if _delta is not None:
                            _query_type = _delta.query_type or "parameter_delta"
                            logger.info("LLM classified+extracted: type=%s, delta_type=%s for: %s",
                                        _query_type, _delta.delta_type, query[:50])
                            # Non-delta types: clear delta, dispatch to correct handler
                            if _query_type != "parameter_delta":
                                _delta = None
                                # Remember a TRUE new_query classification so the
                                # LLM-parse path below can apply a gated
                                # topic-change reset (F3) — provider-scoped/
                                # contextual fields from the previous topic must
                                # not silently leak into an unrelated new topic.
                                if _query_type == "new_query":
                                    _llm_new_query = True
                    except Exception as _llm_err:
                        logger.warning("LLM delta extraction error: %s", _llm_err)

            # ── Pre-LLM informational detection (first-turn) ──────────
            # When there's no conversation state, the combined classifier is
            # skipped entirely so _query_type stays None.  Catch informational
            # queries here so they don't fall through to the full LLM parse
            # pipeline which would try indicator search instead of answering.
            if _query_type is None and self._looks_informational(query):
                _query_type = "informational"
                logger.info("📖 Pre-LLM heuristic → informational for: %s", query[:60])

            # ── Dispatch: Pro Mode ──────────────────────────────────────
            if _query_type == "pro_mode" and auto_pro_mode:
                logger.info("🚀 LLM → Pro Mode for: %s", query[:50])
                return await self._execute_pro_mode(query, conv_id)

            # ── Dispatch: Informational ─────────────────────────────────
            if _query_type == "informational":
                logger.info("📖 Dispatching informational handler for: %s", query[:50])
                # Build a minimal intent so the handler can operate without
                # waiting for the full LLM parse (saves ~2-4s on first turn).
                _info_intent = ParsedIntent(
                    originalQuery=query,
                    queryType="informational",
                    apiProvider="NONE",
                    indicators=["INFORMATIONAL"],
                    clarificationNeeded=False,
                    parameters={},
                )
                _info_response = self._handle_informational_intent(
                    query=query,
                    intent=_info_intent,
                    conversation_id=conv_id,
                    tracker=tracker,
                )
                # Handle async definition sentinel — await the LLM call
                if isinstance(_info_response, _ic_DefinitionSentinel):
                    _info_response = await _info_response.resolve()
                if _info_response is not None:
                    conv_id = conversation_manager.add_message_safe(
                        conv_id, "user", query, intent=_info_intent,
                    )
                    conversation_manager.add_message_safe(
                        conv_id, "assistant", _info_response.message or "",
                    )
                    return _info_response
                # If handler returned None, fall through to LLM parse

            # ── Dispatch: Parameter Delta ───────────────────────────────
            if _delta is not None:
                    if self._should_use_collective_answer_member_delta(_current_conv_state, _delta):
                        _collective_response = await self._execute_collective_answer_member_delta(
                            query=query,
                            conversation_id=conv_id,
                            tracker=tracker,
                            state=_current_conv_state,
                            delta=_delta,
                        )
                        if _collective_response is not None:
                            return _collective_response

                    _merged_state = merge_state(_current_conv_state, _delta)
                    _force_statscan_canada_delta = self._should_force_statscan_for_canada_decomposition_delta(
                        _merged_state,
                        _delta,
                    )
                    if _force_statscan_canada_delta:
                        if normalize_provider_name(
                            _merged_state.provider or _merged_state.routed_provider or ""
                        ) != "STATSCAN":
                            _merged_state.resolved_indicator_code = None
                            _merged_state.last_indicators_resolved = None
                        _merged_state.provider = "STATSCAN"
                        _merged_state.routed_provider = "STATSCAN"
                        _merged_state.provider_locked = True
                    _delta_intent = materialize_intent(_merged_state)

                    # Route the materialized intent through UnifiedRouter.
                    # Skip re-routing when the delta doesn't change the
                    # indicator, provider, or country — the provider from
                    # conversation state is already correct.  Re-routing
                    # with a short follow-up query (e.g. "last 10 years")
                    # causes catalog false-positives that drift the provider
                    # (e.g. "10 years" matching "10-year bond yield" → IMF).
                    _provider_locked = bool(
                        _merged_state.provider_locked
                        or (_delta_intent.parameters or {}).get("__semantic_provider_locked")
                    )
                    _preserve_current_provider = self._should_preserve_current_provider_for_delta(
                        _current_conv_state,
                        _delta,
                        _delta_intent,
                        query,
                    )
                    if _preserve_current_provider:
                        _provider_locked = True
                        _delta_intent.apiProvider = normalize_provider_name(
                            _current_conv_state.provider or _current_conv_state.routed_provider or _delta_intent.apiProvider
                        )
                    _preserve_statscan_delta_provider = bool(
                        normalize_provider_name(_current_conv_state.provider or _current_conv_state.routed_provider or "") == "STATSCAN"
                        and (
                            _delta.changed_decomposition is not None
                            or _delta.added_dimensions is not None
                            or _delta.removed_dimensions is not None
                            or _delta_intent.parameters.get("__statscan_product_id")
                            or _merged_state.statscan_product_id
                        )
                    )
                    if _preserve_statscan_delta_provider:
                        _provider_locked = True
                        _delta_intent.apiProvider = "STATSCAN"
                    if _provider_locked:
                        if _delta_intent.parameters is None:
                            _delta_intent.parameters = {}
                        _delta_intent.parameters["__semantic_provider_locked"] = True
                    if _preserve_statscan_delta_provider and _merged_state.statscan_product_id:
                        _delta_intent.parameters["__statscan_product_id"] = _merged_state.statscan_product_id
                        _delta_intent.parameters["__statscan_product_authority"] = "verified_conversation_state"
                    _delta_scope_changed = (
                        _delta.changed_country is not None
                        or _delta.changed_countries is not None
                        or _delta.added_countries is not None
                        or _delta.removed_countries is not None
                    )
                    _target_countries = _delta_intent.parameters.get("countries")
                    if not _target_countries:
                        _country_value = _delta_intent.parameters.get("country")
                        _target_countries = [_country_value] if _country_value else []
                    if (
                        _delta_scope_changed
                        and _preserve_current_provider
                        and _merged_state.provider
                        and _target_countries
                        and not self._provider_covers_country_list(_merged_state.provider, _target_countries)
                    ):
                        logger.info(
                            "Delta path: releasing preserved provider %s because expanded scope is unsupported for countries=%s",
                            _merged_state.provider,
                            _target_countries,
                        )
                        _provider_locked = False
                        if _delta_intent.parameters is None:
                            _delta_intent.parameters = {}
                        _delta_intent.parameters.pop("__semantic_provider_locked", None)
                    _delta_changes_routing_params = (
                        _delta.changed_indicator is not None
                        or _delta.added_indicators is not None
                        or _delta.changed_provider is not None
                        or _delta.changed_decomposition is not None
                        or _delta.added_dimensions is not None
                        or _delta.removed_dimensions is not None
                        or _delta.is_new_query
                    )
                    _skip_reroute = (
                        _merged_state.provider
                        and (
                            _delta.changed_provider is not None
                            or
                            not _delta_changes_routing_params
                            or (
                                _provider_locked
                                and _delta.changed_provider is None
                                and not _delta.is_new_query
                            )
                        )
                    )
                    if _skip_reroute:
                        logger.info(
                            "Delta path: skipping re-route (delta_type=%s, preserving provider=%s, provider_locked=%s)",
                            _delta.delta_type, _merged_state.provider,
                            _provider_locked,
                        )
                        if (
                            _delta_scope_changed
                            and not _provider_locked
                            and _merged_state.provider
                        ):
                            if (
                                _target_countries
                                and not self._provider_covers_country_list(_merged_state.provider, _target_countries)
                            ):
                                logger.info(
                                    "Delta path: coverage override on scope-only follow-up %s -> WORLDBANK for countries=%s",
                                    _merged_state.provider,
                                    _target_countries,
                                )
                                _delta_intent.apiProvider = "WORLDBANK"
                                _merged_state.resolved_indicator_code = None
                                _merged_state.last_indicators_resolved = None
                                if _delta_intent.parameters is None:
                                    _delta_intent.parameters = {}
                                _delta_intent.parameters.pop("indicator", None)
                                _delta_intent.parameters.pop("__semantic_provider_locked", None)
                    else:
                        try:
                            _delta_routing = self.unified_router.route(
                                query=_delta_intent.originalQuery or query,
                                indicators=_delta_intent.indicators or [],
                                llm_provider=_delta_intent.apiProvider,
                                country=_delta_intent.parameters.get("country"),
                                countries=_delta_intent.parameters.get("countries"),
                            )
                            if (
                                _delta_routing
                                and _delta_routing.provider
                                and getattr(_delta_routing, "can_override_llm_provider", False)
                            ):
                                _delta_intent.apiProvider = normalize_provider_name(_delta_routing.provider)
                        except Exception as _route_err:
                            logger.debug("Delta routing failed: %s", _route_err)

                    _merged_state.routed_provider = _delta_intent.apiProvider
                    _merged_state.last_indicators_resolved = _delta_intent.indicators

                    # Mark intent as delta-resolved. Pass whether the indicator
                    # needs re-resolution. This is true when:
                    # 1. The indicator explicitly changed (indicator switch)
                    # 2. The provider changed (same concept needs different code)
                    if _delta_intent.parameters is None:
                        _delta_intent.parameters = {}
                    _provider_changed = (
                        normalize_provider_name(_delta_intent.apiProvider)
                        != normalize_provider_name(_current_conv_state.provider or _current_conv_state.routed_provider or "")
                    )
                    _indicator_changed = (
                        _delta.changed_indicator is not None
                        or _delta.added_indicators is not None
                        or _delta.changed_trade_flow is not None
                        or _provider_changed
                    )
                    _delta_intent.parameters["__delta_resolved"] = True
                    _delta_intent.parameters["__delta_indicator_changed"] = _indicator_changed

                    # Build a ParseRouteResult for _execute_resolved_intent
                    _delta_parse_result = ParseRouteResult(
                        intent=_delta_intent,
                        explicit_provider=None,
                        routed_provider=_delta_intent.apiProvider,
                        validation_warning=None,
                    )

                    logger.info(
                        "v2 Delta path: type=%s, indicator=%s, country=%s/%s",
                        _delta.delta_type,
                        _merged_state.indicator,
                        _merged_state.country,
                        _merged_state.countries,
                    )

                    # Ensure StatsCan cube metadata is present in the merged state.
                    # This is critical for 3rd+ round dimension follow-ups: without
                    # cube metadata the LLM delta extractor can't see dimension
                    # members and may misclassify "show energy" as a new query
                    # instead of a dimension change on the current CPI indicator.
                    if (_merged_state.statscan_product_id
                            and not _merged_state.statscan_cube_metadata
                            and _merged_state.provider
                            and _merged_state.provider.upper() in {"STATSCAN", "STATISTICS CANADA"}):
                        try:
                            _cube = await self.statscan_provider._get_cube_metadata(
                                _merged_state.statscan_product_id
                            )
                            if _cube:
                                _merged_state.statscan_cube_metadata = _cube
                                logger.info(
                                    "Delta path: back-filled cube metadata for product %s",
                                    _merged_state.statscan_product_id,
                                )
                        except Exception as _cube_err:
                            logger.debug("Delta path: cube metadata fetch failed: %s", _cube_err)

                    _delta_response = await self._execute_resolved_intent(
                        query=_delta_intent.originalQuery or query,
                        skip_prefetch_clarification=True,
                        skip_post_fetch_clarification=True,
                        conversation_id=conv_id,
                        intent=_delta_intent,
                        parse_result=_delta_parse_result,
                        tracker=tracker,
                    )

                    # After successful fetch, ALWAYS update the resolved_indicator_code
                    # in the persisted state.  The data_fetcher may have resolved
                    # the indicator to a provider-specific code (e.g.,
                    # "NY.GDP.PCAP.CD") — capture it so the next delta turn can
                    # reuse it without re-resolution drift.
                    #
                    # Previously this only saved when _resolved_code differed from
                    # _merged_state.indicator, but on turn 1 the guaranteed-save
                    # path sets indicator = resolved code, so the condition was
                    # always False and resolved_indicator_code was never persisted
                    # via the delta path.  Now we always persist when available.
                    if (
                        _delta_response is not None
                        and not _delta_response.error
                        and not _delta_response.clarificationNeeded
                    ):
                        try:
                            _merged_state.provider = normalize_provider_name(_delta_intent.apiProvider or _merged_state.provider or _merged_state.routed_provider or "")
                            _merged_state.routed_provider = _merged_state.provider
                            _merged_state.provider_locked = bool(
                                (_delta_intent.parameters or {}).get("__semantic_provider_locked")
                                or _merged_state.provider_locked
                            )
                            _resolved_code = (_delta_intent.parameters or {}).get("indicator")
                            if _resolved_code:
                                _merged_state.resolved_indicator_code = str(_resolved_code)
                            if _delta_response.data:
                                update_answer_members_from_data(
                                    _merged_state,
                                    _delta_response.data,
                                    intent=_delta_intent,
                                )
                            conversation_manager.set_conversation_state(conv_id, _merged_state)
                        except Exception as _persist_exc:
                            logger.warning("Failed to persist resolved_indicator_code in delta path: %s", _persist_exc)

                    # Mark that the delta path already saved the merged state.
                    # This prevents the guaranteed save in main.py from
                    # overwriting the carefully-merged state with a freshly
                    # extracted one (which may have indicator drift).
                    if _delta_response is not None:
                        _delta_response.delta_state_saved = True
                    return _delta_response

            # Log fallthrough when classifier said delta but extraction failed
            if _query_type == "parameter_delta" and _delta is None:
                logger.info("Delta extraction returned None despite parameter_delta classification — falling through to LLM parse")

            # ── Dispatch: New Query or fallthrough ──────────────────
            # For new_query, clarification_answer, informational, or when
            # the classifier isn't available, fall through to the standard
            # LLM parse pipeline which handles all of these via
            # conversation_context and queryType classification.

            # Pro Mode: check complexity for first-turn queries or when
            # classifier explicitly says pro_mode (already handled above for follow-ups)
            generic_scope_clarification = self._build_unspecified_geography_breakdown_clarification(
                conversation_id=conv_id,
                query=query,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if generic_scope_clarification is not None:
                return generic_scope_clarification

            if auto_pro_mode and _query_type in (None, "new_query"):
                early_explicit_code_intent = self._build_explicit_provider_code_intent(query)
                early_exact_title_intent = (
                    self._build_exact_indicator_title_intent(query)
                    if early_explicit_code_intent is None
                    else None
                )
                early_shortcut_intent = early_explicit_code_intent or early_exact_title_intent
                if early_shortcut_intent is not None:
                    logger.info(
                        "Skipping auto Pro Mode for exact provider surface query: %s/%s",
                        early_shortcut_intent.apiProvider,
                        (early_shortcut_intent.parameters or {}).get("indicator"),
                    )
                    early_complexity = {"pro_mode_required": False, "complexity_factors": []}
                else:
                    early_complexity = QueryComplexityAnalyzer.detect_complexity(query, intent=None)
                if early_complexity['pro_mode_required']:
                    logger.info("🚀 Auto-switching to Pro Mode (detected: %s)", early_complexity['complexity_factors'])
                    return await self._execute_pro_mode(query, conv_id)

            # Orchestrator: only for explicitly complex new queries that
            # need multi-agent routing (not for simple single-indicator queries)
            from ..config import get_settings
            settings = get_settings()
            if (allow_orchestrator
                    and use_orchestrator
                    and _query_type in (None, "new_query")
                    and not conversation_manager.get_last_intent(conv_id)):
                logger.info("🤖 Using LangChain orchestrator for complex query routing")
                return await self._execute_with_orchestrator(query, conv_id, tracker)

            # ── LLM-based follow-up detection via dynamic prompt ─────
            # Build conversation_context for the LLM system prompt so it can
            # detect follow-ups natively (replacing brittle regex patterns).
            # The raw query is preserved as `original_raw_query` for downstream use.
            conversation_context = None
            last_intent = conversation_manager.get_last_intent(conv_id)
            if last_intent:
                li_params = last_intent.parameters or {}
                # Gather country/countries from prior intent, with fallback
                # to ConversationState which carries forward accumulated context
                prior_country = li_params.get("country", "")
                prior_countries_list = li_params.get("countries")
                if prior_countries_list and isinstance(prior_countries_list, list):
                    country_str = ", ".join(str(c) for c in prior_countries_list)
                elif prior_country:
                    country_str = str(prior_country)
                else:
                    # Fallback: check ConversationState for accumulated country
                    # context that may not be in last_intent parameters (e.g.,
                    # when a follow-up changed indicator but not country).
                    if _current_conv_state:
                        if _current_conv_state.countries:
                            country_str = ", ".join(_current_conv_state.countries)
                        elif _current_conv_state.country:
                            country_str = _current_conv_state.country
                        else:
                            country_str = "not specified"
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
                # Include accumulated state context so the LLM can preserve
                # dimensions, base_indicator, etc. in the fallthrough path.
                if _current_conv_state:
                    if _current_conv_state.dimensions:
                        conversation_context["dimensions"] = str(_current_conv_state.dimensions)
                    if _current_conv_state.base_indicator:
                        conversation_context["base_indicator"] = _current_conv_state.base_indicator

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
                    exact_code_intent = (
                        self._build_explicit_provider_code_intent(query)
                        if conversation_context is None
                        else None
                    )
                    exact_title_intent = (
                        self._build_exact_indicator_title_intent(query)
                        if conversation_context is None and exact_code_intent is None
                        else None
                    )
                    shortcut_intent = exact_code_intent or exact_title_intent
                    if shortcut_intent is not None:
                        cached_intent = parse_result.intent
                        cached_params = cached_intent.parameters or {}
                        shortcut_params = shortcut_intent.parameters or {}
                        cached_provider = normalize_provider_name(cached_intent.apiProvider or "")
                        shortcut_provider = normalize_provider_name(shortcut_intent.apiProvider or "")
                        cached_indicator = str(cached_params.get("indicator") or "").upper()
                        shortcut_indicator = str(shortcut_params.get("indicator") or "").upper()
                        if (
                            not _ir_is_provider_locked(cached_params)
                            or cached_provider != shortcut_provider
                            or cached_indicator != shortcut_indicator
                        ):
                            parse_result = ParseRouteResult(
                                intent=shortcut_intent,
                                explicit_provider=shortcut_intent.apiProvider,
                                routed_provider=shortcut_intent.apiProvider,
                                validation_warning=None,
                            )
                            if _query_hash:
                                _put_cached_parse_result(_query_hash, parse_result)
                            logger.info(
                                "⚡ Intent cache refreshed with deterministic shortcut: %s -> %s/%s",
                                query[:80],
                                shortcut_intent.apiProvider,
                                shortcut_params.get("indicator"),
                            )
                else:
                    exact_code_intent = self._build_explicit_provider_code_intent(query) if conversation_context is None else None
                    exact_title_intent = (
                        self._build_exact_indicator_title_intent(query)
                        if conversation_context is None and exact_code_intent is None
                        else None
                    )
                    shortcut_intent = exact_code_intent or exact_title_intent
                    if shortcut_intent is not None:
                        parse_result = ParseRouteResult(
                            intent=shortcut_intent,
                            explicit_provider=shortcut_intent.apiProvider,
                            routed_provider=shortcut_intent.apiProvider,
                            validation_warning=None,
                        )
                        logger.info(
                            "⚡ Parse shortcut: %s -> %s/%s",
                            query[:80],
                            shortcut_intent.apiProvider,
                            (shortcut_intent.parameters or {}).get("indicator"),
                        )
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

            preserve_statscan_followup = self._should_preserve_statscan_followup_provider(
                _current_conv_state,
                intent,
            )
            provider_locked = _ir_is_provider_locked(intent.parameters or {})
            if preserve_statscan_followup:
                logger.info(
                    "Preserving StatsCan provider for dimension/decomposition follow-up (llm=%s)",
                    intent.apiProvider,
                )
                intent.apiProvider = "STATSCAN"
                preserved_params = dict(intent.parameters or {})
                preserved_params["__semantic_provider_locked"] = True
                if _current_conv_state and _current_conv_state.statscan_product_id:
                    if not preserved_params.get("__statscan_product_id"):
                        preserved_params["__statscan_product_id"] = _current_conv_state.statscan_product_id
                    if (
                        str(preserved_params.get("__statscan_product_id") or "").strip()
                        == str(_current_conv_state.statscan_product_id).strip()
                    ):
                        preserved_params["__statscan_product_authority"] = "verified_conversation_state"
                if _current_conv_state and _current_conv_state.indicator and not preserved_params.get("__semantic_indicator_label"):
                    preserved_params["__semantic_indicator_label"] = _current_conv_state.indicator
                intent.parameters = preserved_params
            elif provider_locked:
                logger.info(
                    "🔒 Skipping UnifiedRouter override for provider-locked exact-title intent (%s)",
                    intent.apiProvider,
                )
            else:
                # Framework: UnifiedRouter determines the provider (overrides LLM).
                # The LLM may guess wrong (e.g., NOT_AVAILABLE for gold price,
                # WorldBank for "from Eurostat"). UnifiedRouter is deterministic
                # and handles explicit mentions plus structural country context.
                try:
                    router_decision = self.unified_router.route(
                        query=query,
                        indicators=intent.indicators or [],
                        llm_provider=intent.apiProvider,
                        country=intent.parameters.get("country") if intent.parameters else None,
                        countries=intent.parameters.get("countries") if intent.parameters else None,
                    )
                    if router_decision and router_decision.provider:
                        routed = normalize_provider_name(router_decision.provider)
                        llm_prov = normalize_provider_name(intent.apiProvider or "")
                        may_override_llm = bool(
                            getattr(router_decision, "can_override_llm_provider", False)
                        )
                        llm_missing = llm_prov in ("NOT_AVAILABLE", "NONE", "UNKNOWN", "")
                        if routed != llm_prov and may_override_llm:
                            logger.info(
                                "🎯 UnifiedRouter structural override: LLM=%s → Router=%s (type=%s, conf=%.2f)",
                                intent.apiProvider, routed, router_decision.match_type, router_decision.confidence,
                            )
                            intent.apiProvider = routed
                        elif routed != llm_prov and not may_override_llm:
                            logger.info(
                                "🧭 UnifiedRouter candidate only: keeping LLM provider=%s; router=%s source=%s candidates=%s",
                                intent.apiProvider,
                                routed,
                                getattr(router_decision, "decision_source", router_decision.match_type),
                                getattr(router_decision, "candidate_providers", []),
                            )
                        # Fix NOT_AVAILABLE only when the router has explicit or
                        # mechanical final authority. Candidate/coverage hints
                        # must not become a semantic provider override.
                        if llm_missing and may_override_llm:
                            intent.apiProvider = routed
                            logger.info("🔧 Fixed NOT_AVAILABLE: router found %s", routed)
                        # Provider locks are allowed only for explicit/mechanical
                        # final authority. Country/region coverage candidates
                        # are intentionally non-authoritative.
                        if (
                            routed == "STATSCAN"
                            and may_override_llm
                        ):
                            if intent.parameters is None:
                                intent.parameters = {}
                            target_countries = intent.parameters.get("countries")
                            if not target_countries:
                                target_countries = [intent.parameters.get("country")] if intent.parameters.get("country") else []
                            normalized_targets = {
                                self._normalize_country_to_iso2(country) or str(country or "").upper()
                                for country in target_countries
                                if country
                            }
                            if normalized_targets == {"CA"}:
                                intent.parameters["__semantic_provider_locked"] = True
                                logger.info(
                                    "🔒 Locked StatsCan provider for Canada-scoped structural route (%s)",
                                    router_decision.match_type,
                                )
                except Exception as e:
                    logger.debug("UnifiedRouter override failed: %s", e)

            # Framework enrichment: recover from avoidable parser clarifications and
            # auto-expand clear multi-concept comparisons to multi-indicator intents.
            self._maybe_resolve_region_clarification(query, intent)
            self._maybe_resolve_temporal_comparison_clarification(query, intent)
            self._maybe_expand_multi_concept_intent(query, intent)
            self._maybe_promote_statscan_axis_decomposition_from_query(query, intent)

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
                # Handle async definition sentinel
                if isinstance(informational_response, _ic_DefinitionSentinel):
                    informational_response = await informational_response.resolve()
                if informational_response is not None:
                    conv_id = conversation_manager.add_message_safe(
                        conv_id, "user", query, intent=intent,
                    )
                    conversation_manager.add_message_safe(
                        conv_id, "assistant", informational_response.message or "",
                    )
                    return informational_response

            # Snapshot the previous successful intent so we can restore it if
            # this round fails (error / no-data).  Clarification intents are
            # intentionally saved so the next turn can resolve them via
            # conversation context, but processing errors should NOT overwrite
            # the last good state.
            _prev_good_intent = conversation_manager.get_last_intent(conv_id)
            _prev_good_state = conversation_manager.get_conversation_state(conv_id)
            _pending_conv_state = None
            conv_id = conversation_manager.add_message_safe(conv_id, "user", query, intent=intent)

            # Dual-write: update ConversationState alongside last_intent.
            # Use merge_new_state_with_previous to preserve ALL accumulated
            # context (country, provider, time range, dimensions, etc.) from
            # prior rounds when the new intent doesn't include those fields.
            try:
                _new_conv_state = extract_state_from_intent(intent, statscan_provider=self.statscan_provider)
                _new_conv_state = merge_new_state_with_previous(_new_conv_state, _prev_good_state)
                _new_conv_state = reset_state_for_topic_change(
                    _new_conv_state, _prev_good_state, llm_new_query=_llm_new_query,
                )
                _pending_conv_state = _new_conv_state
                if not self._use_staged_state_commit():
                    conversation_manager.set_conversation_state(conv_id, _new_conv_state)
            except Exception as _sw_err:
                logger.warning("Dual-write conversation_state failed in process_query: %s", _sw_err)

            if intent.clarificationNeeded:
                semantic_clarification = self._build_structured_semantic_clarification(
                    conversation_id=conv_id,
                    query=query,
                    intent=intent,
                    processing_steps=tracker.to_list(),
                )
                if semantic_clarification is not None:
                    return semantic_clarification
                conversation_manager.clear_pending_indicator_options(conv_id)
                conversation_manager.clear_pending_semantic_clarification(conv_id)
                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    clarificationNeeded=True,
                    clarificationQuestions=intent.clarificationQuestions,
                    processingSteps=tracker.to_list(),
                )

            execution_plan = self._build_minimal_execution_plan(query, intent)

            if intent.needsDecomposition and intent.decompositionType == "provinces":
                intent.decompositionEntities = _normalize_province_decomposition_entities(
                    intent.decompositionEntities
                )
            await self._maybe_expand_statscan_dimension_decomposition_entities(conv_id, intent)
            if (
                intent.needsDecomposition
                or (intent.parameters or {}).get("__dimensions")
                or (intent.parameters or {}).get("__statscan_decomposition_axis")
            ):
                try:
                    _new_conv_state = extract_state_from_intent(intent, statscan_provider=self.statscan_provider)
                    _new_conv_state = merge_new_state_with_previous(_new_conv_state, _prev_good_state)
                    _new_conv_state = reset_state_for_topic_change(
                        _new_conv_state, _prev_good_state, llm_new_query=_llm_new_query,
                    )
                    _pending_conv_state = _new_conv_state
                    if not self._use_staged_state_commit():
                        conversation_manager.set_conversation_state(conv_id, _new_conv_state)
                except Exception as _statscan_state_err:
                    logger.warning(
                        "Failed to refresh conversation state after StatsCan dimension enrichment: %s",
                        _statscan_state_err,
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
                verification_error = await self._verify_execution_result(query, intent, execution_plan, data)
                if verification_error:
                    if _prev_good_intent is not None:
                        conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                    if _prev_good_state is not None:
                        conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                    return self._build_verification_failed_response(
                        conversation_id=conv_id,
                        intent=intent,
                        message=verification_error,
                        processing_steps=tracker.to_list(),
                    )
                self._canonicalize_statscan_dimension_axis_for_response(intent)
                self._persist_verified_conversation_state(
                    conv_id,
                    _pending_conv_state,
                    data,
                    intent=intent,
                )

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
                if (
                    provider_locked
                    and normalize_provider_name(intent.apiProvider or "") in {"IMF", "WORLDBANK", "WORLD BANK", "OECD", "EUROSTAT"}
                    and len(data or []) < len(intent.indicators or [])
                ):
                    logger.info(
                        "🚫 Provider-locked query only resolved %d/%d members; failing closed",
                        len(data or []),
                        len(intent.indicators or []),
                    )
                    return self._build_no_reliable_indicator_match_response(
                        conversation_id=conv_id,
                        intent=intent,
                        query=query,
                        processing_steps=tracker.to_list(),
                    )
            else:
                # Fetch data with retry logic
                data = await retry_async(
                    lambda: self._fetch_data(intent),
                    max_attempts=3,
                    initial_delay=1.0,
                )

            if (
                provider_locked
                and normalize_provider_name(intent.apiProvider or "") in {"IMF", "WORLDBANK", "WORLD BANK", "OECD", "EUROSTAT"}
                and data
                and self._has_implausible_top_series(query, data)
            ):
                logger.info(
                    "🚫 Provider-locked query returned an implausible top series; failing closed"
                )
                return self._build_no_reliable_indicator_match_response(
                    conversation_id=conv_id,
                    intent=intent,
                    query=query,
                    processing_steps=tracker.to_list(),
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
                        self._persist_verified_conversation_state(
                            conv_id,
                            _pending_conv_state,
                            fallback_data,
                            intent=intent,
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
                    self._persist_verified_conversation_state(
                        conv_id,
                        _pending_conv_state,
                        recovered_data,
                        intent=intent,
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
                country = intent.parameters.get("country") or (intent.parameters.get("countries") or [""])[0] if intent.parameters else ""

                error_details = []
                error_details.append(f"No data found for **{indicators}**")
                if country:
                    error_details.append(f"for **{country}**")
                error_details.append(f"from **{provider_name}**.")

                # Provider-change follow-up: give explicit message about unavailability
                provider_change_response = self._build_provider_change_unavailable_response(
                    conv_id, intent, tracker, error_code="no_data_found",
                )
                if provider_change_response:
                    return provider_change_response

                # Add provider-specific suggestions
                suggestions = self._get_no_data_suggestions(provider_name, intent)

                # No data: restore previous good intent so follow-ups aren't corrupted
                if '_prev_good_intent' in locals() and _prev_good_intent is not None:
                    conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                if '_prev_good_state' in locals() and _prev_good_state is not None:
                    conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent,
                    data=None,
                    clarificationNeeded=False,
                    error="no_data_found",
                    message=f"⚠️ **No Data Available**\n\n{' '.join(error_details)}\n\n{suggestions}",
                    processingSteps=tracker.to_list(),
                )

            data = self._rerank_data_by_query_relevance(query, data, is_multi_indicator=is_multi_indicator)
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
            self._persist_verified_conversation_state(
                conv_id,
                _pending_conv_state,
                data,
                intent=intent,
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
            exc_text = str(exc)
            supportability_blocked = (
                "fail-closed supportability block" in exc_text
                or "imf_non_weo_public_surface_unsupported" in exc_text
                or "coingecko_price_unavailable" in exc_text
            )
            if supportability_blocked:
                formatted_message = QueryComplexityAnalyzer.format_error_message(
                    exc_text, query, intent if 'intent' in locals() else None
                )
                if '_prev_good_intent' in locals() and _prev_good_intent is not None:
                    conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                if '_prev_good_state' in locals() and _prev_good_state is not None:
                    conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                return QueryResponse(
                    conversationId=conv_id,
                    intent=intent if "intent" in locals() else None,
                    clarificationNeeded=False,
                    error="data_not_available",
                    message=formatted_message,
                    processingSteps=tracker.to_list(),
                )

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
                        self._persist_verified_conversation_state(
                            conv_id,
                            _pending_conv_state,
                            fallback_data,
                            intent=intent,
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
                    self._persist_verified_conversation_state(
                        conv_id,
                        _pending_conv_state,
                        stale_list,
                        intent=intent,
                    )
                    return QueryResponse(
                        conversationId=conv_id,
                        intent=intent,
                        data=stale_list,
                        clarificationNeeded=False,
                        message="⚠️ The data provider is temporarily unavailable. Showing cached data (may not be the latest).",
                        processingSteps=tracker.to_list(),
                    )

            # Provider-change follow-up: give explicit message about unavailability
            if "intent" in locals() and intent:
                provider_change_response = self._build_provider_change_unavailable_response(
                    conv_id, intent, tracker,
                )
                if provider_change_response:
                    return provider_change_response

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
            # Error: restore previous good intent so follow-ups aren't corrupted
            if '_prev_good_intent' in locals() and _prev_good_intent is not None:
                conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
            if '_prev_good_state' in locals() and _prev_good_state is not None:
                conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
            return QueryResponse(
                conversationId=conv_id,
                intent=intent if "intent" in locals() else None,
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

            # Provider-change follow-up: give explicit message about unavailability
            if "intent" in locals() and intent:
                provider_change_response = self._build_provider_change_unavailable_response(
                    conv_id, intent, tracker,
                )
                if provider_change_response:
                    return provider_change_response

            # Format error message with helpful context
            formatted_message = QueryComplexityAnalyzer.format_error_message(
                str(exc), query, intent if 'intent' in locals() else None
            )
            # Error: restore previous good intent so follow-ups aren't corrupted
            if '_prev_good_intent' in locals() and _prev_good_intent is not None:
                conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
            if '_prev_good_state' in locals() and _prev_good_state is not None:
                conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
            return QueryResponse(
                conversationId=conv_id,
                clarificationNeeded=False,
                error="processing_error",
                message=formatted_message,
                processingSteps=tracker.to_list(),
            )
        finally:
            if tracker_token is not None:
                reset_processing_tracker(tracker_token)

    async def _fetch_multi_indicator_data(self, intent: ParsedIntent) -> List[NormalizedData]:
        """Delegates to :func:`data_fetcher.fetch_multi_indicator_data`."""
        return await _df_fetch_multi_indicator_data(self, intent)

    async def _fetch_data(
        self,
        intent: ParsedIntent,
        execution_plan: Optional[ExecutionPlan] = None,
    ) -> List[NormalizedData]:
        """Delegates to :func:`data_fetcher.fetch_data`."""
        return await _df_fetch_data(self, intent, execution_plan=execution_plan)

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

            # Build conversation_context so the LLM can detect follow-ups
            # (same logic as deterministic pipeline path).
            orch_conversation_context = None
            orch_last_intent = conversation_manager.get_last_intent(conversation_id)
            if orch_last_intent:
                li_params = orch_last_intent.parameters or {}
                prior_country = li_params.get("country", "")
                prior_countries_list = li_params.get("countries")
                if prior_countries_list and isinstance(prior_countries_list, list):
                    country_str = ", ".join(str(c) for c in prior_countries_list)
                elif prior_country:
                    country_str = str(prior_country)
                else:
                    country_str = "not specified"
                orch_conversation_context = {
                    "indicator": ", ".join(orch_last_intent.indicators) if orch_last_intent.indicators else "not specified",
                    "country": country_str,
                    "provider": orch_last_intent.apiProvider or "not specified",
                    "startDate": li_params.get("startDate", "not specified"),
                    "endDate": li_params.get("endDate", "not specified"),
                    "originalQuery": orch_last_intent.originalQuery or "not specified",
                }
                if orch_last_intent.clarificationNeeded:
                    pending_ctx = conversation_manager.get_pending_clarification_context(conversation_id)
                    if pending_ctx:
                        orch_conversation_context["pendingClarification"] = True
                        orch_conversation_context["clarificationQuestion"] = pending_ctx.get("question", "")
                        orch_conversation_context["clarificationOptions"] = ", ".join(
                            str(opt) for opt in (pending_ctx.get("options") or [])
                        )
                        original_from_pending = pending_ctx.get("original_query", "")
                        if original_from_pending:
                            orch_conversation_context["originalQuery"] = original_from_pending
                    elif orch_last_intent.clarificationQuestions:
                        orch_conversation_context["pendingClarification"] = True
                        orch_conversation_context["clarificationQuestion"] = " ".join(
                            orch_last_intent.clarificationQuestions
                        )
                        orch_conversation_context["clarificationOptions"] = ""
                    conversation_manager.clear_all_pending(conversation_id)

            # Run the same post-parse clarification guardrails used by the
            # standard pipeline before agent orchestration takes over.
            if tracker:
                with tracker.track("parsing_query", "🤖 Understanding your question...") as update_parse_metadata:
                    parse_result = await self.pipeline.parse_and_route(
                        query, conversation_history, conversation_context=orch_conversation_context,
                    )
                    intent = parse_result.intent
                    update_parse_metadata({
                        "provider": intent.apiProvider,
                        "indicators": intent.indicators,
                    })
            else:
                parse_result = await self.pipeline.parse_and_route(
                    query, conversation_history, conversation_context=orch_conversation_context,
                )
                intent = parse_result.intent

            self._maybe_resolve_region_clarification(query, intent)
            self._maybe_resolve_temporal_comparison_clarification(query, intent)
            self._maybe_expand_multi_concept_intent(query, intent)

            if intent.clarificationNeeded:
                semantic_clarification = self._build_structured_semantic_clarification(
                    conversation_id=conversation_id,
                    query=query,
                    intent=intent,
                    processing_steps=tracker.to_list() if tracker else None,
                )
                if semantic_clarification is not None:
                    return semantic_clarification
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

        conv_id = conversation_id
        execution_plan = self._build_minimal_execution_plan(query, intent)
        _prev_good_intent = conversation_manager.get_last_intent(conv_id)
        _prev_good_state = conversation_manager.get_conversation_state(conv_id)
        _pending_conv_state = None

        try:
            _new_state = extract_state_from_intent(intent, statscan_provider=self.statscan_provider)
            _new_state = merge_new_state_with_previous(_new_state, _prev_good_state)
            _pending_conv_state = _new_state
            if not self._use_staged_state_commit():
                conversation_manager.set_conversation_state(conv_id, _new_state)
        except Exception as _state_prepare_exc:
            logger.warning(
                "Failed to prepare conversation state for standard pipeline: %s",
                _state_prepare_exc,
            )

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
                result = await self._fetch_data(intent, execution_plan=execution_plan)
            if result:
                # _fetch_data may return QueryResponse or list of NormalizedData
                if isinstance(result, QueryResponse):
                    if not result.conversationId:
                        result.conversationId = conversation_id
                    if not result.intent:
                        result.intent = intent
                    if result.data and not result.alternativeSeries:
                        result.alternativeSeries = self._build_alternative_series(intent, result.data)
                    if result.data:
                        verification_error = await self._verify_execution_result(
                            query,
                            intent,
                            execution_plan,
                            result.data,
                        )
                        if verification_error:
                            if _prev_good_intent is not None:
                                conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                            if _prev_good_state is not None:
                                conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                            return self._build_verification_failed_response(
                                conversation_id=conv_id,
                                intent=intent,
                                message=verification_error,
                                processing_steps=tracker.to_list() if tracker else None,
                            )
                    # Dual-write conversation state (QueryResponse branch)
                    try:
                        _qs = _pending_conv_state
                        if _qs is None:
                            _qs = extract_state_from_intent(intent, statscan_provider=self.statscan_provider)
                            _ex = conversation_manager.get_conversation_state(conv_id)
                            _qs = merge_new_state_with_previous(_qs, _ex)
                        # Ensure cube metadata for StatsCan dimension follow-ups
                        if (_qs.statscan_product_id
                                and not _qs.statscan_cube_metadata
                                and _qs.provider
                                and _qs.provider.upper() in {"STATSCAN", "STATISTICS CANADA"}):
                            try:
                                _cube = await self.statscan_provider._get_cube_metadata(
                                    _qs.statscan_product_id
                                )
                                if _cube:
                                    _qs.statscan_cube_metadata = _cube
                            except Exception as _cube_exc:
                                logger.debug("Non-critical: statscan cube metadata fetch failed (QueryResponse branch): %s", _cube_exc)
                        if self._use_staged_state_commit():
                            if result.data:
                                update_answer_members_from_data(
                                    _qs,
                                    result.data,
                                    intent=intent,
                                )
                            conversation_manager.set_conversation_state(conv_id, _qs)
                        else:
                            if result.data:
                                update_answer_members_from_data(
                                    _qs,
                                    result.data,
                                    intent=intent,
                                )
                            conversation_manager.set_conversation_state(conv_id, _qs)
                    except Exception as _state_exc:
                        logger.warning("Failed to save conversation state (QueryResponse branch): %s", _state_exc)
                    return result
                elif isinstance(result, list):
                    # Rerank and project before returning
                    result = self._rerank_data_by_query_relevance(query, result, is_multi_indicator=is_multi_indicator)
                    result = self._apply_ranking_projection(query, result)
                    result, coverage_warning = await self._maybe_improve_country_coverage(
                        query, intent, result,
                    )
                    verification_error = await self._verify_execution_result(
                        query,
                        intent,
                        execution_plan,
                        result,
                    )
                    if verification_error:
                        if _prev_good_intent is not None:
                            conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                        if _prev_good_state is not None:
                            conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                        return self._build_verification_failed_response(
                            conversation_id=conv_id,
                            intent=intent,
                            message=verification_error,
                            processing_steps=tracker.to_list() if tracker else None,
                        )
                    alternatives = self._build_alternative_series(intent, result)
                    conversation_manager.add_message_safe(
                        conversation_id, "assistant",
                        f"Data fetched: {intent.apiProvider}",
                        intent=intent,
                    )
                    # Dual-write: save conversation state AFTER fetch
                    try:
                        _new_state = _pending_conv_state
                        if _new_state is None:
                            _new_state = extract_state_from_intent(
                                intent, statscan_provider=self.statscan_provider
                            )
                            _existing = conversation_manager.get_conversation_state(conv_id)
                            _new_state = merge_new_state_with_previous(_new_state, _existing)
                        # Explicitly fetch cube metadata for StatsCan dimension follow-ups.
                        # Vector-based fetches don't call _get_cube_metadata, so the
                        # provider's cache may be empty. We fetch it here so R2 has it.
                        if (_new_state.statscan_product_id
                                and not _new_state.statscan_cube_metadata
                                and _new_state.provider
                                and _new_state.provider.upper() in {"STATSCAN", "STATISTICS CANADA"}):
                            try:
                                _cube = await self.statscan_provider._get_cube_metadata(
                                    _new_state.statscan_product_id
                                )
                                if _cube:
                                    _new_state.statscan_cube_metadata = _cube
                            except Exception as _cube_exc:
                                logger.debug("Non-critical: statscan cube metadata fetch failed (list branch): %s", _cube_exc)
                        update_answer_members_from_data(
                            _new_state,
                            result,
                            intent=intent,
                        )
                        conversation_manager.set_conversation_state(conv_id, _new_state)
                    except Exception as _state_exc:
                        logger.warning("Failed to save conversation state (list branch): %s", _state_exc)
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
                verification_error = await self._verify_execution_result(
                    query,
                    intent,
                    execution_plan,
                    fallback_data,
                )
                if verification_error:
                    if _prev_good_intent is not None:
                        conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                    if _prev_good_state is not None:
                        conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                    return self._build_verification_failed_response(
                        conversation_id=conv_id,
                        intent=intent,
                        message=verification_error,
                        processing_steps=tracker.to_list() if tracker else None,
                    )
                self._persist_verified_conversation_state(
                    conv_id,
                    _pending_conv_state,
                    fallback_data,
                    intent=intent,
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
                verification_error = await self._verify_execution_result(
                    query,
                    intent,
                    execution_plan,
                    recovered_data,
                )
                if verification_error:
                    if _prev_good_intent is not None:
                        conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                    if _prev_good_state is not None:
                        conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                    return self._build_verification_failed_response(
                        conversation_id=conv_id,
                        intent=intent,
                        message=verification_error,
                        processing_steps=tracker.to_list() if tracker else None,
                    )
                self._persist_verified_conversation_state(
                    conv_id,
                    _pending_conv_state,
                    recovered_data,
                    intent=intent,
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
                verification_error = await self._verify_execution_result(
                    query,
                    intent,
                    execution_plan,
                    stale_list,
                )
                if verification_error:
                    if _prev_good_intent is not None:
                        conversation_manager.restore_last_intent(conv_id, _prev_good_intent)
                    if _prev_good_state is not None:
                        conversation_manager.restore_conversation_state(conv_id, _prev_good_state)
                    return self._build_verification_failed_response(
                        conversation_id=conversation_id,
                        intent=intent,
                        message=verification_error,
                        processing_steps=tracker.to_list() if tracker else None,
                    )
                self._persist_verified_conversation_state(
                    conv_id,
                    _pending_conv_state,
                    stale_list,
                    intent=intent,
                )
                return QueryResponse(
                    conversationId=conversation_id,
                    intent=intent,
                    data=stale_list,
                    clarificationNeeded=False,
                    message="The data provider is temporarily unavailable. Showing cached data (may not be the latest).",
                    processingSteps=tracker.to_list() if tracker else None,
                )
        except Exception as _stale_exc:
            logger.warning("Stale cache retrieval failed (last-resort fallback): %s", _stale_exc)

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
                countries = intent.parameters.get("countries") or []
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
        """Delegates to :func:`query_helpers.should_use_deep_agents`."""
        from .query_helpers import should_use_deep_agents as _qh_should_deep
        return _qh_should_deep(self, query)

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
                        country = params.get("country") or (params.get("countries") or [""])[0] if params else ""
                    elif hasattr(parsed_intent, "apiProvider"):
                        if provider_name == "Unknown":
                            provider_name = parsed_intent.apiProvider or "Unknown"
                        indicators = ", ".join(parsed_intent.indicators) if parsed_intent.indicators else "requested indicator"
                        params = parsed_intent.parameters or {}
                        country = params.get("country") or (params.get("countries") or [""])[0] if params else ""

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
        execution_plan = self._build_minimal_execution_plan(query, intent)
        previous_intent = conversation_manager.get_last_intent(conversation_id)

        if record_user_message:
            conversation_id = conversation_manager.add_message_safe(
                conversation_id,
                "user",
                query,
                intent=intent,
            )

        if intent.clarificationNeeded:
            semantic_clarification = self._build_structured_semantic_clarification(
                conversation_id=conversation_id,
                query=query,
                intent=intent,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if semantic_clarification is not None:
                return semantic_clarification
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
            lambda: self._fetch_data(intent, execution_plan=execution_plan),
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
            country = intent.parameters.get("country") or (intent.parameters.get("countries") or [""])[0] if intent.parameters else ""
            no_data_clarification = self._build_no_data_indicator_clarification(
                conversation_id=conversation_id,
                query=query,
                intent=intent,
                processing_steps=tracker.to_list() if tracker else None,
            )
            if no_data_clarification:
                return no_data_clarification
            conversation_manager.restore_last_intent(conversation_id, previous_intent)
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

        verification_error = await self._verify_execution_result(
            query,
            intent,
            execution_plan,
            data,
        )
        if verification_error:
            conversation_manager.restore_last_intent(conversation_id, previous_intent)
            return self._build_verification_failed_response(
                conversation_id=conversation_id,
                intent=intent,
                message=verification_error,
                processing_steps=tracker.to_list() if tracker else None,
            )

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
        """Delegates to :func:`query_helpers.build_alternative_series`."""
        from .query_helpers import build_alternative_series as _qh_build_alt
        return _qh_build_alt(intent, data)

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

                    # Extract indicator text directly from the query.  Product
                    # discovery must use metadata search; provider-local known
                    # product maps are intentionally unavailable on the
                    # no-shortcut path.
                    query_lower = query.lower()
                    metadata_service = get_statscan_metadata_service()
                    indicator_found = query_lower.strip()

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
        if intent.decompositionEntities is None:
            intent.decompositionEntities = []
        logger.info("🔄 Starting query decomposition for %d %s",
                   len(intent.decompositionEntities), intent.decompositionType)

        if normalize_provider_name(intent.apiProvider) == "STATSCAN" and intent.decompositionType in ["age groups", "age_group", "ages"]:
            if not intent.decompositionEntities:
                try:
                    product_id = self._infer_statscan_product_id_for_followup(conversation_id, intent)
                    if product_id:
                        age_members = await self.statscan_provider.get_dimension_members(product_id, "age")
                        age_entities = [
                            name for _, name in age_members
                            if name
                            and str(name).strip()
                            and "total" not in str(name).lower()
                            and "all ages" not in str(name).lower()
                            and "15 years and over" not in str(name).lower()
                        ]
                        if age_entities:
                            intent.decompositionEntities = age_entities
                            logger.info(
                                "Resolved StatsCan age-group decomposition entities from product %s: %d entities",
                                product_id,
                                len(age_entities),
                            )
                except Exception as exc:
                    logger.warning("Failed to resolve StatsCan age-group decomposition entities: %s", exc)

        # Check if provider has batch method for efficient multi-entity queries
        # This avoids timeouts by making single API call instead of N parallel requests
        if normalize_provider_name(intent.apiProvider) == "STATSCAN" and intent.decompositionType == "dimension":
            resolved_params = dict(intent.parameters or {})
            dimension_axis = str(resolved_params.get("__statscan_decomposition_axis") or "").strip()
            product_id = (
                self._infer_statscan_product_id_for_followup(conversation_id, intent)
                or resolved_params.get("__statscan_product_id")
                or resolved_params.get("productId")
            )
            product_id = self._extract_statscan_product_id(product_id)
            provider_locked_dimension_request = bool(
                resolved_params.get("__exact_indicator_title_match")
                or resolved_params.get("__exact_provider_code_match")
                or _df_has_statscan_mechanical_dimension_dispatch_authority(resolved_params)
            )
            if (
                provider_locked_dimension_request
                and dimension_axis
                and product_id
                and hasattr(self.statscan_provider, "fetch_multi_dimension_data")
            ):
                try:
                    normalized_product_id = self.statscan_provider._normalize_metadata_product_id(product_id)
                    if intent.parameters is None:
                        intent.parameters = {}
                    intent.parameters["indicator"] = normalized_product_id
                    intent.parameters["__statscan_product_id"] = normalized_product_id

                    indicator_name = intent.indicators[0] if intent.indicators else normalized_product_id
                    dimensions = (
                        resolved_params.get("dimensions")
                        or resolved_params.get("__dimensions")
                        or {}
                    )
                    params = {
                        "productId": normalized_product_id,
                        "indicator": resolved_params.get("__base_indicator") or resolved_params.get("indicator") or indicator_name,
                        "indicatorLabel": str(resolved_params.get("__semantic_indicator_label") or indicator_name),
                        "axis": dimension_axis,
                        "periods": _df_statscan_periods_from_date_range(resolved_params, 60),
                        "dimensions": dimensions,
                        "startDate": resolved_params.get("startDate"),
                        "endDate": resolved_params.get("endDate"),
                    }
                    logger.info(
                        "🚀 Using StatsCan dimension batch method for product=%s axis=%s",
                        normalized_product_id,
                        dimension_axis,
                    )
                    batch_results = await self.statscan_provider.fetch_multi_dimension_data(params)
                    logger.info("✅ StatsCan dimension batch method completed: %d series returned", len(batch_results))
                    return batch_results if isinstance(batch_results, list) else [batch_results]
                except Exception as e:
                    if "statscan_required_dimension_missing" in str(e):
                        raise
                    logger.warning(
                        "⚠️ StatsCan dimension batch method failed (%s), falling back to parallel decomposition",
                        str(e),
                    )

        if normalize_provider_name(intent.apiProvider) == "STATSCAN" and intent.decompositionType in ["provinces", "regions", "territories"]:
            if hasattr(self.statscan_provider, 'fetch_multi_province_data'):
                logger.info("🚀 Using batch method for %d %s (single API call)",
                           len(intent.decompositionEntities), intent.decompositionType)

                try:
                    resolved_provider = normalize_provider_name(intent.apiProvider)
                    resolved_params = dict(intent.parameters or {})
                    intent.apiProvider = resolved_provider
                    intent.parameters = await self._resolve_indicator_for_fetch(
                        resolved_provider,
                        intent,
                        resolved_params,
                    )

                    # Resolve indicator to a product ID for the batch method.
                    # Prefer product evidence saved from the selected StatsCan
                    # candidate/execution state. If absent, exact vector IDs may
                    # be converted mechanically through provider metadata.
                    indicator_name = intent.indicators[0] if intent.indicators else "Population"
                    product_id = self._infer_statscan_product_id_for_followup(conversation_id, intent)
                    if product_id:
                        logger.info("Decomposition: using follow-up product %s from saved StatsCan execution state", product_id)
                    else:
                        _vec_id = await self.statscan_provider._vector_id(
                            str(intent.parameters.get("indicator") or indicator_name),
                            intent.parameters.get("vectorId")
                        )
                        _cached_pid = self.statscan_provider.PRODUCT_ID_CACHE.get(_vec_id)
                        if _cached_pid:
                            product_id = self.statscan_provider._normalize_metadata_product_id(_cached_pid)
                            logger.info("Decomposition: exact/discovered vector %s → product %s", _vec_id, product_id)
                        else:
                            product_id = str(_vec_id)
                            logger.warning("Decomposition: no product ID for vector %s, using as-is", _vec_id)

                    candidate_product_ids: list[str] = []
                    for candidate in [
                        product_id,
                        (intent.parameters or {}).get("__statscan_product_id"),
                        (intent.parameters or {}).get("indicator"),
                        *(intent.indicators or []),
                    ]:
                        extracted = self._extract_statscan_product_id(candidate)
                        if extracted and extracted not in candidate_product_ids:
                            candidate_product_ids.append(extracted)

                    if len(candidate_product_ids) > 1:
                        scored_candidates: list[tuple[int, str]] = []
                        for candidate_product_id in candidate_product_ids:
                            try:
                                cube_meta = await self.statscan_provider._get_cube_metadata(candidate_product_id)
                                dimension_count = len(cube_meta.get("dimension", []) or [])
                            except Exception:
                                dimension_count = -1
                            scored_candidates.append((dimension_count, candidate_product_id))
                        scored_candidates.sort(key=lambda item: (item[0], -candidate_product_ids.index(item[1])), reverse=True)
                        best_product_id = scored_candidates[0][1]
                        if best_product_id != product_id:
                            logger.info(
                                "Decomposition: preferring richer StatsCan product %s over %s among candidates=%s",
                                best_product_id,
                                product_id,
                                candidate_product_ids,
                            )
                        product_id = best_product_id

                    if product_id:
                        normalized_product_id = self.statscan_provider._normalize_metadata_product_id(product_id)
                        if intent.parameters is None:
                            intent.parameters = {}
                        intent.parameters["indicator"] = normalized_product_id
                        intent.parameters["__statscan_product_id"] = normalized_product_id

                    # Build parameters for batch method
                    params = {
                        "productId": intent.parameters.get("__statscan_product_id") or product_id,
                        "indicator": indicator_name,
                        "indicatorLabel": str((intent.parameters or {}).get("__semantic_indicator_label") or indicator_name),
                        "provinces": intent.decompositionEntities,
                        "periods": _df_statscan_periods_from_date_range(intent.parameters or {}, 20),
                        "dimensions": intent.parameters.get("dimensions", {}),
                        "startDate": intent.parameters.get("startDate"),
                        "endDate": intent.parameters.get("endDate"),
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

        # Execute sub-queries in parallel using asyncio.gather.
        # When decomposing into 3+ sub-queries, extend HTTP timeouts to
        # prevent premature failures under parallel load.
        _use_extended = len(sub_queries) >= 3
        if tracker:
            with tracker.track("fetching_data", f"📥 Fetching data for {len(sub_queries)} {intent.decompositionType}..."):
                if _use_extended:
                    with extended_timeout(120.0):
                        results = await asyncio.gather(*[
                            self._execute_sub_query(entity, sq, intent, conversation_id)
                            for entity, sq in sub_queries
                        ], return_exceptions=True)
                else:
                    results = await asyncio.gather(*[
                        self._execute_sub_query(entity, sq, intent, conversation_id)
                        for entity, sq in sub_queries
                    ], return_exceptions=True)
        else:
            if _use_extended:
                with extended_timeout(120.0):
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
            raise DataNotAvailableError(f"All sub-queries failed for {intent.decompositionType}")

        return aggregated_data

    def _generate_sub_query(self, original_query: str, entity: str, decomposition_type: str) -> str:
        """
        Generate a sub-query for a specific entity.

        Delegates to :func:`query_decomposition.generate_sub_query`.
        """
        return generate_sub_query(original_query, entity, decomposition_type)

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
            sub_indicators = list(original_intent.indicators or [])
            if original_intent.decompositionType == "countries":
                # Country decomposition must bind each sub-query to a single country.
                sub_params["country"] = entity
                sub_params.pop("countries", None)
            elif normalize_provider_name(original_intent.apiProvider) == "STATSCAN":
                existing_dimensions = dict(sub_params.get("__dimensions") or sub_params.get("dimensions") or {})
                decomposition_axis = str(sub_params.get("__statscan_decomposition_axis") or "").strip().lower()
                if (
                    original_intent.decompositionType in {"age groups", "age_group", "age_groups", "ages"}
                    or (original_intent.decompositionType == "dimension" and "age" in decomposition_axis)
                ):
                    existing_dimensions["age"] = entity
                elif (
                    original_intent.decompositionType in {"sex", "gender"}
                    or (original_intent.decompositionType == "dimension" and any(token in decomposition_axis for token in ("sex", "gender")))
                ):
                    existing_dimensions["sex"] = entity
                if existing_dimensions:
                    sub_params["__dimensions"] = existing_dimensions
                    sub_params["dimensions"] = existing_dimensions
                    sub_params.pop("__statscan_decomposition_axis", None)
                product_id = self._infer_statscan_product_id_for_followup(conversation_id, original_intent)
                if product_id:
                    sub_params["indicator"] = product_id
                    sub_params["__statscan_product_id"] = product_id
                semantic_label = str(sub_params.get("__semantic_indicator_label") or "").strip()
                if semantic_label:
                    sub_indicators = [semantic_label]

            # Create a modified intent for this entity
            sub_intent = ParsedIntent(
                apiProvider=original_intent.apiProvider,
                indicators=sub_indicators or original_intent.indicators,
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
