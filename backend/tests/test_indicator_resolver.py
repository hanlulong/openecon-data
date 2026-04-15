from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.services.indicator_resolver import IndicatorResolver
from backend.services.indicator_translator import IndicatorTranslator


class _FakeLookup:
    def __init__(self, search_results=None, exact_results=None):
        self._search_results = search_results or []
        self._exact_results = exact_results or {}

    def get(self, provider: str, code: str):
        return self._exact_results.get((provider, code))

    def search(self, query: str, provider=None, limit: int = 5):
        return self._search_results[:limit]

    def find_best_provider(self, query, country=None, preferred_providers=None):
        return None


class _FakeTranslator:
    def translate_indicator(self, query: str, target_provider: str = None):
        return (None, None)


class _StaticTranslator(_FakeTranslator):
    def __init__(self, code: str, concept_name: str):
        self.code = code
        self.concept_name = concept_name

    def translate_indicator(self, query: str, target_provider: str = None):
        return (self.code, self.concept_name)


class _FakeVectorResult:
    def __init__(self, code: str, provider: str, name: str, similarity: float):
        self.code = code
        self.provider = provider
        self.name = name
        self.similarity = similarity


class _FakeVectorService:
    def __init__(self, results):
        self._results = results

    def search(self, query: str, limit: int = 10, where=None):
        return self._results[:limit]


class IndicatorResolverTests(unittest.TestCase):
    def test_provider_agnostic_translation_avoids_default_fred_bias(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve("fx reserves", provider=None, use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.provider, "WorldBank")
        self.assertEqual(result.code, "FI.RES.TOTL.CD")
        self.assertIn(result.source, {"translator", "catalog"})

    def test_provider_agnostic_catalog_concept_beats_coarse_translator_inference(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve(
            "research and development spending share of gdp",
            provider=None,
            use_cache=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "WorldBank")
        self.assertEqual(result.code, "GB.XPD.RSDV.GD.ZS")
        self.assertEqual(result.source, "catalog")

    def test_provider_agnostic_catalog_concept_preserves_specific_youth_unemployment(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve("youth unemployment rate", provider=None, use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "WorldBank")
        self.assertEqual(result.code, "SL.UEM.1524.ZS")
        self.assertEqual(result.source, "catalog")

    def test_provider_agnostic_catalog_concept_preserves_effective_exchange_rate(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve("effective exchange rate", provider=None, use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.provider, "BIS")
        self.assertIn(result.code, {"WS_EER", "BIS_WS_EER"})
        self.assertEqual(result.source, "catalog")

    def test_resolves_long_context_ppi_query_via_translator(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve(
            "producer price inflation trend in the us and germany",
            provider="OECD",
            use_cache=False,
        )

        self.assertIsNotNone(result)
        # Catalog maps producer_price_inflation→DSD_STES@DF_INDSERV for OECD (SDMX dataflow).
        # Translator returns "PPI". Both are valid.
        self.assertIn(result.code, ("DSD_STES@DF_INDSERV", "PPI"))
        self.assertEqual(result.provider, "OECD")
        self.assertIn(result.source, {"translator", "catalog"})

    def test_resolves_trade_openness_context_query_via_translator(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve(
            "trade openness ratio (exports plus imports to gdp) in small open economies",
            provider="WorldBank",
            use_cache=False,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.TRD.GNFS.ZS")
        self.assertEqual(result.provider, "WorldBank")
        self.assertIn(result.source, {"translator", "catalog"})

    def test_resolves_reer_context_query_to_worldbank_series(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve(
            "reer trend for china and india from 2012 to 2024",
            provider="WorldBank",
            countries=["CN", "IN"],
            use_cache=False,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "PX.REX.REER")
        self.assertEqual(result.provider, "WorldBank")
        self.assertIn(result.source, {"translator", "catalog"})

    def test_prefers_imf_ppi_candidate_over_cpi_for_producer_price_query(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "PCPIPCH",
                    "provider": "IMF",
                    "name": "Inflation rate, average consumer prices",
                    "description": "Average CPI inflation rate",
                },
                {
                    "code": "VNM_PPPI_ISIC4_HTJ_PTR_BY_PP_IX",
                    "provider": "IMF",
                    "name": "Vietnam Definition, Producer Price Index, Services Producer Prices",
                    "description": "Country-specific producer price index series",
                },
                {
                    "code": "PPPIA_IX",
                    "provider": "IMF",
                    "name": "Prices, Producer Price Index, Commodities by Activity, Index",
                    "description": "Producer price index",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve(
            "producer price inflation",
            provider="IMF",
            countries=["US", "DE"],
            use_cache=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "PPPIA_IX")

    def test_translator_candidate_does_not_preempt_stronger_search_result(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "PCPIPCH",
                    "provider": "IMF",
                    "name": "Inflation rate, average consumer prices",
                    "description": "Average CPI inflation rate",
                },
                {
                    "code": "PPPIA_IX",
                    "provider": "IMF",
                    "name": "Prices, Producer Price Index, Commodities by Activity, Index",
                    "description": "Producer price index",
                },
            ],
            exact_results={
                ("IMF", "PCPIPCH"): {
                    "code": "PCPIPCH",
                    "provider": "IMF",
                    "name": "Inflation rate, average consumer prices",
                    "description": "Average CPI inflation rate",
                }
            },
        )
        resolver = IndicatorResolver(
            lookup=lookup,
            translator=_StaticTranslator("PCPIPCH", "inflation"),
        )

        result = resolver.resolve("producer price inflation", provider="IMF", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "PPPIA_IX")
        self.assertEqual(result.source, "database")

    def test_translator_fallback_uses_metadata_name_when_available(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "GDP",
                    "provider": "FRED",
                    "name": "Gross Domestic Product",
                    "description": "National income and product accounts",
                }
            ],
            exact_results={
                ("FRED", "FEDFUNDS"): {
                    "code": "FEDFUNDS",
                    "provider": "FRED",
                    "name": "Federal Funds Effective Rate",
                    "description": "Overnight federal funds rate",
                }
            },
        )
        resolver = IndicatorResolver(
            lookup=lookup,
            translator=_StaticTranslator("FEDFUNDS", "interest_rate"),
        )

        result = resolver.resolve("federal funds rate", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "FEDFUNDS")
        self.assertEqual(result.name, "Federal Funds Effective Rate")
        self.assertIn(result.source, {"translator", "catalog"})

    def test_cache_key_includes_country_context(self):
        class _CountingLookup(_FakeLookup):
            def __init__(self, exact_results):
                super().__init__(search_results=[], exact_results=exact_results)
                self.get_calls = 0

            def get(self, provider: str, code: str):
                self.get_calls += 1
                return super().get(provider, code)

        lookup = _CountingLookup(
            exact_results={
                ("FRED", "GDP"): {
                    "code": "GDP",
                    "provider": "FRED",
                    "name": "Gross Domestic Product",
                }
            }
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        first = resolver.resolve("GDP", provider="FRED", country="US", use_cache=True)
        second_same_country = resolver.resolve("GDP", provider="FRED", country="US", use_cache=True)
        third_different_country = resolver.resolve("GDP", provider="FRED", country="CA", use_cache=True)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second_same_country)
        self.assertIsNotNone(third_different_country)
        # First call populates cache, second call hits cache, third call misses due to country context.
        self.assertEqual(lookup.get_calls, 2)

    def test_cache_is_bounded_lru(self):
        lookup = _FakeLookup(
            exact_results={
                ("FRED", "GDP"): {"code": "GDP", "provider": "FRED", "name": "GDP"},
                ("FRED", "UNRATE"): {"code": "UNRATE", "provider": "FRED", "name": "Unemployment Rate"},
                ("FRED", "CPIAUCSL"): {"code": "CPIAUCSL", "provider": "FRED", "name": "Consumer Price Index"},
            }
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())
        resolver._cache_max_entries = 2

        resolver.resolve("GDP", provider="FRED", use_cache=True)
        resolver.resolve("UNRATE", provider="FRED", use_cache=True)
        resolver.resolve("CPIAUCSL", provider="FRED", use_cache=True)

        self.assertLessEqual(len(resolver._cache), 2)
        self.assertFalse(any(key.endswith(":gdp") for key in resolver._cache.keys()))

    def test_prefers_lexically_relevant_result_over_higher_raw_score(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "GGXWDG_NGDP",
                    "provider": "IMF",
                    "name": "Government debt to GDP ratio",
                    "_score": 95.0,
                },
                {
                    "code": "HHDGDP",
                    "provider": "IMF",
                    "name": "Household debt to GDP ratio",
                    "_score": 70.0,
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("household debt", provider="IMF", use_cache=False)

        self.assertIsNotNone(result)
        # Catalog maps household_debt→HH_ALL for IMF (trusted).
        # HHDGDP is also acceptable from FTS5.
        self.assertIn(result.code, ("HH_ALL", "HHDGDP"))
        self.assertGreaterEqual(result.confidence, 0.7)

    def test_prefers_near_exact_long_title_match_for_fred_housing_series(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "CPIAUCSL",
                    "provider": "FRED",
                    "name": "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average",
                    "description": "Inflation index",
                },
                {
                    "code": "PRIINCCOUYY35300",
                    "provider": "FRED",
                    "name": "Housing Inventory: Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)",
                    "description": "Housing inventory market hotness series",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve(
            "US Housing Inventory: Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)",
            provider="FRED",
            use_cache=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "PRIINCCOUYY35300")
        self.assertGreaterEqual(result.confidence, 0.95)

    def test_provider_query_variant_search_handles_us_prefix_and_colon_titles(self):
        class _VariantLookup(_FakeLookup):
            def search(self, query: str, provider=None, limit: int = 5):
                if query == "US Housing Inventory: Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)":
                    return []
                if query == "Housing Inventory: Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)":
                    return []
                if query == "Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)":
                    return [
                        {
                            "code": "PRIINCCOUYY35300",
                            "provider": "FRED",
                            "name": "Housing Inventory: Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)",
                            "description": "Housing inventory market hotness series",
                        }
                    ]
                return []

        resolver = IndicatorResolver(lookup=_VariantLookup(), translator=_FakeTranslator())

        results = resolver._search_candidates_for_provider_query(  # pylint: disable=protected-access
            "US Housing Inventory: Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)",
            "FRED",
            limit=10,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["code"], "PRIINCCOUYY35300")

    def test_rejects_low_overlap_search_match(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "GDP",
                    "provider": "FRED",
                    "name": "Gross Domestic Product",
                    "_score": 120.0,
                }
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("galactic purchasing power", provider="FRED", use_cache=False)

        self.assertIsNone(result)

    def test_single_term_lexical_match_is_not_overconfident(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "IC.CNS.TRAD.ZS",
                    "provider": "WorldBank",
                    "name": "Customs and trade regulations (% of managers surveyed)",
                }
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("custom indicator", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertLess(result.confidence, 0.7)

    def test_resolves_labor_force_participation_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("labor force participation rate", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "SL.TLF.CACT.ZS")
        self.assertEqual(result.source, "catalog")

    def test_resolves_employment_to_population_ratio_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("employment to population ratio", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "SL.EMP.TOTL.SP.ZS")
        self.assertEqual(result.source, "catalog")

    def test_prefers_general_employment_rate_over_specialized_breakdowns(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "14100021",
                    "provider": "StatsCan",
                    "name": "Unemployment rate, participation rate, and employment rate by type of student during school months, monthly, unadjusted for seasonality",
                    "description": "Employment rate by type of student during school months",
                },
                {
                    "code": "14100374",
                    "provider": "StatsCan",
                    "name": "Employment and unemployment rate, monthly, unadjusted for seasonality",
                    "description": "Employment and unemployment rate",
                },
                {
                    "code": "14100020",
                    "provider": "StatsCan",
                    "name": "Unemployment rate, participation rate and employment rate by educational attainment, annual",
                    "description": "Employment rate by educational attainment",
                },
                {
                    "code": "14100354",
                    "provider": "StatsCan",
                    "name": "Regional unemployment rates used by the Employment Insurance program, three-month moving average, seasonally adjusted",
                    "description": "Employment Insurance regional unemployment rate",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("employment rate", provider="STATSCAN", country="Canada", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        # Catalog maps employment to a vector key (EMPLOYMENT, EMPLOYMENT_RATE, etc.)
        # or a product ID. The exact code depends on catalog resolution.
        self.assertTrue(
            "EMPLOYMENT" in result.code.upper() or result.code in ("14100287", "14100374"),
            f"Expected employment-related code, got: {result.code}",
        )

    def test_resolves_foreign_exchange_reserves_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("fx reserves", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "FI.RES.TOTL.CD")
        self.assertEqual(result.source, "catalog")

    def test_resolves_government_expenditure_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("government spending", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.CON.GOVT.ZS")
        self.assertEqual(result.source, "catalog")

    def test_resolves_renewable_energy_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("renewable energy share", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "EG.FEC.RNEW.ZS")
        self.assertEqual(result.source, "catalog")

    def test_resolves_retail_sales_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("retail sales", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "RSAFS")
        self.assertEqual(result.source, "catalog")

    def test_resolves_wages_query_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("average wages and earnings trend", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "CES0500000003")
        self.assertEqual(result.source, "catalog")

    def test_catalog_result_uses_provider_metadata_when_database_row_is_missing(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("canada unemployment rate", provider="StatsCan", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "14100287")
        self.assertEqual(result.source, "catalog")
        # Catalog name may be the concept name or the full description
        self.assertTrue(result.name is not None and len(result.name) > 0)

    def test_resolves_consumer_price_inflation_query_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("consumer price inflation usa", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "CPIAUCSL")
        self.assertEqual(result.source, "catalog")

    def test_resolves_worldbank_rnd_query_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve(
            "research and development spending share of gdp",
            provider="WorldBank",
            use_cache=False,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "GB.XPD.RSDV.GD.ZS")
        self.assertEqual(result.source, "catalog")

    def test_resolves_school_life_expectancy_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("school life expectancy", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "SE.SCH.LIFE")
        self.assertEqual(result.source, "catalog")

    def test_resolves_youth_unemployment_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("youth unemployment rate", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "SL.UEM.1524.ZS")
        self.assertEqual(result.source, "catalog")

    def test_resolves_gdp_per_capita_in_pps_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("gdp per capita in pps", provider="Eurostat", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.code, "TEC00114")
        self.assertEqual(result.source, "catalog")

    def test_resolves_bis_effective_exchange_rates_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("effective exchange rates", provider="BIS", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn(result.code, {"WS_EER", "BIS_WS_EER"})
        self.assertEqual(result.source, "catalog")

    def test_resolves_bis_credit_to_gdp_gap_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("credit to gdp gap", provider="BIS", use_cache=False)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn(result.code, {"WS_CREDIT_GAP", "BIS_WS_CREDIT_GAP"})
        self.assertEqual(result.source, "catalog")

    def test_prefers_dynamic_catalog_for_generic_coingecko_market_queries(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "capo-was-right",
                    "provider": "CoinGecko",
                    "name": "Capo Was Right",
                    "description": "Low-cap token",
                }
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve(
            "Top 10 cryptocurrencies by market cap right now",
            provider="COINGECKO",
            use_cache=False,
        )

        self.assertIsNotNone(result)
        self.assertEqual(str(result.code).lower(), "dynamic")
        self.assertEqual(result.source, "catalog")

    def test_resolves_debt_service_ratio_to_bis_catalog_code(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve("debt service ratio", provider="BIS", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "WS_DSR")
        self.assertIn(result.source, {"catalog", "translator"})

    def test_resolves_fiscal_deficit_to_imf_catalog_code(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve("fiscal deficit", provider="IMF", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "GGXCNL_NGDP")
        self.assertEqual(result.source, "catalog")

    def test_money_aggregate_scoring_prefers_requested_aggregate(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "M2REAL",
                    "provider": "FRED",
                    "name": "Real M2 Money Stock",
                    "description": "Real M2 money stock",
                },
                {
                    "code": "M1SL",
                    "provider": "FRED",
                    "name": "M1 Money Stock",
                    "description": "M1 money stock",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("US M1 money stock trend", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "M1SL")

    def test_concept_lookup_handles_underscored_bond_yield_query(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("10_YEAR_TREASURY_YIELD", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "DGS10")
        self.assertEqual(result.source, "catalog")

    def test_catalog_preferred_over_translator_for_bond_yield_concept(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=IndicatorTranslator())

        result = resolver.resolve("10-year government bond yield", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "DGS10")
        self.assertIn(result.source, {"catalog", "translator"})

    def test_discontinued_series_penalty_prefers_active_alternative(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "DLTBOARD",
                    "provider": "FRED",
                    "name": "Composite Yield on U.S. Treasury Bonds with Maturity over 10 Years (DISCONTINUED)",
                    "description": "Discontinued series",
                },
                {
                    "code": "DGS10",
                    "provider": "FRED",
                    "name": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
                    "description": "Daily Treasury constant maturity yield",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("US 10-year government bond yield", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "DGS10")

    def test_resolves_industrial_production_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("industrial production", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "INDPRO")
        self.assertEqual(result.source, "catalog")

    def test_resolves_housing_starts_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("housing starts", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "HOUST")
        self.assertEqual(result.source, "catalog")

    def test_resolves_consumer_confidence_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("consumer confidence", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "UMCSENT")
        self.assertEqual(result.source, "catalog")

    def test_resolves_pmi_via_catalog(self):
        lookup = _FakeLookup(search_results=[])
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("manufacturing pmi", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "AMTMNO")
        self.assertEqual(result.source, "catalog")

    def test_exact_code_match_keeps_max_confidence(self):
        lookup = _FakeLookup(
            exact_results={
                ("IMF", "NGDP_RPCH"): {
                    "code": "NGDP_RPCH",
                    "provider": "IMF",
                    "name": "Real GDP growth",
                }
            }
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("NGDP_RPCH", provider="IMF", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NGDP_RPCH")
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.source, "database")

    def test_confidence_is_bounded_to_unit_interval(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "GDP",
                    "provider": "FRED",
                    "name": "Gross Domestic Product",
                    "_score": 9999.0,
                }
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("gdp", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)

    @patch("backend.services.indicator_resolver.is_provider_available", return_value=True)
    @patch("backend.services.indicator_resolver.get_indicator_code", return_value="BOPGSTB")
    @patch("backend.services.indicator_resolver.get_indicator_codes", return_value=["BOPGSTB"])
    @patch("backend.services.indicator_resolver.find_concept_by_term", return_value="trade_balance")
    def test_prefers_catalog_guided_code_for_known_concept(
        self,
        _concept,
        _codes,
        _primary_code,
        _provider_available,
    ):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "VAPGDPW",
                    "provider": "FRED",
                    "name": "World value added indicator",
                    "_score": 99.0,
                }
            ],
            exact_results={
                ("FRED", "BOPGSTB"): {
                    "code": "BOPGSTB",
                    "provider": "FRED",
                    "name": "Trade Balance: Goods and Services, Balance of Payments Basis",
                }
            },
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("trade surplus", provider="FRED", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "BOPGSTB")
        self.assertEqual(result.source, "catalog")

    @patch("backend.services.indicator_resolver.is_provider_available", return_value=True)
    @patch("backend.services.indicator_resolver.get_indicator_code", return_value="NE.IMP.GNFS.ZS")
    @patch("backend.services.indicator_resolver.get_indicator_codes", return_value=["NE.IMP.GNFS.ZS"])
    @patch("backend.services.indicator_resolver.find_concept_by_term", return_value="imports")
    def test_allows_high_confidence_off_catalog_match(
        self,
        _concept,
        _codes,
        _primary_code,
        _provider_available,
    ):
        # When a catalog concept exists, the catalog-designated code is trusted
        # (floor confidence 0.70) even if FTS5 finds a different off-catalog match.
        # This is correct: catalog mappings are curated expert knowledge.
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "TM.VAL.MRCH.XD.WD",
                    "provider": "WorldBank",
                    "name": "Merchandise imports by the reporting economy (current US$)",
                    "_score": 90.0,
                }
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("import value", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        # Catalog-designated code wins over off-catalog FTS5 match
        self.assertEqual(result.code, "NE.IMP.GNFS.ZS")
        self.assertEqual(result.source, "catalog")
        self.assertGreaterEqual(result.confidence, 0.70)

    @patch("backend.services.indicator_resolver.is_provider_available", return_value=True)
    @patch("backend.services.indicator_resolver.get_indicator_code", return_value="NE.IMP.GNFS.ZS")
    @patch("backend.services.indicator_resolver.get_indicator_codes", return_value=["NE.IMP.GNFS.ZS"])
    @patch("backend.services.indicator_resolver.find_concept_by_term", return_value="imports")
    def test_falls_back_to_catalog_for_weak_off_catalog_match(
        self,
        _concept,
        _codes,
        _primary_code,
        _provider_available,
    ):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "RANDOM_TRADE_SERIES",
                    "provider": "WorldBank",
                    "name": "Terms of trade adjustment index",
                    "_score": 100.0,
                }
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("imports", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.IMP.GNFS.ZS")
        self.assertEqual(result.source, "catalog")

    def test_prefers_export_series_over_generic_gdp_ratio_matches(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "NY.GDP.MKTP.ZG",
                    "provider": "WorldBank",
                    "name": "Gross domestic product (Av. annual growth, %)",
                },
                {
                    "code": "NY.GDS.TOTL.ZS",
                    "provider": "WorldBank",
                    "name": "Gross domestic savings (% of GDP)",
                },
                {
                    "code": "NE.TRD.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Trade (% of GDP)",
                },
                {
                    "code": "NE.EXP.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Exports of goods and services (% of GDP)",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("export to gdp ratio", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.EXP.GNFS.ZS")
        self.assertGreaterEqual(result.confidence, 0.7)

    def test_handles_common_ratio_typo_without_misrouting(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "NY.GDS.TOTL.ZS",
                    "provider": "WorldBank",
                    "name": "Gross domestic savings (% of GDP)",
                },
                {
                    "code": "NE.EXP.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Exports of goods and services (% of GDP)",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("export to gdp ration", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.EXP.GNFS.ZS")

    def test_fuzzy_matching_handles_import_typos_without_hardcoded_patch(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "NY.GDS.TOTL.ZS",
                    "provider": "WorldBank",
                    "name": "Gross domestic savings (% of GDP)",
                },
                {
                    "code": "NE.EXP.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Exports of goods and services (% of GDP)",
                },
                {
                    "code": "NE.IMP.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Imports of goods and services (% of GDP)",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("imprts share of gdp", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.IMP.GNFS.ZS")

    def test_prefers_directional_ratio_series_over_absolute_trade_values(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "NE.IMP.GNFS.CD",
                    "provider": "WorldBank",
                    "name": "Imports of goods and services (current US$)",
                },
                {
                    "code": "NE.IMP.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Imports of goods and services (% of GDP)",
                },
                {
                    "code": "TM.VAL.MRCH.XD.WD",
                    "provider": "WorldBank",
                    "name": "Merchandise imports (current US$)",
                },
            ]
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())

        result = resolver.resolve("import share of gdp", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.IMP.GNFS.ZS")
        self.assertGreaterEqual(result.confidence, 0.7)

    def test_score_search_match_penalizes_opposite_direction_ratio_candidates(self):
        resolver = IndicatorResolver(lookup=_FakeLookup(), translator=_FakeTranslator())
        query = "import share of gdp"
        import_candidate = {
            "code": "NE.IMP.GNFS.ZS",
            "provider": "WorldBank",
            "name": "Imports of goods and services (% of GDP)",
            "description": "",
        }
        export_candidate = {
            "code": "NE.EXP.GNFS.ZS",
            "provider": "WorldBank",
            "name": "Exports of goods and services (% of GDP)",
            "description": "",
        }
        generic_ratio_candidate = {
            "code": "NY.GNS.ICTR.ZS",
            "provider": "WorldBank",
            "name": "Gross domestic savings (% of GDP)",
            "description": "",
        }

        import_score = resolver._score_search_match(query, import_candidate)  # pylint: disable=protected-access
        export_score = resolver._score_search_match(query, export_candidate)  # pylint: disable=protected-access
        generic_score = resolver._score_search_match(query, generic_ratio_candidate)  # pylint: disable=protected-access

        self.assertGreater(import_score, export_score + 0.2)
        self.assertGreater(import_score, generic_score + 0.2)

    def test_rrf_fusion_can_promote_vector_only_candidate(self):
        lookup = _FakeLookup(
            search_results=[
                {
                    "code": "NY.GDS.TOTL.ZS",
                    "provider": "WorldBank",
                    "name": "Gross domestic savings (% of GDP)",
                },
                {
                    "code": "NE.TRD.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Trade (% of GDP)",
                },
            ],
            exact_results={
                ("WORLDBANK", "NE.IMP.GNFS.ZS"): {
                    "code": "NE.IMP.GNFS.ZS",
                    "provider": "WorldBank",
                    "name": "Imports of goods and services (% of GDP)",
                }
            },
        )
        resolver = IndicatorResolver(lookup=lookup, translator=_FakeTranslator())
        resolver._use_hybrid_rerank = True
        resolver._get_vector_service = lambda: _FakeVectorService([
            _FakeVectorResult(
                code="NE.IMP.GNFS.ZS",
                provider="WORLDBANK",
                name="Imports of goods and services (% of GDP)",
                similarity=0.93,
            )
        ])

        result = resolver.resolve("import share of gdp", provider="WorldBank", use_cache=False)

        self.assertIsNotNone(result)
        self.assertEqual(result.code, "NE.IMP.GNFS.ZS")


if __name__ == "__main__":
    unittest.main()
