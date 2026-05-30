from __future__ import annotations

import sqlite3
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

    @staticmethod
    def _lookup_with_sqlite_rows(rows):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE indicators (
                provider TEXT,
                code TEXT,
                name TEXT,
                popularity INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO indicators (provider, code, name, popularity) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        class _DB:
            def _get_connection(self):
                return conn

            def search(self, query, provider=None, category=None, limit=20):
                return []

            def get_by_code(self, provider, code):
                return None

        return IndicatorLookup(db=_DB()), conn

    def test_exact_name_matches_skips_provider_scan_for_ascii_miss(self):
        lookup, conn = self._lookup_with_sqlite_rows(
            [
                ("WorldBank", "NY.GDP.MKTP.CD", "GDP (current US$)", 100),
                ("WorldBank", "SP.POP.TOTL", "Population, total", 80),
            ]
        )
        statements = []
        conn.set_trace_callback(statements.append)

        result = lookup.exact_name_matches(["Canada GDP"], provider="WorldBank")

        self.assertEqual(result, [])
        provider_full_scans = [
            statement
            for statement in statements
            if "WHERE provider = 'WorldBank'" in statement
            and "lower(name) LIKE" not in statement
            and "trim(name)" not in statement
            and "lower(trim(name))" not in statement
        ]
        self.assertEqual(provider_full_scans, [])

    def test_exact_name_matches_keeps_ascii_sql_lower_match(self):
        lookup, _conn = self._lookup_with_sqlite_rows(
            [
                ("WorldBank", "NY.GDP.MKTP.CD", "Gross Domestic Product", 100),
            ]
        )

        result = lookup.exact_name_matches(["gross domestic product"], provider="WorldBank")

        self.assertEqual([row["code"] for row in result], ["NY.GDP.MKTP.CD"])

    def test_exact_name_matches_keeps_unicode_casefold_match(self):
        lookup, conn = self._lookup_with_sqlite_rows(
            [
                ("CoinGecko", "ae-coin", "Æ Coin", 100),
            ]
        )
        statements = []
        conn.set_trace_callback(statements.append)

        result = lookup.exact_name_matches(["æ coin"], provider="CoinGecko")

        self.assertEqual([row["code"] for row in result], ["ae-coin"])
        self.assertTrue(
            any(
                "WHERE provider = 'CoinGecko'" in statement
                and "lower(name) LIKE" not in statement
                and "trim(name)" not in statement
                and "lower(trim(name))" not in statement
                for statement in statements
            )
        )

    def test_exact_name_matches_does_not_unicode_match_generic_ascii(self):
        lookup, _conn = self._lookup_with_sqlite_rows(
            [
                ("CoinGecko", "ae-coin", "Æ Coin", 100),
            ]
        )

        result = lookup.exact_name_matches(["Coin"], provider="CoinGecko")

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
