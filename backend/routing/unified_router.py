"""
Unified Router - Single Entry Point for All Routing Decisions

The LLM now handles semantic routing (indicator detection, crypto vs.
fiscal classification, US-only indicators, etc.) via the provider capability
matrix in the prompt.

This router retains only final-authority STRUCTURAL routing:
1. Explicit provider mention ("from FRED", "using IMF")
2. Exchange rate detection (ExchangeRate-API + BIS for REER/NEER)
3. Bilateral trade detection (Comtrade is the ONLY bilateral trade provider)
4. HS commodity-code trade detection (Comtrade)
5. LLM provider choice (trust the LLM for semantic provider selection)

Country, region, and broad-topic coverage hints are exposed as candidate
metadata only; they must not override the LLM as final provider authority.

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

_EXPLICIT_PROVIDER_DIRECTIVE_ALIASES: Dict[str, List[str]] = {
    "OECD": ["oecd"],
    "FRED": ["fred", "federal reserve", "st. louis fed", "stlouisfed"],
    "WorldBank": ["world bank", "worldbank"],
    "Comtrade": ["comtrade", "un comtrade", "united nations comtrade"],
    "StatsCan": ["statscan", "statistics canada", "stats canada"],
    "IMF": ["imf", "the imf", "international monetary fund"],
    "BIS": ["bis", "bank for international settlements"],
    "Eurostat": ["eurostat"],
    "ExchangeRate": ["exchangerate", "exchange rate api", "exchange rate-api"],
    "CoinGecko": ["coingecko", "coin gecko"],
}

_START_OF_QUERY_PROVIDERS = ["OECD", "IMF", "BIS", "Eurostat"]
_START_OF_QUERY_EXCLUSIONS = ["countries", "country", "members", "member", "nations", "nation", "average"]


def _provider_alias_pattern(alias: str) -> str:
    """Return a boundary-safe provider alias pattern."""

    return rf"(?<![a-z0-9_-]){re.escape(alias)}(?![a-z0-9_-])"


def detect_explicit_provider_match(query: str) -> Optional[Tuple[str, str]]:
    """Return (provider, matched_keyword) if user explicitly names a provider, else None."""
    query_lower = query.lower()

    directive_matches: List[Tuple[int, str, str]] = []
    for provider, aliases in _EXPLICIT_PROVIDER_DIRECTIVE_ALIASES.items():
        for alias in aliases:
            alias_pattern = _provider_alias_pattern(alias)
            directive_pattern = (
                rf"(?<![a-z0-9_-])"
                rf"(?P<keyword>(?:from|using|use|via)\s+{alias_pattern}|"
                rf"according\s+to\s+{alias_pattern})"
            )
            for match in re.finditer(directive_pattern, query_lower):
                directive_matches.append((match.start(), provider, match.group("keyword")))
    if directive_matches:
        # A syntactic provider directive ("from CoinGecko") is stronger than a
        # bare provider word elsewhere in the title ("FRED Energy").  When a
        # query contains multiple directives, use the later one as the most
        # local source qualifier rather than falling back to provider-map order.
        _, provider, keyword = max(directive_matches, key=lambda item: item[0])
        return provider, keyword

    for provider in _START_OF_QUERY_PROVIDERS:
        provider_lower = provider.lower()
        if query_lower.startswith(provider_lower + " "):
            if not any(term in query_lower[:30] for term in _START_OF_QUERY_EXCLUSIONS):
                return provider, f"{provider} (at start)"

    for provider, keywords in _EXPLICIT_PROVIDER_KEYWORDS.items():
        for keyword in keywords:
            # Match provider aliases as standalone text, not as arbitrary
            # substrings inside provider-native codes/slugs.  For example,
            # CoinGecko has a valid asset id `fredenergy`; the bare FRED alias
            # must not steal `fredenergy from CoinGecko` before the explicit
            # CoinGecko suffix is seen.
            if re.search(_provider_alias_pattern(keyword), query_lower):
                return provider, keyword

    return None



def _correct_coingecko(provider: str, query: str, indicators: List[str]) -> Tuple[str, Optional[str]]:
    """Reject impossible CoinGecko choices without choosing a semantic replacement.

    Returns (corrected_provider, reason_or_None).
    This is a lightweight provider-contract guard: CoinGecko only serves crypto
    assets.  It may reject CoinGecko for obvious non-crypto macro/fiscal queries,
    but it must not decide the replacement provider by keyword.
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
        reason = "CoinGecko rejected: query has macro/fiscal terms but no crypto asset"
        logger.warning(f"  {reason}")
        return "", reason

    return provider, None


@dataclass
class RoutingDecision:
    """Result of a routing decision."""
    provider: str
    confidence: float
    fallbacks: List[str] = field(default_factory=list)
    reasoning: str = ""
    match_type: str = "default"  # explicit, structural, llm, default
    matched_pattern: Optional[str] = None
    decision_source: str = "default"
    semantic_authority: str = "none"
    final_authority: bool = False
    candidate_providers: List[str] = field(default_factory=list)

    @property
    def can_override_llm_provider(self) -> bool:
        """Whether QueryService may replace the LLM provider with this route."""
        return self.final_authority and self.decision_source in {
            "explicit_provider",
            "mechanical_structure",
        }


class UnifiedRouter:
    """
    Single entry point for all provider routing decisions.

    After the no-shortcut migration, this router handles only final-authority
    structural routing:
    1. Explicit provider mention (highest confidence)
    2. Exchange rate → ExchangeRate-API / BIS for REER
    3. HS commodity-code and bilateral trade → Comtrade
    4. LLM provider choice (trust the LLM for semantic decisions)
    5. Default → WorldBank when no provider evidence exists

    Country, regional, and domain coverage hints are candidate metadata only.
    They are not allowed to mutate final provider selection unless a later
    LLM/evidence stage makes an explicit decision.
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
        coverage_candidates = self._coverage_candidates(
            query=query,
            query_lower=query_lower,
            country=country,
            countries=countries,
        )

        # Fallback geography extraction when parser omits country information.
        if not country and not countries:
            detected_countries = CountryResolver.detect_all_countries_in_query(query)
            if len(detected_countries) == 1:
                country = detected_countries[0]
            elif len(detected_countries) > 1:
                countries = detected_countries
            coverage_candidates = self._coverage_candidates(
                query=query,
                query_lower=query_lower,
                country=country,
                countries=countries,
            )

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
                decision_source="explicit_provider",
                semantic_authority="exact_user_input",
                final_authority=True,
                candidate_providers=coverage_candidates,
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
                    match_type="structural",
                    matched_pattern="effective exchange rate",
                    reasoning="Effective exchange rates (REER/NEER) are best sourced from BIS",
                    decision_source="mechanical_structure",
                    final_authority=True,
                    candidate_providers=coverage_candidates,
                )
            return self._create_decision(
                provider="ExchangeRate",
                confidence=0.90,
                match_type="structural",
                matched_pattern="exchange rate",
                reasoning="Exchange rate query routed to ExchangeRate-API",
                decision_source="mechanical_structure",
                final_authority=True,
                candidate_providers=coverage_candidates,
            )

        # 2b. HS commodity code + trade verb → Comtrade (structural: HS codes are Comtrade-specific)
        if self._is_hs_code_trade_query(query_lower):
            hs_match = _HS_CODE_RE.search(query)
            matched_code = hs_match.group(0) if hs_match else "HS code"
            return self._create_decision(
                provider="Comtrade",
                confidence=0.92,
                match_type="structural",
                matched_pattern=f"HS code: {matched_code}",
                reasoning=f"Query contains HS commodity code ({matched_code}), routed to Comtrade",
                decision_source="mechanical_structure",
                final_authority=True,
                candidate_providers=coverage_candidates,
            )

        # 3. Bilateral trade → Comtrade (structural: only bilateral trade provider)
        if self._is_bilateral_trade_query(query_lower, query):
            return self._create_decision(
                provider="Comtrade",
                confidence=0.88,
                match_type="structural",
                matched_pattern="bilateral trade",
                reasoning="Bilateral trade query routed to Comtrade",
                decision_source="mechanical_structure",
                final_authority=True,
                candidate_providers=coverage_candidates,
            )

        # 4. Trust LLM's provider choice for semantic provider decisions.
        if llm_provider and llm_provider.upper() not in {"NOT_AVAILABLE", "NONE", "UNKNOWN"}:
            corrected, reason = _correct_coingecko(llm_provider, query, indicators)
            if corrected:
                return self._create_decision(
                    provider=corrected,
                    confidence=0.60,
                    match_type="llm",
                    reasoning=reason or f"Using LLM suggested provider: {llm_provider}",
                    decision_source="llm_provider",
                    semantic_authority="llm_adjudication",
                    final_authority=True,
                    candidate_providers=coverage_candidates,
                )
            return self._create_decision(
                provider=self.DEFAULT_PROVIDER,
                confidence=0.35,
                match_type="default",
                reasoning=reason or "LLM provider rejected by provider contract; no semantic replacement chosen",
                decision_source="unsupported",
                final_authority=False,
                candidate_providers=coverage_candidates,
            )

        # 5. Default.  Coverage candidates may be present but are not final
        # semantic provider authority.
        return self._create_decision(
            provider=self.DEFAULT_PROVIDER,
            confidence=0.50,
            match_type="default",
            reasoning=f"No explicit or mechanical provider route matched, using default: {self.DEFAULT_PROVIDER}",
            decision_source="default",
            final_authority=False,
            candidate_providers=coverage_candidates,
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
        decision_source: str = "default",
        semantic_authority: str = "none",
        final_authority: bool = False,
        candidate_providers: Optional[List[str]] = None,
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
            decision_source=decision_source,
            semantic_authority=semantic_authority,
            final_authority=final_authority,
            candidate_providers=candidate_providers or [],
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

    def _coverage_candidates(
        self,
        *,
        query: str,
        query_lower: str,
        country: Optional[str],
        countries: Optional[List[str]],
    ) -> List[str]:
        """Return non-authoritative provider coverage hints.

        These hints are intentionally weak evidence.  They help downstream
        candidate/evidence stages know where to search, but they never select
        the final provider on their own.
        """
        candidates: list[str] = []

        def add(provider: str) -> None:
            if provider not in candidates:
                candidates.append(provider)

        if CountryResolver.is_canadian_region(query):
            add("StatsCan")

        fred_terms = ("federal funds", "fed funds", "fomc rate", "st louis fed")
        if any(term in query_lower for term in fred_terms):
            add("FRED")

        trade_flow_terms = (
            "semiconductor", "chip", "chips", "pharmaceutical", "pharmaceuticals",
            "agricultural", "agriculture", "electronics", "electronic",
            "auto parts", "textile", "textiles", "petroleum", "oil",
            "steel", "mineral", "minerals", "soybean", "soybeans",
            "commodity", "commodities", "goods",
        )
        has_trade_flow = (
            not self._is_aggregate_trade_indicator(query_lower)
            and "trade balance" not in query_lower
            and "current account" not in query_lower
            and any(term in query_lower for term in ("import", "imports", "export", "exports"))
            and any(term in query_lower for term in trade_flow_terms)
        )
        if has_trade_flow:
            add("Comtrade")

        property_terms = (
            "residential property", "property prices", "real estate prices",
            "real estate market", "house prices", "housing market index",
            "property price index", "housing price index",
        )
        if any(term in query_lower for term in property_terms):
            add("BIS")

        forecast_terms = ("forecast", "forecasts", "projection", "projections")
        macro_terms = (
            "inflation", "gdp growth", "economic growth", "current account",
            "fiscal deficit", "government debt", "commodity price index",
            "trade volume", "world economic outlook",
        )
        macro_group_terms = (
            "global", "world", "advanced economies", "emerging markets",
            "developing economies", "g20", "emerging economies",
        )
        has_macro = any(term in query_lower for term in macro_terms)
        if has_macro or any(term in query_lower for term in forecast_terms) or any(
            term in query_lower for term in macro_group_terms
        ):
            add("IMF")

        if country and CountryResolver.is_us(country):
            add("FRED")
        if country and CountryResolver.is_eu_member(country):
            add("Eurostat")
        if country and CountryResolver.is_non_oecd_major(country):
            add("WorldBank")
        if countries and any(CountryResolver.is_non_oecd_major(c) for c in countries):
            add("WorldBank")

        if re.search(r"\beu\b", query_lower) or re.search(r"\beuro(?:pe|pean|zone)\b", query_lower):
            add("Eurostat")
        if "oecd" in query_lower or "g7" in query_lower:
            add("OECD")
        if any(term in query_lower for term in ["developing countries", "emerging markets", "g20 countries"]):
            add("WorldBank")
        if any(term in query_lower for term in ["all provinces", "canadian provinces", "each province", "by province", "provincial data"]):
            add("StatsCan")

        return candidates


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
    return corrected or "NOT_AVAILABLE"


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
