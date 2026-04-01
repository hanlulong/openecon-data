"""
Keyword Matcher - Pattern Detection for Provider Routing

Consolidates keyword patterns from:
- provider_router.py (PROVIDER_KEYWORDS, PROVIDER_KEYWORDS_PRIORITY, US_ONLY_INDICATORS)
- deep_agent_orchestrator.py (PROVIDER_CAPABILITIES specialties)

This module provides:
1. Explicit provider detection ("from OECD", "using IMF")
2. US-only indicator detection
3. Indicator-based provider hints
4. Regional keyword detection
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result of keyword matching."""
    provider: Optional[str]
    confidence: float
    matched_keyword: Optional[str]
    match_type: str  # "explicit", "indicator", "region", "query_type"
    reasoning: str


class KeywordMatcher:
    """
    Detects providers based on keyword patterns in queries.

    Priority order:
    1. Explicit provider mentions ("from OECD", "using IMF")
    2. US-only indicators (must use FRED)
    3. Indicator-specific patterns (trade → Comtrade, crypto → CoinGecko)
    4. Regional patterns (OECD countries → WorldBank/OECD)
    """

    # ==========================================================================
    # Explicit Provider Keywords
    # ==========================================================================

    # Phrases that explicitly request a specific provider
    EXPLICIT_PROVIDER_KEYWORDS: Dict[str, List[str]] = {
        "OECD": ["from oecd", "using oecd", "via oecd", "according to oecd", "oecd data"],
        "FRED": ["fred", "from fred", "using fred", "via fred", "federal reserve", "st. louis fed", "stlouisfed", "the fed"],
        "WorldBank": ["world bank", "worldbank", "from world bank", "using world bank", "wb data", "world bank data"],
        "Comtrade": ["comtrade", "un comtrade", "from comtrade", "using comtrade", "united nations comtrade"],
        "StatsCan": ["statscan", "statistics canada", "stats canada", "from statscan", "using statscan"],
        "IMF": ["from imf", "using imf", "international monetary fund", "from the imf", "according to the imf", "imf data"],
        "BIS": ["from bis", "using bis", "bank for international settlements", "bis data"],
        "Eurostat": ["from eurostat", "using eurostat", "via eurostat", "according to eurostat", "eu statistics", "european statistics", "eurostat data"],
        "ExchangeRate": ["exchangerate", "exchange rate api", "from exchangerate"],
        "CoinGecko": ["coingecko", "coin gecko", "from coingecko", "using coingecko", "crypto prices"]
    }

    # Providers that can appear at start of query (e.g., "OECD GDP for Italy")
    # Excludes patterns like "OECD countries" which should route to WorldBank
    START_OF_QUERY_PROVIDERS = ["OECD", "IMF", "BIS", "Eurostat"]
    START_OF_QUERY_EXCLUSIONS = ["countries", "country", "members", "member", "nations", "nation", "average"]

    # ==========================================================================
    # US-Only Indicators (MUST use FRED)
    # ==========================================================================

    US_ONLY_INDICATORS: Set[str] = {
        "case-shiller", "case shiller",
        "federal funds", "fed funds",
        "pce", "personal consumption",
        "nonfarm payrolls",
        "initial claims", "unemployment claims",
        "michigan consumer", "consumer sentiment",
        "s&p 500", "sp500", "s&p",
        "dow jones", "djia",
        "prime lending rate",
        "mortgage rate", "30-year mortgage"
    }

    # ==========================================================================
    # Indicator-Specific Keywords (Priority Order)
    # ==========================================================================

    # Order matters! More specific patterns should come first.
    # Trade keywords MUST come before country keywords.
    INDICATOR_KEYWORDS: Dict[str, List[str]] = {
        # Trade-specific → COMTRADE
        "Comtrade": [
            "exports to", "imports from", "trade with",
            "trade flow", "bilateral trade", "trade deficit", "trade surplus",
            "top importers", "top exporters", "importers of", "exporters of",
            "electric vehicle export", "machinery export", "textile import",
            "coffee export", "oil export", "crude oil export",
            "iron ore export", "semiconductor export", "pharmaceutical export",
            "wine export", "automobile export", "flower export",
            "agricultural import", "agricultural export",
            "fashion export", "textile export",
            "exports of", "imports of", "export of", "import of",
        ],

        # US-specific + Commodity Prices → FRED (must come before OECD)
        # NOTE: Keep FRED commodity matching focused on PPI-style indicators.
        # Broad "commodity prices" matching is handled by IMF patterns below.
        # NOTE: Country-indicator combos like "us gdp", "us inflation" etc.
        # are now handled by catalog (Priority 3.5) + country routing and
        # were removed in Cycle 6 cleanup.
        "FRED": [
            "us housing", "us retail", "us industrial", "us consumer",
            "federal funds", "fed rate", "treasury yield", "treasury rate",
            # NOTE: "bond yield", "government bond yield", "10-year yield" etc.
            # removed in Cycle 7 — catalog bond_yield concept handles these at
            # Priority 3.5. "treasury" kept as US-specific term.
            "yield spread", "yield curve",
            "m1 money supply", "m2 money supply", "money stock", "us money supply",
            "s&p 500", "s&p500", "dow jones", "nasdaq",
            # Commodity-related PPI indices
            "producer price index", "ppi commodities", "ppi all commodities",
            "metal price index", "base metal", "base metals",
            "agricultural price", "agricultural commodity",
            "food price index", "energy price index",
            # Commodity spot prices (FRED has daily/monthly commodity price series)
            "gold price", "silver price", "oil price", "crude oil",
            "wti", "brent", "natural gas price", "copper price",
            "platinum price", "palladium price",
            # Labor market (FRED has key employment/claims series)
            "nonfarm payrolls", "payroll employment", "initial claims",
            "jobless claims", "unemployment claims", "continuing claims",
            "average hourly earnings", "us wages",
            # Housing (FRED has housing starts, home sales, case-shiller)
            "case-shiller", "case shiller", "home sales",
            "existing home sales", "new home sales",
        ],

        # European countries → EUROSTAT (must come before OECD)
        # NOTE: Country-indicator combos like "france gdp", "germany unemployment"
        # etc. are now handled by Eurostat EU-country routing (Priority 3j) +
        # catalog (Priority 3.5) + country-based routing (Priority 6) and were
        # removed in Cycle 6 cleanup. "in <country>" patterns also removed as
        # CountryResolver detects country from query text.
        "Eurostat": [
            "eu ", "european union", "eurozone", "euro area",
            "eu member states", "european countries",
            # Eurostat-specific indicators
            "harmonized index", "hicp", "harmonized consumer price",
            "purchasing power standards", "pps",
            "railway freight", "freight transport by rail",
        ],

        # Canadian indicators → STATSCAN
        # NOTE: Country-indicator combos like "canada gdp", "canadian unemployment"
        # etc. are now handled by Canadian query handler (Priority 3g) + catalog
        # (Priority 3.5) and were removed in Cycle 6 cleanup.
        "StatsCan": [
            "canada housing", "canada retail",
            "canadian housing", "canadian retail",
            "building permit", "residential construction", "commercial construction",
            "cpi breakdown", "cpi component", "price index breakdown",
        ],

        # Fiscal/financial → IMF
        # Includes macro + broad commodity indices
        "IMF": [
            "current account balance", "current account deficit", "current account surplus",
            "current account", "balance of payments",
            "inflation forecast", "economic forecast",
            "fiscal deficit", "government debt",
            "inflation outlook", "global inflation", "world inflation",
            "government deficit", "budget deficit", "fiscal balance",
            "government surplus", "budget surplus", "budget balance",
            "public debt", "sovereign debt", "national debt",
            "government spending", "public spending", "fiscal policy",
            "government balance", "deficit to gdp", "debt to gdp",
            "commodity price index", "commodity prices index",
            "primary commodity", "primary commodity prices", "commodity prices",
        ],

        # Housing/property/Banking → BIS
        # NOTE: Many BIS patterns were removed in Cycle 7 cleanup.
        # Catalog concepts (house_prices, credit, credit_to_gdp_gap,
        # household_debt, corporate_debt, debt_service_ratio,
        # effective_exchange_rate, interest_rate) handle most BIS routing
        # at Priority 3.5. Only patterns NOT in catalog are kept here.
        "BIS": [
            # Housing/Property (not in catalog)
            "house price to income", "property valuation",
            "housing valuation", "real estate valuation",
            "property market", "housing market valuation",
            # Credit (not in catalog)
            "private non-financial",
            # Household debt ratios (catalog misroutes to gross_national_income)
            "household debt to income",
            "household debt to disposable income",
            # Exchange rates (not in catalog or catalog misroutes)
            "exchange rate index",
            # Global liquidity (catalog misroutes to money_supply → WorldBank)
            "global liquidity", "liquidity indicator",
            "international debt securities",
            # Policy rates (not in catalog)
            "repo rate", "cash rate",
            # Banking sector indicators (not in catalog)
            "banking sector", "banking system",
            "financial stability", "banking stability",
            "bank deposits", "banking deposits",
            "lending spread",
            "deposit rates",
        ],

        # Development indicators → WorldBank
        "WorldBank": [
            "life expectancy", "infant mortality", "maternal mortality",
            "poverty", "poverty rate", "poverty headcount", "extreme poverty",
            "access to electricity", "access to clean water", "access to sanitation",
            "school enrollment", "literacy rate", "primary enrollment",
            "gini index", "income share", "income inequality",
            "forest area", "co2 emissions", "renewable energy",
            "agricultural land", "arable land",
            "mobile subscriptions", "internet users",
            "fertility rate", "birth rate", "death rate",
            "broad money", "money supply growth", "monetary aggregate growth",
        ],

        # OECD-specific indicators (regional phrases moved to REGIONAL_KEYWORDS)
        # NOTE: Country-indicator combos like "japan gdp", "korea unemployment"
        # etc. are now handled by catalog (Priority 3.5) + country-based routing
        # (Priority 6, OECD non-EU → WorldBank) and were removed in Cycle 6
        # cleanup.
        "OECD": [
            # OECD-specific statistics (not available elsewhere)
            "oecd average",  # Comparing to OECD average
            "tax revenue", "tax revenue to gdp", "tax receipts", "taxation",
            "tax as percent of gdp", "tax % of gdp", "tax to gdp",
            "revenue statistics", "tax statistics",
            "tax wedge", "labor income tax", "taxation of labor",
            "r&d spending", "r&d expenditure", "research and development spending",
            "productivity growth", "labor productivity", "productivity comparison",
            "pension spending", "pension expenditure",
            "environmental tax", "carbon tax",
        ],

        # Currency/exchange → ExchangeRate
        # NOTE: Most exchange rate patterns were removed in Cycle 7 cleanup.
        # unified_router._is_exchange_rate_query() handles "exchange rate",
        # "forex", "usd to", "eur to", etc. at Priority 3b (before keyword
        # matching). catalog exchange_rate concept covers "currency exchange",
        # "fx rate" at Priority 3.5. Only keep patterns not covered elsewhere.
        "ExchangeRate": [
            "usd strength", "currency strength index",
            "dollar strength", "currency index",
        ],

        # Crypto → CoinGecko
        "CoinGecko": [
            "stablecoin", "defi", "decentralized finance",
            "cryptocurrency trading volume", "crypto trading volume",
            "nft", "blockchain", "altcoin", "crypto market cap",
            "bitcoin", "btc", "ethereum", "eth", "solana", "cardano", "dogecoin",
            "xrp", "ripple", "litecoin", "ltc", "binance coin", "bnb",
            "market cap", "trading volume",
        ],
    }

    # ==========================================================================
    # Regional Keywords
    # ==========================================================================

    REGIONAL_KEYWORDS: Dict[str, List[str]] = {
        "Eurostat": [
            "european countries", "eu countries", "eu member states", "eurozone countries",
            "across eu", "in europe", "european union countries", "eu region",
        ],
        "OECD": [
            "oecd countries", "oecd members", "oecd area", "oecd nations",
            "across oecd", "all oecd countries", "oecd member countries",
            "g7 countries", "g7 nations",  # All G7 members are in OECD
        ],
        "WorldBank": [
            "developing countries", "emerging markets", "emerging economies",
            "low-income countries", "middle-income countries",
            "asian countries", "latin american countries", "african countries",
            "south america", "sub-saharan africa",
            "g20 countries",  # G20 includes non-OECD countries like China, Brazil
        ],
        "StatsCan": [
            "all provinces", "canadian provinces", "each province",
            "by province", "provincial data",
        ],
    }

    # NOTE: QUERY_TYPE_PATTERNS and detect_query_type() were removed in
    # Cycle 7 cleanup — they were dead code (never called from outside
    # this module). Query type classification is handled by
    # unified_router's special-case handlers (3a: crypto, 3b: exchange
    # rate, 3c-3j: trade/fiscal) and catalog concepts at Priority 3.5.

    # ==========================================================================
    # Anti-Patterns (prevent misrouting)
    # ==========================================================================

    # Fiscal keywords that should NEVER go to CoinGecko
    NON_CRYPTO_FISCAL_KEYWORDS: Set[str] = {
        "government", "deficit", "surplus", "fiscal", "budget",
        "debt", "gdp", "unemployment", "inflation", "trade",
        "export", "import", "tax", "spending", "economic"
    }

    # Keywords that MUST go to CoinGecko
    CRYPTO_KEYWORDS: Set[str] = {
        "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
        "solana", "cardano", "dogecoin", "altcoin", "defi", "nft",
        "blockchain", "stablecoin", "coin", "token", "xrp", "ripple",
        "litecoin", "ltc", "bnb"
    }

    # ==========================================================================
    # Public Methods
    # ==========================================================================

    @classmethod
    def detect_explicit_provider(cls, query: str) -> Optional[MatchResult]:
        """
        Detect if user explicitly mentions a data provider.

        Args:
            query: User's natural language query

        Returns:
            MatchResult if provider explicitly mentioned, None otherwise
        """
        query_lower = query.lower()

        # Check for provider at start of query (e.g., "OECD GDP for Italy")
        for provider in cls.START_OF_QUERY_PROVIDERS:
            provider_lower = provider.lower()
            if query_lower.startswith(provider_lower + " "):
                # Exclude patterns like "OECD countries"
                if not any(term in query_lower[:30] for term in cls.START_OF_QUERY_EXCLUSIONS):
                    logger.info(f"🎯 Explicit provider at start: {provider}")
                    return MatchResult(
                        provider=provider,
                        confidence=1.0,
                        matched_keyword=f"{provider} (at start)",
                        match_type="explicit",
                        reasoning=f"Query starts with '{provider}' indicating explicit provider request"
                    )

        # Check for explicit keyword mentions
        for provider, keywords in cls.EXPLICIT_PROVIDER_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    logger.info(f"🎯 Explicit provider keyword: {keyword} → {provider}")
                    return MatchResult(
                        provider=provider,
                        confidence=1.0,
                        matched_keyword=keyword,
                        match_type="explicit",
                        reasoning=f"Explicit mention of '{keyword}' requests {provider}"
                    )

        return None

    @classmethod
    def detect_us_only_indicator(
        cls,
        query: str,
        indicators: List[str],
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
    ) -> Optional[MatchResult]:
        """
        Check if query contains US-only indicators (must use FRED).

        Skips the check when non-US countries are explicitly requested,
        so queries like "consumer sentiment in Germany" fall through to
        the correct provider (Eurostat/WorldBank) instead of FRED.

        Args:
            query: User's query
            indicators: List of parsed indicators
            country: Single country from parsed intent
            countries: Multiple countries from parsed intent

        Returns:
            MatchResult if US-only indicator found and no non-US country context
        """
        # If any non-US country is explicitly requested, skip US-only check.
        # This lets "consumer sentiment in Germany" route to Eurostat, not FRED.
        all_countries = countries or ([country] if country else [])
        if all_countries:
            non_us = [c for c in all_countries
                      if c.upper() not in ("US", "USA", "UNITED STATES", "UNITED_STATES")]
            if non_us:
                return None

        query_lower = query.lower()
        indicators_str = " ".join(indicators).lower() if indicators else ""
        combined = f"{query_lower} {indicators_str}"

        for indicator in cls.US_ONLY_INDICATORS:
            if indicator in combined:
                logger.info(f"🇺🇸 US-only indicator: {indicator} → FRED")
                return MatchResult(
                    provider="FRED",
                    confidence=0.95,
                    matched_keyword=indicator,
                    match_type="indicator",
                    reasoning=f"'{indicator}' is a US-only indicator that requires FRED"
                )

        return None

    # Keywords that need word boundary matching to avoid false positives
    # e.g., "defi" should not match "deficit", "eth" should not match "method"
    WORD_BOUNDARY_KEYWORDS: Set[str] = {
        "defi", "eth", "btc", "nft", "coin", "token",
    }

    @classmethod
    def _keyword_matches(cls, keyword: str, text: str) -> bool:
        """
        Check if keyword matches in text, using word boundaries for problematic keywords.

        Args:
            keyword: The keyword to search for
            text: The text to search in

        Returns:
            True if keyword matches (with appropriate boundary rules)
        """
        if keyword in cls.WORD_BOUNDARY_KEYWORDS:
            # Use regex word boundary for problematic short keywords
            pattern = rf'\b{re.escape(keyword)}\b'
            return bool(re.search(pattern, text))
        else:
            # Standard substring match for most keywords
            return keyword in text

    @classmethod
    def detect_indicator_provider(cls, query: str, indicators: List[str]) -> Optional[MatchResult]:
        """
        Detect provider based on indicator-specific keywords.

        Args:
            query: User's query
            indicators: List of parsed indicators

        Returns:
            MatchResult if indicator pattern matched
        """
        query_lower = query.lower()
        indicators_str = " ".join(indicators).lower() if indicators else ""
        combined = f"{query_lower} {indicators_str}"

        # Score all matched keywords and pick the most specific match instead of
        # relying on provider iteration order.
        best_provider: Optional[str] = None
        best_keyword: Optional[str] = None
        best_score: float = 0.0

        for provider, keywords in cls.INDICATOR_KEYWORDS.items():
            for keyword in keywords:
                if cls._keyword_matches(keyword, combined):
                    # Specificity score: prioritize longer/more informative keywords.
                    token_count = len(re.findall(r"[a-z0-9]+", keyword))
                    char_count = len(keyword)
                    score = (token_count * 10.0) + min(char_count, 60) / 10.0

                    # Small boost if phrase appears directly in original query text.
                    if keyword in query_lower:
                        score += 2.0

                    if score > best_score:
                        best_score = score
                        best_provider = provider
                        best_keyword = keyword

        if best_provider and best_keyword:
            confidence = min(0.95, 0.6 + (best_score / 100.0))
            logger.info(f"🎯 Indicator keyword: {best_keyword} → {best_provider}")
            return MatchResult(
                provider=best_provider,
                confidence=confidence,
                matched_keyword=best_keyword,
                match_type="indicator",
                reasoning=f"Keyword '{best_keyword}' indicates {best_provider} as best source"
            )

        return None

    @classmethod
    def detect_regional_provider(cls, query: str) -> Optional[MatchResult]:
        """
        Detect provider based on regional group mentions.

        Args:
            query: User's query

        Returns:
            MatchResult if regional pattern matched
        """
        query_lower = query.lower()

        for provider, keywords in cls.REGIONAL_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    logger.info(f"🌍 Regional keyword: {keyword} → {provider}")
                    return MatchResult(
                        provider=provider,
                        confidence=0.80,
                        matched_keyword=keyword,
                        match_type="region",
                        reasoning=f"Query about '{keyword}' routed to {provider}"
                    )

        return None

    @classmethod
    def correct_coingecko_misrouting(
        cls,
        provider: str,
        query: str,
        indicators: List[str]
    ) -> Tuple[str, Optional[str]]:
        """
        Correct cases where CoinGecko is incorrectly selected for non-crypto queries.

        Args:
            provider: Selected provider
            query: Original query
            indicators: List of indicators

        Returns:
            Tuple of (corrected_provider, correction_reason)
        """
        if provider.upper() != "COINGECKO":
            return provider, None

        query_lower = query.lower()
        indicators_str = " ".join(indicators).lower() if indicators else ""
        combined = f" {query_lower} {indicators_str} "  # Add spaces for word boundary matching

        # Check for crypto keywords using word boundaries
        # Note: "defi" should NOT match "deficit" - use word boundary matching
        def has_word(text: str, word: str) -> bool:
            """Check if word appears as a complete word in text."""
            return f" {word} " in text or text.startswith(f"{word} ") or text.endswith(f" {word}")

        has_crypto = any(has_word(combined, kw) for kw in cls.CRYPTO_KEYWORDS)

        # Check for fiscal keywords (also with word boundaries)
        has_fiscal = any(has_word(combined, kw) for kw in cls.NON_CRYPTO_FISCAL_KEYWORDS)

        # If fiscal but no crypto, this is misrouted
        if has_fiscal and not has_crypto:
            reason = f"CoinGecko corrected to IMF: query has fiscal keywords but no crypto"
            logger.warning(f"🚨 {reason}")
            return "IMF", reason

        return provider, None

    @classmethod
    def match(cls, query: str, indicators: Optional[List[str]] = None) -> Optional[MatchResult]:
        """
        Main matching method - returns best provider match.

        Priority:
        1. Explicit provider mention
        2. US-only indicator
        3. Indicator-specific keywords
        4. Regional keywords

        Args:
            query: User's query
            indicators: Optional list of parsed indicators

        Returns:
            Best MatchResult or None
        """
        indicators = indicators or []

        # Priority 1: Explicit provider
        result = cls.detect_explicit_provider(query)
        if result:
            return result

        # Priority 2: US-only indicator
        result = cls.detect_us_only_indicator(query, indicators)
        if result:
            return result

        # Priority 3: Indicator keywords
        result = cls.detect_indicator_provider(query, indicators)
        if result:
            return result

        # Priority 4: Regional keywords
        result = cls.detect_regional_provider(query)
        if result:
            return result

        return None
