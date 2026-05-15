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
from ..unified_router import (
    UnifiedRouter,
    RoutingDecision,
    detect_explicit_provider_match,
    _correct_coingecko,
)
from ..country_resolver import CountryResolver


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

    def test_normalize_provider_display_country_variations(self):
        """World Bank-style display names should normalize back to ISO2."""
        assert CountryResolver.normalize("Korea, Rep.") == "KR"
        assert CountryResolver.normalize("korea rep") == "KR"

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

    def test_energy_exporter_region_detection_prefers_energy_group(self):
        """Oil-exporter phrasing should map to generalized energy exporters."""
        regions = CountryResolver.detect_regions_in_query("compare debt across oil exporters")
        assert "ENERGY_EXPORTERS" in regions
        assert "OPEC" not in regions

    def test_region_detection_uses_word_boundaries(self):
        """Region keyword matching should avoid substring false positives."""
        regions = CountryResolver.detect_regions_in_query("show specific country inflation")
        assert "PACIFIC_ISLANDS" not in regions


class TestExplicitProviderDetection:
    """Tests for inline explicit provider detection (replaced KeywordMatcher)."""

    def test_explicit_provider_detection(self):
        """Test explicit provider keyword detection."""
        # FRED
        result = detect_explicit_provider_match("Get GDP from FRED")
        assert result is not None
        assert result[0] == "FRED"

        # World Bank
        result = detect_explicit_provider_match("World Bank poverty data")
        assert result is not None
        assert result[0] == "WorldBank"

        # IMF
        result = detect_explicit_provider_match("Get data from IMF")
        assert result is not None
        assert result[0] == "IMF"

    def test_explicit_provider_from_eurostat(self):
        """Test explicit Eurostat detection for various phrasings."""
        result = detect_explicit_provider_match("Poland unemployment from Eurostat")
        assert result is not None
        assert result[0] == "Eurostat"

        result = detect_explicit_provider_match("Germany GDP using Eurostat")
        assert result is not None
        assert result[0] == "Eurostat"

        result = detect_explicit_provider_match("Show me Eurostat data on inflation")
        assert result is not None
        assert result[0] == "Eurostat"

    def test_explicit_provider_all_providers(self):
        """Test that explicit detection works for every provider with 'from X' syntax."""
        cases = [
            ("GDP from FRED", "FRED"),
            ("data from World Bank", "WorldBank"),
            ("trade from Comtrade", "Comtrade"),
            ("GDP from StatsCan", "StatsCan"),
            ("data from IMF", "IMF"),
            ("rates from BIS", "BIS"),
            ("data from Eurostat", "Eurostat"),
        ]
        for query, expected_provider in cases:
            result = detect_explicit_provider_match(query)
            assert result is not None, f"Expected {expected_provider} for '{query}', got None"
            assert result[0] == expected_provider, f"For '{query}': {result[0]} != {expected_provider}"

    def test_explicit_provider_detection_does_not_match_inside_asset_slug(self):
        """Bare provider aliases should not hijack provider-native slug text."""
        result = detect_explicit_provider_match("fredenergy from CoinGecko")

        assert result is not None
        assert result[0] == "CoinGecko"

    def test_explicit_provider_directive_overrides_bare_alias_in_title(self):
        """A source directive is stronger than a provider word inside an asset/title."""
        result = detect_explicit_provider_match("FRED Energy cryptocurrency price from CoinGecko")

        assert result is not None
        assert result == ("CoinGecko", "from coingecko")

    def test_later_provider_directive_overrides_earlier_bare_alias(self):
        """Provider directives should not depend on provider dictionary order."""
        result = detect_explicit_provider_match("FRED data from World Bank")

        assert result is not None
        assert result == ("WorldBank", "from world bank")

    def test_start_of_query_provider(self):
        """Test provider detection at start of query."""
        result = detect_explicit_provider_match("OECD GDP for Italy")
        assert result is not None
        assert result[0] == "OECD"

        # But not "OECD countries" - should return None
        result = detect_explicit_provider_match("OECD countries GDP comparison")
        assert result is None

    def test_coingecko_misrouting_correction(self):
        """Test CoinGecko misrouting correction."""
        corrected, reason = _correct_coingecko(
            "CoinGecko", "government deficit forecast", ["fiscal deficit"]
        )
        assert corrected == ""
        assert reason is not None

        corrected, reason = _correct_coingecko(
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

    def test_explicit_eurostat(self, router):
        """Explicit Eurostat request routes to Eurostat, not WorldBank."""
        decision = router.route("Poland unemployment from Eurostat")
        assert decision.provider == "Eurostat"
        assert decision.match_type == "explicit"
        assert decision.confidence >= 0.9

    def test_explicit_eurostat_various_phrasings(self, router):
        """All Eurostat explicit phrasings route correctly."""
        queries = [
            "Germany GDP from Eurostat",
            "France inflation using Eurostat",
            "Italy unemployment via Eurostat",
            "Show me Eurostat data on GDP",
        ]
        for q in queries:
            decision = router.route(q)
            assert decision.provider == "Eurostat", f"'{q}' routed to {decision.provider}, expected Eurostat"
            assert decision.match_type == "explicit"

    def test_explicit_bis(self, router):
        """Explicit BIS request."""
        decision = router.route("Policy rate from BIS")
        assert decision.provider == "BIS"
        assert decision.match_type == "explicit"

    def test_explicit_coingecko_slug_containing_fred_routes_to_coingecko(self, router):
        """CoinGecko provider suffix wins even when the asset slug starts with fred."""
        decision = router.route("fredenergy from CoinGecko")

        assert decision.provider == "CoinGecko"
        assert decision.match_type == "explicit"

    def test_explicit_coingecko_suffix_overrides_fred_asset_name(self, router):
        """CoinGecko suffix wins when the asset's display name contains FRED."""
        decision = router.route("FRED Energy cryptocurrency price from CoinGecko")

        assert decision.provider == "CoinGecko"
        assert decision.match_type == "explicit"
        assert decision.matched_pattern == "from coingecko"
        assert decision.semantic_authority == "exact_user_input"

    # ==========================================================================
    # US-Only Indicator Tests (LLM handles semantic routing; router defers)
    # ==========================================================================

    def test_case_shiller_routes_via_catalog_or_llm(self, router):
        """Case-Shiller: catalog may match house prices (BIS); LLM handles FRED routing."""
        decision = router.route("Case-Shiller home price index", llm_provider="FRED")
        # Catalog matches "home price" to house_prices concept → BIS; LLM would pick FRED
        assert decision.provider in ("FRED", "BIS")

    def test_federal_funds_routes_via_catalog_or_llm(self, router):
        """Federal funds rate is a structural FRED cue."""
        decision = router.route("Federal funds rate history", llm_provider="FRED")
        assert decision.provider == "FRED"

    def test_sp500_routes_to_fred_via_llm(self, router):
        """S&P 500: no catalog match, LLM sets provider=FRED; router trusts LLM."""
        decision = router.route("S&P 500 historical data", llm_provider="FRED")
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

    def test_unilateral_goods_trade_routes_to_comtrade(self, router):
        """Unilateral goods flows are semantic provider choices, not structural routes."""
        decision = router.route("Japan semiconductor exports 2020-2023")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_us_trade_balance_no_partner_routes_to_fred(self, router):
        """US trade balance without partner does not force FRED without LLM evidence."""
        decision = router.route("US trade balance history")
        assert decision.provider == "WorldBank"
        assert "FRED" in decision.candidate_providers
        assert decision.final_authority is False

    # ==========================================================================
    # Country-Based Routing Tests
    # ==========================================================================

    def test_us_query_routes_to_fred(self, router):
        """US scope is a coverage hint, not final provider authority."""
        decision = router.route("US GDP growth", country="US")
        assert decision.provider == "WorldBank"
        assert "FRED" in decision.candidate_providers
        assert decision.final_authority is False

    def test_canada_query_routes_to_statscan(self, router):
        """Canadian scope is a coverage hint, not final provider authority."""
        decision = router.route("Canada unemployment rate")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.final_authority is False

    def test_eu_country_routes_to_eurostat_or_catalog(self, router):
        """EU country queries use Eurostat (country routing) or catalog provider.

        After Phase 3 LLM-refactor, catalog may match first (e.g., GDP → WorldBank).
        Both are valid; the LLM handles semantic refinement.
        """
        decision = router.route("Germany GDP growth", country="Germany")
        assert decision.provider in ("Eurostat", "WorldBank")

    def test_non_oecd_major_routes_to_worldbank(self, router):
        """Non-OECD major economies use WorldBank."""
        decision = router.route("China GDP growth", country="China")
        assert decision.provider == "WorldBank"

    def test_non_oecd_with_imf_indicator_routes_to_imf(self, router):
        """Debt wording alone cannot force IMF without LLM/evidence."""
        decision = router.route("China government debt", country="China", indicators=["government debt"])
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

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

    def test_reer_routes_to_bis(self, router):
        """Real effective exchange rate uses BIS (WS_XRU/WS_EER datasets)."""
        decision = router.route("Real effective exchange rate for Japan")
        assert decision.provider == "BIS"

    # ==========================================================================
    # Crypto Tests (LLM handles crypto detection; router trusts LLM + guard)
    # ==========================================================================

    def test_bitcoin_routes_to_coingecko_via_llm(self, router):
        """Bitcoin: LLM sets provider=CoinGecko; router trusts it."""
        decision = router.route("Bitcoin price history", indicators=["bitcoin"], llm_provider="CoinGecko")
        assert decision.provider == "CoinGecko"

    def test_ethereum_routes_to_coingecko_via_llm(self, router):
        """Ethereum: LLM sets provider=CoinGecko; router trusts it."""
        decision = router.route("Ethereum market cap", indicators=["ethereum"], llm_provider="CoinGecko")
        assert decision.provider == "CoinGecko"

    def test_xrp_routes_to_coingecko_via_llm(self, router):
        """XRP/Ripple: LLM sets provider=CoinGecko; router trusts it."""
        decision = router.route("XRP price performance over the last 6 months", llm_provider="CoinGecko")
        assert decision.provider == "CoinGecko"

    def test_top_crypto_market_cap_routes_to_coingecko_via_llm(self, router):
        """Top crypto market-cap ranking: LLM sets provider=CoinGecko."""
        decision = router.route("Top 10 cryptocurrencies by market cap right now", llm_provider="CoinGecko")
        assert decision.provider == "CoinGecko"

    # ==========================================================================
    # Property/Housing Tests
    # ==========================================================================

    def test_property_prices_route_to_bis(self, router):
        """Property price keywords do not force BIS without LLM/evidence."""
        decision = router.route("Property prices in Australia")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_house_prices_route_to_bis(self, router):
        """House price keywords do not force BIS without LLM/evidence."""
        decision = router.route("House prices in Tokyo")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_real_estate_prices_route_to_bis(self, router):
        """Real estate keywords do not force BIS without LLM/evidence."""
        decision = router.route("US residential property prices 2018-2024")
        assert decision.provider == "WorldBank"
        assert "FRED" in decision.candidate_providers
        assert decision.final_authority is False

    # ==========================================================================
    # Fiscal/IMF Tests
    # ==========================================================================

    def test_government_debt_routes_to_imf(self, router):
        """Government debt wording does not force IMF without LLM/evidence."""
        decision = router.route("Government debt to GDP ratio", indicators=["government debt"])
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_fiscal_deficit_routes_to_imf(self, router):
        """Fiscal deficit wording does not force IMF without LLM/evidence."""
        decision = router.route("Fiscal deficit forecast")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_global_forecast_routes_to_imf(self, router):
        """Global macro forecast wording does not force IMF without LLM/evidence."""
        decision = router.route("Global inflation forecast 2024-2026")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_macro_group_queries_route_to_imf(self, router):
        """Broad macro group wording does not force IMF without LLM/evidence."""
        decision = router.route("Emerging markets current account balance 2018-2023")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_global_commodity_price_index_routes_to_imf(self, router):
        """Commodity price wording does not force IMF without LLM/evidence."""
        decision = router.route("Global commodity price index 2019-2024")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_oil_price_routes_to_fred_via_catalog(self, router):
        """Generic oil price does not force FRED without LLM/evidence."""
        decision = router.route("oil price")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_brent_oil_price_routes_to_fred_via_catalog(self, router):
        """Brent oil price does not force FRED without LLM/evidence."""
        decision = router.route("Brent oil price")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_wti_oil_price_routes_to_fred_via_catalog(self, router):
        """WTI oil price does not force FRED without LLM/evidence."""
        decision = router.route("WTI oil price")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_budget_balance_routes_to_imf(self, router):
        """Budget balance wording does not force IMF without LLM/evidence."""
        decision = router.route("Government budget balance")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

    def test_gdp_to_debt_ratio_routes_to_imf(self, router):
        """Debt-to-GDP phrasing does not force IMF without LLM/evidence."""
        decision = router.route("GDP to debt ratio in China")
        assert decision.provider == "WorldBank"
        assert decision.final_authority is False

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

    def test_oecd_countries_catalog_overrides_regional(self, router):
        """When catalog matches a concept, it takes priority over regional group routing.

        The catalog routes 'GDP' to WorldBank even for OECD-country group queries.
        Explicit provider mention ('from OECD') still overrides everything.
        """
        decision = router.route("GDP across OECD countries")
        assert decision.provider in ("OECD", "WorldBank")

        # Explicit provider mention always wins
        decision = router.route("GDP from OECD")
        assert decision.provider == "OECD"

    def test_eu_countries_routes_to_eurostat_or_catalog(self, router):
        """EU countries queries route to Eurostat, or catalog provider if concept matches."""
        decision = router.route("Unemployment in EU countries")
        # Catalog may match "unemployment" → WorldBank; regional group would pick Eurostat
        assert decision.provider in ("Eurostat", "WorldBank")

        # Explicit provider always wins
        decision = router.route("Unemployment from Eurostat")
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
        """Canadian trade balance only contributes StatsCan as coverage evidence."""
        decision = router.route("Canada trade balance")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.final_authority is False

    def test_canada_exports_no_partner_routes_to_statscan(self, router):
        """Canadian exports without partner do not force StatsCan."""
        decision = router.route("Canada total exports")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.final_authority is False

    def test_canada_gdp_routes_to_statscan(self, router):
        """Canadian GDP does not force StatsCan without LLM/evidence."""
        decision = router.route("Canada GDP")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.match_type == "default"
        assert decision.final_authority is False

    def test_canada_cpi_monthly_routes_to_statscan(self, router):
        """Canadian CPI does not force StatsCan without LLM/evidence."""
        decision = router.route("Canada CPI monthly")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.match_type == "default"
        assert decision.final_authority is False

    def test_canada_inflation_routes_to_statscan(self, router):
        """Canadian inflation does not force StatsCan without LLM/evidence."""
        decision = router.route("Canada inflation rate")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.final_authority is False

    def test_canada_gdp_growth_routes_to_canada_capable_provider(self, router):
        """Canada GDP growth may resolve to StatsCan or a global macro provider."""
        decision = router.route("Canada GDP growth")
        assert decision.provider in ("StatsCan", "WorldBank", "IMF")

    def test_canada_population_routes_to_statscan(self, router):
        """Canadian population does not force StatsCan without LLM/evidence."""
        decision = router.route("Canada population")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.final_authority is False

    def test_canada_life_expectancy_routes_to_worldbank(self, router):
        """Canada life expectancy: StatsCan not available, uses WorldBank."""
        decision = router.route("Canada life expectancy")
        assert decision.provider == "WorldBank"

    def test_canada_residential_property_routes_to_bis(self, router):
        """Canadian property keywords do not force BIS without LLM/evidence."""
        decision = router.route("Canada residential property prices 2015-2024")
        assert decision.provider == "WorldBank"
        assert "StatsCan" in decision.candidate_providers
        assert decision.final_authority is False

    def test_goods_exports_without_partner_route_to_valid_provider(self, router):
        """Goods export flow queries without explicit partner route to a trade-capable provider.

        After Phase 3, the LLM handles semantic routing for unilateral export queries.
        The router may route via catalog or country-based logic.
        """
        decision = router.route("Mexico auto parts exports 2018-2023")
        assert decision.provider in ("Comtrade", "WorldBank")

    def test_global_trade_volume_routes_to_valid_provider(self, router):
        """Global trade volume queries use IMF or WorldBank.

        After Phase 3, the LLM handles forecast/global macro classification.
        """
        decision = router.route("World trade volume growth 2018-2023")
        assert decision.provider in ("IMF", "WorldBank")

    def test_forecast_queries_route_to_valid_provider(self, router):
        """Projection/forecast queries use IMF, WorldBank, or Eurostat.

        Eurozone queries now correctly route to Eurostat via region detection.
        After Phase 3, the LLM handles forecast detection.
        """
        decision = router.route("Eurozone GDP growth projections 2024-2026")
        assert decision.provider in ("IMF", "WorldBank", "Eurostat")

    def test_eu_country_government_debt_routes_to_eurostat_or_catalog(self, router):
        """Historical EU-country macro debt query should use Eurostat or catalog provider.

        After Phase 3, catalog may match 'government debt' to IMF/WorldBank.
        """
        decision = router.route("Italy government debt 2015-2023")
        assert decision.provider in ("Eurostat", "IMF", "WorldBank")

    def test_broad_semantic_keywords_defer_to_llm_provider(self, router):
        """Property/macro keywords must not override the LLM provider choice."""
        cases = [
            ("Property prices in Australia", "BIS"),
            ("Fiscal deficit forecast", "IMF"),
            ("oil price", "FRED"),
            ("Canada unemployment rate", "StatsCan"),
        ]

        for query, llm_provider in cases:
            decision = router.route(query, llm_provider=llm_provider)
            assert decision.provider == llm_provider
            assert decision.match_type == "llm"
            assert decision.semantic_authority == "llm_adjudication"
            assert decision.final_authority is True

    def test_country_scope_is_candidate_metadata_not_final_authority(self, router):
        """Country coverage hints must not mutate the final provider by themselves."""
        decision = router.route(
            "number of households in Canada",
            country="CA",
            llm_provider="WorldBank",
        )

        assert decision.provider == "WorldBank"
        assert decision.match_type == "llm"
        assert "StatsCan" in decision.candidate_providers
        assert decision.semantic_authority == "llm_adjudication"
        assert decision.can_override_llm_provider is False

    def test_explicit_and_mechanical_routes_can_override_llm_provider(self, router):
        """Only exact user/provider structure can override a weaker LLM provider."""
        explicit = router.route("GDP from FRED", llm_provider="WorldBank")
        assert explicit.provider == "FRED"
        assert explicit.can_override_llm_provider is True

        hs = router.route("China imports of HS 8542", llm_provider="WorldBank")
        assert hs.provider == "Comtrade"
        assert hs.can_override_llm_provider is True

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

    def test_us_only_indicator_via_llm_confidence(self, router):
        """US-only indicators via LLM have medium confidence (LLM match)."""
        decision = router.route("Federal funds rate", llm_provider="FRED")
        assert decision.confidence >= 0.5

    def test_keyword_match_medium_confidence(self, router):
        """Keyword-only matches remain low-confidence defaults."""
        decision = router.route("Life expectancy trends")
        assert decision.confidence <= 0.6
        assert decision.final_authority is False

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
        ("US GDP growth last 5 years", "WorldBank"),
        ("Germany unemployment rate", "WorldBank"),
        ("Japan inflation rate", "WorldBank"),  # Catalog: inflation → WorldBank; OECD non-EU defaults to WB
        ("China GDP growth", "WorldBank"),
        ("Brazil inflation rate", "WorldBank"),
    ]

    # Trade queries
    TRADE_QUERIES = [
        ("US exports to China", "Comtrade"),
        ("Germany imports from France", "Comtrade"),
        ("Trade deficit between US and Mexico", "Comtrade"),
        ("Exports as % of GDP for Germany", "WorldBank"),
        ("US trade balance history", "WorldBank"),
        # Aggregate trade indicators (% of GDP) must NOT route to Comtrade
        ("Import share of GDP in China and US", "WorldBank"),
        ("Imports of goods and services as % of GDP for India and Indonesia", "WorldBank"),
        ("Merchandise imports as share of GDP in Mexico and Brazil", "WorldBank"),
        ("Service imports share of GDP in UAE and Qatar", "WorldBank"),
        ("Import share of GDP in India vs Vietnam", "WorldBank"),
        ("Export share of GDP in Japan and South Korea", "WorldBank"),
        # Exact sweep queries (Q031, Q032, Q036, Q037, Q039) — must route to WorldBank
        ("Exports to GDP ratio in China and the UK since 2000", "WorldBank"),
        ("Import share of GDP in China and the US since 2000", "WorldBank"),
        ("Merchandise exports as share of GDP in Vietnam and Bangladesh", "WorldBank"),
        ("Merchandise imports as share of GDP in Mexico and Brazil", "WorldBank"),
        ("Service imports share of GDP in UAE and Qatar", "WorldBank"),
    ]

    # Financial queries
    # Note: S&P 500 and Bitcoin now rely on LLM provider selection;
    # without llm_provider, they fall through to catalog/country/default.
    FINANCIAL_QUERIES = [
        ("USD to EUR exchange rate", "ExchangeRate"),
        ("Government debt to GDP ratio", "WorldBank"),
        ("House prices in Australia", "WorldBank"),
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


class TestHSCodeRouting:
    """Tests for HS commodity code detection and routing to Comtrade."""

    @pytest.fixture
    def router(self):
        return UnifiedRouter()

    # HS code + trade verb → Comtrade
    HS_CODE_TRADE_QUERIES = [
        ("China imports of HS 8542 integrated circuits", "Comtrade"),
        ("France exports of HS 2204 wine", "Comtrade"),
        ("HS 8703 trade data for Germany", "Comtrade"),
        ("Japan imports HS8471 computers", "Comtrade"),
        ("US exports of HS-2709 crude petroleum", "Comtrade"),
        ("India HS 6204 imports of women's clothing", "Comtrade"),
        ("Brazil HS 0901 coffee exports 2020-2023", "Comtrade"),
        ("HS 8517 telephone equipment trade flows", "Comtrade"),
        ("South Korea HS 854231 semiconductor imports", "Comtrade"),
    ]

    @pytest.mark.parametrize("query,expected_provider", HS_CODE_TRADE_QUERIES)
    def test_hs_code_trade_queries_route_to_comtrade(self, router, query, expected_provider):
        """Queries with HS codes + trade language route to Comtrade."""
        decision = router.route(query)
        assert decision.provider == expected_provider, \
            f"Query '{query}' routed to {decision.provider}, expected {expected_provider}"
        assert decision.confidence >= 0.9
        assert "HS" in decision.matched_pattern

    def test_hs_code_without_trade_verb_does_not_route_to_comtrade(self, router):
        """HS code without trade language should NOT force Comtrade routing."""
        decision = router.route("What is HS 8542?")
        # Without trade verbs, this should not match the HS code trade check
        assert decision.provider != "Comtrade" or decision.match_type != "indicator"

    def test_hs_code_match_type_is_indicator(self, router):
        """HS code routing should have match_type='indicator'."""
        decision = router.route("China imports of HS 8542")
        assert decision.provider == "Comtrade"
        assert decision.match_type == "structural"

    def test_hs_code_with_hyphen(self, router):
        """HS codes with hyphen separator are recognized."""
        decision = router.route("Germany exports of HS-8703 vehicles")
        assert decision.provider == "Comtrade"

    def test_hs_code_no_space(self, router):
        """HS codes without space (e.g., HS8703) are recognized."""
        decision = router.route("Mexico HS8703 automobile imports")
        assert decision.provider == "Comtrade"

    def test_hs_code_six_digit(self, router):
        """Six-digit HS codes are recognized."""
        decision = router.route("UK imports of HS 854231 semiconductors")
        assert decision.provider == "Comtrade"

    def test_hs_code_takes_priority_over_country_routing(self, router):
        """HS code detection should override country-based routing.

        Without HS code detection, 'China imports of HS 8542' would route
        to WorldBank (non-OECD major economy). HS code should win.
        """
        decision = router.route("China imports of HS 8542 integrated circuits")
        assert decision.provider == "Comtrade"
        assert "HS" in decision.matched_pattern

    def test_explicit_provider_overrides_hs_code(self, router):
        """Explicit provider mention still takes highest priority over HS code."""
        decision = router.route("HS 8542 imports from FRED")
        assert decision.provider == "FRED"
        assert decision.match_type == "explicit"
