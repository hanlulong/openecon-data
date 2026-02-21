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
        self.assertEqual(result.source, "translator")

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
        self.assertEqual(result.code, "HHDGDP")
        self.assertGreaterEqual(result.confidence, 0.7)

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
        self.assertEqual(result.code, "NAPM")
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
        self.assertEqual(result.code, "TM.VAL.MRCH.XD.WD")
        self.assertEqual(result.source, "database")
        self.assertGreaterEqual(result.confidence, 0.75)

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
