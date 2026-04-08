from __future__ import annotations

import unittest

from backend.services.indicator_database import IndicatorLookup


class _FakeDB:
    def __init__(self):
        self.last_search_provider = None
        self.last_get_provider = None
        self.last_search_query = None

    def search(self, query, provider=None, category=None, limit=20):
        self.last_search_query = query
        self.last_search_provider = provider
        return []

    def get_by_code(self, provider, code):
        self.last_get_provider = provider
        return None


class IndicatorLookupTests(unittest.TestCase):
    def test_search_normalizes_provider_aliases(self):
        db = _FakeDB()
        lookup = IndicatorLookup(db=db)

        lookup.search("gdp", provider="WORLDBANK")
        self.assertEqual(db.last_search_provider, "WorldBank")

        lookup.search("unemployment", provider="STATSCAN")
        self.assertEqual(db.last_search_provider, "StatsCan")

    def test_get_normalizes_provider_aliases(self):
        db = _FakeDB()
        lookup = IndicatorLookup(db=db)

        lookup.get("EXCHANGERATE", "USD")
        self.assertEqual(db.last_get_provider, "ExchangeRate")

    def test_search_normalizes_machine_style_query_tokens(self):
        db = _FakeDB()
        lookup = IndicatorLookup(db=db)

        lookup.search("EXPORT_TO_GDP_RATIO", provider="WORLDBANK")
        self.assertEqual(db.last_search_query, "export gdp gross domestic product ratio")


if __name__ == "__main__":
    unittest.main()
