from __future__ import annotations

import asyncio
import httpx
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.providers import comtrade as comtrade_module
from backend.models import NormalizedData
from backend.providers.comtrade import ComtradeProvider
from backend.providers.fred import FREDProvider
from backend.providers.worldbank import WorldBankProvider
from backend.providers.imf import IMFProvider
from backend.providers.bis import BISProvider
from backend.providers.eurostat import EurostatProvider
from backend.providers.oecd import OECDProvider
from backend.services.rate_limiter import ProviderRateLimitWaitExceeded
from backend.tests.utils import MockAsyncClient, MockAsyncResponse, run
from backend.utils.retry import DataNotAvailableError


class ProviderTests(unittest.TestCase):
    def test_oecd_lookup_terms_prioritize_semantic_alias_for_short_code(self) -> None:
        provider = OECDProvider()
        terms = provider._build_indicator_lookup_terms("PPI")  # pylint: disable=protected-access

        self.assertTrue(terms)
        self.assertIn("ppi", [term.lower() for term in terms])
        self.assertTrue(any("producer" in term.lower() for term in terms))
        self.assertNotEqual(terms[0].upper(), "PPI")

    def test_oecd_local_catalog_prefers_producer_price_flow_for_ppi_query(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        catalog = {
            "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE_CPC": {
                "name": "National accounts price indicators",
                "description": "Consumer expenditure price index",
                "structure": "DSD_NAMAIN10",
            },
            "DSD_PRICES@DF_PPI": {
                "name": "Producer price index",
                "description": "Producer prices for industry",
                "structure": "DSD_PRICES",
            },
        }

        with patch.object(OECDProvider, "_load_dataflows_catalog", return_value=catalog):
            _, dataflow, _ = run(provider._resolve_indicator("PPI"))  # pylint: disable=protected-access

        self.assertEqual(dataflow, "DSD_PRICES@DF_PPI")

    def test_oecd_resolve_indicator_keeps_explicit_dataflow_id(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        agency, dataflow, version = run(provider._resolve_indicator("DSD_LFS@DF_IALFS_EMP_WAP_Q"))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.SDD.TPS")
        self.assertEqual(dataflow, "DSD_LFS@DF_IALFS_EMP_WAP_Q")
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_maps_government_dataflows_to_gov_agency(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        agency, dataflow, version = run(provider._resolve_indicator("DSD_GOV@DF_GOV_PF_2025"))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.GOV.GIP")
        self.assertEqual(dataflow, "DSD_GOV@DF_GOV_PF_2025")
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_uses_canonical_gdp_dataflow(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        agency, dataflow, version = run(provider._resolve_indicator("GDP"))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.SDD.NAD")
        self.assertEqual(dataflow, "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE")
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_expands_partial_dataflow_prefix(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        catalog = {
            "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE": {
                "name": "Annual GDP and components - expenditure approach",
                "description": "GDP expenditure table",
                "structure": "DSD_NAMAIN10",
            },
            "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE_CPC": {
                "name": "National accounts price indicators",
                "description": "GDP expenditure price variants",
                "structure": "DSD_NAMAIN10",
            },
        }

        with patch.object(OECDProvider, "_load_dataflows_catalog", return_value=catalog):
            agency, dataflow, version = run(provider._resolve_indicator("OECD_DSD_NAMAIN10@DF_TABLE1"))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.SDD.NAD")
        self.assertEqual(dataflow, "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE")
        self.assertEqual(version, "1.0")

    def test_fred_series_id_explicit_codes_passthrough(self) -> None:
        """Test that explicit FRED series codes pass through directly without resolver."""
        provider = FREDProvider(api_key="test-key")

        # Short alphanumeric codes that look like FRED series IDs pass through directly
        explicit_codes = ["GDP", "UNRATE", "FEDFUNDS", "CPIAUCSL", "SP500", "DGS10", "M2SL"]
        for code in explicit_codes:
            with self.subTest(code=code):
                result = provider._series_id(code, None)
                self.assertEqual(result, code,
                    f"Explicit code '{code}' should pass through directly")

    def test_fred_series_id_delegates_to_indicator_resolver(self) -> None:
        """Test that natural language indicators are resolved via IndicatorResolver."""
        provider = FREDProvider(api_key="test-key")

        from dataclasses import dataclass

        @dataclass
        class MockResolved:
            code: str
            name: str
            confidence: float
            source: str
            metadata: dict = None
            def __post_init__(self):
                if self.metadata is None:
                    self.metadata = {}

        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = MockResolved(
            code="A191RL1Q225SBEA",
            name="Real GDP Growth Rate",
            confidence=0.95,
            source="database",
        )

        with patch("backend.services.indicator_resolver.get_indicator_resolver", return_value=mock_resolver):
            result = provider._series_id("GDP growth", None)
            self.assertEqual(result, "A191RL1Q225SBEA")
            mock_resolver.resolve.assert_called_with("GDP growth", provider="FRED")

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

    def test_fred_exact_stale_series_skips_default_observation_window(self) -> None:
        provider = FREDProvider(api_key="test-key")
        calls: list[dict] = []

        class RecordingClient(MockAsyncClient):
            async def get(self, url: str, *, params: dict | None = None, **kwargs) -> MockAsyncResponse:
                calls.append(dict(params or {}))
                return await super().get(url, params=params, **kwargs)

        responses = [
            MockAsyncResponse(
                {
                    "seriess": [
                        {
                            "title": "State Tax Collections: T51 Documentary and Stock Transfer Taxes for New Mexico",
                            "units": "Thousands of Dollars",
                            "frequency": "Annual",
                            "last_updated": "2024-01-01",
                            "observation_start": "1957-01-01",
                            "observation_end": "2020-01-01",
                        }
                    ]
                }
            ),
            MockAsyncResponse(
                {
                    "observations": [
                        {"date": "2020-01-01", "value": "100"},
                    ]
                }
            ),
        ]

        with patch("backend.providers.fred.get_http_client", return_value=RecordingClient(responses)):
            result = run(
                provider.fetch_series(
                    {
                        "indicator": "QTAXT51QTAXCAT3NMNO",
                        "startDate": "2021-04-20",
                        "endDate": "2026-04-19",
                        "__exact_indicator_title_match": True,
                        "__original_query": "State Tax Collections: T51 Documentary and Stock Transfer Taxes for New Mexico from FRED",
                    }
                )
            )

        self.assertEqual(result.metadata.seriesId, "QTAXT51QTAXCAT3NMNO")
        self.assertEqual(len(result.data), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["series_id"], "QTAXT51QTAXCAT3NMNO")
        self.assertNotIn("observation_start", calls[1])
        self.assertNotIn("observation_end", calls[1])

    def test_fred_exact_stale_series_respects_explicit_time_scope(self) -> None:
        provider = FREDProvider(api_key="test-key")
        calls: list[dict] = []

        class RecordingClient(MockAsyncClient):
            async def get(self, url: str, *, params: dict | None = None, **kwargs) -> MockAsyncResponse:
                calls.append(dict(params or {}))
                return await super().get(url, params=params, **kwargs)

        responses = [
            MockAsyncResponse(
                {
                    "seriess": [
                        {
                            "title": "State Tax Collections: T51 Documentary and Stock Transfer Taxes for New Mexico",
                            "units": "Thousands of Dollars",
                            "frequency": "Annual",
                            "last_updated": "2024-01-01",
                            "observation_start": "1957-01-01",
                            "observation_end": "2020-01-01",
                        }
                    ]
                }
            ),
            MockAsyncResponse({"observations": []}),
        ]

        with patch("backend.providers.fred.get_http_client", return_value=RecordingClient(responses)):
            result = run(
                provider.fetch_series(
                    {
                        "indicator": "QTAXT51QTAXCAT3NMNO",
                        "startDate": "2021-04-20",
                        "endDate": "2026-04-19",
                        "__exact_indicator_title_match": True,
                        "__original_query": "State Tax Collections T51 for New Mexico from FRED from 2021 to 2026",
                    }
                )
            )

        self.assertEqual(result.metadata.seriesId, "QTAXT51QTAXCAT3NMNO")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["observation_start"], "2021-04-20")
        self.assertEqual(calls[1]["observation_end"], "2026-04-19")

    def test_worldbank_fetch_indicator(self) -> None:
        provider = WorldBankProvider()

        batch_response = MockAsyncResponse(
            [
                {"page": 1, "pages": 1, "per_page": 1000, "total": 1},
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
        # Provide enough responses for batch + potential fallback
        responses = [batch_response, batch_response]

        with patch("backend.providers.worldbank.get_http1_client", return_value=MockAsyncClient(responses)):
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

    def test_worldbank_resolve_indicator_prefers_exact_provider_title_match(self) -> None:
        provider = WorldBankProvider(metadata_search_service=None)

        code = run(provider._resolve_indicator_code("Completion rate, upper secondary education, female (%)"))

        self.assertEqual(code, "UIS.CR.3.F")

    def test_worldbank_small_multi_country_prefers_parallel_single_country_fetch(self) -> None:
        provider = WorldBankProvider()

        class RecordingClient:
            def __init__(self, responses):
                self._responses = list(responses)
                self.urls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.urls.append(str(url))
                if not self._responses:
                    raise AssertionError("No more mock responses available")
                response = self._responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        usa_response = MockAsyncResponse(
            [
                {"page": 1, "pages": 1, "per_page": 1000, "total": 1},
                [
                    {
                        "indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"},
                        "country": {"id": "USA", "value": "United States"},
                        "countryiso3code": "USA",
                        "date": "2020",
                        "value": 1.2,
                        "unit": "",
                        "obs_status": "",
                        "decimal": 1,
                    }
                ],
            ]
        )
        can_response = MockAsyncResponse(
            [
                {"page": 1, "pages": 1, "per_page": 1000, "total": 1},
                [
                    {
                        "indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"},
                        "country": {"id": "CAN", "value": "Canada"},
                        "countryiso3code": "CAN",
                        "date": "2020",
                        "value": 0.8,
                        "unit": "",
                        "obs_status": "",
                        "decimal": 1,
                    }
                ],
            ]
        )
        client = RecordingClient([usa_response, can_response])

        with patch("backend.providers.worldbank.get_http1_client", return_value=client):
            results = run(
                provider.fetch_indicator(
                    indicator="FP.CPI.TOTL.ZG",
                    countries=["US", "CA"],
                    start_date="2020-01-01",
                    end_date="2020-12-31",
                )
            )

        self.assertEqual(len(results), 2)
        self.assertEqual({result.metadata.country for result in results}, {"United States", "Canada"})
        self.assertTrue(all(";" not in url for url in client.urls))
        self.assertTrue(any("/country/US/" in url for url in client.urls))
        self.assertTrue(any("/country/CA/" in url for url in client.urls))

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

    def test_comtrade_commodity_code_resolves_catalog_heading_titles(self) -> None:
        class _Lookup:
            def search(self, text, provider=None, limit=3):
                self.text = text
                self.provider = provider
                return [
                    {
                        "provider": "Comtrade",
                        "code": "5106",
                        "name": "5106 - Yarn of carded wool, not put up for retail sale",
                    }
                ]

        lookup = _Lookup()
        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            code = ComtradeProvider._commodity_code(  # pylint: disable=protected-access
                "exports of Yarn of carded wool, not put up for retail sale"
            )

        self.assertEqual(code, "5106")
        self.assertEqual(lookup.provider, "Comtrade")
        self.assertEqual(lookup.text, "Yarn of carded wool, not put up for retail sale")

    def test_comtrade_fetch_single_reporter_retries_timeout_then_succeeds(self) -> None:
        provider = ComtradeProvider(api_key="demo")

        class _FlakyClient:
            def __init__(self):
                self.calls = 0

            async def get(self, url, *, params=None, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))
                return MockAsyncResponse(
                    {
                        "data": [
                            {
                                "period": 2020,
                                "periodDesc": "2020",
                                "reporterDesc": "France",
                                "partnerDesc": "China",
                                "flowDesc": "Exports",
                                "primaryValue": 200,
                                "cmdDesc": "All Commodities",
                            }
                        ]
                    },
                    request_url=url,
                )

        result = run(
            provider._fetch_single_reporter_data(  # pylint: disable=protected-access
                _FlakyClient(),
                "France",
                "156",
                "TOTAL",
                "X",
                "2020",
                "A",
            )
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "France")
        self.assertEqual(result[0].data[0].value, 200)

    def test_comtrade_overall_budget_covers_single_reporter_retry_envelope(self) -> None:
        retry_envelope = (
            (comtrade_module.MAX_RETRIES * comtrade_module.REQUEST_TIMEOUT)
            + sum(
                comtrade_module.RETRY_DELAY_BASE * (2 ** attempt)
                for attempt in range(comtrade_module.MAX_RETRIES - 1)
            )
            + comtrade_module.GLOBAL_REQUEST_MIN_INTERVAL_SECONDS
        )

        single_task_budget = comtrade_module._comtrade_overall_time_budget(1)  # pylint: disable=protected-access
        two_task_budget = comtrade_module._comtrade_overall_time_budget(2)  # pylint: disable=protected-access
        wide_fanout_budget = comtrade_module._comtrade_overall_time_budget(10)  # pylint: disable=protected-access

        self.assertGreaterEqual(single_task_budget, retry_envelope)
        self.assertGreater(two_task_budget, single_task_budget)
        self.assertLessEqual(wide_fanout_budget, comtrade_module.COMTRADE_OVERALL_TIME_BUDGET_CAP)

    def test_comtrade_fetch_single_reporter_retries_transient_http_500_then_succeeds(self) -> None:
        provider = ComtradeProvider(api_key="demo")

        class _Http500Response(MockAsyncResponse):
            def __init__(self) -> None:
                super().__init__({}, status_code=500, request_url="https://example.com/comtrade")

            def raise_for_status(self) -> None:
                response = httpx.Response(
                    500,
                    request=httpx.Request("GET", "https://example.com/comtrade"),
                )
                raise httpx.HTTPStatusError("server error", request=response.request, response=response)

        responses = [
            _Http500Response(),
            MockAsyncResponse(
                {
                    "data": [
                        {
                            "period": 2020,
                            "periodDesc": "2020",
                            "reporterDesc": "Germany",
                            "partnerDesc": "France",
                            "flowDesc": "Imports",
                            "primaryValue": 300,
                            "cmdDesc": "All Commodities",
                        }
                    ]
                }
            ),
        ]
        client = MockAsyncClient(responses)

        with patch("backend.providers.comtrade.asyncio.sleep", new=AsyncMock()):
            result = run(
                provider._fetch_single_reporter_data(  # pylint: disable=protected-access
                    client,
                    "Germany",
                    "251",
                    "TOTAL",
                    "M",
                    "2020",
                    "A",
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "Germany")
        self.assertEqual(result[0].data[0].value, 300)

    def test_comtrade_fetch_single_reporter_retries_empty_total_response(self) -> None:
        provider = ComtradeProvider(api_key="demo")
        responses = [
            MockAsyncResponse({"data": []}),
            MockAsyncResponse(
                {
                    "data": [
                        {
                            "period": 2020,
                            "periodDesc": "2020",
                            "reporterDesc": "Germany",
                            "partnerDesc": "France",
                            "flowDesc": "Exports",
                            "primaryValue": 300,
                            "cmdDesc": "All Commodities",
                        }
                    ]
                }
            ),
        ]
        client = MockAsyncClient(responses)

        with patch("backend.providers.comtrade.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = run(
                provider._fetch_single_reporter_data(  # pylint: disable=protected-access
                    client,
                    "Germany",
                    "251",
                    "TOTAL",
                    "X",
                    "2020",
                    "A",
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "Germany")
        self.assertEqual(result[0].data[0].value, 300)
        self.assertEqual(len(client._responses), 0)  # pylint: disable=protected-access
        sleep_mock.assert_awaited_once()

    def test_comtrade_fetch_single_reporter_does_not_retry_empty_hs_subheading(self) -> None:
        provider = ComtradeProvider(api_key="demo")
        client = MockAsyncClient([MockAsyncResponse({"data": []})])

        with patch("backend.providers.comtrade.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = run(
                provider._fetch_single_reporter_data(  # pylint: disable=protected-access
                    client,
                    "China",
                    "0",
                    "030448",
                    "X",
                    "2020",
                    "A",
                )
            )

        self.assertEqual(result, [])
        self.assertEqual(len(client._responses), 0)  # pylint: disable=protected-access
        sleep_mock.assert_not_awaited()

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

    def test_comtrade_splits_comma_separated_partner_input(self) -> None:
        provider = ComtradeProvider(api_key="demo")
        captured_partner_codes = []

        async def _fake_fetch(
            client, reporter_raw, partner_code, commodity_code, flow_code, period_param, freq_code
        ):
            captured_partner_codes.append(partner_code)
            return []

        with patch.object(provider, "_fetch_single_reporter_data", new=AsyncMock(side_effect=_fake_fetch)):
            run(
                provider.fetch_trade_data(
                    reporter="UK",
                    partner="Germany, Netherlands",
                    flow="EXPORT",
                    start_year=2021,
                    end_year=2021,
                )
            )

        self.assertIn("276", captured_partner_codes)  # Germany
        self.assertIn("528", captured_partner_codes)  # Netherlands

    def test_comtrade_uses_process_local_event_loop_request_semaphore(self) -> None:
        provider_a = ComtradeProvider(api_key="demo")
        provider_b = ComtradeProvider(api_key="demo")
        active = 0
        max_active = 0
        start_times = []

        async def _fake_fetch(
            self, client, reporter_raw, partner_code, commodity_code, flow_code, period_param, freq_code
        ):
            nonlocal active, max_active
            start_times.append(asyncio.get_running_loop().time())
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        async def _run_two_fetches():
            with (
                patch.object(ComtradeProvider, "_fetch_single_reporter_data", new=_fake_fetch),
                patch("backend.providers.comtrade.GLOBAL_REQUEST_MIN_INTERVAL_SECONDS", 0.02),
            ):
                await asyncio.gather(
                    provider_a.fetch_trade_data(
                        reporter="United States",
                        partner="China",
                        flow="EXPORT",
                        start_year=2020,
                        end_year=2020,
                    ),
                    provider_b.fetch_trade_data(
                        reporter="Germany",
                        partner="China",
                        flow="EXPORT",
                        start_year=2020,
                        end_year=2020,
                    ),
                )

        run(_run_two_fetches())

        self.assertEqual(max_active, 1)
        self.assertEqual(len(start_times), 2)
        self.assertGreaterEqual(start_times[1] - start_times[0], 0.015)

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

        wb_resp = MockAsyncResponse(
            [
                {"page": 1, "pages": 1, "per_page": 1000, "total": 1},
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
        responses = [wb_resp, wb_resp]  # Enough for batch + potential fallback

        with patch("backend.providers.worldbank.get_http1_client", return_value=MockAsyncClient(responses)):
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

    def test_imf_code_input_uses_friendly_catalog_label(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        code, label = run(provider._resolve_indicator_code("GGXWDG_NGDP"))

        self.assertEqual(code, "GGXWDG_NGDP")
        self.assertIsNotNone(label)
        assert label is not None
        self.assertIn("debt", label.lower())

    def test_imf_exact_local_code_bypasses_metadata_discovery(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _Lookup:
            def get(self, provider_name: str, code: str):
                if provider_name == "IMF" and code == "RACFACBFIRM_XDC":
                    return {
                        "code": "RACFACBFIRM_XDC",
                        "name": "Reserve assets, claims on banks, firms, national currency",
                    }
                return None

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
            code, label = run(provider._resolve_indicator_code("RACFACBFIRM_XDC"))

        self.assertEqual(code, "RACFACBFIRM_XDC")
        self.assertIsNotNone(label)
        assert label is not None
        self.assertIn("reserve", label.lower())

    def test_imf_gdp_resolves_to_level_code_not_growth(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        code, label = run(provider._resolve_indicator_code("GDP"))

        self.assertEqual(code, "NGDPD")
        self.assertIsNotNone(label)
        assert label is not None
        self.assertIn("gdp", label.lower())

    def test_imf_fetch_batch_uses_alternative_code_when_primary_missing(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        responses = [
            MockAsyncResponse({"values": {}}),
            MockAsyncResponse(
                {
                    "values": {
                        "PPPIA_IX": {
                            "USA": {"2020": 100.0, "2021": 103.5},
                            "DEU": {"2020": 99.1, "2021": 102.0},
                        }
                    }
                }
            ),
        ]

        with patch.object(provider, "_resolve_indicator_code", return_value=("PPI", "Producer Price Index")), \
             patch.object(provider, "_get_alternative_indicator_codes", return_value=["PPPIA_IX"]), \
             patch("backend.providers.imf.get_http_client", return_value=MockAsyncClient(responses)):
            result = run(
                provider.fetch_batch_indicator(
                    indicator="producer price inflation",
                    countries=["USA", "DEU"],
                )
            )

        self.assertEqual(len(result), 2)
        self.assertTrue(all(series.metadata.seriesId == "PPPIA_IX" for series in result))

    def test_imf_fetch_batch_retries_on_invalid_json_body(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _InvalidJSONResponse(MockAsyncResponse):
            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        responses = [
            _InvalidJSONResponse({}),
            MockAsyncResponse(
                {
                    "values": {
                        "NGDP_RPCH": {
                            "USA": {"2020": 2.1, "2021": 5.8}
                        }
                    }
                }
            ),
        ]

        with patch.object(provider, "_resolve_indicator_code", return_value=("NGDP_RPCH", "Real GDP growth")), \
             patch("backend.providers.imf.get_http_client", return_value=MockAsyncClient(responses)):
            result = run(
                provider.fetch_batch_indicator(
                    indicator="GDP growth rate",
                    countries=["USA"],
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.seriesId, "NGDP_RPCH")

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

    def test_eurostat_resolve_accepts_uppercase_table_code_with_digits(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        dataset_code, dataset_label = run(provider._resolve_dataset_code("TEC00118"))

        self.assertEqual(dataset_code, "tec00118")
        self.assertIsNone(dataset_label)

    def test_eurostat_ppp_indices_do_not_force_gdp_filter(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        class RecordingClient:
            def __init__(self, response):
                self.response = response
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append((str(url), dict(params or {})))
                self.response.request = MockAsyncResponse([], request_url=str(url)).request
                return self.response

        response = MockAsyncResponse(
            {
                "value": {"0": 1.10, "1": 1.12},
                "dimension": {
                    "time": {
                        "category": {
                            "index": {"2023": 0, "2024": 1},
                            "label": {"2023": "2023", "2024": "2024"},
                        }
                    },
                    "geo": {
                        "category": {
                            "index": {"DE": 0},
                            "label": {"DE": "Germany"},
                        }
                    },
                    "unit": {
                        "category": {
                            "index": {"PLI_EU27_2020": 0},
                            "label": {"PLI_EU27_2020": "Price level index (EU27_2020=100)"},
                        }
                    },
                },
                "id": ["geo", "time"],
                "size": [1, 2],
                "updated": "2026-01-01",
            }
        )
        client = RecordingClient(response)

        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="PRC_PPP_IND", country="DE", start_year=2023, end_year=2024))

        self.assertEqual(series.metadata.seriesId, "prc_ppp_ind")
        self.assertEqual(series.metadata.country, "DE")
        self.assertEqual(len(series.data), 2)
        self.assertEqual(len(client.calls), 1)
        _, params = client.calls[0]
        self.assertNotIn("na_item", params)

    def test_oecd_resolve_indicator_expands_catalog_code_alias(self) -> None:
        class StubMetadata:
            def __init__(self):
                self.search_terms = []

            async def search_with_sdmx_fallback(self, provider: str, indicator: str):
                self.search_terms.append(indicator)
                if indicator.lower() != "long-term interest rates":
                    return []
                return [{"code": "DSD_IRLT@DF_IRLT", "name": "Long-term interest rates", "agency": "OECD.SDD.TPS"}]

            async def discover_indicator(self, provider: str, indicator_name: str, search_results):
                return {
                    "code": "DSD_IRLT@DF_IRLT",
                    "name": "Long-term interest rates",
                    "agency": "OECD.SDD.TPS",
                    "confidence": 0.95,
                }

        metadata_stub = StubMetadata()
        provider = OECDProvider(metadata_search_service=metadata_stub)

        agency, dataflow, version = run(provider._resolve_indicator("IRLT"))

        self.assertEqual(agency, "OECD.SDD.TPS")
        self.assertEqual(dataflow, "DSD_IRLT@DF_IRLT")
        self.assertEqual(version, "1.0")
        self.assertIn("IRLT", [term.upper() for term in provider._build_indicator_lookup_terms("IRLT")])  # pylint: disable=protected-access
        self.assertTrue(any(term.lower() == "long-term interest rates" for term in metadata_stub.search_terms))

    def test_oecd_fetch_multi_country_skips_aggregate_for_explicit_country_comparison(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        call_countries = []

        async def _fake_fetch_indicator(indicator: str, country: str, start_year=None, end_year=None):
            call_countries.append(country)
            return NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "OECD",
                        "indicator": indicator,
                        "country": country,
                        "frequency": "annual",
                        "unit": "%",
                        "lastUpdated": "2024-01-01",
                        "seriesId": "TEST",
                    },
                    "data": [{"date": "2023-01-01", "value": 1.0}],
                }
            )

        with patch.object(provider, "fetch_indicator", new=AsyncMock(side_effect=_fake_fetch_indicator)):
            results = run(
                provider.fetch_multi_country(
                    indicator="PPI",
                    countries=["US", "DE"],
                    start_year=2019,
                    end_year=2024,
                )
            )

        self.assertEqual(len(results), 2)
        self.assertIn("USA", call_countries)
        self.assertIn("DEU", call_countries)
        self.assertNotIn("OECD", call_countries)

    def test_oecd_fetch_indicator_fails_fast_when_rate_limit_wait_is_too_long(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.SDD.TPS", "DSD_LFS@DF_IALFS_EMP_WAP_Q", "1.0")),
        ), patch(
            "backend.providers.oecd.wait_for_provider",
            new=AsyncMock(side_effect=ProviderRateLimitWaitExceeded("wait too long")),
        ):
            with self.assertRaisesRegex(DataNotAvailableError, "temporarily rate-limited"):
                run(provider.fetch_indicator("employment rate", country="USA"))

    def test_oecd_fetch_indicator_short_circuits_when_circuit_is_open(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        with patch("backend.providers.oecd.is_provider_circuit_open", return_value=True), \
             patch.object(provider, "_resolve_indicator", new=AsyncMock(side_effect=AssertionError("should not resolve"))):
            with self.assertRaisesRegex(DataNotAvailableError, "temporarily unavailable due to rate limiting"):
                run(provider.fetch_indicator("employment rate", country="USA"))

    def test_oecd_fetch_multi_country_fails_fast_for_large_country_sets(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        with self.assertRaisesRegex(DataNotAvailableError, "more than 8 countries"):
            run(
                provider.fetch_multi_country(
                    indicator="employment rate",
                    countries=["US", "CA", "GB", "FR", "DE", "IT", "JP", "KR", "AU"],
                )
            )

    def test_worldbank_does_not_expand_short_country_codes_as_groups(self) -> None:
        provider = WorldBankProvider()

        self.assertIsNone(provider._expand_country_group("US"))
        self.assertIsNone(provider._expand_country_group("usa"))
        self.assertIsNone(provider._expand_country_group("UK"))
        self.assertIsNotNone(provider._expand_country_group("G7"))


if __name__ == "__main__":
    unittest.main()
