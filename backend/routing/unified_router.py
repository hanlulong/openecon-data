"""
Unified Router - Single Entry Point for All Routing Decisions

This module consolidates routing logic from:
- provider_router.py (deterministic rules)
- deep_agent_orchestrator.py (scoring-based selection)
- catalog_service.py (YAML-based indicator mappings)

Components used:
- CountryResolver: Country normalization and region membership
- CatalogService: Indicator-to-provider mappings from YAML

Provider routing formerly relied on KeywordMatcher (600+ lines of regex).
Phase 2 of the LLM-refactor inlined the few still-needed checks and
deleted keyword_matcher.py.  The LLM prompt + catalog + country routing
now cover the same ground with less brittle code.

Usage:
    from backend.routing import UnifiedRouter

    router = UnifiedRouter()
    decision = router.route(query, intent)

    print(f"Provider: {decision.provider}")
    print(f"Confidence: {decision.confidence}")
    print(f"Reasoning: {decision.reasoning}")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Set, Tuple

from .country_resolver import CountryResolver

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lightweight inline replacements for KeywordMatcher (removed in Phase 2)
# ---------------------------------------------------------------------------

# Explicit provider keywords — detects "from FRED", "using IMF", etc.
_EXPLICIT_PROVIDER_KEYWORDS: Dict[str, List[str]] = {
    "OECD": ["from oecd", "using oecd", "via oecd", "according to oecd", "oecd data"],
    "FRED": ["fred", "from fred", "using fred", "via fred", "federal reserve", "st. louis fed", "stlouisfed", "the fed"],
    "WorldBank": ["world bank", "worldbank", "from world bank", "using world bank", "wb data", "world bank data"],
    "Comtrade": ["comtrade", "un comtrade", "from comtrade", "using comtrade", "united nations comtrade"],
    "StatsCan": ["statscan", "statistics canada", "stats canada", "from statscan", "using statscan"],
    "IMF": ["from imf", "using imf", "international monetary fund", "from the imf", "according to the imf", "imf data"],
    "BIS": ["from bis", "using bis", "bank for international settlements", "bis data"],
    "Eurostat": ["from eurostat", "using eurostat", "via eurostat", "according to eurostat", "eu statistics", "european statistics", "eurostat data"],
    "ExchangeRate": ["exchangerate", "exchange rate api", "from exchangerate"],
    "CoinGecko": ["coingecko", "coin gecko", "from coingecko", "using coingecko", "crypto prices"],
}

_START_OF_QUERY_PROVIDERS = ["OECD", "IMF", "BIS", "Eurostat"]
_START_OF_QUERY_EXCLUSIONS = ["countries", "country", "members", "member", "nations", "nation", "average"]

# US-only indicators that must use FRED
_US_ONLY_INDICATORS: Set[str] = {
    "case-shiller", "case shiller",
    "federal funds", "fed funds",
    "pce", "personal consumption",
    "nonfarm payrolls",
    "initial claims", "unemployment claims",
    "michigan consumer", "consumer sentiment",
    "s&p 500", "sp500", "s&p",
    "dow jones", "djia",
    "prime lending rate",
    "mortgage rate", "30-year mortgage",
}

# Anti-misrouting: fiscal keywords that should NEVER go to CoinGecko
_NON_CRYPTO_FISCAL_KEYWORDS: Set[str] = {
    "government", "deficit", "surplus", "fiscal", "budget",
    "debt", "gdp", "unemployment", "inflation", "trade",
    "export", "import", "tax", "spending", "economic",
}

_CRYPTO_KEYWORDS: Set[str] = {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
    "solana", "cardano", "dogecoin", "altcoin", "defi", "nft",
    "blockchain", "stablecoin", "coin", "token", "xrp", "ripple",
    "litecoin", "ltc", "bnb",
}


def detect_explicit_provider_match(query: str) -> Optional[Tuple[str, str]]:
    """Return (provider, matched_keyword) if user explicitly names a provider, else None."""
    query_lower = query.lower()

    for provider in _START_OF_QUERY_PROVIDERS:
        provider_lower = provider.lower()
        if query_lower.startswith(provider_lower + " "):
            if not any(term in query_lower[:30] for term in _START_OF_QUERY_EXCLUSIONS):
                return provider, f"{provider} (at start)"

    for provider, keywords in _EXPLICIT_PROVIDER_KEYWORDS.items():
        for keyword in keywords:
            if keyword in query_lower:
                return provider, keyword

    return None


def detect_us_only_indicator(
    query: str,
    indicators: List[str],
    country: Optional[str] = None,
    countries: Optional[List[str]] = None,
) -> Optional[str]:
    """Return matched US-only indicator term, or None."""
    all_countries = countries or ([country] if country else [])
    if all_countries:
        non_us = [c for c in all_countries
                  if c.upper() not in ("US", "USA", "UNITED STATES", "UNITED_STATES")]
        if non_us:
            return None

    query_lower = query.lower()
    indicators_str = " ".join(indicators).lower() if indicators else ""
    combined = f"{query_lower} {indicators_str}"

    for indicator in _US_ONLY_INDICATORS:
        if indicator in combined:
            return indicator

    return None


def _correct_coingecko(provider: str, query: str, indicators: List[str]) -> Tuple[str, Optional[str]]:
    """If CoinGecko was chosen for a non-crypto query, redirect to IMF.

    Returns (corrected_provider, reason_or_None).
    """
    if provider.upper() != "COINGECKO":
        return provider, None

    query_lower = query.lower()
    indicators_str = " ".join(indicators).lower() if indicators else ""
    combined = f" {query_lower} {indicators_str} "

    def has_word(text: str, word: str) -> bool:
        return f" {word} " in text or text.startswith(f"{word} ") or text.endswith(f" {word}")

    has_crypto = any(has_word(combined, kw) for kw in _CRYPTO_KEYWORDS)
    has_fiscal = any(has_word(combined, kw) for kw in _NON_CRYPTO_FISCAL_KEYWORDS)

    if has_fiscal and not has_crypto:
        reason = "CoinGecko corrected to IMF: query has fiscal keywords but no crypto"
        logger.warning(f"  {reason}")
        return "IMF", reason

    return provider, None


@dataclass
class RoutingDecision:
    """Result of a routing decision."""
    provider: str
    confidence: float
    fallbacks: List[str] = field(default_factory=list)
    reasoning: str = ""
    match_type: str = "default"  # explicit, keyword, indicator, country, catalog, default
    matched_pattern: Optional[str] = None


class UnifiedRouter:
    """
    Single entry point for all provider routing decisions.

    Routing Priority:
    1. Explicit provider mention (highest confidence)
    2. US-only indicators → FRED
    3. Keyword-based indicator patterns
    4. Canadian queries → StatsCan
    5. Country-based routing (EU → Eurostat, non-OECD → WorldBank)
    6. CatalogService lookup (YAML-based)
    7. LLM provider choice (lowest confidence)
    """

    # Fallback chains when primary provider fails
    # IMPORTANT: Avoid circular chains! OECD → Eurostat → OECD is NOT allowed.
    # Keys are UPPERCASE for consistent lookup via get_fallbacks()
    FALLBACK_MAP: Dict[str, List[str]] = {
        "OECD": ["WorldBank", "Eurostat"],
        "EUROSTAT": ["WorldBank", "IMF"],  # NOT OECD - avoids circular chain
        "BIS": ["IMF", "WorldBank"],
        "IMF": ["BIS", "WorldBank", "OECD"],
        "STATSCAN": ["WorldBank", "OECD"],
        "FRED": ["WorldBank", "OECD"],
        "COMTRADE": ["WorldBank"],
        "WORLDBANK": ["IMF", "OECD"],  # WorldBank needs fallbacks too
        "EXCHANGERATE": ["FRED"],  # FRED has some exchange rate data
        "COINGECKO": [],  # No fallback for crypto
    }

    # Default provider when no other rules match
    DEFAULT_PROVIDER = "WorldBank"

    def __init__(self, catalog_service=None, use_catalog: bool = True):
        """
        Initialize router with optional CatalogService.

        Args:
            catalog_service: Optional CatalogService instance for YAML lookups.
                             If None and use_catalog=True, will auto-import.
            use_catalog: Whether to use CatalogService for routing decisions.
        """
        self._catalog_service = catalog_service
        self._use_catalog = use_catalog

        # Auto-import CatalogService if not provided
        if self._catalog_service is None and self._use_catalog:
            try:
                from ..services import catalog_service as cs
                self._catalog_service = cs
            except ImportError:
                logger.debug("CatalogService not available, catalog routing disabled")
                self._use_catalog = False

    def route(
        self,
        query: str,
        indicators: Optional[List[str]] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        llm_provider: Optional[str] = None,
    ) -> RoutingDecision:
        """
        Determine the best provider for a query.

        Args:
            query: User's natural language query
            indicators: List of parsed indicators (from LLM)
            country: Single country from intent parameters
            countries: Multiple countries from intent parameters
            llm_provider: Provider suggested by LLM (lowest priority)

        Returns:
            RoutingDecision with provider, confidence, fallbacks, and reasoning
        """
        indicators = indicators or []
        countries = countries or []
        query_lower = query.lower()

        # Fallback geography extraction when parser omits country information.
        if not country and not countries:
            detected_countries = CountryResolver.detect_all_countries_in_query(query)
            if len(detected_countries) == 1:
                country = detected_countries[0]
            elif len(detected_countries) > 1:
                countries = detected_countries

        # Priority 1: Explicit provider mention (ABSOLUTE HIGHEST)
        explicit_match = detect_explicit_provider_match(query)
        if explicit_match:
            provider_name, matched_kw = explicit_match
            return self._create_decision(
                provider=provider_name,
                confidence=1.0,
                match_type="explicit",
                matched_pattern=matched_kw,
                reasoning=f"Explicit mention of '{matched_kw}' requests {provider_name}",
            )

        # Priority 2: US-only indicators (MUST use FRED)
        # Pass country context so non-US queries skip this check
        us_indicator = detect_us_only_indicator(
            query, indicators, country=country, countries=countries
        )
        if us_indicator:
            return self._create_decision(
                provider="FRED",
                confidence=0.95,
                match_type="indicator",
                matched_pattern=us_indicator,
                reasoning=f"'{us_indicator}' is a US-only indicator that requires FRED",
            )

        # Priority 3: Special case handlers (before keyword matching)

        # 3a: Crypto and token market data → CoinGecko
        if self._is_crypto_query(query_lower, indicators):
            return self._create_decision(
                provider="CoinGecko",
                confidence=0.92,
                match_type="indicator",
                matched_pattern="crypto asset query",
                reasoning="Cryptocurrency/token market query routed to CoinGecko",
            )

        # 3b: Exchange rate queries → ExchangeRate
        if self._is_exchange_rate_query(query_lower, indicators):
            # Exception: Real/Nominal effective exchange rate (REER/NEER) → BIS
            # BIS has WS_XRU (REER) and WS_EER (NEER) with global country coverage
            if any(t in query_lower for t in ("real effective exchange rate", "reer", "nominal effective exchange rate", "neer", "effective exchange rate")):
                return self._create_decision(
                    provider="BIS",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="effective exchange rate",
                    reasoning="Effective exchange rates (REER/NEER) are best sourced from BIS (WS_XRU/WS_EER)",
                )
            return self._create_decision(
                provider="ExchangeRate",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="exchange rate",
                reasoning="Exchange rate query routed to ExchangeRate-API",
            )

        # 3c: Trade as % of GDP → WorldBank (NOT Comtrade)
        if self._is_trade_ratio_query(query_lower, indicators):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="trade % of GDP",
                reasoning="Trade as percentage of GDP is a development indicator from WorldBank",
            )

        # 3c2: Debt-to-GDP style macro debt ratios → IMF.
        # Avoid routing these to BIS debt-service datasets.
        if self._is_macro_debt_ratio_query(query_lower, indicators):
            return self._create_decision(
                provider="IMF",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="debt-to-gdp ratio",
                reasoning="Macro debt-to-GDP ratio queries are best served by IMF fiscal datasets",
            )

        # 3d: US trade balance (without partner) → FRED
        if self._is_us_trade_balance_no_partner(query, country):
            return self._create_decision(
                provider="FRED",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="US trade balance",
                reasoning="US trade balance (without partner) uses FRED BOPGSTB series",
            )

        # 3d2: US housing construction indicators (housing starts/building permits) → FRED
        if self._is_us_housing_construction_query(query, query_lower, indicators, country):
            return self._create_decision(
                provider="FRED",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="US housing construction",
                reasoning="US housing starts/building permits are FRED series",
            )

        # 3d3: Money supply / money stock queries.
        if self._is_money_supply_query(query_lower, indicators):
            if self._is_us_context(country, query_lower):
                return self._create_decision(
                    provider="FRED",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="US money supply",
                    reasoning="US money supply/stock series are best sourced from FRED",
                )
            return self._create_decision(
                provider="WorldBank",
                confidence=0.84,
                match_type="indicator",
                matched_pattern="money supply",
                reasoning="Cross-country money supply queries are best covered by WorldBank",
            )

        # 3d4: Bond-yield / sovereign-rate queries.
        if self._is_bond_yield_query(query_lower, indicators):
            if self._is_us_context(country, query_lower):
                return self._create_decision(
                    provider="FRED",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="US bond yield",
                    reasoning="US Treasury yield queries are best sourced from FRED",
                )
            if "oecd" in query_lower:
                return self._create_decision(
                    provider="OECD",
                    confidence=0.82,
                    match_type="indicator",
                    matched_pattern="oecd bond yield",
                    reasoning="OECD long-term interest rate comparisons route to OECD",
                )
            return self._create_decision(
                provider="IMF",
                confidence=0.78,
                match_type="indicator",
                matched_pattern="bond yield",
                reasoning="Cross-country sovereign yield queries default to IMF",
            )

        # 3e: IMF for forecast/projection and global macro aggregates.
        if self._is_forecast_or_projection_query(query_lower):
            return self._create_decision(
                provider="IMF",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="forecast/projection",
                reasoning="Forecast/projection macro query routed to IMF",
            )

        if self._is_global_macro_imf_query(query_lower):
            return self._create_decision(
                provider="IMF",
                confidence=0.86,
                match_type="indicator",
                matched_pattern="global macro aggregate",
                reasoning="Global/aggregate macro query routed to IMF",
            )

        # 3f: Multi-country ratio comparisons are best covered by WorldBank.
        if self._is_worldbank_cross_country_ratio_query(query_lower):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.84,
                match_type="indicator",
                matched_pattern="cross-country ratio",
                reasoning="Cross-country ratio comparison routed to WorldBank",
            )

        if self._is_worldbank_group_current_account_query(query_lower):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.84,
                match_type="indicator",
                matched_pattern="group current account",
                reasoning="Country-group current account history routed to WorldBank",
            )

        # 3g: Canadian query handling
        if CountryResolver.is_canadian_region(query):
            return self._handle_canadian_query(query, indicators, country)

        # 3h: Property/house prices → BIS
        if self._is_property_price_query(query_lower, indicators):
            return self._create_decision(
                provider="BIS",
                confidence=0.88,
                match_type="indicator",
                matched_pattern="property prices",
                reasoning="Property/house prices best sourced from BIS",
            )

        # 3i: Merchandise trade flow queries default to Comtrade.
        if self._is_merchandise_trade_flow_query(query_lower):
            return self._create_decision(
                provider="Comtrade",
                confidence=0.86,
                match_type="indicator",
                matched_pattern="merchandise trade flow",
                reasoning="Import/export goods flow query routed to Comtrade",
            )

        # 3i2: Bilateral trade deficit/surplus with explicit partner → Comtrade.
        # "Trade deficit between US and Mexico" is a bilateral flow query.
        if self._is_bilateral_trade_balance_query(query_lower, query):
            return self._create_decision(
                provider="Comtrade",
                confidence=0.86,
                match_type="indicator",
                matched_pattern="bilateral trade balance",
                reasoning="Bilateral trade deficit/surplus with partner routed to Comtrade",
            )

        # 3j: Prefer Eurostat for standard historical EU-country macro indicators.
        if country and CountryResolver.is_eu_member(country):
            if self._is_eurostat_country_indicator_query(query_lower):
                return self._create_decision(
                    provider="Eurostat",
                    confidence=0.82,
                    match_type="country",
                    matched_pattern=country,
                    reasoning=f"EU country ({country}) historical macro query routed to Eurostat",
                )

        # Priority 3.5: CatalogService lookup (curated concept → provider mappings)
        # Placed after special-case handlers (crypto, exchange rate, trade flows,
        # Eurostat EU) but before generic keyword matching. This ensures specific
        # routing rules (e.g., bilateral trade → Comtrade) are respected, while
        # catalog concepts (e.g., jobless_claims → FRED/ICSA) still override
        # generic keyword and country-based routing.
        if self._use_catalog and self._catalog_service:
            catalog_decision = self._route_by_catalog(indicators, country, query=query)
            if catalog_decision:
                return catalog_decision

        # NOTE: Priority 4 (keyword indicator patterns) and Priority 5
        # (regional keywords) were removed in the Phase 2 LLM refactor.
        # The catalog (Priority 3.5) + country-based routing (Priority 6)
        # + special-case handlers (3a-3j) + regional group routing cover
        # the same ground.

        # Priority 5: Regional group routing (OECD countries, EU countries, etc.)
        regional_decision = self._route_by_regional_group(query_lower)
        if regional_decision:
            return regional_decision

        # Priority 6: Country-based routing
        country_decision = self._route_by_country(country, countries, query_lower, indicators)
        if country_decision:
            return country_decision

        # Priority 7: (Catalog lookup moved to Priority 1.5)

        # Priority 8: Multi-country with non-OECD → WorldBank
        if countries and len(countries) > 1:
            has_non_oecd = any(CountryResolver.is_non_oecd_major(c) for c in countries)
            if has_non_oecd:
                return self._create_decision(
                    provider="WorldBank",
                    confidence=0.75,
                    match_type="country",
                    reasoning="Multi-country query with non-OECD countries → WorldBank",
                )

        # Priority 9: Use LLM's suggestion if available
        if llm_provider and llm_provider != self.DEFAULT_PROVIDER:
            # Validate and potentially correct LLM choice
            corrected, reason = _correct_coingecko(
                llm_provider, query, indicators
            )
            return self._create_decision(
                provider=corrected,
                confidence=0.60,
                match_type="llm",
                reasoning=reason or f"Using LLM suggested provider: {llm_provider}",
            )

        # Priority 10: Default provider
        return self._create_decision(
            provider=self.DEFAULT_PROVIDER,
            confidence=0.50,
            match_type="default",
            reasoning=f"No specific routing rules matched, using default: {self.DEFAULT_PROVIDER}",
        )

    def route_with_intent(self, intent: Any, original_query: str) -> RoutingDecision:
        """
        Route using a ParsedIntent object (compatibility method).

        Args:
            intent: ParsedIntent object from LLM parsing
            original_query: Original user query

        Returns:
            RoutingDecision
        """
        # Extract data from intent
        indicators = getattr(intent, "indicators", []) or []
        parameters = getattr(intent, "parameters", {}) or {}
        country = parameters.get("country", "")
        countries = parameters.get("countries", [])
        llm_provider = getattr(intent, "apiProvider", None)

        return self.route(
            query=original_query,
            indicators=indicators,
            country=country,
            countries=countries,
            llm_provider=llm_provider,
        )

    def get_fallbacks(self, provider: str) -> List[str]:
        """Get fallback providers when primary fails."""
        return self.FALLBACK_MAP.get(provider.upper(), [self.DEFAULT_PROVIDER])

    # ==========================================================================
    # Private Helper Methods
    # ==========================================================================

    def _create_decision(
        self,
        provider: str,
        confidence: float,
        match_type: str = "default",
        matched_pattern: Optional[str] = None,
        reasoning: str = "",
    ) -> RoutingDecision:
        """Create a RoutingDecision with fallbacks."""
        fallbacks = self.get_fallbacks(provider)

        logger.info(f"🎯 Routing: {provider} (conf={confidence:.2f}, type={match_type})")
        if matched_pattern:
            logger.debug(f"   Pattern: {matched_pattern}")

        return RoutingDecision(
            provider=provider,
            confidence=confidence,
            fallbacks=fallbacks,
            reasoning=reasoning,
            match_type=match_type,
            matched_pattern=matched_pattern,
        )

    def _is_crypto_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Check if query is clearly about cryptocurrencies/tokens."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        strong_token_terms = [
            "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
            "xrp", "ripple", "solana", "cardano", "dogecoin", "litecoin",
            "altcoin", "defi", "nft", "token", "stablecoin", "bnb",
        ]
        strong_phrase_terms = ["binance coin"]

        if any(
            re.search(rf"\b{re.escape(term)}\b", combined)
            for term in strong_token_terms
        ):
            return True
        if any(phrase in combined for phrase in strong_phrase_terms):
            return True

        has_market_word = any(term in combined for term in ["market cap", "market capitalization", "trading volume", "coin ranking"])
        has_coin_context = any(term in combined for term in ["coin", "token", "cryptocurrency", "crypto"])
        return bool(has_market_word and has_coin_context)

    def _is_exchange_rate_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Check if query is about exchange rates."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        exchange_patterns = [
            "exchange rate", "forex", "currency exchange", "fx rate",
            "usd to", "eur to", "gbp to", "jpy to", "cad to", "aud to",
            "to usd", "to eur", "to gbp", "to jpy", "to cad", "to aud",
            "usd/", "eur/", "gbp/", "/usd", "/eur", "/gbp",
            "dollar to euro", "euro to dollar", "pound to dollar",
        ]
        return any(pattern in combined for pattern in exchange_patterns)

    def _is_trade_ratio_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Check if query is about trade as % of GDP (WorldBank indicator)."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        has_trade = any(term in combined for term in ["export", "import", "trade"])
        ratio_patterns = [
            "as % of gdp", "% of gdp", "as percentage of gdp", "as percent of gdp",
            "to gdp ratio", "/gdp ratio", "to gdp", "/gdp",
            "share of gdp", "as share of gdp",
        ]
        has_ratio = any(pattern in query_lower for pattern in ratio_patterns)

        return has_trade and has_ratio

    def _is_macro_debt_ratio_query(self, query_lower: str, indicators: List[str]) -> bool:
        """
        Detect public/macro debt-to-GDP style requests.

        Excludes BIS-specific debt contexts (debt service, household/private credit).
        """
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        if "debt" not in combined:
            return False

        ratio_patterns = [
            "debt to gdp",
            "debt-to-gdp",
            "debt as % of gdp",
            "debt as percentage of gdp",
            "% of gdp debt",
            "gdp to debt ratio",
            "gdp/debt ratio",
        ]
        has_ratio = any(pattern in combined for pattern in ratio_patterns)
        if not has_ratio:
            return False

        bis_specific_terms = [
            "debt service",
            "debt service ratio",
            "household debt",
            "private debt",
            "private sector credit",
            "credit to gdp",
            "credit gap",
            "bank credit",
        ]
        if any(term in combined for term in bis_specific_terms):
            return False

        return True

    def _is_us_trade_balance_no_partner(self, query: str, country: Optional[str]) -> bool:
        """Check if query is US trade balance without partner country."""
        query_lower = query.lower()
        is_us = self._is_us_context(country, query_lower)
        is_trade_balance = any(term in query_lower for term in [
            "trade balance", "trade deficit", "trade surplus"
        ])
        has_partner = self._has_bilateral_trade_partner(query)

        return is_us and is_trade_balance and not has_partner

    def _is_us_housing_construction_query(
        self,
        query: str,
        query_lower: str,
        indicators: List[str],
        country: Optional[str],
    ) -> bool:
        """Check if query asks for US housing starts/building permits."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        if not self._is_us_context(country, query_lower):
            return False

        return any(term in combined for term in [
            "housing starts",
            "building permits",
            "building permit",
            "residential construction",
            "housing construction",
        ])

    @staticmethod
    def _is_us_context(country: Optional[str], query_lower: str) -> bool:
        """Detect US query context from structured country or query text."""
        return (
            (country and country.upper() in ["US", "USA", "UNITED STATES"])
            or any(term in query_lower for term in ["us ", "u.s.", "united states", "america"])
        )

    def _is_money_supply_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Detect money supply and monetary aggregate queries."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"
        return any(term in combined for term in [
            "money supply",
            "money stock",
            "broad money",
            "narrow money",
            "m1",
            "m2",
            "m3",
            "monetary aggregate",
        ])

    def _is_bond_yield_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Detect sovereign/government bond yield and long-term interest queries."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"
        return any(term in combined for term in [
            "bond yield",
            "government bond yield",
            "treasury yield",
            "10-year yield",
            "10 year yield",
            "2-year yield",
            "2 year yield",
            "yield spread",
            "yield curve",
            "long-term interest rate",
            "long term interest rate",
            "sovereign yield",
        ])

    def _is_property_price_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Check if query is about property/house prices."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        return any(term in combined for term in [
            "property price", "house price", "property prices", "house prices",
            "housing price", "real estate price", "real estate market",
            "residential property", "housing market"
        ])

    def _is_forecast_or_projection_query(self, query_lower: str) -> bool:
        """Check if query asks for forecasted/projected data."""
        return any(term in query_lower for term in [
            "forecast", "forecasts", "projection", "projections",
            "outlook", "scenario", "expected", "expectation",
        ])

    def _is_global_macro_imf_query(self, query_lower: str) -> bool:
        """Detect global/aggregate macro queries that are better served by IMF datasets."""
        has_scope = any(term in query_lower for term in [
            "world", "global", "emerging markets", "emerging economies",
            "advanced economies", "developing economies", "developing countries",
            "eurozone",
        ])
        if not has_scope:
            return False

        # IMF WEO-style aggregates and global macro monitoring.
        if any(term in query_lower for term in [
            "trade volume", "commodity price", "commodity prices index",
            "world economic outlook",
        ]):
            return True

        if "current account" in query_lower and any(term in query_lower for term in [
            "world", "global", "emerging", "advanced",
        ]):
            return True

        if "inflation" in query_lower and any(term in query_lower for term in [
            "world", "global", "developing", "emerging", "advanced",
        ]):
            return True

        return False

    def _is_worldbank_cross_country_ratio_query(self, query_lower: str) -> bool:
        """Detect cross-country indicator ratios that should default to WorldBank."""
        has_group_scope = any(term in query_lower for term in [
            " countries", "economies", "region", "regions",
            "european countries", "oil exporting countries", "oecd countries",
            "latin american countries", "middle eastern countries",
            "african countries", "g7 countries", "brics countries",
        ])
        if not has_group_scope:
            return False

        ratio_patterns = [
            "as % of gdp", "% of gdp", "as percentage of gdp", "as percent of gdp",
            "to gdp ratio", "ratio to gdp", "share of gdp",
        ]
        has_ratio = any(pattern in query_lower for pattern in ratio_patterns)
        if not has_ratio:
            return False

        # Trade ratios are handled separately in _is_trade_ratio_query.
        if any(term in query_lower for term in ["import", "export", "trade as"]):
            return False

        # Forecast-style ratios should remain with IMF.
        if self._is_forecast_or_projection_query(query_lower):
            return False

        return True

    def _is_worldbank_group_current_account_query(self, query_lower: str) -> bool:
        """Detect historical country-group current account queries for WorldBank."""
        if "current account" not in query_lower:
            return False
        if self._is_forecast_or_projection_query(query_lower):
            return False
        if self._is_global_macro_imf_query(query_lower):
            return False
        return any(term in query_lower for term in [
            "countries", "country", "oil exporting", "european countries",
            "latin american", "african", "asian countries",
        ])

    def _is_bilateral_trade_balance_query(self, query_lower: str, query: str) -> bool:
        """Detect trade deficit/surplus/balance queries with an explicit bilateral partner."""
        has_trade_balance = any(term in query_lower for term in [
            "trade deficit", "trade surplus", "trade balance",
        ])
        if not has_trade_balance:
            return False
        return self._has_bilateral_trade_partner(query)

    def _is_merchandise_trade_flow_query(self, query_lower: str) -> bool:
        """Detect merchandise import/export flow queries for Comtrade."""
        has_flow = any(term in query_lower for term in [" exports", " export", " imports", " import"])
        if not has_flow:
            return False

        # Keep macro trade indicators away from Comtrade routing.
        blocked_patterns = [
            "trade balance", "trade deficit", "trade surplus", "trade volume",
            "current account", "% of gdp", "as percentage of gdp", "to gdp ratio",
        ]
        if any(pattern in query_lower for pattern in blocked_patterns):
            return False

        return True

    def _is_eurostat_country_indicator_query(self, query_lower: str) -> bool:
        """Detect historical EU-country macro queries that should use Eurostat."""
        if self._is_forecast_or_projection_query(query_lower):
            return False
        if self._is_property_price_query(query_lower, []):
            return False
        return any(term in query_lower for term in [
            "gdp", "inflation", "unemployment", "employment",
            "government debt", "youth unemployment", "trade balance",
            "energy consumption", "population",
        ])

    def _handle_canadian_query(
        self,
        query: str,
        indicators: List[str],
        country: Optional[str]
    ) -> RoutingDecision:
        """Handle Canadian-specific routing."""
        query_lower = query.lower()
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        # Property market level queries are better served by BIS cross-country datasets.
        if any(term in combined for term in [
            "residential property", "property prices", "real estate market", "real estate prices",
        ]):
            return self._create_decision(
                provider="BIS",
                confidence=0.86,
                match_type="indicator",
                matched_pattern="canada property market",
                reasoning="Canadian residential/property market query routed to BIS",
            )

        # Check for trade queries
        is_trade = any(term in combined for term in ["import", "export", "trade"])

        if is_trade:
            # Bilateral trade with partner → Comtrade
            has_partner = self._has_bilateral_trade_partner(query)
            if has_partner:
                return self._create_decision(
                    provider="Comtrade",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="Canadian bilateral trade",
                    reasoning="Canadian bilateral trade (with partner) → Comtrade",
                )
            # Trade balance (no partner) → StatsCan
            if "trade balance" in combined:
                return self._create_decision(
                    provider="StatsCan",
                    confidence=0.85,
                    match_type="indicator",
                    matched_pattern="Canadian trade balance",
                    reasoning="Canadian trade balance (no partner) → StatsCan",
                )
            # Exports/Imports (no partner) → StatsCan
            return self._create_decision(
                provider="StatsCan",
                confidence=0.85,
                match_type="indicator",
                matched_pattern="Canadian imports/exports",
                reasoning="Canadian imports/exports (no partner) → StatsCan",
            )

        # For basic global indicators, prefer WorldBank over StatsCan
        # (StatsCan has complex table IDs that don't resolve well for
        # simple concepts like "population" or "GDP")
        global_indicators = [
            "population", "life expectancy", "fertility", "mortality",
            "gdp", "gdp per capita", "gdp growth",
            "co2", "emissions", "forest", "renewable energy",
            "literacy", "education", "poverty",
        ]
        if any(term in combined for term in global_indicators):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.80,
                match_type="indicator",
                matched_pattern="Canada global indicator",
                reasoning="Canadian query with global indicator → WorldBank (broader coverage)",
            )

        # Default for Canadian queries → StatsCan
        return self._create_decision(
            provider="StatsCan",
            confidence=0.85,
            match_type="country",
            matched_pattern="Canada",
            reasoning="Canadian query routed to StatsCan",
        )

    def _route_by_regional_group(self, query_lower: str) -> Optional[RoutingDecision]:
        """Route queries that mention specific regional/country groups."""
        # Eurostat for EU group queries
        eu_group_terms = [
            "european countries", "eu countries", "eu member states",
            "eurozone countries", "across eu", "european union countries",
        ]
        if any(term in query_lower for term in eu_group_terms):
            return self._create_decision(
                provider="Eurostat",
                confidence=0.80,
                match_type="region",
                matched_pattern="EU country group",
                reasoning="Query about EU/European countries routed to Eurostat",
            )

        # OECD for OECD group queries
        oecd_group_terms = [
            "oecd countries", "oecd members", "oecd area", "oecd nations",
            "across oecd", "all oecd countries", "oecd member countries",
            "g7 countries", "g7 nations",
        ]
        if any(term in query_lower for term in oecd_group_terms):
            return self._create_decision(
                provider="OECD",
                confidence=0.80,
                match_type="region",
                matched_pattern="OECD country group",
                reasoning="Query about OECD countries/members routed to OECD",
            )

        # WorldBank for developing/emerging/regional group queries
        wb_group_terms = [
            "developing countries", "emerging markets", "emerging economies",
            "low-income countries", "middle-income countries",
            "asian countries", "latin american countries", "african countries",
            "south america", "sub-saharan africa", "g20 countries",
        ]
        if any(term in query_lower for term in wb_group_terms):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.80,
                match_type="region",
                matched_pattern="development/regional group",
                reasoning="Query about developing/regional country group routed to WorldBank",
            )

        # StatsCan for Canadian provincial queries
        statscan_group_terms = [
            "all provinces", "canadian provinces", "each province",
            "by province", "provincial data",
        ]
        if any(term in query_lower for term in statscan_group_terms):
            return self._create_decision(
                provider="StatsCan",
                confidence=0.80,
                match_type="region",
                matched_pattern="Canadian provinces",
                reasoning="Query about Canadian provinces routed to StatsCan",
            )

        return None

    def _has_bilateral_trade_partner(self, query: str) -> bool:
        """
        Detect explicit bilateral trade phrasing.

        Avoids false positives from date ranges such as "from 2020 to 2024".
        """
        query_lower = query.lower()

        if any(term in query_lower for term in ["bilateral", "trading partner", "trade partner"]):
            return True

        # "between X and Y" usually indicates bilateral trade relationship.
        if re.search(r"\bbetween\b.+\band\b", query_lower):
            return True

        # Require a trade verb near to/from/with instead of matching any "to"/"from".
        if re.search(r"\b(exports?|imports?|trade(?:\s+flow)?|trading)\s+(to|from|with)\b", query_lower):
            return True

        # Multiple explicit countries in a trade query usually implies bilateral context.
        if any(term in query_lower for term in ["export", "import", "trade"]):
            mentioned = CountryResolver.detect_all_countries_in_query(query)
            if len(mentioned) >= 2:
                return True

        return False

    def _route_by_country(
        self,
        country: Optional[str],
        countries: Optional[List[str]],
        query_lower: str,
        indicators: List[str],
    ) -> Optional[RoutingDecision]:
        """Route based on country membership."""
        if not country:
            return None

        # US queries
        if CountryResolver.is_us(country):
            # Check for IMF-specific indicators
            indicators_str = " ".join(indicators).lower()
            is_imf_indicator = any(term in indicators_str for term in [
                "debt", "fiscal", "deficit", "current account"
            ])
            if is_imf_indicator:
                return self._create_decision(
                    provider="IMF",
                    confidence=0.80,
                    match_type="indicator",
                    reasoning=f"US query with IMF-specific indicator",
                )
            return self._create_decision(
                provider="FRED",
                confidence=0.80,
                match_type="country",
                matched_pattern="United States",
                reasoning="US query routed to FRED",
            )

        # Non-OECD major economies → WorldBank or IMF
        if CountryResolver.is_non_oecd_major(country):
            indicators_str = " ".join(indicators).lower()
            is_imf_indicator = any(term in indicators_str for term in [
                "debt", "fiscal", "deficit", "inflation", "unemployment", "current account"
            ])
            if is_imf_indicator:
                return self._create_decision(
                    provider="IMF",
                    confidence=0.80,
                    match_type="indicator",
                    reasoning=f"Non-OECD country ({country}) with IMF indicator → IMF",
                )
            return self._create_decision(
                provider="WorldBank",
                confidence=0.75,
                match_type="country",
                matched_pattern=country,
                reasoning=f"Non-OECD major economy ({country}) → WorldBank",
            )

        # EU members → Eurostat (with WorldBank fallback)
        if CountryResolver.is_eu_member(country):
            return self._create_decision(
                provider="Eurostat",
                confidence=0.75,
                match_type="country",
                matched_pattern=country,
                reasoning=f"EU member ({country}) → Eurostat",
            )

        # OECD non-EU → prefer WorldBank for standard macro indicators
        # (unemployment, GDP, inflation, etc.) since OECD resolution is
        # unreliable.  Only route to OECD for OECD-specific indicators
        # that WorldBank doesn't cover well.
        if CountryResolver.is_oecd_non_eu(country):
            indicators_str = " ".join(indicators).lower()
            # OECD-specific indicators (not well covered by WorldBank)
            oecd_specific_terms = [
                "r&d", "tax revenue", "gini", "income inequality",
                "social spending", "pension", "education spending",
                "health expenditure",
            ]
            has_oecd_specific = any(
                term in indicators_str or term in query_lower
                for term in oecd_specific_terms
            )
            if has_oecd_specific:
                return self._create_decision(
                    provider="OECD",
                    confidence=0.75,
                    match_type="indicator",
                    reasoning=f"OECD non-EU country ({country}) with OECD-specific indicator",
                )
            # Standard macro indicators → WorldBank (broader, more reliable)
            standard_macro_terms = [
                "unemployment", "employment", "labor force", "productivity",
                "gdp", "inflation", "population", "trade",
            ]
            has_standard_macro = any(
                term in indicators_str or term in query_lower
                for term in standard_macro_terms
            )
            if has_standard_macro:
                return self._create_decision(
                    provider="WorldBank",
                    confidence=0.80,
                    match_type="indicator",
                    reasoning=f"OECD non-EU country ({country}) with standard macro indicator → WorldBank (broader coverage)",
                )
            # Fall through to default

        return None

    def _route_by_catalog(
        self,
        indicators: List[str],
        country: Optional[str],
        query: Optional[str] = None,
    ) -> Optional[RoutingDecision]:
        """Route using CatalogService YAML mappings.

        Checks both parsed indicators AND the raw query text against
        catalog concepts. Returns a special 'not_available' decision
        when all providers are marked as unavailable for a concept.
        """
        if not self._catalog_service:
            return None

        # Build candidate terms: parsed indicators + raw query
        terms_to_check = list(indicators) if indicators else []
        if query:
            query_clean = query.strip()
            if query_clean and query_clean not in terms_to_check:
                terms_to_check.append(query_clean)

        if not terms_to_check:
            return None

        # Try to find a matching concept in catalog
        for term in terms_to_check:
            concept_name = self._catalog_service.find_concept_by_term(term)
            if concept_name:
                # Normalize country to list format for catalog
                countries_list = [country] if country else None

                provider, code, confidence = self._catalog_service.get_best_provider(
                    concept_name,
                    countries=countries_list,
                )
                if provider and confidence > 0.5:
                    logger.info(f"📚 Catalog match: {term} → {concept_name} → {provider}")
                    return self._create_decision(
                        provider=provider,
                        confidence=confidence,
                        match_type="catalog",
                        matched_pattern=f"catalog:{concept_name}",
                        reasoning=f"Catalog lookup: {concept_name} → {provider} (code: {code})",
                    )

                # Check if concept exists but ALL providers are unavailable
                concept = self._catalog_service.get_concept(concept_name)
                if concept:
                    providers_map = concept.get("providers", {})
                    not_available = concept.get("not_available", [])
                    has_any_provider = bool(providers_map) and any(
                        p.lower() not in [na.lower() for na in not_available]
                        for p in providers_map
                    )
                    if not has_any_provider:
                        desc = concept.get("description", "").strip()
                        logger.info(
                            f"📚 Catalog concept '{concept_name}' has no available providers"
                        )
                        return self._create_decision(
                            provider="not_available",
                            confidence=0.99,
                            match_type="catalog",
                            matched_pattern=f"catalog:{concept_name}:not_available",
                            reasoning=f"Not available: {desc}" if desc else
                            f"'{concept_name}' is not available through any provider",
                        )

        return None


# ==========================================================================
# Compatibility Layer
# ==========================================================================

def route_provider(intent: Any, original_query: str) -> str:
    """
    Compatibility function matching ProviderRouter.route_provider() signature.

    Args:
        intent: ParsedIntent object
        original_query: Original user query

    Returns:
        Provider name string
    """
    router = UnifiedRouter()
    decision = router.route_with_intent(intent, original_query)
    return decision.provider


def detect_explicit_provider(query: str) -> Optional[str]:
    """
    Compatibility function matching ProviderRouter.detect_explicit_provider() signature.

    Args:
        query: User's natural language query

    Returns:
        Provider name if explicitly mentioned, None otherwise
    """
    result = detect_explicit_provider_match(query)
    return result[0] if result else None


def correct_coingecko_misrouting(provider: str, query: str, indicators: list) -> str:
    """
    Compatibility function matching ProviderRouter.correct_coingecko_misrouting() signature.

    Returns just the corrected provider string (discards the reason).

    Args:
        provider: Selected provider
        query: Original query
        indicators: List of indicators

    Returns:
        Corrected provider name string
    """
    corrected, _reason = _correct_coingecko(provider, query, indicators)
    return corrected


def validate_routing(provider: str, original_query: str, intent: Any) -> Optional[str]:
    """
    Post-routing validation to catch incorrect routing decisions.

    Migrated from ProviderRouter.validate_routing(). Checks for obvious mismatches
    between query content and selected provider.

    Args:
        provider: Selected provider
        original_query: Original user query
        intent: ParsedIntent object

    Returns:
        Warning message if routing seems incorrect, None if OK
    """
    query_lower = original_query.lower()

    # Check 1: European/EU query but not routed to Eurostat
    if any(keyword in query_lower for keyword in ["european countries", "eu countries", "eu member"]):
        if provider.upper() not in ["EUROSTAT", "OECD"]:
            warning = f"Query mentions European countries but routed to {provider}, not Eurostat"
            logger.warning(warning)
            return warning

    # Check 2: OECD query but not routed to OECD
    if any(keyword in query_lower for keyword in ["oecd countries", "oecd members"]):
        if provider.upper() != "OECD":
            warning = f"Query mentions OECD countries but routed to {provider}, not OECD"
            logger.warning(warning)
            return warning

    # Check 3: FRED selected for non-US multi-country query
    if provider.upper() == "FRED":
        parameters = getattr(intent, "parameters", {}) or {}
        countries = parameters.get("countries", [])
        country = parameters.get("country", "")
        if isinstance(countries, list) and len(countries) > 1:
            if not all(c.upper() in ["US", "USA", "UNITED STATES"] for c in countries):
                warning = f"FRED selected for multi-country query ({countries}) - should use WorldBank"
                logger.warning(warning)
                return warning
        elif country and country.upper() not in ["US", "USA", "UNITED STATES"]:
            warning = f"FRED selected for non-US query (country={country}) - should use WorldBank or OECD"
            logger.warning(warning)
            return warning

    # Check 4: Developing countries query not routed to WorldBank
    if any(keyword in query_lower for keyword in ["developing countries", "emerging markets"]):
        if provider.upper() not in ["WORLDBANK", "IMF"]:
            warning = f"Query about developing countries routed to {provider}, not WorldBank"
            logger.warning(warning)
            return warning

    return None
