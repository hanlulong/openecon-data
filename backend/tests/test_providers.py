from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.models import NormalizedData
from backend.providers.comtrade import ComtradeProvider
from backend.providers.fred import FREDProvider
from backend.providers.worldbank import WorldBankProvider
from backend.providers.imf import IMFProvider
from backend.providers.bis import BISProvider
from backend.providers.eurostat import EurostatProvider
from backend.tests.utils import MockAsyncClient, MockAsyncResponse, run


class ProviderTests(unittest.TestCase):
    def test_fred_series_id_mapping(self) -> None:
        """Test that indicator names are properly mapped to FRED series IDs."""
        provider = FREDProvider(api_key="test-key")

        # Test common indicator name variations
        test_cases = [
            # Natural language with spaces
            ("GDP growth", "A191RL1Q225SBEA"),
            ("unemployment rate", "UNRATE"),
            ("consumer confidence index", "UMCSENT"),
            ("10 year treasury", "DGS10"),
            ("mortgage rate", "MORTGAGE30US"),
            ("retail sales growth", "RSXFS"),
            ("inflation rate", "CPIAUCSL"),

            # Underscore format (from LLM parsing)
            ("GDP_GROWTH", "A191RL1Q225SBEA"),
            ("UNEMPLOYMENT_RATE", "UNRATE"),
            ("CONSUMER_CONFIDENCE_INDEX", "UMCSENT"),
            ("10_YEAR_TREASURY", "DGS10"),
            ("MORTGAGE_RATE", "MORTGAGE30US"),
            ("RETAIL_SALES_GROWTH", "RSXFS"),

            # Short forms
            ("GDP", "GDP"),
            ("CPI", "CPIAUCSL"),
            ("unemployment", "UNRATE"),
            ("inflation", "CPIAUCSL"),

            # Case variations
            ("gdp growth", "A191RL1Q225SBEA"),
            ("Unemployment Rate", "UNRATE"),

            # Explicit series IDs should pass through
            ("UNRATE", "UNRATE"),
            ("FEDFUNDS", "FEDFUNDS"),
        ]

        for indicator, expected_series_id in test_cases:
            with self.subTest(indicator=indicator):
                result = provider._series_id(indicator, None)
                self.assertEqual(result, expected_series_id,
                    f"Indicator '{indicator}' mapped to '{result}', expected '{expected_series_id}'")

                # Verify result is valid for FRED API (≤25 alphanumeric chars)
                self.assertLessEqual(len(result), 25,
                    f"Series ID '{result}' is too long ({len(result)} chars)")
                self.assertTrue(result.replace('_', '').isalnum(),
                    f"Series ID '{result}' contains invalid characters")

    def test_fred_series_id_explicit_override(self) -> None:
        """Test that explicit series IDs override indicator names."""
        provider = FREDProvider(api_key="test-key")

        # When both indicator and seriesId are provided, seriesId should win
        result = provider._series_id("unemployment rate", "CUSTOM_SERIES")
        self.assertEqual(result, "CUSTOM_SERIES")

    def test_fred_fetch_series(self) -> None:
        provider = FREDProvider(api_key="test-key")

        responses = [
            MockAsyncResponse(
                {
                    "seriess": [
                        {
                            "title": "Real Gross Domestic Product",
                            "units": "Billions of Chained 2017 Dollars",
                            "frequency": "Quarterly",
                            "last_updated": "2024-01-01",
                        }
                    ]
                }
            ),
            MockAsyncResponse(
                {
                    "observations": [
                        {"date": "2020-01-01", "value": "100"},
                        {"date": "2020-04-01", "value": "."},
                    ]
                }
            ),
        ]

        with patch("backend.providers.fred.get_http_client", return_value=MockAsyncClient(responses)):
            result = run(provider.fetch_series({"seriesId": "GDP"}))

        self.assertIsInstance(result, NormalizedData)
        self.assertEqual(result.metadata.source, "FRED")
        self.assertEqual(result.metadata.seriesId, "GDP")
        self.assertEqual(len(result.data), 2)
        self.assertEqual(result.data[0].value, 100.0)
        self.assertIsNone(result.data[1].value)

    def test_worldbank_fetch_indicator(self) -> None:
        provider = WorldBankProvider()

        responses = [
            MockAsyncResponse(
                [
                    {"page": 1},
                    [
                        {
                            "indicator": {"id": "NY.GDP.MKTP.CD", "value": "GDP (current US$)"},
                            "country": {"id": "USA", "value": "United States"},
                            "countryiso3code": "USA",
                            "date": "2020",
                            "value": 21000000000000,
                            "unit": "",
                            "obs_status": "",
                            "decimal": 0,
                        }
                    ],
                ],
                headers={"Date": "Mon, 01 Jan 2024 00:00:00 GMT"}
            )
        ]

        with patch("backend.providers.worldbank.get_http_client", return_value=MockAsyncClient(responses)):
            results = run(
                provider.fetch_indicator(
                    indicator="NY.GDP.MKTP.CD",
                    country="US",
                    start_date="2020-01-01",
                    end_date="2020-12-31",
                )
            )

        self.assertEqual(len(results), 1)
        data = results[0]
        self.assertEqual(data.metadata.source, "World Bank")
        self.assertEqual(data.metadata.indicator, "GDP (current US$)")
        self.assertEqual(data.data[0].date, "2020-01-01")
        self.assertEqual(data.data[0].value, 21000000000000)

    def test_comtrade_fetch_trade_data(self) -> None:
        provider = ComtradeProvider(api_key="demo")

        responses = [
            MockAsyncResponse(
                {
                    "data": [
                        {
                            "period": 2020,
                            "periodDesc": "2020",
                            "reporterDesc": "United States",
                            "partnerDesc": "World",
                            "flowDesc": "Exports",
                            "primaryValue": 100,
                            "cmdDesc": "All Commodities",
                        },
                        {
                            "period": 2020,
                            "periodDesc": "2020",
                            "reporterDesc": "United States",
                            "partnerDesc": "World",
                            "flowDesc": "Imports",
                            "primaryValue": 75,
                            "cmdDesc": "All Commodities",
                        },
                    ]
                }
            )
        ]

        with patch("backend.providers.comtrade.get_http_client", return_value=MockAsyncClient(responses)):
            result = run(
                provider.fetch_trade_data(
                    reporter="US",
                    commodity="TOTAL",
                    start_year=2020,
                    end_year=2020,
                    flow="BOTH",
                )
            )

        self.assertEqual(len(result), 2)
        indicators = {series.metadata.indicator for series in result}
        self.assertIn("Exports - All Commodities", indicators)
        self.assertIn("Imports - All Commodities", indicators)

    def test_comtrade_fetch_trade_balance(self) -> None:
        provider = ComtradeProvider(api_key="demo")

        responses = [
            MockAsyncResponse(
                {
                    "data": [
                        {
                            "period": 2020,
                            "reporterDesc": "Canada",
                            "flowDesc": "Exports",
                            "primaryValue": 120,
                            "cmdDesc": "All Commodities",
                        }
                    ]
                }
            ),
            MockAsyncResponse(
                {
                    "data": [
                        {
                            "period": 2020,
                            "reporterDesc": "Canada",
                            "flowDesc": "Imports",
                            "primaryValue": 100,
                            "cmdDesc": "All Commodities",
                        },
                    ]
                }
            )
        ]

        with patch("backend.providers.comtrade.get_http_client", return_value=MockAsyncClient(responses)):
            balance = run(provider.fetch_trade_balance(reporter="CA", partner="US", start_year=2020, end_year=2020))

        self.assertEqual(balance.metadata.indicator, "Trade Balance with US")
        self.assertEqual(balance.data[0].value, 20)

    def test_worldbank_metadata_discovery(self) -> None:
        class StubMetadata:
            async def search_worldbank(self, keyword: str):
                self.keyword = keyword
                return [{"code": "NY.CUSTOM.CODE", "name": "Custom Indicator"}]

            async def discover_indicator(self, provider: str, indicator_name: str, search_results):
                return {"code": "NY.CUSTOM.CODE", "name": "Custom Indicator", "confidence": 0.9}

            async def search_with_sdmx_fallback(self, provider: str, indicator: str):
                return await self.search_worldbank(indicator)

        metadata_stub = StubMetadata()
        provider = WorldBankProvider(metadata_search_service=metadata_stub)
        self.addCleanup(lambda: provider.INDICATOR_MAPPINGS.pop("CUSTOM_INDICATOR", None))

        responses = [
            MockAsyncResponse(
                [
                    {"page": 1},
                    [
                        {
                            "indicator": {"id": "NY.CUSTOM.CODE", "value": "Custom Indicator"},
                            "country": {"id": "USA", "value": "United States"},
                            "countryiso3code": "USA",
                            "date": "2021",
                            "value": 123.4,
                            "unit": "",
                            "obs_status": "",
                            "decimal": 0,
                        }
                    ],
                ],
                headers={"Date": "Tue, 02 Jan 2024 00:00:00 GMT"}
            )
        ]

        with patch("backend.providers.worldbank.get_http_client", return_value=MockAsyncClient(responses)):
            results = run(provider.fetch_indicator(indicator="custom indicator", country="US"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.indicator, "Custom Indicator")
        self.assertEqual(results[0].data[0].value, 123.4)
        self.assertEqual(metadata_stub.keyword, "custom indicator")

    def test_imf_metadata_discovery(self) -> None:
        class StubMetadata:
            async def search_imf(self, keyword: str):
                self.keyword = keyword
                return [{"code": "CUSTOM_CODE", "name": "Custom IMF Indicator"}]

            async def discover_indicator(self, provider: str, indicator_name: str, search_results):
                return {"code": "CUSTOM_CODE", "name": "Custom IMF Indicator", "confidence": 0.95}

            async def search_with_sdmx_fallback(self, provider: str, indicator: str):
                return await self.search_imf(indicator)

        metadata_stub = StubMetadata()
        provider = IMFProvider(metadata_search_service=metadata_stub)
        self.addCleanup(lambda: provider.INDICATOR_MAPPINGS.pop("CUSTOM_IMF", None))

        responses = [
            MockAsyncResponse(
                {
                    "values": {
                        "CUSTOM_CODE": {
                            "USA": {"2020": 1.2, "2021": 1.3}
                        }
                    },
                    "name": "Custom IMF Indicator"
                }
            )
        ]

        with patch("backend.providers.imf.get_http_client", return_value=MockAsyncClient(responses)):
            series = run(provider.fetch_indicator(indicator="custom imf", country="USA"))

        self.assertEqual(series.metadata.indicator, "Custom IMF Indicator")
        self.assertEqual(series.data[0].value, 1.2)
        self.assertEqual(metadata_stub.keyword, "custom imf")

    def test_bis_metadata_discovery(self) -> None:
        class StubMetadata:
            async def search_bis(self, keyword: str):
                self.keyword = keyword
                return [{"code": "CUSTOM_FLOW", "name": "Custom BIS Flow"}]

            async def discover_indicator(self, provider: str, indicator_name: str, search_results):
                return {"code": "CUSTOM_FLOW", "name": "Custom BIS Flow", "confidence": 0.9}

            async def search_with_sdmx_fallback(self, provider: str, indicator: str):
                return await self.search_bis(indicator)

        metadata_stub = StubMetadata()
        provider = BISProvider(metadata_search_service=metadata_stub)
        self.addCleanup(lambda: provider.INDICATOR_MAPPINGS.pop("CUSTOM_BIS", None))

        responses = [
            MockAsyncResponse(
                {
                    "data": {
                        "dataSets": [
                            {
                                "series": {
                                    "0:0:0": {
                                        "observations": {
                                            "0": [1.5],
                                            "1": [1.75],
                                        }
                                    }
                                }
                            }
                        ],
                        "structure": {
                            "dimensions": {
                                "observation": [
                                    {"id": "FREQ", "values": [{"id": "M"}]},
                                    {"id": "REF_AREA", "values": [{"id": "US"}]},
                                    {"id": "TIME_PERIOD", "values": [{"id": "2020-01"}, {"id": "2020-02"}]},
                                ]
                            }
                        },
                    }
                }
            )
        ]

        with patch("backend.providers.bis.get_http_client", return_value=MockAsyncClient(responses)):
            series_list = run(provider.fetch_indicator(indicator="custom bis", country="US", frequency="M"))

        self.assertEqual(len(series_list), 1)
        self.assertEqual(series_list[0].metadata.indicator, "Custom BIS Flow")
        self.assertEqual(series_list[0].data[0].value, 1.5)
        self.assertEqual(metadata_stub.keyword, "custom bis")

    def test_eurostat_sdmx3_fetch(self) -> None:
        class StubMetadata:
            async def search_eurostat(self, keyword: str):
                self.keyword = keyword
                return [{"code": "custom_dataset", "name": "Custom Eurostat Dataset"}]

            async def discover_indicator(self, provider: str, indicator_name: str, search_results):
                return {"code": "custom_dataset", "name": "Custom Eurostat Dataset", "confidence": 0.92}

            async def search_with_sdmx_fallback(self, provider: str, indicator: str):
                return await self.search_eurostat(indicator)

        metadata_stub = StubMetadata()
        provider = EurostatProvider(metadata_search_service=metadata_stub)
        self.addCleanup(lambda: provider.DATASET_MAPPINGS.pop("CUSTOM EUROSTAT", None))

        # JSON-stat 2.0 format response (what Eurostat actually returns)
        responses = [
            MockAsyncResponse(
                {
                    "value": {"0": 1000, "1": 1100},
                    "dimension": {
                        "time": {
                            "category": {
                                "index": {"2019": 0, "2020": 1},
                                "label": {"2019": "2019", "2020": "2020"}
                            }
                        },
                        "unit": {
                            "category": {
                                "index": {"CP_MEUR": 0},
                                "label": {"CP_MEUR": "Million euro"}
                            }
                        },
                        "geo": {
                            "category": {
                                "index": {"DE": 0},
                                "label": {"DE": "Germany"}
                            }
                        }
                    },
                    "id": ["unit", "geo", "time"],
                    "size": [1, 1, 2],
                    "updated": "2024-01-01"
                }
            )
        ]

        with patch("backend.providers.eurostat.get_http_client", return_value=MockAsyncClient(responses)):
            series = run(provider.fetch_indicator(indicator="custom eurostat", country="DE", start_year=2019, end_year=2020))

        self.assertEqual(series.metadata.indicator, "Custom Eurostat Dataset")
        self.assertEqual(series.metadata.country, "DE")
        self.assertEqual(series.metadata.seriesId, "custom_dataset")
        self.assertEqual(series.metadata.frequency, "annual")
        self.assertEqual(series.metadata.unit, "Million euro")
        self.assertEqual(series.data[0].value, 1000)
        self.assertEqual(metadata_stub.keyword, "custom eurostat")

    def test_worldbank_does_not_expand_short_country_codes_as_groups(self) -> None:
        provider = WorldBankProvider()

        self.assertIsNone(provider._expand_country_group("US"))
        self.assertIsNone(provider._expand_country_group("usa"))
        self.assertIsNone(provider._expand_country_group("UK"))
        self.assertIsNotNone(provider._expand_country_group("G7"))


if __name__ == "__main__":
    unittest.main()
