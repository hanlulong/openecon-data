"""
Unified Router - Single Entry Point for All Routing Decisions

This module consolidates routing logic from:
- provider_router.py (deterministic rules)
- deep_agent_orchestrator.py (scoring-based selection)
- catalog_service.py (YAML-based indicator mappings)

Components used:
- CountryResolver: Country normalization and region membership
- KeywordMatcher: Pattern detection for providers and indicators
- CatalogService: Indicator-to-provider mappings from YAML

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
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from .country_resolver import CountryResolver
from .keyword_matcher import KeywordMatcher

logger = logging.getLogger(__name__)


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
        "IMF": ["WorldBank", "OECD"],
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
        query_lower = query.lower()

        # Priority 1: Explicit provider mention (ABSOLUTE HIGHEST)
        match = KeywordMatcher.detect_explicit_provider(query)
        if match and match.provider:
            return self._create_decision(
                provider=match.provider,
                confidence=match.confidence,
                match_type=match.match_type,
                matched_pattern=match.matched_keyword,
                reasoning=match.reasoning,
            )

        # Priority 2: US-only indicators (MUST use FRED)
        match = KeywordMatcher.detect_us_only_indicator(query, indicators)
        if match and match.provider:
            return self._create_decision(
                provider=match.provider,
                confidence=match.confidence,
                match_type=match.match_type,
                matched_pattern=match.matched_keyword,
                reasoning=match.reasoning,
            )

        # Priority 3: Special case handlers (before keyword matching)

        # 3a: Exchange rate queries → ExchangeRate
        if self._is_exchange_rate_query(query_lower, indicators):
            # Exception: Real/Nominal effective exchange rate (REER/NEER) → IMF or BIS
            if "real effective exchange rate" in query_lower or "reer" in query_lower:
                return self._create_decision(
                    provider="IMF",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="real effective exchange rate",
                    reasoning="Real effective exchange rate (REER) is best sourced from IMF",
                )
            if "nominal effective exchange rate" in query_lower or "neer" in query_lower:
                return self._create_decision(
                    provider="BIS",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="nominal effective exchange rate",
                    reasoning="Nominal effective exchange rate (NEER) is best sourced from BIS",
                )
            return self._create_decision(
                provider="ExchangeRate",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="exchange rate",
                reasoning="Exchange rate query routed to ExchangeRate-API",
            )

        # 3b: Trade as % of GDP → WorldBank (NOT Comtrade)
        if self._is_trade_ratio_query(query_lower, indicators):
            return self._create_decision(
                provider="WorldBank",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="trade % of GDP",
                reasoning="Trade as percentage of GDP is a development indicator from WorldBank",
            )

        # 3c: US trade balance (without partner) → FRED
        if self._is_us_trade_balance_no_partner(query_lower, country):
            return self._create_decision(
                provider="FRED",
                confidence=0.90,
                match_type="indicator",
                matched_pattern="US trade balance",
                reasoning="US trade balance (without partner) uses FRED BOPGSTB series",
            )

        # 3d: Canadian query handling
        if CountryResolver.is_canadian_region(query):
            return self._handle_canadian_query(query_lower, indicators, country)

        # 3e: Property/house prices → BIS
        if self._is_property_price_query(query_lower, indicators):
            return self._create_decision(
                provider="BIS",
                confidence=0.88,
                match_type="indicator",
                matched_pattern="property prices",
                reasoning="Property/house prices best sourced from BIS",
            )

        # Priority 4: Keyword-based indicator patterns
        match = KeywordMatcher.detect_indicator_provider(query, indicators)
        if match and match.provider:
            return self._create_decision(
                provider=match.provider,
                confidence=match.confidence,
                match_type=match.match_type,
                matched_pattern=match.matched_keyword,
                reasoning=match.reasoning,
            )

        # Priority 5: Regional query detection
        match = KeywordMatcher.detect_regional_provider(query)
        if match and match.provider:
            return self._create_decision(
                provider=match.provider,
                confidence=match.confidence,
                match_type=match.match_type,
                matched_pattern=match.matched_keyword,
                reasoning=match.reasoning,
            )

        # Priority 6: Country-based routing
        country_decision = self._route_by_country(country, countries, query_lower, indicators)
        if country_decision:
            return country_decision

        # Priority 7: CatalogService lookup (if available)
        if self._use_catalog and self._catalog_service:
            catalog_decision = self._route_by_catalog(indicators, country)
            if catalog_decision:
                return catalog_decision

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
            corrected, reason = KeywordMatcher.correct_coingecko_misrouting(
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

    def _is_us_trade_balance_no_partner(self, query_lower: str, country: Optional[str]) -> bool:
        """Check if query is US trade balance without partner country."""
        is_us = (
            (country and country.upper() in ["US", "USA", "UNITED STATES"]) or
            any(term in query_lower for term in ["us ", "u.s.", "united states", "america"])
        )
        is_trade_balance = any(term in query_lower for term in [
            "trade balance", "trade deficit", "trade surplus"
        ])
        has_partner = any(term in query_lower for term in [
            " to ", " from ", " with ", " between ", "bilateral", "partner"
        ])

        return is_us and is_trade_balance and not has_partner

    def _is_property_price_query(self, query_lower: str, indicators: List[str]) -> bool:
        """Check if query is about property/house prices."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        return any(term in combined for term in [
            "property price", "house price", "property prices", "house prices",
            "housing price", "real estate price"
        ])

    def _handle_canadian_query(
        self,
        query_lower: str,
        indicators: List[str],
        country: Optional[str]
    ) -> RoutingDecision:
        """Handle Canadian-specific routing."""
        indicators_str = " ".join(indicators).lower()
        combined = f"{query_lower} {indicators_str}"

        # Check for trade queries
        is_trade = any(term in combined for term in ["import", "export", "trade"])

        if is_trade:
            # Bilateral trade with partner → Comtrade
            has_partner = any(term in query_lower for term in [
                " to ", " from ", " with ", " between ", "bilateral", "partner"
            ])
            if has_partner:
                return self._create_decision(
                    provider="Comtrade",
                    confidence=0.90,
                    match_type="indicator",
                    matched_pattern="Canadian bilateral trade",
                    reasoning="Canadian bilateral trade (with partner) → Comtrade",
                )
            # Trade balance (no partner) → WorldBank
            if "trade balance" in combined:
                return self._create_decision(
                    provider="WorldBank",
                    confidence=0.85,
                    match_type="indicator",
                    matched_pattern="Canadian trade balance",
                    reasoning="Canadian trade balance (no partner) → WorldBank",
                )
            # Exports/Imports (no partner) → StatsCan
            return self._create_decision(
                provider="StatsCan",
                confidence=0.85,
                match_type="indicator",
                matched_pattern="Canadian imports/exports",
                reasoning="Canadian imports/exports (no partner) → StatsCan",
            )

        # Default for Canadian queries → StatsCan
        return self._create_decision(
            provider="StatsCan",
            confidence=0.85,
            match_type="country",
            matched_pattern="Canada",
            reasoning="Canadian query routed to StatsCan",
        )

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

        # OECD non-EU → OECD for OECD-specific indicators
        if CountryResolver.is_oecd_non_eu(country):
            indicators_str = " ".join(indicators).lower()
            has_oecd_indicator = any(term in indicators_str or term in query_lower for term in [
                "unemployment", "employment", "labor force", "productivity",
                "education", "health expenditure", "r&d", "tax revenue",
                "gini", "income inequality", "social spending", "pension"
            ])
            if has_oecd_indicator:
                return self._create_decision(
                    provider="OECD",
                    confidence=0.75,
                    match_type="indicator",
                    reasoning=f"OECD non-EU country ({country}) with OECD indicator",
                )
            # Fall through to default

        return None

    def _route_by_catalog(
        self,
        indicators: List[str],
        country: Optional[str],
    ) -> Optional[RoutingDecision]:
        """Route using CatalogService YAML mappings."""
        if not self._catalog_service or not indicators:
            return None

        # Try to find a matching concept in catalog
        for indicator in indicators:
            # CatalogService functions are module-level
            concept_name = self._catalog_service.find_concept_by_term(indicator)
            if concept_name:
                # Normalize country to list format for catalog
                countries_list = [country] if country else None

                provider, code, confidence = self._catalog_service.get_best_provider(
                    concept_name,
                    countries=countries_list,
                )
                if provider and confidence > 0.5:
                    logger.info(f"📚 Catalog match: {indicator} → {concept_name} → {provider}")
                    return self._create_decision(
                        provider=provider,
                        confidence=confidence,
                        match_type="catalog",
                        matched_pattern=f"catalog:{concept_name}",
                        reasoning=f"Catalog lookup: {concept_name} → {provider} (code: {code})",
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
