"""
Keyword Matcher - Pattern Detection for Provider Routing

Consolidates keyword patterns from:
- provider_router.py (PROVIDER_KEYWORDS, PROVIDER_KEYWORDS_PRIORITY, US_ONLY_INDICATORS)
- deep_agent_orchestrator.py (PROVIDER_CAPABILITIES specialties)

This module provides:
1. Explicit provider detection ("from OECD", "using IMF")
2. Query type classification (trade, currency, crypto, economic)
3. US-only indicator detection
4. Indicator-based provider hints
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
        "FRED": ["fred", "from fred", "federal reserve", "st. louis fed", "the fed"],
        "WorldBank": ["world bank", "worldbank", "from world bank", "wb data"],
        "Comtrade": ["comtrade", "un comtrade", "from comtrade", "united nations comtrade"],
        "StatsCan": ["statscan", "statistics canada", "stats canada", "from statscan"],
        "IMF": ["from imf", "using imf", "international monetary fund", "from the imf", "imf data"],
        "BIS": ["from bis", "using bis", "bank for international settlements", "bis data"],
        "Eurostat": ["from eurostat", "eu statistics", "european statistics"],
        "ExchangeRate": ["exchangerate", "exchange rate api"],
        "CoinGecko": ["coingecko", "coin gecko", "crypto prices"]
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
            "top importers", "top exporters",
            "electric vehicle export", "machinery export", "textile import",
            "coffee export", "oil export", "crude oil export",
            "iron ore export", "semiconductor export", "pharmaceutical export",
            "wine export", "automobile export", "flower export",
            "agricultural import", "agricultural export",
            "fashion export", "textile export",
            "exports of", "imports of", "export of", "import of",
        ],

        # US-specific + Commodity Prices → FRED (must come before OECD)
        # NOTE: FRED has Producer Price Index for commodities (PPIACO) but NOT gold/silver spot prices
        # Gold/silver spot prices are not available from any of our providers
        "FRED": [
            "us gdp", "us unemployment", "us inflation", "us cpi",
            "us housing", "us retail", "us industrial", "us consumer",
            "u.s. gdp", "u.s. unemployment", "u.s. inflation", "u.s. cpi",
            "united states gdp", "united states unemployment",
            "american gdp", "american unemployment", "american inflation",
            "federal funds", "fed rate", "treasury yield", "treasury rate",
            "s&p 500", "s&p500", "dow jones", "nasdaq",
            "us trade balance", "u.s. trade balance", "us trade deficit",
            "u.s. trade deficit", "united states trade balance",
            # Commodity price indices - FRED has PPI data (not spot prices)
            "commodity price", "commodity prices", "commodity index",
            "producer price index", "ppi commodities", "ppi all commodities",
            "metal price index", "base metal", "base metals",
            "agricultural price", "agricultural commodity",
            "food price index", "energy price index",
        ],

        # European countries → EUROSTAT (must come before OECD)
        "Eurostat": [
            "eu ", "european union", "eurozone", "euro area",
            "eu member states", "european countries",
            # Major EU countries with common indicators
            "france gdp", "france unemployment", "france inflation", "france cpi",
            "germany gdp", "germany unemployment", "germany inflation", "germany cpi",
            "italy gdp", "italy unemployment", "italy inflation", "italy cpi",
            "spain gdp", "spain unemployment", "spain inflation", "spain cpi",
            "netherlands gdp", "netherlands unemployment",
            "belgium gdp", "belgium unemployment",
            "austria gdp", "austria unemployment",
            "portugal gdp", "portugal unemployment",
            "greece gdp", "greece unemployment",
            "ireland gdp", "ireland unemployment",
            "poland gdp", "poland unemployment",
            "sweden gdp", "sweden unemployment",
            # Eurostat-specific indicators
            "harmonized index", "hicp", "harmonized consumer price",
            "purchasing power standards", "pps",
            "railway freight", "freight transport by rail",
            # Country mentions (with leading space)
            "in france", "in germany", "in italy", "in spain",
            "in netherlands", "in belgium", "in austria",
        ],

        # Canadian indicators → STATSCAN
        "StatsCan": [
            "canada gdp", "canada unemployment", "canada inflation", "canada cpi",
            "canada population", "canada housing", "canada retail", "canada trade",
            "canadian gdp", "canadian unemployment", "canadian inflation", "canadian cpi",
            "canadian population", "canadian housing", "canadian retail",
            "building permit", "residential construction", "commercial construction",
            "cpi breakdown", "cpi component", "price index breakdown",
        ],

        # Fiscal/financial → IMF
        # NOTE: IMF DataMapper API does NOT have commodity prices (those are in PCPS which is inaccessible)
        # Commodity queries are routed to FRED for Producer Price Indices as fallback
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
        ],

        # Housing/property/Banking → BIS
        # INFRASTRUCTURE FIX: Expanded banking keyword coverage
        "BIS": [
            # Housing/Property
            "house price to income", "property valuation",
            "housing valuation", "real estate valuation",
            "property market", "housing market valuation",
            "residential property price", "property price index",
            "commercial property price", "house price index",
            # Credit to private sector
            "credit to non-financial", "credit to gdp", "credit gap",
            "credit-to-gdp", "credit to gdp gap", "credit-to-gdp gap",
            "credit to private", "private non-financial", "non-financial sector",
            "total credit", "credit to private non-financial",
            "bank credit", "credit growth", "banking credit",
            "private sector credit", "credit to households",
            "credit to corporations", "corporate credit",
            # Household debt variants
            "household debt", "household credit",
            "household debt to gdp", "household debt to income",
            "household debt to disposable income",
            "private household debt",
            # Debt service
            "debt service ratio", "debt service",
            # Exchange rates
            "effective exchange rate", "exchange rate index",
            "real effective exchange rate", "nominal effective exchange rate",
            # Global liquidity
            "global liquidity", "liquidity indicator",
            "international debt securities",
            # Policy rates
            "policy rate", "central bank policy rate",
            "repo rate", "cash rate", "base rate",
            # Banking sector indicators
            "banking sector", "banking system",
            "financial stability", "banking stability",
            "bank deposits", "banking deposits",
            "interest rate spread", "lending spread",
            "deposit rates", "lending rates",
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
        ],

        # OECD-specific indicators (regional phrases moved to REGIONAL_KEYWORDS)
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
            # OECD non-EU country indicators (Japan, Korea, Australia, etc.)
            "japan gdp", "japan unemployment", "japan inflation", "japan cpi",
            "korea gdp", "korea unemployment", "korea inflation", "korea cpi",
            "south korea gdp", "south korea unemployment",
            "australia gdp", "australia unemployment", "australia inflation",
            "new zealand gdp", "new zealand unemployment",
            "mexico gdp", "mexico unemployment", "mexico inflation",
            "switzerland gdp", "switzerland unemployment", "switzerland inflation",
            "norway gdp", "norway unemployment", "norway inflation",
            "israel gdp", "israel unemployment", "israel inflation",
        ],

        # Currency/exchange → ExchangeRate
        "ExchangeRate": [
            "exchange rate", "forex", "currency exchange", "fx rate",
            "usd to", "eur to", "gbp to", "jpy to", "cny to", "cad to", "aud to",
            "to usd", "to eur", "to gbp", "to jpy", "to cny", "to cad", "to aud",
            "dollar to", "euro to", "pound to", "yen to", "yuan to",
            "usd/eur", "eur/usd", "gbp/usd", "usd/jpy", "usd/cny",
            "usd strength", "currency strength index",
            "dollar strength", "currency index",
        ],

        # Crypto → CoinGecko
        "CoinGecko": [
            "stablecoin", "defi", "decentralized finance",
            "cryptocurrency trading volume", "crypto trading volume",
            "nft", "blockchain", "altcoin", "crypto market cap",
            "bitcoin", "btc", "ethereum", "eth", "solana", "cardano", "dogecoin",
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

    # ==========================================================================
    # Query Type Patterns
    # ==========================================================================

    QUERY_TYPE_PATTERNS: Dict[str, List[str]] = {
        "trade": [
            "export", "import", "trade balance", "trade deficit", "trade surplus",
            "bilateral trade", "trade flow", "trading partner",
        ],
        "currency": [
            "exchange rate", "forex", "currency", "fx rate",
            "usd to", "eur to", "gbp to",
        ],
        "crypto": [
            "bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft",
            "btc", "eth", "altcoin",
        ],
        "fiscal": [
            "government debt", "fiscal deficit", "budget", "government spending",
            "public debt", "national debt", "sovereign debt",
        ],
        "development": [
            "poverty", "life expectancy", "literacy", "mortality",
            "access to", "enrollment",
        ],
        "commodity": [
            # Precious metals
            "gold price", "gold", "silver price", "silver", "platinum", "palladium",
            "precious metal", "precious metals",
            # Base metals
            "copper price", "copper", "iron ore", "aluminum", "zinc", "nickel",
            # Energy
            "oil price", "crude oil", "natural gas", "coal price", "fuel price",
            # Agricultural
            "wheat price", "corn price", "coffee price", "cocoa", "sugar price",
            # General
            "commodity", "commodities", "commodity index", "commodity price",
        ],
    }

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
        "blockchain", "stablecoin", "coin", "token"
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
    def detect_us_only_indicator(cls, query: str, indicators: List[str]) -> Optional[MatchResult]:
        """
        Check if query contains US-only indicators (must use FRED).

        Args:
            query: User's query
            indicators: List of parsed indicators

        Returns:
            MatchResult if US-only indicator found
        """
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

        # Check indicator keywords in priority order
        for provider, keywords in cls.INDICATOR_KEYWORDS.items():
            for keyword in keywords:
                if cls._keyword_matches(keyword, combined):
                    logger.info(f"🎯 Indicator keyword: {keyword} → {provider}")
                    return MatchResult(
                        provider=provider,
                        confidence=0.85,
                        matched_keyword=keyword,
                        match_type="indicator",
                        reasoning=f"Keyword '{keyword}' indicates {provider} as best source"
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
    def detect_query_type(cls, query: str) -> Optional[str]:
        """
        Classify the query type (trade, currency, crypto, fiscal, development).

        Args:
            query: User's query

        Returns:
            Query type string or None
        """
        query_lower = query.lower()

        for query_type, patterns in cls.QUERY_TYPE_PATTERNS.items():
            if any(pattern in query_lower for pattern in patterns):
                return query_type

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
