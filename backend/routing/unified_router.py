"""
Unified Router - Single Entry Point for All Routing Decisions

The LLM now handles all semantic routing (indicator detection, crypto vs.
fiscal classification, US-only indicators, etc.) via the provider capability
matrix in the prompt.

This router retains only STRUCTURAL routing:
1. Explicit provider mention ("from FRED", "using IMF")
2. Exchange rate detection (ExchangeRate-API + BIS for REER/NEER)
3. Bilateral trade detection (Comtrade is the ONLY bilateral trade provider)
4. Catalog concept matching (data-driven YAML lookups, not rules)
5. Country-based defaults (structural membership: EU→Eurostat, US→FRED)
6. LLM provider choice (trust the LLM for everything else)

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
    match_type: str = "default"  # explicit, indicator, country, catalog, region, llm, default
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
    6. Catalog concept match (data-driven YAML lookups)
    7. Country-based routing (US → FRED, EU → Eurostat, etc.)
    8. Multi-country routing
    9. LLM provider choice (trust the LLM for semantic decisions)
    10. Default → WorldBank

    Semantic routing (US-only indicators, crypto detection, indicator
    classification) is handled by the LLM via the provider capability
    matrix in the prompt.  The _correct_coingecko() guard remains as
    a lightweight structural safeguard.
    """

    # Fallback chains when primary provider fails
    FALLBACK_MAP: Dict[str, List[str]] = {
        "OECD": ["WorldBank", "Eurostat"],
        "EUROSTAT": ["WorldBank", "IMF"],
        "BIS": ["IMF", "WorldBank"],
        "IMF": ["BIS", "WorldBank", "OECD"],
        "STATSCAN": ["WorldBank", "OECD"],
        "FRED": ["WorldBank", "OECD"],
        "COMTRADE": ["Eurostat", "WorldBank"],
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

        # Detect regional context (EU/Europe, OECD, etc.) from the query.
        # This feeds into catalog routing so coverage-specific providers
        # (e.g., Eurostat for EU) are preferred over global defaults.
        detected_region = self._detect_region_context(query_lower)

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

        # 5. Regional group routing (EU countries, OECD countries, etc.)
        #    Moved BEFORE catalog so regional context overrides catalog defaults.
        regional_decision = self._route_by_regional_group(query_lower)
        if regional_decision:
            return regional_decision

        # 6. Catalog concept match (data-driven YAML lookups)
        #    Pass detected region AND countries so coverage-specific providers
        #    are preferred (e.g. StatsCan for CA, Eurostat for EU).
        if self._use_catalog and self._catalog_service:
            catalog_decision = self._route_by_catalog(
                indicators, country, query=query,
                region_context=detected_region,
                countries=countries,
            )
            if catalog_decision:
                return catalog_decision

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

        # 10. Default
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

    def _is_bilateral_trade_query(self, query_lower: str, query: str) -> bool:
        """Detect bilateral trade queries (exports from X to Y, trade between X and Y).

        This is structural: Comtrade is the only provider for bilateral trade flows.

        IMPORTANT: Aggregate trade indicators (import/export share of GDP, trade as % of GDP)
        are macro indicators from WorldBank/IMF, NOT bilateral trade flows. These must NOT
        match here so they can fall through to catalog routing.
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
        4. Catalog-aware routing: if the catalog concept lists StatsCan as a
           provider with coverage [CA], prefer StatsCan (country-specific source
           with higher frequency/timeliness). Otherwise fall back to the best
           global provider from the catalog.
        5. Development-only indicators (no StatsCan coverage) → WorldBank
        6. Default → StatsCan
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

        # Catalog-aware routing: check if StatsCan covers the concept.
        # This replaces the old hardcoded global_indicators list with a
        # data-driven approach. The catalog YAML files are the source of
        # truth for which providers cover which concepts and countries.
        if self._use_catalog and self._catalog_service:
            catalog_decision = self._route_canada_by_catalog(
                indicators, query, combined,
            )
            if catalog_decision:
                return catalog_decision

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

    def _route_canada_by_catalog(
        self,
        indicators: List[str],
        query: str,
        combined: str,
    ) -> Optional[RoutingDecision]:
        """Use the catalog to decide between StatsCan and global providers for Canada.

        For each indicator/query term, find the catalog concept. If the concept
        lists StatsCan as a provider (meaning it has Canada-specific data),
        prefer StatsCan. Otherwise, use the catalog's best provider for CA.

        This is a FRAMEWORK solution: as new concepts are added to catalog YAML
        files with StatsCan entries, they automatically route correctly without
        any code changes.
        """
        if not self._catalog_service:
            return None

        # Build candidate terms: parsed indicators + raw query
        terms_to_check = list(indicators) if indicators else []
        query_clean = query.strip()
        if query_clean and query_clean not in terms_to_check:
            terms_to_check.append(query_clean)

        if not terms_to_check:
            return None

        for term in terms_to_check:
            concept_name = self._catalog_service.find_concept_by_term(term)
            if not concept_name:
                continue

            # Check if StatsCan is listed as a provider for this concept
            statscan_available = self._catalog_service.is_provider_available(
                concept_name, "StatsCan"
            )

            if statscan_available:
                # StatsCan has this concept — prefer it for Canada queries
                code = self._catalog_service.get_indicator_code(
                    concept_name, "StatsCan", "primary"
                )
                logger.info(
                    f"📚 Canada catalog match: {term} → {concept_name} → StatsCan"
                    f" (code: {code})"
                )
                return self._create_decision(
                    provider="StatsCan",
                    confidence=0.88,
                    match_type="catalog",
                    matched_pattern=f"catalog:{concept_name}:StatsCan",
                    reasoning=(
                        f"Catalog lookup: Canada + {concept_name} → StatsCan "
                        f"(country-specific source, code: {code})"
                    ),
                )

            # Concept exists but StatsCan doesn't have it — fall through
            # to StatsCan default instead of redirecting to a global provider.
            # StatsCan has 40K+ tables; the indicator resolver's FTS5/embedding
            # search will find the right table even without a catalog entry.
            logger.info(
                f"📚 Canada catalog match: {term} → {concept_name} — "
                f"StatsCan not in catalog, falling through to StatsCan default"
            )
            return None

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

    @staticmethod
    def _detect_region_context(query_lower: str) -> Optional[str]:
        """Detect regional context from query text.

        Returns a region tag (``"eu"``, ``"oecd"``, etc.) when the query
        mentions a region/bloc but not a specific country.  This allows
        downstream catalog routing to prefer region-specific providers
        (e.g. Eurostat for EU queries, OECD for OECD-group queries).
        """
        # EU / Europe / Eurozone
        if re.search(r"\b(?:eu|euro(?:pe|pean|zone|stat)?)\b", query_lower):
            return "eu"
        # OECD
        if re.search(r"\boecd\b", query_lower):
            return "oecd"
        return None

    @staticmethod
    def _region_to_representative_countries(region: str) -> Optional[List[str]]:
        """Map a detected region tag to a representative country list.

        The catalog's ``get_best_provider`` uses country context to apply
        coverage bonuses.  By supplying a representative EU member, we
        nudge the catalog toward Eurostat (coverage=eu_members) instead
        of WorldBank (coverage=global).
        """
        if region == "eu":
            # DE (Germany) is an EU member; any single EU member works.
            return ["DE"]
        if region == "oecd":
            # Use a non-EU OECD member so the catalog prefers OECD-coverage
            # providers over EU-specific ones.
            return ["AU"]
        return None

    def _route_by_catalog(
        self,
        indicators: List[str],
        country: Optional[str],
        query: Optional[str] = None,
        region_context: Optional[str] = None,
        countries: Optional[List[str]] = None,
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
                # Merge single country and countries list into one list
                # so the catalog can apply coverage bonuses for ALL detected
                # countries (e.g. StatsCan for CA, Eurostat for EU).
                countries_list: Optional[List[str]] = None
                if country:
                    countries_list = [country]
                if countries:
                    countries_list = list(dict.fromkeys(
                        (countries_list or []) + list(countries)
                    ))

                # If no explicit country but a region was detected, use a
                # representative country so the catalog applies coverage bonuses
                # (e.g. Eurostat for EU queries).
                if not countries_list and region_context:
                    countries_list = self._region_to_representative_countries(region_context)

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
