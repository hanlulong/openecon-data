"""
Provider Router Service

Deterministic provider selection based on query characteristics.
This replaces the complex routing logic previously embedded in the LLM prompt.

NOTE: This module is being consolidated into backend/routing/unified_router.py
The new UnifiedRouter provides the same functionality with cleaner separation of concerns.
To use the new routing system, set environment variable USE_UNIFIED_ROUTER=true.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional, Dict, List

# Load .env file BEFORE checking environment variables
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from ..models import ParsedIntent
from ..routing.country_resolver import CountryResolver


logger = logging.getLogger(__name__)

# Feature flags for gradual migration to UnifiedRouter
USE_UNIFIED_ROUTER = os.environ.get("USE_UNIFIED_ROUTER", "false").lower() == "true"
SHADOW_MODE = os.environ.get("UNIFIED_ROUTER_SHADOW", "false").lower() == "true"

# Log feature flag status at startup
if SHADOW_MODE:
    logger.info(f"🔍 [SHADOW] Shadow mode ENABLED - comparing legacy vs unified routing")
if USE_UNIFIED_ROUTER:
    logger.info(f"🔀 [UNIFIED] UnifiedRouter ENABLED - using new routing system")


class ProviderRouter:
    """
    Routes queries to appropriate data providers using deterministic rules.

    Priority hierarchy:
    1. Explicit provider mention (highest priority)
    2. Keyword-based provider detection (fixes WorldBank over-selection)
    3. US-only indicators (FRED)
    4. Country-specific providers (StatsCan for Canada)
    5. Indicator-specific providers (IMF for debt, BIS for housing prices)
    6. Default providers (WorldBank for most countries)
    """

    # US-only indicators that MUST use FRED
    US_ONLY_INDICATORS = {
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

    # DEPRECATED: Country sets moved to CountryResolver (backend/routing/country_resolver.py)
    # These are kept for backward compatibility but should use CountryResolver methods.
    # Use CountryResolver.is_oecd_member(), is_eu_member(), etc. instead.

    # OECD member countries (lowercase for legacy compatibility)
    OECD_MEMBERS = {c.lower() for c in CountryResolver.OECD_MEMBERS} | {
        "south korea", "czech republic", "czechia", "türkiye", "britain"
    }

    # OECD members that are NOT in EU
    OECD_NON_EU = {c.lower() for c in CountryResolver.OECD_NON_EU} | {
        "south korea", "türkiye", "britain"
    }

    # Non-OECD major economies
    NON_OECD_MAJOR = {c.lower() for c in CountryResolver.NON_OECD_MAJOR}

    # EU member states
    EU_MEMBERS = {c.lower() for c in CountryResolver.EU_MEMBERS} | {
        "czech republic", "czechia"
    }

    # Provider name variations for EXPLICIT mentions (e.g., "from OECD", "using IMF")
    # Note: "OECD", "IMF", "BIS", "Eurostat" at start of query are handled separately in detect_explicit_provider()
    # These should be phrases that indicate user wants a specific data source
    PROVIDER_KEYWORDS = {
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

    # Regional keywords for detecting multi-country queries
    REGIONAL_KEYWORDS = {
        "EUROSTAT": [
            "european countries", "eu countries", "eu member states", "eurozone countries",
            "across eu", "in europe", "european union countries", "eu region"
        ],
        "OECD": [
            "oecd countries", "oecd members", "oecd area", "oecd nations",
            "across oecd", "all oecd countries", "oecd member countries"
        ],
        "WORLDBANK": [
            "developing countries", "emerging markets", "emerging economies",
            "low-income countries", "middle-income countries",
            "asian countries", "latin american countries", "african countries",
            "south america", "sub-saharan africa",
            "g7 countries", "g20 countries"
        ],
        "STATSCAN": [
            "all provinces", "canadian provinces", "each province",
            "by province", "provincial data"
        ]
    }

    @classmethod
    def detect_explicit_provider(cls, query: str) -> Optional[str]:
        """
        Detect if user explicitly mentions a data provider.

        Args:
            query: User's natural language query

        Returns:
            Provider name if explicitly mentioned, None otherwise
        """
        query_lower = query.lower()

        # Special handling for OECD/IMF/BIS at start of query
        # These are often used as "OECD GDP for Italy" meaning "get GDP for Italy from OECD"
        # But NOT "OECD countries" or "OECD members" (those should use WorldBank)
        for provider in ["OECD", "IMF", "BIS", "Eurostat"]:
            provider_lower = provider.lower()
            # Check if query starts with provider name (with word boundary)
            if query_lower.startswith(provider_lower + " "):
                # Exclude patterns like "OECD countries", "IMF members", etc.
                # Check both singular and plural forms
                if not any(term in query_lower[:30] for term in ["countries", "country", "members", "member", "nations", "nation", "average"]):
                    logger.info(f"🎯 Explicit provider detected at start of query: {provider}")
                    return provider

        # Standard keyword matching for all providers
        for provider, keywords in cls.PROVIDER_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    logger.info(f"🎯 Explicit provider detected: {provider} (keyword: '{keyword}')")
                    return provider

        return None

    @classmethod
    def is_us_only_indicator(cls, indicators: List[str]) -> bool:
        """Check if query contains US-only indicators"""
        for indicator in indicators:
            indicator_lower = indicator.lower()
            for us_indicator in cls.US_ONLY_INDICATORS:
                if us_indicator in indicator_lower:
                    return True
        return False

    @classmethod
    def is_canadian_query(cls, query: str, parameters: Dict) -> bool:
        """Check if query is about Canada or Canadian regions"""
        query_lower = query.lower()

        # Check query text
        if any(term in query_lower for term in ["canada", "canadian", "ontario", "quebec",
                                                  "alberta", "british columbia", "bc ", "toronto",
                                                  "montreal", "vancouver"]):
            return True

        # Check parameters
        country = parameters.get("country", "").lower()
        if country in ["canada", "ca", "can"]:
            return True

        return False

    @classmethod
    def is_non_oecd_country(cls, country: str) -> bool:
        """Check if country is a non-OECD major economy.

        Uses CountryResolver as the single source of truth.
        """
        return CountryResolver.is_non_oecd_major(country) if country else False

    # Keyword patterns for specific providers (to fix WorldBank over-selection)
    # NOTE: Order matters! Dictionary is checked in order, so more specific providers should come first.
    # Trade keywords MUST come before country keywords to avoid mis-routing trade queries
    PROVIDER_KEYWORDS_PRIORITY = {
        # Trade-specific keywords → COMTRADE (MUST be first to catch trade queries)
        # NOTE: REMOVED " to " and " from " patterns - they incorrectly match date ranges like "from 2020 to 2024"
        # Trade partner detection is now handled by explicit patterns like "exports to", "imports from"
        "COMTRADE": [
            "exports to", "imports from", "trade with",  # Bilateral trade with explicit partner
            "trade flow", "bilateral trade", "trade deficit", "trade surplus",
            "top importers", "top exporters",
            "electric vehicle export", "machinery export", "textile import",
            "coffee export", "oil export", "crude oil export",
            "iron ore export", "semiconductor export", "pharmaceutical export",
            "wine export", "automobile export", "flower export",
            "agricultural import", "agricultural export",
            "fashion export", "textile export",
            # Commodity-specific patterns
            "exports of", "imports of", "export of", "import of",
        ],

        # US-specific economic indicators → FRED
        # IMPORTANT: Must come before OECD to prevent US queries being routed to OECD
        "FRED": [
            "us gdp", "us unemployment", "us inflation", "us cpi",
            "us housing", "us retail", "us industrial", "us consumer",
            "u.s. gdp", "u.s. unemployment", "u.s. inflation", "u.s. cpi",
            "united states gdp", "united states unemployment",
            "american gdp", "american unemployment", "american inflation",
            "federal funds", "fed rate", "treasury yield", "treasury rate",
            "s&p 500", "s&p500", "dow jones", "nasdaq",
            # US trade balance (without partner) - FRED has BOPGSTB series
            "us trade balance", "u.s. trade balance", "us trade deficit",
            "u.s. trade deficit", "united states trade balance",
        ],

        # European countries and EU-specific queries → EUROSTAT
        # IMPORTANT: Must come BEFORE OECD to catch EU-specific queries first
        # Priority for all EU member states to route to Eurostat instead of World Bank
        "EUROSTAT": [
            # EU/European region keywords
            "eu ", "european union", "eurozone", "euro area",
            "eu member states", "european countries",
            # Individual EU member states (use with economic indicators)
            "france gdp", "france unemployment", "france inflation", "france cpi",
            "germany gdp", "germany unemployment", "germany inflation", "germany cpi",
            "italy gdp", "italy unemployment", "italy inflation", "italy cpi",
            "spain gdp", "spain unemployment", "spain inflation", "spain cpi",
            "netherlands gdp", "netherlands unemployment", "netherlands inflation", "netherlands cpi",
            "belgium gdp", "belgium unemployment", "belgium inflation", "belgium cpi",
            "austria gdp", "austria unemployment", "austria inflation", "austria cpi",
            "portugal gdp", "portugal unemployment", "portugal inflation", "portugal cpi",
            "greece gdp", "greece unemployment", "greece inflation", "greece cpi",
            "ireland gdp", "ireland unemployment", "ireland inflation", "ireland cpi",
            "poland gdp", "poland unemployment", "poland inflation", "poland cpi",
            "sweden gdp", "sweden unemployment", "sweden inflation", "sweden cpi",
            "denmark gdp", "denmark unemployment", "denmark inflation", "denmark cpi",
            "finland gdp", "finland unemployment", "finland inflation", "finland cpi",
            "czech gdp", "czech unemployment", "czech inflation", "czech cpi",
            "czech republic gdp", "czech republic unemployment", "czech republic inflation", "czech republic cpi",
            "czechia gdp", "czechia unemployment", "czechia inflation", "czechia cpi",
            "romania gdp", "romania unemployment", "romania inflation", "romania cpi",
            "hungary gdp", "hungary unemployment", "hungary inflation", "hungary cpi",
            "bulgaria gdp", "bulgaria unemployment", "bulgaria inflation", "bulgaria cpi",
            "croatia gdp", "croatia unemployment", "croatia inflation", "croatia cpi",
            "slovakia gdp", "slovakia unemployment", "slovakia inflation", "slovakia cpi",
            "slovenia gdp", "slovenia unemployment", "slovenia inflation", "slovenia cpi",
            "estonia gdp", "estonia unemployment", "estonia inflation", "estonia cpi",
            "latvia gdp", "latvia unemployment", "latvia inflation", "latvia cpi",
            "lithuania gdp", "lithuania unemployment", "lithuania inflation", "lithuania cpi",
            "luxembourg gdp", "luxembourg unemployment", "luxembourg inflation", "luxembourg cpi",
            "malta gdp", "malta unemployment", "malta inflation", "malta cpi",
            "cyprus gdp", "cyprus unemployment", "cyprus inflation", "cyprus cpi",
            # Country names alone (for broader matching)
            # Include variations for common query patterns (in X, X?, etc.)
            " france", " germany", " italy", " spain", " netherlands",
            " belgium", " austria", " portugal", " greece", " ireland",
            " poland", " sweden", " denmark", " finland", " czech",
            " czech republic", " czechia", " romania", " hungary", " bulgaria", " croatia",
            " slovakia", " slovenia", " estonia", " latvia", " lithuania",
            " luxembourg", " malta", " cyprus",
            "in france", "in germany", "in italy", "in spain", "in netherlands",
            "in belgium", "in austria", "in portugal", "in greece", "in ireland",
            "in poland", "in sweden", "in denmark", "in finland", "in czech",
            "in czech republic", "in czechia", "in romania", "in hungary", "in bulgaria",
            "in croatia", "in slovakia", "in slovenia", "in estonia", "in latvia",
            "in lithuania", "in luxembourg", "in malta", "in cyprus",
            # Eurostat-specific indicators
            "harmonized index", "hicp", "harmonized consumer price",
            "purchasing power standards", "pps",
            "railway freight", "freight transport by rail"
        ],

        # Canadian-specific indicators → STATSCAN
        # IMPORTANT: Must include basic Canadian economic indicators to prevent OECD routing
        "STATSCAN": [
            # Basic Canadian economic indicators (prevents OECD from catching these)
            "canada gdp", "canada unemployment", "canada inflation", "canada cpi",
            "canada population", "canada housing", "canada retail", "canada trade",
            "canadian gdp", "canadian unemployment", "canadian inflation", "canadian cpi",
            "canadian population", "canadian housing", "canadian retail",
            # Specific StatsCan indicators
            "building permit", "residential construction", "commercial construction",
            "cpi breakdown", "cpi component", "price index breakdown",
            "consumer price index breakdown"
        ],

        # Fiscal/financial keywords → IMF
        # IMPORTANT: Fiscal/budget/debt queries MUST go to IMF (not CoinGecko!)
        "IMF": [
            "current account balance", "balance of payments",
            "inflation forecast", "economic forecast",
            "commodity price index", "primary commodity",
            "fiscal deficit", "government debt",
            # Inflation outlook keywords - global/world inflation queries
            "inflation outlook", "global inflation", "world inflation",
            # Fiscal/government budget keywords - CRITICAL for preventing CoinGecko routing
            "government deficit", "budget deficit", "fiscal balance",
            "government surplus", "budget surplus", "budget balance",
            "public debt", "sovereign debt", "national debt",
            "government spending", "public spending", "fiscal policy",
            "government balance", "deficit to gdp", "debt to gdp"
        ],

        # Property/housing and financial stability keywords → BIS
        "BIS": [
            "house price to income", "property valuation",
            "housing valuation", "real estate valuation",
            "property market", "housing market valuation",
            # Credit keywords - all variations to catch "credit to private non-financial sector"
            "credit to non-financial", "credit to gdp", "credit gap",
            "credit-to-gdp", "credit to gdp gap", "credit-to-gdp gap",
            "credit to private", "private non-financial", "non-financial sector",
            "total credit", "credit to private non-financial",
            "bank credit", "credit growth", "banking credit",  # Bank credit growth keywords
            "debt service ratio", "debt service",
            "residential property price", "property price index",
            "effective exchange rate", "exchange rate index",
            "global liquidity", "liquidity indicator",
            "international debt securities",
            "policy rate", "central bank policy rate",
            "commercial property price"
        ],

        # WorldBank development indicators (MUST come before OECD to catch life expectancy, poverty, etc.)
        # WorldBank is the authoritative source for development indicators across all countries
        "WorldBank": [
            "life expectancy", "infant mortality", "maternal mortality",
            "poverty", "poverty rate", "poverty headcount", "extreme poverty",
            "access to electricity", "access to clean water", "access to sanitation",
            "school enrollment", "literacy rate", "primary enrollment",
            "gini index", "income share", "income inequality",
            "forest area", "co2 emissions", "renewable energy",
            "agricultural land", "arable land",
            "mobile subscriptions", "internet users",
            "fertility rate", "birth rate", "death rate"
        ],

        # OECD statistical indicators (tax revenue, labor, education, health, etc.)
        # OECD is the authoritative source for comparative tax statistics across OECD member countries
        # NOTE: Do NOT include simple country names here - they cause over-routing to OECD
        # Country-specific routing is handled later based on OECD membership + specific indicators
        "OECD": [
            # OECD regional keywords
            "oecd countries", "oecd members", "oecd average", "oecd member countries",
            "oecd nations", "across oecd", "all oecd countries",
            # Tax and revenue keywords - MUST be routed to OECD for OECD member countries
            "tax revenue", "tax revenue to gdp", "tax receipts", "taxation",
            "tax as percent of gdp", "tax % of gdp", "tax to gdp",
            "revenue statistics", "tax statistics",
            "tax wedge", "labor income tax", "taxation of labor",
            # R&D keywords (OECD-specific)
            "r&d spending", "r&d expenditure", "research and development spending",
            # Productivity keywords (OECD-specific)
            "productivity growth", "labor productivity", "productivity comparison",
            # Pension keywords (OECD-specific)
            "pension spending", "pension expenditure",
            # Environmental (OECD-specific)
            "environmental tax", "carbon tax"
        ],

        # Currency/exchange rate queries → EXCHANGERATE
        # IMPORTANT: Must come before COMTRADE to prevent "USD to EUR" being caught by " to " pattern
        "EXCHANGERATE": [
            "exchange rate", "forex", "currency exchange", "fx rate",
            "usd to", "eur to", "gbp to", "jpy to", "cny to", "cad to", "aud to",
            "to usd", "to eur", "to gbp", "to jpy", "to cny", "to cad", "to aud",
            "dollar to", "euro to", "pound to", "yen to", "yuan to",
            "usd/eur", "eur/usd", "gbp/usd", "usd/jpy", "usd/cny",
            "usd strength", "currency strength index",
            "dollar strength", "currency index"
        ],

        # Crypto-specific keywords → COINGECKO
        "COINGECKO": [
            "stablecoin", "defi", "decentralized finance",
            "cryptocurrency trading volume", "crypto trading volume",
            "nft", "blockchain", "altcoin", "crypto market cap"
        ]
    }

    @classmethod
    def detect_regional_query(cls, query: str) -> Optional[str]:
        """
        Detect if query mentions a regional group (e.g., "OECD countries", "European countries").

        Args:
            query: User's natural language query

        Returns:
            Provider name if regional keywords detected, None otherwise
        """
        query_lower = query.lower()

        # Check for regional keywords in priority order
        for provider, keywords in cls.REGIONAL_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    logger.info(f"🌍 Regional query detected: '{keyword}' → {provider}")
                    return provider

        return None

    @classmethod
    def detect_keyword_provider(cls, query: str, indicators: List[str]) -> Optional[str]:
        """
        Detect provider based on specific keywords to fix WorldBank over-selection.

        This runs BEFORE the main routing logic to catch obvious cases.

        Args:
            query: User's natural language query
            indicators: List of indicators from parsed intent

        Returns:
            Provider name if keywords match, None otherwise
        """
        query_lower = query.lower()
        indicators_str = " ".join(indicators).lower() if indicators else ""
        combined_text = f"{query_lower} {indicators_str}"

        # EXCEPTION 1: Canada trade data routing
        # - Bilateral trade (with partner country) → Comtrade (UN international trade data)
        # - Trade balance → World Bank (has BN.GSR.GNFS.CD indicator)
        # - Exports/Imports (total, no partner) → StatsCan (has vector mappings)
        if cls.is_canadian_query(query_lower, {}):
            # Check if this is a trade query (imports/exports/trade balance)
            is_trade_query = any(term in combined_text for term in ["import", "export", "trade balance", "trade"])

            if is_trade_query:
                # Check if there's a partner country mentioned (bilateral trade → Comtrade)
                has_partner = any(term in query_lower for term in [
                    " to ", " from ", " with ", " between ",
                    "bilateral", "partner"
                ])

                if has_partner:
                    # Bilateral trade with partner country → MUST use Comtrade
                    logger.info(f"🇨🇦 Canadian bilateral trade (with partner) → routing to Comtrade")
                    return "Comtrade"
                elif "trade balance" in combined_text:
                    # Trade balance (no partner) → World Bank (StatsCan requires complex coordinate queries)
                    logger.info(f"🇨🇦 Canadian trade balance (no partner) → routing to WorldBank (has indicator)")
                    return "WorldBank"
                else:
                    # Exports/Imports (no partner) → StatsCan (has vector mappings for total exports/imports)
                    logger.info(f"🇨🇦 Canadian imports/exports (no partner) → routing to StatsCan")
                    return "StatsCan"

        # EXCEPTION 2: Property/house prices → BIS for non-US, FRED for US
        if any(term in combined_text for term in ["property price", "house price", "property prices", "house prices"]):
            # Check if US-specific
            is_us = any(term in query_lower for term in ["us ", "u.s.", "united states", "america"])
            if is_us:
                logger.info(f"🏠 US property prices → routing to BIS (has US data)")
                return "BIS"  # BIS has better property price indices than FRED
            else:
                logger.info(f"🏠 Property prices → routing to BIS")
                return "BIS"

        # EXCEPTION 3: US trade balance (without partner country) → FRED
        # MUST come before COMTRADE patterns to catch "US trade deficit" before "trade deficit"
        is_us_query = any(term in query_lower for term in ["us ", "u.s.", "united states", "america"])
        is_trade_balance = any(term in combined_text for term in ["trade balance", "trade deficit", "trade surplus"])
        has_partner = any(term in query_lower for term in [
            " to ", " from ", " with ", " between ", "bilateral", "partner"
        ])
        if is_us_query and is_trade_balance and not has_partner:
            logger.info(f"🇺🇸 US trade balance (no partner) → routing to FRED (has BOPGSTB)")
            return "FRED"

        # EXCEPTION 4: Exchange rate queries → ExchangeRate-API
        # MUST come before COMTRADE " to "/" from " pattern matching
        exchange_rate_patterns = [
            "exchange rate", "forex", "currency exchange", "fx rate",
            "usd to", "eur to", "gbp to", "jpy to", "cad to", "aud to",
            "to usd", "to eur", "to gbp", "to jpy", "to cad", "to aud",
            "usd/", "eur/", "gbp/", "/usd", "/eur", "/gbp",
            "dollar to euro", "euro to dollar", "pound to dollar",
        ]
        if any(pattern in query_lower for pattern in exchange_rate_patterns):
            logger.info(f"💱 Exchange rate query detected → routing to ExchangeRate")
            return "ExchangeRate"

        # EXCEPTION 5: Exports/Imports as % of GDP → WorldBank
        # These are macro development indicators (NE.EXP.GNFS.ZS, NE.IMP.GNFS.ZS), NOT bilateral trade data
        # MUST come before COMTRADE keyword matching to prevent mis-routing
        ratio_to_gdp_patterns = [
            "as % of gdp", "% of gdp", "as percentage of gdp", "as percent of gdp",
            "to gdp ratio", "/gdp ratio", "to gdp", "/gdp",
            "share of gdp", "as share of gdp",
        ]
        if any(term in combined_text for term in ["export", "import", "trade"]):
            if any(pattern in query_lower for pattern in ratio_to_gdp_patterns):
                logger.info(f"📊 Trade as % of GDP indicator → routing to WorldBank (not Comtrade)")
                return "WorldBank"

        # Extract country context for OECD routing decision
        # We need to know if the query is about an EU country vs non-EU OECD country
        mentioned_countries = []
        for country in cls.OECD_NON_EU:
            if country in query_lower:
                mentioned_countries.append(country)
                break

        # Also check for EU countries
        is_eu_country = any(country in query_lower for country in cls.EU_MEMBERS)

        for provider, keywords in cls.PROVIDER_KEYWORDS_PRIORITY.items():
            # Special handling for OECD keywords
            if provider == "OECD":
                for keyword in keywords:
                    if keyword in combined_text:
                        # OECD country mentions (Japan, Korea, etc.) - always route to OECD
                        if keyword in cls.OECD_NON_EU:
                            logger.info(f"🎯 Keyword-based routing: '{keyword}' (OECD non-EU country) → {provider}")
                            return provider
                        # OECD regional keywords - route to OECD for multi-country queries
                        elif any(region in keyword for region in ["oecd countries", "oecd members", "oecd average"]):
                            logger.info(f"🎯 Keyword-based routing: '{keyword}' (OECD regional) → {provider}")
                            return provider
                        # Other OECD keywords (unemployment, tax, etc.) - only route if country is non-EU OECD
                        elif mentioned_countries and not is_eu_country:
                            logger.info(f"🎯 Keyword-based routing: '{keyword}' for {mentioned_countries[0]} → {provider}")
                            return provider
                        # If EU country or no clear country context, skip OECD routing
                        # Let it fall through to other providers
                continue

            # For other providers, normal keyword matching
            for keyword in keywords:
                if keyword in combined_text:
                    logger.info(f"🎯 Keyword-based routing: '{keyword}' → {provider}")
                    return provider

        return None

    @classmethod
    def route_provider(cls, intent: ParsedIntent, original_query: str) -> str:
        """
        Determine the best provider for a query using deterministic rules.

        Args:
            intent: Parsed intent from LLM
            original_query: Original user query text

        Returns:
            Provider name (may override intent.apiProvider)
        """
        # Feature flag: Use new UnifiedRouter if enabled
        if USE_UNIFIED_ROUTER:
            try:
                from ..routing import UnifiedRouter
                router = UnifiedRouter()
                decision = router.route_with_intent(intent, original_query)
                if decision.provider != intent.apiProvider:
                    logger.info(f"🔄 [UnifiedRouter] Provider routing: {intent.apiProvider} → {decision.provider}")
                return decision.provider
            except Exception as e:
                logger.warning(f"UnifiedRouter failed, falling back to legacy: {e}")
                # Fall through to legacy routing

        # Shadow mode: Run UnifiedRouter FIRST and compare at every return point
        unified_result = None
        if SHADOW_MODE:
            logger.info(f"🔍 [SHADOW] Running UnifiedRouter comparison for: {original_query[:50]}...")
            try:
                from ..routing import UnifiedRouter
                router = UnifiedRouter()
                unified_result = router.route_with_intent(intent, original_query)
                logger.info(f"🔍 [SHADOW] UnifiedRouter returned: {unified_result.provider}")
            except Exception as e:
                logger.warning(f"🔍 [SHADOW] UnifiedRouter error: {e}")

        def _log_shadow_comparison(legacy_provider: str, decision_point: str = "") -> str:
            """Compare legacy with unified router and log differences."""
            if not SHADOW_MODE:
                return legacy_provider
            if unified_result is None:
                logger.info(f"🔍 [SHADOW] No comparison - UnifiedRouter failed")
                return legacy_provider
            if unified_result.provider != legacy_provider:
                logger.info(
                    f"🔍 [SHADOW] ❌ DIFFERENCE at {decision_point}: "
                    f"legacy={legacy_provider} vs unified={unified_result.provider} "
                    f"(unified_type={unified_result.match_type})"
                )
            else:
                logger.info(
                    f"🔍 [SHADOW] ✅ Agreement at {decision_point}: {legacy_provider}"
                )
            return legacy_provider

        # Priority 1: Explicit provider mention (ABSOLUTE HIGHEST)
        explicit_provider = cls.detect_explicit_provider(original_query)
        if explicit_provider:
            if explicit_provider != intent.apiProvider:
                logger.info(f"🔄 Overriding LLM provider ({intent.apiProvider}) with explicit request: {explicit_provider}")
            return _log_shadow_comparison(explicit_provider, "explicit_provider")

        # Priority 2: Keyword-based provider detection (HIGHEST PRIORITY after explicit)
        # This catches specific indicators (BIS financial, IMF fiscal, etc.) before generic regional routing
        keyword_provider = cls.detect_keyword_provider(original_query, intent.indicators)

        # Priority 3: Regional query detection (OECD countries, EU countries, etc.)
        regional_provider = cls.detect_regional_query(original_query)

        # Decision logic: Keyword provider takes priority over regional provider
        # Exception: If no keyword provider, use regional provider
        if keyword_provider:
            if keyword_provider != intent.apiProvider:
                logger.info(f"🔄 Overriding LLM provider ({intent.apiProvider}) with keyword-based routing: {keyword_provider}")
            return _log_shadow_comparison(keyword_provider, "keyword_routing")
        elif regional_provider:
            if regional_provider != intent.apiProvider:
                logger.info(f"🔄 Overriding LLM provider ({intent.apiProvider}) with regional detection: {regional_provider}")
            return _log_shadow_comparison(regional_provider, "regional_routing")

        # Priority 4: US-only indicators MUST use FRED
        if cls.is_us_only_indicator(intent.indicators):
            logger.info(f"🇺🇸 US-only indicator detected → routing to FRED")
            return _log_shadow_comparison("FRED", "us_only_indicator")

        # Priority 5: Canadian queries MUST use StatsCan (unless keyword routing already caught it)
        if cls.is_canadian_query(original_query, intent.parameters):
            logger.info(f"🇨🇦 Canadian query detected → routing to StatsCan")
            return _log_shadow_comparison("StatsCan", "canadian_query")

        # Get country and indicators for analysis
        country = intent.parameters.get("country", "")
        indicators_str = " ".join(intent.indicators).lower()
        query_lower = original_query.lower()

        # Priority 5: Multi-country queries MUST use WorldBank (unless trade/exchange rate)
        # EXCEPTION: Don't override for IMF-specific indicators (debt, fiscal, inflation)
        countries = intent.parameters.get("countries", [])
        indicators_str_check = " ".join(intent.indicators).lower()
        is_imf_indicator = any(term in indicators_str_check for term in ["debt", "fiscal", "deficit", "inflation", "unemployment", "current account"])

        if isinstance(countries, list) and len(countries) > 1:
            # Check if any country is non-OECD
            has_non_oecd = any(cls.is_non_oecd_country(c) for c in countries)
            if has_non_oecd and not is_imf_indicator:
                logger.info(f"🌍 Multi-country query with non-OECD countries → routing to WorldBank")
                return _log_shadow_comparison("WorldBank", "multi_country_non_oecd")

        # Priority 6: Non-OECD major economies prefer WorldBank
        # EXCEPTION: IMF is better for debt, fiscal, inflation, unemployment data
        if cls.is_non_oecd_country(country):
            if is_imf_indicator:
                logger.info(f"💰 Non-OECD country ({country}) with IMF indicator → routing to IMF")
                return _log_shadow_comparison("IMF", "non_oecd_imf_indicator")
            else:
                logger.info(f"🌍 Non-OECD country ({country}) → routing to WorldBank")
                return _log_shadow_comparison("WorldBank", "non_oecd_country")

        # Priority 7: Exchange rate queries → ExchangeRate (current rates only!)
        # Note: Historical bilateral exchange rates (USD/EUR) are NOT available without paid API
        if any(term in indicators_str for term in ["exchange rate", "forex", "currency", "fxrate"]) or \
           any(term in query_lower for term in [
               "exchange rate", "forex", "currency", "real exchange",
               "purchasing power parity", "ppp exchange", "currency movement",
               "interest rate differential", "currency pair"
           ]):
            # Exception 1: Real effective exchange rate (REER) → IMF
            if "real effective exchange rate" in query_lower or "reer" in query_lower:
                logger.info(f"💱 Real effective exchange rate (REER) → routing to IMF")
                return _log_shadow_comparison("IMF", "reer_exchange_rate")

            # For all other exchange rate queries (historical or current) → ExchangeRate
            # The provider will handle the limitation check and return appropriate error if historical
            logger.info(f"💱 Exchange rate query → routing to ExchangeRate")
            return _log_shadow_comparison("ExchangeRate", "exchange_rate")

        # Priority 8: European-specific queries → Eurostat (before general EU routing)
        # European regions, EU statistics, harmonized data
        if any(term in query_lower for term in [
            "european region", "eu member", "eurozone", "harmonized",
            "eu statistics", "european union", "across eu",
            "railway freight", "purchasing power standards"
        ]):
            # Check for specific European indicators
            if any(indicator in query_lower for indicator in [
                "r&d expenditure", "railway", "freight transport",
                "household disposable income", "purchasing power"
            ]):
                logger.info(f"🇪🇺 European-specific query detected → routing to Eurostat")
                return _log_shadow_comparison("Eurostat", "european_specific")

        # Priority 9: Indicator-specific routing (legacy - most now handled by keyword routing)

        # Multi-country economic groups → IMF or WorldBank
        # Oil-exporting countries, emerging markets, etc.
        if any(term in query_lower for term in [
            "oil-exporting countries", "opec", "emerging market",
            "developing countries", "low-income countries"
        ]):
            logger.info(f"🌍 Multi-country economic group detected → routing to IMF")
            return _log_shadow_comparison("IMF", "multi_country_group")

        # Fiscal/debt data → IMF (best source)
        if any(term in indicators_str for term in ["debt", "fiscal", "deficit", "government spending",
                                                     "balance of payments", "current account"]):
            logger.info(f"💰 Fiscal/debt indicator detected → preferring IMF")
            return _log_shadow_comparison("IMF", "fiscal_debt_indicator")

        # Housing/property prices → BIS (for global/multi-country) or FRED (for US only)
        if any(term in indicators_str for term in ["house price", "property price", "housing price",
                                                     "real estate"]):
            # Check if query is global/multi-country or specifically non-US
            is_global = any(term in query_lower for term in [
                "globally", "global", "by region", "across", "oecd countries",
                "major cities", "countries with", "emerging markets"
            ])
            is_us_only = country.upper() in ["US", "USA", "UNITED STATES"] or \
                        any(term in query_lower for term in ["us housing", "united states housing"])

            if is_global or (not country and not is_us_only):
                logger.info(f"🏠 Global/multi-country property prices → routing to BIS")
                return _log_shadow_comparison("BIS", "global_property_prices")
            elif is_us_only:
                logger.info(f"🏠 US housing prices → routing to FRED")
                return _log_shadow_comparison("FRED", "us_housing_prices")
            else:
                logger.info(f"🏠 Non-US housing prices → routing to BIS")
                return _log_shadow_comparison("BIS", "non_us_housing_prices")

        # Trade data → Comtrade (with exceptions for US/Canada trade balance and % of GDP)
        if any(term in indicators_str for term in ["import", "export", "trade", "bilateral"]):
            # EXCEPTION 1: Trade as % of GDP (development indicator) → WorldBank
            # These are macro development indicators (NE.EXP.GNFS.ZS, NE.IMP.GNFS.ZS), NOT bilateral trade
            ratio_to_gdp_patterns = [
                "as % of gdp", "% of gdp", "as percentage of gdp", "as percent of gdp",
                "to gdp ratio", "/gdp ratio", "to gdp", "/gdp",
                "share of gdp", "as share of gdp",
            ]
            matched_pattern = next((p for p in ratio_to_gdp_patterns if p in query_lower), None)
            if matched_pattern:
                logger.info(f"📊 Trade as % of GDP indicator → routing to WorldBank (not Comtrade)")
                return _log_shadow_comparison("WorldBank", "trade_pct_gdp")

            # EXCEPTION 2: US trade balance (no partner country) → FRED has BOPGSTB series
            is_us = country.upper() in ["US", "USA", "UNITED STATES"] if country else \
                    any(term in query_lower for term in ["us ", "u.s.", "united states", "america"])
            is_trade_balance = "trade balance" in indicators_str or "trade balance" in query_lower or \
                               "trade deficit" in indicators_str or "trade deficit" in query_lower
            has_partner = any(term in query_lower for term in [" to ", " from ", " with ", " between ", "bilateral", "partner"])

            if is_us and is_trade_balance and not has_partner:
                logger.info(f"🇺🇸 US trade balance (no partner) → routing to FRED (has BOPGSTB)")
                return _log_shadow_comparison("FRED", "us_trade_balance")

            logger.info(f"📦 Trade data detected → routing to Comtrade")
            return _log_shadow_comparison("Comtrade", "trade_data")

        # Cryptocurrency → CoinGecko (legacy - most now handled by keyword routing)
        if any(term in indicators_str for term in ["bitcoin", "ethereum", "crypto", "btc", "eth",
                                                     "solana", "cardano", "dogecoin"]):
            logger.info(f"₿ Cryptocurrency detected → routing to CoinGecko")
            return _log_shadow_comparison("CoinGecko", "cryptocurrency")

        # Priority 10: Default routing based on country context

        # No country specified - check query context
        if not country:
            # Check for European context
            if any(term in query_lower for term in ["european", "eu ", "eurozone", "europe"]):
                logger.info(f"🇪🇺 European context detected (no specific country) → routing to Eurostat")
                return _log_shadow_comparison("Eurostat", "european_context")

            # Check for OECD context or multi-country OECD-specific data
            if any(term in query_lower for term in [
                "oecd", "oecd countries", "oecd member",
                "environmental tax", "pension expenditure", "productivity growth"
            ]) and any(term in query_lower for term in ["by country", "across", "countries"]):
                logger.info(f"🌐 OECD multi-country data detected → routing to OECD")
                return _log_shadow_comparison("OECD", "oecd_multi_country")

            # US economic indicators without explicit country
            # NOTE: Don't auto-route to FRED - let keyword routing handle IMF-specific indicators
            # Only route to FRED if it's clearly a US-only query with no IMF indicators
            if not any(term in indicators_str for term in ["debt", "fiscal", "deficit", "government spending"]):
                if any(term in query_lower for term in ["retail sales", "consumer confidence", "housing starts"]):
                    # These are US-specific indicators that FRED excels at
                    if not any(term in query_lower for term in ["countries", "compare", "across", "global"]):
                        logger.info(f"🇺🇸 US-specific economic indicator (no country) → routing to FRED")
                        return _log_shadow_comparison("FRED", "us_specific_no_country")

        # US queries → Conditional routing based on indicator type
        if country.upper() in ["US", "USA", "UNITED STATES"]:
            # Don't auto-route to FRED for indicators that IMF handles better
            # IMF has better debt, fiscal, GDP growth data
            if not any(term in indicators_str for term in ["debt", "fiscal", "deficit", "current account", "inflation"]):
                logger.info(f"🇺🇸 US query (non-IMF indicator) → routing to FRED")
                return _log_shadow_comparison("FRED", "us_non_imf")
            # Otherwise let IMF keyword routing (Priority 2) handle it

        # OECD non-EU countries → prefer OECD for OECD-specific indicators
        # Countries like Japan, Korea, Australia, New Zealand, Mexico, etc.
        if country.lower() in cls.OECD_NON_EU:
            # Check if query has OECD-specific indicators
            has_oecd_indicator = any(term in query_lower or term in indicators_str for term in [
                "unemployment", "employment", "labor force", "productivity",
                "education", "health expenditure", "r&d", "research and development",
                "tax revenue", "gini", "income inequality", "social spending",
                "government spending", "public spending", "pension"
            ])

            if has_oecd_indicator:
                logger.info(f"🌐 OECD non-EU country ({country}) with OECD indicator → routing to OECD")
                return _log_shadow_comparison("OECD", "oecd_non_eu_indicator")
            # Otherwise fall through to WorldBank

        # EU countries → prefer Eurostat for EU-specific data, otherwise WorldBank
        if country.lower() in cls.EU_MEMBERS:
            logger.info(f"🇪🇺 EU country → preferring WorldBank (unless Eurostat specified)")
            return _log_shadow_comparison("WorldBank", "eu_country_default")

        # Priority 11: Use LLM's provider choice if no override rules apply
        logger.info(f"✅ Using LLM provider choice: {intent.apiProvider}")
        return _log_shadow_comparison(intent.apiProvider, "llm_choice")

    # Keywords that should NEVER go to CoinGecko
    NON_CRYPTO_FISCAL_KEYWORDS = [
        "government", "deficit", "surplus", "fiscal", "budget",
        "debt", "gdp", "unemployment", "inflation", "trade",
        "export", "import", "tax", "spending", "economic"
    ]

    # Keywords that MUST go to CoinGecko
    CRYPTO_KEYWORDS = [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
        "solana", "cardano", "dogecoin", "altcoin", "defi", "nft",
        "blockchain", "stablecoin", "coin", "token"
    ]

    @classmethod
    def correct_coingecko_misrouting(cls, provider: str, query: str, indicators: List[str]) -> str:
        """
        Correct cases where CoinGecko is incorrectly selected for non-crypto queries.

        This is a CRITICAL safety check - fiscal queries should NEVER go to CoinGecko.

        Args:
            provider: Selected provider
            query: Original query
            indicators: List of indicators

        Returns:
            Corrected provider (IMF if fiscal, original if crypto)
        """
        if provider.upper() != "COINGECKO":
            return provider

        query_lower = query.lower()
        indicators_str = " ".join(indicators).lower() if indicators else ""
        combined = f"{query_lower} {indicators_str}"

        # Check if query contains ANY crypto keywords
        has_crypto = any(kw in combined for kw in cls.CRYPTO_KEYWORDS)

        # Check if query contains fiscal/economic keywords
        has_fiscal = any(kw in combined for kw in cls.NON_CRYPTO_FISCAL_KEYWORDS)

        # If has fiscal keywords but NO crypto keywords, this is a misrouting
        if has_fiscal and not has_crypto:
            logger.warning(f"🚨 CORRECTING MISROUTING: CoinGecko selected for fiscal query '{query}' → IMF")
            return "IMF"

        return provider

    @classmethod
    def validate_routing(cls, provider: str, original_query: str, intent: ParsedIntent) -> Optional[str]:
        """
        Post-routing validation to catch incorrect routing decisions.

        Args:
            provider: Selected provider
            original_query: Original user query
            intent: Parsed intent

        Returns:
            Warning message if routing seems incorrect, None if OK
        """
        query_lower = original_query.lower()

        # Check 1: European/EU query but not routed to Eurostat
        if any(keyword in query_lower for keyword in ["european countries", "eu countries", "eu member"]):
            if provider.upper() not in ["EUROSTAT", "OECD"]:
                warning = f"⚠️ Query mentions European countries but routed to {provider}, not Eurostat"
                logger.warning(warning)
                return warning

        # Check 2: OECD query but not routed to OECD
        if any(keyword in query_lower for keyword in ["oecd countries", "oecd members"]):
            if provider.upper() != "OECD":
                warning = f"⚠️ Query mentions OECD countries but routed to {provider}, not OECD"
                logger.warning(warning)
                return warning

        # Check 3: FRED selected for non-US multi-country query
        if provider.upper() == "FRED":
            countries = intent.parameters.get("countries", [])
            country = intent.parameters.get("country", "")
            if isinstance(countries, list) and len(countries) > 1:
                if not all(c.upper() in ["US", "USA", "UNITED STATES"] for c in countries):
                    warning = f"⚠️ FRED selected for multi-country query ({countries}) - should use WorldBank"
                    logger.warning(warning)
                    return warning
            elif country and country.upper() not in ["US", "USA", "UNITED STATES"]:
                warning = f"⚠️ FRED selected for non-US query (country={country}) - should use WorldBank or OECD"
                logger.warning(warning)
                return warning

        # Check 4: Developing countries query not routed to WorldBank
        if any(keyword in query_lower for keyword in ["developing countries", "emerging markets"]):
            if provider.upper() not in ["WORLDBANK", "IMF"]:
                warning = f"⚠️ Query about developing countries routed to {provider}, not WorldBank"
                logger.warning(warning)
                return warning

        return None

    @classmethod
    def get_fallback_provider(cls, primary_provider: str, intent: ParsedIntent) -> Optional[str]:
        """
        Get fallback provider when primary fails.

        Args:
            primary_provider: Provider that failed
            intent: Parsed intent

        Returns:
            Fallback provider name or None
        """
        fallback_map = {
            "OECD": "WorldBank",  # WorldBank has broader coverage
            "Eurostat": "WorldBank",  # WorldBank works for EU countries
            "BIS": "IMF",  # IMF has some financial data
            "IMF": "WorldBank",  # WorldBank is most comprehensive
        }

        fallback = fallback_map.get(primary_provider.upper())
        if fallback:
            logger.info(f"🔄 Fallback: {primary_provider} → {fallback}")

        return fallback
