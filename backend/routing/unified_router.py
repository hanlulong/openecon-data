"""
Unified Router - Single Entry Point for All Routing Decisions

The LLM now handles semantic routing (indicator detection, crypto vs.
fiscal classification, US-only indicators, etc.) via the provider capability
matrix in the prompt.

This router retains only STRUCTURAL routing:
1. Explicit provider mention ("from FRED", "using IMF")
2. Exchange rate detection (ExchangeRate-API + BIS for REER/NEER)
3. Bilateral trade detection (Comtrade is the ONLY bilateral trade provider)
4. Country-based defaults (structural membership: EU→Eurostat, US→FRED)
5. LLM provider choice (trust the LLM for everything else)

A lightweight CoinGecko guard (_correct_coingecko) prevents fiscal queries
from landing on the crypto-only provider.

Usage:
    from backend.routing import UnifiedRouter

    router = UnifiedRouter()
    decision = router.route(query, indicators)

    print(f"Provider: {decision.provider}")
    print(f"Confidence: {decision.confidence}")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple

from .country_resolver import CountryResolver

logger = logging.getLogger(__name__)

# HS (Harmonized System) commodity code pattern — matches "HS 8542", "HS-2204",
# "HS8703", etc.  Used to route queries with explicit HS codes to Comtrade.
_HS_CODE_RE = re.compile(r'\bHS\s*[-]?\s*\d{4,6}\b', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Inline helpers (lightweight, structural checks only)
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



def _correct_coingecko(provider: str, query: str, indicators: List[str]) -> Tuple[str, Optional[str]]:
    """If CoinGecko was chosen for a non-crypto query, redirect to IMF.

    Returns (corrected_provider, reason_or_None).
    This is a lightweight structural guard — CoinGecko only serves crypto data,
    so fiscal/macro queries that land here are obvious misroutes.
    """
    if provider.upper() != "COINGECKO":
        return provider, None

    query_lower = query.lower()
    indicators_str = " ".join(indicators).lower() if indicators else ""
    combined = f" {query_lower} {indicators_str} "

    # Structural: if the query mentions macro/fiscal terms but no crypto asset
    # names, CoinGecko cannot serve it.  The LLM should rarely misroute, but
    # this guard catches edge cases.
    _FISCAL = re.compile(
        r"\b(?:government|deficit|surplus|fiscal|budget|debt|gdp|unemployment"
        r"|inflation|tax|spending)\b"
    )
    _CRYPTO = re.compile(
        r"\b(?:bitcoin|btc|ethereum|eth|crypto|cryptocurrency|xrp|ripple"
        r"|solana|cardano|dogecoin|litecoin|bnb|defi|nft|stablecoin|altcoin)\b"
    )

    if _FISCAL.search(combined) and not _CRYPTO.search(combined):
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
    match_type: str = "default"  # explicit, indicator, country, region, llm, default
    matched_pattern: Optional[str] = None


class UnifiedRouter:
    """
    Single entry point for all provider routing decisions.

    After LLM-refactor, this router handles only structural routing:
    1. Explicit provider mention (highest confidence)
    2. Exchange rate → ExchangeRate-API / BIS for REER
    3. Bilateral trade → Comtrade (only bilateral trade provider)
    4. Canadian queries → StatsCan (only Canada-specific provider)
    5. Regional group routing (EU countries → Eurostat, etc.)
    6. Country-based routing (US → FRED, EU → Eurostat, etc.)
    7. Multi-country routing
    8. LLM provider choice (trust the LLM for semantic decisions)
    9. Default → WorldBank

    Semantic routing (US-only indicators, crypto detection, indicator
    classification) is handled by the LLM via the provider capability
    matrix in the prompt.  The _correct_coingecko() guard remains as
    a lightweight structural safeguard.
    """

    # Fallback chains when primary provider fails.
    #
    # Design: no direct A↔B mutual pairs (e.g. if A→B then B must NOT→A).
    # The runtime get_fallbacks() also filters out providers already tried
    # via an ``exclude`` parameter to prevent cycles at call sites.
    #
    # Tier structure (fallbacks generally flow downward):
    #   Tier 1 (specialty): OECD, BIS, StatsCan, CoinGecko, Comtrade, ExchangeRate
    #   Tier 2 (regional):  Eurostat
    #   Tier 3 (broad):     FRED, IMF
    #   Tier 4 (universal): WorldBank  (sink — no outgoing fallbacks)
    FALLBACK_MAP: Dict[str, List[str]] = {
        "OECD": ["Eurostat", "WorldBank"],
        "EUROSTAT": ["WorldBank", "IMF"],
        "BIS": ["IMF", "WorldBank"],
        "IMF": ["FRED", "WorldBank"],
        "STATSCAN": ["FRED", "WorldBank"],
        "FRED": ["WorldBank"],
        "COMTRADE": ["Eurostat", "WorldBank"],
        "WORLDBANK": [],
        "EXCHANGERATE": ["FRED"],
        "COINGECKO": ["FRED"],
    }

    DEFAULT_PROVIDER = "WorldBank"

    def __init__(self, catalog_service=None, use_catalog: bool = True):
        # Arguments retained for API compatibility only. Provider routing must
        # not use semantic catalog shortcuts under the no-rule matching policy.
        self._catalog_service = None
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
            llm_provider: Provider suggested by LLM

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

        # 1. Explicit provider mention (ABSOLUTE HIGHEST)
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

        # 2. Exchange rate → ExchangeRate-API / BIS for REER/NEER
        if self._is_exchange_rate_query(query_lower, indicators):
            if any(t in query_lower for t in (
                "real effective exchange rate", "reer",
                "nominal effective exchange rate", "neer",
                "effective exchange rate",
            )):
                return self._create_decision(
                    provider="BIS",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="effective exchange rate",
                    reasoning="Effective exchange rates (REER/NEER) are best sourced from BIS",
                )
            return self._create_decision(
                provider="ExchangeRate",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="exchange rate",
                reasoning="Exchange rate query routed to ExchangeRate-API",
            )

        # 2b. HS commodity code + trade verb → Comtrade (structural: HS codes are Comtrade-specific)
        if self._is_hs_code_trade_query(query_lower):
            hs_match = _HS_CODE_RE.search(query)
            matched_code = hs_match.group(0) if hs_match else "HS code"
            return self._create_decision(
                provider="Comtrade",
                confidence=0.92,
                match_type="indicator",
                matched_pattern=f"HS code: {matched_code}",
                reasoning=f"Query contains HS commodity code ({matched_code}), routed to Comtrade",
            )

        # 3. Bilateral trade → Comtrade (structural: only bilateral trade provider)
        if self._is_bilateral_trade_query(query_lower, query):
            return self._create_decision(
                provider="Comtrade",
                confidence=0.88,
                match_type="indicator",
                matched_pattern="bilateral trade",
                reasoning="Bilateral trade query routed to Comtrade",
            )

        # 4. Canadian queries (structural: StatsCan is the only Canada-specific provider)
        if CountryResolver.is_canadian_region(query):
            return self._handle_canadian_query(query, indicators, country)

        # 5. Structural specialty-provider cues that should override broad regional defaults.
        if self._is_fred_structural_query(query_lower):
            return self._create_decision(
                provider="FRED",
                confidence=0.88,
                match_type="indicator",
                matched_pattern="US monetary / FRED structural cue",
                reasoning="US-specific monetary query routed to FRED",
            )

        if self._is_non_bilateral_trade_flow_query(query_lower):
            return self._create_decision(
                provider="Comtrade",
                confidence=0.82,
                match_type="indicator",
                matched_pattern="unilateral goods trade flow",
                reasoning="Goods import/export flow query routed to Comtrade",
            )

        if self._is_property_market_query(query_lower):
            return self._create_decision(
                provider="BIS",
                confidence=0.84,
                match_type="indicator",
                matched_pattern="property / housing market",
                reasoning="Property or housing price query routed to BIS",
            )

        if self._is_imf_macro_query(query_lower):
            return self._create_decision(
                provider="IMF",
                confidence=0.82,
                match_type="indicator",
                matched_pattern="IMF macro aggregate / forecast",
                reasoning="Macro aggregate / projection query routed to IMF",
            )

        # 6. Regional group routing (EU countries, OECD countries, etc.)
        regional_decision = self._route_by_regional_group(query_lower)
        if regional_decision:
            return regional_decision

        # 7. Country-based routing
        country_decision = self._route_by_country(country, countries, query_lower, indicators)
        if country_decision:
            return country_decision

        # 8. Multi-country with non-OECD → WorldBank
        if countries and len(countries) > 1:
            has_non_oecd = any(CountryResolver.is_non_oecd_major(c) for c in countries)
            if has_non_oecd:
                return self._create_decision(
                    provider="WorldBank",
                    confidence=0.75,
                    match_type="country",
                    reasoning="Multi-country query with non-OECD countries → WorldBank",
                )

        # 9. Trust LLM's provider choice
        if llm_provider and llm_provider != self.DEFAULT_PROVIDER:
            corrected, reason = _correct_coingecko(llm_provider, query, indicators)
            return self._create_decision(
                provider=corrected,
                confidence=0.60,
                match_type="llm",
                reasoning=reason or f"Using LLM suggested provider: {llm_provider}",
            )

        # 11. Default
        return self._create_decision(
            provider=self.DEFAULT_PROVIDER,
            confidence=0.50,
            match_type="default",
            reasoning=f"No specific routing rules matched, using default: {self.DEFAULT_PROVIDER}",
        )

    def route_with_intent(self, intent: Any, original_query: str) -> RoutingDecision:
        """Route using a ParsedIntent object (compatibility method)."""
        indicators = getattr(intent, "indicators", []) or []
        parameters = getattr(intent, "parameters", {}) or {}
        country = parameters.get("country", "")
        countries = parameters.get("countries") or []
        llm_provider = getattr(intent, "apiProvider", None)

        return self.route(
            query=original_query,
            indicators=indicators,
            country=country,
            countries=countries,
            llm_provider=llm_provider,
        )

    def get_fallbacks(self, provider: str, *, exclude: Optional[set] = None) -> List[str]:
        """Get fallback providers when primary fails.

        Args:
            provider: The provider that failed.
            exclude: Optional set of provider names (upper-case) to skip.
                     Callers that chain fallbacks should pass the set of
                     providers already attempted to prevent cycles.

        Returns:
            List of fallback provider names, filtered to exclude the
            primary provider itself and any providers in *exclude*.
        """
        key = provider.upper()
        fallbacks = self.FALLBACK_MAP.get(key, [self.DEFAULT_PROVIDER])
        skip = {key}
        if exclude:
            skip |= {e.upper() for e in exclude}
        return [fb for fb in fallbacks if fb.upper() not in skip]

    # ==========================================================================
    # Private Helper Methods — structural checks only
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

    @staticmethod
    def _is_hs_code_trade_query(query_lower: str) -> bool:
        """Detect queries with explicit HS commodity codes combined with trade language.

        HS (Harmonized System) codes are specific to trade classification and
        uniquely served by Comtrade.  If the query contains an HS code AND
        mentions imports/exports/trade, it's unambiguously a Comtrade query.

        Examples:
            "China imports of HS 8542 integrated circuits" → True
            "France exports of HS 2204 wine" → True
            "HS 8703 trade data for Germany" → True
            "What is HS 8542?" → False (no trade verb)
        """
        if not _HS_CODE_RE.search(query_lower):
            return False

        trade_terms = [
            "import", "imports", "importing",
            "export", "exports", "exporting",
            "trade", "trading", "trade flow", "trade data",
            "shipment", "shipments",
        ]
        return any(term in query_lower for term in trade_terms)

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

    @staticmethod
    def _is_fred_structural_query(query_lower: str) -> bool:
        """Detect strongly US/FRED-specific monetary terms."""
        fred_terms = [
            "federal funds",
            "fed funds",
            "fomc rate",
            "st louis fed",
        ]
        return any(term in query_lower for term in fred_terms)

    @staticmethod
    def _is_aggregate_trade_indicator(query_lower: str) -> bool:
        """Detect aggregate/macro trade indicators that belong to WorldBank/IMF, not Comtrade.

        Queries about trade ratios, shares, or percentages of GDP are macro indicators
        (e.g., "Imports of goods and services (% of GDP)") — NOT bilateral trade flows.
        These are available from WorldBank (NE.IMP.GNFS.ZS, NE.EXP.GNFS.ZS, NE.TRD.GNFS.ZS)
        and should never be routed to Comtrade.
        """
        # Ratio/share/percentage qualifiers that indicate a macro indicator
        aggregate_patterns = [
            r"\b(?:share|%|percent(?:age)?|ratio)\s+(?:of\s+)?gdp\b",
            r"\bof\s+gdp\b",
            r"\bas\s+(?:a\s+)?(?:%|percent(?:age)?|share|proportion|fraction)\s+of\b",
            r"\bto\s+gdp\s+ratio\b",
            r"\bgdp\s+(?:share|ratio|percent(?:age)?)\b",
            r"\b(?:goods\s+and\s+services)\s+(?:as\s+)?(?:%|percent(?:age)?)\b",
            r"\b(?:goods\s+and\s+services)\s+(?:as\s+)?(?:share|proportion)\s+of\b",
            r"\b(?:service|services)\s+(?:imports?|exports?)\s+(?:share|%|percent)\b",
            r"\b(?:merchandise)\s+(?:imports?|exports?)\s+(?:as\s+)?(?:share|%|percent)\b",
        ]
        return any(re.search(pat, query_lower) for pat in aggregate_patterns)

    @classmethod
    def _is_non_bilateral_trade_flow_query(cls, query_lower: str) -> bool:
        """Detect unilateral goods trade-flow queries that belong to Comtrade."""
        if cls._is_aggregate_trade_indicator(query_lower):
            return False
        if "trade balance" in query_lower or "current account" in query_lower:
            return False
        if not any(term in query_lower for term in ["import", "imports", "export", "exports"]):
            return False

        trade_flow_terms = [
            "semiconductor", "chip", "chips", "pharmaceutical", "pharmaceuticals",
            "agricultural", "agriculture", "electronics", "electronic",
            "auto parts", "textile", "textiles", "petroleum", "oil",
            "steel", "mineral", "minerals", "soybean", "soybeans",
            "commodity", "commodities", "goods",
        ]
        return any(term in query_lower for term in trade_flow_terms)

    @staticmethod
    def _is_property_market_query(query_lower: str) -> bool:
        """Detect property/house/real-estate price queries routed to BIS."""
        property_terms = [
            "residential property",
            "property prices",
            "real estate prices",
            "real estate market",
            "house prices",
            "housing market index",
            "property price index",
            "housing price index",
        ]
        return any(term in query_lower for term in property_terms)

    @staticmethod
    def _is_imf_macro_query(query_lower: str) -> bool:
        """Detect global/group macro aggregate and forecast queries best suited for IMF."""
        forecast_terms = ["forecast", "forecasts", "projection", "projections"]
        macro_terms = [
            "inflation", "gdp growth", "economic growth", "current account",
            "fiscal deficit", "government debt", "commodity price index",
            "trade volume", "world economic outlook",
        ]
        macro_group_terms = [
            "global", "world", "advanced economies", "emerging markets",
            "developing economies", "g20", "emerging economies",
        ]

        has_macro = any(term in query_lower for term in macro_terms)
        if has_macro and any(term in query_lower for term in forecast_terms):
            return True
        return has_macro and any(term in query_lower for term in macro_group_terms)

    def _is_bilateral_trade_query(self, query_lower: str, query: str) -> bool:
        """Detect bilateral trade queries (exports from X to Y, trade between X and Y).

        This is structural: Comtrade is the only provider for bilateral trade flows.

        IMPORTANT: Aggregate trade indicators (import/export share of GDP, trade as % of GDP)
        are macro indicators from WorldBank/IMF, NOT bilateral trade flows. These must NOT
        match here so they can fall through to the general routing path.
        """
        # Early exit: aggregate trade indicators (% of GDP, share of GDP) are NOT bilateral
        if self._is_aggregate_trade_indicator(query_lower):
            return False

        # Explicit bilateral language
        if any(term in query_lower for term in ["bilateral", "trading partner", "trade partner"]):
            return True

        # "between X and Y" usually indicates bilateral trade
        if re.search(r"\bbetween\b.+\band\b", query_lower):
            if any(term in query_lower for term in ["trade", "export", "import"]):
                return True

        # Trade verb near to/from/with
        if re.search(r"\b(exports?|imports?|trade(?:\s+flow)?|trading)\s+(to|from|with)\b", query_lower):
            return True

        # Multiple countries in a trade query = bilateral
        if any(term in query_lower for term in ["export", "import", "trade"]):
            mentioned = CountryResolver.detect_all_countries_in_query(query)
            if len(mentioned) >= 2:
                return True

        return False

    def _handle_canadian_query(
        self,
        query: str,
        indicators: List[str],
        country: Optional[str],
    ) -> RoutingDecision:
        """Handle Canadian-specific routing (structural: StatsCan is Canada-only).

        Routing priority for Canada queries:
        1. Property market → BIS (structural: BIS has cross-country property data)
        2. Bilateral trade → Comtrade (structural: only bilateral trade provider)
        3. Non-bilateral trade → StatsCan
        4. Development-only indicators (no StatsCan coverage) → WorldBank
        5. Default → StatsCan
        """
        query_lower = query.lower()
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        # Property market → BIS (structural: BIS has cross-country property data)
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

        # Trade queries
        is_trade = any(term in combined for term in ["import", "export", "trade"])
        if is_trade:
            # Bilateral trade → Comtrade
            if self._is_bilateral_trade_query(query_lower, query):
                return self._create_decision(
                    provider="Comtrade",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="Canadian bilateral trade",
                    reasoning="Canadian bilateral trade → Comtrade",
                )
            # Non-bilateral Canadian trade → StatsCan
            return self._create_decision(
                provider="StatsCan",
                confidence=0.85,
                match_type="indicator",
                matched_pattern="Canadian trade",
                reasoning="Canadian trade (no partner) → StatsCan",
            )

        # Development-only indicators unlikely to be in StatsCan — route to
        # WorldBank. These are structural: StatsCan is a national statistics
        # office and does not track global development metrics.
        _DEVELOPMENT_ONLY = [
            "life expectancy", "fertility", "mortality",
            "co2", "emissions", "forest", "renewable energy",
            "literacy", "poverty",
        ]
        if any(term in combined for term in _DEVELOPMENT_ONLY):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.80,
                match_type="indicator",
                matched_pattern="Canada development indicator",
                reasoning="Canadian query with development indicator → WorldBank",
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

        # Bare "EU" or "Europe"/"European"/"Eurozone" as region indicator → Eurostat
        # "EU employment rate", "European inflation", "Eurozone GDP growth"
        if re.search(r"\beu\b", query_lower) or re.search(r"\beuro(?:pe|pean|zone)\b", query_lower):
            return self._create_decision(
                provider="Eurostat",
                confidence=0.80,
                match_type="region",
                matched_pattern="EU/Europe region",
                reasoning="Query mentions EU/Europe region, routed to Eurostat",
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

    def _route_by_country(
        self,
        country: Optional[str],
        countries: Optional[List[str]],
        query_lower: str,
        indicators: List[str],
    ) -> Optional[RoutingDecision]:
        """Route based on country membership (structural)."""
        if not country:
            return None

        # US → FRED
        if CountryResolver.is_us(country):
            return self._create_decision(
                provider="FRED",
                confidence=0.80,
                match_type="country",
                matched_pattern="United States",
                reasoning="US query routed to FRED",
            )

        # Non-OECD major economies → WorldBank
        if CountryResolver.is_non_oecd_major(country):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.75,
                match_type="country",
                matched_pattern=country,
                reasoning=f"Non-OECD major economy ({country}) → WorldBank",
            )

        # EU members → Eurostat
        if CountryResolver.is_eu_member(country):
            return self._create_decision(
                provider="Eurostat",
                confidence=0.75,
                match_type="country",
                matched_pattern=country,
                reasoning=f"EU member ({country}) → Eurostat",
            )

        # OECD non-EU → WorldBank for standard indicators (OECD resolution unreliable)
        if CountryResolver.is_oecd_non_eu(country):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.70,
                match_type="country",
                matched_pattern=country,
                reasoning=f"OECD non-EU country ({country}) → WorldBank (broader coverage)",
            )

        return None


# ==========================================================================
# Compatibility Layer — preserves public API for callers
# ==========================================================================

def route_provider(intent: Any, original_query: str) -> str:
    """Compatibility function matching ProviderRouter.route_provider() signature."""
    router = UnifiedRouter()
    decision = router.route_with_intent(intent, original_query)
    return decision.provider


def detect_explicit_provider(query: str) -> Optional[str]:
    """Compatibility function matching ProviderRouter.detect_explicit_provider() signature."""
    result = detect_explicit_provider_match(query)
    return result[0] if result else None


def correct_coingecko_misrouting(provider: str, query: str, indicators: list) -> str:
    """Compatibility function matching ProviderRouter.correct_coingecko_misrouting() signature."""
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

    return None
