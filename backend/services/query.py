from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import List, Optional

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
from ..routing.country_resolver import CountryResolver
from ..routing.hybrid_router import HybridRouter
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

        # Optional hybrid router: deterministic candidates + LLM ranking.
        self.hybrid_router: Optional[HybridRouter] = None
        if self.settings.use_hybrid_router:
            self.hybrid_router = HybridRouter(llm_provider=self.openrouter.llm_provider)
            logger.info("🧠 HybridRouter enabled (USE_HYBRID_ROUTER=true)")

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
        Select provider using deterministic router, optionally enhanced by HybridRouter.
        """
        routed_provider = ProviderRouter.route_provider(intent, query)
        routed_provider = ProviderRouter.correct_coingecko_misrouting(
            routed_provider,
            query,
            intent.indicators,
        )

        if not self.hybrid_router:
            return routed_provider

        try:
            params = intent.parameters or {}
            raw_countries = params.get("countries")
            countries = raw_countries if isinstance(raw_countries, list) else []
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
            "unemployment": {"unemployment", "jobless"},
            "inflation": {"inflation", "consumer price", "cpi"},
            "savings": {"saving", "savings"},
        }

        cues: set[str] = set()
        for cue, phrases in cue_map.items():
            if any(phrase in text_lower for phrase in phrases):
                cues.add(cue)
        return cues

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

    async def _get_from_cache(self, provider: str, params: dict):
        """
        Get data from cache (Redis first, then in-memory).

        Args:
            provider: Data provider name
            params: Query parameters

        Returns:
            Cached data if available, None otherwise
        """
        # Try Redis cache first
        try:
            redis_cache = await get_redis_cache()
            query_key = str(params)  # Simple key generation
            cached_data = await redis_cache.get(provider, query_key, params)
            if cached_data:
                logger.info(f"Redis cache hit for {provider}")
                return cached_data
        except Exception as e:
            logger.warning(f"Redis cache error: {e}, falling back to in-memory")

        # Fallback to in-memory cache
        cached_data = cache_service.get_data(provider, params)
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
        # Save to Redis cache
        try:
            redis_cache = await get_redis_cache()
            query_key = str(params)
            await redis_cache.set(provider, query_key, data, params)
            logger.debug(f"Saved to Redis cache: {provider}")
        except Exception as e:
            logger.warning(f"Failed to save to Redis: {e}")

        # Always save to in-memory cache as backup
        cache_service.cache_data(provider, params, data)
        logger.debug(f"Saved to in-memory cache: {provider}")

    def _get_fallback_providers(self, primary_provider: str, indicator: Optional[str] = None) -> List[str]:
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

        Returns:
            List of fallback provider names to try in order
        """
        # General multi-level fallback chains for better data availability
        # These are provider-to-provider mappings regardless of indicator
        general_fallback_chains = {
            "WORLDBANK": ["OECD", "IMF", "EUROSTAT"],
            "OECD": ["WORLDBANK", "EUROSTAT", "IMF"],
            "EUROSTAT": ["WORLDBANK", "OECD", "IMF"],
            "IMF": ["WORLDBANK", "OECD", "BIS"],
            "BIS": ["IMF", "WORLDBANK", "OECD"],  # BIS -> IMF for financial data
            "STATSCAN": ["WORLDBANK", "OECD", "IMF"],  # Added IMF for financial indicators
            "FRED": ["WORLDBANK", "OECD", "IMF"],
            "EXCHANGERATE": ["FRED", "BIS"],
            "COINGECKO": ["FRED"],
            "COMTRADE": ["WORLDBANK"],
        }

        fallback_list = general_fallback_chains.get(primary_provider.upper(), [])
        primary_upper = primary_provider.upper()

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
                    resolved = resolver.resolve(indicator, provider=provider)
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
                    return combined[:5]  # Limit to 5 fallbacks

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
                    return combined
            except Exception as e:
                logger.debug(f"Could not get catalog-based fallbacks: {e}")

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
        self, original_indicators: List[str], fallback_result: List[NormalizedData],
        target_country: Optional[str] = None
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
            target_country: Optional country the query is targeting

        Returns:
            True if fallback data is relevant, False otherwise
        """
        if not fallback_result or not original_indicators:
            return False

        # INFRASTRUCTURE FIX: Country validation
        # Reject data that is for a different country than requested
        if target_country:
            target_upper = target_country.upper()
            # Normalize country names for comparison
            country_aliases = {
                'CANADA': ['CANADA', 'CA', 'CAN'],
                'UNITED STATES': ['UNITED STATES', 'US', 'USA', 'AMERICA'],
                'GERMANY': ['GERMANY', 'DE', 'DEU'],
                'FRANCE': ['FRANCE', 'FR', 'FRA'],
                'UNITED KINGDOM': ['UNITED KINGDOM', 'UK', 'GB', 'GBR', 'BRITAIN'],
                'JAPAN': ['JAPAN', 'JP', 'JPN'],
                'CHINA': ['CHINA', 'CN', 'CHN'],
                'INDIA': ['INDIA', 'IN', 'IND'],
            }

            # Find canonical name for target country
            target_canonical = target_upper
            for canonical, aliases in country_aliases.items():
                if target_upper in aliases:
                    target_canonical = canonical
                    target_aliases = set(aliases)
                    break
            else:
                target_aliases = {target_upper}

            # Check each result for country match
            for data in fallback_result:
                if data.metadata and data.metadata.country:
                    result_country = data.metadata.country.upper()
                    # Find canonical name for result country
                    result_canonical = result_country
                    for canonical, aliases in country_aliases.items():
                        if result_country in aliases:
                            result_canonical = canonical
                            break

                    # If countries don't match, reject the fallback
                    if target_canonical != result_canonical and result_country not in target_aliases:
                        logger.warning(
                            f"Fallback rejected: country mismatch - "
                            f"requested '{target_country}' but got '{data.metadata.country}'"
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

        # Get terms from original indicators
        original_text = ' '.join(original_indicators).lower()
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
                if not (original_metrics & result_metrics):
                    logger.warning(
                        f"Fallback rejected: metrics don't match - original={original_metrics}, result={result_metrics}"
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
        fallback_providers = self._get_fallback_providers(primary_provider, indicator)

        if not fallback_providers:
            raise primary_error

        last_error = primary_error
        for fallback_provider in fallback_providers:
            logger.warning(f"Attempting fallback from {primary_provider} to {fallback_provider}")

            # Create a modified intent for the fallback provider
            fallback_intent = ParsedIntent(
                apiProvider=fallback_provider,
                indicators=intent.indicators,
                parameters=intent.parameters,
                clarificationNeeded=False
            )

            try:
                result = await self._fetch_data(fallback_intent)

                # Validate fallback result is semantically related to original query
                # INFRASTRUCTURE FIX: Pass target country for country-aware validation
                target_country = intent.parameters.get("country") if intent.parameters else None
                if result and self._is_fallback_relevant(intent.indicators, result, target_country):
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

    async def process_query(self, query: str, conversation_id: Optional[str] = None, auto_pro_mode: bool = True, use_orchestrator: bool = False) -> QueryResponse:
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
            if use_orchestrator or settings.use_langchain_orchestrator:
                logger.info("🤖 Using LangChain orchestrator for intelligent query routing")
                return await self._execute_with_orchestrator(query, conv_id, tracker)

            # Early complexity detection (before LLM parsing)
            early_complexity = QueryComplexityAnalyzer.detect_complexity(query, intent=None)

            # If query REQUIRES Pro Mode, automatically switch
            if auto_pro_mode and early_complexity['pro_mode_required']:
                logger.info("🚀 Auto-switching to Pro Mode (detected: %s)", early_complexity['complexity_factors'])
                return await self._execute_pro_mode(query, conv_id)

            logger.info("Parsing query with LLM: %s", query)

            # Detect explicit provider requests BEFORE LLM parsing
            # This ensures user's explicit provider choice is always honored
            explicit_provider = self._detect_explicit_provider(query)
            if explicit_provider:
                logger.info(f"🎯 Explicit provider detected: {explicit_provider}")

            with tracker.track("parsing_query", "🤖 Understanding your question...") as update_parse_metadata:
                intent = await self.openrouter.parse_query(query, history)
                logger.debug("Parsed intent: %s", intent.model_dump())

                # FALLBACK: Extract explicit country references from query when LLM defaults to US/empty.
                self._apply_country_overrides(intent, query)

                # Use deterministic routing (and optional HybridRouter enhancement)
                routed_provider = await self._select_routed_provider(intent, query)

                if routed_provider != intent.apiProvider:
                    logger.info(f"🔄 Provider routing: {intent.apiProvider} → {routed_provider} (ProviderRouter)")
                    intent.apiProvider = routed_provider

                # Validate routing decision (logs warnings if routing seems incorrect)
                validation_warning = ProviderRouter.validate_routing(routed_provider, query, intent)
                if validation_warning:
                    # Log but don't fail - warnings are informational
                    logger.warning(f"Routing validation: {validation_warning}")

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

            # Check if this is a multi-indicator query BEFORE parameter validation
            # (multi-indicator queries will add seriesId during fetch)
            is_multi_indicator = len(intent.indicators) > 1

            # Validate parameters before fetching (skip for multi-indicator as they'll be validated individually)
            # Do validation without tracking step to reduce progress bar clutter
            if not is_multi_indicator:
                is_valid, validation_error, suggestions = ParameterValidator.validate_intent(intent)
            else:
                # For multi-indicator, we'll validate each one separately during fetch
                is_valid = True
                validation_error = None
                suggestions = None

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

            # Check confidence level (skip for multi-indicator as each will be checked individually)
            # Do confidence check without tracking step to reduce progress bar clutter
            if not is_multi_indicator:
                is_confident, confidence_reason = ParameterValidator.check_confidence(intent)
            else:
                is_confident = True
                confidence_reason = None

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
                            conversationId=conversation_id or "",
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
                conversationId=conversation_id or "",
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
                            conversationId=conversation_id or "",
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
                conversationId=conversation_id or "",
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

        # Ensure indicator is in params for providers that need it
        # (when LLM parsed it into intent.indicators but not into params)
        if not params.get("indicator") and intent.indicators:
            if provider in {"STATSCAN", "STATISTICS CANADA", "FRED", "IMF", "WORLDBANK", "EUROSTAT", "OECD", "BIS"}:
                # Try IndicatorResolver first for enhanced resolution
                indicator_query = self._select_indicator_query_for_resolution(intent)
                resolved = resolver.resolve(indicator_query, provider=provider)
                if resolved and resolved.confidence >= 0.7:
                    logger.info(f"🔍 IndicatorResolver: '{indicator_query}' → '{resolved.code}' (confidence: {resolved.confidence:.2f}, source: {resolved.source})")
                    params = {**params, "indicator": resolved.code}
                else:
                    # Fallback to original indicator
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
        explicit_provider_requested = ProviderRouter.detect_explicit_provider(original_query)
        if explicit_provider_requested and explicit_provider_requested.upper() == provider:
            logger.info(f"📋 Skipping catalog override - user explicitly requested {provider}")
        elif indicator_term and provider:
            try:
                from .catalog_service import find_concept_by_term, get_best_provider, is_provider_available
                concept = find_concept_by_term(indicator_term)
                logger.info(f"📋 Catalog concept: '{concept}' for term '{indicator_term}'")
                if concept and not is_provider_available(concept, provider):
                    # Provider is in not_available list - find alternative
                    country = params.get("country") or params.get("region")
                    alt_result = get_best_provider(concept, country)
                    if alt_result:
                        alt_provider = alt_result[0] if isinstance(alt_result, tuple) else alt_result
                        if alt_provider and alt_provider.upper() != provider:
                            logger.info(f"📋 Catalog: {provider} not available for '{indicator_term}', routing to {alt_provider}")
                            intent.apiProvider = alt_provider
                            provider = normalize_provider_name(alt_provider)
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
                # Handle multiple indicators for World Bank
                if len(intent.indicators) > 1:
                    all_data = []
                    for indicator in intent.indicators:
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
                    indicator = intent.indicators[0] if intent.indicators else ""
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
                return await self.comtrade_provider.fetch_trade_data(
                    reporter=params.get("reporter") or params.get("country"),
                    reporters=params.get("reporters") or params.get("countries"),
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
                        indicator = intent.indicators[0] if intent.indicators else ""
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
                indicator = intent.indicators[0] if intent.indicators else "POLICY_RATE"
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
                indicator = intent.indicators[0] if intent.indicators else "GDP"
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
                indicator = intent.indicators[0] if intent.indicators else "GDP"
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
                        # Use parallel multi-country method
                        countries = countries_param if countries_param else [country_param]
                        series_list = await self.oecd_provider.fetch_multi_country(
                            indicator=indicator,
                            countries=countries,
                            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                        )
                        return series_list
                    except Exception as e:
                        logger.warning(f"Multi-country OECD query failed: {e}")

                        # Check if it's a rate limit, timeout, or availability issue
                        error_msg = str(e).lower()
                        if "rate limit" in error_msg or "429" in error_msg or "circuit" in error_msg or "failed" in error_msg or "timeout" in error_msg or "timed out" in error_msg:
                            logger.info("🔄 OECD multi-country failed, attempting WorldBank fallback...")
                            try:
                                countries = countries_param if countries_param else [country_param]
                                worldbank_data = await self.world_bank_provider.fetch_indicator(
                                    indicator=indicator,
                                    countries=countries,
                                    start_date=params.get("startDate"),
                                    end_date=params.get("endDate"),
                                )
                                if worldbank_data:
                                    logger.info(f"✅ WorldBank fallback succeeded for {len(worldbank_data)} countries")
                                    return worldbank_data
                            except Exception as wb_e:
                                logger.warning(f"WorldBank fallback also failed: {wb_e}")
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
                except (DataNotAvailableError, Exception) as e:
                    # Check if it's a rate limit, timeout, or circuit breaker error
                    error_msg = str(e).lower()
                    error_type = type(e).__name__.lower()
                    is_timeout = "timeout" in error_msg or "timed out" in error_msg or "timeout" in error_type
                    is_availability = "rate limit" in error_msg or "429" in error_msg or "circuit" in error_msg or "temporarily unavailable" in error_msg

                    if is_timeout or is_availability:
                        logger.warning(f"OECD unavailable for {params.get('country', 'USA')}: {e}")
                        logger.info("🔄 Attempting fallback providers (WorldBank, then IMF)...")

                        # First try WorldBank fallback (faster and more reliable)
                        try:
                            worldbank_data = await self.world_bank_provider.fetch_indicator(
                                indicator=indicator,
                                countries=[params.get("country", "USA")],
                                start_date=params.get("startDate"),
                                end_date=params.get("endDate"),
                            )
                            if worldbank_data:
                                logger.info(f"✅ WorldBank fallback succeeded")
                                return worldbank_data
                        except Exception as wb_error:
                            logger.warning(f"WorldBank fallback failed: {wb_error}")

                        # Fallback to IMF provider
                        try:
                            # IMF provider needs slightly different parameters
                            imf_params = {
                                **params,
                                "indicator": indicator,
                                "countries": params.get("countries") or [params.get("country", "USA")]
                            }

                            # Handle multiple indicators for IMF
                            if len(intent.indicators) > 1:
                                all_data = []
                                for ind in intent.indicators:
                                    imf_params["indicator"] = ind
                                    data = await self.imf_provider.fetch_data(imf_params)
                                    all_data.extend(data if isinstance(data, list) else [data])
                                return all_data
                            else:
                                data = await self.imf_provider.fetch_data(imf_params)
                                return data if isinstance(data, list) else [data]
                        except Exception as imf_error:
                            logger.error(f"IMF fallback also failed: {imf_error}")
                            # Re-raise original OECD error if all fallbacks fail
                            raise e
                    else:
                        # Not a rate limit/timeout error, raise as-is
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
            conversation_manager.add_message_safe(conversation_id, "user", query)

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
                conversation_manager.add_message_safe(
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
            return await self._standard_query_processing(query, conversation_id, tracker)

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

                todos = result.get("todos", [])
                message = None
                if todos:
                    completed = sum(1 for t in todos if t.get("status") == "completed")
                    message = f"Completed {completed}/{len(todos)} planned tasks"

                # Add to conversation history
                conversation_manager.add_message_safe(
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
        from backend.agents import get_agent_graph, create_initial_state
        from backend.memory.state_manager import get_state_manager
        from backend.memory.conversation_state import EntityContext
        from langchain_core.messages import HumanMessage, AIMessage

        logger.info("🔄 Using LangGraph agent orchestration")

        try:
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
                        if isinstance(parsed_intent, dict):
                            fallback_intent = ParsedIntent(
                                apiProvider=parsed_intent.get("apiProvider", "Unknown"),
                                indicators=parsed_intent.get("indicators", []),
                                parameters=parsed_intent.get("parameters", {}),
                                clarificationNeeded=False
                            )
                        else:
                            fallback_intent = parsed_intent

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
                        # Build ParsedIntent if it's a dict
                        if isinstance(parsed_intent, dict):
                            fallback_intent = ParsedIntent(
                                apiProvider=parsed_intent.get("apiProvider", provider_name),
                                indicators=parsed_intent.get("indicators", []),
                                parameters=parsed_intent.get("parameters", {}),
                                clarificationNeeded=False
                            )
                        else:
                            fallback_intent = parsed_intent

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
                if data and len(data) > 0:
                    first_data = data[0]
                    response.intent = ParsedIntent(
                        apiProvider=first_data.metadata.source if first_data.metadata else "UNKNOWN",
                        indicators=[d.metadata.indicator for d in data if d.metadata],
                        parameters={
                            "merge_with_previous": query_result.get("merge_series", False),
                        },
                        clarificationNeeded=False,
                        recommendedChartType=query_result.get("chart_type", "line"),
                    )

            # If research query, add message
            if result.get("query_type") == "research":
                response.message = query_result.get("message", "")

            # Add to conversation history
            conversation_manager.add_message_safe(
                conversation_id,
                "assistant",
                f"Query processed: {result.get('query_type', 'data_fetch')}"
            )

            return response

        except Exception as e:
            logger.exception(f"LangGraph execution error: {e}")
            # Fall back to standard processing
            logger.warning("Falling back to standard query processing")
            return await self._standard_query_processing(query, conversation_id, tracker)

    async def _standard_query_processing(
        self,
        query: str,
        conversation_id: str,
        tracker: Optional['ProcessingTracker'] = None
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
                intent = await self.openrouter.parse_query(query, history)
        else:
            intent = await self.openrouter.parse_query(query, history)

        # Keep fallback path behavior consistent with the main processing pipeline.
        self._apply_country_overrides(intent, query)
        routed_provider = await self._select_routed_provider(intent, query)
        if routed_provider != intent.apiProvider:
            logger.info(
                "🔄 Provider routing (standard path): %s -> %s",
                intent.apiProvider,
                routed_provider,
            )
            intent.apiProvider = routed_provider

        conversation_manager.add_message_safe(conversation_id, "user", query, intent=intent)

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

        conversation_manager.add_message_safe(
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
                conversation_manager.add_message_safe(conversation_id, "user", query)

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
                    conversation_manager.add_message_safe(
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

                conversation_manager.add_message_safe(conversation_id, "user", query)

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

                conversation_manager.add_message_safe(
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
