from __future__ import annotations

import asyncio
import json
import httpx
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.providers import comtrade as comtrade_module
from backend.models import DataPoint, ExecutionPlan, Metadata, NormalizedData, ParsedIntent
from backend.providers.comtrade import ComtradeProvider
from backend.providers.fred import FREDProvider
from backend.providers.worldbank import WorldBankProvider
from backend.providers.imf import IMFProvider
from backend.providers.bis import BISProvider
from backend.providers.eurostat import EurostatProvider
from backend.providers.oecd import OECDProvider
from backend.providers.coingecko import CoinGeckoProvider
from backend.services.rate_limiter import ProviderRateLimitWaitExceeded
from backend.tests.utils import MockAsyncClient, MockAsyncResponse, run
from backend.utils.retry import DataNotAvailableError


class ProviderTests(unittest.TestCase):
    def test_coingecko_simple_price_fails_closed_for_missing_provider_metric(self) -> None:
        provider = CoinGeckoProvider()

        with patch.object(
            provider,
            "_make_request_with_retry",
            new=AsyncMock(return_value={"rgb": {}}),
        ):
            with self.assertRaises(DataNotAvailableError) as raised:
                run(provider.get_simple_price(["rgb"], vs_currency="usd"))

        self.assertIn("coingecko_price_unavailable", str(raised.exception))
        self.assertIn("requested_ids=rgb", str(raised.exception))

    def test_oecd_lookup_terms_do_not_expand_short_code_semantically(self) -> None:
        provider = OECDProvider()
        terms = provider._build_indicator_lookup_terms("PPI")  # pylint: disable=protected-access

        self.assertEqual(terms, ["PPI"])

    def test_oecd_local_catalog_does_not_finalize_ppi_query(self) -> None:
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
            with self.assertRaises(DataNotAvailableError):
                run(provider._resolve_indicator("PPI"))  # pylint: disable=protected-access

    def test_oecd_resolve_indicator_keeps_explicit_dataflow_id(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        agency, dataflow, version = run(provider._resolve_indicator("DSD_LFS@DF_IALFS_EMP_WAP_Q"))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.SDD.TPS")
        self.assertEqual(dataflow, "DSD_LFS@DF_IALFS_EMP_WAP_Q")
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_uses_exact_registry_agency_and_version(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        code = "DSD_EAG_UOE_FIN@DF_UOE_FIN_NATURE_CUR_CAP"
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "OECD",
            "code": code,
            "raw_metadata": json.dumps(
                {
                    "agencyID": "OECD.EDU.IMEP",
                    "version": "3.1",
                    "structure": "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=OECD.EDU.IMEP:DSD_EAG_UOE_FIN(3.1)",
                }
            ),
        }

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            agency, dataflow, version = run(provider._resolve_indicator(code))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.EDU.IMEP")
        self.assertEqual(dataflow, code)
        self.assertEqual(version, "3.1")
        lookup.get.assert_called_with("OECD", code)

    def test_oecd_resolve_indicator_accepts_exact_agency_dataflow_tuple(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        code = "DSD_EAG_UOE_FIN@DF_UOE_FIN_NATURE_CUR_CAP"
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "OECD",
            "code": code,
            "raw_metadata": json.dumps({"version": "3.1"}),
        }

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            agency, dataflow, version = run(  # pylint: disable=protected-access
                provider._resolve_indicator(f"OECD.EDU.IMEP,{code}")
            )

        self.assertEqual(agency, "OECD.EDU.IMEP")
        self.assertEqual(dataflow, code)
        self.assertEqual(version, "3.1")

    def test_oecd_resolve_indicator_accepts_exact_agency_dataflow_version_tuple(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        agency, dataflow, version = run(  # pylint: disable=protected-access
            provider._resolve_indicator("OECD.ELS.JAI,DSD_TAXBEN_IMW@DF_IMW,1.0")
        )

        self.assertEqual(agency, "OECD.ELS.JAI")
        self.assertEqual(dataflow, "DSD_TAXBEN_IMW@DF_IMW")
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_accepts_exact_non_df_dataflow_ids(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        lookup = MagicMock()
        lookup.get.return_value = None

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            agency, dataflow, version = run(  # pylint: disable=protected-access
                provider._resolve_indicator("OECD_DSD_EARNINGS@PAY_INCIDENCE")
            )

        self.assertEqual(agency, "OECD.ELS.SAE")
        self.assertEqual(dataflow, "DSD_EARNINGS@PAY_INCIDENCE")
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_accepts_prefixed_exact_dataflow_without_catalog(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        code = "DSD_HCQO@DF_HKP"
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "OECD",
            "code": code,
            "raw_metadata": json.dumps({"agencyID": "OECD.ELS.HD", "version": "1.1"}),
        }

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            agency, dataflow, version = run(  # pylint: disable=protected-access
                provider._resolve_indicator("OECD_DSD_HCQO@DF_HKP")
            )

        self.assertEqual(agency, "OECD.ELS.HD")
        self.assertEqual(dataflow, code)
        self.assertEqual(version, "1.1")

    def test_oecd_resolve_indicator_accepts_exact_agency_non_df_dataflow_tuple(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        lookup = MagicMock()
        lookup.get.return_value = None

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            agency, dataflow, version = run(  # pylint: disable=protected-access
                provider._resolve_indicator("OECD.ELS.SAE,DSD_EARNINGS@RMW")
            )

        self.assertEqual(agency, "OECD.ELS.SAE")
        self.assertEqual(dataflow, "DSD_EARNINGS@RMW")
        self.assertEqual(version, "1.0")

    def test_oecd_discovery_prefers_exact_registry_metadata_over_heuristic_agency(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        code = "DSD_EAG_UOE_NON_FIN_STUD@DF_UOE_NF_SHARE_VET"
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "OECD",
            "code": code,
            "raw_metadata": json.dumps(
                {
                    "agencyID": "OECD.EDU.IMEP",
                    "version": "1.0",
                }
            ),
        }

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            agency, dataflow, version = provider._build_result_from_discovery(  # pylint: disable=protected-access
                code,
                {"agency": "OECD.SDD.EDSTAT"},
            )

        self.assertEqual(agency, "OECD.EDU.IMEP")
        self.assertEqual(dataflow, code)
        self.assertEqual(version, "1.0")

    def test_oecd_registry_metadata_falls_back_when_raw_metadata_is_malformed(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        code = "DSD_UNKNOWN@DF_UNKNOWN"
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "OECD",
            "code": code,
            "raw_metadata": "{not-json",
        }

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            agency, dataflow, version = run(provider._resolve_indicator(code))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.SDD.NAD")
        self.assertEqual(dataflow, code)
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_maps_government_dataflows_to_gov_agency(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        agency, dataflow, version = run(provider._resolve_indicator("DSD_GOV@DF_GOV_PF_2025"))  # pylint: disable=protected-access

        self.assertEqual(agency, "OECD.GOV.GIP")
        self.assertEqual(dataflow, "DSD_GOV@DF_GOV_PF_2025")
        self.assertEqual(version, "1.0")

    def test_oecd_resolve_indicator_does_not_use_canonical_gdp_shortcut(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        with self.assertRaises(DataNotAvailableError):
            run(provider._resolve_indicator("GDP"))  # pylint: disable=protected-access

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

    def test_oecd_builds_positional_key_from_structure_metadata(self) -> None:
        metadata = {
            "dimensions": [
                {"id": "MEASURE", "position": 0},
                {"id": "REF_AREA", "position": 1},
                {"id": "ACTIVITY", "position": 2},
                {"id": "COUNTERPART_AREA", "position": 3},
                {"id": "UNIT_MEASURE", "position": 4},
                {"id": "FREQ", "position": 5},
            ]
        }

        key = OECDProvider._build_oecd_key_from_structure(  # pylint: disable=protected-access
            metadata,
            "JPN",
            custom_defaults={"frequency": "A"},
        )

        self.assertEqual(key, ".JPN....A")

    def test_oecd_builds_positional_key_with_provider_native_defaults(self) -> None:
        metadata = {
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "PLAN_TYPE", "position": 1},
                {"id": "DEFINITION_TYPE", "position": 2},
                {"id": "VEHICLE_TYPE", "position": 3},
                {"id": "FREQ", "position": 4},
            ],
            "default_values": {
                "PLAN_TYPE": "_T",
                "DEFINITION_TYPE": "_T",
                "VEHICLE_TYPE": "_T",
                "FREQ": "A",
            },
        }

        defaults = dict(metadata["default_values"])
        key = OECDProvider._build_oecd_key_from_structure(  # pylint: disable=protected-access
            metadata,
            "CAN",
            custom_defaults=defaults,
        )

        self.assertEqual(key, "CAN._T._T._T.A")

    def test_oecd_frequency_hint_uses_structure_native_frequency(self) -> None:
        metadata = {
            "valid_values_by_dimension": {"FREQ": ["A"]},
        }

        frequency = OECDProvider._oecd_expected_frequency_for_structure(  # pylint: disable=protected-access
            metadata,
            "M",
        )

        self.assertEqual(frequency, "A")

    def test_oecd_default_annotations_parse_provider_values(self) -> None:
        defaults = OECDProvider._parse_oecd_default_annotations(  # pylint: disable=protected-access
            [
                {
                    "type": "DEFAULT",
                    "title": "PLAN_TYPE=_T,DEFINITION_TYPE=_T,VEHICLE_TYPE=_T",
                },
                {"type": "LAYOUT_ROW", "title": "REF_AREA"},
            ]
        )

        self.assertEqual(
            defaults,
            {
                "PLAN_TYPE": "_T",
                "DEFINITION_TYPE": "_T",
                "VEHICLE_TYPE": "_T",
            },
        )

    def test_oecd_default_annotations_ignore_not_displayed_as_defaults(self) -> None:
        defaults = OECDProvider._parse_oecd_default_annotations(  # pylint: disable=protected-access
            [
                {"type": "NOT_DISPLAYED", "title": "REF_AREA=CAN,AGE=Y_GE15"},
                {"type": "DEFAULT", "title": "AGE=_Z,FREQ=A"},
            ]
        )

        self.assertEqual(defaults, {"AGE": "_Z", "FREQ": "A"})
        self.assertNotIn("REF_AREA", defaults)

    def test_oecd_registry_external_bases_come_from_provider_links(self) -> None:
        code = "DSD_OTHMRKR@DF_OTHERMARKERS"
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "OECD",
            "code": code,
            "raw_metadata": json.dumps(
                {
                    "links": [
                        {
                            "href": "https://sdmx.oecd.org/dcd-public/rest/dataflow/OECD.DCD.FSD/DSD_OTHMRKR@DF_OTHERMARKERS/1.4",
                            "rel": "external",
                        }
                    ]
                }
            ),
        }

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            bases = OECDProvider._lookup_dataflow_registry_external_bases(  # pylint: disable=protected-access
                code
            )

        self.assertEqual(bases, ["https://sdmx.oecd.org/dcd-public/rest"])

    def test_oecd_builds_all_key_when_structure_has_no_country_dimension(self) -> None:
        metadata = {
            "dimensions": [
                {"id": "MEASURE", "position": 0},
                {"id": "POLLUTANT", "position": 1},
                {"id": "PLASTIC_LIFEC_S", "position": 2},
                {"id": "FREQ", "position": 3},
            ]
        }

        key = OECDProvider._build_oecd_key_from_structure(  # pylint: disable=protected-access
            metadata,
            "USA",
        )

        self.assertEqual(key, "all")

    def test_oecd_ref_area_code_uses_dataflow_native_iso2_constraints(self) -> None:
        metadata = {
            "dimension_ids": ["FREQ", "ADJUSTMENT", "REF_AREA"],
            "valid_values_by_dimension": {"REF_AREA": ["DE", "CA"]},
        }

        self.assertEqual(
            OECDProvider._oecd_ref_area_code_for_structure(metadata, "DEU"),  # pylint: disable=protected-access
            "DE",
        )
        self.assertEqual(
            OECDProvider._oecd_ref_area_code_for_structure(metadata, "CAN"),  # pylint: disable=protected-access
            "CA",
        )

    def test_oecd_country_constraint_error_rejects_absent_ref_area(self) -> None:
        metadata = {
            "dimension_ids": ["REF_AREA", "MEASURE"],
            "valid_values_by_dimension": {"REF_AREA": ["BEFL", "BEFR", "US01"]},
        }

        error = OECDProvider._oecd_country_constraint_error(  # pylint: disable=protected-access
            metadata,
            "CAN",
            "DSD_EAG_WT@DF_STA_TCH_REG",
        )

        self.assertIsNotNone(error)
        self.assertIn("REF_AREA=CAN", error or "")
        self.assertIn("DSD_EAG_WT@DF_STA_TCH_REG", error or "")

    def test_oecd_country_constraint_error_allows_no_ref_area_dataflow(self) -> None:
        metadata = {
            "dimension_ids": ["MEASURE", "FREQ"],
            "valid_values_by_dimension": {"MEASURE": ["GHG"]},
        }

        error = OECDProvider._oecd_country_constraint_error(  # pylint: disable=protected-access
            metadata,
            "USA",
            "DSD_GHG_PLC@DF_GHG_PLC",
        )

        self.assertIsNone(error)

    def test_oecd_country_constraint_error_allows_subnational_prefixes(self) -> None:
        metadata = {
            "dimension_ids": ["REF_AREA", "MEASURE"],
            "valid_values_by_dimension": {"REF_AREA": ["US01", "US02", "BEFL"]},
        }

        error = OECDProvider._oecd_country_constraint_error(  # pylint: disable=protected-access
            metadata,
            "USA",
            "DSD_EAG_WT@DF_STA_TCH_REG",
        )

        self.assertIsNone(error)

    def test_oecd_clamps_default_time_window_to_constraint_range(self) -> None:
        params = {
            "dimensionAtObservation": "AllDimensions",
            "startPeriod": "2021",
            "endPeriod": "2026",
        }
        metadata = {
            "time_ranges": [
                {
                    "dimension": "TIME_PERIOD",
                    "timeRange": {
                        "startPeriod": {"period": "2015-01-01T00:00:00"},
                        "endPeriod": {"period": "2019-12-31T00:00:00"},
                    },
                }
            ]
        }

        OECDProvider._clamp_default_time_params_to_oecd_constraints(  # pylint: disable=protected-access
            params,
            metadata,
        )

        self.assertEqual(params["startPeriod"], "2019")
        self.assertEqual(params["endPeriod"], "2019")

    def test_oecd_default_time_window_prefers_latest_provider_year(self) -> None:
        params = {
            "dimensionAtObservation": "AllDimensions",
            "startPeriod": "2021",
            "endPeriod": "2026",
        }
        metadata = {
            "default_values": {"TIME_PERIOD_END": "2024"},
            "time_ranges": [
                {
                    "dimension": "TIME_PERIOD",
                    "timeRange": {
                        "startPeriod": {"period": "2015-01-01T00:00:00"},
                        "endPeriod": {"period": "2024-12-31T00:00:00"},
                    },
                }
            ],
        }

        OECDProvider._clamp_default_time_params_to_oecd_constraints(  # pylint: disable=protected-access
            params,
            metadata,
        )

        self.assertEqual(params["startPeriod"], "2024")
        self.assertEqual(params["endPeriod"], "2024")

    def test_oecd_provider_advertised_time_params_use_default_span(self) -> None:
        metadata = {
            "default_values": {
                "FREQ": "A3",
                "TIME_PERIOD_START": "2017",
                "TIME_PERIOD_END": "2024",
            },
            "valid_values_by_dimension": {"FREQ": ["A3"]},
            "time_ranges": [
                {
                    "dimension": "TIME_PERIOD",
                    "timeRange": {
                        "startPeriod": {"period": "2015-01-01T00:00:00"},
                        "endPeriod": {"period": "2024-12-31T00:00:00"},
                    },
                }
            ],
        }

        params = OECDProvider._provider_advertised_time_params_from_structure(  # pylint: disable=protected-access
            metadata,
            {
                "dimensionAtObservation": "AllDimensions",
                "startPeriod": "2024",
                "endPeriod": "2024",
            },
        )

        self.assertEqual(
            params,
            {
                "dimensionAtObservation": "AllDimensions",
                "startPeriod": "2017",
                "endPeriod": "2024",
            },
        )

    def test_oecd_default_time_window_preserves_monthly_provider_period(self) -> None:
        params = {
            "dimensionAtObservation": "AllDimensions",
            "startPeriod": "2021",
            "endPeriod": "2026",
        }
        metadata = {
            "default_values": {"FREQ": "M"},
            "valid_values_by_dimension": {"FREQ": ["M"]},
            "time_ranges": [
                {
                    "dimension": "TIME_PERIOD",
                    "timeRange": {
                        "startPeriod": {"period": "1949-01-01T00:00:00"},
                        "endPeriod": {"period": "2026-02-01T00:00:00"},
                    },
                }
            ],
        }

        OECDProvider._clamp_default_time_params_to_oecd_constraints(  # pylint: disable=protected-access
            params,
            metadata,
        )

        self.assertEqual(params["startPeriod"], "2026-02")
        self.assertEqual(params["endPeriod"], "2026-02")

    def test_fred_series_id_explicit_codes_passthrough(self) -> None:
        """Test that explicit FRED series codes pass through directly."""
        provider = FREDProvider(api_key="test-key")

        # Short alphanumeric codes that look like FRED series IDs pass through directly
        explicit_codes = ["GDP", "UNRATE", "FEDFUNDS", "CPIAUCSL", "SP500", "DGS10", "M2SL"]
        for code in explicit_codes:
            with self.subTest(code=code):
                result = provider._series_id(code, None)
                self.assertEqual(result, code,
                    f"Explicit code '{code}' should pass through directly")

    def test_fred_series_id_rejects_natural_language_without_shortcut_fallback(self) -> None:
        """Natural language must use async metadata discovery, not retired shortcuts."""
        provider = FREDProvider(api_key="test-key")

        with self.assertRaises(DataNotAvailableError):
            provider._series_id("GDP growth", None)

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

    def test_fred_fetch_series_infers_country_from_provider_title(self) -> None:
        provider = FREDProvider(api_key="test-key")

        responses = [
            MockAsyncResponse(
                {
                    "seriess": [
                        {
                            "title": "Gross Domestic Product for Canada",
                            "units": "Current U.S. Dollars",
                            "frequency": "Annual",
                            "last_updated": "2026-01-01",
                        }
                    ]
                }
            ),
            MockAsyncResponse(
                {
                    "observations": [
                        {"date": "2024-01-01", "value": "100"},
                    ]
                }
            ),
        ]

        with patch("backend.providers.fred.get_http_client", return_value=MockAsyncClient(responses)):
            result = run(provider.fetch_series({"seriesId": "MKTGDPCAA646NWDB"}))

        self.assertEqual(result.metadata.source, "FRED")
        self.assertEqual(result.metadata.seriesId, "MKTGDPCAA646NWDB")
        self.assertEqual(result.metadata.country, "Canada")

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

    def test_worldbank_recognizes_public_rest_exact_code_shapes(self) -> None:
        provider = WorldBankProvider()

        self.assertTrue(provider._looks_like_worldbank_indicator_code("NY.GDP.MKTP.CD"))  # pylint: disable=protected-access
        self.assertTrue(provider._looks_like_worldbank_indicator_code("fin14q2"))  # pylint: disable=protected-access
        self.assertTrue(provider._looks_like_worldbank_indicator_code("al_prim_some_dfcl_all"))  # pylint: disable=protected-access
        self.assertTrue(provider._looks_like_worldbank_indicator_code("gtap10VALabor"))  # pylint: disable=protected-access
        self.assertFalse(provider._looks_like_worldbank_indicator_code("GDP"))  # pylint: disable=protected-access
        self.assertFalse(provider._looks_like_worldbank_indicator_code("household income"))  # pylint: disable=protected-access

    def test_worldbank_alternatives_prefer_executable_exact_title_wdi_row(self) -> None:
        provider = WorldBankProvider()

        def row(code: str, name: str, source_id: str, source_name: str) -> dict:
            return {
                "provider": "WorldBank",
                "code": code,
                "name": name,
                "raw_metadata": json.dumps(
                    {"source": {"id": source_id, "value": source_name}}
                ),
            }

        primary = row("6.0.GDP_growth", "GDP growth (annual %)", "37", "LAC Equity Lab")
        archived = row(
            "NY.GDP.MKTP.KN.87.ZG",
            "GDP growth (annual %)",
            "57",
            "WDI Database Archives",
        )
        executable_wdi = row(
            "NY.GDP.MKTP.KD.ZG",
            "GDP growth (annual %)",
            "2",
            "World Development Indicators",
        )
        adjacent_topic = row(
            "NV.AGR.PCAP.KD.ZG",
            "Real agricultural GDP per capita growth rate (%)",
            "11",
            "Africa Development Indicators",
        )

        lookup = MagicMock()
        lookup.get.return_value = primary
        lookup.search.return_value = []
        lookup.exact_name_matches.return_value = [primary, archived, executable_wdi]
        db = MagicMock()
        db.search.side_effect = [
            [adjacent_topic, primary],
            [archived, executable_wdi, adjacent_topic],
        ]

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup), \
             patch("backend.services.indicator_database.get_indicator_database", return_value=db):
            alternatives = run(
                provider._get_alternative_indicators(  # pylint: disable=protected-access
                    "GDP growth rate",
                    "6.0.GDP_growth",
                    limit=3,
                )
            )

        self.assertEqual(alternatives[0], "NY.GDP.MKTP.KD.ZG")
        self.assertNotIn("6.0.GDP_growth", alternatives)
        self.assertIn("NY.GDP.MKTP.KN.87.ZG", alternatives)

    def test_worldbank_natural_label_no_data_tries_executable_alternative(self) -> None:
        provider = WorldBankProvider()
        calls = []

        class RecordingClient:
            def __init__(self, responses):
                self.responses = list(responses)

            async def get(self, url, *, params=None, **_kwargs):
                calls.append({"url": str(url), "params": dict(params or {})})
                response = self.responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        primary_empty = MockAsyncResponse(
            [
                {"page": 0, "pages": 0, "per_page": 0, "total": 0, "sourceid": None},
                None,
            ]
        )
        alternative_data = MockAsyncResponse(
            [
                {"page": 1, "pages": 1, "per_page": 1000, "total": 1},
                [
                    {
                        "indicator": {
                            "id": "NY.GDP.MKTP.KD.ZG",
                            "value": "GDP growth (annual %)",
                        },
                        "country": {"id": "CAN", "value": "Canada"},
                        "countryiso3code": "CAN",
                        "date": "2023",
                        "value": 1.2,
                        "unit": "",
                        "obs_status": "",
                        "decimal": 1,
                    }
                ],
            ]
        )
        client = RecordingClient([primary_empty, alternative_data])

        async def resolve_indicator(indicator: str) -> str:
            if indicator == "GDP growth rate":
                return "6.0.GDP_growth"
            return indicator

        with patch("backend.providers.worldbank.get_http1_client", return_value=client), \
             patch("backend.providers.worldbank._wb_is_available", return_value=True), \
             patch.object(provider, "_resolve_indicator_code", new=AsyncMock(side_effect=resolve_indicator)), \
             patch.object(provider, "_get_alternative_indicators", new=AsyncMock(return_value=["NY.GDP.MKTP.KD.ZG"])):
            results = run(
                provider.fetch_indicator(
                    "GDP growth rate",
                    country="CA",
                    start_date="2023-01-01",
                    end_date="2023-12-31",
                )
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("/country/CA/indicator/6.0.GDP_growth", calls[0]["url"])
        self.assertIn("/country/CA/indicator/NY.GDP.MKTP.KD.ZG", calls[1]["url"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.seriesId, "NY.GDP.MKTP.KD.ZG")
        self.assertEqual(results[0].metadata.country, "Canada")
        self.assertEqual(results[0].data[0].value, 1.2)

    def test_worldbank_llm_resolved_code_no_data_can_try_executable_alternative(self) -> None:
        provider = WorldBankProvider()
        calls = []

        class RecordingClient:
            def __init__(self, responses):
                self.responses = list(responses)

            async def get(self, url, *, params=None, **_kwargs):
                calls.append({"url": str(url), "params": dict(params or {})})
                response = self.responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        primary_empty = MockAsyncResponse(
            [
                {"page": 0, "pages": 0, "per_page": 0, "total": 0, "sourceid": None},
                None,
            ]
        )
        alternative_data = MockAsyncResponse(
            [
                {"page": 1, "pages": 1, "per_page": 1000, "total": 1},
                [
                    {
                        "indicator": {
                            "id": "NY.GDP.MKTP.KD.ZG",
                            "value": "GDP growth (annual %)",
                        },
                        "country": {"id": "CAN", "value": "Canada"},
                        "countryiso3code": "CAN",
                        "date": "2023",
                        "value": 1.2,
                        "unit": "",
                        "obs_status": "",
                        "decimal": 1,
                    }
                ],
            ]
        )
        client = RecordingClient([primary_empty, alternative_data])

        with patch("backend.providers.worldbank.get_http1_client", return_value=client), \
             patch("backend.providers.worldbank._wb_is_available", return_value=True), \
             patch.object(provider, "_get_alternative_indicators", new=AsyncMock(return_value=["NY.GDP.MKTP.KD.ZG"])):
            results = run(
                provider.fetch_indicator(
                    "6.0.GDP_growth",
                    country="CA",
                    _allow_semantic_alternatives=True,
                )
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("/country/CA/indicator/6.0.GDP_growth", calls[0]["url"])
        self.assertIn("/country/CA/indicator/NY.GDP.MKTP.KD.ZG", calls[1]["url"])
        self.assertEqual(results[0].metadata.seriesId, "NY.GDP.MKTP.KD.ZG")
        self.assertEqual(results[0].metadata.country, "Canada")

    def test_worldbank_exact_source_indicator_uses_source_series_endpoint(self) -> None:
        provider = WorldBankProvider()
        calls = []

        class RecordingClient(MockAsyncClient):
            async def get(self, url: str, *, params: dict | None = None, **kwargs) -> MockAsyncResponse:
                calls.append({"url": url, "params": dict(params or {})})
                return await super().get(url, params=params, **kwargs)

        missing_response = MockAsyncResponse(
            [
                {
                    "message": [
                        {
                            "id": "175",
                            "key": "Invalid format",
                            "value": "The indicator was not found. It may have been deleted or archived.",
                        }
                    ]
                }
            ]
        )
        source_response = MockAsyncResponse(
            {
                "page": 1,
                "pages": 1,
                "per_page": 10000,
                "total": 1,
                "lastupdated": "2020-07-25",
                "source": {
                    "id": "80",
                    "name": "Gender Disaggregated Labor Database (GDLD)",
                    "data": [
                        {
                            "variable": [
                                {"concept": "Country", "id": "BRA", "value": "Brazil"},
                                {"concept": "Time", "id": "YR2014", "value": "2014"},
                                {
                                    "concept": "Series",
                                    "id": "w_F_skl",
                                    "value": "Annual wage for skilled female workers (US$ Dollars)",
                                },
                                {"concept": "Sector", "id": "TRD", "value": "Trade"},
                            ],
                            "value": 1234.5,
                        }
                    ],
                },
            }
        )
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "WorldBank",
            "code": "w_F_skl",
            "raw_metadata": json.dumps(
                {
                    "source": {
                        "id": "80",
                        "value": "Gender Disaggregated Labor Database (GDLD)",
                    }
                }
            ),
        }

        with patch("backend.providers.worldbank.get_http1_client", return_value=RecordingClient([missing_response, source_response])), \
             patch.object(provider, "_get_alternative_indicators", new=AsyncMock(side_effect=AssertionError("no alternatives for exact codes"))), \
             patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup), \
             patch("backend.providers.worldbank._wb_is_available", return_value=True), \
             patch("backend.providers.worldbank._wb_record_failure"), \
             patch("backend.providers.worldbank._wb_record_success"):
            results = run(provider.fetch_indicator("w_F_skl", country="Brazil", start_date="2014", end_date="2014"))

        self.assertEqual(len(calls), 2)
        self.assertIn("/country/BR/indicator/w_F_skl", calls[0]["url"])
        self.assertIn("/sources/80/country/BRA/series/w_F_skl", calls[1]["url"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.seriesId, "w_F_skl:TRD")
        self.assertEqual(results[0].metadata.country, "Brazil")
        self.assertEqual(results[0].metadata.unit, "USD")
        self.assertEqual(results[0].data[0].date, "2014-01-01")
        self.assertEqual(results[0].data[0].value, 1234.5)

    def test_worldbank_no_country_uses_lowercase_all_endpoint(self) -> None:
        provider = WorldBankProvider()

        class RecordingClient:
            def __init__(self, response):
                self.response = response
                self.urls = []
                self.params = []

            async def get(self, url, *, params=None, **_kwargs):
                self.urls.append(str(url))
                self.params.append(dict(params or {}))
                self.response.request = MockAsyncResponse([], request_url=str(url)).request
                return self.response

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
            ]
        )
        client = RecordingClient(batch_response)

        with patch("backend.providers.worldbank.get_http1_client", return_value=client):
            results = run(provider.fetch_indicator(indicator="NY.GDP.MKTP.CD"))

        self.assertEqual(len(results), 1)
        self.assertTrue(client.urls)
        self.assertIn("/country/all/indicator/NY.GDP.MKTP.CD", client.urls[0])
        self.assertNotIn("/country/ALL/", client.urls[0])
        self.assertEqual(client.params[0].get("MRNEV"), 1)

    def test_worldbank_exact_no_country_retries_world_aggregate_after_empty_all(self) -> None:
        provider = WorldBankProvider()

        class RecordingClient:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append({"url": str(url), "params": dict(params or {})})
                if not self.responses:
                    raise AssertionError("No more mock responses available")
                response = self.responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        empty_all_response = MockAsyncResponse([{"page": 1, "pages": 1, "total": 0}, []])
        world_response = MockAsyncResponse(
            [
                {"page": 1, "pages": 1, "per_page": 1000, "total": 1},
                [
                    {
                        "indicator": {
                            "id": "SI.POV.LMIC",
                            "value": "Poverty headcount ratio at $4.20 a day (2021 PPP) (% of population)",
                        },
                        "country": {"id": "WLD", "value": "World"},
                        "countryiso3code": "WLD",
                        "date": "2024",
                        "value": 18.9,
                        "unit": "",
                        "obs_status": "",
                        "decimal": 0,
                    }
                ],
            ]
        )
        client = RecordingClient([empty_all_response, world_response])

        with patch("backend.providers.worldbank.get_http1_client", return_value=client), \
             patch.object(provider, "_indicator_source_id", return_value="2"):
            results = run(provider.fetch_indicator(indicator="SI.POV.LMIC"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.seriesId, "SI.POV.LMIC")
        self.assertEqual(results[0].metadata.country, "World")
        self.assertEqual(len(client.calls), 2)
        self.assertIn("/country/all/indicator/SI.POV.LMIC", client.calls[0]["url"])
        self.assertIn("/country/WLD/indicator/SI.POV.LMIC", client.calls[1]["url"])
        self.assertEqual(client.calls[0]["params"].get("MRNEV"), 1)
        self.assertEqual(client.calls[1]["params"].get("MRNEV"), 1)

    def test_worldbank_explicit_all_does_not_retry_world_aggregate(self) -> None:
        provider = WorldBankProvider()

        class RecordingClient:
            def __init__(self, response):
                self.response = response
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append({"url": str(url), "params": dict(params or {})})
                self.response.request = MockAsyncResponse([], request_url=str(url)).request
                return self.response

        client = RecordingClient(MockAsyncResponse([{"page": 1, "pages": 1, "total": 0}, []]))

        with patch("backend.providers.worldbank.get_http1_client", return_value=client), \
             patch.object(provider, "_indicator_source_id", return_value="2"), \
             self.assertRaises(DataNotAvailableError):
            run(provider.fetch_indicator(indicator="SI.POV.LMIC", country="all"))

        self.assertEqual(len(client.calls), 1)
        self.assertIn("/country/all/indicator/SI.POV.LMIC", client.calls[0]["url"])

    def test_worldbank_exact_short_catalog_code_uses_source_endpoint_when_generic_empty(self) -> None:
        provider = WorldBankProvider()
        calls = []

        class RecordingClient:
            def __init__(self, responses):
                self.responses = list(responses)

            async def get(self, url, *, params=None, **_kwargs):
                calls.append({"url": str(url), "params": dict(params or {})})
                response = self.responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        generic_empty = MockAsyncResponse(
            [
                {"page": 0, "pages": 0, "per_page": 0, "total": 0, "sourceid": None},
                None,
            ]
        )
        source_response = MockAsyncResponse(
            {
                "page": 1,
                "pages": 1,
                "per_page": 1000,
                "total": 1,
                "lastupdated": "2026-03-31",
                "source": {
                    "id": "15",
                    "name": "Global Economic Monitor",
                    "data": [
                        {
                            "variable": [
                                {"concept": "Country", "id": "AME", "value": "Advanced Economies"},
                                {"concept": "Series", "id": "TOT", "value": "Terms of Trade"},
                                {"concept": "Time", "id": "YR2024", "value": "2024"},
                            ],
                            "value": 101.5,
                        }
                    ],
                },
            }
        )
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "WorldBank",
            "code": "TOT",
            "raw_metadata": json.dumps({"source": {"id": "15", "value": "Global Economic Monitor"}}),
        }

        with patch("backend.providers.worldbank.get_http1_client", return_value=RecordingClient([generic_empty, source_response])), \
             patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup), \
             patch("backend.providers.worldbank._wb_is_available", return_value=True), \
             patch("backend.providers.worldbank._wb_record_failure") as record_failure, \
             patch("backend.providers.worldbank._wb_record_success"):
            results = run(provider.fetch_indicator("TOT"))

        record_failure.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertIn("/country/all/indicator/TOT", calls[0]["url"])
        self.assertIn("/sources/15/country/all/series/TOT", calls[1]["url"])
        self.assertEqual(calls[1]["params"].get("MRNEV"), 5)
        self.assertNotIn("MRV", calls[1]["params"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.seriesId, "TOT")
        self.assertEqual(results[0].metadata.country, "Advanced Economies")
        self.assertEqual(results[0].data[0].date, "2024-01-01")
        self.assertEqual(results[0].data[0].value, 101.5)

    def test_worldbank_source_endpoint_uses_mrnev_and_keeps_native_dimensions(self) -> None:
        provider = WorldBankProvider()
        calls = []

        class RecordingClient:
            def __init__(self, responses):
                self.responses = list(responses)

            async def get(self, url, *, params=None, **_kwargs):
                calls.append({"url": str(url), "params": dict(params or {})})
                response = self.responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        generic_missing = MockAsyncResponse(
            [
                {
                    "message": [
                        {
                            "id": "175",
                            "key": "Invalid format",
                            "value": "The indicator was not found. It may have been deleted or archived.",
                        }
                    ]
                }
            ]
        )
        source_response = MockAsyncResponse(
            {
                "page": 1,
                "pages": 1,
                "per_page": 1000,
                "total": 3,
                "lastupdated": "2025-12-03",
                "source": {
                    "id": "81",
                    "name": "International Debt Statistics: DSSI",
                    "data": [
                        {
                            "variable": [
                                {"concept": "Country", "id": "BGD", "value": "Bangladesh"},
                                {
                                    "concept": "Series",
                                    "id": "DT.INT.DLXF.CD",
                                    "value": "Interest payments on external debt, long-term (INT, current US$)",
                                },
                                {"concept": "Counterpart-Area", "id": "915", "value": "Asian Dev. Bank"},
                                {"concept": "Time", "id": "Monthly", "value": "Monthly"},
                            ],
                            "value": 1568577993.1,
                        },
                        {
                            "variable": [
                                {"concept": "Country", "id": "BGD", "value": "Bangladesh"},
                                {
                                    "concept": "Series",
                                    "id": "DT.INT.DLXF.CD",
                                    "value": "Interest payments on external debt, long-term (INT, current US$)",
                                },
                                {"concept": "Counterpart-Area", "id": "915", "value": "Asian Dev. Bank"},
                                {"concept": "Time", "id": "Annual", "value": "Annual"},
                            ],
                            "value": 5091906245.3,
                        },
                        {
                            "variable": [
                                {"concept": "Country", "id": "BGD", "value": "Bangladesh"},
                                {
                                    "concept": "Series",
                                    "id": "DT.INT.DLXF.CD",
                                    "value": "Interest payments on external debt, long-term (INT, current US$)",
                                },
                                {"concept": "Counterpart-Area", "id": "913", "value": "African Dev. Bank"},
                                {"concept": "Time", "id": "Annual", "value": "Annual"},
                            ],
                            "value": 123.0,
                        },
                    ],
                },
            }
        )
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "WorldBank",
            "code": "DT.INT.DLXF.CD",
            "raw_metadata": json.dumps(
                {"source": {"id": "81", "value": "International Debt Statistics: DSSI"}}
            ),
        }

        with patch("backend.providers.worldbank.get_http1_client", return_value=RecordingClient([generic_missing, source_response])), \
             patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup), \
             patch("backend.providers.worldbank._wb_is_available", return_value=True), \
             patch("backend.providers.worldbank._wb_record_failure") as record_failure, \
             patch("backend.providers.worldbank._wb_record_success"):
            results = run(provider.fetch_indicator("DT.INT.DLXF.CD"))

        record_failure.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertIn("/sources/81/country/all/series/DT.INT.DLXF.CD", calls[1]["url"])
        self.assertEqual(calls[1]["params"].get("MRNEV"), 5)
        self.assertEqual(len(results), 3)
        series_ids = {result.metadata.seriesId for result in results}
        self.assertIn("DT.INT.DLXF.CD:Counterpart-Area=915|Time=Monthly", series_ids)
        self.assertIn("DT.INT.DLXF.CD:Counterpart-Area=915|Time=Annual", series_ids)
        monthly = next(
            result
            for result in results
            if result.metadata.seriesId == "DT.INT.DLXF.CD:Counterpart-Area=915|Time=Monthly"
        )
        self.assertEqual(monthly.metadata.frequency, "monthly")
        self.assertEqual(monthly.data[0].date, "2025-12-03")
        self.assertEqual(monthly.data[0].value, 1568577993.1)
        self.assertTrue(
            any("source lastupdated" in note for note in (monthly.metadata.notes or []))
        )

    def test_worldbank_source_endpoint_converts_iso2_country_to_iso3(self) -> None:
        provider = WorldBankProvider()
        calls = []

        class RecordingClient:
            def __init__(self, responses):
                self.responses = list(responses)

            async def get(self, url, *, params=None, **_kwargs):
                calls.append({"url": str(url), "params": dict(params or {})})
                response = self.responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        generic_missing = MockAsyncResponse(
            [
                {
                    "message": [
                        {
                            "id": "175",
                            "key": "Invalid format",
                            "value": "The indicator was not found. It may have been deleted or archived.",
                        }
                    ]
                }
            ]
        )
        source_response = MockAsyncResponse(
            {
                "page": 1,
                "pages": 1,
                "per_page": 1000,
                "total": 1,
                "lastupdated": "2025-12-18",
                "source": {
                    "id": "88",
                    "name": "Food Prices for Nutrition",
                    "data": [
                        {
                            "variable": [
                                {"concept": "Country", "id": "USA", "value": "United States"},
                                {
                                    "concept": "Series",
                                    "id": "CoHD_v_ss",
                                    "value": "Cost of vegetables relative to the starchy staples in a least-cost healthy diet",
                                },
                                {"concept": "Classification", "id": "FPN 4.1", "value": "FPN 4.1"},
                                {"concept": "Time", "id": "YR2023", "value": "2023"},
                            ],
                            "value": 1.23,
                        },
                    ],
                },
            }
        )
        lookup = MagicMock()
        lookup.get.return_value = {
            "provider": "WorldBank",
            "code": "CoHD_v_ss",
            "raw_metadata": json.dumps({"source": {"id": "88", "value": "Food Prices for Nutrition"}}),
        }

        with patch("backend.providers.worldbank.get_http1_client", return_value=RecordingClient([generic_missing, source_response])), \
             patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup), \
             patch("backend.providers.worldbank._wb_is_available", return_value=True), \
             patch("backend.providers.worldbank._wb_record_failure") as record_failure, \
             patch("backend.providers.worldbank._wb_record_success"):
            results = run(provider.fetch_indicator("CoHD_v_ss", country="US"))

        record_failure.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertIn("/sources/88/country/USA/series/CoHD_v_ss", calls[1]["url"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.country, "United States")
        self.assertEqual(results[0].data[0].value, 1.23)

    def test_worldbank_all_country_fetches_remaining_pages(self) -> None:
        provider = WorldBankProvider()

        class RecordingClient:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append({"url": str(url), "params": dict(params or {})})
                if not self.responses:
                    raise AssertionError("No more mock responses available")
                response = self.responses.pop(0)
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        def _payload(country_id: str, country_name: str, page: int):
            return [
                {"page": page, "pages": 2, "per_page": 1, "total": 2},
                [
                    {
                        "indicator": {"id": "NY.GDP.MKTP.CD", "value": "GDP (current US$)"},
                        "country": {"id": country_id, "value": country_name},
                        "countryiso3code": country_id,
                        "date": "2020",
                        "value": 100 + page,
                        "unit": "",
                        "obs_status": "",
                        "decimal": 0,
                    }
                ],
            ]

        client = RecordingClient(
            [
                MockAsyncResponse(_payload("USA", "United States", 1)),
                MockAsyncResponse(_payload("CAN", "Canada", 2)),
            ]
        )

        with patch("backend.providers.worldbank.get_http1_client", return_value=client):
            results = run(provider.fetch_indicator(indicator="NY.GDP.MKTP.CD"))

        self.assertEqual({result.metadata.country for result in results}, {"United States", "Canada"})
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("page", client.calls[0]["params"])
        self.assertEqual(client.calls[0]["params"].get("MRNEV"), 1)
        self.assertEqual(client.calls[1]["params"].get("page"), 2)
        self.assertEqual(client.calls[1]["params"].get("MRNEV"), 1)

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
        with patch("backend.providers.comtrade_metadata.get_hs_reference_rows", return_value=()), \
             patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
            code = ComtradeProvider._commodity_code(  # pylint: disable=protected-access
                "exports of Yarn of carded wool, not put up for retail sale"
            )

        self.assertEqual(code, "5106")
        self.assertEqual(lookup.provider, "Comtrade")
        self.assertEqual(lookup.text, "Yarn of carded wool, not put up for retail sale")

    def test_comtrade_commodity_code_preserves_specific_hs_subheading_before_broad_terms(self) -> None:
        code = ComtradeProvider._commodity_code(  # pylint: disable=protected-access
            "Steel, stainless; seamless, drill pipe, of a kind used in drilling for oil or gas"
        )

        self.assertEqual(code, "730422")

    def test_comtrade_commodity_code_uses_provider_hs_reference_for_stale_duplicate_title(self) -> None:
        hs_rows = (
            {
                "id": "030741",
                "text": "030741 - -- Live, fresh or chilled",
                "parent": "0307",
                "isLeaf": "1",
                "aggrLevel": 6,
                "standardUnitAbbr": "kg",
            },
            {
                "id": "030742",
                "text": "030742 - Molluscs; cuttle fish and squid, whether in shell or not, live, fresh or chilled",
                "parent": "0307",
                "isLeaf": "1",
                "aggrLevel": 6,
                "standardUnitAbbr": "kg",
            },
        )

        with patch("backend.providers.comtrade_metadata.get_hs_reference_rows", return_value=hs_rows):
            code = ComtradeProvider._commodity_code(  # pylint: disable=protected-access
                "Molluscs; cuttle fish and squid, whether in shell or not, live, fresh or chilled"
            )

        self.assertEqual(code, "030742")

    def test_comtrade_commodity_code_fails_closed_for_provider_hs_reference_tie(self) -> None:
        hs_rows = (
            {
                "id": "030741",
                "text": "030741 - Molluscs; cuttle fish and squid, whether in shell or not, live, fresh or chilled",
                "parent": "0307",
                "isLeaf": "1",
                "aggrLevel": 6,
                "standardUnitAbbr": "kg",
            },
            {
                "id": "030742",
                "text": "030742 - Molluscs; cuttle fish and squid, whether in shell or not, live, fresh or chilled",
                "parent": "0307",
                "isLeaf": "1",
                "aggrLevel": 6,
                "standardUnitAbbr": "kg",
            },
        )

        with self.assertRaises(DataNotAvailableError):
            with patch("backend.providers.comtrade_metadata.get_hs_reference_rows", return_value=hs_rows):
                ComtradeProvider._commodity_code(  # pylint: disable=protected-access
                    "Molluscs; cuttle fish and squid, whether in shell or not, live, fresh or chilled"
                )

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

    def test_comtrade_fetch_trade_data_expands_implicit_sparse_hs_history(self) -> None:
        provider = ComtradeProvider(api_key="demo")
        responses = [
            MockAsyncResponse({"data": []}),  # recent explicit-flow window
            MockAsyncResponse({"data": []}),  # recent both-flow envelope
            MockAsyncResponse(
                {
                    "data": [
                        {
                            "period": 2003,
                            "periodDesc": "2003",
                            "reporterDesc": "China",
                            "partnerDesc": "World",
                            "flowDesc": "Exports",
                            "primaryValue": 97316,
                            "cmdCode": "300110",
                            "cmdDesc": "Glands and other organs",
                        },
                    ]
                }
            ),
            MockAsyncResponse({"data": []}),
        ]
        client = MockAsyncClient(responses)

        with patch("backend.providers.comtrade.get_http_client", return_value=client):
            result = run(
                provider.fetch_trade_data(
                    reporter="China",
                    commodity="300110",
                    flow="EXPORT",
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.indicator, "Exports - Glands and other organs")
        self.assertEqual(result[0].data[0].date, "2003-01-01")
        self.assertEqual(result[0].data[0].value, 97316)
        self.assertIn("period=2002", result[0].metadata.apiUrl)
        self.assertEqual(len(client._responses), 0)  # pylint: disable=protected-access

    def test_comtrade_fetch_trade_data_retries_sparse_hs_with_both_flow_envelope(self) -> None:
        provider = ComtradeProvider(api_key="demo")
        responses = [
            MockAsyncResponse({"data": []}),
            MockAsyncResponse(
                {
                    "data": [
                        {
                            "period": 2020,
                            "periodDesc": "2020",
                            "reporterDesc": "China",
                            "partnerDesc": "World",
                            "flowDesc": "Exports",
                            "primaryValue": 123,
                            "cmdCode": "300110",
                            "cmdDesc": "Glands and other organs",
                        },
                        {
                            "period": 2020,
                            "periodDesc": "2020",
                            "reporterDesc": "China",
                            "partnerDesc": "World",
                            "flowDesc": "Imports",
                            "primaryValue": 50,
                            "cmdCode": "300110",
                            "cmdDesc": "Glands and other organs",
                        },
                    ]
                }
            ),
        ]
        client = MockAsyncClient(responses)

        with patch("backend.providers.comtrade.get_http_client", return_value=client):
            result = run(
                provider.fetch_trade_data(
                    reporter="China",
                    commodity="300110",
                    start_year=2020,
                    end_year=2020,
                    flow="EXPORT",
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.indicator, "Exports - Glands and other organs")
        self.assertEqual(result[0].data[0].value, 123)
        self.assertEqual(len(client._responses), 0)  # pylint: disable=protected-access

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
            return [
                NormalizedData.model_validate(
                    {
                        "metadata": {
                            "source": "UN Comtrade",
                            "indicator": "Exports - All Commodities",
                            "country": reporter_raw,
                            "frequency": "annual",
                            "unit": "USD",
                        },
                        "data": [{"date": "2020-01-01", "value": 1}],
                    }
                )
            ]

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

    def test_imf_exact_long_tail_code_bypasses_translator_fuzzy_proxy(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _Lookup:
            def get(self, provider_name: str, code: str):
                if provider_name == "IMF" and code == "HKG_CPI_PCPI_HEG_L_PCH_YOY_PT":
                    return {
                        "code": "HKG_CPI_PCPI_HEG_L_PCH_YOY_PT",
                        "name": "Hong Kong Definition Consumer Price Index Household Expenditure Group Lower Percent change Year-on-year Percent",
                    }
                return None

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
            code, label = run(provider._resolve_indicator_code("HKG_CPI_PCPI_HEG_L_PCH_YOY_PT"))

        self.assertEqual(code, "HKG_CPI_PCPI_HEG_L_PCH_YOY_PT")
        self.assertIsNotNone(label)

    def test_imf_short_non_weo_catalog_code_fails_closed_without_translator(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _Lookup:
            def get(self, provider_name: str, code: str):
                if provider_name == "IMF" and code == "ABC":
                    return {
                        "code": "ABC",
                        "category": "INDICATOR",
                        "name": "Ambiguous short non-WEO catalog code",
                    }
                return None

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
            with self.assertRaises(DataNotAvailableError):
                run(provider._resolve_indicator_code("ABC"))

    def test_imf_short_datamapper_catalog_code_bypasses_metadata_discovery(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _Lookup:
            def get(self, provider_name: str, code: str):
                if provider_name == "IMF" and code == "TTT":
                    return {
                        "code": "TTT",
                        "category": "AFRREO",
                        "name": "Terms of Trade (Index, 2010 = 100)",
                    }
                return None

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
            code, label = run(provider._resolve_indicator_code("TTT"))

        self.assertEqual(code, "TTT")
        self.assertIsNotNone(label)
        assert label is not None
        self.assertIn("terms of trade", label.lower())

    def test_imf_two_letter_weo_catalog_code_bypasses_metadata_discovery(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _Lookup:
            def get(self, provider_name: str, code: str):
                if provider_name == "IMF" and code == "LP":
                    return {
                        "code": "LP",
                        "category": "WEO",
                        "name": "Population",
                    }
                return None

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
            code, label = run(provider._resolve_indicator_code("LP"))

        self.assertEqual(code, "LP")
        self.assertIsNotNone(label)
        assert label is not None
        self.assertIn("population", label.lower())

    def test_imf_non_datamapper_trade_code_uses_public_sdmx_v21(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _TextResponse(MockAsyncResponse):
            def __init__(self, text: str) -> None:
                super().__init__({})
                self.text = text
                self.content = text.encode("utf-8")

        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">
          <message:DataSet>
            <Series COUNTRY="USA" INDICATOR="XG" TYPE_OF_TRANSFORMATION="FOB_USD" FREQUENCY="A">
              <Obs TIME_PERIOD="2020" OBS_VALUE="100.5" />
              <Obs TIME_PERIOD="2021" OBS_VALUE="110.0" />
            </Series>
          </message:DataSet>
        </message:StructureSpecificData>
        """

        with patch.object(
            provider,
            "_resolve_indicator_code",
            return_value=("TXG_FOB_USD", "External Trade, Exports, Goods, Value, Free on Board, US Dollars"),
        ), patch.object(
            provider,
            "_indicator_catalog_entry",
            return_value={"category": "INDICATOR"},
        ), patch(
            "backend.providers.imf.get_http1_client",
            return_value=MockAsyncClient([_TextResponse(xml)]),
        ):
            result = run(
                provider.fetch_batch_indicator(
                    indicator="TXG_FOB_USD",
                    countries=["USA"],
                    start_year=2020,
                    end_year=2021,
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.seriesId, "TXG_FOB_USD")
        self.assertIn("/IMF.STA,ITG/USA.XG.FOB_USD.A", result[0].metadata.apiUrl or "")
        self.assertEqual(result[0].metadata.sourceUrl, "https://data.imf.org/en/datasets/IMF.STA:ITG")
        self.assertEqual(result[0].data[0].date, "2020-01-01")
        self.assertEqual(result[0].data[0].value, 100.5)

    def test_imf_public_sdmx_csv_response_returns_observations(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _TextResponse(MockAsyncResponse):
            def __init__(self, text: str) -> None:
                super().__init__({})
                self.text = text
                self.content = text.encode("utf-8")
                self.headers = {"content-type": "text/csv"}

        csv_text = (
            "DATAFLOW,COUNTRY,INDICATOR,TYPE_OF_TRANSFORMATION,FREQUENCY,TIME_PERIOD,OBS_VALUE,UNIT\n"
            "IMF.STA:ITG(5.0.0),BRA,MG,CIF_USD,A,2020,166338000000,\n"
            "IMF.STA:ITG(5.0.0),BRA,MG,CIF_USD,A,2021,219408000000,\n"
        )

        with patch.object(
            provider,
            "_resolve_indicator_code",
            return_value=("TMG_CIF_USD", "External Trade, Imports, Goods, Value, CIF, US Dollars"),
        ), patch.object(
            provider,
            "_indicator_catalog_entry",
            return_value={"category": "INDICATOR"},
        ), patch(
            "backend.providers.imf.get_http1_client",
            return_value=MockAsyncClient([_TextResponse(csv_text)]),
        ):
            result = run(
                provider.fetch_batch_indicator(
                    indicator="TMG_CIF_USD",
                    countries=["Brazil"],
                    start_year=2020,
                    end_year=2021,
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "Brazil")
        self.assertIn("/IMF.STA,ITG/BRA.MG.CIF_USD.A", result[0].metadata.apiUrl or "")
        self.assertEqual([point.value for point in result[0].data], [166338000000.0, 219408000000.0])

    def test_imf_non_datamapper_cpi_code_infers_iso3_prefix_for_public_sdmx_v21(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _TextResponse(MockAsyncResponse):
            def __init__(self, text: str) -> None:
                super().__init__({})
                self.text = text
                self.content = text.encode("utf-8")

        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">
          <message:DataSet>
            <Series COUNTRY="NPL" INDEX_TYPE="CPI" COICOP_1999="CP01" TYPE_OF_TRANSFORMATION="IX" FREQUENCY="A">
              <Obs TIME_PERIOD="2020" OBS_VALUE="145.2" />
            </Series>
          </message:DataSet>
        </message:StructureSpecificData>
        """

        with patch.object(
            provider,
            "_resolve_indicator_code",
            return_value=("NPL_PCPI_CP_01_IX", "Nepal Consumer Price Index Food, Index"),
        ), patch.object(
            provider,
            "_indicator_catalog_entry",
            return_value={"category": "INDICATOR"},
        ), patch(
            "backend.providers.imf.get_http1_client",
            return_value=MockAsyncClient([_TextResponse(xml)]),
        ):
            result = run(
                provider.fetch_batch_indicator(
                    indicator="NPL_PCPI_CP_01_IX",
                    countries=["USA"],
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "Nepal")
        self.assertIn("/IMF.STA,CPI/NPL.CPI.CP01.IX.A", result[0].metadata.apiUrl or "")
        self.assertEqual(result[0].data[0].value, 145.2)

    def test_imf_non_datamapper_cpi_base_year_code_uses_public_sdmx_v21(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        class _TextResponse(MockAsyncResponse):
            def __init__(self, text: str) -> None:
                super().__init__({})
                self.text = text
                self.content = text.encode("utf-8")

        empty_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">
          <message:DataSet />
        </message:StructureSpecificData>
        """
        monthly_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">
          <message:DataSet>
            <Series COUNTRY="BRA" INDEX_TYPE="CPI" COICOP_1999="CP01" TYPE_OF_TRANSFORMATION="IX" FREQUENCY="M">
              <Obs TIME_PERIOD="2020-M01" OBS_VALUE="110.0" />
            </Series>
          </message:DataSet>
        </message:StructureSpecificData>
        """

        with patch.object(
            provider,
            "_resolve_indicator_code",
            return_value=(
                "PCPI_CP_01_BY2010_IX",
                "Prices, Consumer Price Index, Food and non-alcoholic beverages, Base Year = 2010, Index",
            ),
        ), patch.object(
            provider,
            "_indicator_catalog_entry",
            return_value={"category": "INDICATOR"},
        ), patch(
            "backend.providers.imf.get_http1_client",
            return_value=MockAsyncClient([_TextResponse(empty_xml), _TextResponse(monthly_xml)]),
        ):
            result = run(
                provider.fetch_batch_indicator(
                    indicator="PCPI_CP_01_BY2010_IX",
                    countries=["Brazil"],
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "Brazil")
        self.assertIn("/IMF.STA,CPI/BRA.CPI.CP01.IX.M", result[0].metadata.apiUrl or "")
        self.assertEqual(result[0].metadata.seriesId, "PCPI_CP_01_BY2010_IX")
        self.assertEqual(result[0].metadata.frequency, "monthly")
        self.assertEqual(result[0].data[0].date, "2020-01-01")
        self.assertEqual(result[0].data[0].value, 110.0)

    def test_imf_detailed_trade_code_still_fails_closed_without_public_sdmx_mapping(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        with patch.object(
            provider,
            "_resolve_indicator_code",
            return_value=("TMG_H5_84T86_CIF_USD", "Detailed HS imports"),
        ), patch.object(
            provider,
            "_indicator_catalog_entry",
            return_value={"category": "INDICATOR"},
        ):
            with self.assertRaises(DataNotAvailableError) as ctx:
                run(provider.fetch_batch_indicator(indicator="TMG_H5_84T86_CIF_USD", countries=["USA"]))

        self.assertIn("requires IMF dataset-family routing", str(ctx.exception))

    def test_imf_parses_bop_dataflow_structure_dimensions(self) -> None:
        payload = {
            "data": {
                "codelists": [{"id": "CL_COUNTRY", "codes": [{"id": "USA"}, {"id": "BRA"}]}],
                "dataflows": [{"id": "BOP", "agencyID": "IMF.STA", "version": "21.0.0"}],
                "dataStructures": [
                    {
                        "id": "DSD_BOP",
                        "agencyID": "IMF.STA",
                        "version": "17.0.0",
                        "dataStructureComponents": {
                            "dimensionList": {
                                "dimensions": [
                                    {
                                        "id": "COUNTRY",
                                        "position": 0,
                                        "type": "Dimension",
                                        "localRepresentation": {
                                            "enumeration": "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=IMF:CL_COUNTRY(1.0.0)"
                                        },
                                    },
                                    {"id": "BOP_ACCOUNTING_ENTRY", "position": 1, "type": "Dimension"},
                                    {"id": "INDICATOR", "position": 2, "type": "Dimension"},
                                    {"id": "UNIT", "position": 3, "type": "Dimension"},
                                    {"id": "FREQUENCY", "position": 4, "type": "Dimension"},
                                ],
                                "timeDimensions": [{"id": "TIME_PERIOD", "position": 5, "type": "TimeDimension"}],
                            }
                        },
                    }
                ],
            }
        }

        metadata = IMFProvider._parse_imf_dataflow_structure(payload)  # pylint: disable=protected-access

        self.assertEqual(metadata["agency"], "IMF.STA")
        self.assertEqual(metadata["dataflow"], "BOP")
        self.assertEqual(metadata["dimension_ids"], ["COUNTRY", "BOP_ACCOUNTING_ENTRY", "INDICATOR", "UNIT", "FREQUENCY"])
        self.assertEqual(metadata["time_dimension_ids"], ["TIME_PERIOD"])
        self.assertEqual(metadata["dimensions"][0]["value_count"], 2)

    def test_imf_parses_xml_bop_dataflow_structure_dimensions(self) -> None:
        payload = """<?xml version='1.0' encoding='UTF-8'?>
        <mes:Structure xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
            xmlns:str="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure"
            xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
            xmlns:xml="http://www.w3.org/XML/1998/namespace">
          <mes:Structures>
            <str:Dataflows>
              <str:Dataflow agencyID="IMF.STA" id="BOP" version="21.0.0">
                <com:Name xml:lang="en">Balance of Payments (BOP)</com:Name>
              </str:Dataflow>
            </str:Dataflows>
            <str:DataStructures>
              <str:DataStructure agencyID="IMF.STA" id="DSD_BOP" version="24.0.0">
                <str:DataStructureComponents>
                  <str:DimensionList>
                    <str:Dimension id="COUNTRY" position="0"><str:ConceptIdentity><Ref id="COUNTRY"/></str:ConceptIdentity></str:Dimension>
                    <str:Dimension id="BOP_ACCOUNTING_ENTRY" position="1"><str:ConceptIdentity><Ref id="BOP_ACCOUNTING_ENTRY"/></str:ConceptIdentity></str:Dimension>
                    <str:Dimension id="INDICATOR" position="2"><str:ConceptIdentity><Ref id="INDICATOR"/></str:ConceptIdentity></str:Dimension>
                    <str:Dimension id="UNIT" position="3"><str:ConceptIdentity><Ref id="UNIT"/></str:ConceptIdentity></str:Dimension>
                    <str:Dimension id="FREQUENCY" position="4"><str:ConceptIdentity><Ref id="FREQ"/></str:ConceptIdentity></str:Dimension>
                    <str:TimeDimension id="TIME_PERIOD"><str:ConceptIdentity><Ref id="TIME_PERIOD"/></str:ConceptIdentity></str:TimeDimension>
                  </str:DimensionList>
                </str:DataStructureComponents>
              </str:DataStructure>
            </str:DataStructures>
            <str:Codelists>
              <str:Codelist agencyID="IMF.STA" id="CL_BOP_ACCOUNTING_ENTRY" version="3.3.0">
                <str:Code id="BX"><com:Name xml:lang="en">Credit</com:Name></str:Code>
              </str:Codelist>
              <str:Codelist agencyID="IMF.STA" id="CL_BOP_INDICATOR" version="1.0.0">
                <str:Code id="SRLO"><com:Name xml:lang="en">Royalties</com:Name></str:Code>
              </str:Codelist>
            </str:Codelists>
          </mes:Structures>
        </mes:Structure>
        """

        metadata = IMFProvider._parse_imf_dataflow_structure(payload)  # pylint: disable=protected-access

        self.assertEqual(metadata["agency"], "IMF.STA")
        self.assertEqual(metadata["dataflow"], "BOP")
        self.assertEqual(metadata["dsd_id"], "DSD_BOP")
        self.assertEqual(metadata["dimension_ids"], ["COUNTRY", "BOP_ACCOUNTING_ENTRY", "INDICATOR", "UNIT", "FREQUENCY"])
        self.assertEqual(metadata["time_dimension_ids"], ["TIME_PERIOD"])
        self.assertEqual(metadata["codelist_sizes"]["CL_BOP_INDICATOR"], 1)
        self.assertEqual(metadata["allowed_values_by_dimension"]["BOP_ACCOUNTING_ENTRY"], {"BX"})
        self.assertEqual(metadata["allowed_values_by_dimension"]["INDICATOR"], {"SRLO"})

    def test_imf_parses_flow_specific_sdmx_codelist_aliases(self) -> None:
        payload = {
            "data": {
                "codelists": [
                    {"id": "CL_LS_INDICATOR", "codes": [{"id": "U"}, {"id": "LF"}]},
                    {"id": "CL_LS_TYPE_OF_TRANSFORMAtION", "codes": [{"id": "PT"}, {"id": "PE"}]},
                    {"id": "CL_FREQ", "codes": [{"id": "A"}]},
                ],
                "dataflows": [{"id": "LS", "agencyID": "IMF.STA", "version": "9.0.0"}],
                "dataStructures": [
                    {
                        "id": "DSD_LS",
                        "agencyID": "IMF.STA",
                        "dataStructureComponents": {
                            "dimensionList": {
                                "dimensions": [
                                    {"id": "INDICATOR", "position": 0, "type": "Dimension"},
                                    {"id": "TYPE_OF_TRANSFORMATION", "position": 1, "type": "Dimension"},
                                    {"id": "FREQUENCY", "position": 2, "type": "Dimension"},
                                ]
                            }
                        },
                    }
                ],
            }
        }

        metadata = IMFProvider._parse_imf_dataflow_structure(payload)  # pylint: disable=protected-access

        self.assertEqual(metadata["allowed_values_by_dimension"]["INDICATOR"], {"U", "LF"})
        self.assertEqual(metadata["allowed_values_by_dimension"]["TYPE_OF_TRANSFORMATION"], {"PT", "PE"})
        self.assertEqual(metadata["allowed_values_by_dimension"]["FREQUENCY"], {"A"})

    def test_imf_parses_xml_flow_specific_sdmx_codelist_aliases(self) -> None:
        payload = """<?xml version='1.0' encoding='UTF-8'?>
        <mes:Structure xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
            xmlns:str="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure"
            xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common">
          <mes:Structures>
            <str:Dataflows>
              <str:Dataflow agencyID="IMF.STA" id="PPI" version="3.0.0" />
            </str:Dataflows>
            <str:DataStructures>
              <str:DataStructure agencyID="IMF.STA" id="DSD_PPI" version="3.0.0">
                <str:DataStructureComponents>
                  <str:DimensionList>
                    <str:Dimension id="INDICATOR" position="0"><str:ConceptIdentity><Ref id="INDICATOR"/></str:ConceptIdentity></str:Dimension>
                    <str:Dimension id="TYPE_OF_TRANSFORMATION" position="1"><str:ConceptIdentity><Ref id="TYPE_OF_TRANSFORMATION"/></str:ConceptIdentity></str:Dimension>
                  </str:DimensionList>
                </str:DataStructureComponents>
              </str:DataStructure>
            </str:DataStructures>
            <str:Codelists>
              <str:Codelist agencyID="IMF.STA" id="CL_PPI_INDICATOR" version="1.0.0">
                <str:Code id="PPI"><com:Name xml:lang="en">Producer price index</com:Name></str:Code>
                <str:Code id="WPI"><com:Name xml:lang="en">Wholesale price index</com:Name></str:Code>
              </str:Codelist>
              <str:Codelist agencyID="IMF.STA" id="CL_PPI_TYPE_OF_TRANSFORMATION" version="1.0.0">
                <str:Code id="IX"><com:Name xml:lang="en">Index</com:Name></str:Code>
              </str:Codelist>
            </str:Codelists>
          </mes:Structures>
        </mes:Structure>
        """

        metadata = IMFProvider._parse_imf_dataflow_structure(payload)  # pylint: disable=protected-access

        self.assertEqual(metadata["allowed_values_by_dimension"]["INDICATOR"], {"PPI", "WPI"})
        self.assertEqual(metadata["allowed_values_by_dimension"]["TYPE_OF_TRANSFORMATION"], {"IX"})

    def test_imf_sdmx_candidate_builder_uses_exact_code_not_label_semantics(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        cpi_candidates = provider._build_sdmx_series_candidates(  # pylint: disable=protected-access
            indicator_code="PCPI_IX",
            indicator_label="Food and non-alcoholic beverages consumer price index",
            countries=["USA"],
        )
        trade_candidates = provider._build_sdmx_series_candidates(  # pylint: disable=protected-access
            indicator_code="TXG_CIF_USD",
            indicator_label="Imports of goods, value, cost insurance and freight",
            countries=["USA"],
        )
        ppi_candidates = provider._build_sdmx_series_candidates(  # pylint: disable=protected-access
            indicator_code="NOT_PPI_IX",
            indicator_label="Producer price index",
            countries=["USA"],
        )
        trade_index_candidates = provider._build_sdmx_series_candidates(  # pylint: disable=protected-access
            indicator_code="TXG_FOB_USD_IX",
            indicator_label="Exports of goods index",
            countries=["USA"],
        )

        self.assertEqual([candidate["key"] for candidate in cpi_candidates], ["USA.CPI._T.IX.A"])
        self.assertEqual([candidate["key"] for candidate in trade_candidates], ["USA.XG.CIF_USD.A"])
        self.assertEqual(ppi_candidates, [])
        self.assertEqual([candidate["key"] for candidate in trade_index_candidates], ["USA.XG.FOB_USD_IX.A"])

    def test_imf_fetches_and_caches_bop_dataflow_structure(self) -> None:
        provider = IMFProvider(metadata_search_service=None)
        provider._DATAFLOW_STRUCTURE_CACHE.clear()  # pylint: disable=protected-access
        payload = {
            "data": {
                "dataflows": [{"id": "BOP", "agencyID": "IMF.STA", "version": "21.0.0"}],
                "dataStructures": [
                    {
                        "id": "DSD_BOP",
                        "dataStructureComponents": {
                            "dimensionList": {
                                "dimensions": [{"id": "COUNTRY", "position": 0}],
                                "timeDimensions": [{"id": "TIME_PERIOD", "position": 1}],
                            }
                        },
                    }
                ],
            }
        }
        response = MockAsyncResponse(
            payload,
            request_url="https://api.imf.org/external/sdmx/2.1/dataflow/IMF.STA/BOP/latest?references=all",
        )

        with patch("backend.providers.imf.get_http1_client", return_value=MockAsyncClient([response])):
            first = run(provider._get_imf_dataflow_structure(dataflow="BOP"))  # pylint: disable=protected-access
            second = run(provider._get_imf_dataflow_structure(dataflow="BOP"))  # pylint: disable=protected-access

        self.assertIs(first, second)
        self.assertEqual(first["dataflow"], "BOP")
        self.assertEqual(first["dimension_ids"], ["COUNTRY"])
        self.assertEqual(first["time_dimension_ids"], ["TIME_PERIOD"])
        self.assertIn("dataflow/IMF.STA/BOP/latest", first["structureUrl"])

    def test_imf_bop_exact_code_fails_closed_without_no_rule_dimension_contract(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        with patch.object(
            provider,
            "_resolve_indicator_code",
            new=AsyncMock(return_value=(
                "BMISO_BP6_FY_USD",
                "Balance of Payments, Current Account, Secondary Income, Debit [BPM6], Fiscal Year, US Dollars",
            )),
        ), patch.object(
            provider, "_classify_execution_family", return_value="NON_DATAMAPPER_INDICATOR",
        ), patch.object(
            provider, "_build_sdmx_series_candidates", return_value=[],
        ):
            with self.assertRaisesRegex(DataNotAvailableError, "no-rule authority policy"):
                run(
                    provider.fetch_batch_indicator(
                        indicator="BMISO_BP6_FY_USD",
                        countries=["United States"],
                    )
                )

    def test_imf_short_natural_language_phrase_fails_closed_without_metadata_search(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        with self.assertRaises(DataNotAvailableError):
            run(provider._resolve_indicator_code("GDP"))

    def test_imf_fetch_batch_does_not_swap_to_alternative_code_when_primary_missing(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        responses = [
            MockAsyncResponse({"values": {}}),
        ]

        with patch.object(provider, "_resolve_indicator_code", return_value=("PPI", "Producer Price Index")), \
             patch("backend.providers.imf.get_http_client", return_value=MockAsyncClient(responses)):
            with self.assertRaises(DataNotAvailableError):
                run(
                    provider.fetch_batch_indicator(
                        indicator="producer price inflation",
                        countries=["USA", "DEU"],
                    )
                )

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

    def test_bis_credit_gap_uses_quarterly_gap_series(self) -> None:
        provider = BISProvider(metadata_search_service=None)

        response = MockAsyncResponse(
            {
                "data": {
                    "dataSets": [
                        {
                            "series": {
                                "0:0:0:0:0": {
                                    "observations": {
                                        "0": ["201.4"],
                                    }
                                },
                                "0:0:0:0:1": {
                                    "observations": {
                                        "0": ["207.8"],
                                    }
                                },
                                "0:0:0:0:2": {
                                    "observations": {
                                        "0": ["-6.4"],
                                    }
                                },
                            }
                        }
                    ],
                    "structure": {
                        "dimensions": {
                            "series": [
                                {"id": "FREQ", "values": [{"id": "Q"}]},
                                {"id": "BORROWERS_CTY", "values": [{"id": "CN"}]},
                                {"id": "TC_BORROWERS", "values": [{"id": "P"}]},
                                {"id": "TC_LENDERS", "values": [{"id": "A"}]},
                                {
                                    "id": "CG_DTYPE",
                                    "values": [
                                        {"id": "A", "name": "Credit-to-GDP ratios (actual data)"},
                                        {"id": "B", "name": "Credit-to-GDP trend (HP filter)"},
                                        {"id": "C", "name": "Credit-to-GDP gaps (actual-trend)"},
                                    ],
                                },
                            ],
                            "observation": [
                                {"id": "TIME_PERIOD", "values": [{"id": "2025-Q3"}]},
                            ],
                        }
                    },
                }
            }
        )

        with patch("backend.providers.bis.get_http_client", return_value=MockAsyncClient([response])):
            series_list = run(provider.fetch_indicator(indicator="BIS_WS_CREDIT_GAP", country="CN"))

        self.assertEqual(len(series_list), 1)
        self.assertEqual(series_list[0].metadata.indicator, "Credit-to-GDP gaps")
        self.assertEqual(series_list[0].metadata.frequency, "quarterly")
        self.assertEqual(series_list[0].metadata.unit, "percentage points")
        self.assertEqual(series_list[0].metadata.dataType, "Gap")
        self.assertIn("/data/WS_CREDIT_GAP/Q.CN", series_list[0].metadata.apiUrl)
        self.assertEqual(series_list[0].data[0].value, -6.4)

    def test_bis_prefixed_dataflow_codes_are_mechanical_passthrough(self) -> None:
        provider = BISProvider(metadata_search_service=None)

        self.assertEqual(
            run(provider._resolve_indicator_code("BIS_WS_CBPOL")),  # pylint: disable=protected-access
            ("WS_CBPOL", None),
        )
        self.assertEqual(
            run(provider._resolve_indicator_code("BIS.WS_XRU")),  # pylint: disable=protected-access
            ("WS_XRU", None),
        )

    def test_bis_exact_dataflow_fallback_selects_requested_country_series(self) -> None:
        provider = BISProvider(metadata_search_service=None)

        no_data = MockAsyncResponse({"errors": [{"code": 404}]}, status_code=404)
        fallback_payload = {
            "data": {
                "dataSets": [
                    {
                        "series": {
                            "0:0:0": {"observations": {"0": [10], "1": [11]}},
                            "0:1:0": {"observations": {"0": [20], "1": [21], "2": [22]}},
                        }
                    }
                ],
                "structure": {
                    "dimensions": {
                        "series": [
                            {"id": "FREQ", "values": [{"id": "A", "name": "Annual"}]},
                            {
                                "id": "REP_CTY",
                                "name": "Reporting country",
                                "values": [
                                    {"id": "US", "name": "United States"},
                                    {"id": "JP", "name": "Japan"},
                                ],
                            },
                            {"id": "MEASURE", "values": [{"id": "A", "name": "All"}]},
                        ],
                        "observation": [
                            {
                                "id": "TIME_PERIOD",
                                "values": [
                                    {"id": "2020"},
                                    {"id": "2021"},
                                    {"id": "2022"},
                                ],
                            }
                        ],
                    }
                },
            }
        }

        client = MockAsyncClient([no_data, no_data, MockAsyncResponse(fallback_payload)])
        with patch("backend.providers.bis.get_http_client", return_value=client):
            series_list = run(
                provider.fetch_indicator(
                    indicator="BIS_WS_CPMI_CASHLESS",
                    country="United States",
                    frequency="A",
                )
            )

        self.assertEqual(len(series_list), 1)
        self.assertEqual(series_list[0].metadata.source, "BIS")
        self.assertEqual(series_list[0].metadata.country, "United States")
        self.assertEqual(series_list[0].metadata.frequency, "annual")
        self.assertIn("WS_CPMI_CASHLESS/A/0:0:0", series_list[0].metadata.seriesId)
        self.assertIn("/data/WS_CPMI_CASHLESS/A", series_list[0].metadata.apiUrl)
        self.assertEqual(series_list[0].data[0].value, 10.0)

    def test_bis_exact_dataflow_fallback_fails_closed_on_country_mismatch(self) -> None:
        provider = BISProvider(metadata_search_service=None)

        no_data = MockAsyncResponse({"errors": [{"code": 404}]}, status_code=404)
        fallback_payload = {
            "data": {
                "dataSets": [{"series": {"0:0:0": {"observations": {"0": [10]}}}}],
                "structure": {
                    "dimensions": {
                        "series": [
                            {"id": "FREQ", "values": [{"id": "A", "name": "Annual"}]},
                            {
                                "id": "REP_CTY",
                                "name": "Reporting country",
                                "values": [{"id": "JP", "name": "Japan"}],
                            },
                            {"id": "MEASURE", "values": [{"id": "A", "name": "All"}]},
                        ],
                        "observation": [{"id": "TIME_PERIOD", "values": [{"id": "2020"}]}],
                    }
                },
            }
        }

        client = MockAsyncClient([
            no_data,
            no_data,
            MockAsyncResponse(fallback_payload),
            no_data,
            no_data,
            no_data,
            no_data,
        ])
        with patch("backend.providers.bis.get_http_client", return_value=client):
            with self.assertRaises(DataNotAvailableError):
                run(
                    provider.fetch_indicator(
                        indicator="BIS_WS_CPMI_CASHLESS",
                        country="United States",
                        frequency="A",
                    )
                )

    def test_imf_bop_bridge_fails_closed_before_label_matching(self) -> None:
        provider = IMFProvider(metadata_search_service=None)

        with patch.object(
            provider,
            "_resolve_indicator_code",
            new=AsyncMock(return_value=("BMISO_BP6_FY_USD", "BOP secondary income debit")),
        ), patch.object(
            provider,
            "_classify_execution_family",
            return_value="NON_DATAMAPPER_INDICATOR",
        ), patch.object(
            provider,
            "_build_sdmx_series_candidates",
            return_value=[],
        ), patch.object(
            provider,
            "_fetch_bop_family",
            side_effect=AssertionError("BOP label-to-codelist bridge must stay disabled"),
        ):
            with self.assertRaisesRegex(DataNotAvailableError, "no-rule authority policy"):
                run(provider.fetch_batch_indicator("BMISO_BP6_FY_USD", ["USA"]))

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

    def test_eurostat_jsonstat_parser_uses_sparse_non_default_tuple(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)
        response = MockAsyncResponse(
            {
                "value": {"2": 1000, "3": 1100},
                "dimension": {
                    "unit": {
                        "category": {
                            "index": {"EUR": 0},
                            "label": {"EUR": "Euro"},
                        }
                    },
                    "nace_r1": {
                        "category": {
                            "index": {"C-O": 0, "C-K": 1},
                            "label": {
                                "C-O": "All NACE activities",
                                "C-K": "Industry and services",
                            },
                        }
                    },
                    "geo": {
                        "category": {
                            "index": {"FR": 0},
                            "label": {"FR": "France"},
                        }
                    },
                    "time": {
                        "category": {
                            "index": {"2002": 0, "2003": 1},
                            "label": {"2002": "2002", "2003": "2003"},
                        }
                    },
                },
                "id": ["unit", "nace_r1", "geo", "time"],
                "size": [1, 2, 1, 2],
                "updated": "2024-01-01",
            }
        )

        with patch("backend.providers.eurostat.get_http_client", return_value=MockAsyncClient([response])):
            series = run(
                provider.fetch_indicator(
                    indicator="custom_sparse",
                    country="FR",
                    start_year=2002,
                    end_year=2003,
                )
            )

        self.assertEqual(series.metadata.seriesId, "custom_sparse")
        self.assertEqual(series.data[0].date, "2002-01-01")
        self.assertEqual(series.data[0].value, 1000)
        self.assertEqual(series.data[1].date, "2003-01-01")
        self.assertEqual(series.data[1].value, 1100)

    def test_eurostat_resolve_accepts_uppercase_table_code_with_digits(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        dataset_code, dataset_label = run(provider._resolve_dataset("TEC00118"))

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

    def test_eurostat_nuts2_table_uses_representative_geo_and_defaults(self) -> None:
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
                "value": {"0": 68.3, "1": 69.8},
                "dimension": {
                    "time": {"category": {"index": {"2023": 0, "2024": 1}}},
                    "geo": {"category": {"index": {"FR10": 0}, "label": {"FR10": "Ile-de-France"}}},
                    "unit": {"category": {"index": {"PC": 0}, "label": {"PC": "Percentage"}}},
                },
                "id": ["geo", "time"],
                "size": [1, 2],
                "updated": "2026-04-17",
            }
        )
        client = RecordingClient(response)

        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="TGS00007", country="FR", start_year=2023))

        self.assertEqual(series.metadata.seriesId, "tgs00007")
        self.assertEqual(series.metadata.country, "FR10")
        _, params = client.calls[0]
        self.assertEqual(params.get("geo"), "FR10")
        self.assertEqual(params.get("unit"), "PC")
        self.assertEqual(params.get("sex"), "T")
        self.assertEqual(params.get("age"), "Y15-64")

    def test_eurostat_nuts2_social_exclusion_table_uses_representative_geo(self) -> None:
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
                "value": {"0": 18.4, "1": 17.9},
                "dimension": {
                    "time": {"category": {"index": {"2023": 0, "2024": 1}}},
                    "geo": {"category": {"index": {"DE30": 0}, "label": {"DE30": "Berlin"}}},
                    "unit": {"category": {"index": {"PC_POP": 0}, "label": {"PC_POP": "Percentage of population"}}},
                },
                "id": ["geo", "time"],
                "size": [1, 2],
                "updated": "2026-04-17",
            }
        )
        client = RecordingClient(response)

        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="TGS00107", country="DE", start_year=2023))

        self.assertEqual(series.metadata.seriesId, "tgs00107")
        self.assertEqual(series.metadata.country, "DE30")
        _, params = client.calls[0]
        self.assertEqual(params.get("geo"), "DE30")
        self.assertEqual(params.get("unit"), "PC_POP")

    def test_eurostat_city_rent_table_uses_capital_geo_and_rent_defaults(self) -> None:
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
                "value": {"0": 920, "1": 980},
                "dimension": {
                    "time": {"category": {"index": {"2022": 0, "2023": 1}}},
                    "geo": {"category": {"index": {"ES_CAP": 0}, "label": {"ES_CAP": "Madrid"}}},
                    "currency": {"category": {"index": {"EUR": 0}, "label": {"EUR": "Euro"}}},
                },
                "id": ["geo", "time"],
                "size": [1, 2],
                "updated": "2026-01-06",
            }
        )
        client = RecordingClient(response)

        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="PRC_COLC_RENTS", country="ES", start_year=2022))

        self.assertEqual(series.metadata.seriesId, "prc_colc_rents")
        self.assertEqual(series.metadata.country, "ES_CAP")
        self.assertEqual(series.metadata.unit, "Euro")
        _, params = client.calls[0]
        self.assertEqual(params.get("geo"), "ES_CAP")
        self.assertEqual(params.get("building"), "FLAT2")
        self.assertEqual(params.get("currency"), "EUR")

    def test_eurostat_fetch_indicator_forwards_provider_dimension_filters_mechanically(self) -> None:
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
                "value": {"0": 12.5},
                "dimension": {
                    "sex": {"category": {"index": {"F": 0}, "label": {"F": "Females"}}},
                    "age": {"category": {"index": {"Y16-24": 0}, "label": {"Y16-24": "16 to 24 years"}}},
                    "geo": {"category": {"index": {"FR": 0}, "label": {"FR": "France"}}},
                    "time": {"category": {"index": {"2024": 0}, "label": {"2024": "2024"}}},
                },
                "id": ["sex", "age", "geo", "time"],
                "size": [1, 1, 1, 1],
                "updated": "2026-01-06",
            }
        )
        client = RecordingClient(response)

        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(
                provider.fetch_indicator(
                    indicator="HLTH_EHIS_PL1E",
                    country="FR",
                    start_year=2024,
                    filters={
                        "sex": "F",
                        "age": "Y16-24",
                        "indicator": "SHOULD_NOT_BE_A_DIMENSION",
                        "__semantic_provider_locked": True,
                        "geo": "DE",
                        "freq": "M",
                        "time_period": "2023",
                        "empty": "",
                    },
                )
            )

        self.assertEqual(series.metadata.seriesId, "hlth_ehis_pl1e")
        _, params = client.calls[0]
        self.assertEqual(params.get("geo"), "FR")
        self.assertEqual(params.get("freq"), "A")
        self.assertEqual(params.get("sex"), "F")
        self.assertEqual(params.get("age"), "Y16-24")
        self.assertNotIn("indicator", params)
        self.assertNotIn("__semantic_provider_locked", params)
        self.assertNotIn("time_period", params)
        self.assertNotIn("empty", params)

    def test_eurostat_exact_dataset_can_fetch_without_geo_filter(self) -> None:
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
                "value": {"0": 10.0, "1": 11.0},
                "dimension": {
                    "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
                    "unit": {"category": {"index": {"PC": 0}, "label": {"PC": "Percentage"}}},
                    "stk_flow": {
                        "category": {
                            "index": {"IMP": 0, "EXP": 1},
                            "label": {"IMP": "Imports", "EXP": "Exports"},
                        }
                    },
                    "geo": {
                        "category": {
                            "index": {"MD": 0, "GE": 1},
                            "label": {"MD": "Moldova", "GE": "Georgia"},
                        }
                    },
                    "time": {"category": {"index": {"2023": 0, "2024": 1}}},
                },
                "id": ["freq", "unit", "stk_flow", "geo", "time"],
                "size": [1, 1, 2, 2, 2],
                "updated": "2026-02-03",
            }
        )
        client = RecordingClient(response)

        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="ENPE_EXT_INTRO", country="__ALL__", start_year=2023))

        self.assertEqual(series.metadata.seriesId, "enpe_ext_intro")
        self.assertEqual(series.metadata.country, "ALL_AVAILABLE")
        self.assertEqual(len(series.data), 2)
        _, params = client.calls[0]
        self.assertNotIn("geo", params)
        self.assertEqual(params.get("freq"), "A")

    def test_eurostat_all_available_empty_result_retries_latest_without_inferred_freq(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        empty_response = MockAsyncResponse(
            {
                "value": {},
                "dimension": {
                    "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
                    "geo": {"category": {"index": {"FR": 0}, "label": {"FR": "France"}}},
                    "time": {"category": {"index": {"2024": 0}}},
                },
                "id": ["freq", "geo", "time"],
                "size": [1, 1, 1],
                "updated": "2026-02-03",
            }
        )
        latest_response = MockAsyncResponse(
            {
                "value": {"0": 12.0},
                "dimension": {
                    "freq": {"category": {"index": {"Q": 0}, "label": {"Q": "Quarterly"}}},
                    "geo": {"category": {"index": {"FR": 0}, "label": {"FR": "France"}}},
                    "time": {"category": {"index": {"2024-Q4": 0}}},
                },
                "id": ["freq", "geo", "time"],
                "size": [1, 1, 1],
                "updated": "2026-02-03",
            }
        )

        class RecordingClient:
            def __init__(self):
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append((str(url), dict(params or {})))
                response = empty_response if len(self.calls) == 1 else latest_response
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        client = RecordingClient()
        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="TOUR_DEM_EXQ2", country="__ALL__"))

        self.assertEqual(len(series.data), 1)
        self.assertEqual(series.metadata.apiUrl and "lastTimePeriod=1" in series.metadata.apiUrl, True)
        _, first_params = client.calls[0]
        _, retry_params = client.calls[1]
        self.assertEqual(first_params.get("freq"), "A")
        self.assertIn("sinceTimePeriod", first_params)
        self.assertNotIn("freq", retry_params)
        self.assertNotIn("sinceTimePeriod", retry_params)
        self.assertEqual(retry_params.get("lastTimePeriod"), "1")

    def test_eurostat_all_available_404_retries_latest_without_inferred_freq(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        class NotFoundResponse(MockAsyncResponse):
            def __init__(self) -> None:
                super().__init__({}, status_code=404, request_url="https://example.com/eurostat")

            def raise_for_status(self) -> None:
                response = httpx.Response(
                    404,
                    request=httpx.Request("GET", "https://example.com/eurostat"),
                )
                raise httpx.HTTPStatusError("not found", request=response.request, response=response)

        latest_response = MockAsyncResponse(
            {
                "value": {"0": 32.0},
                "dimension": {
                    "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
                    "geo": {"category": {"index": {"EU27_2020": 0}, "label": {"EU27_2020": "European Union"}}},
                    "time": {"category": {"index": {"2019": 0}}},
                },
                "id": ["freq", "geo", "time"],
                "size": [1, 1, 1],
                "updated": "2026-02-03",
            }
        )

        class RecordingClient:
            def __init__(self):
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append((str(url), dict(params or {})))
                if len(self.calls) == 1:
                    return NotFoundResponse()
                latest_response.request = MockAsyncResponse([], request_url=str(url)).request
                return latest_response

        client = RecordingClient()
        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="LFSO_19FXWT05", country="__ALL__"))

        self.assertEqual(len(series.data), 1)
        self.assertEqual(series.metadata.seriesId, "lfso_19fxwt05")
        self.assertEqual(series.metadata.apiUrl and "lastTimePeriod=1" in series.metadata.apiUrl, True)
        self.assertEqual(len(client.calls), 2)
        _, first_params = client.calls[0]
        _, retry_params = client.calls[1]
        self.assertEqual(first_params.get("freq"), "A")
        self.assertIn("sinceTimePeriod", first_params)
        self.assertNotIn("freq", retry_params)
        self.assertNotIn("sinceTimePeriod", retry_params)
        self.assertEqual(retry_params.get("lastTimePeriod"), "1")

    def test_eurostat_all_available_404_then_500_is_dataset_supportability(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        class NotFoundResponse(MockAsyncResponse):
            def __init__(self) -> None:
                super().__init__(
                    {"error": [{"label": "ERR_NOT_FOUND_4: LFSO_19FXWT05 is not available for dissemination."}]},
                    status_code=404,
                    request_url="https://example.com/eurostat",
                )

            def raise_for_status(self) -> None:
                response = httpx.Response(
                    404,
                    json={"error": [{"label": "ERR_NOT_FOUND_4: LFSO_19FXWT05 is not available for dissemination."}]},
                    request=httpx.Request("GET", "https://example.com/eurostat"),
                )
                raise httpx.HTTPStatusError("not found", request=response.request, response=response)

        class ServerErrorResponse(MockAsyncResponse):
            def __init__(self) -> None:
                super().__init__(
                    {},
                    status_code=500,
                    request_url="https://example.com/eurostat",
                )

            def raise_for_status(self) -> None:
                response = httpx.Response(
                    500,
                    text="Request failed.",
                    request=httpx.Request("GET", "https://example.com/eurostat"),
                )
                raise httpx.HTTPStatusError("server error", request=response.request, response=response)

        class RecordingClient:
            def __init__(self):
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append((str(url), dict(params or {})))
                return NotFoundResponse() if len(self.calls) == 1 else ServerErrorResponse()

        client = RecordingClient()
        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            with self.assertRaises(DataNotAvailableError) as raised:
                run(provider.fetch_indicator(indicator="LFSO_19FXWT05", country="__ALL__"))

        message = str(raised.exception)
        self.assertIn("eurostat_dataset_not_disseminated", message)
        self.assertIn("dataset=lfso_19fxwt05", message)
        self.assertIn("country=ALL_AVAILABLE", message)
        self.assertEqual(len(client.calls), 2)
        _, retry_params = client.calls[1]
        self.assertEqual(retry_params.get("lastTimePeriod"), "1")

    def test_eurostat_country_default_empty_window_retries_latest_period(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        empty_response = MockAsyncResponse(
            {
                "value": {},
                "dimension": {
                    "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
                    "geo": {"category": {"index": {"ES": 0}, "label": {"ES": "Spain"}}},
                    "time": {"category": {"index": {}}},
                },
                "id": ["freq", "geo", "time"],
                "size": [1, 1, 0],
                "updated": "2026-02-03",
            }
        )
        latest_response = MockAsyncResponse(
            {
                "value": {"0": 225.0},
                "dimension": {
                    "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
                    "geo": {"category": {"index": {"ES": 0}, "label": {"ES": "Spain"}}},
                    "time": {"category": {"index": {"2016": 0}}},
                },
                "id": ["freq", "geo", "time"],
                "size": [1, 1, 1],
                "updated": "2026-02-03",
            }
        )

        class RecordingClient:
            def __init__(self):
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append((str(url), dict(params or {})))
                response = empty_response if len(self.calls) == 1 else latest_response
                response.request = MockAsyncResponse([], request_url=str(url)).request
                return response

        client = RecordingClient()
        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="LFSO_16YMGNEDNC", country="Spain"))

        self.assertEqual(len(series.data), 1)
        self.assertEqual(series.data[0].date, "2016-01-01")
        self.assertEqual(series.metadata.country, "ES")
        self.assertEqual(series.metadata.apiUrl and "lastTimePeriod=1" in series.metadata.apiUrl, True)
        self.assertEqual(len(client.calls), 2)
        _, first_params = client.calls[0]
        _, retry_params = client.calls[1]
        self.assertEqual(first_params.get("geo"), "ES")
        self.assertEqual(first_params.get("freq"), "A")
        self.assertIn("sinceTimePeriod", first_params)
        self.assertEqual(retry_params.get("geo"), "ES")
        self.assertEqual(retry_params.get("freq"), "A")
        self.assertNotIn("sinceTimePeriod", retry_params)
        self.assertEqual(retry_params.get("lastTimePeriod"), "1")

    def test_eurostat_dispatch_exact_title_without_country_uses_all_available(self) -> None:
        from backend.services.data_fetcher import _fetch_from_eurostat

        class RecordingEurostatProvider:
            def __init__(self):
                self.calls = []

            async def fetch_indicator(self, **kwargs):
                self.calls.append(dict(kwargs))
                return NormalizedData(
                    metadata=Metadata(
                        source="Eurostat",
                        indicator="Historical aggregate table",
                        country=kwargs.get("country"),
                        frequency="annual",
                        unit="percent",
                        seriesId=kwargs.get("indicator"),
                    ),
                    data=[DataPoint(date="1999-01-01", value=1.0)],
                )

        provider = RecordingEurostatProvider()
        svc = SimpleNamespace(eurostat_provider=provider)
        params = {
            "indicator": "hsw_hp_svcln",
            "__exact_indicator_title_match": True,
            "__original_query": "Historical aggregate table from Eurostat",
        }
        intent = ParsedIntent(
            apiProvider="EUROSTAT",
            indicators=["Historical aggregate table"],
            parameters=params,
            clarificationNeeded=False,
            originalQuery="Historical aggregate table from Eurostat",
        )
        plan = ExecutionPlan(
            provider="EUROSTAT",
            candidate_id="hsw_hp_svcln",
            fetch_strategy="single_series",
            params=params,
            provider_request={
                "dataset_code": "hsw_hp_svcln",
                "country_scope": [],
                "start_year": None,
                "end_year": None,
                "filters": {},
            },
        )

        series = run(_fetch_from_eurostat(svc, intent, params, plan))

        self.assertEqual(len(series), 1)
        self.assertEqual(provider.calls[0]["country"], "__ALL__")

    def test_eurostat_provider_request_drops_internal_single_underscore_flags(self) -> None:
        from backend.services.data_fetcher import materialize_execution_plan

        intent = ParsedIntent(
            apiProvider="EUROSTAT",
            indicators=["INN_CIS10_MRK"],
            parameters={
                "indicator": "INN_CIS10_MRK",
                "country": "FR",
                "_ranking_scope_expanded": True,
                "__semantic_provider_locked": True,
            },
            clarificationNeeded=False,
            originalQuery="INN_CIS10_MRK from Eurostat",
        )

        plan = materialize_execution_plan(
            None,
            provider="EUROSTAT",
            intent=intent,
            params=intent.parameters,
        )

        filters = plan.provider_request["filters"]
        self.assertNotIn("_ranking_scope_expanded", filters)
        self.assertNotIn("__semantic_provider_locked", filters)

    def test_eurostat_all_available_413_retries_latest_without_inferred_freq(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        class TooLargeResponse(MockAsyncResponse):
            def __init__(self) -> None:
                super().__init__({}, status_code=413, request_url="https://example.com/eurostat")

            def raise_for_status(self) -> None:
                response = httpx.Response(
                    413,
                    request=httpx.Request("GET", "https://example.com/eurostat"),
                )
                raise httpx.HTTPStatusError("too large", request=response.request, response=response)

        latest_response = MockAsyncResponse(
            {
                "value": {"0": 1.0},
                "dimension": {
                    "freq": {"category": {"index": {"M": 0}, "label": {"M": "Monthly"}}},
                    "geo": {"category": {"index": {"FR": 0}, "label": {"FR": "France"}}},
                    "time": {"category": {"index": {"2024-12": 0}}},
                },
                "id": ["freq", "geo", "time"],
                "size": [1, 1, 1],
                "updated": "2026-02-03",
            }
        )

        class RecordingClient:
            def __init__(self):
                self.calls = []

            async def get(self, url, *, params=None, **_kwargs):
                self.calls.append((str(url), dict(params or {})))
                if len(self.calls) == 1:
                    return TooLargeResponse()
                latest_response.request = MockAsyncResponse([], request_url=str(url)).request
                return latest_response

        client = RecordingClient()
        with patch("backend.providers.eurostat.get_http_client", return_value=client):
            series = run(provider.fetch_indicator(indicator="BD_SIZE", country="__ALL__"))

        self.assertEqual(len(series.data), 1)
        self.assertEqual(len(client.calls), 2)
        _, retry_params = client.calls[1]
        self.assertNotIn("freq", retry_params)
        self.assertNotIn("sinceTimePeriod", retry_params)
        self.assertEqual(retry_params.get("lastTimePeriod"), "1")

    def test_eurostat_response_too_large_is_fail_closed_supportability(self) -> None:
        provider = EurostatProvider(metadata_search_service=None)

        class TooLargeResponse(MockAsyncResponse):
            def __init__(self) -> None:
                super().__init__({}, status_code=413, request_url="https://example.com/eurostat")

            def raise_for_status(self) -> None:
                response = httpx.Response(
                    413,
                    request=httpx.Request("GET", "https://example.com/eurostat"),
                )
                raise httpx.HTTPStatusError("too large", request=response.request, response=response)

        class RecordingClient:
            async def get(self, url, *, params=None, **_kwargs):
                return TooLargeResponse()

        with patch("backend.providers.eurostat.get_http_client", return_value=RecordingClient()):
            with self.assertRaises(DataNotAvailableError) as raised:
                run(provider.fetch_indicator(indicator="EF_RD_LEG", country="__ALL__", start_year=2021))

        message = str(raised.exception)
        self.assertIn("eurostat_response_too_large", message)
        self.assertIn("dataset=ef_rd_leg", message)
        self.assertIn("country=ALL_AVAILABLE", message)

    def test_oecd_resolve_indicator_uses_metadata_for_short_code(self) -> None:
        class StubMetadata:
            def __init__(self):
                self.search_terms = []

            async def search_with_sdmx_fallback(self, provider: str, indicator: str):
                self.search_terms.append(indicator)
                if indicator.upper() != "IRLT":
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
        self.assertEqual(provider._build_indicator_lookup_terms("IRLT"), ["IRLT"])  # pylint: disable=protected-access
        self.assertEqual(metadata_stub.search_terms, ["IRLT"])

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

    def test_oecd_fetch_indicator_uses_dimension_key_then_filters_country(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        class _KeyBuilder:
            async def build_key(self, **kwargs):
                self.kwargs = kwargs
                return "USA........"

        key_builder = _KeyBuilder()
        captured = {}

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                captured["url"] = url
                captured["params"] = params
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [{"observations": {"0:0": [1.0], "1:0": [2.0]}}],
                            "structures": [
                                {
                                    "dimensions": {
                                        "observation": [
                                            {
                                                "id": "REF_AREA",
                                                "values": [
                                                    {"id": "USA", "name": "United States"},
                                                    {"id": "DEU", "name": "Germany"},
                                                ],
                                            },
                                            {"id": "TIME_PERIOD", "values": [{"id": "2023"}]},
                                        ]
                                    }
                                }
                            ],
                        },
                        "meta": {"prepared": "2024-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.SDD.TPS", "DSD_TEST@DF_TEST", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=None),
        ), patch("backend.providers.oecd.get_dimension_key_builder", return_value=key_builder), \
             patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TEST", country="USA", start_year=2023, end_year=2023))

        self.assertIn("/data/OECD.SDD.TPS,DSD_TEST@DF_TEST,1.0/USA........", captured["url"])
        self.assertEqual(captured["params"], {
            "dimensionAtObservation": "AllDimensions",
            "startPeriod": "2023",
            "endPeriod": "2023",
        })
        self.assertEqual(series.metadata.country, "United States Of America")
        self.assertEqual(len(series.data), 1)
        self.assertEqual(series.data[0].value, 1.0)

    def test_oecd_fetch_indicator_uses_structure_native_ref_area_code(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        captured = {}
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "FREQ", "position": 0},
                {"id": "ADJUSTMENT", "position": 1},
                {"id": "REF_AREA", "position": 2},
            ],
            "dimension_ids": ["FREQ", "ADJUSTMENT", "REF_AREA"],
            "valid_values_by_dimension": {"REF_AREA": ["DE", "CA"]},
            "default_values": {"FREQ": "A"},
            "time_ranges": [],
        }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                captured["url"] = url
                captured["params"] = params
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [{"observations": {"0:0": [7.0]}}],
                            "structures": [
                                {
                                    "name": "IDC table",
                                    "dimensions": {
                                        "observation": [
                                            {"id": "REF_AREA", "values": [{"id": "DE", "name": "Germany"}]},
                                            {"id": "TIME_PERIOD", "values": [{"id": "2023"}]},
                                        ]
                                    },
                                }
                            ],
                        },
                        "meta": {"prepared": "2024-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.SDD.NAD", "DSD_NASEC10_IDC@DF_TABLE9B_IDC", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TEST", country="Germany", start_year=2023, end_year=2023))

        self.assertIn("/data/OECD.SDD.NAD,DSD_NASEC10_IDC@DF_TABLE9B_IDC,1.0/A..DE", captured["url"])
        self.assertEqual(series.metadata.country, "Germany")
        self.assertEqual(series.data[0].value, 7.0)

    def test_oecd_fetch_indicator_uses_provider_native_default_ref_area_when_unspecified(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        captured = {}
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "FREQ", "position": 0},
                {"id": "REF_AREA", "position": 1},
                {"id": "UNIT_MEASURE", "position": 2},
            ],
            "dimension_ids": ["FREQ", "REF_AREA", "UNIT_MEASURE"],
            "valid_values_by_dimension": {"FREQ": ["A"], "REF_AREA": ["CAN"]},
            "default_values": {"FREQ": "A", "REF_AREA": "CAN", "UNIT_MEASURE": "PS"},
            "time_ranges": [],
        }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                captured["url"] = url
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [{"observations": {"0:0:0:0": [37_448_282.0]}}],
                            "structures": [
                                {
                                    "name": "Population in the National Accounts",
                                    "dimensions": {
                                        "observation": [
                                            {"id": "FREQ", "values": [{"id": "A", "name": "Annual"}]},
                                            {"id": "REF_AREA", "values": [{"id": "CAN", "name": "Canada"}]},
                                            {"id": "UNIT_MEASURE", "values": [{"id": "PS", "name": "Persons"}]},
                                            {"id": "TIME_PERIOD", "values": [{"id": "2019"}]},
                                        ]
                                    },
                                }
                            ],
                        },
                        "meta": {"prepared": "2024-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.SDD.NAD", "DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TEST", country=None, start_year=2019, end_year=2019))

        self.assertIn(
            "/data/OECD.SDD.NAD,DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE,1.0/A.CAN.PS",
            captured["url"],
        )
        self.assertEqual(series.metadata.country, "Canada")
        self.assertEqual(series.data[0].value, 37_448_282.0)

    def test_oecd_fetch_indicator_does_not_request_invalid_monthly_frequency(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        captured = {}
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "FREQ", "position": 1},
            ],
            "dimension_ids": ["REF_AREA", "FREQ"],
            "valid_values_by_dimension": {"REF_AREA": ["USA"], "FREQ": ["A"]},
            "default_values": {},
            "time_ranges": [],
        }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                captured["url"] = url
                captured["params"] = params
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [{"observations": {"0:0:0": [4.2]}}],
                            "structures": [
                                {
                                    "name": "Annual unemployment by field",
                                    "dimensions": {
                                        "observation": [
                                            {"id": "REF_AREA", "values": [{"id": "USA", "name": "United States"}]},
                                            {"id": "FREQ", "values": [{"id": "A", "name": "Annual"}]},
                                            {"id": "TIME_PERIOD", "values": [{"id": "2024"}]},
                                        ]
                                    },
                                }
                            ],
                        },
                        "meta": {"prepared": "2024-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.EDU.IMEP", "DSD_EAG_LSO_EA@DF_LSO_NEAC_UNEMP_FIELD", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TEST", country="USA", start_year=2024, end_year=2024))

        self.assertIn(
            "/data/OECD.EDU.IMEP,DSD_EAG_LSO_EA@DF_LSO_NEAC_UNEMP_FIELD,1.0/USA.A",
            captured["url"],
        )
        self.assertNotIn("USA.M", captured["url"])
        self.assertEqual(series.metadata.frequency, "annual")
        self.assertEqual(series.data[0].value, 4.2)

    def test_oecd_fetch_indicator_retries_provider_default_span_for_sparse_latest_year(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        calls: list[dict[str, object]] = []
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "SEX", "position": 1},
                {"id": "AGE", "position": 2},
                {"id": "ATTAINMENT_LEV", "position": 3},
                {"id": "EDUCATION_FIELD", "position": 4},
                {"id": "STATISTICAL_OPERATION", "position": 5},
                {"id": "FREQ", "position": 6},
            ],
            "dimension_ids": [
                "REF_AREA",
                "SEX",
                "AGE",
                "ATTAINMENT_LEV",
                "EDUCATION_FIELD",
                "STATISTICAL_OPERATION",
                "FREQ",
            ],
            "valid_values_by_dimension": {"REF_AREA": ["USA"], "FREQ": ["A3"]},
            "default_values": {
                "SEX": "_T",
                "AGE": "Y25T64",
                "ATTAINMENT_LEV": "ISCED11A_5T8",
                "EDUCATION_FIELD": "F01+F02",
                "STATISTICAL_OPERATION": "OBS",
                "FREQ": "A3",
                "TIME_PERIOD_START": "2017",
                "TIME_PERIOD_END": "2024",
            },
            "time_ranges": [
                {
                    "dimension": "TIME_PERIOD",
                    "timeRange": {
                        "startPeriod": {"period": "2015-01-01T00:00:00"},
                        "endPeriod": {"period": "2024-12-31T00:00:00"},
                    },
                }
            ],
        }

        def _payload(observations: dict[str, list[float]]) -> dict:
            return {
                "data": {
                    "dataSets": [{"observations": observations}],
                    "structures": [
                        {
                            "name": "Unemployment rates of tertiary-educated adults",
                            "dimensions": {
                                "observation": [
                                    {"id": "REF_AREA", "values": [{"id": "USA", "name": "United States"}]},
                                    {"id": "SEX", "values": [{"id": "_T"}]},
                                    {"id": "AGE", "values": [{"id": "Y25T64"}]},
                                    {"id": "ATTAINMENT_LEV", "values": [{"id": "ISCED11A_5T8"}]},
                                    {"id": "EDUCATION_FIELD", "values": [{"id": "F02"}]},
                                    {"id": "STATISTICAL_OPERATION", "values": [{"id": "OBS"}]},
                                    {"id": "FREQ", "values": [{"id": "A3"}]},
                                    {"id": "TIME_PERIOD", "values": [{"id": "2017"}]},
                                ]
                            },
                        }
                    ],
                },
                "meta": {"prepared": "2026-01-01"},
            }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                calls.append({"url": url, "params": dict(params or {})})
                if len(calls) == 1:
                    return MockAsyncResponse(_payload({}))
                return MockAsyncResponse(_payload({"0:0:0:0:0:0:0:0": [4.2]}))

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.EDU.IMEP", "DSD_EAG_LSO_EA@DF_LSO_NEAC_UNEMP_FIELD", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TEST", country="USA"))

        self.assertEqual(len(calls), 2)
        self.assertIn(
            "/data/OECD.EDU.IMEP,DSD_EAG_LSO_EA@DF_LSO_NEAC_UNEMP_FIELD,1.0/USA._T.Y25T64.ISCED11A_5T8.F01+F02.OBS.A3",
            str(calls[0]["url"]),
        )
        self.assertEqual(calls[0]["params"]["startPeriod"], "2024")
        self.assertEqual(calls[0]["params"]["endPeriod"], "2024")
        self.assertEqual(calls[1]["params"]["startPeriod"], "2017")
        self.assertEqual(calls[1]["params"]["endPeriod"], "2024")
        self.assertEqual(series.metadata.country, "United States Of America")
        self.assertEqual(series.data[0].date, "2017-01-01")
        self.assertEqual(series.data[0].value, 4.2)

    def test_oecd_provider_advertised_span_failure_continues_to_relaxed_key(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        calls: list[dict[str, object]] = []
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "FREQ", "position": 1},
                {"id": "UNIT_MEASURE", "position": 2},
                {"id": "SPENDING_TYPE", "position": 3},
                {"id": "EXPENDITURE_TYPE", "position": 4},
                {"id": "AGE", "position": 5},
                {"id": "SEX", "position": 6},
            ],
            "dimension_ids": [
                "REF_AREA",
                "FREQ",
                "UNIT_MEASURE",
                "SPENDING_TYPE",
                "EXPENDITURE_TYPE",
                "AGE",
                "SEX",
            ],
            "valid_values_by_dimension": {"REF_AREA": ["USA"], "FREQ": ["A"]},
            "default_values": {
                "FREQ": "A",
                "UNIT_MEASURE": "PT_B1GQ",
                "SPENDING_TYPE": "ES50",
                "EXPENDITURE_TYPE": "_T",
                "AGE": "_T",
                "TIME_PERIOD_START": "2010",
                "TIME_PERIOD_END": "2023",
            },
            "time_ranges": [
                {
                    "dimension": "TIME_PERIOD",
                    "timeRange": {
                        "startPeriod": {"period": "2010-01-01T00:00:00"},
                        "endPeriod": {"period": "2023-12-31T00:00:00"},
                    },
                }
            ],
        }

        class _NoResultsResponse(MockAsyncResponse):
            text = "NoResultsFound"

            def __init__(self) -> None:
                super().__init__({}, status_code=404, request_url="https://example.com/oecd")

            def raise_for_status(self) -> None:
                raise httpx.HTTPStatusError(
                    "NoResultsFound",
                    request=httpx.Request("GET", "https://example.com/oecd"),
                    response=httpx.Response(404, text="NoResultsFound"),
                )

        def _payload(observations: dict[str, list[float]]) -> dict:
            return {
                "data": {
                    "dataSets": [{"observations": observations}],
                    "structures": [
                        {
                            "name": "Public expenditure on family",
                            "dimensions": {
                                "observation": [
                                    {"id": "REF_AREA", "values": [{"id": "USA", "name": "United States"}]},
                                    {"id": "FREQ", "values": [{"id": "A", "name": "Annual"}]},
                                    {"id": "UNIT_MEASURE", "values": [{"id": "PT_B1GQ"}]},
                                    {"id": "SPENDING_TYPE", "values": [{"id": "ES50"}]},
                                    {"id": "EXPENDITURE_TYPE", "values": [{"id": "_T"}]},
                                    {"id": "AGE", "values": [{"id": "_T"}]},
                                    {"id": "SEX", "values": [{"id": "_T"}]},
                                    {"id": "TIME_PERIOD", "values": [{"id": "2019"}]},
                                ]
                            },
                        }
                    ],
                },
                "meta": {"prepared": "2026-01-01"},
            }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                calls.append({"url": url, "params": dict(params or {})})
                if "PT_B1GQ" in str(url) or "ES50" in str(url):
                    return _NoResultsResponse()
                return MockAsyncResponse(_payload({"0:0:0:0:0:0:0:0": [2.4]}))

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.ELS.SPD", "DSD_SOCX_AGG@DF_PUB_FAM", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TEST", country="USA"))

        self.assertGreaterEqual(len(calls), 3)
        self.assertIn("PT_B1GQ", str(calls[0]["url"]))
        self.assertIn("ES50", str(calls[0]["url"]))
        self.assertEqual(calls[1]["params"]["startPeriod"], "2010")
        self.assertEqual(calls[1]["params"]["endPeriod"], "2023")
        self.assertNotIn("PT_B1GQ", str(calls[-1]["url"]))
        self.assertIn("USA.A", str(calls[-1]["url"]))
        self.assertEqual(series.metadata.country, "United States Of America")
        self.assertEqual(series.data[0].date, "2019-01-01")
        self.assertEqual(series.data[0].value, 2.4)

    def test_oecd_fetch_indicator_reports_all_missing_observation_values(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "SEX", "position": 1},
            ],
            "dimension_ids": ["REF_AREA", "SEX"],
            "valid_values_by_dimension": {"REF_AREA": ["USA"], "SEX": ["_Z"]},
            "default_values": {"SEX": "_Z"},
            "time_ranges": [],
        }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [
                                {
                                    "observations": {
                                        "0:0:0": [None, 0],
                                        "0:0:1": [None, 0],
                                    }
                                }
                            ],
                            "structures": [
                                {
                                    "name": "Population in the National Accounts",
                                    "dimensions": {
                                        "observation": [
                                            {"id": "REF_AREA", "values": [{"id": "USA", "name": "United States"}]},
                                            {"id": "SEX", "values": [{"id": "_Z", "name": "Not applicable"}]},
                                            {
                                                "id": "TIME_PERIOD",
                                                "values": [{"id": "2019"}, {"id": "2023"}],
                                            },
                                        ]
                                    },
                                    "attributes": {
                                        "observation": [
                                            {
                                                "id": "OBS_STATUS",
                                                "values": [
                                                    {
                                                        "id": "L",
                                                        "name": "Missing value; data exist but were not collected",
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                        "meta": {"prepared": "2026-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.SDD.NAD", "DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            with self.assertRaises(DataNotAvailableError) as raised:
                run(provider.fetch_indicator("TEST", country="USA", start_year=2019, end_year=2023))

        message = str(raised.exception)
        self.assertIn("oecd_missing_valued_observations", message)
        self.assertIn("DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE", message)
        self.assertIn("USA", message)
        self.assertIn("OBS_STATUS=L", message)
        self.assertIn("no collected numeric values", message)

    def test_oecd_fetch_indicator_preserves_requested_country_when_other_country_has_values(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "SEX", "position": 1},
            ],
            "dimension_ids": ["REF_AREA", "SEX"],
            "valid_values_by_dimension": {"REF_AREA": ["USA", "CAN"], "SEX": ["_Z"]},
            "default_values": {"SEX": "_Z"},
            "time_ranges": [],
        }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [
                                {
                                    "observations": {
                                        "0:0:0": [None, 0],
                                        "1:0:0": [37_448_282.0],
                                    }
                                }
                            ],
                            "structures": [
                                {
                                    "name": "Population in the National Accounts",
                                    "dimensions": {
                                        "observation": [
                                            {
                                                "id": "REF_AREA",
                                                "values": [
                                                    {"id": "USA", "name": "United States"},
                                                    {"id": "CAN", "name": "Canada"},
                                                ],
                                            },
                                            {"id": "SEX", "values": [{"id": "_Z", "name": "Not applicable"}]},
                                            {"id": "TIME_PERIOD", "values": [{"id": "2019"}]},
                                        ]
                                    },
                                    "attributes": {
                                        "observation": [
                                            {
                                                "id": "OBS_STATUS",
                                                "values": [{"id": "L", "name": "Missing value"}],
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                        "meta": {"prepared": "2026-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.SDD.NAD", "DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            with self.assertRaises(DataNotAvailableError) as raised:
                run(provider.fetch_indicator("TEST", country="USA", start_year=2019, end_year=2019))

        message = str(raised.exception)
        self.assertIn("oecd_missing_valued_observations", message)
        self.assertIn("USA", message)
        self.assertNotIn("Canada", message)

    def test_oecd_fetch_indicator_keeps_z_dimension_values_when_numeric(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        metadata = {
            "base_url": "https://sdmx.oecd.org/public/rest",
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "SEX", "position": 1},
            ],
            "dimension_ids": ["REF_AREA", "SEX"],
            "valid_values_by_dimension": {"REF_AREA": ["CAN"], "SEX": ["_Z"]},
            "default_values": {"SEX": "_Z"},
            "time_ranges": [],
        }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [{"observations": {"0:0:0": [37_448_282.0]}}],
                            "structures": [
                                {
                                    "name": "Population in the National Accounts",
                                    "dimensions": {
                                        "observation": [
                                            {"id": "REF_AREA", "values": [{"id": "CAN", "name": "Canada"}]},
                                            {"id": "SEX", "values": [{"id": "_Z", "name": "Not applicable"}]},
                                            {"id": "TIME_PERIOD", "values": [{"id": "2019"}]},
                                        ]
                                    },
                                }
                            ],
                        },
                        "meta": {"prepared": "2026-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.SDD.NAD", "DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE", "1.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TEST", country="CAN", start_year=2019, end_year=2019))

        self.assertEqual(series.metadata.country, "Canada")
        self.assertEqual(series.data[0].date, "2019-01-01")
        self.assertEqual(series.data[0].value, 37_448_282.0)

    def test_oecd_fetch_indicator_fails_fast_when_constraints_exclude_country(self) -> None:
        provider = OECDProvider(metadata_search_service=None)

        metadata = {
            "dimensions": [
                {"id": "REF_AREA", "position": 0},
                {"id": "MEASURE", "position": 1},
            ],
            "dimension_ids": ["REF_AREA", "MEASURE"],
            "valid_values_by_dimension": {"REF_AREA": ["BEFL", "BEFR", "US01"]},
            "time_ranges": [],
        }

        class _Client:
            async def get(self, *args, **kwargs):
                raise AssertionError("OECD data endpoint should not be called")

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.EDU.IMEP", "DSD_EAG_WT@DF_STA_TCH_REG", "2.0")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            with self.assertRaises(DataNotAvailableError) as ctx:
                run(provider.fetch_indicator("TEST", country="CAN", start_year=2023, end_year=2023))

        self.assertIn("does not advertise REF_AREA=CAN", str(ctx.exception))

    def test_oecd_fetch_indicator_uses_dedicated_space_from_structure_metadata(self) -> None:
        provider = OECDProvider(metadata_search_service=None)
        captured = {}
        structure_metadata = {
            "base_url": "https://sdmx.oecd.org/sti-public/rest",
            "dimensions": [
                {"id": "MEASURE", "position": 0},
                {"id": "REF_AREA", "position": 1},
                {"id": "ACTIVITY", "position": 2},
                {"id": "COUNTERPART_AREA", "position": 3},
                {"id": "UNIT_MEASURE", "position": 4},
                {"id": "FREQ", "position": 5},
            ],
            "dimension_ids": ["MEASURE", "REF_AREA", "ACTIVITY", "COUNTERPART_AREA", "UNIT_MEASURE", "FREQ"],
        }

        class _Client:
            async def get(self, url, *, params=None, headers=None, timeout=None):
                captured["url"] = url
                captured["params"] = params
                return MockAsyncResponse(
                    {
                        "data": {
                            "dataSets": [{"observations": {"0:0": [3.0]}}],
                            "structures": [
                                {
                                    "name": "TiVA shares",
                                    "dimensions": {
                                        "observation": [
                                            {
                                                "id": "REF_AREA",
                                                "values": [{"id": "JPN", "name": "Japan"}],
                                            },
                                            {"id": "TIME_PERIOD", "values": [{"id": "2023"}]},
                                        ]
                                    },
                                }
                            ],
                        },
                        "meta": {"prepared": "2024-01-01"},
                    }
                )

        with patch.object(
            provider,
            "_resolve_indicator",
            new=AsyncMock(return_value=("OECD.STI.PIE", "DSD_TIVA_MAINSH@DF_MAINSH", "1.1")),
        ), patch.object(
            provider,
            "_get_oecd_dataflow_structure",
            new=AsyncMock(return_value=structure_metadata),
        ), patch("backend.providers.oecd.get_http_client", return_value=_Client()), \
             patch("backend.providers.oecd.wait_for_provider", new=AsyncMock(return_value=0)), \
             patch("backend.providers.oecd.record_provider_request"), \
             patch("backend.providers.oecd.record_provider_success"), \
             patch("backend.providers.oecd.is_provider_circuit_open", return_value=False):
            series = run(provider.fetch_indicator("TiVA", country="Japan", start_year=2023, end_year=2023))

        self.assertIn(
            "https://sdmx.oecd.org/sti-public/rest/data/OECD.STI.PIE,DSD_TIVA_MAINSH@DF_MAINSH,1.1/.JPN....",
            captured["url"],
        )
        self.assertEqual(captured["params"]["startPeriod"], "2023")
        self.assertEqual(series.metadata.country, "Japan")
        self.assertEqual(series.data[0].value, 3.0)

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
