"""
Unified Router - Single Entry Point for All Routing Decisions

Phase 3 of the LLM-refactor: simplified from ~1,300 lines to ~300 lines.
The LLM now handles semantic routing (indicator detection, fiscal vs. trade
classification, etc.) via the provider capability matrix in the prompt.

This router retains only STRUCTURAL routing:
1. Explicit provider mention ("from FRED", "using IMF")
2. Crypto detection (CoinGecko is the ONLY crypto provider)
3. Exchange rate detection (ExchangeRate-API + BIS for REER/NEER)
4. Bilateral trade detection (Comtrade is the ONLY bilateral trade provider)
5. Catalog concept matching (data-driven YAML lookups, not rules)
6. Country-based defaults (structural membership: EU→Eurostat, US→FRED)
7. LLM provider choice (trust the LLM for everything else)

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
from typing import Optional, List, Dict, Any, Set, Tuple

from .country_resolver import CountryResolver

logger = logging.getLogger(__name__)


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

_CRYPTO_KEYWORDS: Set[str] = {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
    "solana", "cardano", "dogecoin", "altcoin", "defi", "nft",
    "blockchain", "stablecoin", "coin", "token", "xrp", "ripple",
    "litecoin", "ltc", "bnb",
}

# Anti-misrouting: fiscal keywords that should NEVER go to CoinGecko
_NON_CRYPTO_FISCAL_KEYWORDS: Set[str] = {
    "government", "deficit", "surplus", "fiscal", "budget",
    "debt", "gdp", "unemployment", "inflation", "trade",
    "export", "import", "tax", "spending", "economic",
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
    """Return matched US-only indicator term, or None.

    Kept for backward compatibility but the set is intentionally minimal.
    The LLM now handles most US-specific indicator routing.
    """
    all_countries = countries or ([country] if country else [])
    if all_countries:
        non_us = [c for c in all_countries
                  if c.upper() not in ("US", "USA", "UNITED STATES", "UNITED_STATES")]
        if non_us:
            return None

    query_lower = query.lower()
    indicators_str = " ".join(indicators).lower() if indicators else ""
    combined = f"{query_lower} {indicators_str}"

    # Minimal set: only truly US-exclusive indicators (no international equivalent)
    us_only = {
        "case-shiller", "case shiller",
        "federal funds", "fed funds",
        "nonfarm payrolls",
        "initial claims", "unemployment claims",
        "s&p 500", "sp500", "s&p",
        "dow jones", "djia",
    }
    for indicator in us_only:
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
    match_type: str = "default"  # explicit, indicator, country, catalog, region, llm, default
    matched_pattern: Optional[str] = None


class UnifiedRouter:
    """
    Single entry point for all provider routing decisions.

    After Phase 3 LLM-refactor, this router handles only structural routing:
    1. Explicit provider mention (highest confidence)
    2. US-only indicators (Case-Shiller, S&P 500, etc.)
    3. Crypto → CoinGecko (only crypto provider)
    4. Exchange rate → ExchangeRate-API / BIS for REER
    5. Bilateral trade → Comtrade (only bilateral trade provider)
    6. Catalog concept match (data-driven YAML lookups)
    7. Regional group routing (EU countries → Eurostat, etc.)
    8. Country-based routing (US → FRED, EU → Eurostat, etc.)
    9. LLM provider choice (trust the LLM for semantic decisions)
    10. Default → WorldBank
    """

    # Fallback chains when primary provider fails
    FALLBACK_MAP: Dict[str, List[str]] = {
        "OECD": ["WorldBank", "Eurostat"],
        "EUROSTAT": ["WorldBank", "IMF"],
        "BIS": ["IMF", "WorldBank"],
        "IMF": ["BIS", "WorldBank", "OECD"],
        "STATSCAN": ["WorldBank", "OECD"],
        "FRED": ["WorldBank", "OECD"],
        "COMTRADE": ["WorldBank"],
        "WORLDBANK": ["IMF", "OECD"],
        "EXCHANGERATE": ["FRED"],
        "COINGECKO": [],
    }

    DEFAULT_PROVIDER = "WorldBank"

    def __init__(self, catalog_service=None, use_catalog: bool = True):
        self._catalog_service = catalog_service
        self._use_catalog = use_catalog

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

        # 2. US-only indicators (Case-Shiller, S&P, Fed funds — no intl equivalent)
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

        # 3. Crypto → CoinGecko (structural: only crypto provider)
        if self._is_crypto_query(query_lower, indicators):
            return self._create_decision(
                provider="CoinGecko",
                confidence=0.92,
                match_type="indicator",
                matched_pattern="crypto asset query",
                reasoning="Cryptocurrency/token market query routed to CoinGecko",
            )

        # 4. Exchange rate → ExchangeRate-API / BIS for REER/NEER
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

        # 5. Bilateral trade → Comtrade (structural: only bilateral trade provider)
        if self._is_bilateral_trade_query(query_lower, query):
            return self._create_decision(
                provider="Comtrade",
                confidence=0.88,
                match_type="indicator",
                matched_pattern="bilateral trade",
                reasoning="Bilateral trade query routed to Comtrade",
            )

        # 6. Canadian queries (structural: StatsCan is the only Canada-specific provider)
        if CountryResolver.is_canadian_region(query):
            return self._handle_canadian_query(query, indicators, country)

        # 7. Catalog concept match (data-driven YAML lookups)
        if self._use_catalog and self._catalog_service:
            catalog_decision = self._route_by_catalog(indicators, country, query=query)
            if catalog_decision:
                return catalog_decision

        # 8. Regional group routing (EU countries, OECD countries, etc.)
        regional_decision = self._route_by_regional_group(query_lower)
        if regional_decision:
            return regional_decision

        # 9. Country-based routing
        country_decision = self._route_by_country(country, countries, query_lower, indicators)
        if country_decision:
            return country_decision

        # 10. Multi-country with non-OECD → WorldBank
        if countries and len(countries) > 1:
            has_non_oecd = any(CountryResolver.is_non_oecd_major(c) for c in countries)
            if has_non_oecd:
                return self._create_decision(
                    provider="WorldBank",
                    confidence=0.75,
                    match_type="country",
                    reasoning="Multi-country query with non-OECD countries → WorldBank",
                )

        # 11. Trust LLM's provider choice
        if llm_provider and llm_provider != self.DEFAULT_PROVIDER:
            corrected, reason = _correct_coingecko(llm_provider, query, indicators)
            return self._create_decision(
                provider=corrected,
                confidence=0.60,
                match_type="llm",
                reasoning=reason or f"Using LLM suggested provider: {llm_provider}",
            )

        # 12. Default
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

    def _is_crypto_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Check if query is about cryptocurrencies/tokens."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        strong_terms = [
            "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
            "xrp", "ripple", "solana", "cardano", "dogecoin", "litecoin",
            "altcoin", "defi", "nft", "token", "stablecoin", "bnb",
        ]

        if any(re.search(rf"\b{re.escape(term)}\b", combined) for term in strong_terms):
            return True
        if "binance coin" in combined:
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

    def _is_bilateral_trade_query(self, query_lower: str, query: str) -> bool:
        """Detect bilateral trade queries (exports from X to Y, trade between X and Y).

        This is structural: Comtrade is the only provider for bilateral trade flows.
        """
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
        """Handle Canadian-specific routing (structural: StatsCan is Canada-only)."""
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

        # Global indicators → WorldBank (broader coverage than StatsCan)
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

    def _route_by_catalog(
        self,
        indicators: List[str],
        country: Optional[str],
        query: Optional[str] = None,
    ) -> Optional[RoutingDecision]:
        """Route using CatalogService YAML mappings (data-driven, not rules)."""
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

        for term in terms_to_check:
            concept_name = self._catalog_service.find_concept_by_term(term)
            if concept_name:
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
