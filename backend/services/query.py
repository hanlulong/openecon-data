from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
from ..services.provider_router import ProviderRouter
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
from ..services.semantic_clarifier import SemanticClarifier
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
        self.semantic_clarifier = SemanticClarifier()
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
        query_lower = query.lower()

        # Provider keywords with their variations
        provider_patterns = {
            # Bare "oecd" can denote country group context (for example "OECD economies"),
            # not an explicit provider request. Require explicit phrasing.
            "OECD": ["from oecd", "using oecd", "via oecd", "according to oecd", "oecd data"],
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
            top_n_match = re.search(r'top\s+(\d+)', query_lower)
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
        import re

        base_currency = params.get("baseCurrency", "USD")
        target_currency = params.get("targetCurrency")

        if not target_currency:
            query_upper = (intent.originalQuery or "").upper()
            to_match = re.search(r'\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b', query_upper)
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

        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=self._normalize_provider_alias(self._detect_explicit_provider(refined_query)),
            routed_provider=api_provider,
            validation_warning=ProviderRouter.validate_routing(
                api_provider,
                refined_query,
                intent,
            ),
        )
        return refined_query, intent, parse_result

    async def _try_resolve_pending_country_follow_up(
        self,
        query: str,
        conversation_id: str,
        auto_pro_mode: bool,
        tracker: Optional['ProcessingTracker'] = None,
    ) -> Optional[QueryResponse]:
        """
        Let geography-only follow-ups override a pending semantic clarification.

        Example:
        - pending clarification from "imports share of gdp in G20"
        - follow-up: "show only US"
        - rewritten query: "imports share of gdp in US"
        """
        pending = conversation_manager.get_pending_semantic_clarification(conversation_id)
        if not pending:
            return None

        extracted_countries = self._extract_countries_from_query(query)
        expanded_region_countries = CountryResolver.expand_regions_in_query(query)
        target_countries = self._normalize_country_targets(extracted_countries or expanded_region_countries)
        if not self._looks_like_country_follow_up(query, target_countries):
            return None

        original_query = str(pending.get("original_query") or "").strip()
        refined_query = self._rewrite_query_with_country_targets(original_query, target_countries)
        if not refined_query:
            return None

        conversation_manager.clear_pending_semantic_clarification(conversation_id)
        conversation_manager.clear_pending_indicator_options(conversation_id)
        return await self.process_query(
            query=refined_query,
            conversation_id=conversation_id,
            auto_pro_mode=auto_pro_mode,
            use_orchestrator=False,
            allow_orchestrator=False,
        )

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
            routed_provider = ProviderRouter.route_provider(intent, query)

        routed_provider = ProviderRouter.correct_coingecko_misrouting(
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
                semantic_provider = ProviderRouter.correct_coingecko_misrouting(
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
        geo_terms: set[str] = set()
        for alias in CountryResolver.COUNTRY_ALIASES.keys():
            for token in re.findall(r"[a-z0-9]+", str(alias or "").lower()):
                if len(token) >= 2:
                    geo_terms.add(token)

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

        text_lower = str(text).lower()
        normalized_text = re.sub(r"[_/]+", " ", text_lower).replace("-", " ")
        search_text = f"{text_lower} {normalized_text}"
        cue_map = {
            "import": {"import", "imports"},
            "export": {"export", "exports"},
            "trade_balance": {
                "trade balance",
                "trade surplus",
                "trade deficit",
                "net trade balance",
                "external balance on goods and services",
                "net exports",
            },
            "current_account": {
                "current account",
                "current account balance",
                "balance of payments current account",
                "bca_ngdpd",
                "bn.cab",
            },
            "trade_openness": {
                "trade openness",
                "trade (% of gdp)",
                "exports plus imports",
                "imports plus exports",
                "exports and imports as % of gdp",
            },
            "debt": {"debt", "liability", "liabilities"},
            "debt_service": {"debt service", "debt service ratio", "dsr"},
            "debt_gdp_ratio": {
                "debt to gdp",
                "debt-to-gdp",
                "debt as % of gdp",
                "debt as percentage of gdp",
                "debt (% of gdp)",
                "debt, total (% of gdp)",
                "% of gdp debt",
                "gdp to debt ratio",
                "gdp/debt ratio",
            },
            "gdp_deflator": {
                "gdp deflator",
                "gross domestic product deflator",
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
            "hicp": {"hicp", "harmonized index", "harmonised index", "harmonized consumer price"},
            "producer_price": {"producer price", "ppi", "wholesale price"},
            "policy_rate": {"policy rate", "repo rate", "fed funds", "federal funds", "benchmark rate", "cash rate"},
            "bond_yield": {
                "bond yield",
                "treasury yield",
                "government bond",
                "sovereign yield",
                "10-year yield",
                "10 year yield",
                "2-year yield",
                "2 year yield",
                "long-term interest rate",
                "long term interest rate",
            },
            "tenor_2y": {"2-year", "2 year", "2yr", "2 yr"},
            "tenor_10y": {"10-year", "10 year", "10yr", "10 yr", "over 10 years", "maturity over 10 years"},
            "tenor_30y": {"30-year", "30 year", "30yr", "30 yr"},
            "money_supply": {
                "money supply",
                "money stock",
                "monetary aggregate",
                "broad money",
                "narrow money",
                "m1",
                "m2",
                "m3",
            },
            "reserves": {"foreign exchange reserves", "fx reserves", "reserve assets", "international reserves", "reserves"},
            "house_prices": {"house price", "house prices", "housing prices", "property prices", "residential property"},
            "employment_rate": {"employment rate"},
            "employment_population": {"employment to population", "employment-population", "employment population ratio"},
            "discontinued": {"discontinued", "deprecated", "legacy"},
            "savings": {"saving", "savings"},
            "credit": {"credit", "lending", "loan"},
            "exchange_rate": {"exchange rate", "forex", "fx", "currency pair", "us dollar exchange"},
            "real_effective_exchange_rate": {
                "reer",
                "real effective exchange rate",
                "real effective fx",
                "trade weighted real exchange rate",
                "ereer",
            },
            "gdp": {"gdp", "gross domestic product"},
        }

        cues: set[str] = set()
        for cue, phrases in cue_map.items():
            if any(phrase in search_text for phrase in phrases):
                cues.add(cue)

        # "Energy importers/exporters" is a country-group qualifier, not a
        # directional trade-flow request. Keep current-account semantics primary.
        energy_group_patterns = (
            "energy importers",
            "energy importing countries",
            "net energy importers",
            "oil importers",
            "energy exporters",
            "energy exporting countries",
            "net energy exporters",
            "oil exporters",
        )
        if any(pattern in search_text for pattern in energy_group_patterns):
            cues.add("energy_group")
            if "current_account" in cues:
                cues.discard("import")
                cues.discard("export")
                cues.discard("trade_balance")

        # Capture ratio phrasing variants not reliably covered by static phrase lists.
        debt_ratio_patterns = [
            r"\bdebt\b.{0,36}\b(?:% of gdp|percent of gdp|percentage of gdp|to gdp|gdp ratio)\b",
            r"\bgdp\b.{0,24}\bdebt\b.{0,12}\bratio\b",
        ]
        if any(re.search(pattern, search_text) for pattern in debt_ratio_patterns):
            cues.add("debt_gdp_ratio")

        return cues

    @staticmethod
    def _single_directional_cue(cues: set[str]) -> str:
        """
        Return the single directional cue requested by a query, if any.

        Empty string means no strict single-direction intent (for example, trade openness).
        """
        has_import = "import" in cues
        has_export = "export" in cues
        if has_import and not has_export:
            return "import"
        if has_export and not has_import:
            return "export"
        return ""

    @classmethod
    def _has_directional_conflict(
        cls,
        query_cues: set[str],
        candidate_cues: set[str],
    ) -> bool:
        """Detect import/export direction conflicts between query and candidate."""
        query_direction = cls._single_directional_cue(query_cues)
        if not query_direction:
            return False
        opposite = "export" if query_direction == "import" else "import"
        return opposite in candidate_cues and query_direction not in candidate_cues

    @classmethod
    def _specific_cues_compatible(
        cls,
        query_cues: set[str],
        candidate_cues: set[str],
    ) -> bool:
        """
        Determine whether two cue sets are semantically compatible.

        Exact cue overlap is preferred, but closely related cue families are accepted
        to avoid discarding valid matches due wording differences.
        """
        if not query_cues:
            return True
        if cls._has_directional_conflict(query_cues, candidate_cues):
            return False
        query_direction = cls._single_directional_cue(query_cues)
        if query_direction and "trade_balance" in candidate_cues and query_direction not in candidate_cues:
            return False
        if query_cues & candidate_cues:
            return True

        compatible_families = [
            {"debt_gdp_ratio", "public_debt"},
            {"trade_openness", "import", "export", "trade_balance"},
            {"gdp_deflator", "inflation", "producer_price"},
            {"bond_yield", "policy_rate", "tenor_2y", "tenor_10y", "tenor_30y"},
            {"exchange_rate", "real_effective_exchange_rate", "reserves"},
            {"current_account", "trade_balance"},
        ]
        for family in compatible_families:
            if (query_cues & family) and (candidate_cues & family):
                return True

        return False

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

    @staticmethod
    def _specialization_mismatch_penalty(query_text: str, candidate_text: str) -> float:
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

        penalty += QueryService._labor_rate_specificity_penalty(query_lower, candidate_lower)

        if not query_mentions("student", "students", "school months", "summer months") and candidate_mentions(
            "student",
            "students",
            "school months",
            "summer months",
        ):
            penalty += 3.4

        if not query_mentions("education", "educational attainment", "school") and candidate_mentions(
            "educational attainment",
            "education",
        ):
            penalty += 2.4

        if not query_mentions("fiscal") and candidate_mentions("fiscal"):
            penalty += 2.1

        if not query_mentions("insurance", "employment insurance", "beneficiaries", "claimants") and candidate_mentions(
            "employment insurance",
            "insurance program",
            "insurance region",
            "beneficiaries",
            "claimants",
        ):
            penalty += 2.2

        if not query_mentions("indigenous") and candidate_mentions("indigenous"):
            penalty += 2.0

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
            penalty += 1.6

        if not query_mentions("urban", "rural", "metropolitan", "population centre", "health region") and candidate_mentions(
            "urban",
            "rural",
            "metropolitan",
            "population centre",
            "health region",
        ):
            penalty += 1.2

        if not query_mentions("industry", "industries", "sector", "sectors") and candidate_mentions(
            "industry",
            "industries",
            "sector",
            "sectors",
        ):
            penalty += 1.7

        if not query_mentions("firm size", "business size", "company size", "enterprise size") and candidate_mentions(
            "firm size",
            "business size",
            "enterprise size",
        ):
            penalty += 1.7

        if not query_mentions("province", "provinces", "territory", "territories", "regional", "region", "regions") and candidate_mentions(
            "province",
            "provinces",
            "territory",
            "territories",
            "regional",
            "regions",
        ):
            penalty += 1.3

        if not query_mentions("youth", "age", "aged", "years old", "15-24", "15 to 24", "25-64", "25 to 64") and (
            re.search(r"\baged?\b", candidate_lower)
            or re.search(r"\b\d{1,2}\s*(?:to|-)\s*\d{1,2}\b", candidate_lower)
            or re.search(r"\b\d{1,2}\s+years?\s+(?:and|or)\s+over\b", candidate_lower)
        ):
            penalty += 2.0

        return penalty

    @staticmethod
    def _labor_rate_specificity_penalty(query_text: str, candidate_text: str) -> float:
        """Penalize near-miss labour-rate candidates for generic rate queries."""
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
            if re.search(metric_pattern, candidate_lower):
                return True
            # Recognize percentage-of-labor-force indicators as equivalent to "rate"
            # e.g., "unemployment, total (% of total labor force)" IS the unemployment rate
            if metric in candidate_lower and re.search(r"\(%\s+of\s+(?:total\s+)?labo[u]?r\s+force\)", candidate_lower):
                return True
            return False

        penalty = 0.0
        if not has_metric_rate(requested_metric):
            penalty += 2.0

        for sibling_metric in ("employment", "unemployment", "participation"):
            if sibling_metric == requested_metric:
                continue
            if has_metric_rate(sibling_metric):
                penalty += 0.45

        return penalty

    def _score_series_relevance(self, query: str, series: Any) -> float:
        """Score semantic relevance of one returned series to the original query."""
        query_text = str(query or "").lower()
        series_text = self._series_text_for_relevance(series).lower()
        if not series_text:
            return -1.0

        score = 0.0
        query_cues = self._extract_indicator_cues(query_text)
        series_cues = self._extract_indicator_cues(series_text)
        query_direction = self._single_directional_cue(query_cues)

        if query_cues:
            cue_overlap = query_cues & series_cues
            score += float(len(cue_overlap)) * 2.5
            if not cue_overlap:
                score -= 2.0
        if self._has_directional_conflict(query_cues, series_cues):
            score -= 3.4

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
        if "import" in query_cues and "import" not in series_cues:
            score -= 2.2
            if "trade_balance" in series_cues:
                score -= 1.0
        if "export" in query_cues and "export" not in series_cues:
            score -= 2.2
            if "trade_balance" in series_cues:
                score -= 1.0
        if "trade_balance" in query_cues and "trade_balance" not in series_cues:
            score -= 2.2
        if "current_account" in query_cues and "current_account" not in series_cues:
            score -= 2.6
        if "trade_openness" in query_cues:
            if "trade_openness" in series_cues:
                score += 2.5
            else:
                score -= 2.6
        if "debt_service" in query_cues and "debt_service" not in series_cues:
            score -= 2.2
        if "debt_gdp_ratio" in query_cues:
            if "debt_gdp_ratio" in series_cues:
                score += 2.5
            else:
                score -= 2.8
            if "debt_service" in series_cues:
                score -= 3.0
        if "gdp_deflator" in query_cues and "gdp_deflator" not in series_cues:
            score -= 2.8
        if "hicp" in query_cues and "hicp" not in series_cues:
            score -= 2.6
        if "public_debt" in query_cues and "public_debt" not in series_cues:
            if ("household_debt" in series_cues) or ("credit" in series_cues) or ("debt_service" in series_cues):
                score -= 2.4
        if "credit" in query_cues and "credit" not in series_cues:
            score -= 1.8
        if "exchange_rate" in query_cues and "exchange_rate" not in series_cues:
            score -= 1.8
        if "real_effective_exchange_rate" in query_cues and "real_effective_exchange_rate" not in series_cues:
            score -= 2.8
        if "money_supply" in query_cues and "money_supply" not in series_cues:
            score -= 2.2
        if "bond_yield" in query_cues and "bond_yield" not in series_cues:
            score -= 2.2
        if "tenor_2y" in query_cues and "tenor_2y" not in series_cues:
            score -= 2.6
        if "tenor_10y" in query_cues and "tenor_10y" not in series_cues:
            score -= 2.6
        if "tenor_30y" in query_cues and "tenor_30y" not in series_cues:
            score -= 2.6
        if "policy_rate" in query_cues and "policy_rate" not in series_cues:
            score -= 2.0
        if "house_prices" in query_cues and "house_prices" not in series_cues:
            score -= 2.2
        if "reserves" in query_cues and "reserves" not in series_cues:
            score -= 2.0
        if "employment_population" in query_cues and "employment_population" not in series_cues:
            score -= 2.2
        if "producer_price" in query_cues and "producer_price" not in series_cues:
            score -= 2.0
        if "discontinued" in series_cues and "discontinued" not in query_cues:
            score -= 3.0

        score -= self._specialization_mismatch_penalty(query_text, series_text)

        # Generic GDP series should not dominate directional/ratio trade queries.
        if "gdp (current us$)" in series_text and ({"import", "export", "trade_balance"} & query_cues):
            score -= 3.0

        # Trade flow totals are usually not ratio indicators.
        if has_ratio_query and "total trade" in series_text:
            score -= 1.5
        if has_ratio_query and ({"import", "export"} & query_cues):
            ratio_signals = (
                "% of gdp",
                "percent of gdp",
                "percentage of gdp",
                "share of gdp",
                "ne.imp.gnfs.zs",
                "ne.exp.gnfs.zs",
                "_ngdp",
            )
            has_ratio_signal = any(signal in series_text for signal in ratio_signals)
            directional_tokens = ()
            if query_direction == "import":
                directional_tokens = ("import", "imports", ".imp.", "_imp", "imp_")
            elif query_direction == "export":
                directional_tokens = ("export", "exports", ".exp.", "_exp", "exp_")
            has_directional_signal = (
                any(token in series_text for token in directional_tokens)
                if directional_tokens else True
            )
            has_directional_ratio_signal = has_ratio_signal and has_directional_signal
            if has_directional_ratio_signal:
                score += 1.2
            else:
                score -= 2.6
                if has_ratio_signal and directional_tokens:
                    score -= 1.4
                if any(token in series_text for token in ("current us$", "constant us$", "million", "billion")):
                    score -= 1.2

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

    def _extract_ranking_value(
        self,
        series: NormalizedData,
        target_year: Optional[int],
    ) -> tuple[Optional[float], Optional[DataPoint]]:
        """Extract comparable ranking value from one series."""
        points = list(series.data or [])
        if not points:
            return None, None

        selected_point: Optional[DataPoint] = None
        if target_year:
            year_prefix = f"{target_year:04d}"
            year_points = [
                point for point in points
                if str(point.date).startswith(year_prefix) and point.value is not None
            ]
            if year_points:
                selected_point = sorted(year_points, key=lambda point: str(point.date))[-1]

        if selected_point is None:
            valid_points = [point for point in points if point.value is not None]
            if valid_points:
                selected_point = sorted(valid_points, key=lambda point: str(point.date))[-1]

        if selected_point is None:
            return None, None
        return float(selected_point.value), selected_point

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
        self,
        indicator_query: str,
        provider: str,
        resolved: Any,
    ) -> float:
        """Score semantic relevance between user indicator query and resolved candidate."""
        if not resolved:
            return -999.0

        provider_norm = normalize_provider_name(provider or getattr(resolved, "provider", ""))
        code_text = str(getattr(resolved, "code", "") or "")
        code_hint = self._code_semantic_hint(provider_norm, code_text)
        resolved_metadata = getattr(resolved, "metadata", None) or {}
        metadata_indicator = str(resolved_metadata.get("indicator", "") or "")
        metadata_description = str(resolved_metadata.get("description", "") or "")
        synthetic_series = {
            "metadata": {
                "source": provider_norm,
                "indicator": " ".join(
                    part for part in [
                        metadata_indicator,
                        str(getattr(resolved, "name", "") or ""),
                        metadata_description,
                        code_hint,
                        code_text,
                    ] if part
                ),
                "seriesId": code_text,
            }
        }
        return self._score_series_relevance(indicator_query, synthetic_series)

    def _code_semantic_hint(self, provider: str, code: str) -> str:
        """
        Derive lightweight semantic hints from provider-native code patterns.

        This improves relevance scoring when resolver candidates are code-heavy
        and have limited human-readable metadata.
        """
        provider_norm = normalize_provider_name(provider)
        code_upper = str(code or "").upper().strip()
        if not code_upper:
            return ""

        hints: List[str] = []

        if provider_norm in {"WORLDBANK", "WORLD BANK"}:
            if ".IMP." in code_upper:
                hints.extend(["imports", "import"])
            if ".EXP." in code_upper:
                hints.extend(["exports", "export"])
            if ".TRD." in code_upper:
                hints.extend(["trade openness", "trade"])
            if ".RSB." in code_upper:
                hints.extend(["trade balance", "external balance"])
            if ".CAB." in code_upper:
                hints.extend(["current account"])
            if ".DOD." in code_upper:
                hints.extend(["government debt", "public debt"])
            if ".REX.REER" in code_upper:
                hints.extend(["real effective exchange rate", "reer"])
            if ".WPI." in code_upper:
                hints.extend(["producer price", "ppi"])
            if ".CPI." in code_upper:
                hints.extend(["consumer price", "inflation", "cpi"])
            if ".DEFL." in code_upper:
                hints.extend(["gdp deflator"])
            if code_upper.endswith(".ZS"):
                hints.extend(["% of gdp", "share of gdp"])
            if ".YG." in code_upper or ".1524." in code_upper:
                hints.extend(["youth", "15 to 24 years"])

        if provider_norm == "FRED":
            if code_upper.startswith("DGS"):
                hints.extend(["government bond yield", "treasury yield"])
                tenor = code_upper.replace("DGS", "")
                if tenor.isdigit():
                    hints.extend([f"{tenor}-year", f"{tenor} year"])
            if code_upper.startswith("GS") and code_upper[2:].isdigit():
                tenor = code_upper[2:]
                hints.extend([f"{tenor}-year", f"{tenor} year", "government bond yield"])
            if "PPI" in code_upper:
                hints.extend(["producer price", "ppi"])
            if "CPI" in code_upper:
                hints.extend(["consumer price", "cpi", "inflation"])
            if "FEDFUNDS" in code_upper or code_upper in {"DFF", "DFEDTARU", "DFEDTARL"}:
                hints.extend(["policy rate", "federal funds"])
            if code_upper.startswith("DEX"):
                hints.extend(["exchange rate", "fx"])
            if "M1" in code_upper:
                hints.extend(["money supply", "m1"])
            if "M2" in code_upper:
                hints.extend(["money supply", "m2"])
            if "M3" in code_upper:
                hints.extend(["money supply", "m3"])

        if provider_norm == "IMF":
            if "BCA" in code_upper:
                hints.extend(["current account"])
            if "NGDP" in code_upper:
                hints.extend(["gdp", "% of gdp"])
            if "EREER" in code_upper or "REER" in code_upper:
                hints.extend(["real effective exchange rate", "reer"])
            if "PCPIPCH" in code_upper:
                hints.extend(["consumer price", "inflation", "cpi"])
            if "PPPI" in code_upper or "PWPI" in code_upper:
                hints.extend(["producer price", "ppi"])
            if code_upper.startswith("LER"):
                hints.extend(["employment rate"])
            if code_upper.startswith("LUR"):
                hints.extend(["unemployment rate"])
            if "_FY" in code_upper:
                hints.extend(["fiscal"])
            if "_FM" in code_upper:
                hints.extend(["female", "women"])
            if "_ML" in code_upper:
                hints.extend(["male", "men"])
            if "_UR" in code_upper:
                hints.extend(["urban"])
            if "_RU" in code_upper:
                hints.extend(["rural"])
            if "_IFT" in code_upper:
                hints.extend(["informal"])
            if "15T24" in code_upper:
                hints.extend(["15 to 24 years", "youth"])
            if "1564" in code_upper:
                hints.extend(["15 to 64 years"])
            if "GE15" in code_upper:
                hints.extend(["15 years and over"])

        if provider_norm == "BIS":
            if code_upper == "WS_DSR":
                hints.extend(["debt service ratio"])
            if code_upper == "WS_SPP":
                hints.extend(["house prices"])
            if code_upper == "WS_CBPOL":
                hints.extend(["policy rate"])

        if provider_norm == "OECD":
            if "UNEM" in code_upper:
                hints.extend(["unemployment rate"])
            if "EMP" in code_upper and "UNEM" not in code_upper:
                hints.extend(["employment rate"])
            if "CPI" in code_upper:
                hints.extend(["consumer price", "inflation", "cpi"])
            if "PPI" in code_upper:
                hints.extend(["producer price", "ppi"])
            if code_upper in {"IRLT", "IRST"} or "IRLT" in code_upper:
                hints.extend(["long-term interest rate", "bond yield"])

        return " ".join(dict.fromkeys(hints))

    def _minimum_resolved_relevance_threshold(self, indicator_query: str) -> float:
        """
        Minimum semantic relevance required to accept a resolved indicator code.

        Keeps high-precision intents strict (imports/exports ratios, REER, HICP, etc.)
        while allowing broader queries to remain flexible.
        """
        normalized_query = str(indicator_query or "").strip().lower()
        cue_set = self._extract_indicator_cues(normalized_query)
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
        has_ratio_query = any(pattern in normalized_query for pattern in ratio_patterns)

        threshold = -0.40
        strict_precision_cues = {
            "import",
            "export",
            "trade_balance",
            "trade_openness",
            "debt_gdp_ratio",
            "public_debt",
            "gdp_deflator",
            "hicp",
            "producer_price",
            "real_effective_exchange_rate",
            "bond_yield",
            "money_supply",
            "policy_rate",
            "house_prices",
        }
        high_precision_cues = {
            "trade_openness",
            "gdp_deflator",
            "hicp",
            "producer_price",
            "real_effective_exchange_rate",
        }

        if cue_set & strict_precision_cues:
            threshold = max(threshold, 0.10)
        if cue_set & high_precision_cues:
            threshold = max(threshold, 0.35)
        if has_ratio_query and (cue_set & {"import", "export"}):
            threshold = max(threshold, 0.45)
        return threshold

    @staticmethod
    def _is_placeholder_indicator_code(code: Optional[str]) -> bool:
        """Return True when indicator code is a non-actionable placeholder."""
        normalized = str(code or "").strip().upper()
        if not normalized:
            return True
        return normalized in {
            "N/A",
            "NA",
            "NONE",
            "NULL",
            "UNKNOWN",
            "DYNAMIC",
            "AUTO",
            "-",
            "--",
            "TBD",
        }

    def _format_indicator_option_name(
        self,
        provider: str,
        code: str,
        name: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Build a user-facing indicator label for clarification options.

        Prefers human-readable names/descriptions over opaque provider codes.
        """
        provider_norm = normalize_provider_name(provider)
        code_text = str(code or "").strip()
        candidate_name = str(name or "").strip()
        metadata_dict = metadata or {}

        description = str(metadata_dict.get("description") or "").strip()
        if (
            (not candidate_name or self._looks_like_provider_indicator_code(provider_norm, candidate_name))
            and description
            and not self._looks_like_provider_indicator_code(provider_norm, description)
        ):
            candidate_name = description

        if provider_norm == "BIS" and code_text:
            # BIS series codes are often opaque (for example WS_DSR); surface dataflow label.
            flow_name, flow_description = self.bis_provider._lookup_dataflow_info(code_text.upper())
            if flow_name and (not candidate_name or self._looks_like_provider_indicator_code(provider_norm, candidate_name)):
                candidate_name = str(flow_name).strip()
            elif flow_description and not candidate_name:
                candidate_name = str(flow_description).strip()

        if not candidate_name:
            candidate_name = code_text or "Unknown indicator"

        return re.sub(r"\s+", " ", candidate_name.replace("_", " ")).strip()

    def _dedupe_indicator_choice_options(self, options: List[str]) -> List[str]:
        """
        De-duplicate clarification options while preserving order.

        Removes:
        - options without parseable provider/code
        - placeholder/non-actionable codes (for example N/A, DYNAMIC)
        - near-duplicate entries from the same provider with equivalent label text
        """
        deduped: List[str] = []
        seen_by_code: set[tuple[str, str]] = set()
        seen_by_label: set[tuple[str, str]] = set()

        for option in options:
            option_text = str(option or "").strip()
            if not option_text:
                continue

            parsed = self._parse_indicator_option(option_text)
            if not parsed:
                continue

            provider, code = parsed
            code_upper = str(code).strip().upper()
            if self._is_placeholder_indicator_code(code_upper):
                continue

            code_key = (provider, code_upper)
            if code_key in seen_by_code:
                continue

            option_body = re.sub(r"^\[[^\]]+\]\s*", "", option_text)
            option_label = re.sub(r"\([^()]*\)\s*$", "", option_body).strip().lower()
            option_label = re.sub(r"[^a-z0-9]+", " ", option_label).strip()
            label_key = (provider, option_label)

            if option_label and label_key in seen_by_label:
                continue

            seen_by_code.add(code_key)
            if option_label:
                seen_by_label.add(label_key)
            deduped.append(option_text)

        return deduped

    @staticmethod
    def _parse_indicator_option(option: str) -> Optional[tuple[str, str]]:
        """Parse one option string like '[IMF] Indicator name (CODE)'."""
        text = str(option or "").strip()
        if not text:
            return None
        match = re.search(r"^\s*\[([^\]]+)\].*?\(([^()]+)\)\s*$", text)
        if not match:
            return None
        provider = normalize_provider_name(match.group(1))
        code = str(match.group(2) or "").strip()
        if not provider or not code:
            return None
        return provider, code

    def _store_pending_indicator_options(
        self,
        conversation_id: str,
        query: str,
        intent: ParsedIntent,
        options: List[str],
        question_lines: Optional[List[str]] = None,
    ) -> None:
        """Persist indicator-choice clarification options for follow-up turns."""
        if not conversation_id or not options:
            return

        clean_options = self._dedupe_indicator_choice_options(options)
        if not clean_options:
            return

        payload = {
            "original_query": str(query or "").strip() or str(intent.originalQuery or "").strip(),
            "intent": intent.model_dump() if hasattr(intent, "model_dump") else None,
            "options": [str(option) for option in clean_options if str(option).strip()],
            "question_lines": [str(line) for line in (question_lines or []) if str(line).strip()],
        }
        try:
            conversation_manager.set_pending_indicator_options(conversation_id, payload)
        except Exception as exc:
            logger.debug("Failed to store pending indicator options: %s", exc)

    def _store_pending_semantic_clarification(
        self,
        conversation_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """Persist semantic clarification state for a follow-up turn."""
        if not conversation_id or not payload:
            return
        try:
            conversation_manager.set_pending_semantic_clarification(conversation_id, payload)
        except Exception as exc:
            logger.debug("Failed to store pending semantic clarification: %s", exc)

    def _build_clarification_options(
        self,
        options: Optional[List[str]],
    ) -> Optional[List[ClarificationOption]]:
        """Convert raw option strings into structured clarification choices."""
        clean_options = self._dedupe_indicator_choice_options(
            [str(option) for option in (options or []) if str(option).strip()]
        )
        if not clean_options:
            return None

        structured_options: List[ClarificationOption] = []
        for idx, option_text in enumerate(clean_options, start=1):
            provider = None
            code = None
            label = option_text

            parsed = self._parse_indicator_option(option_text)
            if parsed:
                provider, code = parsed
                option_body = re.sub(r"^\[[^\]]+\]\s*", "", option_text).strip()
                label = re.sub(r"\s*\([^()]+\)\s*$", "", option_body).strip() or option_body or option_text

            structured_options.append(
                ClarificationOption(
                    id=str(idx),
                    label=label,
                    value=option_text,
                    provider=provider,
                    code=code,
                )
            )

        return structured_options

    @staticmethod
    def _indicator_option_label_key(option: str) -> Optional[str]:
        """Build a provider-agnostic label key for one indicator option."""
        option_text = str(option or "").strip()
        if not option_text:
            return None
        option_body = re.sub(r"^\[[^\]]+\]\s*", "", option_text).strip()
        label = re.sub(r"\s*\([^()]+\)\s*$", "", option_body).strip() or option_body
        label_key = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        return label_key or None

    def _has_materially_distinct_indicator_options(self, options: Optional[List[str]]) -> bool:
        """
        Return True when indicator-choice options differ by more than provider/code.

        Prefetch clarification should only interrupt execution when the user is
        choosing between genuinely different indicator meanings. If every option
        has the same display label and only differs by provider, allow normal
        fetch/fallback behavior to proceed.
        """
        clean_options = self._dedupe_indicator_choice_options(
            [str(option) for option in (options or []) if str(option).strip()]
        )
        label_keys = {
            label_key
            for option in clean_options
            if (label_key := self._indicator_option_label_key(option))
        }
        return len(label_keys) >= 2

    @staticmethod
    def _match_structured_clarification_option(
        user_query: str,
        options: List[ClarificationOption],
    ) -> Optional[ClarificationOption]:
        """Match a user reply against structured clarification options."""
        text = str(user_query or "").strip()
        if not text or not options:
            return None

        numeric_patterns = [
            r"^\s*(\d{1,2})\s*$",
            r"^\s*(?:option|choose|pick|select)\s*(\d{1,2})\s*$",
            r"^\s*#\s*(\d{1,2})\s*$",
        ]
        numeric = None
        for pattern in numeric_patterns:
            numeric = re.fullmatch(pattern, text.lower())
            if numeric:
                break
        if not numeric:
            ordinal_map = {
                "first": 1,
                "second": 2,
                "third": 3,
                "fourth": 4,
                "fifth": 5,
            }
            ordinal_value = ordinal_map.get(text.lower().strip())
            if ordinal_value is not None:
                numeric = re.match(r"(\d+)", str(ordinal_value))
        if numeric:
            idx = int(numeric.group(1)) - 1
            if 0 <= idx < len(options):
                return options[idx]
            return None

        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        for option in options:
            option_label = re.sub(r"\s+", " ", str(option.label or "").lower()).strip()
            option_value = re.sub(r"\s+", " ", str(option.value or "").lower()).strip()
            if normalized in {option_label, option_value}:
                return option
            if len(normalized) >= 4 and (
                normalized in option_label
                or normalized in option_value
            ):
                return option

        return None

    def _match_indicator_choice_option(self, user_query: str, options: List[str]) -> Optional[str]:
        """Match a user follow-up response against stored clarification options."""
        text = str(user_query or "").strip()
        if not text or not options:
            return None

        numeric_patterns = [
            r"^\s*(\d{1,2})\s*$",
            r"^\s*(?:option|choose|pick|select)\s*(\d{1,2})\s*$",
            r"^\s*#\s*(\d{1,2})\s*$",
        ]
        numeric = None
        for pattern in numeric_patterns:
            numeric = re.fullmatch(pattern, text.lower())
            if numeric:
                break
        if not numeric:
            ordinal_map = {
                "first": 1,
                "second": 2,
                "third": 3,
                "fourth": 4,
                "fifth": 5,
            }
            ordinal_value = ordinal_map.get(text.lower().strip())
            if ordinal_value is not None:
                numeric = re.match(r"(\d+)", str(ordinal_value))
        if numeric:
            idx = int(numeric.group(1)) - 1
            if 0 <= idx < len(options):
                return options[idx]
            return None

        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        for option in options:
            option_text = str(option or "").strip()
            if not option_text:
                continue

            option_lower = re.sub(r"\s+", " ", option_text.lower()).strip()
            option_body = re.sub(r"^\[[^\]]+\]\s*", "", option_text).strip()
            option_body_lower = re.sub(r"\s+", " ", option_body.lower()).strip()

            if normalized in {option_lower, option_body_lower}:
                return option_text

            parsed = self._parse_indicator_option(option_text)
            if parsed and normalized == parsed[1].lower():
                return option_text

            if len(normalized) >= 6 and normalized in option_body_lower:
                return option_text

        return None

    async def _try_resolve_pending_indicator_choice(
        self,
        query: str,
        conversation_id: str,
        tracker: Optional['ProcessingTracker'] = None,
    ) -> Optional[QueryResponse]:
        """
        Apply a pending indicator-choice clarification when the user replies with
        an option number or indicator text.
        """
        pending = conversation_manager.get_pending_indicator_options(conversation_id)
        if not pending:
            return None

        raw_options = [str(option) for option in (pending.get("options") or []) if str(option).strip()]
        options = self._dedupe_indicator_choice_options(raw_options)
        if not options:
            conversation_manager.clear_pending_indicator_options(conversation_id)
            return None

        selected_option = self._match_indicator_choice_option(query, options)
        if not selected_option:
            text = str(query or "").strip()
            if re.fullmatch(r"\d{1,2}", text):
                return QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=True,
                    clarificationQuestions=pending.get("question_lines") or [],
                    clarificationOptions=self._build_clarification_options(options),
                    message="Please choose one of the listed option numbers.",
                    processingSteps=tracker.to_list() if tracker else None,
                )

            # User moved on to a new natural-language request; clear stale state.
            if len(text.split()) >= 3:
                conversation_manager.clear_pending_indicator_options(conversation_id)
            return None

        parsed = self._parse_indicator_option(selected_option)
        if not parsed:
            conversation_manager.clear_pending_indicator_options(conversation_id)
            return None

        selected_provider, selected_code = parsed
        original_query = str(pending.get("original_query") or "").strip()
        raw_intent = pending.get("intent")
        intent = self._coerce_parsed_intent(raw_intent, original_query or query)
        if not intent:
            conversation_manager.clear_pending_indicator_options(conversation_id)
            return None

        conversation_id = conversation_manager.add_message_safe(conversation_id, "user", query)

        intent.apiProvider = selected_provider
        intent.indicators = [selected_code]
        intent.clarificationNeeded = False
        intent.clarificationQuestions = []
        if not intent.originalQuery:
            intent.originalQuery = original_query or query

        params = dict(intent.parameters or {})
        params.pop("seriesId", None)
        params.pop("series_id", None)
        params.pop("code", None)
        params["indicator"] = selected_code
        intent.parameters = params

        try:
            if tracker:
                with tracker.track(
                    "clarification_selection",
                    "✅ Applying your indicator selection...",
                    {"provider": selected_provider, "indicator": selected_code},
                ):
                    data = await retry_async(
                        lambda: self._fetch_data(intent),
                        max_attempts=2,
                        initial_delay=0.3,
                    )
            else:
                data = await retry_async(
                    lambda: self._fetch_data(intent),
                    max_attempts=2,
                    initial_delay=0.3,
                )
        except Exception as exc:
            return self._build_failed_indicator_choice_response(
                conversation_id=conversation_id,
                query=original_query or query,
                intent=intent,
                options=options,
                selected_option=selected_option,
                question_lines=pending.get("question_lines") or [],
                tracker=tracker,
                error=str(exc),
            )

        if not data:
            return self._build_failed_indicator_choice_response(
                conversation_id=conversation_id,
                query=original_query or query,
                intent=intent,
                options=options,
                selected_option=selected_option,
                question_lines=pending.get("question_lines") or [],
                tracker=tracker,
            )

        data = self._rerank_data_by_query_relevance(intent.originalQuery or query, data)
        if self._is_ranking_query(intent.originalQuery or query):
            data = self._apply_ranking_projection(intent.originalQuery or query, data)

        recovered_data = await self._maybe_recover_from_uncertain_match(
            intent.originalQuery or query,
            intent,
            data,
        )
        if recovered_data:
            data = recovered_data

        clarification_response = self._build_uncertain_result_clarification(
            conversation_id=conversation_id,
            query=intent.originalQuery or query,
            intent=intent,
            data=data,
            processing_steps=tracker.to_list() if tracker else None,
        )
        if clarification_response:
            return clarification_response

        conversation_id = conversation_manager.add_message_safe(
            conversation_id,
            "assistant",
            f"Retrieved data for selected indicator {selected_code}.",
            intent=intent,
        )
        conversation_manager.clear_pending_indicator_options(conversation_id)

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            data=data,
            clarificationNeeded=False,
            processingSteps=tracker.to_list() if tracker else None,
        )

    async def _try_resolve_pending_semantic_clarification(
        self,
        query: str,
        conversation_id: str,
        auto_pro_mode: bool,
        use_orchestrator: bool,
        allow_orchestrator: bool,
        tracker: Optional['ProcessingTracker'] = None,
    ) -> Optional[QueryResponse]:
        """Apply a pending semantic clarification and re-enter the main pipeline."""
        pending = conversation_manager.get_pending_semantic_clarification(conversation_id)
        if not pending:
            return None

        option_payloads = pending.get("options") or []
        options: List[ClarificationOption] = []
        for raw_option in option_payloads:
            try:
                options.append(ClarificationOption.model_validate(raw_option))
            except Exception:
                continue

        if not options:
            conversation_manager.clear_pending_semantic_clarification(conversation_id)
            return None

        selected_option = self._match_structured_clarification_option(query, options)
        refined_query = ""
        if selected_option is not None:
            refined_query = str(selected_option.value or "").strip()
        else:
            text = str(query or "").strip()
            if re.fullmatch(r"\d{1,2}", text):
                return QueryResponse(
                    conversationId=conversation_id,
                    clarificationNeeded=True,
                    clarificationQuestions=pending.get("question_lines") or [],
                    clarificationOptions=options,
                    message="Please choose one of the listed option numbers, or type the metric you want.",
                    processingSteps=tracker.to_list() if tracker else None,
                )

            if len(text.split()) <= 6:
                refined_query = str(self.semantic_clarifier.build_custom_query(pending, text) or "").strip()

            if not refined_query:
                if len(text.split()) >= 3:
                    conversation_manager.clear_pending_semantic_clarification(conversation_id)
                return None

        original_query = str(pending.get("original_query") or "").strip()
        if refined_query.lower() == original_query.lower():
            return QueryResponse(
                conversationId=conversation_id,
                clarificationNeeded=True,
                clarificationQuestions=pending.get("question_lines") or [],
                clarificationOptions=options,
                message="Please choose a more specific metric, or type a different one.",
                processingSteps=tracker.to_list() if tracker else None,
            )

        conversation_manager.clear_pending_semantic_clarification(conversation_id)
        deterministic_intent = self._build_intent_from_semantic_clarification(
            pending=pending,
            selected_option=selected_option if selected_option is not None else ClarificationOption(
                id="custom",
                label=refined_query,
                value=refined_query,
            ),
            refined_query=refined_query,
        )
        if deterministic_intent is not None:
            parse_result = ParseRouteResult(
                intent=deterministic_intent,
                explicit_provider=self._normalize_provider_alias(self._detect_explicit_provider(refined_query)),
                routed_provider=deterministic_intent.apiProvider,
                validation_warning=ProviderRouter.validate_routing(
                    deterministic_intent.apiProvider,
                    refined_query,
                    deterministic_intent,
                ),
            )
            return await self._execute_resolved_intent(
                query=refined_query,
                conversation_id=conversation_id,
                intent=deterministic_intent,
                parse_result=parse_result,
                tracker=tracker,
            )
        return await self.process_query(
            query=refined_query,
            conversation_id=conversation_id,
            auto_pro_mode=auto_pro_mode,
            use_orchestrator=False,
            allow_orchestrator=False,
        )

    async def _maybe_recover_from_uncertain_match(
        self,
        query: str,
        intent: Optional[ParsedIntent],
        data: List[NormalizedData],
    ) -> Optional[List[NormalizedData]]:
        """
        Try one automatic refetch when current top match looks uncertain.

        This is a framework-level recovery step before asking the user to pick
        from options. It reduces clarification loops when a clearly better
        indicator/provider exists.
        """
        if not intent or not data:
            return None
        if intent.indicators and len(intent.indicators) > 1:
            return None

        params = dict(intent.parameters or {})
        if params.get("_uncertain_recovery_attempted"):
            return None
        if not self._needs_indicator_clarification(query, data, intent):
            return None

        top_series = data[0]
        current_score = self._score_series_relevance(query, top_series)
        top_provider, top_code = self._extract_series_provider_and_code(top_series)

        target_countries = self._collect_target_countries(params)
        target_country = target_countries[0] if target_countries else None
        indicator_query = self._select_indicator_query_for_resolution(intent) or query
        primary_provider = normalize_provider_name(intent.apiProvider or "")
        explicit_provider = normalize_provider_name(self._detect_explicit_provider(intent.originalQuery or "") or "")

        resolver = get_indicator_resolver()
        candidate_keys: set[tuple[str, str]] = set()
        candidate_ordered: List[tuple[str, str]] = []

        def _add_candidate(provider_name: str, code: str) -> None:
            provider_norm = normalize_provider_name(provider_name or "")
            code_norm = str(code or "").strip()
            if not provider_norm or not code_norm:
                return
            key = (provider_norm, code_norm.upper())
            if key in candidate_keys:
                return
            candidate_keys.add(key)
            candidate_ordered.append((provider_norm, code_norm))

        try:
            direct = resolver.resolve(
                indicator_query,
                provider=primary_provider or None,
                country=target_country,
                countries=target_countries or None,
                use_cache=False,
            )
            if direct and getattr(direct, "code", None):
                _add_candidate(getattr(direct, "provider", primary_provider), getattr(direct, "code", ""))
        except Exception:
            pass

        try:
            broad = resolver.resolve(
                indicator_query,
                country=target_country,
                countries=target_countries or None,
                use_cache=False,
            )
            if broad and getattr(broad, "code", None):
                _add_candidate(getattr(broad, "provider", primary_provider), getattr(broad, "code", ""))
        except Exception:
            pass

        for option in self._collect_indicator_choice_options(query, intent, max_options=4):
            parsed = self._parse_indicator_option(option)
            if parsed:
                _add_candidate(parsed[0], parsed[1])

        best_data: Optional[List[NormalizedData]] = None
        best_score = current_score

        for provider_name, code in candidate_ordered:
            if top_provider and top_code and provider_name == top_provider and code.upper() == top_code.upper():
                continue
            if explicit_provider and provider_name != explicit_provider:
                continue

            attempt_intent = intent.model_copy(deep=True)
            attempt_intent.apiProvider = provider_name
            attempt_intent.indicators = [code]
            attempt_params = dict(attempt_intent.parameters or {})
            attempt_params["_uncertain_recovery_attempted"] = True
            attempt_params.pop("seriesId", None)
            attempt_params.pop("series_id", None)
            attempt_params.pop("code", None)
            attempt_params["indicator"] = code
            attempt_intent.parameters = attempt_params

            try:
                candidate_data = await retry_async(
                    lambda i=attempt_intent: self._fetch_data(i),
                    max_attempts=2,
                    initial_delay=0.4,
                )
            except Exception:
                continue
            if not candidate_data:
                continue

            candidate_data = self._rerank_data_by_query_relevance(query, candidate_data)
            if self._is_ranking_query(query):
                candidate_data = self._apply_ranking_projection(query, candidate_data)
            if not candidate_data:
                continue
            if self._has_implausible_top_series(query, candidate_data):
                continue

            candidate_score = self._score_series_relevance(query, candidate_data[0])
            candidate_uncertain = self._needs_indicator_clarification(query, candidate_data, attempt_intent)
            if (
                (not candidate_uncertain and candidate_score >= (best_score + 0.10))
                or candidate_score >= (best_score + 0.35)
            ):
                best_data = candidate_data
                best_score = candidate_score

        return best_data

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

    def _provider_covers_country_list(self, provider: str, countries: Optional[List[str]]) -> bool:
        """Check whether a provider can plausibly cover all requested countries."""
        if not countries:
            return True

        provider_upper = normalize_provider_name(provider)
        normalized_iso2 = [
            self._normalize_country_to_iso2(country) or str(country).upper()
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

    def _provider_supports_requested_scope(
        self,
        provider: str,
        query: str,
        countries: Optional[List[str]],
    ) -> bool:
        """Filter options that are incompatible with the requested comparison scope."""
        if not countries:
            return True

        provider_upper = normalize_provider_name(provider)
        query_lower = str(query or "").lower()
        country_count = len([country for country in countries if country])
        comparison_markers = (
            self._is_comparison_query(query_lower)
            or "member countries" in query_lower
            or "by country" in query_lower
            or "country by country" in query_lower
            or "each country" in query_lower
        )

        if provider_upper == "OECD" and country_count > 8 and comparison_markers:
            return False

        return True

    def _apply_indicator_option_to_intent(self, intent: ParsedIntent, option_text: str) -> bool:
        """Apply one indicator-choice option directly onto an existing intent."""
        parsed = self._parse_indicator_option(option_text)
        if not parsed:
            return False

        provider_name, code = parsed
        intent.apiProvider = provider_name
        intent.indicators = [code]
        intent.clarificationNeeded = False
        intent.clarificationQuestions = []

        params = dict(intent.parameters or {})
        params.pop("seriesId", None)
        params.pop("series_id", None)
        params.pop("code", None)
        params["indicator"] = code
        intent.parameters = params
        return True

    def _provider_can_execute_indicator_option(
        self,
        provider: str,
        code: str,
        option_name: Optional[str] = None,
    ) -> bool:
        """
        Cheap provider-side executability guard for clarification options.

        Some metadata/discovery layers know about provider-adjacent indicator codes
        that the concrete fetch provider in this app does not actually support.
        Keep those out of user-facing choices.
        """
        provider_upper = normalize_provider_name(provider)
        code_text = str(code or "").strip()
        option_label = str(option_name or "").strip()
        if not code_text:
            return False

        if provider_upper == "IMF":
            return bool(
                self.imf_provider._indicator_code(code_text)  # pylint: disable=protected-access
                or (option_label and self.imf_provider._indicator_code(option_label))  # pylint: disable=protected-access
            )

        return True

    def _build_no_reliable_indicator_match_response(
        self,
        conversation_id: str,
        intent: ParsedIntent,
        query: str,
        processing_steps: Optional[List[Any]] = None,
    ) -> QueryResponse:
        """Return a clarification instead of guessing when no reliable option remains."""
        query_text = str(query or intent.originalQuery or "").strip()
        target_countries = self._collect_target_countries(intent.parameters)
        questions = [
            "I could not find a reliable indicator and provider combination for this request without guessing.",
        ]
        if target_countries and len(target_countries) > 8 and self._is_comparison_query(query_text):
            questions.append(
                "Large cross-country comparisons are the hardest case here, and the current metric is not reliably available across the full scope."
            )
            questions.append(
                "Try a smaller country set, ask for one overall group value, or choose a different metric wording."
            )
        else:
            questions.append(
                "Please rephrase with a more specific metric, narrower geography, or different provider scope."
            )

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=questions,
            message="I stopped before fetching because the available matches were not reliable enough.",
            processingSteps=processing_steps,
        )

    def _get_direct_provider_indicator_translation(
        self,
        provider: str,
        indicator_query: str,
    ) -> Optional[str]:
        """
        Return a direct provider-native code when the translator has a strong match.

        This is used to avoid over-eager clarification for high-signal queries where
        the provider already has a stable concept mapping, such as World Bank imports
        as % of GDP.
        """
        provider_name = normalize_provider_name(provider)
        indicator_text = str(indicator_query or "").strip()
        if not provider_name or not indicator_text:
            return None

        try:
            translated_code, translated_concept = self.indicator_translator.translate_indicator(
                indicator_text,
                target_provider=provider_name,
            )
        except Exception:
            return None

        if self._is_placeholder_indicator_code(translated_code):
            return None
        if not self._provider_can_execute_indicator_option(
            provider=provider_name,
            code=str(translated_code),
            option_name=translated_concept,
        ):
            return None
        if not self._is_resolved_indicator_plausible(
            provider=provider_name,
            indicator_query=indicator_text,
            resolved_code=str(translated_code),
            resolved_name=str(translated_concept or indicator_text),
        ):
            return None
        return str(translated_code)

    def _collect_indicator_choice_options(
        self,
        query: str,
        intent: ParsedIntent,
        max_options: int = 3,
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
            high_signal_raw_cues = {
                cue for cue in raw_cues
                if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
            }
            if (
                ("debt_gdp_ratio" in raw_cues and "debt_gdp_ratio" not in indicator_cues)
                or (high_signal_raw_cues and not (high_signal_raw_cues & indicator_cues))
            ):
                indicator_query = raw_query
        if not indicator_query:
            return []

        query_cues = self._extract_indicator_cues(raw_query or indicator_query)
        high_signal_query_cues = {
            cue for cue in query_cues
            if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        }

        target_countries = self._collect_target_countries(intent.parameters)
        if not target_countries and raw_query:
            target_countries = self._extract_countries_from_query(raw_query)
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
            if target_iso2 and not self._provider_covers_country_list(provider_name, target_iso2):
                continue
            if target_iso2 and not all(
                self._provider_supports_country_for_options(provider_name, iso2)
                for iso2 in target_iso2
            ):
                continue
            if not self._provider_supports_requested_scope(
                provider_name,
                raw_query or indicator_query,
                target_iso2,
            ):
                continue

            try:
                resolved = resolver.resolve(
                    indicator_query,
                    provider=provider_name,
                    country=target_country,
                    countries=target_countries or None,
                    use_cache=False,
                )
            except Exception:
                resolved = None

            if not resolved or not resolved.code or resolved.confidence < 0.55:
                continue
            if self._is_placeholder_indicator_code(str(resolved.code)):
                continue

            resolved_name = " ".join(
                part
                for part in [
                    str(getattr(resolved, "name", "") or ""),
                    str((getattr(resolved, "metadata", None) or {}).get("indicator", "") or ""),
                    str((getattr(resolved, "metadata", None) or {}).get("description", "") or ""),
                ]
                if part
            )
            if not self._is_resolved_indicator_plausible(
                provider=provider_name,
                indicator_query=raw_query or indicator_query,
                resolved_code=str(resolved.code),
                resolved_name=resolved_name,
            ):
                continue
            if not self._provider_can_execute_indicator_option(
                provider=provider_name,
                code=str(resolved.code),
                option_name=resolved_name,
            ):
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
            if relevance_score < -0.5:
                continue

            option_cues = self._extract_indicator_cues(
                f"{resolved.name or ''} {resolved.code or ''}"
            )
            if high_signal_query_cues and not self._specific_cues_compatible(
                high_signal_query_cues,
                option_cues,
            ):
                continue
            specific_query_cues = high_signal_query_cues & {
                "trade_openness",
                "gdp_deflator",
                "hicp",
                "debt_gdp_ratio",
                "public_debt",
                "trade_balance",
                "import",
                "export",
                "house_prices",
                "bond_yield",
            }
            if specific_query_cues and not self._specific_cues_compatible(
                specific_query_cues,
                option_cues,
            ):
                continue
            if "trade_openness" in specific_query_cues and "trade_openness" not in option_cues:
                continue
            if "gdp_deflator" in specific_query_cues and "gdp_deflator" not in option_cues:
                continue
            if "hicp" in specific_query_cues and "hicp" not in option_cues:
                continue
            if "debt_gdp_ratio" in specific_query_cues and not (
                {"debt_gdp_ratio", "public_debt"} & option_cues
            ):
                continue

            combined_score = float(resolved.confidence) + (0.12 * relevance_score)
            provider_label = provider_labels.get(provider_name, provider_name)
            option_name = self._format_indicator_option_name(
                provider=provider_name,
                code=str(resolved.code),
                name=getattr(resolved, "name", None),
                metadata=getattr(resolved, "metadata", None),
            )
            option_text = f"[{provider_label}] {option_name} ({resolved.code})"
            scored_options.append((combined_score, option_text, provider_name))

        scored_options.sort(key=lambda item: item[0], reverse=True)
        raw_options = [option for _, option, _ in scored_options]
        deduped_options = self._dedupe_indicator_choice_options(raw_options)
        return deduped_options[:max_options]

    def _infer_query_concept_groups(self, query: str) -> set[str]:
        """
        Infer high-level concept families from a query.

        Used for UX safeguards: when users ask for multiple concept families
        in one request but parser produced a single indicator, we clarify
        instead of returning potentially misleading partial results.
        """
        cues = self._extract_indicator_cues(query)
        cues = {
            cue for cue in cues
            if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        }

        groups: set[str] = set()
        if cues & {"import", "export", "trade_balance", "trade_openness"}:
            groups.add("trade")
        if cues & {"unemployment", "employment_population"}:
            groups.add("labor")
        if cues & {"inflation", "hicp", "gdp_deflator", "producer_price", "house_prices"}:
            groups.add("prices")
        if cues & {"debt_gdp_ratio", "public_debt", "debt_service", "household_debt", "credit"}:
            groups.add("debt_credit")
        if cues & {"policy_rate", "bond_yield"}:
            groups.add("rates")
        if cues & {"exchange_rate", "real_effective_exchange_rate", "reserves", "current_account"}:
            groups.add("external")
        if cues & {"money_supply"}:
            groups.add("money")
        if cues & {"savings"}:
            groups.add("savings")
        return groups

    def _build_multi_concept_query_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        is_multi_indicator: bool,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """
        Ask user to select focus when query spans multiple concept families but
        current intent resolved to a single indicator.
        """
        if not intent:
            return None
        if is_multi_indicator:
            return None
        if intent.indicators and len(intent.indicators) > 1:
            return None

        concept_groups = self._infer_query_concept_groups(query)
        if len(concept_groups) < 2:
            return None

        # Avoid over-triggering for canonical single-metric openness phrasing.
        query_lower = str(query or "").lower()
        if (
            "trade openness" in query_lower
            or "exports plus imports" in query_lower
            or "export plus import" in query_lower
        ):
            return None

        group_labels = {
            "trade": "trade flows",
            "labor": "labor market",
            "prices": "prices/inflation",
            "debt_credit": "debt/credit",
            "rates": "interest rates/yields",
            "external": "external sector/FX",
            "money": "money supply",
            "savings": "savings",
        }
        detected_labels = [group_labels.get(group, group) for group in sorted(concept_groups)]
        options = self._collect_indicator_choice_options(query, intent, max_options=4)

        clarification_questions = [
            "Your query mixes multiple indicator families, which can lead to partial or incorrect results in one fetch.",
            f"I detected: {', '.join(detected_labels)}.",
            "Please choose the first indicator focus to fetch accurately:",
        ]
        if options:
            clarification_questions.extend(
                f"{idx}. {option}" for idx, option in enumerate(options, start=1)
            )
            clarification_questions.append(
                "Reply with the option number (for example, 1) or the exact indicator you want first."
            )
            self._store_pending_indicator_options(
                conversation_id=conversation_id,
                query=query,
                intent=intent,
                options=options,
                question_lines=clarification_questions,
            )
        else:
            clarification_questions.append(
                "Reply with the exact indicator focus first (for example: unemployment rate, CPI inflation, or government debt-to-GDP)."
            )

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            clarificationOptions=self._build_clarification_options(options),
            processingSteps=processing_steps,
        )

    # ======================================================================
    # Informational / Metadata Query Handling
    # ======================================================================

    @staticmethod
    def _is_simple_single_country_query(query: str) -> bool:
        """
        Detect simple single-country macro queries that work better through
        the deterministic pipeline (with UnifiedRouter) than the orchestrator.

        Examples: "unemployment rate in Japan 2020-2024", "GDP of Australia"
        """
        q = str(query or "").lower()
        # Must mention exactly one country
        countries = CountryResolver.detect_all_countries_in_query(query)
        if len(countries) != 1:
            return False
        # Must mention a standard macro indicator
        macro_terms = {
            "gdp", "unemployment", "inflation", "cpi", "population",
            "labor force", "labour force", "employment rate",
            "interest rate", "government debt", "trade balance",
        }
        return any(term in q for term in macro_terms)

    @staticmethod
    def _looks_informational(query: str) -> bool:
        """
        Lightweight pre-LLM check for probable informational queries.

        Detects questions about data availability, provider coverage, or
        indicator discovery — as opposed to actual data fetch requests.
        Uses word co-occurrence rather than exact phrases to handle varied
        word orders.
        """
        q = str(query or "").lower()
        # Must contain at least one "question" word AND one "metadata" word
        question_words = {
            "what", "which", "list", "does", "browse", "search",
            "available", "show me", "have", "offer", "cover",
        }
        metadata_words = {
            "series", "indicators", "indicator", "datasets", "metrics",
            "providers", "data", "sources", "variables", "measures",
        }
        words = set(q.split())
        has_question = bool(words & question_words)
        has_metadata = bool(words & metadata_words)
        if not (has_question and has_metadata):
            return False
        # Exclude queries that are clearly data fetches despite containing
        # "data" — e.g., "show me GDP data for Japan" is a fetch, not info.
        fetch_signals = {"for", "in", "from", "last", "since", "between", "rate", "growth"}
        # If query has "data" as the only metadata word AND has fetch signals,
        # it's probably a data fetch, not informational.
        pure_metadata = words & metadata_words
        if pure_metadata == {"data"} and (words & fetch_signals):
            return False
        return True

    def _handle_informational_intent(
        self,
        query: str,
        intent: ParsedIntent,
        conversation_id: str,
        tracker: Optional["ProcessingTracker"] = None,
    ) -> Optional[QueryResponse]:
        """
        Handle informational queries classified by the LLM (queryType=informational).

        Uses the LLM-extracted indicators and provider from the intent rather
        than regex pattern matching — future-proof and handles novel phrasings.
        """
        # Use LLM-extracted provider (if user specified one)
        provider = str(intent.apiProvider or "").strip()
        if provider.lower() in ("worldbank", "world bank", ""):
            # "WorldBank" is the neutral placeholder — don't filter by it
            # unless user explicitly said "World Bank"
            query_lower = str(query or "").lower()
            if "world bank" not in query_lower and "worldbank" not in query_lower:
                provider = None

        # Use LLM-extracted indicators as search terms
        search_terms = " ".join(str(ind) for ind in (intent.indicators or []))
        # Clean search terms — remove meta-language the LLM might include
        search_terms = re.sub(
            r"\b(?:available|indicators?|series|datasets?|metrics?)\b",
            " ", search_terms, flags=re.IGNORECASE,
        )
        search_terms = " ".join(search_terms.split()).strip()

        if not search_terms and not provider:
            return None

        from .indicator_lookup import IndicatorLookup
        lookup = IndicatorLookup()

        results = lookup.search(search_terms or "", provider=provider, limit=30)

        if not results and provider and search_terms:
            # Try broader search without provider filter
            results = lookup.search(search_terms, provider=None, limit=20)

        if not results:
            provider_label = provider or "any provider"
            topic_label = search_terms or "that topic"
            message = (
                f"I couldn't find indicators matching \"{topic_label}\" "
                f"in {provider_label}. Try rephrasing or searching a different provider."
            )
            return QueryResponse(
                conversationId=conversation_id,
                clarificationNeeded=False,
                message=message,
                processingSteps=tracker.to_list() if tracker else None,
            )

        message = self._format_informational_results(results, provider, search_terms, query)

        return QueryResponse(
            conversationId=conversation_id,
            clarificationNeeded=False,
            message=message,
            processingSteps=tracker.to_list() if tracker else None,
        )

    def _format_informational_results(
        self,
        results: List[Dict[str, Any]],
        provider_filter: Optional[str],
        topic: Optional[str],
        original_query: str,
    ) -> str:
        """Format indicator search results as a readable text response."""
        # Group by provider
        by_provider: Dict[str, List[Dict[str, Any]]] = {}
        for r in results:
            prov = str(r.get("provider") or r.get("source") or "Unknown")
            by_provider.setdefault(prov, []).append(r)

        total = len(results)
        topic_label = topic or "your search"
        if provider_filter:
            header = f"Found **{total}** indicators in **{provider_filter}** matching \"{topic_label}\":"
        else:
            header = f"Found **{total}** indicators matching \"{topic_label}\" across providers:"

        lines = [header, ""]

        for prov, indicators in by_provider.items():
            lines.append(f"**{prov}** ({len(indicators)} results):")
            for ind in indicators[:10]:  # Cap at 10 per provider
                code = ind.get("code") or ind.get("series_id") or "?"
                name = ind.get("name") or ind.get("title") or ind.get("description") or code
                # Truncate long names
                if len(name) > 80:
                    name = name[:77] + "..."
                lines.append(f"  - {name} (`{code}`)")
            if len(indicators) > 10:
                lines.append(f"  - ... and {len(indicators) - 10} more")
            lines.append("")

        lines.append("To fetch data for any indicator, just ask: *\"Show me [indicator name]\"*")
        return "\n".join(lines)

    def _verify_semantic_discriminators(
        self,
        original_query: str,
        code: str,
        series_name: str,
    ) -> bool:
        """
        Unified semantic verification — single source of truth.

        Returns True if the code/name matches the query's semantic discriminators.
        Used by all resolution paths (concept override, catalog, translator,
        direct translation, fetch_data) to prevent wrong indicator types.
        """
        original_lower = str(original_query or "").lower()
        resolver = get_indicator_resolver()
        disc_set = getattr(resolver, '_semantic_discriminators', set())
        query_discs = {d for d in disc_set if d in original_lower}
        if not query_discs:
            return True  # No discriminators in query — accept anything

        name_lower = str(series_name or "").lower()
        code_lower = str(code or "").lower()

        def _matches(disc: str, text: str) -> bool:
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

        missing = {d for d in query_discs if not _matches(d, name_lower) and not _matches(d, code_lower)}
        if missing:
            logger.info(
                "🔬 Semantic verification: '%s' (%s) missing discriminators %s from '%s'",
                code, name_lower[:40], missing, original_lower[:40],
            )
            return False
        return True

    def _auto_resolve_broad_concept_for_region(self, query: str) -> str:
        """
        Auto-rewrite broad concepts to their default metric for region queries.

        When a multi-country region is detected and the query uses a broad
        concept (e.g. "employment"), we auto-select the most common metric
        (e.g. "employment rate") to avoid wrong indicator resolution.

        Returns the rewritten query, or the original if no rewrite is needed.
        """
        regions = CountryResolver.detect_regions_in_query(query)
        if not regions:
            return query
        expanded = CountryResolver.expand_regions_in_query(query)
        if len(expanded) < 2:
            return query

        plan = self.semantic_clarifier.detect(query)
        if not plan:
            return query

        options = plan.get("options") or []
        if not options:
            return query

        default_value = str(options[0].get("value") or "").strip()
        if not default_value or default_value == query:
            return query

        logger.info(
            "🔄 Auto-resolved broad concept for region query: '%s' -> '%s'",
            query, default_value,
        )
        return default_value

    def _build_semantic_ambiguity_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        is_multi_indicator: bool,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """
        Ask for a more precise metric when the query uses a broad concept.

        This runs before fetch, so the system avoids silently picking one
        provider-specific series for an underspecified concept like employment,
        trade, debt, interest rates, productivity, or wages.
        """
        if is_multi_indicator:
            return None
        if intent and intent.indicators and len(intent.indicators) > 1:
            return None

        plan = self.semantic_clarifier.detect(query)
        if not plan:
            return None

        # Skip semantic ambiguity when query uses a specific metric phrase
        # (not a broad concept).  "unemployment rate" is specific;
        # "employment" is broad.  "trade deficit" is specific; "trade" is broad.
        _specific_metric_patterns = [
            r"\bunemployment\s+rate\b", r"\bemployment\s+rate\b",
            r"\btrade\s+balance\b", r"\btrade\s+deficit\b", r"\btrade\s+surplus\b",
            r"\btrade\s+openness\b", r"\bcurrent\s+account\b",
            r"\bbond\s+yield\b", r"\bpolicy\s+rate\b", r"\blending\s+rate\b",
            r"\binflation\s+rate\b", r"\bgdp\s+growth\b", r"\bgdp\s+per\s+capita\b",
            r"\binterest\s+rate\b", r"\bexchange\s+rate\b",
            r"\bdebt\s+to\s+gdp\b", r"\bdebt.to.gdp\b",
            r"\blabor\s+force\s+participation\b", r"\bpoverty\s+rate\b",
            r"\bmortality\s+rate\b", r"\bliteracy\s+rate\b",
            r"\bfertility\s+rate\b", r"\bbirth\s+rate\b",
            r"\blife\s+expectancy\b", r"\bgini\s+coefficient\b",
        ]
        query_lower_check = str(query or "").lower()
        if any(re.search(p, query_lower_check) for p in _specific_metric_patterns):
            logger.info(
                "⏭️ Skipping semantic ambiguity — query has specific metric: '%s'",
                query[:60],
            )
            return None

        # For multi-country region queries, auto-select the default metric
        # instead of asking.  The user's primary intent is clear (compare
        # countries) so we pick the most common interpretation of the broad
        # concept and rewrite the query transparently.
        regions = CountryResolver.detect_regions_in_query(query)
        if regions:
            expanded = CountryResolver.expand_regions_in_query(query)
            if len(expanded) >= 2:
                logger.info(
                    "⏭️ Skipping semantic ambiguity clarification for multi-country "
                    "region query (region=%s, %d countries)",
                    regions, len(expanded),
                )
                return None

        # Skip semantic ambiguity when query has BOTH a clear country AND
        # a specific time reference.  "US unemployment during 2020" has
        # enough context — the user knows what they want.
        query_lower = str(query or "").lower()
        has_country = bool(CountryResolver.detect_all_countries_in_query(query))
        has_time = bool(re.search(r"\b(20\d{2}|19\d{2}|last|since|during|after|before)\b", query_lower))
        if has_country and has_time:
            logger.info(
                "⏭️ Skipping semantic ambiguity clarification — query has clear "
                "country + time context: '%s'",
                query[:60],
            )
            return None

        options = [
            ClarificationOption(
                id=str(idx),
                label=str(option.get("label") or "").strip(),
                value=str(option.get("value") or "").strip(),
            )
            for idx, option in enumerate(plan.get("options") or [], start=1)
            if str(option.get("label") or "").strip() and str(option.get("value") or "").strip()
        ]
        if len(options) < 2:
            return None

        clarification_questions = list(plan.get("question_lines") or [])
        clarification_questions.extend(
            f"{option.id}. {option.label}" for option in options
        )
        clarification_questions.append(
            "Reply with the option number, or type a different metric."
        )

        payload = {
            "kind": plan.get("kind"),
            "concept_label": plan.get("concept_label"),
            "original_query": str(plan.get("original_query") or query or "").strip(),
            "question_lines": clarification_questions,
            "options": [option.model_dump() for option in options],
        }
        self._store_pending_semantic_clarification(conversation_id, payload)

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            clarificationOptions=options,
            processingSteps=processing_steps,
        )

    @staticmethod
    def _humanize_region_name(region: str) -> str:
        """Convert normalized region keys into user-facing labels."""
        text = str(region or "").strip()
        if not text:
            return text

        replacements = {
            "BRICS_PLUS": "BRICS+",
            "EUROZONE": "euro area",
            "LATAM": "Latin America",
            "MENA": "Middle East and North Africa",
            "SSA": "Sub-Saharan Africa",
            "EAST_ASIA": "East Asia",
            "SOUTH_ASIA": "South Asia",
            "SOUTHEAST_ASIA": "Southeast Asia",
        }
        if text in replacements:
            return replacements[text]
        if text.isupper():
            return text.replace("_", " ")
        return text.replace("_", " ").title()

    def _has_explicit_group_scope(self, query: str) -> bool:
        """Return True when the query already makes group scope explicit."""
        query_lower = str(query or "").lower()
        if self._is_comparison_query(query_lower) or self._is_ranking_query(query_lower):
            return True

        explicit_markers = [
            "member countries",
            "member country",
            "members",
            "countries",
            "nations",
            "nation",
            "economies",
            "each country",
            "by country",
            "country by country",
            "cross-country",
            "group average",
            "regional average",
            "average across",
            "mean across",
            "aggregate",
            "aggregated",
            "overall",
            "as a group",
            "as a whole",
            "group as a whole",
            "one value",
            "single value",
            "combined",
            "total for the group",
        ]
        if any(marker in query_lower for marker in explicit_markers):
            return True

        # When a SPECIFIC indicator is named alongside a group, default to
        # comparison.  Users who say "trade openness G7" or "inflation BRICS"
        # want to compare member countries, not get one aggregate value.
        specific_indicators = [
            "gdp", "inflation", "unemployment", "trade openness",
            "trade balance", "debt", "cpi", "interest rate", "policy rate",
            "population", "life expectancy", "fertility", "reserves",
            "savings", "investment", "exports", "imports", "employment",
            "labor force", "current account", "fiscal balance",
        ]
        if any(ind in query_lower for ind in specific_indicators):
            return True

        return False

    def _rewrite_group_scope_query(
        self,
        query: str,
        region: str,
        scope: str,
    ) -> str:
        """Rewrite an ambiguous group query into an explicit scope query."""
        query_text = str(query or "").strip()
        region_label = self._humanize_region_name(region)
        if not query_text or not region_label:
            return query_text

        region_patterns = [re.escape(region_label)]
        region_upper = str(region or "").strip()
        if region_upper and region_upper != region_label:
            region_patterns.append(re.escape(region_upper.replace("_", " ")))
        region_patterns.append(re.escape(region_upper))
        region_regex = "|".join(pattern for pattern in region_patterns if pattern)

        rewritten = query_text
        if scope == "compare_members":
            rewritten = re.sub(
                rf"\b(in|for|within|among)\s+(?:the\s+)?(?:{region_regex})\b",
                f"across {region_label} member countries",
                rewritten,
                count=1,
                flags=re.IGNORECASE,
            )
            if rewritten == query_text:
                rewritten = re.sub(
                    rf"\b(?:{region_regex})\b",
                    f"{region_label} member countries",
                    rewritten,
                    count=1,
                    flags=re.IGNORECASE,
                )
            if not self._is_comparison_query(rewritten):
                rewritten = f"compare {rewritten}"
            return rewritten

        rewritten = re.sub(
            rf"\b(in|for|within|among)\s+(?:the\s+)?(?:{region_regex})\b",
            f"for the {region_label} group as a whole",
            rewritten,
            count=1,
            flags=re.IGNORECASE,
        )
        if rewritten == query_text:
            rewritten = re.sub(
                rf"\b(?:{region_regex})\b",
                f"the {region_label} group as a whole",
                rewritten,
                count=1,
                flags=re.IGNORECASE,
            )
        if rewritten == query_text:
            rewritten = f"{query_text} for the {region_label} group as a whole"
        return rewritten

    def _build_group_scope_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        is_multi_indicator: bool,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """
        Ask whether a group query means member-country comparison or one group value.
        """
        if is_multi_indicator:
            return None
        if intent and intent.indicators and len(intent.indicators) > 1:
            return None

        explicit_provider = self._normalize_provider_alias(
            self._detect_explicit_provider(query)
        )
        if explicit_provider:
            return None

        regions = CountryResolver.detect_regions_in_query(query)
        if len(regions) != 1:
            return None

        region = regions[0]
        expanded = CountryResolver.expand_region(region)
        if not expanded or len(expanded) < 2:
            return None
        if self._has_explicit_group_scope(query):
            return None

        region_label = self._humanize_region_name(region)
        options = [
            ClarificationOption(
                id="1",
                label="compare member countries",
                value=self._rewrite_group_scope_query(query, region, "compare_members"),
            ),
            ClarificationOption(
                id="2",
                label="one overall group value (aggregate/average if available)",
                value=self._rewrite_group_scope_query(query, region, "group_value"),
            ),
        ]

        clarification_questions = [
            f"Your query mentions {region_label}, but the scope is still ambiguous.",
            f"Do you want to compare {region_label} member countries, or ask for one overall value for the group?",
        ]
        clarification_questions.extend(
            f"{option.id}. {option.label}" for option in options
        )
        clarification_questions.append(
            "Reply with the option number, or type a different scope."
        )

        payload = {
            "kind": "group_scope",
            "region": region,
            "original_query": str(query or "").strip(),
            "question_lines": clarification_questions,
            "options": [option.model_dump() for option in options],
        }
        self._store_pending_semantic_clarification(conversation_id, payload)

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            clarificationOptions=options,
            processingSteps=processing_steps,
        )

    async def _filter_viable_indicator_choice_options(
        self,
        query: str,
        intent: ParsedIntent,
        options: List[str],
        max_options: int = 3,
    ) -> List[str]:
        """
        Keep only indicator-choice options that can plausibly fetch usable data.

        Clarification should not present provider/code choices that immediately
        fail for the current geography/scope. This performs a lightweight
        preflight fetch for the small candidate list used in the UI.
        """
        clean_options = self._dedupe_indicator_choice_options(
            [str(option) for option in (options or []) if str(option).strip()]
        )
        if len(clean_options) < 2:
            return clean_options[:max_options]

        viable_options: List[str] = []
        tracker_token = None
        if get_processing_tracker() is not None:
            tracker_token = activate_processing_tracker(None)  # type: ignore[arg-type]

        try:
            for option_text in clean_options:
                parsed = self._parse_indicator_option(option_text)
                if not parsed:
                    continue

                provider_name, code = parsed
                attempt_intent = intent.model_copy(deep=True)
                attempt_intent.apiProvider = provider_name
                attempt_intent.indicators = [code]
                if not attempt_intent.originalQuery:
                    attempt_intent.originalQuery = str(query or "").strip()

                attempt_params = dict(attempt_intent.parameters or {})
                attempt_params["_prefetch_option_validation"] = True
                attempt_params.pop("seriesId", None)
                attempt_params.pop("series_id", None)
                attempt_params.pop("code", None)
                attempt_params["indicator"] = code
                attempt_intent.parameters = attempt_params

                try:
                    candidate_data = await retry_async(
                        lambda i=attempt_intent: self._fetch_data(i),
                        max_attempts=1,
                        initial_delay=0.2,
                    )
                except Exception:
                    continue

                if not candidate_data:
                    continue

                candidate_data = self._rerank_data_by_query_relevance(query, candidate_data)
                if self._is_ranking_query(query):
                    candidate_data = self._apply_ranking_projection(query, candidate_data)
                if not candidate_data:
                    continue
                if self._has_implausible_top_series(query, candidate_data):
                    continue
                if self._needs_indicator_clarification(query, candidate_data, attempt_intent):
                    continue

                viable_options.append(option_text)
                if len(viable_options) >= max_options:
                    break
        finally:
            if tracker_token is not None:
                reset_processing_tracker(tracker_token)

        return viable_options

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
        """Re-prompt after an indicator option fails and drop the dead option."""
        remaining_options = [
            option for option in self._dedupe_indicator_choice_options(options)
            if option != selected_option
        ]
        if remaining_options:
            self._store_pending_indicator_options(
                conversation_id=conversation_id,
                query=query,
                intent=intent,
                options=remaining_options,
                question_lines=question_lines or [],
            )
        else:
            conversation_manager.clear_pending_indicator_options(conversation_id)

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=bool(remaining_options),
            clarificationQuestions=question_lines or [],
            clarificationOptions=self._build_clarification_options(remaining_options),
            error=error,
            message="That option did not return usable data. Please choose a different option.",
            processingSteps=tracker.to_list() if tracker else None,
        )

    async def _build_prefetch_indicator_choice_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        explicit_provider: Optional[str],
        is_multi_indicator: bool,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """
        Clarify indicator choice before fetch when the routed provider guess is weak.

        This is the first interpretation-first step: if the routed provider cannot
        resolve the indicator plausibly but cross-provider candidate retrieval can
        produce viable alternatives, ask the user before making any data request.
        """
        if not intent:
            return None
        if explicit_provider:
            return None
        if is_multi_indicator:
            return None

        # Skip prefetch clarification for providers that don't use traditional
        # indicator resolution (they use coin IDs, currency codes, etc.)
        provider_upper = normalize_provider_name(intent.apiProvider or "")
        if provider_upper in ("COINGECKO", "EXCHANGERATE"):
            return None

        if intent.indicators and len(intent.indicators) > 1:
            return None

        # Skip prefetch clarification when indicator was resolved via catalog.
        # Catalog codes are hand-verified — second-guessing them with semantic
        # plausibility checks causes false rejections (e.g., ICSA for "jobless claims").
        params = dict(intent.parameters or {})
        if params.get("__catalog_resolved"):
            logger.info(
                "Skipping prefetch clarification — catalog-resolved indicator: %s",
                params.get("indicator", "?"),
            )
            return None

        query_text = str(query or "").strip()
        indicator_query = self._select_indicator_query_for_resolution(intent)
        if not indicator_query:
            indicator_query = str(intent.indicators[0] if intent.indicators else "").strip()
        if not indicator_query:
            indicator_query = query_text
        if not indicator_query:
            return None

        provider = normalize_provider_name(intent.apiProvider or "")
        current_indicator = str(params.get("indicator") or "").strip()
        if (
            current_indicator
            and self._looks_like_provider_indicator_code(provider, current_indicator)
            and self._is_resolved_indicator_plausible(
                provider=provider,
                indicator_query=indicator_query,
                resolved_code=current_indicator,
            )
        ):
            return None

        target_countries = self._collect_target_countries(params)
        target_country = target_countries[0] if target_countries else None
        resolver = get_indicator_resolver()
        resolved = None
        try:
            resolved = resolver.resolve(
                indicator_query,
                provider=provider,
                country=target_country,
                countries=target_countries or None,
                use_cache=False,
            )
        except Exception:
            resolved = None

        primary_accepted = False
        primary_relevance = -999.0
        current_label = f"{provider or 'Unknown provider'} routing guess"
        if resolved and getattr(resolved, "code", None):
            threshold = self._indicator_resolution_threshold(
                indicator_query=indicator_query,
                resolved_source=str(getattr(resolved, "source", "") or ""),
            )
            primary_relevance = self._score_resolved_indicator_relevance(
                indicator_query=indicator_query,
                provider=provider,
                resolved=resolved,
            )
            primary_accepted = float(getattr(resolved, "confidence", 0.0) or 0.0) >= threshold
            resolved_name_full = " ".join(
                part
                for part in [
                    str(getattr(resolved, "name", "") or ""),
                    str((getattr(resolved, "metadata", None) or {}).get("indicator", "") or ""),
                    str((getattr(resolved, "metadata", None) or {}).get("description", "") or ""),
                ]
                if part
            )
            logger.info(
                "🔬 Prefetch resolution: query='%s' code=%s conf=%.2f threshold=%.2f "
                "relevance=%.2f accepted=%s",
                indicator_query, resolved.code,
                float(getattr(resolved, "confidence", 0.0) or 0.0),
                threshold, primary_relevance, primary_accepted,
            )
            if primary_accepted:
                plausible = self._is_resolved_indicator_plausible(
                    provider=provider,
                    indicator_query=indicator_query,
                    resolved_code=str(resolved.code),
                    resolved_name=resolved_name_full,
                )
                logger.info("🔬 Plausibility check: %s (code=%s)", plausible, resolved.code)
                if not plausible:
                    primary_accepted = False
            if primary_accepted and primary_relevance < self._minimum_resolved_relevance_threshold(indicator_query):
                primary_accepted = False

            current_name = self._format_indicator_option_name(
                provider=provider,
                code=str(resolved.code),
                name=getattr(resolved, "name", None),
                metadata=getattr(resolved, "metadata", None),
            )
            current_label = f"{current_name} from {provider or getattr(resolved, 'provider', 'unknown provider')}"
            if primary_accepted and primary_relevance >= 0.65:
                return None

        options = self._collect_indicator_choice_options(query_text or indicator_query, intent, max_options=4)
        if not options:
            if not primary_accepted:
                direct_translation = self._get_direct_provider_indicator_translation(
                    provider=provider,
                    indicator_query=indicator_query,
                )
                if direct_translation:
                    logger.info(
                        "🟢 Allowing direct %s translation '%s' for '%s' to bypass prefetch clarification",
                        provider,
                        direct_translation,
                        indicator_query,
                    )
                    return None

                # Before giving up, try fallback providers.  This is critical
                # for OECD-routed queries where indicator resolution fails but
                # WorldBank/Eurostat have the same data.
                fallback_providers = self._get_fallback_providers(provider)
                original_provider = intent.apiProvider
                for fb_provider in fallback_providers:
                    fb_query = indicator_query or query_text or ""
                    # Temporarily switch provider for option collection
                    intent.apiProvider = fb_provider
                    fb_options = self._collect_indicator_choice_options(
                        fb_query, intent, max_options=4,
                    )
                    if fb_options:
                        logger.info(
                            "⬅️ Primary %s failed, found options via fallback %s",
                            original_provider, fb_provider,
                        )
                        options = fb_options
                        break  # Keep intent.apiProvider = fb_provider
                    fb_direct = self._get_direct_provider_indicator_translation(
                        provider=fb_provider,
                        indicator_query=fb_query,
                    )
                    if fb_direct:
                        logger.info(
                            "⬅️ Primary %s failed, direct translation via fallback %s: '%s'",
                            original_provider, fb_provider, fb_direct,
                        )
                        return None  # Let normal fetch proceed with fallback provider

                if not options:
                    # Restore original provider before returning error
                    intent.apiProvider = original_provider
                    return self._build_no_reliable_indicator_match_response(
                        conversation_id=conversation_id,
                        intent=intent,
                        query=query_text or indicator_query,
                        processing_steps=processing_steps,
                    )
            return None
        if len(options) >= 2 and not self._has_materially_distinct_indicator_options(options):
            return None

        target_countries = self._collect_target_countries(intent.parameters)
        should_skip_viability_prefetch = len(target_countries) > 8
        if len(options) >= 2 and not should_skip_viability_prefetch:
            viable_options = await self._filter_viable_indicator_choice_options(
                query=query_text or indicator_query,
                intent=intent,
                options=options,
                max_options=4,
            )
            if len(viable_options) >= 2:
                options = viable_options

        top_option = self._parse_indicator_option(options[0]) if options else None
        top_matches_primary = bool(
            resolved
            and top_option
            and top_option[0] == normalize_provider_name(getattr(resolved, "provider", provider))
            and str(top_option[1]).upper() == str(getattr(resolved, "code", "") or "").upper()
        )

        if len(options) == 1:
            if top_option and (not primary_accepted or not top_matches_primary):
                self._apply_indicator_option_to_intent(intent, options[0])
            return None

        if len(options) < 2:
            if not primary_accepted:
                direct_translation = self._get_direct_provider_indicator_translation(
                    provider=provider,
                    indicator_query=indicator_query,
                )
                if direct_translation:
                    logger.info(
                        "🟢 Allowing direct %s translation '%s' for '%s' to bypass prefetch clarification",
                        provider,
                        direct_translation,
                        indicator_query,
                    )
                    return None
                return self._build_no_reliable_indicator_match_response(
                    conversation_id=conversation_id,
                    intent=intent,
                    query=query_text or indicator_query,
                    processing_steps=processing_steps,
                )
            return None

        if primary_accepted and top_matches_primary:
            return None

        if primary_accepted and not top_matches_primary and primary_relevance >= 0.65:
            return None

        clarification_questions = [
            "I found multiple plausible indicator matches before fetching data.",
            f"Current routed match: {current_label}",
            "Please choose the indicator you intended:",
        ]
        clarification_questions.extend(
            f"{idx}. {option}" for idx, option in enumerate(options, start=1)
        )
        clarification_questions.append(
            "Reply with the option number (for example, 1) or the exact indicator text you want."
        )

        self._store_pending_indicator_options(
            conversation_id=conversation_id,
            query=query_text or indicator_query,
            intent=intent,
            options=options,
            question_lines=clarification_questions,
        )

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            clarificationOptions=self._build_clarification_options(options),
            processingSteps=processing_steps,
        )

    async def _build_post_parse_clarification(
        self,
        conversation_id: str,
        query: str,
        parse_result: ParseRouteResult,
        validation: ValidationResult,
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """Apply the shared clarification guardrails after parse/validation."""
        intent = parse_result.intent

        # Early exit: concept is not available from any provider.
        # The catalog explicitly marks these concepts as unavailable across all
        # providers (e.g., gold spot prices were discontinued by FRED/WorldBank).
        if normalize_provider_name(intent.apiProvider or "") == "NOT_AVAILABLE":
            indicator_text = intent.indicators[0] if intent.indicators else query
            return QueryResponse(
                conversationId=conversation_id,
                intent=intent,
                data=None,
                clarificationNeeded=False,
                message=(
                    f"'{indicator_text}' is not currently available through any of our "
                    f"data providers. This may be because the data series has been "
                    f"discontinued, archived, or is only available through specialized sources."
                ),
            )

        multi_concept_clarification = self._build_multi_concept_query_clarification(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            is_multi_indicator=validation.is_multi_indicator,
            processing_steps=processing_steps,
        )
        if multi_concept_clarification:
            return multi_concept_clarification

        semantic_clarification = self._build_semantic_ambiguity_clarification(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            is_multi_indicator=validation.is_multi_indicator,
            processing_steps=processing_steps,
        )
        if semantic_clarification:
            return semantic_clarification

        group_scope_clarification = self._build_group_scope_clarification(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            is_multi_indicator=validation.is_multi_indicator,
            processing_steps=processing_steps,
        )
        if group_scope_clarification:
            return group_scope_clarification

        return await self._build_prefetch_indicator_choice_clarification(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            explicit_provider=parse_result.explicit_provider,
            is_multi_indicator=validation.is_multi_indicator,
            processing_steps=processing_steps,
        )

    def _build_invalid_intent_response(
        self,
        conversation_id: str,
        intent: ParsedIntent,
        validation_error: Optional[str],
        suggestions: Optional[Dict[str, Any]],
        processing_steps: Optional[List[Any]] = None,
    ) -> QueryResponse:
        """Build the standard validation-failure clarification response."""
        clarification_qs = ParameterValidator.suggest_clarification(intent, validation_error)
        message_parts = ["❌ **Cannot Process Query**", str(validation_error or "The query is missing required details.")]
        if suggestions:
            if suggestions.get("suggestion"):
                message_parts.append(f"\n**💡 Suggestion**: {suggestions['suggestion']}")
            if suggestions.get("common_indicators"):
                message_parts.append(f"\n**Common indicators**: {', '.join(suggestions['common_indicators'])}")
            if suggestions.get("example"):
                message_parts.append(f"\n**Example**: {suggestions['example']}")

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_qs,
            message="\n".join(message_parts),
            processingSteps=processing_steps,
        )

    def _build_low_confidence_intent_response(
        self,
        conversation_id: str,
        intent: ParsedIntent,
        confidence_reason: Optional[str],
        processing_steps: Optional[List[Any]] = None,
    ) -> QueryResponse:
        """Build the standard low-confidence clarification response."""
        reason = str(confidence_reason or "I could not confidently determine the requested data.")
        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=[
                f"I'm not certain about this query: {reason}",
                "Could you rephrase with more specific details?",
                "Or would you like to use Pro Mode for a custom analysis?",
            ],
            message=f"⚠️ **Uncertain Query**\n{reason}\n\nPlease provide more details or use Pro Mode for better results.",
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
        """
        Determine whether returned data looks semantically uncertain for the query.
        """
        if not data:
            return False
        if intent and intent.indicators and len(intent.indicators) > 1:
            # Multi-indicator comparisons intentionally mix concept families;
            # avoid forcing indicator-choice clarification loops.
            return False

        # Skip clarification for high-confidence pre-resolved intents.
        # When the concept override or translator already validated the indicator,
        # second-guessing it with the uncertainty check discards valid data
        # (e.g., "Germany industrial production" → NV.IND.TOTL.KD.ZG → 9 data pts
        # was being replaced with a clarification prompt).
        if intent and intent.confidence and intent.confidence >= 0.90:
            # Check if indicator looks like a pre-resolved provider code
            indicator = (intent.parameters or {}).get("indicator", "")
            provider_upper = normalize_provider_name(intent.apiProvider or "")
            is_pre_resolved = False
            if indicator and "." in indicator and indicator[0].isalpha():
                is_pre_resolved = True
            # FRED codes are uppercase alphanumeric (e.g., ICSA, PAYEMS, MHHNGSP)
            # and are valid when they came from catalog routing
            elif indicator and provider_upper == "FRED" and re.fullmatch(r"[A-Z0-9]{3,}", indicator):
                is_pre_resolved = True
            if is_pre_resolved:
                logger.info(
                    "Skipping uncertainty check — high-confidence pre-resolved indicator: %s (conf=%.2f)",
                    indicator, intent.confidence,
                )
                return False

        scored: List[tuple[float, Any]] = [
            (self._score_series_relevance(query, series), series)
            for series in data
        ]
        scored.sort(key=lambda item: item[0], reverse=True)

        top_score, top_series = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else -999.0
        score_gap = top_score - second_score if len(scored) > 1 else 99.0
        top_meta = getattr(top_series, "metadata", None)
        query_cues = self._extract_indicator_cues(query)
        top_series_cues = self._extract_indicator_cues(self._series_text_for_relevance(top_series))
        high_signal_query_cues = {
            cue for cue in query_cues
            if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        }
        strict_cues = {
            "import",
            "export",
            "trade_balance",
            "trade_openness",
            "debt_gdp_ratio",
            "gdp_deflator",
            "public_debt",
            "money_supply",
            "bond_yield",
            "tenor_2y",
            "tenor_10y",
            "tenor_30y",
            "policy_rate",
            "house_prices",
            "employment_rate",
            "hicp",
            "reserves",
            "employment_population",
            "producer_price",
            "exchange_rate",
        }

        # Strong-consensus guardrail: if all returned series are the same provider/code
        # and relevance is clearly above uncertainty range, skip clarification.
        normalized_keys: set[tuple[str, str]] = set()
        for _, series in scored:
            provider_name, provider_code = self._extract_series_provider_and_code(series)
            if provider_name and provider_code:
                normalized_keys.add((provider_name, provider_code.upper()))
        has_consensus_code = len(normalized_keys) == 1 and len(scored) >= 2

        normalized_indicator_keys: set[tuple[str, str]] = set()
        for _, series in scored:
            meta = getattr(series, "metadata", None) if series is not None else None
            provider_name = normalize_provider_name(str(getattr(meta, "source", "") or "")) if meta else ""
            indicator_text = str(getattr(meta, "indicator", "") or getattr(meta, "seriesId", "") or "").strip().lower() if meta else ""
            indicator_text = re.sub(r"\s+", " ", indicator_text)
            if provider_name and indicator_text:
                normalized_indicator_keys.add((provider_name, indicator_text))
        has_consensus_indicator = len(normalized_indicator_keys) == 1 and len(scored) >= 2

        aligned_high_signal = (not high_signal_query_cues) or bool(high_signal_query_cues & top_series_cues)
        if top_score >= 1.0 and aligned_high_signal and (
            has_consensus_code
            or has_consensus_indicator
            or score_gap >= 0.45
            or second_score < 0.8
        ):
            return False

        top_provider = normalize_provider_name(str(getattr(top_meta, "source", "") or "")) if top_meta else ""
        explicit_provider = normalize_provider_name(self._detect_explicit_provider(query) or "")
        if explicit_provider and top_provider == explicit_provider and top_score >= 0.8 and aligned_high_signal:
            return False

        # High-specificity terms should not trigger clarification when already aligned.
        if "hicp" in query_cues and "hicp" in top_series_cues and top_score >= 0.8:
            return False
        if "gdp_deflator" in query_cues and "gdp_deflator" in top_series_cues and top_score >= 0.8:
            return False
        if "trade_openness" in query_cues and "trade_openness" in top_series_cues and top_score >= 0.8:
            return False

        # Framework-level concept consistency check:
        # if query concept and returned series concept disagree, ask clarification.
        try:
            from .catalog_service import find_concept_by_term, find_concepts_by_code

            query_concept = find_concept_by_term(query)
            if not query_concept and intent and intent.originalQuery:
                query_concept = find_concept_by_term(str(intent.originalQuery))

            if query_concept:
                top_provider, top_code = self._extract_series_provider_and_code(top_series)
                top_series_concepts = (
                    find_concepts_by_code(top_provider, top_code)
                    if top_provider and top_code
                    else []
                )
                if top_series_concepts and query_concept not in top_series_concepts:
                    return True

                if not top_series_concepts and top_meta:
                    inferred_series_concept = find_concept_by_term(
                        str(getattr(top_meta, "indicator", "") or "")
                    )
                    if (
                        inferred_series_concept
                        and inferred_series_concept != query_concept
                        and (bool(high_signal_query_cues) or top_score < 1.5)
                    ):
                        return True
        except Exception:
            pass

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

            if canonical and canonical.confidence >= 0.9 and top_meta:
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
                    canonical_cues = self._extract_indicator_cues(
                        f"{canonical.name or ''} {canonical.code or ''}"
                    )
                    cue_conflict = bool(high_signal_query_cues) and not (
                        high_signal_query_cues & top_series_cues
                    )
                    canonical_supports_query = not high_signal_query_cues or bool(
                        high_signal_query_cues & canonical_cues
                    )

                    if (
                        top_provider != canonical_provider
                        and cue_conflict
                        and canonical_supports_query
                        and top_score < 0.85
                    ):
                        return True
                    if (
                        not _codes_match(top_code, canonical_code)
                        and cue_conflict
                        and canonical_supports_query
                        and top_score < 0.6
                    ):
                        return True
                    # Canonical match aligns with returned series/provider.
                    if top_provider == canonical_provider and _codes_match(top_code, canonical_code):
                        return False
        except Exception:
            pass

        if (
            self._is_temporal_split_query(query)
            and (high_signal_query_cues & top_series_cues)
            and top_score >= 0.55
        ):
            return False

        if "debt_gdp_ratio" in query_cues and "debt_gdp_ratio" not in top_series_cues:
            return True
        if (
            "public_debt" in query_cues
            and "public_debt" not in top_series_cues
            and ("debt_service" in top_series_cues or "credit" in top_series_cues)
        ):
            return True
        if (high_signal_query_cues & strict_cues) and not (high_signal_query_cues & top_series_cues) and top_score < 0.8:
            return True
        if top_score < 0.25:
            return True

        if len(scored) > 1:
            if score_gap < 0.2 and top_score < 0.7:
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
        if intent and intent.indicators and len(intent.indicators) > 1:
            return None
        if not intent or not self._needs_indicator_clarification(query, data, intent):
            return None

        scored = sorted(
            (
                (self._score_series_relevance(query, series), series)
                for series in (data or [])
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        top_score, top_series = scored[0] if scored else (-1.0, None)
        top_meta = getattr(top_series, "metadata", None) if top_series else None
        top_provider = normalize_provider_name(getattr(top_meta, "source", "") or "") if top_meta else ""
        top_code = str(getattr(top_meta, "seriesId", "") or "").strip().upper() if top_meta else ""
        query_cues = self._extract_indicator_cues(query)
        top_series_cues = self._extract_indicator_cues(self._series_text_for_relevance(top_series))
        high_signal_query_cues = {
            cue for cue in query_cues
            if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        }
        if top_meta and top_score >= 0.95 and (
            not high_signal_query_cues
            or bool(high_signal_query_cues & top_series_cues)
        ):
            return None

        options = self._dedupe_indicator_choice_options(
            self._collect_indicator_choice_options(query, intent)
        )

        if top_meta:
            current_provider = normalize_provider_name(getattr(top_meta, "source", "") or "")
            current_code = str(getattr(top_meta, "seriesId", "") or "").strip()
            current_name = self._format_indicator_option_name(
                provider=current_provider,
                code=current_code,
                name=getattr(top_meta, "indicator", "") or "",
                metadata={"description": str(getattr(top_meta, "description", "") or "")},
            )
            current_label = f"{current_name} from {getattr(top_meta, 'source', 'unknown source')}"
        else:
            current_label = "Unknown indicator"

        if options and top_meta and top_score >= 0.75:
            leading_option = options[0].upper()
            provider_token = f"[{top_provider}]"
            code_token = f"({top_code})" if top_code else ""
            if provider_token in leading_option and (not code_token or code_token in leading_option):
                # Best ranked option already matches current top series with strong relevance.
                # Avoid unnecessary clarification loops.
                return None

        # Ensure at least two options are available for genuinely uncertain matches.
        if len(options) < 2 and top_meta:
            current_code = str(getattr(top_meta, "seriesId", "") or "").strip()
            current_provider = normalize_provider_name(getattr(top_meta, "source", "") or "")
            if not self._is_placeholder_indicator_code(current_code):
                current_name = self._format_indicator_option_name(
                    provider=current_provider,
                    code=current_code,
                    name=getattr(top_meta, "indicator", "") or "",
                    metadata={"description": str(getattr(top_meta, "description", "") or "")},
                )
                current_option = f"[{current_provider}] {current_name} ({current_code})"
                options.insert(0, current_option)

        if len(options) < 2:
            try:
                resolver = get_indicator_resolver()
                target_countries = self._collect_target_countries(intent.parameters)
                if not target_countries:
                    target_countries = self._extract_countries_from_query(query)
                target_country = target_countries[0] if target_countries else None

                canonical = resolver.resolve(
                    query,
                    country=target_country,
                    countries=target_countries or None,
                    use_cache=False,
                )
                if canonical and canonical.code:
                    if self._is_placeholder_indicator_code(canonical.code):
                        canonical = None
                if canonical and canonical.code:
                    provider_label = normalize_provider_name(canonical.provider or "")
                    canonical_cues = self._extract_indicator_cues(
                        f"{canonical.name or ''} {canonical.code or ''}"
                    )
                    cue_compatible = self._specific_cues_compatible(high_signal_query_cues, canonical_cues)
                    provider_compatible = self._provider_covers_country_list(
                        provider_label,
                        target_countries or None,
                    )
                    if cue_compatible and provider_compatible:
                        canonical_name = self._format_indicator_option_name(
                            provider=provider_label,
                            code=str(canonical.code),
                            name=str(canonical.name or canonical.code),
                            metadata=getattr(canonical, "metadata", None),
                        )
                        canonical_option = (
                            f"[{provider_label}] "
                            f"{canonical_name} "
                            f"({canonical.code})"
                        )
                        options.append(canonical_option)
            except Exception:
                pass

        options = self._dedupe_indicator_choice_options(options)
        mismatch_hint = self._build_indicator_mismatch_hint(query, top_series)
        directional_conflict = self._has_directional_conflict(high_signal_query_cues, top_series_cues)
        severe_mismatch = bool(mismatch_hint) and (directional_conflict or top_score < 0.25)
        distinct_options: set[tuple[str, str]] = set()
        for option in options:
            parsed = self._parse_indicator_option(option)
            if not parsed:
                continue
            provider_name, indicator_code = parsed
            code_upper = str(indicator_code).strip().upper()
            if self._is_placeholder_indicator_code(code_upper):
                continue
            distinct_options.add((provider_name, code_upper))

        if top_provider and top_code and not self._is_placeholder_indicator_code(top_code):
            distinct_options.add((top_provider, top_code))

        if len(distinct_options) < 2 or len(options) < 2:
            if not severe_mismatch:
                return None
            clarification_questions = [
                "The current indicator match may be wrong for your request.",
                f"Current match: {current_label}",
                mismatch_hint,
                "Please reply with the exact indicator name or code you want (for example, NE.IMP.GNFS.ZS).",
            ]
            return QueryResponse(
                conversationId=conversation_id,
                intent=intent,
                clarificationNeeded=True,
                clarificationQuestions=clarification_questions,
                clarificationOptions=self._build_clarification_options(options),
                processingSteps=processing_steps,
            )

        clarification_questions = [
            "I found multiple plausible indicators and the current match is uncertain.",
            f"Current match: {current_label}",
        ]
        if mismatch_hint:
            clarification_questions.append(mismatch_hint)
        clarification_questions.append("Please choose one option:")
        clarification_questions.extend(
            f"{idx}. {option}" for idx, option in enumerate(options, start=1)
        )
        clarification_questions.append(
            "Reply with the option number (for example, 1) or the exact indicator text you want."
        )

        self._store_pending_indicator_options(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            options=options,
            question_lines=clarification_questions,
        )

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            clarificationOptions=self._build_clarification_options(options),
            processingSteps=processing_steps,
        )

    def _build_indicator_mismatch_hint(self, query: str, top_series: Any) -> Optional[str]:
        """Generate a concise mismatch explanation for uncertain indicator matches."""
        if top_series is None:
            return None

        query_cues = self._extract_indicator_cues(query)
        top_series_cues = self._extract_indicator_cues(self._series_text_for_relevance(top_series))
        high_signal_query_cues = {
            cue for cue in query_cues
            if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        }

        query_direction = self._single_directional_cue(high_signal_query_cues)
        if query_direction and self._has_directional_conflict(high_signal_query_cues, top_series_cues):
            opposite = "exports" if query_direction == "import" else "imports"
            return (
                f"Potential mismatch: your query asks for {query_direction}s, "
                f"but the current match appears closer to {opposite}."
            )

        if high_signal_query_cues and not (high_signal_query_cues & top_series_cues):
            cue_labels = {
                "import": "imports",
                "export": "exports",
                "trade_balance": "trade balance",
                "trade_openness": "trade openness",
                "debt_gdp_ratio": "debt-to-GDP ratio",
                "public_debt": "public debt",
                "debt_service": "debt service",
                "gdp_deflator": "GDP deflator",
                "hicp": "HICP",
                "producer_price": "producer prices",
                "real_effective_exchange_rate": "real effective exchange rate",
                "bond_yield": "bond yields",
                "policy_rate": "policy rate",
                "money_supply": "money supply",
                "house_prices": "house prices",
                "current_account": "current account",
                "reserves": "foreign-exchange reserves",
            }
            readable = [
                cue_labels.get(cue, cue.replace("_", " "))
                for cue in sorted(high_signal_query_cues)
            ]
            if readable:
                return (
                    "Potential mismatch: the current match does not clearly reflect "
                    f"your requested concept ({', '.join(readable[:3])})."
                )

        return None

    def _build_no_data_indicator_clarification(
        self,
        conversation_id: str,
        query: str,
        intent: Optional[ParsedIntent],
        processing_steps: Optional[List[Any]] = None,
    ) -> Optional[QueryResponse]:
        """
        Offer indicator choices when no data is returned and intent may be ambiguous.

        This improves UX for wrong/ambiguous series matches by letting users pick
        from ranked alternatives instead of receiving a hard no-data error.
        """
        if not intent:
            return None
        if intent.indicators and len(intent.indicators) > 1:
            return None

        options = self._dedupe_indicator_choice_options(
            self._collect_indicator_choice_options(query=query, intent=intent, max_options=4)
        )
        if len(options) < 2:
            return None

        clarification_questions = [
            "I couldn't find data with the current indicator selection.",
            "Please choose the indicator you intended:",
        ]
        clarification_questions.extend(
            f"{idx}. {option}" for idx, option in enumerate(options, start=1)
        )
        clarification_questions.append(
            "Reply with the option number (for example, 1) or the exact indicator text you want."
        )

        self._store_pending_indicator_options(
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            options=options,
            question_lines=clarification_questions,
        )

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            clarificationOptions=self._build_clarification_options(options),
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
            # Examples: DSD_...@DF_..., CPI, PPI, IRLT
            return bool(re.fullmatch(r"[A-Z0-9_@\.]{3,}", code_upper))

        if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
            return bool(re.fullmatch(r"[A-Z0-9_]{3,}", code_upper))

        return False

    def _is_resolved_indicator_plausible(
        self,
        provider: str,
        indicator_query: str,
        resolved_code: str,
        resolved_name: str = "",
    ) -> bool:
        """
        Lightweight semantic plausibility check for resolved provider codes.

        Prevents high-confidence but semantically off-target code matches from
        overriding clearer natural-language intent (especially for opaque FRED IDs).
        """
        provider_upper = normalize_provider_name(provider)
        query_cues = self._extract_indicator_cues(indicator_query or "")
        code_upper = str(resolved_code or "").upper()
        query_lower = str(indicator_query or "").lower()
        candidate_text = " ".join(
            part
            for part in [
                resolved_name,
                self._code_semantic_hint(provider_upper, code_upper),
                code_upper,
            ]
            if part
        ).lower()

        if not query_cues:
            return True

        if self._specialization_mismatch_penalty(query_lower, candidate_text) >= 1.8:
            return False

        if "gdp_deflator" in query_cues and not any(
            token in code_upper for token in ("DEFL", "DEFLATOR", "GDPDEFL")
        ):
            return False

        if "hicp" in query_cues and provider_upper in {"WORLDBANK", "IMF", "FRED", "STATSCAN", "STATISTICS CANADA"}:
            return False

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
        has_ratio_query = any(pattern in query_lower for pattern in ratio_patterns)

        if "current_account" in query_cues and not any(
            token in code_upper for token in ("BCA", "CAB", "CURRENT", "CURR")
        ):
            return False

        if "real_effective_exchange_rate" in query_cues and not any(
            token in code_upper for token in ("EREER", "REER")
        ):
            return False
        if (
            "real_effective_exchange_rate" in query_cues
            and provider_upper in {"WORLDBANK", "WORLD BANK"}
            and code_upper == "REER"
        ):
            # World Bank has explicit REER series IDs; bare REER is typically too
            # ambiguous and often coverage-poor in practice.
            return False

        if "trade_openness" in query_cues:
            if provider_upper in {"WORLDBANK", "WORLD BANK"}:
                # Trade openness should map to Trade (% of GDP), not net trade balance.
                if code_upper in {"NE.RSB.GNFS.ZS", "BN.GSR.GNFS.CD"}:
                    return False
                if "TRD.GNFS" not in code_upper:
                    return False
            if provider_upper == "IMF" and "XS_GDP" not in code_upper:
                return False

        if "producer_price" in query_cues:
            if provider_upper in {"WORLDBANK", "WORLD BANK"} and not any(
                token in code_upper for token in ("WPI", "PPI", "FP.WPI")
            ):
                return False
            if provider_upper == "IMF" and not any(
                token in code_upper for token in ("PPI", "PPPI", "PWPI")
            ):
                return False
            if provider_upper == "FRED" and "PPI" not in code_upper:
                return False
            if provider_upper == "OECD" and "PPI" not in code_upper:
                return False

        if "house_prices" in query_cues:
            if provider_upper in {"WORLDBANK", "WORLD BANK", "IMF"}:
                return False
            if provider_upper == "BIS" and code_upper != "WS_SPP":
                return False
            if provider_upper == "FRED" and not any(
                token in code_upper for token in ("HPI", "CSUSHPI", "USSTHPI")
            ):
                return False
            if provider_upper == "EUROSTAT" and "HPI" not in code_upper:
                return False

        if provider_upper == "FRED":
            domain_tokens = {
                "credit": ("CREDIT", "LOAN", "LEND", "TOTBKCR", "BUSLOANS", "REVOL", "NONREV", "TOTALSL"),
                "inflation": ("CPI", "PPI", "PCE", "DEFL", "INFL"),
                "exchange_rate": ("DEX", "EXCH", "XRU", "REER"),
                "real_effective_exchange_rate": ("REER",),
                "trade_balance": ("BOP", "TRADE", "NETEXP"),
                "current_account": ("BCA", "CURR", "CAB"),
                "import": ("IMP", "IMPORT"),
                "export": ("EXP", "EXPORT"),
                "money_supply": ("M1", "M2", "M3", "MZM", "MONEY", "MONETARY"),
                "policy_rate": ("FEDFUNDS", "DFEDTAR", "DFF"),
                "bond_yield": ("DGS", "GS", "TB3MS", "YIELD", "TREASURY"),
                "house_prices": ("HPI", "CSUSHPI", "USSTHPI"),
                "reserves": ("REER", "DEX", "EXCH", "RESERV"),
            }

            for cue, tokens in domain_tokens.items():
                if cue in query_cues and not any(token in code_upper for token in tokens):
                    return False

            if "m1" in query_lower and "M1" not in code_upper:
                return False
            if "m2" in query_lower and "M2" not in code_upper:
                return False
            if "m3" in query_lower and "M3" not in code_upper:
                return False
            if ("10-year" in query_lower or "10 year" in query_lower) and not (
                "10" in code_upper or "DGS10" in code_upper or "GS10" in code_upper
            ):
                return False
            if "tenor_2y" in query_cues and not (
                "2" in code_upper or "DGS2" in code_upper or "GS2" in code_upper
            ):
                return False
            if "tenor_10y" in query_cues and not (
                "10" in code_upper or "DGS10" in code_upper or "GS10" in code_upper
            ):
                return False
            if "tenor_30y" in query_cues and not (
                "30" in code_upper or "DGS30" in code_upper or "GS30" in code_upper
            ):
                return False

        if provider_upper == "OECD":
            if "bond_yield" in query_cues and not any(
                token in code_upper for token in ("IRLT", "YIELD", "BOND")
            ):
                return False
            if "gdp_deflator" in query_cues and "DEFL" not in code_upper:
                return False
            if "hicp" in query_cues and "HICP" not in code_upper:
                return False
            if "trade_openness" in query_cues and not any(
                token in code_upper for token in ("TRADE", "XS_GDP", "BOP")
            ):
                return False
            if "producer_price" in query_cues and "PPI" not in code_upper:
                return False

        if provider_upper == "BIS":
            # Guardrail: generic debt-to-GDP queries should not resolve to BIS debt-service series.
            if "debt_gdp_ratio" in query_cues:
                if code_upper in {"WS_DSR", "WS_DEBT_SEC2_PUB"}:
                    return False
                # Generic debt/public debt ratio queries should not stay on BIS unless
                # they explicitly ask for credit/household/debt-service constructs.
                if not (query_cues & {"credit", "household_debt", "debt_service"}):
                    return False
                # WS_TC is valid for BIS credit/household debt contexts only.
                if code_upper == "WS_TC" and not (query_cues & {"credit", "household_debt"}):
                    return False

            if "debt_service" in query_cues and code_upper != "WS_DSR":
                return False
            if (
                "public_debt" in query_cues
                and code_upper.startswith("WS_")
                and not (query_cues & {"credit", "debt_service", "household_debt"})
            ):
                return False
            if "real_effective_exchange_rate" in query_cues and code_upper == "WS_XRU":
                return False

        if has_ratio_query and "trade_balance" in query_cues:
            if provider_upper in {"WORLDBANK", "WORLD BANK"} and code_upper in {"BN.GSR.GNFS.CD"}:
                return False

        return True

    def _extract_series_provider_and_code(self, series: Any) -> tuple[str, str]:
        """Extract normalized provider and provider-native code from one series."""
        meta = getattr(series, "metadata", None) if series is not None else None
        if not meta:
            return "", ""

        provider = normalize_provider_name(str(getattr(meta, "source", "") or ""))
        series_id = str(getattr(meta, "seriesId", "") or "").strip()
        indicator = str(getattr(meta, "indicator", "") or "").strip()
        if series_id:
            return provider, series_id
        if indicator and self._looks_like_provider_indicator_code(provider, indicator):
            return provider, indicator
        return provider, ""

    def _has_implausible_top_series(self, query: str, data: List[Any]) -> bool:
        """
        Check whether top-ranked result is semantically implausible for the query.

        This is used as a post-agent guardrail before final response emission.
        """
        if not data:
            return False

        provider, code = self._extract_series_provider_and_code(data[0])
        if not provider or not code:
            return False

        indicator_name = ""
        meta = getattr(data[0], "metadata", None)
        if meta:
            indicator_name = str(getattr(meta, "indicator", "") or "")

        return not self._is_resolved_indicator_plausible(
            provider=provider,
            indicator_query=query,
            resolved_code=code,
            resolved_name=indicator_name,
        )

    def _normalize_bis_metadata_labels(self, data: List[Any]) -> None:
        """
        Replace opaque provider indicator codes with human-readable labels when possible.

        Applies both to fresh and cached responses so user-facing metadata stays clear.
        """
        if not data:
            return

        for series in data:
            metadata = getattr(series, "metadata", None) if series is not None else None
            if not metadata:
                continue

            source = normalize_provider_name(str(getattr(metadata, "source", "") or ""))
            indicator_value = str(getattr(metadata, "indicator", "") or "").strip()
            series_id_value = str(getattr(metadata, "seriesId", "") or "").strip().upper()
            description_value = str(getattr(metadata, "description", "") or "").strip()

            if source == "BIS":
                code_value = series_id_value
                if not code_value:
                    indicator_upper = indicator_value.upper()
                    if indicator_upper.startswith("WS_"):
                        code_value = indicator_upper

                if code_value:
                    name, description = self.bis_provider._lookup_dataflow_info(code_value)
                    if name and (not indicator_value or indicator_value.upper() == code_value):
                        metadata.indicator = name
                        indicator_value = str(name).strip()
                    if description and not description_value:
                        metadata.description = description
                        description_value = str(description).strip()

            # Generic fallback for all providers:
            # if indicator is code-like and we have a human-readable description,
            # promote description to user-facing indicator label.
            if description_value:
                indicator_code_like = self._looks_like_provider_indicator_code(source, indicator_value)
                indicator_matches_series = bool(
                    indicator_value
                    and series_id_value
                    and indicator_value.upper() == series_id_value.upper()
                )
                description_is_human = bool(re.search(r"[A-Za-z]", description_value)) and (" " in description_value)
                if description_is_human and (
                    not indicator_value
                    or indicator_code_like
                    or indicator_matches_series
                ):
                    metadata.indicator = description_value

    def _apply_concept_provider_override(
        self,
        provider: str,
        intent: ParsedIntent,
        params: dict,
    ) -> tuple[str, dict]:
        """
        Re-route provider using catalog concept availability from the original query.

        This prevents semantically impossible provider/concept combinations from
        continuing into indicator resolution (e.g., public debt ratio on BIS).
        """
        original_query = str(intent.originalQuery or "").strip()
        explicit_provider_requested = normalize_provider_name(
            self._detect_explicit_provider(original_query) or ""
        )
        if explicit_provider_requested and explicit_provider_requested == provider:
            return provider, params

        blocked_override_providers = {
            normalize_provider_name(str(candidate))
            for candidate in (params.get("__fallback_excluded_providers") or [])
            if candidate
        }
        blocked_override_providers.discard("")
        in_fallback_context = bool(blocked_override_providers)

        distilled_query = self._build_distilled_indicator_query(original_query) if original_query else ""
        concept_candidates: List[str] = []
        if original_query:
            concept_candidates.append(original_query)

        if distilled_query and distilled_query not in concept_candidates:
            concept_candidates.append(distilled_query)

        fallback_query = self._select_indicator_query_for_resolution(intent)
        if fallback_query and fallback_query not in concept_candidates:
            concept_candidates.append(fallback_query)

        if not concept_candidates:
            return provider, params

        try:
            from .catalog_service import find_concept_by_term, get_best_provider, is_provider_available

            concept_name = None
            matched_query = ""
            matched_candidates: List[tuple[str, str]] = []
            for candidate in concept_candidates:
                matched_concept = find_concept_by_term(candidate)
                if matched_concept:
                    matched_candidates.append((matched_concept, candidate))
            if matched_candidates:
                concept_name, matched_query = max(
                    matched_candidates,
                    key=lambda item: (
                        len(str(item[0]).split("_")),
                        len(str(item[1]).strip()),
                    ),
                )
            if not concept_name:
                return provider, params

            countries_ctx = params.get("countries") if isinstance(params.get("countries"), list) else None
            if not countries_ctx:
                country_value = params.get("country") or params.get("region")
                countries_ctx = [country_value] if country_value else None
            coverage_ok = self._provider_covers_country_list(provider, countries_ctx or [])

            # Evaluate current provider suitability (availability + coverage + confidence)
            preferred_provider, preferred_code, preferred_confidence = get_best_provider(
                concept_name,
                countries_ctx,
                preferred_provider=provider,
            )
            preferred_provider_normalized = normalize_provider_name(preferred_provider or "")
            provider_supported = (
                bool(preferred_provider_normalized)
                and preferred_provider_normalized == provider
            )
            if not coverage_ok:
                provider_supported = False
                preferred_confidence = max(0.0, float(preferred_confidence or 0.0) - 0.25)

            best_provider, best_code, best_confidence = get_best_provider(concept_name, countries_ctx)
            best_provider_normalized = normalize_provider_name(best_provider or "")
            requested_country_count = len(countries_ctx or [])
            broad_global_providers = {"WORLDBANK", "IMF"}
            niche_coverage_providers = {"OECD", "EUROSTAT", "FRED", "STATSCAN"}

            # Keep existing provider when:
            # 1) it is available and suitable for requested coverage, and
            # 2) no clearly better provider exists.
            if provider_supported:
                # Guardrail for multi-country comparisons: avoid moving from broad
                # global providers to narrower providers unless the confidence gain
                # is material. This preserves country coverage and avoids spurious
                # reroutes (for example, WB -> OECD on mixed-country queries).
                confidence_gain = float(best_confidence or 0.0) - float(preferred_confidence or 0.0)
                low_confidence_current = float(preferred_confidence or 0.0) < 0.80
                if (
                    requested_country_count >= 2
                    and provider in broad_global_providers
                    and best_provider_normalized in niche_coverage_providers
                    and confidence_gain < (0.18 if low_confidence_current else 0.28)
                ):
                    return provider, params
                current_indicator = params.get("indicator")
                current_indicator_is_code = (
                    bool(current_indicator)
                    and self._looks_like_provider_indicator_code(provider, str(current_indicator))
                )
                canonical_code = preferred_code if provider_supported else best_code
                if canonical_code and (not current_indicator or not current_indicator_is_code):
                    # Unified semantic verification — single source of truth
                    from .indicator_resolver import get_indicator_resolver
                    _resolver = get_indicator_resolver()
                    code_meta = _resolver.lookup.get(provider, canonical_code) if provider else None
                    code_name = (code_meta.get("name", "") if code_meta else "")
                    if not self._verify_semantic_discriminators(original_query, canonical_code, code_name):
                        missing_discs = True  # Flag for downstream logic
                        if missing_discs:
                            # Try provider-agnostic resolution — another provider
                            # might have a code that matches the discriminators
                            alt_provider, alt_code, alt_conf = get_best_provider(
                                concept_name, countries_ctx
                            )
                            alt_normalized = normalize_provider_name(alt_provider or "")
                            if alt_normalized and alt_normalized != provider and alt_code:
                                alt_meta = _resolver.lookup.get(alt_normalized, alt_code) if alt_normalized else None
                                alt_name = (alt_meta.get("name", "") if alt_meta else "").lower()
                                alt_missing = {d for d in query_discs if d not in alt_name and d not in alt_code.lower()}
                                if not alt_missing or len(alt_missing) < len(missing_discs):
                                    logger.info(
                                        "🔄 Switching provider %s → %s (code %s matches discriminators better)",
                                        provider, alt_normalized, alt_code,
                                    )
                                    params = {**params, "indicator": alt_code}
                                    intent.parameters = params
                                    intent.indicators = [alt_code]
                                    intent.apiProvider = alt_normalized
                                    return alt_normalized, params
                            return provider, params

                    params = {**params, "indicator": canonical_code, "__catalog_resolved": True}
                    intent.parameters = params
                    distinct_indicators = {str(value) for value in (intent.indicators or []) if value}
                    if not distinct_indicators or len(distinct_indicators) == 1:
                        intent.indicators = [canonical_code]
                    logger.info(
                        "📋 Concept code override: %s -> %s for provider %s",
                        matched_query,
                        canonical_code,
                        provider,
                    )

                if (
                    not best_provider_normalized
                    or best_provider_normalized == provider
                    or best_confidence < (preferred_confidence + 0.08)
                ):
                    return provider, params
            else:
                # If catalog still says provider is available but suitability could not be confirmed,
                # only override when we have a concrete better provider.
                if coverage_ok and is_provider_available(concept_name, provider) and (
                    not best_provider_normalized or best_provider_normalized == provider
                ):
                    return provider, params

            if (
                in_fallback_context
                and best_provider_normalized
                and best_provider_normalized != provider
            ):
                logger.info(
                    "📋 Concept override skipped in fallback context: keeping %s instead of rerouting to %s",
                    provider,
                    best_provider_normalized,
                )
                return provider, params

            alt_provider = best_provider
            alt_code = best_code
            alt_provider_normalized = best_provider_normalized
            if not alt_provider_normalized or alt_provider_normalized == provider:
                return provider, params
            if alt_provider_normalized in blocked_override_providers:
                logger.info(
                    "📋 Concept override skipped: candidate provider %s is blocked in this fallback context",
                    alt_provider_normalized,
                )
                return provider, params

            logger.info(
                "📋 Concept override: rerouting '%s' from %s to %s (best_conf=%.2f, current_conf=%.2f, coverage_ok=%s)",
                concept_name,
                provider,
                alt_provider_normalized,
                best_confidence,
                preferred_confidence,
                provider_supported,
            )
            provider = alt_provider_normalized
            intent.apiProvider = alt_provider or provider

            if alt_code:
                params = {**params, "indicator": alt_code, "__catalog_resolved": True}
                intent.parameters = params
                if not intent.indicators or len(intent.indicators) <= 1:
                    intent.indicators = [alt_code]
                logger.info(
                    "📋 Concept override indicator: %s -> %s",
                    matched_query,
                    alt_code,
                )
        except Exception as exc:
            logger.debug("Concept provider override skipped: %s", exc)

        return provider, params

    def _indicator_resolution_threshold(self, indicator_query: str, resolved_source: str) -> float:
        """
        Dynamic acceptance threshold for resolver output.

        Long natural-language indicator prompts and directional trade queries tend to
        score lower in lexical systems; use a slightly lower threshold there while
        keeping strict defaults for weakly-signaled queries.
        """
        threshold = 0.68
        normalized_query = str(indicator_query or "").strip().lower()
        cue_set = self._extract_indicator_cues(normalized_query)
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
        has_ratio_query = any(pattern in normalized_query for pattern in ratio_patterns)
        strict_precision_cues = {
            "import",
            "export",
            "trade_balance",
            "trade_openness",
            "debt_gdp_ratio",
            "public_debt",
            "gdp_deflator",
            "hicp",
            "producer_price",
            "real_effective_exchange_rate",
            "bond_yield",
            "money_supply",
            "policy_rate",
            "house_prices",
        }
        high_precision_cues = {
            "trade_openness",
            "gdp_deflator",
            "hicp",
            "producer_price",
            "real_effective_exchange_rate",
        }

        if cue_set:
            threshold = 0.64
        if len(normalized_query.split()) >= 6:
            threshold = min(threshold, 0.62)
        if resolved_source in {"catalog", "translator"}:
            threshold = min(threshold, 0.62)
        if cue_set & strict_precision_cues:
            threshold = max(threshold, 0.72)
        if cue_set & high_precision_cues:
            threshold = max(threshold, 0.76)
        if has_ratio_query and (cue_set & {"import", "export"}):
            threshold = max(threshold, 0.78)
        if resolved_source in {"catalog", "translator"} and (cue_set & high_precision_cues):
            threshold = min(threshold, 0.74)

        return threshold

    def _apply_catalog_availability_override(
        self,
        provider: str,
        intent: ParsedIntent,
        params: dict,
        fallback_excluded_providers: set,
    ) -> tuple[str, dict]:
        """Check catalog availability and re-route to a better provider if needed.

        If the current provider is in the not_available list for the resolved
        indicator, proactively switches to the best alternative provider.
        Respects explicit provider requests from the user.

        Returns (provider, params) — both may be updated.
        """
        indicator_term = (params.get("indicator") or (intent.indicators[0] if intent.indicators else ""))
        logger.info(f"📋 Catalog check: indicator='{indicator_term}', provider='{provider}'")

        original_query = intent.originalQuery or ""
        explicit_provider_requested = normalize_provider_name(self._detect_explicit_provider(original_query) or "")
        if explicit_provider_requested and explicit_provider_requested == provider:
            logger.info(f"📋 Skipping catalog override - user explicitly requested {provider}")
            return provider, params

        if not indicator_term or not provider:
            return provider, params

        try:
            from .catalog_service import find_concept_by_term, get_best_provider, is_provider_available
            concept = find_concept_by_term(indicator_term)
            logger.info(f"📋 Catalog concept: '{concept}' for term '{indicator_term}'")
            if concept and not is_provider_available(concept, provider):
                countries_ctx = params.get("countries") if isinstance(params.get("countries"), list) else None
                if not countries_ctx:
                    country = params.get("country") or params.get("region")
                    countries_ctx = [country] if country else None

                alt_provider, alt_code, _ = get_best_provider(concept, countries_ctx)
                if alt_provider and alt_provider.upper() != provider:
                    alt_provider_normalized = normalize_provider_name(alt_provider)
                    if (
                        fallback_excluded_providers
                        and alt_provider_normalized
                        and alt_provider_normalized != provider
                    ):
                        logger.info(
                            "📋 Catalog reroute skipped in fallback context: keeping %s instead of %s",
                            provider,
                            alt_provider_normalized,
                        )
                    elif alt_provider_normalized in fallback_excluded_providers:
                        logger.info(
                            "📋 Catalog reroute skipped: candidate provider %s is blocked in this fallback context",
                            alt_provider_normalized,
                        )
                    else:
                        logger.info(
                            "📋 Catalog: %s not available for '%s', routing to %s",
                            provider,
                            indicator_term,
                            alt_provider,
                        )
                        intent.apiProvider = alt_provider
                        provider = alt_provider_normalized

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

        return provider, params

    async def _resolve_indicator_for_fetch(
        self,
        provider: str,
        intent: ParsedIntent,
        params: dict,
    ) -> dict:
        """Resolve and validate the indicator code for a fetch operation.

        Uses IndicatorSelector (embed → LLM pick) as primary resolution,
        falling back to the legacy IndicatorResolver if embeddings are unavailable.

        Mutates intent.parameters and potentially intent.indicators (for
        WorldBank multi-indicator collapse). Returns the updated params dict.
        """
        resolver = get_indicator_resolver()

        if provider not in {"STATSCAN", "STATISTICS CANADA", "FRED", "IMF", "WORLDBANK", "EUROSTAT", "OECD", "BIS"}:
            return params

        existing_indicator = str(params.get("indicator") or "").strip()
        has_explicit_code = bool(
            existing_indicator
            and self._looks_like_provider_indicator_code(provider, existing_indicator)
        )

        # Qualifier-aware indicator recovery: when the LLM strips qualifiers
        # ("GDP growth G7" → indicators=["GDP"]), recover the full indicator
        # from the original query before any resolution path.
        # SKIP if indicator is already a provider-specific code (e.g., SP.POP.GROW,
        # NY.GDP.MKTP.KD.ZG) — these are already resolved and should not be augmented.
        if not params.get("__qualifier_checked"):
            original_q = str(intent.originalQuery or "").lower()
            # Use the indicator TEXT (from LLM or intent) not the resolved CODE
            indicator_text = ""
            if intent.indicators:
                indicator_text = str(intent.indicators[0] or "").strip().lower()
            if not indicator_text:
                indicator_text = existing_indicator.lower() if existing_indicator else ""
            # Skip qualifier recovery if indicator is already a provider code
            _skip_qualifier = self._looks_like_provider_indicator_code(
                provider, indicator_text.upper()
            ) if indicator_text else False
            _qual_pairs = [
                ("per capita", " per capita"),
                ("per person", " per capita"),
                ("growth rate", " growth rate"),
                ("growth", " growth"),
            ]
            for qual, suffix in _qual_pairs:
                if _skip_qualifier:
                    break
                if qual in original_q and qual not in indicator_text:
                    augmented = indicator_text + suffix
                    # Verify the augmented query resolves to a different (better) code
                    aug_code = self._get_direct_provider_indicator_translation(
                        provider=provider, indicator_query=augmented.strip(),
                    )
                    if aug_code and aug_code != existing_indicator:
                        existing_indicator = aug_code
                        has_explicit_code = True
                        params = {**params, "indicator": aug_code, "__catalog_resolved": True, "__qualifier_checked": True}
                        intent.parameters = params
                        logger.info(
                            "📝 Qualifier recovered: '%s' + '%s' → %s",
                            indicator_text, suffix.strip(), aug_code,
                        )
                    break
            params = {**params, "__qualifier_checked": True}
            intent.parameters = params

        # Catalog-resolved codes are hand-verified and should not be second-guessed.
        if has_explicit_code and params.get("__catalog_resolved"):
            logger.info(
                "🔒 Keeping catalog-resolved %s indicator: %s",
                provider,
                existing_indicator,
            )
            params = {**params, "indicator": existing_indicator}
            intent.parameters = params
            return params

        # Path 1: Validate explicit code against query context
        if has_explicit_code:
            plausibility_query = self._select_indicator_query_for_resolution(intent)
            if not plausibility_query:
                plausibility_query = str(intent.originalQuery or "").strip()
            if not plausibility_query:
                plausibility_query = str(intent.indicators[0] if intent.indicators else existing_indicator)

            if plausibility_query and not self._is_resolved_indicator_plausible(
                provider=provider,
                indicator_query=plausibility_query,
                resolved_code=existing_indicator,
            ):
                logger.info(
                    "🔎 Explicit %s indicator '%s' conflicts with query context '%s'; attempting dynamic resolution",
                    provider,
                    existing_indicator,
                    plausibility_query,
                )
                has_explicit_code = False

        if has_explicit_code:
            logger.info(
                "🔒 Keeping explicit %s indicator code: %s",
                provider,
                existing_indicator,
            )
            params = {**params, "indicator": existing_indicator}
            intent.parameters = params
            return params

        # Path 2-4: Dynamic resolution (direct translation → resolver → raw query)
        indicator_query = self._select_indicator_query_for_resolution(intent)
        if not indicator_query and intent.indicators:
            indicator_query = str(intent.indicators[0] or "").strip()
        if not indicator_query:
            indicator_query = existing_indicator

        if not indicator_query:
            return params

        country_context = params.get("country")
        countries_context = params.get("countries") if isinstance(params.get("countries"), list) else None
        selected_query_override = (
            bool(intent.indicators)
            and indicator_query != str(intent.indicators[0] or "").strip()
        )

        # Qualifier preservation: the LLM sometimes drops key modifiers
        # like "growth", "per capita", "rate" when parsing multi-country
        # queries.  Check the original query for these qualifiers and
        # augment the indicator_query if they're missing.
        # This is applied unconditionally (even for catalog-resolved codes)
        # because the multi-country loop may reset indicators back to
        # the raw LLM output between calls.
        original_q = str(intent.originalQuery or "").lower()
        iq_lower = indicator_query.lower()
        _qualifier_map = [
            ("per capita", " per capita"),
            ("per person", " per capita"),
            ("growth rate", " growth rate"),
            ("growth", " growth"),
        ]
        for qualifier, suffix in _qualifier_map:
            if qualifier in original_q and qualifier not in iq_lower:
                indicator_query = indicator_query + suffix
                logger.info(
                    "📝 Qualifier preserved: added '%s' to indicator query -> '%s'",
                    suffix.strip(), indicator_query,
                )
                break

        # Path 1.5: IndicatorSelector (embed → LLM pick) — primary resolution
        # for all 330K indicators. Uses OpenAI embeddings for semantic retrieval
        # and LLM for variant selection.
        try:
            from .indicator_selector import IndicatorSelector
            selector = IndicatorSelector()
            selection = await selector.select(indicator_query, provider)
            if selection.code:
                logger.info(
                    "🎯 IndicatorSelector resolved: '%s' → %s [%s]",
                    indicator_query, selection.code, selection.source,
                )
                params = {**params, "indicator": selection.code}
                intent.parameters = params
                return params
            if selection.needs_user_choice:
                # Store options for clarification — will be handled by caller
                logger.info(
                    "🔵 IndicatorSelector needs user choice: %d options",
                    len(selection.options),
                )
                params = {**params, "__indicator_options": selection.options}
                intent.parameters = params
                # Don't return — fall through to legacy resolver as backup
        except Exception as e:
            logger.debug("IndicatorSelector unavailable, using legacy resolver: %s", e)

        # Path 2: Legacy IndicatorResolver (catalog + database FTS + vector search)
        # Fallback when IndicatorSelector is unavailable or returns no result.
        resolved = resolver.resolve(
            indicator_query,
            provider=provider,
            country=country_context,
            countries=countries_context,
        )

        # Evaluate resolver result
        accepted_resolved = False
        if resolved:
            threshold = self._indicator_resolution_threshold(
                indicator_query=indicator_query,
                resolved_source=resolved.source,
            )
            relevance_threshold = self._minimum_resolved_relevance_threshold(
                indicator_query,
            )
            resolved_relevance = self._score_resolved_indicator_relevance(
                indicator_query=indicator_query,
                provider=provider,
                resolved=resolved,
            )
            accepted_resolved = resolved.confidence >= threshold
            if accepted_resolved and not self._is_resolved_indicator_plausible(
                provider=provider,
                indicator_query=indicator_query,
                resolved_code=resolved.code,
                resolved_name=" ".join(
                    part
                    for part in [
                        str(getattr(resolved, "name", "") or ""),
                        str((getattr(resolved, "metadata", None) or {}).get("indicator", "") or ""),
                        str((getattr(resolved, "metadata", None) or {}).get("description", "") or ""),
                    ]
                    if part
                ),
            ):
                accepted_resolved = False
            if accepted_resolved and resolved_relevance < relevance_threshold:
                accepted_resolved = False
            logger.info(
                (
                    "🔍 IndicatorResolver candidate: '%s' → '%s' "
                    "(conf=%.2f, src=%s, threshold=%.2f, relevance=%.2f, min_relevance=%.2f, accepted=%s)"
                ),
                indicator_query,
                resolved.code,
                resolved.confidence,
                resolved.source,
                threshold,
                resolved_relevance,
                relevance_threshold,
                accepted_resolved,
            )

        # Path 3: Direct translation fallback (fuzzy IndicatorTranslator)
        # Only runs if the resolver didn't find a confident match.
        direct_translation_code: Optional[str] = None
        if not accepted_resolved:
            direct_translation = self._get_direct_provider_indicator_translation(
                provider=provider,
                indicator_query=indicator_query,
            )
            if direct_translation and self._looks_like_provider_indicator_code(provider, direct_translation):
                _resolver_inst = get_indicator_resolver()
                _lookup = getattr(_resolver_inst, 'lookup', None)
                _meta = _lookup.get(provider, direct_translation) if _lookup else None
                _name = (_meta.get("name", "") if _meta else "")
                if not self._verify_semantic_discriminators(
                    intent.originalQuery or indicator_query,
                    direct_translation,
                    _name,
                ):
                    direct_translation = None

            if direct_translation and self._looks_like_provider_indicator_code(provider, direct_translation):
                logger.info(
                    "📌 Using direct %s indicator translation fallback: '%s' -> '%s'",
                    provider,
                    indicator_query,
                    direct_translation,
                )
                direct_translation_code = str(direct_translation)

        # Path 4: Apply best result or fall back to raw query
        # Resolver (catalog/FTS5) wins when accepted; translator is fallback only.
        if accepted_resolved and resolved:
            params = {**params, "indicator": resolved.code}
            if provider in {"WORLDBANK", "WORLD BANK"} and selected_query_override and len(intent.indicators) > 1:
                logger.info(
                    "🔎 Collapsing World Bank multi-indicator intent to resolved indicator '%s' after semantic override",
                    resolved.code,
                )
                intent.indicators = [resolved.code]
        else:
            params = {**params, "indicator": indicator_query}

        intent.parameters = params
        return params

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

        distilled_original = self._build_distilled_indicator_query(original_query)

        def _fallback_to_original_or_distilled() -> str:
            return distilled_original or original_query

        indicator_lower = indicator_query.lower()
        if any(term in indicator_lower for term in ("discontinued", "deprecated", "legacy")):
            logger.info("🔎 Parsed indicator appears deprecated/discontinued. Using original query.")
            return _fallback_to_original_or_distilled()

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
        has_ratio_original = any(pattern in original_lower for pattern in ratio_patterns)
        has_ratio_indicator = any(pattern in indicator_lower for pattern in ratio_patterns)
        if has_ratio_original and not has_ratio_indicator:
            logger.info(
                "🔎 Indicator dropped GDP-ratio context. Using original query for resolution."
            )
            return _fallback_to_original_or_distilled()

        original_cues = self._extract_indicator_cues(original_query)
        indicator_cues = self._extract_indicator_cues(indicator_query)
        high_signal_exclusions = {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        high_signal_original_cues = {
            cue for cue in original_cues if cue not in high_signal_exclusions
        }
        high_signal_indicator_cues = {
            cue for cue in indicator_cues if cue not in high_signal_exclusions
        }

        if high_signal_original_cues and not (high_signal_original_cues & high_signal_indicator_cues):
            logger.info(
                "🔎 Indicator cue mismatch (original=%s, parsed=%s). Using original query for resolution.",
                sorted(high_signal_original_cues),
                sorted(high_signal_indicator_cues),
            )
            return _fallback_to_original_or_distilled()

        directional_cues = {"import", "export", "trade_balance"}
        original_directional = high_signal_original_cues & directional_cues
        indicator_directional = high_signal_indicator_cues & directional_cues
        if original_directional and not (original_directional & indicator_directional):
            logger.info(
                "🔎 Directional cue mismatch (original=%s, parsed=%s). Using original query for resolution.",
                sorted(original_directional),
                sorted(indicator_directional),
            )
            return _fallback_to_original_or_distilled()

        original_terms = self._tokenize_indicator_terms(original_query)
        indicator_terms = self._tokenize_indicator_terms(indicator_query)
        if original_terms and indicator_terms:
            overlap = len(original_terms & indicator_terms) / max(len(original_terms), 1)
            if overlap < 0.15:
                logger.info(
                    "🔎 Low indicator-term overlap (%.2f). Using original query for resolution.",
                    overlap,
                )
                return _fallback_to_original_or_distilled()

        # Ranking/comparison phrasing can contain execution words ("top", "rank",
        # "highest") that are poor resolver inputs. Prefer a distilled metric phrase.
        if (self._is_ranking_query(original_query) or self._is_comparison_query(original_query)) and distilled_original:
            return distilled_original

        # If parser returned a long natural-language sentence as the indicator,
        # prefer a distilled metric phrase for stable cross-provider resolution.
        if len(indicator_query.split()) >= 8 and distilled_original:
            return distilled_original

        return indicator_query

    def _is_ranking_query(self, query: str) -> bool:
        """Detect ranking/sorting intent from query phrasing."""
        query_lower = str(query or "").lower()
        return re.search(
            r"\b(rank|ranking|ranked|top(?:\s+\d+)?|highest|lowest|largest|smallest|best|worst)\b",
            query_lower,
        ) is not None

    def _is_comparison_query(self, query: str) -> bool:
        """Detect comparison intent from query phrasing."""
        query_lower = str(query or "").lower()
        return re.search(
            r"\b(compare|comparison|versus|vs|between|across|contrast)\b",
            query_lower,
        ) is not None

    def _is_temporal_split_query(self, query: str) -> bool:
        """Detect before/after time-split phrasing (for example, 'before and after 2018')."""
        query_lower = str(query or "").lower()
        if "before" not in query_lower or "after" not in query_lower:
            return False
        return bool(re.search(r"\b(19\d{2}|20\d{2})\b", query_lower))

    def _extract_top_n_from_query(self, query: str, default: int = 10) -> int:
        """Extract ranking limit from query text (for example, 'top 10')."""
        query_lower = str(query or "").lower()
        match = re.search(r"\btop\s+(\d{1,3})\b", query_lower)
        if match:
            try:
                value = int(match.group(1))
                return max(1, min(100, value))
            except ValueError:
                return default
        if any(term in query_lower for term in ("highest", "lowest", "largest", "smallest", "best", "worst")):
            return 1
        return default

    def _extract_target_year_from_query(self, query: str) -> Optional[int]:
        """Extract explicit target year from query, if present."""
        query_text = str(query or "")
        years = [int(match) for match in re.findall(r"\b(19\d{2}|20\d{2})\b", query_text)]
        if not years:
            return None
        # For ranking-like phrasing, the latest stated year is usually intended target.
        return max(years)

    def _build_distilled_indicator_query(self, query: str) -> str:
        """
        Distill a noisy natural-language query into a stable metric phrase.

        This is used for cross-provider indicator resolution when the original
        phrasing contains ranking/comparison scaffolding.
        """
        query_text = str(query or "").strip()
        if not query_text:
            return ""

        query_lower = query_text.lower()
        cues = self._extract_indicator_cues(query_lower)
        if not cues:
            return ""

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
        has_ratio = any(pattern in query_lower for pattern in ratio_patterns)

        if (
            "trade_openness" in cues
            or "trade openness" in query_lower
            or "exports plus imports" in query_lower
            or "export plus import" in query_lower
        ):
            return "trade openness ratio (exports plus imports to GDP)"
        if "gdp_deflator" in cues:
            return "GDP deflator inflation"
        if "employment_population" in cues:
            return "employment to population ratio"
        # Check unemployment BEFORE employment_rate — "unemployment rate"
        # produces both cues, and returning "employment rate" would be wrong.
        if "unemployment" in cues:
            return "unemployment rate"
        if "employment_rate" in cues:
            return "employment rate"
        if "producer_price" in cues:
            return "producer price inflation"
        if "house_prices" in cues:
            return "house price index"
        if "debt_service" in cues:
            return "debt service ratio"
        if "debt_gdp_ratio" in cues or "public_debt" in cues:
            return "government debt (% of GDP)"
        if "bond_yield" in cues:
            if "long-term interest rate" in query_lower or "long term interest rate" in query_lower:
                return "long-term interest rate"
            if "tenor_30y" in cues:
                return "30-year government bond yield"
            if "tenor_10y" in cues:
                return "10-year government bond yield"
            if "tenor_2y" in cues:
                return "2-year government bond yield"
            return "government bond yield"
        if "policy_rate" in cues:
            return "policy rate"
        if "money_supply" in cues:
            if "m1" in query_lower:
                return "M1 money supply"
            if "m2" in query_lower:
                return "M2 money supply"
            if "m3" in query_lower:
                return "M3 money supply"
            return "money supply"
        if "reserves" in cues:
            return "foreign exchange reserves"
        if "current_account" in cues:
            return "current account balance (% of GDP)"
        if "real_effective_exchange_rate" in cues:
            return "real effective exchange rate"
        if "exchange_rate" in cues:
            return "exchange rate"
        if "trade_balance" in cues:
            if has_ratio:
                return "trade balance (% of GDP)"
            return "trade balance"
        if "import" in cues:
            if has_ratio:
                return "imports as % of GDP"
            return "imports"
        if "export" in cues:
            if has_ratio:
                return "exports as % of GDP"
            return "exports"
        # NOTE: "unemployment" cue is handled earlier (before employment_rate)
        if "hicp" in cues:
            return "HICP inflation"
        if "inflation" in cues:
            if "hicp" in query_lower:
                return "HICP inflation"
            if "cpi" in query_lower:
                return "CPI inflation"
            return "inflation rate"
        if "credit" in cues:
            return "private sector credit to GDP"
        if "savings" in cues:
            return "gross savings (% of GDP)"
        if "gdp" in cues:
            if "growth" in query_lower:
                return "GDP growth"
            if "per capita" in query_lower:
                return "GDP per capita"
            return "GDP"

        return ""

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

        years = [int(match) for match in re.findall(r"\b(19\d{2}|20\d{2})\b", query_lower)]
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
                    if context_countries and not self._provider_covers_country_list(provider, context_countries):
                        continue

                    # Check if this provider has the indicator
                    resolved = resolver.resolve(
                        indicator,
                        provider=provider,
                        country=country,
                        countries=context_countries or None,
                        use_cache=False,
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
                from .catalog_service import get_fallback_providers as get_compat_fallbacks
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
            returned_iso2: set[str] = set()
            for data in fallback_result:
                if not data.metadata or not data.metadata.country:
                    continue

                result_country = data.metadata.country
                result_iso2 = self._normalize_country_to_iso2(result_country)
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

        # Geography/context words should not dominate semantic relevance checks.
        geo_terms: set[str] = set()
        for alias in CountryResolver.COUNTRY_ALIASES.keys():
            for token in re.findall(r"[a-z0-9]+", str(alias or "").lower()):
                if len(token) >= 2:
                    geo_terms.add(token)
        for code in CountryResolver.COUNTRY_ALIASES.values():
            token = str(code or "").strip().lower()
            if token:
                geo_terms.add(token)

        # Extract key terms from text
        def extract_key_terms(text: str) -> set:
            stop_words = {
                'data', 'statistics', 'annual', 'quarterly', 'monthly',
                'index', 'rate', 'by', 'and', 'the', 'of', 'for', 'in', 'to',
                'a', 'an', 'all', 'from', 'with', 'as', 'at', 'show', 'plot',
                'get', 'find', 'display', 'chart', 'graph', 'value', 'values',
                'economic', 'activity', 'activities',
                'trend', 'trends', 'historical', 'history', 'before', 'after',
                'between', 'versus', 'vs', 'across', 'compare', 'comparison',
                'contrast', 'since', 'last', 'past', 'latest',
            }
            terms = set()
            # Use regex tokenization so dotted/underscored provider codes
            # (for example PX.REX.REER) preserve informative sub-tokens.
            for clean in re.findall(r"[a-z0-9]+", text.lower().replace('-', ' ').replace('_', ' ')):
                if not clean:
                    continue
                if clean.isdigit():
                    continue
                if re.fullmatch(r"(19|20)\d{2}", clean):
                    continue
                if clean in geo_terms:
                    continue
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
        original_cues = self._extract_indicator_cues(original_text)
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
                candidate_cues = self._extract_indicator_cues(candidate_text)
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
        original_subjects = original_terms & subject_entities
        original_metrics = original_terms & metric_types
        original_qualifiers = original_terms & metric_qualifiers

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
        # Use semantic indicator query (or original query) for smarter fallbacks.
        indicator = self._select_indicator_query_for_resolution(intent)
        if not indicator:
            indicator = str(intent.originalQuery or "").strip() or (
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

            fallback_indicators = list(intent.indicators or [])
            fallback_indicator_query = self._select_indicator_query_for_resolution(intent)
            if fallback_indicator_query:
                if not fallback_indicators:
                    fallback_indicators = [fallback_indicator_query]
                elif len(fallback_indicators) == 1:
                    # Normalize to a semantic indicator phrase across providers.
                    # This prevents provider-native source codes (for example IMF EREER)
                    # from leaking into fallback providers with different code spaces.
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

            pending_choice_response = await self._try_resolve_pending_indicator_choice(
                query=query,
                conversation_id=conv_id,
                tracker=tracker,
            )
            if pending_choice_response is not None:
                return pending_choice_response

            pending_country_follow_up = await self._try_resolve_pending_country_follow_up(
                query=query,
                conversation_id=conv_id,
                auto_pro_mode=auto_pro_mode,
                tracker=tracker,
            )
            if pending_country_follow_up is not None:
                return pending_country_follow_up

            pending_semantic_response = await self._try_resolve_pending_semantic_clarification(
                query=query,
                conversation_id=conv_id,
                auto_pro_mode=auto_pro_mode,
                use_orchestrator=use_orchestrator,
                allow_orchestrator=allow_orchestrator,
                tracker=tracker,
            )
            if pending_semantic_response is not None:
                return pending_semantic_response

            # All pending clarification attempts failed — this is a new query.
            # Clear stale pending states to prevent them from interfering with
            # future turns (fixes zombie clarification bug).
            conversation_manager.clear_all_pending(conv_id)

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

            # Auto-rewrite broad concepts for multi-country region queries.
            query = self._auto_resolve_broad_concept_for_region(query)

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

            logger.info("Parsing query with LLM: %s", query)

            with tracker.track("parsing_query", "🤖 Understanding your question...") as update_parse_metadata:
                parse_result = await self.pipeline.parse_and_route(query, history)
                intent = parse_result.intent
                logger.debug("Parsed intent: %s", intent.model_dump())
                update_parse_metadata({
                    "provider": intent.apiProvider,
                    "indicators": intent.indicators,
                })

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

            # For FRED provider, set indicator (let _series_id() handle normalization)
            if normalize_provider_name(intent.apiProvider) == "FRED":
                params["indicator"] = indicator

            # For StatsCan, set indicator field
            if normalize_provider_name(intent.apiProvider) == "STATSCAN":
                params["indicator"] = indicator

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

            # Create a new intent with single indicator
            single_intent = ParsedIntent(
                apiProvider=single_provider,
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

        internal_param_keys = {"__fallback_excluded_providers", "__catalog_resolved", "__qualifier_checked"}
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

            # LangGraph mode - state-persistent agent orchestration.
            # Pass the pre-resolved intent so LangGraph doesn't re-parse from scratch
            # (the intent has already been through concept override, indicator resolution,
            # semantic validation, etc. — all of which would be lost on re-parse).
            if use_langgraph and not use_react_agent:
                return await self._execute_with_langgraph(
                    query, conversation_id, conversation_history, tracker,
                    pre_resolved_intent=intent,
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

                if isinstance(data, list) and data:
                    self._normalize_bis_metadata_labels(data)
                    data = self._rerank_data_by_query_relevance(query, data)
                    data = self._apply_ranking_projection(query, data)

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
                    if provider_name == "Unknown" or indicators == "requested indicator":
                        logger.warning(
                            "Orchestrator returned empty result without usable routing metadata. "
                            "Retrying through standard pipeline."
                        )
                        return await self._standard_query_processing(
                            query,
                            conversation_id,
                            tracker,
                            record_user_message=False,
                        )

                    recovery_intent = ParsedIntent(
                        apiProvider=str(provider_name),
                        indicators=list(indicators_list) if indicators_list else [query],
                        parameters={"country": country} if country else {},
                        clarificationNeeded=False,
                        originalQuery=query,
                    )
                    recovered_data = await self._maybe_recover_from_empty_data(query, recovery_intent)
                    if recovered_data:
                        recovered_data, coverage_warning = await self._maybe_improve_country_coverage(
                            query,
                            recovery_intent,
                            recovered_data,
                        )
                        return QueryResponse(
                            conversationId=conversation_id,
                            intent=recovery_intent,
                            data=recovered_data,
                            clarificationNeeded=False,
                            message=coverage_warning,
                            processingSteps=tracker.to_list() if tracker else None,
                        )

                    no_data_clarification = self._build_no_data_indicator_clarification(
                        conversation_id=conversation_id,
                        query=query,
                        intent=recovery_intent,
                        processing_steps=tracker.to_list() if tracker else None,
                    )
                    if no_data_clarification:
                        return no_data_clarification

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
                    if response.intent:
                        recovered_uncertain_data = await self._maybe_recover_from_uncertain_match(
                            query,
                            response.intent,
                            data,
                        )
                        if recovered_uncertain_data:
                            data = recovered_uncertain_data
                            response.data = data
                    if response.intent:
                        data, coverage_warning = await self._maybe_improve_country_coverage(
                            query,
                            response.intent,
                            data,
                        )
                        response.data = data
                        if coverage_warning:
                            if response.message:
                                response.message = f"{response.message}\n\n{coverage_warning}"
                            else:
                                response.message = coverage_warning
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

        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            data=data,
            clarificationNeeded=False,
            message=coverage_warning,
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
