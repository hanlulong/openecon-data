"""
Baseline Routing Tests for UnifiedRouter

These tests verify that the consolidated UnifiedRouter produces correct
routing decisions for various query patterns extracted from:
- provider_router.py test cases
- deep_agent_orchestrator.py capabilities
- Known production queries

Run with: pytest backend/routing/tests/ -v
"""

import pytest
from ..unified_router import UnifiedRouter, RoutingDecision
from ..country_resolver import CountryResolver
from ..keyword_matcher import KeywordMatcher


class TestCountryResolver:
    """Tests for CountryResolver."""

    def test_normalize_us_variations(self):
        """Test US country name normalization."""
        assert CountryResolver.normalize("US") == "US"
        assert CountryResolver.normalize("USA") == "US"
        assert CountryResolver.normalize("United States") == "US"
        assert CountryResolver.normalize("america") == "US"
        assert CountryResolver.normalize("u.s.") == "US"

    def test_normalize_uk_variations(self):
        """Test UK country name normalization."""
        assert CountryResolver.normalize("UK") == "GB"
        assert CountryResolver.normalize("United Kingdom") == "GB"
        assert CountryResolver.normalize("Britain") == "GB"

    def test_oecd_membership(self):
        """Test OECD membership checks."""
        assert CountryResolver.is_oecd_member("US") is True
        assert CountryResolver.is_oecd_member("Japan") is True
        assert CountryResolver.is_oecd_member("Germany") is True
        assert CountryResolver.is_oecd_member("China") is False
        assert CountryResolver.is_oecd_member("India") is False

    def test_eu_membership(self):
        """Test EU membership checks."""
        assert CountryResolver.is_eu_member("Germany") is True
        assert CountryResolver.is_eu_member("France") is True
        assert CountryResolver.is_eu_member("UK") is False  # Brexit
        assert CountryResolver.is_eu_member("US") is False
        assert CountryResolver.is_eu_member("Norway") is False

    def test_non_oecd_major(self):
        """Test non-OECD major economy checks."""
        assert CountryResolver.is_non_oecd_major("China") is True
        assert CountryResolver.is_non_oecd_major("India") is True
        assert CountryResolver.is_non_oecd_major("Brazil") is True
        assert CountryResolver.is_non_oecd_major("US") is False
        assert CountryResolver.is_non_oecd_major("Germany") is False

    def test_canadian_region_detection(self):
        """Test Canadian region detection."""
        assert CountryResolver.is_canadian_region("Canada GDP growth") is True
        assert CountryResolver.is_canadian_region("Ontario unemployment") is True
        assert CountryResolver.is_canadian_region("Toronto housing prices") is True
        assert CountryResolver.is_canadian_region("US GDP growth") is False

    def test_get_regions(self):
        """Test region membership listing."""
        us_regions = CountryResolver.get_regions("US")
        assert "OECD" in us_regions
        assert "G7" in us_regions
        assert "G20" in us_regions

        germany_regions = CountryResolver.get_regions("Germany")
        assert "OECD" in germany_regions
        assert "EU" in germany_regions
        assert "G7" in germany_regions

        china_regions = CountryResolver.get_regions("China")
        assert "Emerging" in china_regions
        assert "BRICS" in china_regions
        assert "G20" in china_regions


class TestKeywordMatcher:
    """Tests for KeywordMatcher."""

    def test_explicit_provider_detection(self):
        """Test explicit provider keyword detection."""
        # FRED
        result = KeywordMatcher.detect_explicit_provider("Get GDP from FRED")
        assert result is not None
        assert result.provider == "FRED"

        # World Bank
        result = KeywordMatcher.detect_explicit_provider("World Bank poverty data")
        assert result is not None
        assert result.provider == "WorldBank"

        # IMF
        result = KeywordMatcher.detect_explicit_provider("Get data from IMF")
        assert result is not None
        assert result.provider == "IMF"

    def test_start_of_query_provider(self):
        """Test provider detection at start of query."""
        result = KeywordMatcher.detect_explicit_provider("OECD GDP for Italy")
        assert result is not None
        assert result.provider == "OECD"

        # But not "OECD countries" - should return None
        result = KeywordMatcher.detect_explicit_provider("OECD countries GDP comparison")
        assert result is None

    def test_us_only_indicators(self):
        """Test US-only indicator detection."""
        # Case-Shiller
        result = KeywordMatcher.detect_us_only_indicator("Case-Shiller home prices", [])
        assert result is not None
        assert result.provider == "FRED"

        # Federal funds
        result = KeywordMatcher.detect_us_only_indicator("Federal funds rate history", [])
        assert result is not None
        assert result.provider == "FRED"

        # Non-US indicator
        result = KeywordMatcher.detect_us_only_indicator("Germany GDP growth", [])
        assert result is None

    def test_indicator_provider_detection(self):
        """Test indicator-based provider detection."""
        # Trade query → Comtrade
        result = KeywordMatcher.detect_indicator_provider("US exports to China", [])
        assert result is not None
        assert result.provider == "Comtrade"

        # Fiscal query → IMF
        result = KeywordMatcher.detect_indicator_provider("Government debt as percentage of GDP", [])
        assert result is not None
        assert result.provider == "IMF"

        # Development query → WorldBank
        result = KeywordMatcher.detect_indicator_provider("Life expectancy in Africa", [])
        assert result is not None
        assert result.provider == "WorldBank"

    def test_regional_provider_detection(self):
        """Test regional keyword detection."""
        # OECD countries
        result = KeywordMatcher.detect_regional_provider("GDP across OECD countries")
        assert result is not None
        assert result.provider == "OECD"

        # EU countries
        result = KeywordMatcher.detect_regional_provider("Unemployment in EU countries")
        assert result is not None
        assert result.provider == "Eurostat"

        # Developing countries
        result = KeywordMatcher.detect_regional_provider("Poverty in developing countries")
        assert result is not None
        assert result.provider == "WorldBank"

    def test_coingecko_misrouting_correction(self):
        """Test CoinGecko misrouting correction."""
        # Fiscal query misrouted to CoinGecko should be corrected to IMF
        corrected, reason = KeywordMatcher.correct_coingecko_misrouting(
            "CoinGecko", "government deficit forecast", ["fiscal deficit"]
        )
        assert corrected == "IMF"
        assert reason is not None

        # Actual crypto query should stay at CoinGecko
        corrected, reason = KeywordMatcher.correct_coingecko_misrouting(
            "CoinGecko", "bitcoin price", ["bitcoin"]
        )
        assert corrected == "CoinGecko"
        assert reason is None


class TestUnifiedRouter:
    """Tests for UnifiedRouter."""

    @pytest.fixture
    def router(self):
        return UnifiedRouter()

    # ==========================================================================
    # Explicit Provider Tests
    # ==========================================================================

    def test_explicit_fred(self, router):
        """Explicit FRED request."""
        decision = router.route("Get US GDP from FRED")
        assert decision.provider == "FRED"
        assert decision.match_type == "explicit"
        assert decision.confidence >= 0.9

    def test_explicit_world_bank(self, router):
        """Explicit World Bank request."""
        decision = router.route("World Bank poverty statistics")
        assert decision.provider == "WorldBank"
        assert decision.match_type == "explicit"

    def test_explicit_imf(self, router):
        """Explicit IMF request."""
        decision = router.route("Get debt data from IMF")
        assert decision.provider == "IMF"
        assert decision.match_type == "explicit"

    # ==========================================================================
    # US-Only Indicator Tests
    # ==========================================================================

    def test_case_shiller_routes_to_fred(self, router):
        """Case-Shiller index must use FRED."""
        decision = router.route("Case-Shiller home price index")
        assert decision.provider == "FRED"

    def test_federal_funds_routes_to_fred(self, router):
        """Federal funds rate must use FRED."""
        decision = router.route("Federal funds rate history")
        assert decision.provider == "FRED"

    def test_sp500_routes_to_fred(self, router):
        """S&P 500 must use FRED."""
        decision = router.route("S&P 500 historical data")
        assert decision.provider == "FRED"

    # ==========================================================================
    # Trade Query Tests
    # ==========================================================================

    def test_bilateral_trade_routes_to_comtrade(self, router):
        """Bilateral trade queries use Comtrade."""
        decision = router.route("US exports to China")
        assert decision.provider == "Comtrade"

    def test_trade_deficit_with_partner_routes_to_comtrade(self, router):
        """Trade deficit with partner country uses Comtrade."""
        decision = router.route("Trade deficit between US and Mexico")
        assert decision.provider == "Comtrade"

    def test_trade_as_percent_gdp_routes_to_worldbank(self, router):
        """Trade as % of GDP uses WorldBank."""
        decision = router.route("Exports as % of GDP for Germany")
        assert decision.provider == "WorldBank"

    def test_us_trade_balance_no_partner_routes_to_fred(self, router):
        """US trade balance without partner uses FRED."""
        decision = router.route("US trade balance history")
        assert decision.provider == "FRED"

    # ==========================================================================
    # Country-Based Routing Tests
    # ==========================================================================

    def test_us_query_routes_to_fred(self, router):
        """US economic queries use FRED."""
        decision = router.route("US GDP growth", country="US")
        assert decision.provider == "FRED"

    def test_canada_query_routes_to_statscan(self, router):
        """Canadian queries use StatsCan."""
        decision = router.route("Canada unemployment rate")
        assert decision.provider == "StatsCan"

    def test_eu_country_routes_to_eurostat(self, router):
        """EU country queries use Eurostat."""
        decision = router.route("Germany GDP growth", country="Germany")
        assert decision.provider == "Eurostat"

    def test_non_oecd_major_routes_to_worldbank(self, router):
        """Non-OECD major economies use WorldBank."""
        decision = router.route("China GDP growth", country="China")
        assert decision.provider == "WorldBank"

    def test_non_oecd_with_imf_indicator_routes_to_imf(self, router):
        """Non-OECD with debt indicator uses IMF."""
        decision = router.route("China government debt", country="China", indicators=["government debt"])
        assert decision.provider == "IMF"

    # ==========================================================================
    # Exchange Rate Tests
    # ==========================================================================

    def test_exchange_rate_routes_to_exchangerate(self, router):
        """Exchange rate queries use ExchangeRate-API."""
        decision = router.route("USD to EUR exchange rate")
        assert decision.provider == "ExchangeRate"

    def test_forex_routes_to_exchangerate(self, router):
        """Forex queries use ExchangeRate-API."""
        decision = router.route("Current forex rates")
        assert decision.provider == "ExchangeRate"

    def test_reer_routes_to_imf(self, router):
        """Real effective exchange rate uses IMF."""
        decision = router.route("Real effective exchange rate for Japan")
        assert decision.provider == "IMF"

    # ==========================================================================
    # Crypto Tests
    # ==========================================================================

    def test_bitcoin_routes_to_coingecko(self, router):
        """Bitcoin queries use CoinGecko."""
        decision = router.route("Bitcoin price history", indicators=["bitcoin"])
        assert decision.provider == "CoinGecko"

    def test_ethereum_routes_to_coingecko(self, router):
        """Ethereum queries use CoinGecko."""
        decision = router.route("Ethereum market cap", indicators=["ethereum"])
        assert decision.provider == "CoinGecko"

    # ==========================================================================
    # Property/Housing Tests
    # ==========================================================================

    def test_property_prices_route_to_bis(self, router):
        """Property price queries use BIS."""
        decision = router.route("Property prices in Australia")
        assert decision.provider == "BIS"

    def test_house_prices_route_to_bis(self, router):
        """House price queries use BIS."""
        decision = router.route("House prices in Tokyo")
        assert decision.provider == "BIS"

    # ==========================================================================
    # Fiscal/IMF Tests
    # ==========================================================================

    def test_government_debt_routes_to_imf(self, router):
        """Government debt queries use IMF."""
        decision = router.route("Government debt to GDP ratio", indicators=["government debt"])
        assert decision.provider == "IMF"

    def test_fiscal_deficit_routes_to_imf(self, router):
        """Fiscal deficit queries use IMF."""
        decision = router.route("Fiscal deficit forecast")
        assert decision.provider == "IMF"

    def test_budget_balance_routes_to_imf(self, router):
        """Budget balance queries use IMF."""
        decision = router.route("Government budget balance")
        assert decision.provider == "IMF"

    # ==========================================================================
    # Development Indicator Tests
    # ==========================================================================

    def test_life_expectancy_routes_to_worldbank(self, router):
        """Life expectancy queries use WorldBank."""
        decision = router.route("Life expectancy in Nigeria")
        assert decision.provider == "WorldBank"

    def test_poverty_routes_to_worldbank(self, router):
        """Poverty queries use WorldBank."""
        decision = router.route("Poverty rate in Sub-Saharan Africa")
        assert decision.provider == "WorldBank"

    def test_literacy_routes_to_worldbank(self, router):
        """Literacy queries use WorldBank."""
        decision = router.route("Literacy rate in India")
        assert decision.provider == "WorldBank"

    # ==========================================================================
    # Regional Query Tests
    # ==========================================================================

    def test_oecd_countries_routes_correctly(self, router):
        """OECD countries queries route appropriately."""
        decision = router.route("GDP across OECD countries")
        assert decision.provider == "OECD"

    def test_eu_countries_routes_to_eurostat(self, router):
        """EU countries queries route to Eurostat."""
        decision = router.route("Unemployment in EU countries")
        assert decision.provider == "Eurostat"

    def test_developing_countries_routes_to_worldbank(self, router):
        """Developing countries queries route to WorldBank."""
        decision = router.route("GDP growth in developing countries")
        assert decision.provider == "WorldBank"

    # ==========================================================================
    # Canadian Query Tests
    # ==========================================================================

    def test_canada_bilateral_trade_routes_to_comtrade(self, router):
        """Canadian bilateral trade uses Comtrade."""
        decision = router.route("Canada exports to US")
        assert decision.provider == "Comtrade"

    def test_canada_trade_balance_routes_to_statscan(self, router):
        """Canadian trade balance uses StatsCan."""
        decision = router.route("Canada trade balance")
        assert decision.provider == "StatsCan"

    def test_canada_exports_no_partner_routes_to_statscan(self, router):
        """Canadian exports (no partner) uses StatsCan."""
        decision = router.route("Canada total exports")
        assert decision.provider == "StatsCan"

    def test_canada_residential_property_routes_to_bis(self, router):
        """Canadian residential property market queries use BIS."""
        decision = router.route("Canada residential property prices 2015-2024")
        assert decision.provider == "BIS"

    def test_goods_exports_without_partner_route_to_comtrade(self, router):
        """Goods export flow queries route to Comtrade even without explicit partner."""
        decision = router.route("Mexico auto parts exports 2018-2023")
        assert decision.provider == "Comtrade"

    def test_global_trade_volume_routes_to_imf(self, router):
        """Global trade volume growth queries use IMF."""
        decision = router.route("World trade volume growth 2018-2023")
        assert decision.provider == "IMF"

    def test_forecast_queries_route_to_imf(self, router):
        """Projection/forecast queries use IMF."""
        decision = router.route("Eurozone GDP growth projections 2024-2026")
        assert decision.provider == "IMF"

    def test_eu_country_government_debt_routes_to_eurostat(self, router):
        """Historical EU-country macro debt query should use Eurostat."""
        decision = router.route("Italy government debt 2015-2023")
        assert decision.provider == "Eurostat"

    # ==========================================================================
    # Fallback Tests
    # ==========================================================================

    def test_fallbacks_for_oecd(self, router):
        """OECD fallbacks include WorldBank."""
        fallbacks = router.get_fallbacks("OECD")
        assert "WorldBank" in fallbacks

    def test_fallbacks_for_eurostat(self, router):
        """Eurostat fallbacks include WorldBank."""
        fallbacks = router.get_fallbacks("Eurostat")
        assert "WorldBank" in fallbacks

    def test_fallbacks_for_bis(self, router):
        """BIS fallbacks include IMF."""
        fallbacks = router.get_fallbacks("BIS")
        assert "IMF" in fallbacks


class TestRoutingDecisionConfidence:
    """Tests for routing decision confidence levels."""

    @pytest.fixture
    def router(self):
        return UnifiedRouter()

    def test_explicit_provider_high_confidence(self, router):
        """Explicit provider mentions have high confidence."""
        decision = router.route("Get data from FRED")
        assert decision.confidence >= 0.9

    def test_us_only_indicator_high_confidence(self, router):
        """US-only indicators have high confidence."""
        decision = router.route("Federal funds rate")
        assert decision.confidence >= 0.9

    def test_keyword_match_medium_confidence(self, router):
        """Keyword matches have medium-high confidence."""
        decision = router.route("Life expectancy trends")
        assert decision.confidence >= 0.7

    def test_default_provider_low_confidence(self, router):
        """Default provider has lower confidence."""
        decision = router.route("Some random economic data")
        assert decision.confidence <= 0.6


# ==========================================================================
# Baseline Queries from Production
# ==========================================================================

class TestProductionQueries:
    """Test routing for known production query patterns."""

    @pytest.fixture
    def router(self):
        return UnifiedRouter()

    # Economic indicators
    ECONOMIC_QUERIES = [
        ("US GDP growth last 5 years", "FRED"),
        ("Germany unemployment rate", "Eurostat"),
        ("Japan inflation rate", "OECD"),
        ("China GDP growth", "WorldBank"),
        ("Brazil inflation rate", "WorldBank"),
    ]

    # Trade queries
    TRADE_QUERIES = [
        ("US exports to China", "Comtrade"),
        ("Germany imports from France", "Comtrade"),
        ("Trade deficit between US and Mexico", "Comtrade"),
        ("Exports as % of GDP for Germany", "WorldBank"),
        ("US trade balance history", "FRED"),
    ]

    # Financial queries
    FINANCIAL_QUERIES = [
        ("S&P 500 performance", "FRED"),
        ("Bitcoin price", "CoinGecko"),
        ("USD to EUR exchange rate", "ExchangeRate"),
        ("Government debt to GDP ratio", "IMF"),
        ("House prices in Australia", "BIS"),
    ]

    @pytest.mark.parametrize("query,expected_provider", ECONOMIC_QUERIES)
    def test_economic_queries(self, router, query, expected_provider):
        """Test economic indicator routing."""
        decision = router.route(query)
        assert decision.provider == expected_provider, \
            f"Query '{query}' routed to {decision.provider}, expected {expected_provider}"

    @pytest.mark.parametrize("query,expected_provider", TRADE_QUERIES)
    def test_trade_queries(self, router, query, expected_provider):
        """Test trade data routing."""
        decision = router.route(query)
        assert decision.provider == expected_provider, \
            f"Query '{query}' routed to {decision.provider}, expected {expected_provider}"

    @pytest.mark.parametrize("query,expected_provider", FINANCIAL_QUERIES)
    def test_financial_queries(self, router, query, expected_provider):
        """Test financial data routing."""
        decision = router.route(query)
        assert decision.provider == expected_provider, \
            f"Query '{query}' routed to {decision.provider}, expected {expected_provider}"
