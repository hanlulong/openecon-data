from __future__ import annotations

import unittest

from backend.services.indicator_resolver import IndicatorResolver


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


class IndicatorResolverTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

