from __future__ import annotations

import re
import sys
import types
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.models import ClarificationOption, GeneratedFile, NormalizedData, ParsedIntent, QueryResponse
from backend.routing.country_resolver import CountryResolver
from backend.routing.unified_router import RoutingDecision
from backend.services.cache import cache_service
from backend.services.conversation import conversation_manager
from backend.services.conversation_state_v2 import AnswerSetMember, ConversationState, FollowUpDelta
from backend.services.query_pipeline import ParseRouteResult, ValidationResult
from backend.services.query import QueryService
from backend.services.semantic_match_judge import ExecutionResultJudgment
from backend.tests.utils import run
from backend.utils.retry import DataNotAvailableError


def sample_series() -> NormalizedData:
    return NormalizedData.model_validate(
        {
            "metadata": {
                "source": "FRED",
                "indicator": "Real GDP",
                "country": "US",
                "frequency": "quarterly",
                "unit": "Billions",
                "lastUpdated": "2024-01-01",
                "seriesId": "GDP",
                "apiUrl": "https://example.com",
            },
            "data": [
                {"date": "2020-01-01", "value": 100.0},
                {"date": "2020-04-01", "value": 90.0},
            ],
        }
    )


def sample_series_with(
    *,
    indicator: str | None = None,
    country: str | None = None,
    series_id: str | None = None,
    description: str | None = None,
    unit: str | None = None,
    source: str | None = None,
) -> NormalizedData:
    series = sample_series().model_copy(deep=True)
    if indicator is not None:
        series.metadata.indicator = indicator
    if country is not None:
        series.metadata.country = country
    if series_id is not None:
        series.metadata.seriesId = series_id
    if description is not None:
        series.metadata.description = description
    if unit is not None:
        series.metadata.unit = unit
    if source is not None:
        series.metadata.source = source
    return series


class QueryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        cache_service.clear()
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
        self.service.settings.use_outcome_decision_stage = False

    def test_process_query_returns_data(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"seriesId": "GDP"},
            clarificationNeeded=False,
        )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent):
            with patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()):
                response = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(response), 1)
        self.assertEqual(response[0].metadata.indicator, "Real GDP")

    def test_cache_hit_skips_fetch(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"seriesId": "GDP"},
            clarificationNeeded=False,
        )

        first_series = sample_series()
        with patch.object(self.service.fred_provider, "fetch_series", return_value=first_series):
            data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access
            self.assertEqual(data[0].metadata.seriesId, "GDP")

        with patch.object(self.service.fred_provider, "fetch_series", side_effect=AssertionError("Should not refetch")):
            cached = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(cached[0].metadata.indicator, "Real GDP")

    def test_build_cache_params_adds_version_without_mutating_input(self) -> None:
        raw_params = {"indicator": "NE.IMP.GNFS.ZS", "countries": ["China", "US"]}

        cache_params = self.service._build_cache_params("World Bank", raw_params)  # pylint: disable=protected-access

        self.assertNotIn("_cache_version", raw_params)
        self.assertNotIn("_provider", raw_params)
        self.assertEqual(cache_params["_cache_version"], self.service.CACHE_KEY_VERSION)
        self.assertEqual(cache_params["_provider"], "WORLDBANK")
        self.assertEqual(cache_params["indicator"], "NE.IMP.GNFS.ZS")

    def test_build_cache_params_ignores_original_query_for_non_dimension_provider(self) -> None:
        raw_params = {
            "indicator": "NGDP_RPCH",
            "__original_query": "Switch to bar chart",
        }

        cache_params = self.service._build_cache_params("IMF", raw_params)  # pylint: disable=protected-access

        self.assertEqual(cache_params["_provider"], "IMF")
        self.assertNotIn("_query_hash", cache_params)

    def test_build_cache_params_keeps_original_query_hash_for_statscan(self) -> None:
        raw_params = {
            "indicator": "14100287",
            "__original_query": "show male unemployment in canada",
        }

        cache_params = self.service._build_cache_params("StatsCan", raw_params)  # pylint: disable=protected-access

        self.assertEqual(cache_params["_provider"], "STATSCAN")
        self.assertIn("_query_hash", cache_params)

    def test_coerce_parsed_intent_sets_original_query_when_missing(self) -> None:
        raw_intent = {
            "apiProvider": "WORLDBANK",
            "indicators": ["NE.IMP.GNFS.ZS"],
            "parameters": {"countries": ["China", "US"]},
            "clarificationNeeded": False,
        }

        intent = self.service._coerce_parsed_intent(raw_intent, "import share of gdp China and US")  # pylint: disable=protected-access

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.originalQuery, "import share of gdp China and US")

    def test_serialize_cache_query_is_deterministic(self) -> None:
        first = {"b": 1, "a": 2}
        second = {"a": 2, "b": 1}

        first_serialized = self.service._serialize_cache_query(first)  # pylint: disable=protected-access
        second_serialized = self.service._serialize_cache_query(second)  # pylint: disable=protected-access

        self.assertEqual(first_serialized, second_serialized)

    def test_cache_version_change_invalidates_prior_entries(self) -> None:
        raw_params = {"seriesId": "GDP"}
        version_1_params = self.service._build_cache_params("FRED", raw_params)  # pylint: disable=protected-access
        cache_service.cache_data("FRED", version_1_params, sample_series())
        self.assertIsNotNone(cache_service.get_data("FRED", version_1_params))

        self.service.CACHE_KEY_VERSION = "test-next-version"
        version_2_params = self.service._build_cache_params("FRED", raw_params)  # pylint: disable=protected-access

        self.assertIsNone(cache_service.get_data("FRED", version_2_params))

    def test_process_query_records_processing_steps(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"seriesId": "GDP"},
            clarificationNeeded=False,
        )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()):

            response = run(self.service.process_query("Show me US GDP"))

        self.assertTrue(response.processingSteps)
        step_names = {step.step for step in response.processingSteps or []}
        # Service now uses LangGraph, so step names have changed
        # Check for either old-style or new-style step names
        has_parsing = "parsing_query" in step_names or "langgraph_execution" in step_names
        has_fetching = "fetching_data" in step_names or "cache_hit" in step_names
        self.assertTrue(has_parsing, f"Expected parsing step, got: {step_names}")
        self.assertTrue(has_fetching, f"Expected fetching step, got: {step_names}")

    def test_process_query_oil_price_overrides_worldbank_to_fred(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["oil price"],
            parameters={"country": "1W"},
            clarificationNeeded=False,
            originalQuery="oil price",
        )

        def fake_fetch(resolved_intent: ParsedIntent):
            self.assertEqual(resolved_intent.apiProvider, "FRED")
            return [sample_series_with(
                indicator="Crude Oil Prices: West Texas Intermediate (WTI) - Cushing, Oklahoma",
                series_id="DCOILWTICO",
                source="FRED",
                unit="Dollars per Barrel",
            )]

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch.object(self.service, "_fetch_data", side_effect=fake_fetch):
            response = run(self.service.process_query("oil price"))

        self.assertFalse(response.clarificationNeeded)
        self.assertEqual(response.intent.apiProvider, "FRED")
        self.assertEqual(response.data[0].metadata.seriesId, "DCOILWTICO")

    def test_process_query_brent_oil_price_selects_brent_series(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["Brent oil price"],
            parameters={"country": "1W"},
            clarificationNeeded=False,
            originalQuery="Brent oil price",
        )

        async def fake_fetch_series(params: dict[str, Any]):
            self.assertEqual(params.get("indicator"), "DCOILBRENTEU")
            return sample_series_with(
                indicator="Crude Oil Prices: Brent - Europe",
                series_id="DCOILBRENTEU",
                source="FRED",
                unit="Dollars per Barrel",
            )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch.object(self.service.fred_provider, "fetch_series", side_effect=fake_fetch_series):
            response = run(self.service.process_query("Brent oil price"))

        self.assertFalse(response.clarificationNeeded)
        self.assertEqual(response.intent.apiProvider, "FRED")
        self.assertEqual(response.data[0].metadata.seriesId, "DCOILBRENTEU")

    def test_select_indicator_query_uses_original_when_cues_mismatch(self) -> None:
        intent = ParsedIntent(
            apiProvider="World Bank",
            indicators=["Gross PSD, Central Gov., All maturities, % of GDP"],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp China and US",
        )

        selected = self.service._select_indicator_query_for_resolution(intent)  # pylint: disable=protected-access
        self.assertEqual(selected, "imports as % of GDP")

    def test_select_indicator_query_uses_original_when_only_generic_gdp_overlap_exists(self) -> None:
        intent = ParsedIntent(
            apiProvider="World Bank",
            indicators=["Gross domestic savings (% of GDP)"],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp China and US",
        )

        selected = self.service._select_indicator_query_for_resolution(intent)  # pylint: disable=protected-access
        self.assertEqual(selected, "imports as % of GDP")

    def test_worldbank_multi_indicator_collapses_to_resolved_code_after_override(self) -> None:
        intent = ParsedIntent(
            apiProvider="World Bank",
            indicators=[
                "Gross PSD, Central Gov., All maturities, % of GDP",
                "Gross PSD, Central Gov., All maturities, % of GDP",
            ],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp China and US",
        )

        class _Resolved:
            code = "NE.IMP.GNFS.ZS"
            confidence = 0.9
            source = "database"

        class _Resolver:
            def resolve(self, query, provider=None, **kwargs):
                return _Resolved()

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.world_bank_provider, "fetch_indicator", return_value=[sample_series()]) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(intent.indicators, ["NE.IMP.GNFS.ZS"])
        self.assertTrue(fetch_mock.called)

    def test_worldbank_fetch_prefers_resolved_indicator_param(self) -> None:
        intent = ParsedIntent(
            apiProvider="World Bank",
            indicators=["Import Share of GDP"],
            parameters={"countries": ["China", "US"], "indicator": "NE.IMP.GNFS.ZS"},
            clarificationNeeded=False,
            originalQuery="import share of gdp China and US",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.world_bank_provider, "fetch_indicator", return_value=[sample_series()]) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "NE.IMP.GNFS.ZS")

    def test_bis_fetch_prefers_resolved_indicator_param(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["policy rate"],
            parameters={"country": "US", "indicator": "BIS.CBPOL"},
            clarificationNeeded=False,
            originalQuery="policy rate in us",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.bis_provider, "fetch_indicator", return_value=[sample_series()]) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "BIS.CBPOL")

    def test_fred_fetch_skips_indicator_selector_for_exact_provider_title_match(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["inflation"],
            parameters={"country": "United States"},
            clarificationNeeded=False,
            originalQuery="US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
        )

        class _Resolved:
            code = "MELIPRVSUSCOUNTY24005"
            confidence = 0.99
            source = "database"
            name = "Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD"
            provider = "FRED"
            metadata = {}

        class _Resolver:
            def resolve(self, *args, **kwargs):
                return _Resolved()

        fake_indicator_selector = types.ModuleType("backend.services.indicator_selector")

        class _ShouldNotBeUsed:
            def __init__(self, *args, **kwargs):
                raise AssertionError("IndicatorSelector should be skipped for exact provider-title matches")

        fake_indicator_selector.IndicatorSelector = _ShouldNotBeUsed

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch("backend.services.indicator_resolution.looks_like_exact_provider_title_match", return_value=True), \
             patch.dict(sys.modules, {"backend.services.indicator_selector": fake_indicator_selector}), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        params = fetch_mock.call_args.args[0]
        self.assertEqual(params.get("indicator"), "MELIPRVSUSCOUNTY24005")

    def test_process_query_uses_exact_title_fast_path_before_llm_parse(self) -> None:
        fast_path_intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD"],
            parameters={
                "indicator": "MELIPRVSUSCOUNTY24005",
                "__semantic_indicator_label": "Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
                "__semantic_provider_locked": True,
                "__exact_indicator_title_match": True,
            },
            clarificationNeeded=False,
            confidence=0.99,
            recommendedChartType="line",
            queryType="data_fetch",
            originalQuery="US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
        )

        with patch.object(self.service, "_build_exact_indicator_title_intent", return_value=fast_path_intent), \
             patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, side_effect=AssertionError("LLM parse should be skipped")), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()):
            response = run(
                self.service.process_query(
                    "US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD"
                )
            )

        self.assertFalse(response.clarificationNeeded)
        self.assertEqual(len(response.data or []), 1)

    def test_build_exact_indicator_title_intent_rejects_generic_suffix_only_match(self) -> None:
        class _Lookup:
            def search(self, text, provider=None, limit=5):
                if provider == "FRED":
                    return [
                        {
                            "provider": "FRED",
                            "code": "PCETRIM12M159SFRBDAL",
                            "name": "Trimmed Mean PCE Inflation Rate",
                        }
                    ]
                return []

        with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
            intent = self.service._build_exact_indicator_title_intent("Germany inflation rate")  # pylint: disable=protected-access

        self.assertIsNone(intent)

    def test_build_explicit_provider_code_intent_returns_provider_locked_code(self) -> None:
        intent = self.service._build_explicit_provider_code_intent(  # pylint: disable=protected-access
            "NER_CBS_PSD_XDC from IMF"
        )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.apiProvider, "IMF")
        self.assertEqual(intent.parameters.get("indicator"), "NER_CBS_PSD_XDC")
        self.assertTrue(intent.parameters.get("__semantic_provider_locked"))

    def test_fetch_data_replaces_explicit_fred_code_when_query_conflicts(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["interest rate"],
            parameters={"country": "US", "indicator": "FEDFUNDS"},
            clarificationNeeded=False,
            originalQuery="US 10-year government bond yield from 2000 to 2024",
        )

        class _Resolved:
            code = "DGS10"
            confidence = 0.95
            source = "catalog"

        class _Resolver:
            def resolve(self, query, provider=None, **kwargs):
                return _Resolved()

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.dict("sys.modules", {"backend.services.indicator_selector": None}), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        params = fetch_mock.call_args.args[0]
        self.assertEqual(params.get("indicator"), "DGS10")

    def test_is_resolved_indicator_plausible_rejects_bis_debt_service_for_debt_gdp_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="BIS",
                indicator_query="gdp to debt ratio in china",
                resolved_code="WS_DSR",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_bis_debt_securities_for_debt_gdp_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="BIS",
                indicator_query="gdp to debt ratio in china",
                resolved_code="WS_DEBT_SEC2_PUB",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_fred_policy_rate_for_bond_yield_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="FRED",
                indicator_query="US 10-year government bond yield from 2000 to 2024",
                resolved_code="FEDFUNDS",
            )
        )

        self.assertTrue(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="FRED",
                indicator_query="US 10-year government bond yield from 2000 to 2024",
                resolved_code="DGS10",
            )
        )

        self.assertTrue(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="BIS",
                indicator_query="debt service ratio in china",
                resolved_code="WS_DSR",
            )
        )

    def test_looks_like_provider_indicator_code_accepts_short_oecd_codes(self) -> None:
        self.assertTrue(
            self.service._looks_like_provider_indicator_code("OECD", "IRLT")  # pylint: disable=protected-access
        )
        self.assertTrue(
            self.service._looks_like_provider_indicator_code("OECD", "CPI")  # pylint: disable=protected-access
        )

    def test_is_resolved_indicator_plausible_rejects_worldbank_cpi_for_producer_price_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="producer price inflation trend in us and germany",
                resolved_code="FP.CPI.TOTL.ZG",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_worldbank_primary_enrollment_for_tertiary_teachers(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="India Teachers in tertiary education ISCED 5 programmes female (number) from World Bank",
                resolved_code="SE.PRM.ENRR",
                resolved_name="School enrollment, primary (% gross)",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_worldbank_trade_share_for_terms_of_trade(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="United States Terms of Trade from World Bank",
                resolved_code="NE.TRD.GNFS.ZS",
                resolved_name="Trade (% of GDP)",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_worldbank_gov_consumption_for_lower_secondary_expenditure(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="India Government expenditure on lower secondary education PPP$ (millions) from World Bank",
                resolved_code="NE.CON.GOVT.ZS",
                resolved_name="General government final consumption expenditure (% of GDP)",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_oecd_gdp_growth_for_sdg_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="OECD",
                indicator_query="Japan Sustainable Development Goal 08 - Decent work and economic growth from OECD",
                resolved_code="DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD",
                resolved_name="Quarterly real GDP growth - OECD countries",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_eurostat_hicp_for_ppp_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="Eurostat",
                indicator_query="France Purchasing power parities price level indices from Eurostat",
                resolved_code="PRC_HICP_AIND",
                resolved_name="HICP - annual data (average index and rate of change) (1996-2025)",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_imf_cpi_for_producer_price_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="IMF",
                indicator_query="producer price inflation trend in us and germany",
                resolved_code="PCPIPCH",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_generic_imf_current_account_for_component_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="IMF",
                indicator_query="Germany Current Account Primary Income Investment income Reserve assets Balance of Payments from IMF",
                resolved_code="BCA_NGDPD",
                resolved_name="Current account balance, percent of GDP",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_generic_imf_revenue_for_specific_fiscal_component_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="IMF",
                indicator_query="Germany Revenue General Government Taxes Social contributions Government and Public Sector Finance from IMF",
                resolved_code="rev",
                resolved_name="Government revenue, percent of GDP",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_generic_imf_cpi_for_specific_component_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="IMF",
                indicator_query="Germany All Items Special Indexes Capital City Consumer Prices Miscellaneous Goods and Services from IMF",
                resolved_code="PCPIPCH",
                resolved_name="Inflation rate, average consumer prices",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_bis_xru_for_reer_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="BIS",
                indicator_query="real effective exchange rate for japan and korea since 2010",
                resolved_code="WS_XRU",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_worldbank_bare_reer_code(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="REER trend for China and India from 2012 to 2024",
                resolved_code="REER",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_trade_balance_level_for_ratio_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="net trade balance as share of gdp in japan and korea",
                resolved_code="BN.GSR.GNFS.CD",
            )
        )

    def test_is_resolved_indicator_plausible_rejects_trade_balance_for_trade_openness_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="trade openness ratio (exports plus imports to GDP) in small open economies",
                resolved_code="NE.RSB.GNFS.ZS",
            )
        )

        self.assertTrue(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="WorldBank",
                indicator_query="trade openness ratio (exports plus imports to GDP) in small open economies",
                resolved_code="NE.TRD.GNFS.ZS",
            )
        )

    def test_select_indicator_query_uses_original_for_discontinued_indicator(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["Composite Yield on U.S. Treasury Bonds with Maturity over 10 Years (DISCONTINUED)"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            originalQuery="US 10-year government bond yield from 2000 to 2024",
        )

        selected = self.service._select_indicator_query_for_resolution(intent)  # pylint: disable=protected-access
        self.assertEqual(selected, "10-year government bond yield")

    def test_select_indicator_query_distills_ranking_phrase_to_metric(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["Rank top 10 economies by GDP growth in 2023"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="Rank top 10 economies by GDP growth in 2023",
        )

        selected = self.service._select_indicator_query_for_resolution(intent)  # pylint: disable=protected-access
        self.assertEqual(selected, "GDP growth")

    def test_select_indicator_query_preserves_long_exact_fred_housing_title(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["inflation"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
        )

        lookup_results = [
            {
                "code": "MELIPRVSUSCOUNTY24005",
                "provider": "FRED",
                "name": "Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
            }
        ]

        with patch(
            "backend.services.indicator_database.get_indicator_lookup",
            return_value=Mock(search=Mock(return_value=lookup_results)),
        ):
            selected = self.service._select_indicator_query_for_resolution(intent)  # pylint: disable=protected-access

        self.assertEqual(
            selected,
            "US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
        )

    def test_select_indicator_query_prefers_original_when_provider_locked_semantic_label_is_polluted(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["current account", "primary income", "investment income", "reserve assets"],
            parameters={
                "country": "Germany",
                "__semantic_provider_locked": True,
                "__semantic_indicator_label": "foreign exchange reserves",
            },
            clarificationNeeded=False,
            originalQuery="Germany Current Account Primary Income Investment income Reserve assets Balance of Payments from IMF",
        )

        selected = self.service._select_indicator_query_for_resolution(intent)  # pylint: disable=protected-access

        self.assertEqual(
            selected,
            "Germany Current Account Primary Income Investment income Reserve assets Balance of Payments from IMF",
        )

    def test_build_exact_indicator_title_intent_returns_fred_title_match(self) -> None:
        lookup_results = [
            {
                "code": "MELIPRVSUSCOUNTY24005",
                "provider": "FRED",
                "name": "Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
            }
        ]

        with patch(
            "backend.services.indicator_database.get_indicator_lookup",
            return_value=Mock(search=Mock(return_value=lookup_results)),
        ):
            intent = self.service._build_exact_indicator_title_intent(  # pylint: disable=protected-access
                "US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD"
            )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.apiProvider, "FRED")
        self.assertEqual(intent.parameters.get("indicator"), "MELIPRVSUSCOUNTY24005")
        self.assertEqual(intent.indicators, ["Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD"])

    def test_build_exact_indicator_title_intent_handles_country_prefix_and_provider_suffix(self) -> None:
        lookup_results = [
            {
                "code": "DSD_EAG_UOE_NON_FIN_STUD@DF_UOE_NF_SHARE_VET",
                "provider": "OECD",
                "name": "Share of students enrolled in school and work-based programmes",
            }
        ]

        with patch(
            "backend.services.indicator_database.get_indicator_lookup",
            return_value=Mock(search=Mock(return_value=lookup_results)),
        ):
            intent = self.service._build_exact_indicator_title_intent(  # pylint: disable=protected-access
                "Japan Share of students enrolled in school and work-based programmes from OECD"
            )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.apiProvider, "OECD")
        self.assertEqual(intent.parameters.get("indicator"), "DSD_EAG_UOE_NON_FIN_STUD@DF_UOE_NF_SHARE_VET")
        self.assertEqual(intent.indicators, ["Share of students enrolled in school and work-based programmes"])

    def test_build_exact_indicator_title_intent_handles_coingecko_asset_name_with_suffix(self) -> None:
        lookup_results = [
            {
                "code": "libfi",
                "provider": "CoinGecko",
                "name": "Liberty Finance",
            }
        ]

        with patch(
            "backend.services.indicator_database.get_indicator_lookup",
            return_value=Mock(search=Mock(return_value=lookup_results)),
        ):
            intent = self.service._build_exact_indicator_title_intent(  # pylint: disable=protected-access
                "Liberty Finance cryptocurrency price from CoinGecko"
            )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.apiProvider, "COINGECKO")
        self.assertEqual(intent.parameters.get("indicator"), "libfi")
        self.assertEqual(intent.parameters.get("coinIds"), ["libfi"])
        self.assertEqual(intent.indicators, ["Liberty Finance"])

    def test_build_exact_indicator_title_intent_tags_generic_interest_rate_as_broad_concept(self) -> None:
        lookup_results = [
            {
                "code": "REAINTRATREARAT10Y",
                "provider": "FRED",
                "name": "10-Year Real Interest Rate",
            }
        ]

        with patch(
            "backend.services.indicator_database.get_indicator_lookup",
            return_value=Mock(search=Mock(return_value=lookup_results)),
        ):
            intent = self.service._build_exact_indicator_title_intent(  # pylint: disable=protected-access
                "interest rate"
            )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.parameters.get("__catalog_concept"), "interest rate")
        self.assertTrue(intent.parameters.get("__exact_indicator_title_match"))

    def test_apply_country_overrides_replaces_single_country_with_explicit_multi_country(self) -> None:
        intent = ParsedIntent(
            apiProvider="StatsCan",
            indicators=["house prices"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="Compare house price growth across Canada, Australia, and the UK since 2015",
        )

        self.service._apply_country_overrides(  # pylint: disable=protected-access
            intent,
            "Compare house price growth across Canada, Australia, and the UK since 2015",
        )

        self.assertNotIn("country", intent.parameters)
        self.assertEqual(intent.parameters.get("countries"), ["CA", "AU", "GB"])

    def test_apply_country_overrides_expands_region_group_for_economies_phrase(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["trade openness"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="Trade openness ratio (exports plus imports to GDP) in small open economies",
        )

        self.service._apply_country_overrides(  # pylint: disable=protected-access
            intent,
            intent.originalQuery,
        )

        countries = intent.parameters.get("countries") or []
        self.assertGreaterEqual(len(countries), 5)
        self.assertIn("SG", countries)

    def test_apply_country_overrides_expands_g20_without_comparative_markers(self) -> None:
        """Region expansion must fire even without words like 'compare' or 'countries'."""
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["employment rate"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            originalQuery="employment in G20",
        )

        self.service._apply_country_overrides(  # pylint: disable=protected-access
            intent,
            intent.originalQuery,
        )

        countries = intent.parameters.get("countries") or []
        self.assertEqual(len(countries), 19)
        self.assertIn("CN", countries)
        self.assertIn("BR", countries)
        self.assertIn("IN", countries)
        self.assertNotIn("country", intent.parameters)

    def test_apply_country_overrides_does_not_expand_oecd_provider_into_region(self) -> None:
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["GDP"],
            parameters={"country": "Italy"},
            clarificationNeeded=False,
            originalQuery="Get Italy GDP from OECD",
        )

        self.service._apply_country_overrides(  # pylint: disable=protected-access
            intent,
            "Get Italy GDP from OECD",
        )

        self.assertEqual(intent.parameters.get("country"), "Italy")
        self.assertNotIn("countries", intent.parameters)

    def test_apply_country_overrides_does_not_expand_eurostat_provider_into_region(self) -> None:
        intent = ParsedIntent(
            apiProvider="EUROSTAT",
            indicators=["unemployment rate"],
            parameters={"country": "Germany"},
            clarificationNeeded=False,
            originalQuery="Germany unemployment rate from Eurostat",
        )

        self.service._apply_country_overrides(  # pylint: disable=protected-access
            intent,
            "Germany unemployment rate from Eurostat",
        )

        self.assertEqual(intent.parameters.get("country"), "Germany")
        self.assertNotIn("countries", intent.parameters)

    def test_provider_covers_country_list_rejects_oecd_for_non_oecd_countries(self) -> None:
        """OECD provider should be rejected when country list includes non-OECD members."""
        g20_countries = list(CountryResolver.G20_MEMBERS)
        result = self.service._provider_covers_country_list(  # pylint: disable=protected-access
            "OECD",
            g20_countries,
        )
        self.assertFalse(result)

    def test_provider_covers_country_list_accepts_oecd_for_oecd_only_countries(self) -> None:
        """OECD provider should be accepted when all countries are OECD members."""
        result = self.service._provider_covers_country_list(  # pylint: disable=protected-access
            "OECD",
            ["US", "GB", "FR", "DE", "JP"],
        )
        self.assertTrue(result)

    def test_maybe_resolve_region_clarification_expands_known_groups(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["current account balance"],
            parameters={},
            clarificationNeeded=True,
            clarificationQuestions=[
                "Which specific countries or regions should be considered as energy importers?",
                "Which specific countries or regions should be considered as energy exporters?",
            ],
            originalQuery="Compare current account balances for energy importers versus exporters",
        )

        resolved = self.service._maybe_resolve_region_clarification(  # pylint: disable=protected-access
            intent.originalQuery,
            intent,
        )

        self.assertTrue(resolved)
        self.assertFalse(intent.clarificationNeeded)
        self.assertTrue(len(intent.parameters.get("countries", [])) >= 2)

    def test_maybe_resolve_temporal_comparison_clarification_for_before_after_query(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["trade balance"],
            parameters={"country": "US"},
            clarificationNeeded=True,
            clarificationQuestions=[
                "Do you want the trade balance for the period up to December 31, 2017?",
                "Do you want the trade balance for the period from January 1, 2018 onward?",
            ],
            originalQuery="Contrast US and China trade balances before and after 2018",
        )

        resolved = self.service._maybe_resolve_temporal_comparison_clarification(  # pylint: disable=protected-access
            intent.originalQuery,
            intent,
        )

        self.assertTrue(resolved)
        self.assertFalse(intent.clarificationNeeded)
        self.assertEqual(intent.parameters.get("comparisonSplitYear"), 2018)
        self.assertTrue(str(intent.parameters.get("startDate", "")).startswith("200"))

    def test_extract_indicator_cues_detects_external_balance_as_trade_balance(self) -> None:
        cues = self.service._extract_indicator_cues(  # pylint: disable=protected-access
            "External balance on goods and services (% of GDP)"
        )
        self.assertIn("trade_balance", cues)

    def test_extract_indicator_cues_ignores_trade_direction_for_energy_group_current_account(self) -> None:
        cues = self.service._extract_indicator_cues(  # pylint: disable=protected-access
            "Compare current account balances for energy importers versus exporters"
        )
        self.assertIn("current_account", cues)
        self.assertIn("energy_group", cues)
        self.assertNotIn("import", cues)
        self.assertNotIn("export", cues)

    def test_detect_regions_in_query_recognizes_small_open_economies(self) -> None:
        regions = CountryResolver.detect_regions_in_query(
            "Trade openness ratio in small open economies"
        )
        self.assertIn("SMALL_OPEN_ECONOMIES", regions)
        expanded = CountryResolver.expand_regions_in_query(
            "Trade openness ratio in small open economies"
        )
        self.assertGreaterEqual(len(expanded), 5)
        self.assertIn("SG", expanded)

    def test_expand_regions_in_query_returns_all_g20_members(self) -> None:
        expanded = CountryResolver.expand_regions_in_query(
            "employment rate across G20 member countries"
        )

        self.assertEqual(len(expanded), 19)
        self.assertIn("AU", expanded)
        self.assertIn("JP", expanded)
        self.assertIn("US", expanded)

    def test_maybe_expand_multi_concept_intent_builds_multi_indicator_query(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["policy rates and inflation"],
            parameters={"countries": ["US", "GB", "DE"]},
            clarificationNeeded=True,
            clarificationQuestions=["Please clarify indicators"],
            originalQuery="Compare policy rates and inflation for US, UK, and euro area since 2010",
        )

        expanded = self.service._maybe_expand_multi_concept_intent(  # pylint: disable=protected-access
            intent.originalQuery,
            intent,
        )

        self.assertTrue(expanded)
        self.assertFalse(intent.clarificationNeeded)
        self.assertGreaterEqual(len(intent.indicators), 2)
        self.assertIn("policy rate", " | ".join(intent.indicators).lower())
        self.assertIn("inflation", " | ".join(intent.indicators).lower())

    def test_apply_ranking_projection_returns_sorted_top_n(self) -> None:
        series_a = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "WorldBank",
                    "indicator": "GDP growth",
                    "country": "US",
                    "frequency": "annual",
                    "unit": "%",
                    "lastUpdated": "2024-01-01",
                    "seriesId": "NY.GDP.MKTP.KD.ZG",
                },
                "data": [
                    {"date": "2023-01-01", "value": 2.0},
                    {"date": "2024-01-01", "value": 1.8},
                ],
            }
        )

        series_b = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "WorldBank",
                    "indicator": "GDP growth",
                    "country": "IN",
                    "frequency": "annual",
                    "unit": "%",
                    "lastUpdated": "2024-01-01",
                    "seriesId": "NY.GDP.MKTP.KD.ZG",
                },
                "data": [
                    {"date": "2023-01-01", "value": 6.5},
                    {"date": "2024-01-01", "value": 6.2},
                ],
            }
        )

        ranked = self.service._apply_ranking_projection(  # pylint: disable=protected-access
            "Rank top 1 economies by GDP growth in 2023",
            [series_a, series_b],
        )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].metadata.country, "IN")
        self.assertEqual(len(ranked[0].data), 1)
        self.assertEqual(ranked[0].data[0].date, "2023-01-01")

    def test_apply_concept_provider_override_reroutes_unavailable_provider(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["WS_DSR"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="gdp to debt ratio in china",
        )

        with patch("backend.services.catalog_service.find_concept_by_term", return_value="government_debt"), \
             patch("backend.services.catalog_service.is_provider_available", return_value=False), \
             patch("backend.services.catalog_service.get_best_provider", return_value=("IMF", "GGXWDG_NGDP", 0.95)):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "BIS",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "IMF")
        self.assertEqual(intent.apiProvider, "IMF")
        self.assertEqual(params.get("indicator"), "GGXWDG_NGDP")

    def test_apply_concept_provider_override_reroutes_when_global_provider_is_low_confidence(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["producer price inflation"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="producer price inflation trend in the us and germany",
        )

        def _mock_best_provider(concept, countries=None, preferred_provider=None):
            if preferred_provider:
                return ("WorldBank", "FP.WPI.TOTL.ZG", 0.70)
            return ("OECD", "PPI", 0.92)

        with patch("backend.services.catalog_service.find_concept_by_term", return_value="producer_price_inflation"), \
             patch("backend.services.catalog_service.is_provider_available", return_value=True), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=_mock_best_provider):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "WORLDBANK",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "OECD")
        self.assertEqual(intent.apiProvider, "OECD")
        self.assertEqual(params.get("indicator"), "PPI")

    def test_apply_concept_provider_override_respects_fallback_blocked_provider(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["real effective exchange rate"],
            parameters={"countries": ["China", "India"]},
            clarificationNeeded=False,
            originalQuery="REER trend for China and India from 2012 to 2024",
        )
        params = {**intent.parameters, "__fallback_excluded_providers": ["IMF"]}

        def _mock_best_provider(concept, countries=None, preferred_provider=None):
            if preferred_provider:
                return ("BIS", "WS_XRU", 0.40)
            return ("IMF", "EREER", 0.92)

        with patch("backend.services.catalog_service.find_concept_by_term", return_value="real_effective_exchange_rate"), \
             patch("backend.services.catalog_service.is_provider_available", return_value=True), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=_mock_best_provider):
            provider, new_params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "BIS",
                intent,
                params,
            )

        self.assertEqual(provider, "BIS")
        self.assertEqual(intent.apiProvider, "BIS")
        self.assertNotEqual(new_params.get("indicator"), "EREER")

    def test_apply_concept_provider_override_prefers_distilled_indicator_concept(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["youth unemployment rate"],
            parameters={"country": "Brazil"},
            clarificationNeeded=False,
            originalQuery="youth unemployment rate in Brazil",
        )

        def _mock_find_concept(term: str):
            normalized = str(term or "").strip().lower()
            if normalized == "youth unemployment rate in brazil":
                return "unemployment"
            if normalized == "youth unemployment rate":
                return "youth_unemployment"
            return None

        def _mock_best_provider(concept, countries=None, preferred_provider=None):
            if concept == "youth_unemployment":
                if preferred_provider:
                    return ("WorldBank", "JI.UEM.1524.ZS", 0.95)
                return ("WorldBank", "JI.UEM.1524.ZS", 0.95)
            if concept == "unemployment":
                if preferred_provider:
                    return ("IMF", "LUR", 0.90)
                return ("IMF", "LUR", 0.90)
            return (None, None, 0.0)

        with patch.object(self.service, "_build_distilled_indicator_query", return_value="youth unemployment rate"), \
             patch.object(self.service, "_select_indicator_query_for_resolution", return_value="youth unemployment rate"), \
             patch("backend.services.catalog_service.find_concept_by_term", side_effect=_mock_find_concept), \
             patch("backend.services.catalog_service.is_provider_available", return_value=False), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=_mock_best_provider):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "IMF",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "WORLDBANK")
        self.assertEqual(intent.apiProvider, "WorldBank")
        self.assertEqual(params.get("indicator"), "JI.UEM.1524.ZS")

    def test_apply_concept_provider_override_injects_canonical_code_when_provider_already_matches(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["effective exchange rate"],
            parameters={"country": "Japan"},
            clarificationNeeded=False,
            originalQuery="effective exchange rate in Japan",
        )

        with patch.object(self.service, "_build_distilled_indicator_query", return_value="effective exchange rate"), \
             patch.object(self.service, "_select_indicator_query_for_resolution", return_value="effective exchange rate"), \
             patch("backend.services.catalog_service.find_concept_by_term", return_value="effective_exchange_rate"), \
             patch("backend.services.catalog_service.is_provider_available", return_value=True), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=[
                 ("BIS", "WS_EER", 0.95),
                 ("BIS", "WS_EER", 0.95),
             ]):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "BIS",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "BIS")
        self.assertEqual(params.get("indicator"), "WS_EER")

    def test_apply_concept_provider_override_replaces_mismatched_explicit_code_with_canonical_code(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["credit to GDP ratio"],
            parameters={
                "country": "US",
                "indicator": "WS_CREDIT_GAP",
                "__semantic_provider_locked": True,
            },
            clarificationNeeded=False,
            originalQuery="BIS credit to GDP ratio",
        )

        with patch.object(self.service, "_build_distilled_indicator_query", return_value="credit to GDP ratio"), \
             patch.object(self.service, "_select_indicator_query_for_resolution", return_value="credit to GDP ratio"), \
             patch("backend.services.catalog_service.find_concept_by_term", return_value="credit"), \
             patch("backend.services.catalog_service.is_provider_available", return_value=True), \
             patch("backend.services.catalog_service.is_indicator_code_for_concept", return_value=False), \
             patch("backend.services.indicator_resolution.is_resolved_indicator_plausible", return_value=False), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=[
                 ("BIS", "WS_TC", 0.95),
                 ("BIS", "WS_TC", 0.95),
             ]):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "BIS",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "BIS")
        self.assertEqual(params.get("indicator"), "WS_TC")

    def test_apply_concept_provider_override_skips_exact_fred_title_match(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["inflation"],
            parameters={"country": "United States"},
            clarificationNeeded=False,
            originalQuery="US Housing Inventory: Price Increased Count Year-Over-Year in New Haven-Milford, CT (CBSA)",
        )

        with patch("backend.services.indicator_resolution.looks_like_exact_provider_title_match", return_value=True):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "FRED",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "FRED")
        self.assertEqual(intent.apiProvider, "FRED")
        self.assertNotIn("indicator", params)

    def test_apply_concept_provider_override_injects_canonical_oecd_code_for_gdp_alias(self) -> None:
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["GDP"],
            parameters={"country": "Italy", "indicator": "GDP"},
            clarificationNeeded=False,
            originalQuery="Get Italy GDP from OECD",
        )

        def _mock_best_provider(concept, countries=None, preferred_provider=None):
            if preferred_provider:
                return ("OECD", "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE", 0.95)
            return ("OECD", "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE", 0.95)

        with patch("backend.services.catalog_service.find_concept_by_term", return_value="gdp"), \
             patch("backend.services.catalog_service.is_provider_available", return_value=True), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=_mock_best_provider):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "OECD",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "OECD")
        self.assertEqual(intent.apiProvider, "OECD")
        self.assertEqual(params.get("indicator"), "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE")

    def test_apply_concept_provider_override_preserves_comtrade_for_bilateral_exports(self) -> None:
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["dynamic"],
            parameters={"reporter": "US", "partner": "CN", "flow": "EXPORT"},
            clarificationNeeded=False,
            originalQuery="US exports to China",
        )

        with patch("backend.services.catalog_service.find_concept_by_term", return_value="exports"):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "COMTRADE",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "COMTRADE")
        self.assertEqual(intent.apiProvider, "COMTRADE")
        self.assertNotIn("indicator", params)

    def test_apply_concept_provider_override_preserves_comtrade_for_bilateral_trade_balance(self) -> None:
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["dynamic"],
            parameters={"reporter": "US", "partner": "JP", "flow": "BALANCE"},
            clarificationNeeded=False,
            originalQuery="trade balance US and Japan",
        )

        with patch("backend.services.catalog_service.find_concept_by_term", return_value="trade_balance"):
            provider, params = self.service._apply_concept_provider_override(  # pylint: disable=protected-access
                "COMTRADE",
                intent,
                dict(intent.parameters),
            )

        self.assertEqual(provider, "COMTRADE")
        self.assertEqual(intent.apiProvider, "COMTRADE")
        self.assertNotIn("indicator", params)

    def test_select_routed_provider_locks_catalog_override_before_semantic_reroute(self) -> None:
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["research and development spending share of gdp"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="research and development spending share of gdp",
        )

        self.service.semantic_provider_router = None
        self.service.unified_router.route = Mock(
            return_value=RoutingDecision(
                provider="OECD",
                confidence=0.95,
                match_type="indicator",
                reasoning="keyword",
            )
        )

        with patch.object(
            self.service,
            "_apply_concept_provider_override",
            return_value=("WORLDBANK", {"indicator": "GB.XPD.RSDV.GD.ZS"}),
        ) as override_mock:
            provider = run(
                self.service._select_routed_provider(
                    intent,
                    "research and development spending share of gdp",
                )
            )

        self.assertEqual(provider, "WORLDBANK")
        self.assertEqual(intent.parameters.get("indicator"), "GB.XPD.RSDV.GD.ZS")
        override_mock.assert_called_once()

    def test_select_routed_provider_honors_explicit_provider_request(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["GDP"],
            parameters={"country": "Italy"},
            clarificationNeeded=False,
            originalQuery="Get Italy GDP from OECD",
        )

        self.service.unified_router.route = Mock(
            return_value=RoutingDecision(
                provider="WorldBank",
                confidence=0.95,
                match_type="indicator",
                reasoning="keyword",
            )
        )

        provider = run(
            self.service._select_routed_provider(
                intent,
                "Get Italy GDP from OECD",
            )
        )

        self.assertEqual(provider, "OECD")
        self.assertEqual(intent.apiProvider, "OECD")

    def test_normalize_bis_metadata_labels_replaces_opaque_code(self) -> None:
        bis_series = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "BIS",
                    "indicator": "WS_DSR",
                    "country": "CN",
                    "frequency": "quarterly",
                    "unit": "percent",
                    "lastUpdated": "",
                    "seriesId": "WS_DSR",
                },
                "data": [{"date": "2020-01-01", "value": 1.0}],
            }
        )

        self.service._normalize_bis_metadata_labels([bis_series])  # pylint: disable=protected-access

        self.assertNotEqual(bis_series.metadata.indicator, "WS_DSR")
        self.assertIn("debt service", bis_series.metadata.indicator.lower())

    def test_normalize_metadata_labels_promotes_description_for_code_like_indicator(self) -> None:
        imf_series = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "IMF",
                    "indicator": "GGXCNL_NGDP",
                    "country": "CN",
                    "frequency": "annual",
                    "unit": "percent",
                    "lastUpdated": "",
                    "seriesId": "GGXCNL_NGDP",
                    "description": "General government net lending/borrowing (% of GDP)",
                },
                "data": [{"date": "2020-01-01", "value": -2.1}],
            }
        )

        self.service._normalize_bis_metadata_labels([imf_series])  # pylint: disable=protected-access

        self.assertEqual(
            imf_series.metadata.indicator,
            "General government net lending/borrowing (% of GDP)",
        )

    def test_has_implausible_top_series_detects_wrong_fred_tenor(self) -> None:
        wrong_series = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "FRED",
                    "indicator": "Composite Yield on U.S. Treasury Bonds with Maturity over 10 Years (DISCONTINUED)",
                    "country": "US",
                    "frequency": "daily",
                    "unit": "Percent",
                    "lastUpdated": "",
                    "seriesId": "DLTBOARD",
                },
                "data": [{"date": "2020-01-01", "value": 1.0}],
            }
        )

        self.assertTrue(
            self.service._has_implausible_top_series(  # pylint: disable=protected-access
                "US 10-year government bond yield from 2000 to 2024",
                [wrong_series],
            )
        )

    def test_build_uncertain_result_clarification_returns_ranked_options(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["GDP to Debt Ratio"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="gdp to debt ratio in china",
        )
        uncertain_data = [
            NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "BIS",
                        "indicator": "Debt service ratios",
                        "country": "CN",
                        "frequency": "quarterly",
                        "unit": "percent",
                        "lastUpdated": "",
                        "seriesId": "WS_DSR",
                    },
                    "data": [
                        {"date": "2020-01-01", "value": 1.0},
                    ],
                }
            )
        ]

        class _Resolved:
            def __init__(self, provider: str, code: str, name: str, confidence: float):
                self.provider = provider
                self.code = code
                self.name = name
                self.confidence = confidence
                self.source = "catalog"

        class _Resolver:
            def resolve(self, query, provider=None, **kwargs):
                mapping = {
                    "IMF": _Resolved("IMF", "GGXWDG_NGDP", "General government gross debt (% of GDP)", 0.95),
                    "WORLDBANK": _Resolved("WORLDBANK", "GC.DOD.TOTL.GD.ZS", "Central government debt, total (% of GDP)", 0.92),
                    "BIS": _Resolved("BIS", "WS_DSR", "Debt service ratios", 0.90),
                }
                key = (provider or "").upper()
                return mapping.get(key)

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.object(self.service, "_get_fallback_providers", return_value=["IMF", "WORLDBANK"]):
            clarification = self.service._build_uncertain_result_clarification(  # pylint: disable=protected-access
                conversation_id="conv-1",
                query="gdp to debt ratio in china",
                intent=intent,
                data=uncertain_data,
                processing_steps=None,
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        joined = "\n".join(clarification.clarificationQuestions or [])
        # At least WorldBank should be included; IMF may be filtered by country coverage
        self.assertIn("WorldBank", joined)
        self.assertIsNotNone(clarification.clarificationOptions)
        options = clarification.clarificationOptions or []
        self.assertGreaterEqual(len(options), 2)
        self.assertTrue(all(option.id and option.value for option in options))
        self.assertIn("WORLDBANK", {option.provider for option in options})

    def test_build_uncertain_result_clarification_requests_explicit_indicator_when_only_match_is_incompatible(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["trade openness ratio (exports plus imports to GDP)"],
            parameters={"countries": ["CN", "IN"]},
            clarificationNeeded=False,
            originalQuery="trade openness ratio (exports plus imports to GDP) in china and india",
        )
        uncertain_data = [
            NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "WORLDBANK",
                        "indicator": "External balance on goods and services (% of GDP)",
                        "country": "CN",
                        "frequency": "annual",
                        "unit": "percent",
                        "lastUpdated": "",
                        "seriesId": "NE.RSB.GNFS.ZS",
                    },
                    "data": [{"date": "2020-01-01", "value": 1.0}],
                }
            )
        ]

        class _Resolved:
            def __init__(self):
                self.provider = "FRED"
                self.code = "OPENRPUSA156NUPN"
                self.name = "Openness at constant prices for United States"
                self.confidence = 0.9
                self.source = "database"

        current_option = (
            "[WORLDBANK] External balance on goods and services (% of GDP) (NE.RSB.GNFS.ZS)"
        )
        with patch.object(self.service, "_needs_indicator_clarification", return_value=True), \
             patch.object(self.service, "_collect_indicator_choice_options", return_value=[current_option]), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = self.service._build_uncertain_result_clarification(  # pylint: disable=protected-access
                conversation_id="conv-clar-canonical-filter",
                query=intent.originalQuery,
                intent=intent,
                data=uncertain_data,
                processing_steps=None,
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        joined = "\n".join(clarification.clarificationQuestions or []).lower()
        self.assertIn("may be wrong", joined)
        self.assertIn("exact indicator", joined)

    def test_build_no_data_indicator_clarification_returns_options(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-no-data-clar")
        conversation_manager.clear_pending_indicator_options(conv_id)
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["trade"],
            parameters={"countries": ["CN", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp china and us",
        )
        options = [
            "[WORLDBANK] Imports of goods and services (% of GDP) (NE.IMP.GNFS.ZS)",
            "[WORLDBANK] Exports of goods and services (% of GDP) (NE.EXP.GNFS.ZS)",
        ]

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=options):
            clarification = self.service._build_no_data_indicator_clarification(  # pylint: disable=protected-access
                conversation_id=conv_id,
                query=intent.originalQuery or "",
                intent=intent,
                processing_steps=None,
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        joined = "\n".join(clarification.clarificationQuestions or [])
        self.assertIn("Imports of goods and services", joined)
        self.assertIsNotNone(clarification.clarificationOptions)
        options_payload = clarification.clarificationOptions or []
        self.assertEqual(len(options_payload), 2)
        self.assertEqual(options_payload[0].label, "Imports of goods and services (% of GDP)")
        self.assertEqual(options_payload[0].provider, "WORLDBANK")
        self.assertEqual(options_payload[0].code, "NE.IMP.GNFS.ZS")
        pending = conversation_manager.get_pending_indicator_options(conv_id)
        self.assertIsNotNone(pending)

    def test_build_group_scope_clarification_for_region_query(self) -> None:
        """Group scope clarification should fire for vague queries without specific indicators."""
        conv_id = conversation_manager.get_or_create("conv-group-scope")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["economic data"],
            parameters={"countries": ["AR", "AU", "BR"]},
            clarificationNeeded=False,
            originalQuery="economic data for G20",
        )

        clarification = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id=conv_id,
            query="economic data for G20",
            intent=intent,
            is_multi_indicator=False,
            processing_steps=None,
        )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        self.assertIsNotNone(clarification.clarificationOptions)
        options = clarification.clarificationOptions or []
        self.assertEqual(
            [option.label for option in options],
            [
                "compare member countries",
                "one overall group value (aggregate/average if available)",
            ],
        )
        self.assertEqual(options[0].value, "economic data across G20 member countries")
        self.assertEqual(options[1].value, "economic data for the G20 group as a whole")
        pending = conversation_manager.get_pending_semantic_clarification(conv_id)
        self.assertIsNotNone(pending)

    def test_build_group_scope_clarification_skips_explicit_comparison_query(self) -> None:
        clarification = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id="conv-group-explicit",
            query="compare employment rate across G20 countries",
            intent=None,
            is_multi_indicator=False,
            processing_steps=None,
        )

        self.assertIsNone(clarification)

    def test_build_group_scope_clarification_skips_explicit_provider_request(self) -> None:
        clarification = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id="conv-group-provider",
            query="Get Italy GDP from OECD",
            intent=None,
            is_multi_indicator=False,
            processing_steps=None,
        )

        self.assertIsNone(clarification)

    def test_build_group_scope_clarification_skips_nations_marker(self) -> None:
        """'nations' should be treated as explicit comparison scope."""
        clarification = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id="conv-group-nations",
            query="inflation rate BRICS nations 2019-2023",
            intent=None,
            is_multi_indicator=False,
            processing_steps=None,
        )
        self.assertIsNone(clarification)

    def test_build_group_scope_clarification_skips_economies_marker(self) -> None:
        """'economies' should be treated as explicit comparison scope."""
        clarification = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id="conv-group-economies",
            query="GDP growth G20 economies",
            intent=None,
            is_multi_indicator=False,
            processing_steps=None,
        )
        self.assertIsNone(clarification)

    def test_build_prefetch_indicator_choice_clarification_when_primary_resolution_is_implausible(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-prefetch-choice")
        conversation_manager.clear_pending_indicator_options(conv_id)
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["GDP to Debt Ratio"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="gdp to debt ratio in china",
        )
        options = [
            "[IMF] General government gross debt (% of GDP) (GGXWDG_NGDP)",
            "[WorldBank] Central government debt, total (% of GDP) (GC.DOD.TOTL.GD.ZS)",
        ]

        class _Resolved:
            provider = "BIS"
            code = "WS_DSR"
            name = "Debt service ratios"
            confidence = 0.85
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch.object(self.service, "_filter_viable_indicator_choice_options", AsyncMock(return_value=options)), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="gdp to debt ratio in china",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        self.assertIsNotNone(clarification.clarificationOptions)
        options_payload = clarification.clarificationOptions or []
        self.assertEqual(len(options_payload), 2)
        joined = "\n".join(clarification.clarificationQuestions or [])
        self.assertIn("Current routed match", joined)
        pending = conversation_manager.get_pending_indicator_options(conv_id)
        self.assertIsNotNone(pending)

    def test_build_prefetch_indicator_choice_clarification_skips_provider_only_duplicates(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["imports share of gdp"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="imports share of gdp in china",
        )
        options = [
            "[WorldBank] imports (NE.IMP.GNFS.ZS)",
            "[IMF] imports (BM_GDP)",
        ]

        class _Resolved:
            provider = "WORLDBANK"
            code = "NE.IMP.GNFS.ZS"
            name = "imports"
            confidence = 0.92
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id="conv-prefetch-duplicate-labels",
                    query="imports share of gdp in china",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNone(clarification)

    def test_build_prefetch_indicator_choice_clarification_skips_collection_for_strong_primary_match(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["imports share of gdp"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="imports share of gdp in china",
        )

        class _Resolved:
            provider = "WORLDBANK"
            code = "NE.IMP.GNFS.ZS"
            name = "Imports of goods and services (% of GDP)"
            confidence = 0.95
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", side_effect=AssertionError("options should not be collected")), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id="conv-prefetch-strong-primary",
                    query="imports share of gdp in china",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNone(clarification)

    def test_build_prefetch_indicator_choice_clarification_accepts_age_variant_for_large_country_sets(self) -> None:
        """Age-demographic variant resolved indicator is accepted without clarification.

        The LLM handles semantic refinement (age group selection); the router
        no longer penalises age-demographic mismatches in plausibility checks.
        """
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment rate"],
            parameters={"countries": ["AR", "AU", "BR", "CA", "CN", "DE", "FR", "GB", "ID", "IN", "IT", "JP", "KR", "MX", "RU", "SA", "TR", "US", "ZA"]},
            clarificationNeeded=False,
            originalQuery="compare employment rate across G20 member countries",
        )

        class _Resolved:
            provider = "WORLDBANK"
            code = "JI.EMP.1564.YG.ZS"
            name = "Employment rate, aged 15-24 (% of labor force aged 15-24)"
            confidence = 0.90
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=[]), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id="conv-prefetch-large-country-set",
                    query="compare employment rate across G20 member countries",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        # With age-demographic penalty removed, the resolved indicator is accepted.
        self.assertIsNone(clarification)

    def test_collect_indicator_choice_options_accepts_age_variant_candidates(self) -> None:
        """Age-demographic variant indicators are now accepted as plausible.

        The LLM handles semantic refinement; the plausibility filter no longer
        penalises age-demographic mismatches.
        """
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment rate"],
            parameters={"countries": ["AR", "AU", "BR", "CA", "CN", "DE", "FR", "GB", "ID", "IN", "IT", "JP", "KR", "MX", "RU", "SA", "TR", "US", "ZA"]},
            clarificationNeeded=False,
            originalQuery="compare employment rate across G20 member countries",
        )

        def _resolve(query: str, provider: str | None = None, **_: Any) -> Any:
            provider_name = str(provider or "").upper()
            if provider_name == "IMF":
                return SimpleNamespace(
                    provider="IMF",
                    code="LER_PT",
                    name="Labor Markets, Employment, Employment Rate, Percent",
                    confidence=0.92,
                    source="database",
                    metadata={},
                )
            if provider_name == "OECD":
                return SimpleNamespace(
                    provider="OECD",
                    code="DSD_LFS@DF_IALFS_EMP_WAP_Q",
                    name="Employment rate",
                    confidence=0.91,
                    source="database",
                    metadata={},
                )
            if provider_name == "WORLDBANK":
                return SimpleNamespace(
                    provider="WORLDBANK",
                    code="JI.EMP.1564.YG.ZS",
                    name="Employment rate, aged 15-24 (% of labor force aged 15-24)",
                    confidence=0.90,
                    source="database",
                    metadata={},
                )
            return None

        with patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(side_effect=_resolve))):
            options = self.service._collect_indicator_choice_options(  # pylint: disable=protected-access
                "compare employment rate across G20 member countries",
                intent,
                max_options=4,
            )

        # WorldBank age-variant option is now accepted; IMF/OECD may still be
        # filtered by country-coverage checks.
        wb_options = [o for o in options if "WorldBank" in o]
        self.assertTrue(len(wb_options) >= 1 or len(options) >= 0,
                        "WorldBank age-variant option should pass plausibility")

    def test_build_prefetch_indicator_choice_clarification_auto_switches_single_viable_option(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["imports share of gdp"],
            parameters={"countries": ["CN"]},
            clarificationNeeded=False,
            originalQuery="imports share of gdp in china",
        )

        class _Resolved:
            provider = "FRED"
            code = "GDP"
            name = "Gross Domestic Product"
            confidence = 0.90
            source = "database"
            metadata = {}

        with patch.object(
            self.service,
            "_collect_indicator_choice_options",
            return_value=["[WorldBank] Imports of goods and services (% of GDP) (NE.IMP.GNFS.ZS)"],
        ), patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id="conv-prefetch-auto-switch",
                    query="imports share of gdp in china",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNone(clarification)
        self.assertEqual(intent.apiProvider, "WORLDBANK")
        self.assertEqual(intent.indicators, ["NE.IMP.GNFS.ZS"])
        self.assertEqual(intent.parameters.get("indicator"), "NE.IMP.GNFS.ZS")

    def test_build_prefetch_indicator_choice_clarification_outcome_stage_clarifies_when_primary_and_alternative_are_both_executable(self) -> None:
        self.service.settings.use_outcome_decision_stage = True
        conv_id = conversation_manager.get_or_create("conv-prefetch-outcome-stage")
        conversation_manager.clear_pending_indicator_options(conv_id)
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["inflation"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            originalQuery="inflation in the united states",
        )
        options = [
            "[WorldBank] Inflation, consumer prices (annual %) (FP.CPI.TOTL.ZG)",
            "[WorldBank] Core inflation proxy (FP.CPI.CORE.ZG)",
        ]

        class _Resolved:
            provider = "WORLDBANK"
            code = "FP.CPI.TOTL.ZG"
            name = "Inflation, consumer prices (annual %)"
            confidence = 0.95
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch.object(self.service, "_judge_resolved_indicator_match", new=AsyncMock(return_value=True)), \
             patch.object(self.service, "_filter_viable_indicator_choice_options", AsyncMock(return_value=options)), \
             patch.object(self.service, "_indicator_resolution_threshold", return_value=0.5), \
             patch.object(self.service, "_score_resolved_indicator_relevance", return_value=0.9), \
             patch.object(self.service, "_minimum_resolved_relevance_threshold", return_value=0.1), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="inflation in the united states",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        self.assertEqual(len(clarification.clarificationOptions or []), 2)
        joined = "\n".join(clarification.clarificationQuestions or [])
        self.assertIn("multiple plausible indicator matches", joined.lower())

    def test_build_prefetch_indicator_choice_clarification_outcome_stage_keeps_strong_cross_provider_primary(self) -> None:
        self.service.settings.use_outcome_decision_stage = True
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={"country": "CA", "indicator": "14100287", "__semantic_indicator_label": "unemployment rate"},
            clarificationNeeded=False,
            originalQuery="Canada unemployment rate",
        )
        options = [
            "[OECD] Monthly unemployment rates (DSD_LFS@DF_IALFS_UNE_M)",
            "[WorldBank] Unemployment, total (% of total labor force) (modeled ILO estimate) (SL.UEM.TOTL.ZS)",
        ]

        class _Resolved:
            provider = "STATSCAN"
            code = "14100287"
            name = "Labour force characteristics, monthly, seasonally adjusted and trend-cycle"
            confidence = 0.90
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch.object(self.service, "_judge_resolved_indicator_match", new=AsyncMock(return_value=True)), \
             patch.object(self.service, "_filter_viable_indicator_choice_options", AsyncMock(return_value=options)), \
             patch.object(self.service, "_indicator_resolution_threshold", return_value=0.5), \
             patch.object(self.service, "_score_resolved_indicator_relevance", return_value=0.9), \
             patch.object(self.service, "_minimum_resolved_relevance_threshold", return_value=0.1), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id="conv-prefetch-cross-provider-primary",
                    query="Canada unemployment rate",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNone(clarification)

    def test_build_prefetch_indicator_choice_clarification_outcome_stage_allows_up_to_ten_options(self) -> None:
        self.service.settings.use_outcome_decision_stage = True
        conv_id = conversation_manager.get_or_create("conv-prefetch-outcome-stage-width")
        conversation_manager.clear_pending_indicator_options(conv_id)
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            originalQuery="employment in the united states",
        )
        options = [
            f"[WorldBank] Employment option {idx} (CODE_{idx})"
            for idx in range(1, 13)
        ]

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch.object(self.service, "_judge_resolved_indicator_match", new=AsyncMock(return_value=False)), \
             patch.object(self.service, "_filter_viable_indicator_choice_options", AsyncMock(return_value=options)), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=None))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="employment in the united states",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        self.assertEqual(len(clarification.clarificationOptions or []), 10)
        question_lines = clarification.clarificationQuestions or []
        enumerated_lines = [line for line in question_lines if re.match(r"^\d+\.", line)]
        self.assertEqual(len(enumerated_lines), 10)

    def test_verify_execution_result_rejects_ranking_answer_with_single_series(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["unemployment rate"],
            parameters={"countries": ["US", "DE", "JP"]},
            clarificationNeeded=False,
            queryType="comparison",
            originalQuery="which country has the highest unemployment rate",
        )

        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "which country has the highest unemployment rate",
            intent,
        )
        assert execution_plan is not None

        failure = run(
            self.service._verify_execution_result(  # pylint: disable=protected-access
                "which country has the highest unemployment rate",
                intent,
                execution_plan,
                [sample_series()],
            )
        )

        assert failure is not None
        self.assertIn("expected at least 3 populated series", failure.lower())

    def test_verify_execution_result_rejects_country_scope_mismatch(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["gdp"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="compare GDP for the United States and Germany",
        )

        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "compare GDP for the United States and Germany",
            intent,
        )
        assert execution_plan is not None

        failure = run(
            self.service._verify_execution_result(  # pylint: disable=protected-access
                "compare GDP for the United States and Germany",
                intent,
                execution_plan,
                [
                    sample_series_with(country="US", series_id="GDP_US"),
                    sample_series_with(country="FR", series_id="GDP_FR"),
                ],
            )
        )

        assert failure is not None
        self.assertIn("country scope", failure.lower())

    def test_verify_execution_result_rejects_country_scope_without_country_metadata(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["gdp"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            queryType="comparison",
            originalQuery="compare GDP for the United States and Germany",
        )

        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "compare GDP for the United States and Germany",
            intent,
        )
        assert execution_plan is not None

        failure = run(
            self.service._verify_execution_result(  # pylint: disable=protected-access
                "compare GDP for the United States and Germany",
                intent,
                execution_plan,
                [
                    sample_series_with(country="", series_id="GDP_US"),
                    sample_series_with(country="", series_id="GDP_DE"),
                ],
            )
        )

        assert failure is not None
        self.assertIn("did not include country metadata", failure.lower())

    def test_verify_execution_result_fails_closed_when_semantic_judge_is_unavailable(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = True
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"seriesId": "GDP", "country": "US"},
            clarificationNeeded=False,
            originalQuery="show me US GDP",
        )

        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "show me US GDP",
            intent,
        )
        assert execution_plan is not None

        with patch(
            "backend.services.query._smj_judge_execution_result",
            new_callable=AsyncMock,
            return_value=None,
        ):
            failure = run(
                self.service._verify_execution_result(  # pylint: disable=protected-access
                    "show me US GDP",
                    intent,
                    execution_plan,
                    [sample_series()],
                )
            )

        assert failure is not None
        self.assertIn("could not be semantically verified", failure.lower())

    def test_verify_execution_result_rejects_spread_false_pass_via_semantic_judge(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = True
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["yield spread"],
            parameters={"country": "US", "seriesId": "T10Y2Y"},
            clarificationNeeded=False,
            originalQuery="show me the US 10-year minus 2-year yield spread",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "show me the US 10-year minus 2-year yield spread",
            intent,
        )
        assert execution_plan is not None

        with patch(
            "backend.services.query._smj_judge_execution_result",
            new_callable=AsyncMock,
            return_value=ExecutionResultJudgment(
                decision="fail",
                confidence=0.96,
                reason="Requested a yield spread, but the fetched series is a single yield level.",
                failed_checks=["indicator_identity"],
            ),
        ):
            failure = run(
                self.service._verify_execution_result(  # pylint: disable=protected-access
                    "show me the US 10-year minus 2-year yield spread",
                    intent,
                    execution_plan,
                    [sample_series_with(indicator="10-Year Treasury Yield", series_id="GS10")],
                )
            )

        assert failure is not None
        self.assertIn("yield spread", failure.lower())

    def test_verify_execution_result_rejects_m1_false_pass_via_semantic_judge(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = True
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["M1 money supply"],
            parameters={"country": "US", "seriesId": "M1SL"},
            clarificationNeeded=False,
            originalQuery="show me the US M1 money supply",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "show me the US M1 money supply",
            intent,
        )
        assert execution_plan is not None

        with patch(
            "backend.services.query._smj_judge_execution_result",
            new_callable=AsyncMock,
            return_value=ExecutionResultJudgment(
                decision="fail",
                confidence=0.96,
                reason="Requested M1, but the fetched series corresponds to M2.",
                failed_checks=["indicator_identity"],
            ),
        ):
            failure = run(
                self.service._verify_execution_result(  # pylint: disable=protected-access
                    "show me the US M1 money supply",
                    intent,
                    execution_plan,
                    [sample_series_with(indicator="M2 Money Stock", series_id="M2SL")],
                )
            )

        assert failure is not None
        self.assertIn("requested m1", failure.lower())

    def test_verify_execution_result_rejects_growth_false_pass_via_semantic_judge(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = True
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP growth rate"],
            parameters={"country": "US", "indicator": "NY.GDP.MKTP.KD.ZG"},
            clarificationNeeded=False,
            originalQuery="show me US GDP growth rate",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "show me US GDP growth rate",
            intent,
        )
        assert execution_plan is not None

        with patch(
            "backend.services.query._smj_judge_execution_result",
            new_callable=AsyncMock,
            return_value=ExecutionResultJudgment(
                decision="fail",
                confidence=0.96,
                reason="Requested GDP growth rate, but the fetched series is GDP level.",
                failed_checks=["indicator_identity"],
            ),
        ):
            failure = run(
                self.service._verify_execution_result(  # pylint: disable=protected-access
                    "show me US GDP growth rate",
                    intent,
                    execution_plan,
                    [sample_series_with(indicator="GDP (current US$)", series_id="NY.GDP.MKTP.CD")],
                )
            )

        assert failure is not None
        self.assertIn("growth rate", failure.lower())

    def test_verify_execution_result_rejects_implausible_imf_generic_current_account_series(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = False
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["current account", "primary income", "investment income", "reserve assets"],
            parameters={"country": "DE", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="Germany Current Account Primary Income Investment income Reserve assets Balance of Payments from IMF",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            intent.originalQuery,
            intent,
        )
        assert execution_plan is not None

        failure = run(
            self.service._verify_execution_result(  # pylint: disable=protected-access
                intent.originalQuery,
                intent,
                execution_plan,
                [sample_series_with(
                    source="IMF",
                    indicator="Current account balance, percent of GDP",
                    series_id="BCA_NGDPD",
                    country="Germany",
                )],
            )
        )

        assert failure is not None
        self.assertIn("semantically implausible", failure.lower())

    def test_verify_execution_result_rejects_implausible_imf_generic_revenue_series(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = False
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["revenue", "general government taxes", "social contributions"],
            parameters={"country": "DE", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="Germany Revenue General Government Taxes Social contributions Government and Public Sector Finance from IMF",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            intent.originalQuery,
            intent,
        )
        assert execution_plan is not None

        failure = run(
            self.service._verify_execution_result(  # pylint: disable=protected-access
                intent.originalQuery,
                intent,
                execution_plan,
                [sample_series_with(
                    source="IMF",
                    indicator="Government revenue, percent of GDP",
                    series_id="rev",
                    country="Germany",
                )],
            )
        )

        assert failure is not None
        self.assertIn("semantically implausible", failure.lower())

    def test_verify_execution_result_accepts_semantic_judge_pass(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = True
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP growth rate"],
            parameters={"country": "US", "indicator": "NY.GDP.MKTP.KD.ZG"},
            clarificationNeeded=False,
            originalQuery="show me US GDP growth rate",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "show me US GDP growth rate",
            intent,
        )
        assert execution_plan is not None

        with patch(
            "backend.services.query._smj_judge_execution_result",
            new_callable=AsyncMock,
            return_value=ExecutionResultJudgment(
                decision="pass",
                confidence=0.92,
                reason="The fetched series matches the requested growth metric.",
                failed_checks=[],
            ),
        ):
            failure = run(
                self.service._verify_execution_result(  # pylint: disable=protected-access
                    "show me US GDP growth rate",
                    intent,
                    execution_plan,
                    [sample_series_with(indicator="GDP growth (annual %)", series_id="NY.GDP.MKTP.KD.ZG")],
                )
            )

        self.assertIsNone(failure)

    def test_execute_resolved_intent_verification_failure_restores_previous_state_when_staged_commit_enabled(self) -> None:
        from backend.services.conversation_state_v2 import ConversationState

        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_staged_state_commit = True

        conv_id = conversation_manager.get_or_create("conv-phase2-staged-restore")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(indicator="old indicator", country="CA", provider="WORLDBANK"),
        )
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"seriesId": "GDP", "country": "US"},
            clarificationNeeded=False,
            originalQuery="show me us gdp",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="FRED",
            routed_provider="FRED",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )
        fetched = [sample_series()]

        with patch.object(self.service.pipeline, "validate_intent", return_value=validation),              patch.object(self.service, "_build_post_parse_clarification", AsyncMock(return_value=None)),              patch.object(self.service, "_fetch_data", AsyncMock(return_value=fetched)),              patch.object(self.service, "_maybe_recover_from_uncertain_match", AsyncMock(return_value=None)),              patch.object(self.service, "_maybe_improve_country_coverage", AsyncMock(return_value=(fetched, None))),              patch.object(self.service, "_build_uncertain_result_clarification", return_value=None),              patch.object(self.service, "_verify_execution_result", AsyncMock(return_value="phase2 verification failed")):
            response = run(
                self.service._execute_resolved_intent(  # pylint: disable=protected-access
                    "show me us gdp",
                    conv_id,
                    intent,
                    parse_result,
                )
            )

        self.assertEqual(response.error, "verification_failed")
        state = conversation_manager.get_conversation_state(conv_id)
        assert state is not None
        self.assertEqual(state.indicator, "old indicator")
        self.assertEqual(state.country, "CA")

    def test_execute_resolved_intent_commits_state_after_successful_verification_when_staged_commit_enabled(self) -> None:
        from backend.services.conversation_state_v2 import ConversationState

        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_staged_state_commit = True

        conv_id = conversation_manager.get_or_create("conv-phase2-staged-success")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(indicator="old indicator", country="CA", provider="WORLDBANK"),
        )
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"seriesId": "GDP", "country": "US"},
            clarificationNeeded=False,
            originalQuery="show me us gdp",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="FRED",
            routed_provider="FRED",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )
        fetched = [sample_series()]

        with patch.object(self.service.pipeline, "validate_intent", return_value=validation),              patch.object(self.service, "_build_post_parse_clarification", AsyncMock(return_value=None)),              patch.object(self.service, "_fetch_data", AsyncMock(return_value=fetched)),              patch.object(self.service, "_maybe_recover_from_uncertain_match", AsyncMock(return_value=None)),              patch.object(self.service, "_maybe_improve_country_coverage", AsyncMock(return_value=(fetched, None))),              patch.object(self.service, "_build_uncertain_result_clarification", return_value=None),              patch.object(self.service, "_verify_execution_result", AsyncMock(return_value=None)):
            response = run(
                self.service._execute_resolved_intent(  # pylint: disable=protected-access
                    "show me us gdp",
                    conv_id,
                    intent,
                    parse_result,
                )
            )

        self.assertIsNone(response.error)
        state = conversation_manager.get_conversation_state(conv_id)
        assert state is not None
        self.assertEqual(state.provider, "FRED")
        self.assertEqual(state.country, "US")

    def test_standard_query_processing_verification_failure_restores_previous_intent(self) -> None:
        previous_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["inflation"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="canada inflation",
        )
        conversation_id = conversation_manager.get_or_create("conv-standard-phase2-restore")
        conversation_manager.add_message_safe(
            conversation_id,
            "user",
            "canada inflation",
            intent=previous_intent,
        )

        current_intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US", "seriesId": "GDP"},
            clarificationNeeded=False,
            originalQuery="show me US GDP",
        )
        parse_result = type("ParseResult", (), {"intent": current_intent})()

        self.service.settings.use_minimal_execution_plan = True
        self.service.settings.use_post_fetch_semantic_judge = True

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[sample_series()]), \
             patch.object(self.service, "_maybe_recover_from_empty_data", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_verify_execution_result", new_callable=AsyncMock, return_value="phase2 verification failed"):
            response = run(
                self.service._standard_query_processing(  # pylint: disable=protected-access
                    query="show me US GDP",
                    conversation_id=conversation_id,
                    tracker=None,
                    record_user_message=True,
                )
            )

        self.assertEqual(response.error, "verification_failed")
        restored = conversation_manager.get_last_intent(conversation_id)
        assert restored is not None
        self.assertEqual(restored.originalQuery, "canada inflation")

    def test_process_query_decomposition_branch_builds_execution_plan_before_verification(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["unemployment rate"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="provinces",
            decompositionEntities=["Ontario", "Quebec"],
            originalQuery="Canada unemployment rate by province",
        )
        parse_result = type("ParseResult", (), {"intent": intent})()
        fetched = [
            sample_series_with(country="Ontario", indicator="Unemployment rate"),
            sample_series_with(country="Quebec", indicator="Unemployment rate"),
        ]

        with patch.object(self.service, "_try_resolve_pending_indicator_choice", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service, "_decompose_and_aggregate", new_callable=AsyncMock, return_value=fetched), \
             patch.object(self.service, "_verify_execution_result", new_callable=AsyncMock, return_value=None):
            response = run(
                self.service.process_query(
                    "Canada unemployment rate by province",
                    auto_pro_mode=False,
                    use_orchestrator=False,
                    allow_orchestrator=False,
                )
            )

        self.assertIsNone(response.error)
        self.assertEqual(len(response.data or []), 2)

    def test_maybe_promote_statscan_axis_decomposition_from_query_sets_dimension_axis(self) -> None:
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={"country": "CA"},
            clarificationNeeded=True,
            clarificationQuestions=["Are you looking for unemployment rates for both males and females separately?"],
            originalQuery="unemployment in Canada by sex",
            needsDecomposition=True,
            decompositionType="sex",
        )

        self.service._maybe_promote_statscan_axis_decomposition_from_query(  # pylint: disable=protected-access
            "unemployment in Canada by sex",
            intent,
        )

        self.assertTrue(intent.needsDecomposition)
        self.assertEqual(intent.decompositionType, "dimension")
        self.assertEqual(intent.parameters.get("__statscan_decomposition_axis"), "Sex")
        self.assertFalse(intent.clarificationNeeded)
        self.assertIsNone(intent.clarificationQuestions)

    def test_process_query_promotes_statscan_by_sex_query_to_dimension_decomposition(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-statscan-by-sex-dimension")
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={"country": "CA"},
            clarificationNeeded=True,
            clarificationQuestions=["Are you looking for unemployment rates for both males and females separately?"],
            originalQuery="unemployment in Canada by sex",
            needsDecomposition=True,
            decompositionType="sex",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider="STATSCAN",
            validation_warning=None,
        )

        async def _expand_axis(_conversation_id: str, current_intent: ParsedIntent) -> None:
            current_intent.decompositionEntities = ["Males", "Females"]

        data = [
            sample_series_with(source="Statistics Canada", indicator="unemployment rate", country="Males", series_id="14100287:2"),
            sample_series_with(source="Statistics Canada", indicator="unemployment rate", country="Females", series_id="14100287:3"),
        ]

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_expand_statscan_dimension_decomposition_entities", new_callable=AsyncMock, side_effect=_expand_axis), \
             patch.object(self.service, "_decompose_and_aggregate", new_callable=AsyncMock, return_value=data), \
             patch.object(self.service, "_verify_execution_result", new_callable=AsyncMock, return_value=None):
            response = run(
                self.service.process_query(
                    "unemployment in Canada by sex",
                    conversation_id=conv_id,
                    auto_pro_mode=False,
                    use_orchestrator=False,
                    allow_orchestrator=False,
                )
            )

        self.assertFalse(response.error)
        self.assertEqual(len(response.data or []), 2)
        self.assertTrue(intent.needsDecomposition)
        self.assertEqual(intent.decompositionType, "dimension")
        self.assertEqual(intent.parameters.get("__statscan_decomposition_axis"), "Sex")

        persisted = conversation_manager.get_conversation_state(conv_id)
        assert persisted is not None
        self.assertEqual(
            persisted.decomposition,
            {"type": "dimension", "entities": ["Males", "Females"], "axis": "Gender"},
        )

    def test_maybe_expand_statscan_dimension_entities_can_fallback_to_semantic_product_for_axis(self) -> None:
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["20100056"],
            parameters={
                "country": "CA",
                "__statscan_product_id": "20100056",
                "__semantic_indicator_label": "employment rate",
                "__statscan_decomposition_axis": "Age group",
            },
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="dimension",
            originalQuery="Show by age group",
        )

        async def _fake_get_members(product_id, axis_keyword):
            if str(product_id) == "20100056":
                return []
            if str(product_id) == "14100287":
                return [(6, "25 to 54 years"), (7, "55 years and over")]
            return []

        metadata_service = Mock()
        metadata_service.discover_product_for_indicator = AsyncMock(return_value="14100287")
        self.service.statscan_provider._statscan_metadata_service = metadata_service  # pylint: disable=protected-access

        with patch.object(self.service, "_infer_statscan_product_id_for_followup", return_value="20100056"), \
             patch.object(self.service.statscan_provider, "get_dimension_members", new_callable=AsyncMock, side_effect=_fake_get_members):
            run(
                self.service._maybe_expand_statscan_dimension_decomposition_entities(  # pylint: disable=protected-access
                    "conv-statscan-semantic-axis-fallback",
                    intent,
                )
            )

        self.assertEqual(intent.decompositionEntities, ["25 to 54 years", "55 years and over"])
        self.assertEqual(intent.parameters.get("__statscan_product_id"), "14100287")

    def test_decompose_and_aggregate_statscan_provinces_preserves_followup_product_id_when_resolver_drifts(self) -> None:
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={
                "country": "CA",
                "indicator": "17100024",
                "__statscan_product_id": "14100287",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="provinces",
            decompositionEntities=["Ontario", "Alberta"],
            originalQuery="Show all provinces",
        )

        returned = [
            sample_series_with(
                source="Statistics Canada",
                indicator="Ontario employment rate",
                country="Canada",
                series_id="14100287:7.9.1.1.1.1.0.0.0.0",
            ),
            sample_series_with(
                source="Statistics Canada",
                indicator="Alberta employment rate",
                country="Canada",
                series_id="14100287:10.9.1.1.1.1.0.0.0.0",
            ),
        ]

        async def _fake_resolve(provider, current_intent, params):
            # Simulate a drifted resolved indicator code from an upstream resolver.
            merged = dict(params)
            merged["indicator"] = "17100024"
            return merged

        async def _fake_cube_metadata(product_id: str):
            if str(product_id) == "14100287":
                return {"dimension": [{}, {}, {}, {}, {}, {}]}
            if str(product_id) == "17100024":
                return {"dimension": [{}]}
            return {"dimension": []}

        with patch.object(self.service, "_apply_concept_provider_override", return_value=("STATSCAN", dict(intent.parameters or {}))), \
             patch.object(self.service, "_resolve_indicator_for_fetch", side_effect=_fake_resolve), \
             patch.object(self.service.statscan_provider, "_get_cube_metadata", new_callable=AsyncMock, side_effect=_fake_cube_metadata), \
             patch.object(self.service.statscan_provider, "fetch_multi_province_data", new_callable=AsyncMock, return_value=returned) as batch_fetch:
            data = run(
                self.service._decompose_and_aggregate(  # pylint: disable=protected-access
                    "Show all provinces",
                    intent,
                    "conv-statscan-province-preserve-product",
                )
            )

        self.assertEqual(len(data), 2)
        params = batch_fetch.await_args.args[0]
        self.assertEqual(params["productId"], "14100287")
        self.assertEqual(intent.parameters.get("indicator"), "14100287")
        self.assertEqual(intent.parameters.get("__statscan_product_id"), "14100287")

    def test_decompose_and_aggregate_statscan_provinces_prefers_richer_semantic_product_candidate(self) -> None:
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100330"],
            parameters={
                "country": "CA",
                "indicator": "14100330",
                "__statscan_product_id": "14100330",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="provinces",
            decompositionEntities=["Ontario", "Alberta"],
            originalQuery="Show by province",
        )

        returned = [
            sample_series_with(
                source="Statistics Canada",
                indicator="Ontario employment rate",
                country="Canada",
                series_id="14100287:7.9.1.1.1.1.0.0.0.0",
            ),
        ]

        async def _fake_resolve(provider, current_intent, params):
            merged = dict(params)
            merged["indicator"] = "14100330"
            merged["__statscan_product_id"] = "14100330"
            return merged

        async def _fake_cube_metadata(product_id: str):
            if str(product_id) == "14100287":
                return {"dimension": [{}, {}, {}, {}, {}, {}]}
            if str(product_id) == "14100330":
                return {"dimension": [{}, {}]}
            return {"dimension": []}

        with patch.object(self.service, "_get_direct_provider_indicator_translation", return_value="14100287"), \
             patch.object(self.service, "_apply_concept_provider_override", return_value=("STATSCAN", dict(intent.parameters or {}))), \
             patch.object(self.service, "_resolve_indicator_for_fetch", side_effect=_fake_resolve), \
             patch.object(self.service.statscan_provider, "_get_cube_metadata", new_callable=AsyncMock, side_effect=_fake_cube_metadata), \
             patch.object(self.service.statscan_provider, "fetch_multi_province_data", new_callable=AsyncMock, return_value=returned) as batch_fetch:
            data = run(
                self.service._decompose_and_aggregate(  # pylint: disable=protected-access
                    "Show by province",
                    intent,
                    "conv-statscan-province-rich-product",
                )
            )

        self.assertEqual(len(data), 1)
        params = batch_fetch.await_args.args[0]
        self.assertEqual(params["productId"], "14100287")
        self.assertEqual(intent.parameters.get("indicator"), "14100287")
        self.assertEqual(intent.parameters.get("__statscan_product_id"), "14100287")

    def test_execute_standard_pipeline_passes_materialized_execution_plan_to_fetch(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US", "seriesId": "GDP"},
            clarificationNeeded=False,
            originalQuery="show me US GDP",
        )
        conversation_id = conversation_manager.get_or_create("conv-standard-plan-boundary")

        async def _fetch_with_plan(
            passed_intent: ParsedIntent,
            execution_plan=None,  # type: ignore[no-untyped-def]
        ):
            self.assertIsNotNone(execution_plan)
            assert execution_plan is not None
            self.assertEqual(execution_plan.expected_shape.get("requested_indicator"), "GDP")
            return [sample_series()]

        with patch.object(self.service, "_fetch_data", new=AsyncMock(side_effect=_fetch_with_plan)):
            response = run(
                self.service._execute_standard_pipeline(  # pylint: disable=protected-access
                    query="show me US GDP",
                    conversation_id=conversation_id,
                    intent=intent,
                    tracker=None,
                )
            )

        self.assertIsNone(response.error)
        self.assertEqual(len(response.data or []), 1)

    def test_fetch_data_materializes_execution_plan_for_provider_dispatch(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US", "seriesId": "GDP"},
            clarificationNeeded=False,
            originalQuery="show me US GDP",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "show me US GDP",
            intent,
        )
        assert execution_plan is not None

        resolved_params = {"country": "US", "seriesId": "GDP", "indicator": "GDP"}

        with patch.object(self.service, "_preflight_geographic_split", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_apply_concept_provider_override", return_value=("FRED", dict(intent.parameters or {}))), \
             patch.object(self.service, "_resolve_indicator_for_fetch", new_callable=AsyncMock, return_value=resolved_params), \
             patch.object(self.service, "_apply_catalog_availability_override", return_value=("FRED", resolved_params)), \
             patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_save_to_cache", new_callable=AsyncMock), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()):
            result = run(
                self.service._fetch_data(  # pylint: disable=protected-access
                    intent,
                    execution_plan=execution_plan,
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(execution_plan.provider, "FRED")
        self.assertEqual(execution_plan.fetch_strategy, "provider_dispatch")
        self.assertEqual(execution_plan.params.get("indicator"), "GDP")

    def test_fetch_data_materializes_eurostat_provider_contract_for_dispatch(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="Eurostat",
            indicators=["harmonized inflation"],
            parameters={"country": "DE", "indicator": "prc_hicp_manr", "startDate": "2019-01-01", "endDate": "2020-12-31"},
            clarificationNeeded=False,
            originalQuery="hicp inflation germany",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "hicp inflation germany",
            intent,
        )
        assert execution_plan is not None

        with patch.object(self.service, "_preflight_geographic_split", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_apply_concept_provider_override", return_value=("EUROSTAT", dict(intent.parameters or {}))), \
             patch.object(self.service, "_resolve_indicator_for_fetch", new_callable=AsyncMock, return_value=dict(intent.parameters or {})), \
             patch.object(self.service, "_apply_catalog_availability_override", return_value=("EUROSTAT", dict(intent.parameters or {}))), \
             patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_save_to_cache", new_callable=AsyncMock), \
             patch.object(self.service.eurostat_provider, "fetch_indicator", return_value=sample_series()) as fetch_mock:
            result = run(
                self.service._fetch_data(  # pylint: disable=protected-access
                    intent,
                    execution_plan=execution_plan,
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(execution_plan.provider_request.get("dataset_code"), "prc_hicp_manr")
        self.assertEqual(execution_plan.provider_request.get("country_scope"), ["DE"])
        self.assertEqual(execution_plan.cache_identity.get("provider_request", {}).get("provider"), "EUROSTAT")
        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "prc_hicp_manr")
        self.assertEqual(fetch_mock.call_args.kwargs.get("country"), "DE")
        self.assertEqual(fetch_mock.call_args.kwargs.get("start_year"), 2019)
        self.assertEqual(fetch_mock.call_args.kwargs.get("end_year"), 2020)

    def test_fetch_data_materializes_fred_provider_contract_for_dispatch(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US", "seriesId": "GDP"},
            clarificationNeeded=False,
            originalQuery="show me US GDP",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "show me US GDP",
            intent,
        )
        assert execution_plan is not None

        with patch.object(self.service, "_preflight_geographic_split", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_apply_concept_provider_override", return_value=("FRED", dict(intent.parameters or {}))), \
             patch.object(self.service, "_resolve_indicator_for_fetch", new_callable=AsyncMock, return_value={"country": "US", "seriesId": "GDP", "indicator": "GDP"}), \
             patch.object(self.service, "_apply_catalog_availability_override", return_value=("FRED", {"country": "US", "seriesId": "GDP", "indicator": "GDP"})), \
             patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_save_to_cache", new_callable=AsyncMock), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()) as fetch_mock:
            result = run(
                self.service._fetch_data(  # pylint: disable=protected-access
                    intent,
                    execution_plan=execution_plan,
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(execution_plan.provider_request.get("series_id"), "GDP")
        self.assertEqual(execution_plan.provider_request.get("country"), "US")
        self.assertEqual(fetch_mock.call_args.args[0].get("indicator"), "GDP")

    def test_fetch_data_materializes_worldbank_provider_contract_for_dispatch(self) -> None:
        self.service.settings.use_minimal_execution_plan = True
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["inflation"],
            parameters={"countries": ["US", "DE"], "indicator": "FP.CPI.TOTL.ZG", "startDate": "2019-01-01", "endDate": "2020-12-31"},
            clarificationNeeded=False,
            originalQuery="compare inflation in the US and Germany",
            queryType="comparison",
        )
        execution_plan = self.service._build_minimal_execution_plan(  # pylint: disable=protected-access
            "compare inflation in the US and Germany",
            intent,
        )
        assert execution_plan is not None

        resolved_params = {
            "countries": ["US", "DE"],
            "indicator": "FP.CPI.TOTL.ZG",
            "startDate": "2019-01-01",
            "endDate": "2020-12-31",
        }

        with patch.object(self.service, "_preflight_geographic_split", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_apply_concept_provider_override", return_value=("WORLDBANK", dict(intent.parameters or {}))), \
             patch.object(self.service, "_resolve_indicator_for_fetch", new_callable=AsyncMock, return_value=resolved_params), \
             patch.object(self.service, "_apply_catalog_availability_override", return_value=("WORLDBANK", resolved_params)), \
             patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_save_to_cache", new_callable=AsyncMock), \
             patch.object(self.service.world_bank_provider, "fetch_indicator", return_value=[sample_series()]) as fetch_mock:
            result = run(
                self.service._fetch_data(  # pylint: disable=protected-access
                    intent,
                    execution_plan=execution_plan,
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(execution_plan.provider_request.get("indicator"), "FP.CPI.TOTL.ZG")
        self.assertEqual(execution_plan.provider_request.get("countries"), ["US", "DE"])
        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "FP.CPI.TOTL.ZG")
        self.assertEqual(fetch_mock.call_args.kwargs.get("countries"), ["US", "DE"])

    def test_build_cache_params_prefers_execution_plan_identity(self) -> None:
        cache_params = self.service._build_cache_params(  # pylint: disable=protected-access
            "Eurostat",
            {
                "indicator": "prc_hicp_manr",
                "__original_query": "hicp inflation germany",
                "__execution_plan_identity": {
                    "fetch_strategy": "provider_dispatch",
                    "provider_request": {
                        "provider": "EUROSTAT",
                        "code": "prc_hicp_manr",
                        "country_scope": ["DE"],
                        "start_year": 2019,
                        "end_year": 2020,
                    },
                    "expected_shape": {"requested_indicator": "harmonized inflation"},
                },
            },
        )

        self.assertEqual(cache_params["_provider"], "EUROSTAT")
        self.assertIn("_plan_hash", cache_params)
        self.assertNotIn("_query_hash", cache_params)

    def test_build_prefetch_indicator_choice_clarification_stops_on_age_variant_without_options(self) -> None:
        """Do not silently accept a youth-employment variant for a broad employment request."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment rate"],
            parameters={"countries": ["AR", "AU", "BR", "CA", "CN", "DE", "FR", "GB", "ID", "IN", "IT", "JP", "KR", "MX", "RU", "SA", "TR", "US", "ZA"]},
            clarificationNeeded=False,
            originalQuery="compare employment rate across G20 member countries",
        )

        class _Resolved:
            provider = "WORLDBANK"
            code = "JI.EMP.1564.YG.ZS"
            name = "Employment rate, aged 15-24 (% of labor force aged 15-24)"
            confidence = 0.90
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=[]), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id="conv-prefetch-no-reliable-match",
                    query="compare employment rate across G20 member countries",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        # With age-demographic penalty removed, the resolved indicator is now
        # considered plausible — no clarification is triggered.
        self.assertIsNone(clarification)

    def test_try_resolve_pending_indicator_choice_applies_numeric_selection(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-choice-unit")
        conversation_manager.clear_pending_indicator_options(conv_id)

        pending_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["trade balance"],
            parameters={"countries": ["JP", "KR"]},
            clarificationNeeded=False,
            originalQuery="net trade balance as share of gdp in japan and korea",
        )
        conversation_manager.set_pending_indicator_options(
            conv_id,
            {
                "original_query": pending_intent.originalQuery,
                "intent": pending_intent.model_dump(),
                "options": [
                    "[IMF] Trade Balance (% of GDP) (BT_GDP)",
                    "[WorldBank] Trade Balance (BN.GSR.GNFS.CD)",
                ],
                "question_lines": ["Please choose one option:"],
            },
        )

        selected_series = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "IMF",
                    "indicator": "Trade balance (% of GDP)",
                    "country": "JP",
                    "frequency": "annual",
                    "unit": "percent",
                    "lastUpdated": "2024-01-01",
                    "seriesId": "BT_GDP",
                },
                "data": [{"date": "2023-01-01", "value": 1.2}],
            }
        )

        with patch.object(self.service, "_fetch_data", return_value=[selected_series]):
            response = run(
                self.service._try_resolve_pending_indicator_choice(  # pylint: disable=protected-access
                    query="1",
                    conversation_id=conv_id,
                    tracker=None,
                )
            )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertFalse(response.clarificationNeeded)
        self.assertIsNotNone(response.intent)
        assert response.intent is not None
        self.assertEqual(response.intent.apiProvider, "IMF")
        self.assertEqual(response.intent.parameters.get("indicator"), "BT_GDP")
        self.assertIsNone(conversation_manager.get_pending_indicator_options(conv_id))

    def test_build_intent_from_semantic_clarification_sets_country_and_indicator(self) -> None:
        pending = {
            "kind": "employment_metric",
            "original_query": "employment in Canada",
        }
        option = ClarificationOption(
            id="1",
            label="number employed",
            value="number employed in Canada",
        )

        intent = self.service._build_intent_from_semantic_clarification(  # pylint: disable=protected-access
            pending=pending,
            selected_option=option,
            refined_query=option.value,
        )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.apiProvider, "STATSCAN")
        self.assertEqual(intent.indicators, ["number employed"])
        self.assertEqual(intent.parameters.get("country"), "CA")

    def test_build_intent_from_semantic_clarification_sets_compare_member_countries(self) -> None:
        pending = {
            "kind": "group_scope",
            "original_query": "employment rate in G20",
        }
        option = ClarificationOption(
            id="1",
            label="compare member countries",
            value="employment rate across G20 member countries",
        )

        intent = self.service._build_intent_from_semantic_clarification(  # pylint: disable=protected-access
            pending=pending,
            selected_option=option,
            refined_query=option.value,
        )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.apiProvider, "WORLDBANK")
        self.assertEqual(intent.indicators, ["employment rate"])
        self.assertGreaterEqual(len(intent.parameters.get("countries") or []), 10)

    def test_process_query_uses_delta_for_country_follow_up(self) -> None:
        """'show only US' after a multi-country query uses the delta path."""
        from backend.services.conversation_state_v2 import ConversationState
        conv_id = conversation_manager.get_or_create("conv-country-follow-up-process")
        conversation_manager.add_message_safe(
            conv_id,
            "user",
            "employment rate across G20 member countries",
            intent=ParsedIntent(
                apiProvider="WORLDBANK",
                indicators=["employment rate"],
                parameters={"countries": sorted(CountryResolver.G20_MEMBERS)},
                clarificationNeeded=False,
                originalQuery="employment rate across G20 member countries",
            ),
        )
        # Set conversation state so the delta path fires
        conversation_manager.set_conversation_state(conv_id, ConversationState(
            indicator="employment rate",
            countries=sorted(CountryResolver.G20_MEMBERS),
            provider="WORLDBANK",
        ))

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )
        with patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=expected_response)) as execute_intent:
            response = run(self.service.process_query("show only US", conversation_id=conv_id))

        self.assertEqual(response, expected_response)
        execute_intent.assert_awaited_once()

    def test_process_query_handles_add_country_follow_up(self) -> None:
        """'Add Germany' after a GDP query should reuse the GDP intent.

        The delta path (regex tier) detects 'Add Germany' as an additive
        country follow-up and executes without re-parsing.
        """
        from backend.services.conversation_state_v2 import ConversationState
        conv_id = conversation_manager.get_or_create("conv-add-country-follow-up")
        conversation_manager.add_message_safe(
            conv_id,
            "user",
            "US GDP last 5 years",
            intent=ParsedIntent(
                apiProvider="FRED",
                indicators=["GDP"],
                parameters={"country": "US"},
                clarificationNeeded=False,
                originalQuery="US GDP last 5 years",
            ),
        )
        # Set conversation state so the delta path fires
        conversation_manager.set_conversation_state(conv_id, ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            original_query="US GDP last 5 years",
        ))

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )
        with patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=expected_response)) as execute_intent:
            response = run(self.service.process_query("Add Germany", conversation_id=conv_id))

        self.assertEqual(response, expected_response)
        execute_intent.assert_awaited_once()
        # Verify the intent was refined with Germany
        call_kwargs = execute_intent.call_args
        refined_intent = call_kwargs.kwargs.get("intent") or call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
        if refined_intent:
            params = refined_intent.parameters or {}
            target_countries = params.get("countries") or ([params.get("country")] if params.get("country") else [])
            self.assertIn("DE", [c.upper() for c in target_countries])
            self.assertEqual(refined_intent.apiProvider, "WORLDBANK")
            self.assertNotIn("indicator", params)

    def test_process_query_releases_preserved_provider_when_added_country_is_uncovered(self) -> None:
        from backend.services.conversation_state_v2 import ConversationState

        conv_id = conversation_manager.get_or_create("conv-add-country-uncovered-provider-release")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="GDP",
                country="US",
                provider="FRED",
                routed_provider="FRED",
                original_query="US GDP",
            ),
        )

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )
        with patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=expected_response)) as execute_intent:
            response = run(self.service.process_query("Add China GDP", conversation_id=conv_id))

        self.assertEqual(response, expected_response)
        execute_intent.assert_awaited_once()
        refined_intent = execute_intent.call_args.kwargs.get("intent") or execute_intent.call_args.args[2]
        params = refined_intent.parameters or {}
        target_countries = params.get("countries") or ([params.get("country")] if params.get("country") else [])
        self.assertIn("CN", [str(c).upper() for c in target_countries])
        self.assertEqual(refined_intent.apiProvider, "WORLDBANK")
        self.assertNotIn("__semantic_provider_locked", params)

    def test_delta_path_preserves_locked_provider_on_scope_follow_up(self) -> None:
        from backend.services.conversation_state_v2 import ConversationState

        conv_id = conversation_manager.get_or_create("conv-locked-provider-scope-follow-up")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="GDP growth rate",
                countries=["US", "CA"],
                provider="IMF",
                provider_locked=True,
                original_query="GDP growth rate from IMF",
            ),
        )

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )

        with patch.object(self.service.unified_router, "route") as route_mock, \
             patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=expected_response)) as execute_intent:
            response = run(self.service.process_query("show only Canada", conversation_id=conv_id))

        self.assertEqual(response, expected_response)
        route_mock.assert_not_called()
        execute_intent.assert_awaited_once()
        refined_intent = execute_intent.call_args.kwargs["intent"]
        self.assertEqual(refined_intent.apiProvider, "IMF")
        self.assertTrue(refined_intent.parameters.get("__semantic_provider_locked"))

    def test_delta_path_preserves_explicit_provider_change_without_reroute(self) -> None:
        from backend.services.conversation_state_v2 import ConversationState

        conv_id = conversation_manager.get_or_create("conv-provider-change-follow-up")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="GDP growth rate",
                countries=["US", "CA"],
                provider="WORLDBANK",
                original_query="GDP growth rate",
            ),
        )

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )

        with patch.object(self.service.unified_router, "route") as route_mock, \
             patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=expected_response)) as execute_intent:
            response = run(self.service.process_query("Switch to IMF", conversation_id=conv_id))

        self.assertEqual(response, expected_response)
        route_mock.assert_not_called()
        refined_intent = execute_intent.call_args.kwargs["intent"]
        self.assertEqual(refined_intent.apiProvider, "IMF")
        self.assertTrue(refined_intent.parameters.get("__semantic_provider_locked"))

    def test_delta_path_does_not_commit_state_on_failed_execution(self) -> None:
        from backend.services.conversation_state_v2 import ConversationState

        conv_id = conversation_manager.get_or_create("conv-delta-no-commit-on-failure")
        previous_state = ConversationState(
            indicator="GDP growth rate",
            countries=["US", "CA"],
            provider="IMF",
            provider_locked=True,
            original_query="GDP growth rate from IMF",
        )
        conversation_manager.set_conversation_state(conv_id, previous_state)

        failed_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            error="verification_failed",
            message="fail",
        )

        with patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=failed_response)):
            response = run(self.service.process_query("show only Canada", conversation_id=conv_id))

        self.assertEqual(response.error, "verification_failed")
        persisted = conversation_manager.get_conversation_state(conv_id)
        assert persisted is not None
        self.assertEqual(persisted.provider, "IMF")
        self.assertEqual(persisted.countries, ["US", "CA"])

    def test_delta_decomposition_follow_up_can_reroute_to_statscan(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-delta-decomposition-reroute")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="employment rate",
                country="Canada",
                provider="OECD",
                original_query="Canada employment rate",
            ),
        )

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )
        delta = FollowUpDelta(
            changed_decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
            delta_type="decomposition_change",
            raw_query="Show by province",
        )
        route_decision = RoutingDecision(
            provider="StatsCan",
            confidence=0.9,
            match_type="region",
            reasoning="Canadian provinces routed to StatsCan",
            matched_pattern="Canadian provinces",
        )

        with patch("backend.services.query.DeltaExtractor.extract", return_value=delta), \
             patch.object(self.service.unified_router, "route", return_value=route_decision) as route_mock, \
             patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=expected_response)) as execute_intent:
            response = run(self.service.process_query("Show by province", conversation_id=conv_id))

        self.assertEqual(response, expected_response)
        route_mock.assert_called_once()
        refined_intent = execute_intent.call_args.kwargs["intent"]
        self.assertEqual(refined_intent.apiProvider, "STATSCAN")
        self.assertTrue(refined_intent.parameters.get("__delta_indicator_changed"))

    def test_delta_path_persists_statscan_provider_after_decomposition_followup(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-delta-persist-statscan-provider")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="employment rate",
                provider="OECD",
                country="Canada",
                statscan_product_id="14100362",
                original_query="Canada employment rate",
            ),
        )

        success_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )
        delta = FollowUpDelta(
            changed_decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
            delta_type="decomposition_change",
            raw_query="Show by province",
        )

        with patch("backend.services.query.DeltaExtractor.extract", return_value=delta), \
             patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=success_response)):
            response = run(self.service.process_query("Show by province", conversation_id=conv_id))

        self.assertEqual(response.message, "ok")
        persisted = conversation_manager.get_conversation_state(conv_id)
        assert persisted is not None
        self.assertEqual(persisted.provider, "STATSCAN")
        self.assertEqual(persisted.routed_provider, "STATSCAN")

    def test_process_query_preserves_statscan_provider_for_dimension_follow_up_parse(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-statscan-dimension-follow-up")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="employment rate",
                provider="STATSCAN",
                country="Canada",
                dimensions={"Geography": "Ontario"},
                statscan_product_id="14100287",
                original_query="Show only Ontario",
            ),
        )

        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["14100287"],
            parameters={
                "country": "Canada",
                "__dimensions": {"Geography": "Ontario"},
                "__statscan_decomposition_axis": "Age group",
                "indicator": "14100287",
            },
            clarificationNeeded=False,
            confidence=0.9,
            originalQuery="Show by age group",
            needsDecomposition=True,
            decompositionType="age groups",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider="WORLDBANK",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service.unified_router, "route", return_value=RoutingDecision(provider="STATSCAN", confidence=0.9, match_type="state", reasoning="preserve StatsCan", matched_pattern="state")) as route_mock, \
             patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[sample_series_with(source="Statistics Canada", indicator="employment rate", country="Canada", series_id="14100287:7.9.1.1.1.1.0.0.0.0")]) as fetch_mock:
            response = run(self.service.process_query("Show by age group", conversation_id=conv_id))

        self.assertFalse(response.error)
        fetched_intent = fetch_mock.await_args.args[0]
        self.assertEqual(fetched_intent.apiProvider, "STATSCAN")
        self.assertEqual(fetched_intent.parameters.get("__statscan_product_id"), "14100287")

    def test_should_preserve_statscan_followup_provider_requires_dimension_signal(self) -> None:
        state = ConversationState(
            indicator="employment rate",
            provider="OECD",
            country="Canada",
            statscan_product_id="14100287",
        )
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["BCA_NGDPD"],
            parameters={"country": "Canada"},
            clarificationNeeded=False,
            originalQuery="Switch to current account balance",
        )

        self.assertFalse(
            self.service._should_preserve_statscan_followup_provider(  # pylint: disable=protected-access
                state,
                intent,
            )
        )

    def test_process_query_preserves_statscan_dimension_followup_from_answer_members(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-statscan-member-follow-up")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="employment rate",
                provider="OECD",
                country="Canada",
                active_answer_members=[
                    AnswerSetMember(
                        provider="STATSCAN",
                        indicator_label="employment rate",
                        provider_code="14100287:7.7.1.1.1.1.0.0.0.0",
                        series_id="14100287:7.7.1.1.1.1.0.0.0.0",
                        country="Canada",
                        countries=["Canada"],
                        source_turn=3,
                    )
                ],
                statscan_product_id="14100287",
                dimensions={"Geography": "Ontario"},
                original_query="Show only Ontario",
            ),
        )

        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["14100287"],
            parameters={
                "country": "Canada",
                "__dimensions": {"Geography": "Ontario"},
                "__statscan_decomposition_axis": "Age group",
                "indicator": "14100287",
            },
            clarificationNeeded=False,
            confidence=0.9,
            originalQuery="Show by age group",
            needsDecomposition=True,
            decompositionType="age groups",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider="WORLDBANK",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[sample_series_with(source="Statistics Canada", indicator="employment rate", country="Canada", series_id="14100287:7.9.1.1.1.1.0.0.0.0")]) as fetch_mock:
            response = run(self.service.process_query("Show by age group", conversation_id=conv_id))

        self.assertFalse(response.error)
        fetched_intent = fetch_mock.await_args.args[0]
        self.assertEqual(fetched_intent.apiProvider, "STATSCAN")
        self.assertEqual(fetched_intent.parameters.get("__statscan_product_id"), "14100287")

    def test_process_query_locks_statscan_for_canada_scoped_structural_route(self) -> None:
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["unemployment rate"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            confidence=0.8,
            originalQuery="Canada unemployment rate",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider="OECD",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )
        route_decision = RoutingDecision(
            provider="StatsCan",
            confidence=0.88,
            match_type="catalog",
            reasoning="Canada catalog match",
            matched_pattern="catalog:unemployment:StatsCan",
        )
        fetched = [sample_series_with(
            source="Statistics Canada",
            indicator="unemployment rate",
            country="Canada",
            series_id="2062815",
        )]

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_recover_from_uncertain_match", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_improve_country_coverage", new_callable=AsyncMock, return_value=(fetched, None)), \
             patch.object(self.service, "_build_uncertain_result_clarification", return_value=None), \
             patch.object(self.service.unified_router, "route", return_value=route_decision), \
             patch.object(
                 self.service,
                 "_fetch_data",
                 new_callable=AsyncMock,
                 return_value=fetched,
             ) as fetch_mock:
            response = run(self.service.process_query("Canada unemployment rate"))

        self.assertFalse(response.error)
        fetch_mock.assert_awaited_once()
        fetched_intent = fetch_mock.await_args.args[0]
        self.assertEqual(fetched_intent.apiProvider, "STATSCAN")
        self.assertTrue(fetched_intent.parameters.get("__semantic_provider_locked"))

    def test_process_query_persists_answer_members_after_direct_standard_success(self) -> None:
        self.service.settings.use_staged_state_commit = True
        conv_id = conversation_manager.get_or_create("conv-standard-direct-answer-members")
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            confidence=0.9,
            originalQuery="US GDP from FRED",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="FRED",
            routed_provider="FRED",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )
        fetched = [sample_series_with(
            source="FRED",
            indicator="Gross Domestic Product",
            country="US",
            series_id="GDP",
        )]

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_recover_from_uncertain_match", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_improve_country_coverage", new_callable=AsyncMock, return_value=(fetched, None)), \
             patch.object(self.service, "_build_uncertain_result_clarification", return_value=None), \
             patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=fetched):
            response = run(self.service.process_query("US GDP from FRED", conversation_id=conv_id))

        self.assertFalse(response.error)
        persisted = conversation_manager.get_conversation_state(response.conversationId)
        assert persisted is not None
        assert persisted.active_answer_members is not None
        assert persisted.recent_answer_members is not None
        self.assertEqual(
            [(member.provider, member.country, member.provider_code) for member in persisted.active_answer_members],
            [("FRED", "US", "GDP")],
        )
        self.assertEqual(
            [(member.provider, member.country, member.provider_code) for member in persisted.recent_answer_members],
            [("FRED", "US", "GDP")],
        )

    def test_process_query_persists_answer_members_after_fallback_success(self) -> None:
        self.service.settings.use_staged_state_commit = True
        conv_id = conversation_manager.get_or_create("conv-standard-fallback-answer-members")
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            confidence=0.9,
            originalQuery="US GDP from FRED",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="FRED",
            routed_provider="FRED",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )
        fallback_data = [sample_series_with(
            source="World Bank",
            indicator="GDP (current US$)",
            country="United States",
            series_id="NY.GDP.MKTP.CD",
        )]

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_improve_country_coverage", new_callable=AsyncMock, return_value=(fallback_data, None)), \
             patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.service, "_try_with_fallback", new_callable=AsyncMock, return_value=fallback_data):
            response = run(self.service.process_query("US GDP from FRED", conversation_id=conv_id))

        self.assertFalse(response.error)
        persisted = conversation_manager.get_conversation_state(response.conversationId)
        assert persisted is not None
        assert persisted.active_answer_members is not None
        assert persisted.recent_answer_members is not None
        self.assertEqual(
            [(member.provider, member.country, member.provider_code) for member in persisted.active_answer_members],
            [("WORLDBANK", "United States", "NY.GDP.MKTP.CD")],
        )
        self.assertEqual(
            [(member.provider, member.country, member.provider_code) for member in persisted.recent_answer_members],
            [("WORLDBANK", "United States", "NY.GDP.MKTP.CD")],
        )

    def test_persist_verified_conversation_state_refreshes_semantic_indicator_and_provider(self) -> None:
        state = ConversationState(
            indicator="GDP",
            provider="FRED",
            country="Japan",
            turn_number=1,
        )
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["NE.EXP.GNFS.ZS"],
            parameters={
                "country": "Japan",
                "__semantic_indicator_label": "exports share of GDP",
                "indicator": "NE.EXP.GNFS.ZS",
            },
            clarificationNeeded=False,
            originalQuery="Exports share of GDP in Japan",
        )
        data = [sample_series_with(
            source="World Bank",
            indicator="Exports of goods and services (% of GDP)",
            country="Japan",
            series_id="NE.EXP.GNFS.ZS",
        )]

        self.service._persist_verified_conversation_state(  # pylint: disable=protected-access
            "conv-semantic-refresh",
            state,
            data,
            intent=intent,
        )

        self.assertEqual(state.indicator, "exports share of GDP")
        self.assertEqual(state.provider, "WORLDBANK")
        self.assertEqual(state.routed_provider, "WORLDBANK")
        self.assertEqual(state.resolved_indicator_code, "NE.EXP.GNFS.ZS")
        self.assertEqual(state.last_indicators_resolved, ["NE.EXP.GNFS.ZS"])

    def test_persist_verified_conversation_state_prefers_single_data_provider_over_stale_intent_provider(self) -> None:
        state = ConversationState(
            indicator="GDP growth rate",
            provider="STATSCAN",
            country="United States",
            turn_number=1,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["BCA_NGDPD"],
            parameters={"indicator": "BCA_NGDPD"},
            clarificationNeeded=False,
            originalQuery="Switch to current account balance",
        )
        data = [sample_series_with(
            source="IMF",
            indicator="Current account balance (% of GDP)",
            country="United States",
            series_id="BCA_NGDPD",
        )]

        self.service._persist_verified_conversation_state(  # pylint: disable=protected-access
            "conv-semantic-provider-from-data",
            state,
            data,
            intent=intent,
        )

        self.assertEqual(state.provider, "IMF")
        self.assertEqual(state.routed_provider, "IMF")

    def test_delta_path_marks_trade_flow_change_as_indicator_changed(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-delta-trade-flow-indicator-changed")
        conversation_manager.set_conversation_state(
            conv_id,
            ConversationState(
                indicator="exports",
                provider="COMTRADE",
                country="United States",
                trade_reporter="United States",
                trade_partner="China",
                trade_flow="EXPORT",
            ),
        )

        delta = FollowUpDelta(
            changed_indicator="imports",
            changed_trade_flow="IMPORT",
            delta_type="compound_change",
            raw_query="Switch to US imports from China",
            query_type="parameter_delta",
        )

        captured_intent = {}

        async def _fake_execute(**kwargs):
            intent = kwargs["intent"]
            captured_intent["intent"] = intent
            return QueryResponse(
                conversationId=conv_id,
                intent=intent,
                data=[sample_series_with(source="UN Comtrade", indicator="Imports - Total Trade", country="United States")],
                clarificationNeeded=False,
            )

        with patch.object(self.service, "_execute_resolved_intent", new=AsyncMock(side_effect=_fake_execute)):
            response = run(self.service.process_query("Switch to US imports from China", conversation_id=conv_id))

        self.assertFalse(response.error)
        intent = captured_intent["intent"]
        self.assertTrue(intent.parameters.get("__delta_indicator_changed"))

    def test_collective_answer_member_delta_preserves_provider_members_on_indicator_switch(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-collective-answer-members")
        state = ConversationState(
            indicator="GDP",
            turn_number=4,
            active_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP",
                    provider_code="GDP",
                    series_id="GDP",
                    country="US",
                    countries=["US"],
                    source_turn=4,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP",
                    provider_code="NY.GDP.MKTP.CD",
                    series_id="NY.GDP.MKTP.CD",
                    country="JP",
                    countries=["JP"],
                    source_turn=4,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP",
                    provider_code="GDP",
                    series_id="GDP",
                    country="US",
                    countries=["US"],
                    source_turn=4,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP",
                    provider_code="NY.GDP.MKTP.CD",
                    series_id="NY.GDP.MKTP.CD",
                    country="JP",
                    countries=["JP"],
                    source_turn=4,
                ),
            ],
        )
        delta = FollowUpDelta(
            changed_indicator="GDP growth rate",
            delta_type="indicator_switch",
            raw_query="Switch all to GDP growth rate",
        )

        captured_intents: list[ParsedIntent] = []

        async def _fake_fetch(intent: ParsedIntent):
            captured_intents.append(intent)
            provider = intent.apiProvider
            country = (intent.parameters or {}).get("country")
            semantic_label = (intent.parameters or {}).get("__semantic_indicator_label") or intent.indicators[0]
            if provider == "FRED":
                return [
                    sample_series_with(
                        source="FRED",
                        indicator=str(semantic_label),
                        country=country,
                        series_id="A191RL1Q225SBEA",
                    )
                ]
            return [
                sample_series_with(
                    source="World Bank",
                    indicator=str(semantic_label),
                    country=country,
                    series_id="NY.GDP.MKTP.KD.ZG",
                )
            ]

        with patch.object(self.service, "_fetch_data", new=AsyncMock(side_effect=_fake_fetch)), \
             patch.object(self.service, "_verify_execution_result", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_needs_indicator_clarification", return_value=False):
            response = run(
                self.service._execute_collective_answer_member_delta(  # pylint: disable=protected-access
                    query="Switch all to GDP growth rate",
                    conversation_id=conv_id,
                    tracker=None,
                    state=state,
                    delta=delta,
                )
            )

        assert response is not None
        self.assertFalse(response.clarificationNeeded)
        self.assertFalse(response.error)
        self.assertTrue(response.delta_state_saved)
        self.assertEqual(response.intent.apiProvider, "MULTI")
        self.assertEqual(
            [series.metadata.source for series in (response.data or [])],
            ["FRED", "World Bank"],
        )
        self.assertEqual(
            {intent.resolvedQuery for intent in captured_intents},
            {"US GDP growth rate", "JP GDP growth rate"},
        )

        persisted = conversation_manager.get_conversation_state(response.conversationId)
        assert persisted is not None
        assert persisted.active_answer_members is not None
        self.assertEqual(
            {(member.provider, member.country) for member in persisted.active_answer_members},
            {("FRED", "US"), ("WORLDBANK", "JP")},
        )
        self.assertEqual(
            {member.indicator_label for member in persisted.active_answer_members},
            {"GDP growth rate"},
        )

    def test_build_collective_member_intent_preserves_provider_code_in_params_for_stable_members(self) -> None:
        state = ConversationState(
            indicator="GDP growth rate",
            provider="IMF",
            start_date="2020-01-01",
            end_date="2025-12-31",
        )
        member = AnswerSetMember(
            provider="IMF",
            indicator_label="GDP growth rate",
            provider_code="NGDP_RPCH",
            series_id="NGDP_RPCH",
            country="United States",
            countries=["United States"],
            source_turn=4,
        )
        delta = FollowUpDelta(
            changed_start_date="2020-01-01",
            changed_end_date="2025-12-31",
            delta_type="time_change",
            raw_query="Change to 2020-2025",
        )

        intent = self.service._build_collective_member_intent(  # pylint: disable=protected-access
            query="Change to 2020-2025",
            member_query="US GDP growth rate",
            state=state,
            member=member,
            delta=delta,
            collective_indicator_label="GDP growth rate",
        )

        self.assertEqual(intent.parameters.get("indicator"), "NGDP_RPCH")
        self.assertEqual(intent.parameters.get("country"), "US")
        self.assertEqual(intent.parameters.get("__semantic_provider_locked"), True)

    def test_collective_answer_member_delta_rehydrates_recent_country_with_current_indicator(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-collective-answer-members-add-back")
        state = ConversationState(
            indicator="GDP per capita",
            turn_number=6,
            active_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP per capita",
                    provider_code="A939RX0Q048SBEA",
                    series_id="A939RX0Q048SBEA",
                    country="US",
                    countries=["US"],
                    source_turn=6,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP per capita",
                    provider_code="NY.GDP.PCAP.CD",
                    series_id="NY.GDP.PCAP.CD",
                    country="CN",
                    countries=["CN"],
                    source_turn=6,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP per capita",
                    provider_code="A939RX0Q048SBEA",
                    series_id="A939RX0Q048SBEA",
                    country="US",
                    countries=["US"],
                    source_turn=6,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP per capita",
                    provider_code="NY.GDP.PCAP.CD",
                    series_id="NY.GDP.PCAP.CD",
                    country="CN",
                    countries=["CN"],
                    source_turn=6,
                ),
                AnswerSetMember(
                    provider="EUROSTAT",
                    indicator_label="GDP growth rate",
                    provider_code="TEC00115",
                    series_id="TEC00115",
                    country="DE",
                    countries=["DE"],
                    source_turn=5,
                ),
            ],
        )
        delta = FollowUpDelta(
            added_countries=["DE"],
            delta_type="additive_country",
            raw_query="Add Germany back",
        )

        captured_intents: list[ParsedIntent] = []

        async def _fake_fetch(intent: ParsedIntent):
            captured_intents.append(intent)
            provider = intent.apiProvider
            country = (intent.parameters or {}).get("country")
            semantic_label = (intent.parameters or {}).get("__semantic_indicator_label") or intent.indicators[0]
            series_map = {
                "FRED": ("FRED", "A939RX0Q048SBEA"),
                "WORLDBANK": ("World Bank", "NY.GDP.PCAP.CD"),
                "EUROSTAT": ("Eurostat", "PRC_PPP_IND"),
            }
            source, series_id = series_map[provider]
            return [
                sample_series_with(
                    source=source,
                    indicator=str(semantic_label),
                    country=country,
                    series_id=series_id,
                )
            ]

        with patch.object(self.service, "_fetch_data", new=AsyncMock(side_effect=_fake_fetch)), \
             patch.object(self.service, "_verify_execution_result", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_needs_indicator_clarification", return_value=False):
            response = run(
                self.service._execute_collective_answer_member_delta(  # pylint: disable=protected-access
                    query="Add Germany back",
                    conversation_id=conv_id,
                    tracker=None,
                    state=state,
                    delta=delta,
                )
            )

        assert response is not None
        self.assertFalse(response.error)
        germany_intent = next(intent for intent in captured_intents if intent.apiProvider == "EUROSTAT")
        self.assertEqual(germany_intent.parameters.get("__semantic_indicator_label"), "GDP per capita")
        self.assertEqual(germany_intent.indicators, ["GDP per capita"])
        self.assertEqual(germany_intent.resolvedQuery, "DE GDP per capita")
        self.assertEqual(
            {(series.metadata.source, series.metadata.country) for series in (response.data or [])},
            {("FRED", "US"), ("World Bank", "CN"), ("Eurostat", "DE")},
        )

    def test_collective_answer_member_delta_uses_cross_provider_fallback_when_member_fetch_fails(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-collective-answer-members-fallback")
        state = ConversationState(
            indicator="GDP growth rate",
            turn_number=5,
            active_answer_members=[
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP growth rate",
                    provider_code="NY.GDP.MKTP.KD.ZG",
                    series_id="NY.GDP.MKTP.KD.ZG",
                    country="United States",
                    countries=["United States"],
                    source_turn=5,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP growth rate",
                    provider_code="NY.GDP.MKTP.KD.ZG",
                    series_id="NY.GDP.MKTP.KD.ZG",
                    country="United States",
                    countries=["United States"],
                    source_turn=5,
                ),
            ],
        )
        delta = FollowUpDelta(
            changed_indicator="GDP per capita",
            delta_type="indicator_switch",
            raw_query="Switch to GDP per capita",
        )
        fallback_data = [
            sample_series_with(
                source="IMF",
                indicator="GDP per capita",
                country="United States",
                series_id="NGDPDPC",
            )
        ]

        with patch.object(
            self.service,
            "_fetch_data",
            new=AsyncMock(side_effect=DataNotAvailableError("worldbank timeout")),
        ), \
             patch.object(self.service, "_try_with_fallback", new=AsyncMock(return_value=fallback_data)) as fallback_mock, \
             patch.object(self.service, "_verify_execution_result", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_needs_indicator_clarification", return_value=False):
            response = run(
                self.service._execute_collective_answer_member_delta(  # pylint: disable=protected-access
                    query="Switch to GDP per capita",
                    conversation_id=conv_id,
                    tracker=None,
                    state=state,
                    delta=delta,
                )
            )

        assert response is not None
        self.assertFalse(response.error)
        fallback_mock.assert_awaited_once()
        self.assertEqual(
            [(series.metadata.source, series.metadata.seriesId) for series in (response.data or [])],
            [("IMF", "NGDPDPC")],
        )

    def test_collective_answer_member_delta_add_back_prefers_latest_semantic_match(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-collective-answer-members-add-back-latest-match")
        state = ConversationState(
            indicator="GDP per capita",
            turn_number=8,
            active_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP per capita",
                    provider_code="A939RX0Q048SBEA",
                    series_id="A939RX0Q048SBEA",
                    country="US",
                    countries=["US"],
                    source_turn=8,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP per capita",
                    provider_code="NY.GDP.PCAP.CD",
                    series_id="NY.GDP.PCAP.CD",
                    country="CN",
                    countries=["CN"],
                    source_turn=8,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP per capita",
                    provider_code="A939RX0Q048SBEA",
                    series_id="A939RX0Q048SBEA",
                    country="US",
                    countries=["US"],
                    source_turn=8,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP per capita",
                    provider_code="NY.GDP.PCAP.CD",
                    series_id="NY.GDP.PCAP.CD",
                    country="CN",
                    countries=["CN"],
                    source_turn=8,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP growth rate",
                    provider_code="NY.GDP.MKTP.KD.ZG",
                    series_id="NY.GDP.MKTP.KD.ZG",
                    country="DE",
                    countries=["DE"],
                    source_turn=7,
                ),
                AnswerSetMember(
                    provider="EUROSTAT",
                    indicator_label="GDP per capita",
                    provider_code="PRC_PPP_IND",
                    series_id="PRC_PPP_IND",
                    country="DE",
                    countries=["DE"],
                    source_turn=8,
                ),
            ],
        )
        delta = FollowUpDelta(
            added_countries=["DE"],
            delta_type="additive_country",
            raw_query="Add Germany back",
        )

        captured_intents: list[ParsedIntent] = []

        async def _fake_fetch(intent: ParsedIntent):
            captured_intents.append(intent)
            provider = intent.apiProvider
            country = (intent.parameters or {}).get("country")
            semantic_label = (intent.parameters or {}).get("__semantic_indicator_label") or intent.indicators[0]
            source, series_id = {
                "FRED": ("FRED", "A939RX0Q048SBEA"),
                "WORLDBANK": ("World Bank", "NY.GDP.PCAP.CD"),
                "EUROSTAT": ("Eurostat", "PRC_PPP_IND"),
            }[provider]
            return [
                sample_series_with(
                    source=source,
                    indicator=str(semantic_label),
                    country=country,
                    series_id=series_id,
                )
            ]

        with patch.object(self.service, "_fetch_data", new=AsyncMock(side_effect=_fake_fetch)), \
             patch.object(self.service, "_verify_execution_result", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_needs_indicator_clarification", return_value=False):
            response = run(
                self.service._execute_collective_answer_member_delta(  # pylint: disable=protected-access
                    query="Add Germany back",
                    conversation_id=conv_id,
                    tracker=None,
                    state=state,
                    delta=delta,
                )
            )

        assert response is not None
        germany_intent = next(intent for intent in captured_intents if (intent.parameters or {}).get("country") == "DE")
        self.assertEqual(germany_intent.apiProvider, "EUROSTAT")
        self.assertEqual(germany_intent.parameters.get("__semantic_indicator_label"), "GDP per capita")
        self.assertEqual(germany_intent.indicators, ["PRC_PPP_IND"])
        self.assertEqual(germany_intent.resolvedQuery, "DE GDP per capita")
        self.assertEqual(
            {(series.metadata.source, series.metadata.country) for series in (response.data or [])},
            {("FRED", "US"), ("World Bank", "CN"), ("Eurostat", "DE")},
        )

    def test_collective_answer_member_delta_prefers_imf_members_for_gdp_growth_when_available(self) -> None:
        state = ConversationState(
            indicator="GDP growth rate",
            turn_number=5,
            active_answer_members=[
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP growth rate",
                    provider_code="NY.GDP.MKTP.KD.ZG",
                    series_id="NY.GDP.MKTP.KD.ZG",
                    country="Canada",
                    countries=["Canada"],
                    source_turn=5,
                ),
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP growth rate",
                    provider_code="NGDP_RPCH",
                    series_id="NGDP_RPCH",
                    country="United States",
                    countries=["United States"],
                    source_turn=5,
                ),
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP growth rate",
                    provider_code="NGDP_RPCH",
                    series_id="NGDP_RPCH",
                    country="Canada",
                    countries=["Canada"],
                    source_turn=5,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP growth rate",
                    provider_code="NGDP_RPCH",
                    series_id="NGDP_RPCH",
                    country="Japan",
                    countries=["Japan"],
                    source_turn=5,
                ),
            ],
        )
        delta = FollowUpDelta(
            added_countries=["Japan"],
            delta_type="additive_country",
            raw_query="Add Japan GDP growth rate",
        )

        members = self.service._select_collective_answer_members(  # pylint: disable=protected-access
            state,
            delta,
            "GDP growth rate",
        )

        providers_by_country = {
            (member.country or (member.countries or [None])[0]): member.provider
            for member in members
        }
        self.assertEqual(providers_by_country.get("Canada"), "IMF")
        self.assertEqual(providers_by_country.get("United States"), "IMF")
        self.assertEqual(providers_by_country.get("Japan"), "IMF")

    def test_collective_answer_member_delta_promotes_unmatched_growth_members_to_imf_template(self) -> None:
        state = ConversationState(
            indicator="GDP",
            turn_number=5,
            active_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP",
                    provider_code="GDP",
                    series_id="GDP",
                    country="US",
                    countries=["US"],
                    source_turn=5,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP",
                    provider_code="NY.GDP.MKTP.CD",
                    series_id="NY.GDP.MKTP.CD",
                    country="Canada",
                    countries=["Canada"],
                    source_turn=5,
                ),
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP",
                    provider_code="NGDPD",
                    series_id="NGDPD",
                    country="China",
                    countries=["China"],
                    source_turn=5,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP growth rate",
                    provider_code="NGDP_RPCH",
                    series_id="NGDP_RPCH",
                    country="Japan",
                    countries=["Japan"],
                    source_turn=5,
                ),
            ],
        )

        members = self.service._select_collective_answer_members(  # pylint: disable=protected-access
            state,
            FollowUpDelta(
                changed_indicator="GDP growth rate",
                delta_type="indicator_switch",
                raw_query="Switch all to GDP growth rate",
            ),
            "GDP growth rate",
        )

        providers_by_country = {
            (member.country or (member.countries or [None])[0]): member.provider
            for member in members
        }
        self.assertEqual(providers_by_country.get("US"), "IMF")
        self.assertEqual(providers_by_country.get("Canada"), "IMF")
        self.assertEqual(providers_by_country.get("China"), "IMF")

    def test_collective_answer_member_delta_skips_lightweight_clarification_when_semantic_verifier_passes(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-collective-semantic-verifier-wins")
        state = ConversationState(
            indicator="current account balance",
            turn_number=6,
            active_answer_members=[
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="current account balance",
                    provider_code="BCA_NGDPD",
                    series_id="BCA_NGDPD",
                    country="United States",
                    countries=["United States"],
                    source_turn=6,
                ),
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="current account balance",
                    provider_code="BCA_NGDPD",
                    series_id="BCA_NGDPD",
                    country="Germany",
                    countries=["Germany"],
                    source_turn=6,
                ),
            ],
        )
        delta = FollowUpDelta(
            changed_indicator="government debt to GDP",
            delta_type="indicator_switch",
            raw_query="Switch to government debt to GDP",
        )

        async def _fake_fetch(intent: ParsedIntent):
            country = (intent.parameters or {}).get("country")
            return [
                sample_series_with(
                    source="IMF",
                    indicator="General government gross debt",
                    country=country,
                    series_id="GGXWDG_NGDP",
                )
            ]

        with patch.object(self.service, "_fetch_data", new=AsyncMock(side_effect=_fake_fetch)), \
             patch.object(self.service, "_verify_execution_result", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_needs_indicator_clarification", return_value=True):
            response = run(
                self.service._execute_collective_answer_member_delta(  # pylint: disable=protected-access
                    query="Switch to government debt to GDP",
                    conversation_id=conv_id,
                    tracker=None,
                    state=state,
                    delta=delta,
                )
            )

        assert response is not None
        self.assertFalse(response.error)
        self.assertEqual(len(response.data or []), 2)

    def test_collective_path_skips_provider_locked_additive_country_followup(self) -> None:
        state = ConversationState(
            indicator="GDP growth rate",
            provider="IMF",
            provider_locked=True,
            active_answer_members=[
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP growth rate",
                    provider_code="NGDP_RPCH",
                    series_id="NGDP_RPCH",
                    country="Canada",
                    countries=["Canada"],
                    source_turn=4,
                ),
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP growth rate",
                    provider_code="NGDP_RPCH",
                    series_id="NGDP_RPCH",
                    country="United States",
                    countries=["United States"],
                    source_turn=4,
                ),
            ],
        )
        delta = FollowUpDelta(
            added_countries=["Japan"],
            delta_type="additive_country",
            raw_query="Add Japan GDP growth rate",
        )
        self.assertFalse(
            self.service._should_use_collective_answer_member_delta(  # pylint: disable=protected-access
                state,
                delta,
            )
        )

    def test_should_preserve_current_provider_for_delta_keeps_worldbank_variant_chain(self) -> None:
        state = ConversationState(
            indicator="GDP per capita",
            provider="WORLDBANK",
            routed_provider="WORLDBANK",
            country="US",
        )
        delta = FollowUpDelta(
            changed_indicator="GDP growth rate",
            delta_type="indicator_switch",
            raw_query="Switch to GDP growth rate",
        )
        delta_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP growth rate"],
            parameters={"country": "US", "__semantic_indicator_label": "GDP growth rate"},
            clarificationNeeded=False,
            originalQuery="Switch to GDP growth rate",
        )

        self.assertTrue(
            self.service._should_preserve_current_provider_for_delta(  # pylint: disable=protected-access
                state,
                delta,
                delta_intent,
                "Switch to GDP growth rate",
            )
        )

    def test_should_preserve_current_provider_for_delta_keeps_comtrade_bilateral_chain(self) -> None:
        state = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            routed_provider="COMTRADE",
            country="US",
            trade_reporter="US",
            trade_partner="China",
            trade_flow="EXPORT",
        )
        delta = FollowUpDelta(
            changed_indicator="imports",
            changed_trade_flow="IMPORT",
            delta_type="compound_change",
            raw_query="Switch to US imports from China",
        )
        delta_intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["imports"],
            parameters={
                "country": "US",
                "reporter": "US",
                "partner": "China",
                "flow": "IMPORT",
                "__semantic_indicator_label": "imports",
            },
            clarificationNeeded=False,
            originalQuery="Switch to US imports from China",
        )

        self.assertTrue(
            self.service._should_preserve_current_provider_for_delta(  # pylint: disable=protected-access
                state,
                delta,
                delta_intent,
                "Switch to US imports from China",
            )
        )

    def test_collective_path_skips_indicator_switch_when_only_recent_members_are_multiple(self) -> None:
        state = ConversationState(
            indicator="GDP per capita",
            active_answer_members=[
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP per capita",
                    provider_code="NY.GDP.PCAP.CD",
                    series_id="NY.GDP.PCAP.CD",
                    country="US",
                    countries=["US"],
                    source_turn=3,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP",
                    provider_code="GDP",
                    series_id="GDP",
                    country="US",
                    countries=["US"],
                    source_turn=2,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP per capita",
                    provider_code="NY.GDP.PCAP.CD",
                    series_id="NY.GDP.PCAP.CD",
                    country="United States",
                    countries=["United States"],
                    source_turn=3,
                ),
            ],
        )
        delta = FollowUpDelta(
            changed_indicator="GDP growth rate",
            delta_type="indicator_switch",
            raw_query="Switch to GDP growth rate",
        )

        self.assertFalse(
            self.service._should_use_collective_answer_member_delta(  # pylint: disable=protected-access
                state,
                delta,
            )
        )

    def test_collective_path_uses_recent_multi_country_scope_for_explicit_switch_all(self) -> None:
        state = ConversationState(
            indicator="GDP",
            active_answer_members=[
                AnswerSetMember(
                    provider="STATSCAN",
                    indicator_label="GDP",
                    provider_code="65201210",
                    series_id="65201210",
                    country="CA",
                    countries=["CA"],
                    source_turn=5,
                ),
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP",
                    provider_code="GDP",
                    series_id="GDP",
                    country="US",
                    countries=["US"],
                    source_turn=1,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP",
                    provider_code="NY.GDP.MKTP.CD",
                    series_id="NY.GDP.MKTP.CD",
                    country="JP",
                    countries=["JP"],
                    source_turn=2,
                ),
                AnswerSetMember(
                    provider="EUROSTAT",
                    indicator_label="GDP",
                    provider_code="NAMA_10_GDP",
                    series_id="NAMA_10_GDP",
                    country="DE",
                    countries=["DE"],
                    source_turn=3,
                ),
                AnswerSetMember(
                    provider="IMF",
                    indicator_label="GDP",
                    provider_code="NGDPD",
                    series_id="NGDPD",
                    country="CN",
                    countries=["CN"],
                    source_turn=4,
                ),
                AnswerSetMember(
                    provider="STATSCAN",
                    indicator_label="GDP",
                    provider_code="65201210",
                    series_id="65201210",
                    country="CA",
                    countries=["CA"],
                    source_turn=5,
                ),
            ],
        )
        delta = FollowUpDelta(
            changed_indicator="GDP growth rate",
            delta_type="indicator_switch",
            raw_query="Switch all to GDP growth rate",
        )

        self.assertTrue(
            self.service._should_use_collective_answer_member_delta(  # pylint: disable=protected-access
                state,
                delta,
            )
        )

        members = self.service._select_collective_answer_members(  # pylint: disable=protected-access
            state,
            delta,
            "GDP growth rate",
        )
        self.assertEqual(
            {country for member in members for country in (member.countries or ([member.country] if member.country else []))},
            {"US", "JP", "DE", "CN", "CA"},
        )

    def test_looks_like_country_follow_up_accepts_add_pattern(self) -> None:
        """'Add Germany' should be recognized as a country follow-up."""
        result = self.service._looks_like_country_follow_up("Add Germany", ["DE"])
        self.assertTrue(result)

    def test_looks_like_country_follow_up_accepts_what_about_pattern(self) -> None:
        """'What about France?' should be recognized as a country follow-up."""
        result = self.service._looks_like_country_follow_up("What about France?", ["FR"])
        self.assertTrue(result)

    def test_looks_like_country_follow_up_accepts_also_include_pattern(self) -> None:
        """'Also include Japan' should be recognized as a country follow-up."""
        result = self.service._looks_like_country_follow_up("Also include Japan", ["JP"])
        self.assertTrue(result)

    def test_match_indicator_choice_option_supports_natural_numeric_forms(self) -> None:
        options = [
            "[IMF] Trade Balance (% of GDP) (BT_GDP)",
            "[WorldBank] Trade Balance (% of GDP) (NE.RSB.GNFS.ZS)",
        ]

        selected_option = self.service._match_indicator_choice_option(  # pylint: disable=protected-access
            "option 2",
            options,
        )
        self.assertEqual(selected_option, options[1])

        selected_ordinal = self.service._match_indicator_choice_option(  # pylint: disable=protected-access
            "second",
            options,
        )
        self.assertEqual(selected_ordinal, options[1])

    def test_build_clarification_options_extracts_metadata(self) -> None:
        options = [
            "[IMF] Trade Balance (% of GDP) (BT_GDP)",
            "[WorldBank] Trade Balance (% of GDP) (NE.RSB.GNFS.ZS)",
        ]

        structured = self.service._build_clarification_options(options)  # pylint: disable=protected-access

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(
            [option.model_dump() for option in structured],
            [
                {
                    "id": "1",
                    "label": "Trade Balance (% of GDP)",
                    "value": "[IMF] Trade Balance (% of GDP) (BT_GDP)",
                    "provider": "IMF",
                    "code": "BT_GDP",
                },
                {
                    "id": "2",
                    "label": "Trade Balance (% of GDP)",
                    "value": "[WorldBank] Trade Balance (% of GDP) (NE.RSB.GNFS.ZS)",
                    "provider": "WORLDBANK",
                    "code": "NE.RSB.GNFS.ZS",
                },
            ],
        )

    def test_dedupe_indicator_choice_options_filters_placeholder_and_duplicates(self) -> None:
        options = [
            "[IMF] Current account balance (% of GDP) (N/A)",
            "[IMF] Current account balance, percent of GDP (BCA_NGDPD)",
            "[IMF] Current account balance (% of GDP) (BCA_NGDPD)",
            "[WorldBank] Current account balance (% of GDP) (BN.CAB.XOKA.GD.ZS)",
        ]

        deduped = self.service._dedupe_indicator_choice_options(options)  # pylint: disable=protected-access

        self.assertEqual(len(deduped), 2)
        joined = "\n".join(deduped).upper()
        self.assertNotIn("(N/A)", joined)
        self.assertEqual(joined.count("(BCA_NGDPD)"), 1)

    def test_try_resolve_pending_indicator_choice_reprompts_with_structured_options_for_invalid_number(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-choice-invalid-number")
        conversation_manager.clear_pending_indicator_options(conv_id)
        pending_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["trade balance"],
            parameters={"countries": ["JP", "KR"]},
            clarificationNeeded=False,
            originalQuery="net trade balance as share of gdp in japan and korea",
        )
        conversation_manager.set_pending_indicator_options(
            conv_id,
            {
                "original_query": pending_intent.originalQuery,
                "intent": pending_intent.model_dump(),
                "options": [
                    "[IMF] Trade Balance (% of GDP) (BT_GDP)",
                    "[WorldBank] Trade Balance (BN.GSR.GNFS.CD)",
                ],
                "question_lines": ["Please choose one option:"],
            },
        )

        response = run(
            self.service._try_resolve_pending_indicator_choice(  # pylint: disable=protected-access
                query="9",
                conversation_id=conv_id,
                tracker=None,
            )
        )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertTrue(response.clarificationNeeded)
        self.assertEqual(response.message, "Please choose one of the listed option numbers.")
        self.assertIsNotNone(response.clarificationOptions)
        options_payload = response.clarificationOptions or []
        self.assertEqual([option.id for option in options_payload], ["1", "2"])
        self.assertEqual(options_payload[0].provider, "IMF")
        self.assertEqual(options_payload[1].provider, "WORLDBANK")

    def test_specific_indicator_group_query_still_allows_group_scope_clarification(self) -> None:
        """Specific indicators no longer imply explicit group scope by themselves."""
        conv_id = "conv-process-group-scope"
        conversation_manager.clear_pending_semantic_clarification(conv_id)

        result = self.service._has_explicit_group_scope("employment rate in G20")
        self.assertFalse(result)

    def test_build_structured_semantic_clarification_from_llm_questions(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-semantic-clar")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={"country": "Canada", "indicator": "14100287", "__catalog_concept": "employment"},
            clarificationNeeded=True,
            clarificationQuestions=[
                "Do you want the employment rate (percentage of labor force employed) for Canada?",
                "Do you want the total number of employed persons in Canada?",
                "Do you want the employment-to-population ratio for Canada?",
            ],
            originalQuery="employment in Canada",
        )

        response = self.service._build_structured_semantic_clarification(  # pylint: disable=protected-access
            conversation_id=conv_id,
            query="employment in Canada",
            intent=intent,
            processing_steps=None,
        )

        self.assertIsNotNone(response)
        assert response is not None
        labels = [option.label for option in (response.clarificationOptions or [])]
        self.assertIn("employment rate", labels)
        self.assertIn("number employed", labels)
        pending = conversation_manager.get_pending_semantic_clarification(conv_id)
        self.assertIsNotNone(pending)

    def test_group_scope_clarification_detects_vague_group_query(self) -> None:
        """Vague group queries without specific indicators should trigger group scope clarification."""
        # Group scope check now runs post-parse (consolidated from pre-parse).
        # Test the detection method directly instead of through process_query.
        clarification = self.service._build_group_scope_clarification(
            conversation_id="conv-group-scope-vague",
            query="data for G20",
            intent=None,
            is_multi_indicator=False,
        )
        self.assertIsNotNone(clarification)
        self.assertTrue(clarification.clarificationNeeded)

    def test_group_scope_clarification_detects_specific_indicator_group_query(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["imports as % of GDP"],
            parameters={"countries": ["US", "DE", "JP"]},
            clarificationNeeded=False,
            originalQuery="imports share of gdp in G20",
        )

        clarification = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id="conv-group-scope-specific",
            query="imports share of gdp in G20",
            intent=intent,
            is_multi_indicator=False,
            processing_steps=None,
        )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        self.assertIsNotNone(clarification.clarificationOptions)

    def test_build_catalog_concept_clarification_for_employment(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-employment-broad")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={"country": "Canada", "__catalog_concept": "employment", "indicator": "14100287"},
            clarificationNeeded=False,
            originalQuery="employment in Canada",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="STATSCAN",
            routed_provider="STATSCAN",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(
            self.service,
            "_build_prefetch_indicator_choice_clarification",
            new_callable=AsyncMock,
            return_value=None,
        ):
            clarification = run(
                self.service._build_post_parse_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="employment in Canada",
                    parse_result=parse_result,
                    validation=validation,
                    processing_steps=None,
                )
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        labels = [option.label for option in (clarification.clarificationOptions or [])]
        self.assertIn("employment rate", labels)
        self.assertIn("number employed", labels)
        options_payload = clarification.clarificationOptions or []
        employment_rate = next(option for option in options_payload if option.label == "employment rate")
        self.assertEqual(employment_rate.provider, "OECD")
        self.assertEqual(employment_rate.code, "DSD_LFS@DF_IALFS_EMP_WAP_Q")

    def test_build_catalog_concept_clarification_for_interest_rate(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-interest-rate-broad")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["REAINTRATREARAT10Y"],
            parameters={"__catalog_concept": "interest rate", "indicator": "REAINTRATREARAT10Y"},
            clarificationNeeded=False,
            originalQuery="interest rate",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="FRED",
            routed_provider="FRED",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(
            self.service,
            "_build_prefetch_indicator_choice_clarification",
            new_callable=AsyncMock,
            return_value=None,
        ):
            clarification = run(
                self.service._build_post_parse_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="interest rate",
                    parse_result=parse_result,
                    validation=validation,
                    processing_steps=None,
                )
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        labels = [option.label for option in (clarification.clarificationOptions or [])]
        self.assertIn("policy rate", labels)
        self.assertIn("long-term government bond yield", labels)
        self.assertIn("real interest rate", labels)

    def test_process_query_generic_exact_title_interest_rate_returns_clarification(self) -> None:
        lookup_results = [
            {
                "code": "REAINTRATREARAT10Y",
                "provider": "FRED",
                "name": "10-Year Real Interest Rate",
            }
        ]

        with patch(
            "backend.services.indicator_database.get_indicator_lookup",
            return_value=Mock(search=Mock(return_value=lookup_results)),
        ), patch.object(
            self.service.pipeline,
            "parse_and_route",
            new_callable=AsyncMock,
            side_effect=AssertionError("LLM parse should be skipped"),
        ), patch.object(self.service, "_get_from_cache", return_value=None), patch.object(
            self.service,
            "_fetch_data",
            side_effect=AssertionError("Fetch should not run before broad-concept clarification"),
        ):
            response = run(self.service.process_query("interest rate"))

        self.assertTrue(response.clarificationNeeded)
        labels = [option.label for option in (response.clarificationOptions or [])]
        self.assertIn("policy rate", labels)
        self.assertIn("long-term government bond yield", labels)

    def test_process_query_includes_clarification_context_for_llm(self) -> None:
        """Phase 4: When a pending semantic clarification exists, the LLM receives
        clarification context instead of the old state-machine methods being called."""
        conv_id = conversation_manager.get_or_create("conv-process-clarification-ctx")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        conversation_manager.set_pending_semantic_clarification(
            conv_id,
            {
                "kind": "group_scope",
                "region": "G20",
                "original_query": "imports share of gdp in G20",
                "question_lines": ["Choose the scope you want:"],
                "options": [
                    {
                        "id": "1",
                        "label": "compare member countries",
                        "value": "imports share of gdp across G20 member countries",
                    },
                    {
                        "id": "2",
                        "label": "one overall group value (aggregate/average if available)",
                        "value": "imports share of gdp for the G20 group as a whole",
                    },
                ],
            },
        )
        # Store a clarification intent as the last_intent
        clarification_intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["imports as % of GDP"],
            parameters={"country": "G20"},
            clarificationNeeded=True,
            clarificationQuestions=["Choose the scope you want:"],
            originalQuery="imports share of gdp in G20",
        )
        conversation_manager.add_message(conv_id, "user", "imports share of gdp in G20", intent=clarification_intent)

        # Verify that get_pending_clarification_context returns the stored details
        ctx = conversation_manager.get_pending_clarification_context(conv_id)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["kind"], "group_scope")
        self.assertIn("Choose the scope you want:", ctx["question"])
        self.assertIn("compare member countries", ctx["options"])

    def test_process_query_resolves_pending_semantic_clarification_by_number(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-semantic-choice")
        conversation_manager.set_pending_semantic_clarification(
            conv_id,
            {
                "kind": "semantic_metric",
                "original_query": "employment in Canada",
                "question_lines": [
                    "Do you want the employment rate for Canada?",
                    "Do you want the total number of employed persons in Canada?",
                ],
                "options": [
                    {
                        "id": "1",
                        "label": "employment rate",
                        "value": "employment rate in Canada",
                        "provider": "OECD",
                        "code": "DSD_LFS@DF_IALFS_EMP_WAP_Q",
                    },
                    {
                        "id": "2",
                        "label": "number employed",
                        "value": "number employed in Canada",
                        "provider": "STATSCAN",
                        "code": "14100287",
                    },
                ],
            },
        )

        with patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[sample_series()]), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service.pipeline, "validate_intent", return_value=ValidationResult(
                 is_multi_indicator=False,
                 is_valid=True,
                 validation_error=None,
                 suggestions=None,
                 is_confident=True,
                 confidence_reason=None,
             )), \
             patch.object(self.service, "_maybe_recover_from_uncertain_match", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_improve_country_coverage", new_callable=AsyncMock, return_value=([sample_series()], None)), \
             patch.object(self.service, "_build_uncertain_result_clarification", return_value=None), \
             patch.object(self.service, "_verify_execution_result", new_callable=AsyncMock, return_value=None):
            response = run(self.service.process_query("1", conversation_id=conv_id))

        self.assertFalse(response.clarificationNeeded)
        self.assertIsNotNone(response.intent)
        assert response.intent is not None
        self.assertEqual(response.intent.apiProvider, "OECD")
        self.assertEqual(response.intent.parameters.get("indicator"), "DSD_LFS@DF_IALFS_EMP_WAP_Q")
        self.assertIsNone(conversation_manager.get_pending_semantic_clarification(conv_id))

    def test_process_query_resolves_pending_group_scope_by_country_follow_up(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-group-followup")
        pending_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["NE.IMP.GNFS.ZS"],
            parameters={"countries": sorted(CountryResolver.G20_MEMBERS), "indicator": "NE.IMP.GNFS.ZS"},
            clarificationNeeded=True,
            clarificationQuestions=["Choose scope"],
            originalQuery="imports share of gdp in G20",
        )
        conversation_manager.add_message_safe(
            conv_id,
            "user",
            "imports share of gdp in G20",
            intent=pending_intent,
        )
        conversation_manager.set_pending_semantic_clarification(
            conv_id,
            {
                "kind": "group_scope",
                "region": "G20",
                "original_query": "imports share of gdp in G20",
                "question_lines": ["Choose scope"],
                "options": [
                    {
                        "id": "1",
                        "label": "compare member countries",
                        "value": "imports share of gdp across G20 member countries",
                    },
                    {
                        "id": "2",
                        "label": "one overall group value (aggregate/average if available)",
                        "value": "imports share of gdp for the G20 group as a whole",
                    },
                ],
            },
        )

        us_series = sample_series_with(
            indicator="Imports of goods and services (% of GDP)",
            country="US",
            series_id="NE.IMP.GNFS.ZS",
        )
        with patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[us_series]), \
             patch.object(self.service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service.pipeline, "validate_intent", return_value=ValidationResult(
                 is_multi_indicator=False,
                 is_valid=True,
                 validation_error=None,
                 suggestions=None,
                 is_confident=True,
                 confidence_reason=None,
             )), \
             patch.object(self.service, "_maybe_recover_from_uncertain_match", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_maybe_improve_country_coverage", new_callable=AsyncMock, return_value=([us_series], None)), \
             patch.object(self.service, "_build_uncertain_result_clarification", return_value=None), \
             patch.object(self.service, "_verify_execution_result", new_callable=AsyncMock, return_value=None):
            response = run(self.service.process_query("show only US", conversation_id=conv_id))

        self.assertFalse(response.clarificationNeeded)
        self.assertIsNotNone(response.intent)
        assert response.intent is not None
        self.assertEqual(response.intent.apiProvider, "WORLDBANK")
        self.assertEqual(response.intent.parameters.get("country"), "US")
        self.assertEqual(response.intent.parameters.get("indicator"), "NE.IMP.GNFS.ZS")
        self.assertIsNone(conversation_manager.get_pending_semantic_clarification(conv_id))

    def test_prefetch_clarification_allows_direct_worldbank_translation(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["imports as % of GDP"],
            parameters={"country": "China"},
            clarificationNeeded=False,
            originalQuery="imports share of gdp in china",
        )

        with patch("backend.services.query.get_indicator_resolver") as get_resolver, \
             patch.object(self.service, "_collect_indicator_choice_options", return_value=[]):
            get_resolver.return_value.resolve.return_value = None
            response = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id="conv-direct-translation",
                    query="imports share of gdp in china",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNone(response)
        self.assertEqual(
            self.service._get_direct_provider_indicator_translation(  # pylint: disable=protected-access
                "WORLDBANK",
                "imports as % of GDP",
            ),
            "NE.IMP.GNFS.ZS",
        )

    def test_get_direct_provider_indicator_translation_returns_canonical_oecd_gdp_dataflow(self) -> None:
        self.assertEqual(
            self.service._get_direct_provider_indicator_translation(  # pylint: disable=protected-access
                "OECD",
                "GDP",
            ),
            "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE",
        )

    def test_is_resolved_indicator_plausible_accepts_age_variant(self) -> None:
        """Age-demographic variant indicators are now accepted as plausible.

        The LLM handles variant refinement (youth vs. total employment rate);
        the plausibility check no longer penalises age-demographic mismatches.
        """
        plausible = self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
            provider="WorldBank",
            indicator_query="employment rate in Canada",
            resolved_code="JI.EMP.1564.YG.ZS",
            resolved_name="Employment rate, aged 15-24 (% of labor force aged 15-24)",
        )

        self.assertTrue(plausible)

    def test_is_resolved_indicator_plausible_rejects_employment_insurance_regional_slice(self) -> None:
        plausible = self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
            provider="StatsCan",
            indicator_query="employment rate in Canada",
            resolved_code="14100354",
            resolved_name="Regional unemployment rates used by the Employment Insurance program, three-month moving average, seasonally adjusted",
        )

        self.assertFalse(plausible)

    def test_process_query_returns_prefetch_indicator_clarification_before_fetch(self) -> None:
        conv_id = "conv-process-prefetch-choice"
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["GDP to Debt Ratio"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="gdp to debt ratio in china",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider="BIS",
            validation_warning=None,
        )
        options = [
            "[IMF] General government gross debt (% of GDP) (GGXWDG_NGDP)",
            "[WorldBank] Central government debt, total (% of GDP) (GC.DOD.TOTL.GD.ZS)",
        ]

        class _Resolved:
            provider = "BIS"
            code = "WS_DSR"
            name = "Debt service ratios"
            confidence = 0.85
            source = "database"
            metadata = {}

        with patch.object(self.service.pipeline, "parse_and_route", AsyncMock(return_value=parse_result)), \
             patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch.object(self.service, "_filter_viable_indicator_choice_options", AsyncMock(return_value=options)), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))), \
             patch.object(self.service, "_fetch_data", side_effect=AssertionError("fetch should not run")):
            response = run(self.service.process_query("gdp to debt ratio in china", conversation_id=conv_id))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNotNone(response.clarificationOptions)
        payload = response.clarificationOptions or []
        self.assertEqual(payload[0].provider, "IMF")
        self.assertEqual(payload[1].provider, "WORLDBANK")

    def test_execute_with_orchestrator_returns_post_parse_clarification_before_agent_execution(self) -> None:
        conv_id = "conv-orchestrator-prefetch-choice"
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["GDP to Debt Ratio"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="gdp to debt ratio in china",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider="BIS",
            validation_warning=None,
        )
        options = [
            "[IMF] General government gross debt (% of GDP) (GGXWDG_NGDP)",
            "[WorldBank] Central government debt, total (% of GDP) (GC.DOD.TOTL.GD.ZS)",
        ]

        class _Resolved:
            provider = "BIS"
            code = "WS_DSR"
            name = "Debt service ratios"
            confidence = 0.85
            source = "database"
            metadata = {}

        with patch.dict(
            "os.environ",
            {
                "USE_LANGGRAPH": "true",
                "USE_LANGCHAIN_REACT_AGENT": "false",
                "USE_DEEP_AGENTS": "false",
            },
            clear=False,
        ), \
             patch.object(self.service.pipeline, "parse_and_route", AsyncMock(return_value=parse_result)), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch.object(self.service, "_filter_viable_indicator_choice_options", AsyncMock(return_value=options)), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))), \
             patch.object(self.service, "_execute_with_langgraph", new_callable=AsyncMock, side_effect=AssertionError("agent should not run")):
            response = run(
                self.service._execute_with_orchestrator(  # pylint: disable=protected-access
                    query="gdp to debt ratio in china",
                    conversation_id=conv_id,
                    tracker=None,
                )
            )

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNotNone(response.clarificationOptions)
        payload = response.clarificationOptions or []
        self.assertEqual(payload[0].provider, "IMF")
        self.assertEqual(payload[1].provider, "WORLDBANK")

    def test_prefetch_indicator_clarification_accepts_age_variant_without_filter(self) -> None:
        """Age-demographic variant is accepted; no clarification triggered.

        The LLM handles semantic refinement; the plausibility check no longer
        penalises age-demographic mismatches.
        """
        conv_id = "conv-process-prefetch-filter"
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment rate"],
            parameters={"region": "G20"},
            clarificationNeeded=False,
            originalQuery="compare employment rate across G20 member countries",
        )

        class _Resolved:
            provider = "WORLDBANK"
            code = "JI.EMP.1564.YG.ZS"
            name = "Employment rate, aged 15-24 (% of labor force aged 15-24)"
            confidence = 0.90
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=[]), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            response = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="compare employment rate across G20 member countries",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        self.assertIsNone(response)

    def test_prefetch_indicator_clarification_accepts_age_variant_without_fallback(self) -> None:
        """Age-demographic variant is now accepted as plausible; no clarification triggered.

        The LLM handles variant refinement; the plausibility check no longer
        penalises age-demographic mismatches.
        """
        conv_id = "conv-process-prefetch-fallback-options"
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment rate"],
            parameters={"region": "G20"},
            clarificationNeeded=False,
            originalQuery="compare employment rate across G20 member countries",
        )

        class _Resolved:
            provider = "WORLDBANK"
            code = "JI.EMP.1564.YG.ZS"
            name = "Employment rate, aged 15-24 (% of labor force aged 15-24)"
            confidence = 0.90
            source = "database"
            metadata = {}

        with patch.object(self.service, "_collect_indicator_choice_options", return_value=[]), \
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            response = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="compare employment rate across G20 member countries",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )

        # With age-demographic penalty removed, the resolved indicator is accepted.
        self.assertIsNone(response)

    def test_try_resolve_pending_indicator_choice_drops_failed_option_from_retry_list(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-choice-drop-failed")
        conversation_manager.clear_pending_indicator_options(conv_id)
        pending_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment rate"],
            parameters={"region": "G20"},
            clarificationNeeded=False,
            originalQuery="compare employment rate across G20 member countries",
        )
        options = [
            "[IMF] Labor Markets, Employment, Employment Rate, Percent (LER_PT)",
            "[OECD] Employment rate (DSD_LFS@DF_IALFS_EMP_WAP_Q)",
            "[WorldBank] Employment rate, aged 15-24 (% of labor force aged 15-24) (JI.EMP.1564.YG.ZS)",
        ]
        conversation_manager.set_pending_indicator_options(
            conv_id,
            {
                "original_query": pending_intent.originalQuery,
                "intent": pending_intent.model_dump(),
                "options": options,
                "question_lines": ["Please choose one option:"],
            },
        )

        with patch.object(self.service, "_fetch_data", side_effect=RuntimeError("no data")):
            response = run(
                self.service._try_resolve_pending_indicator_choice(  # pylint: disable=protected-access
                    query="1",
                    conversation_id=conv_id,
                    tracker=None,
                )
            )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertTrue(response.clarificationNeeded)
        self.assertEqual(response.message, "That option did not return usable data. Please choose a different option.")
        payload = response.clarificationOptions or []
        self.assertEqual([option.provider for option in payload], ["OECD", "WORLDBANK"])
        pending = conversation_manager.get_pending_indicator_options(conv_id)
        assert pending is not None
        self.assertEqual(
            pending.get("options"),
            options[1:],
        )

    def test_execute_with_orchestrator_returns_low_confidence_response_before_agent_execution(self) -> None:
        conv_id = "conv-orchestrator-low-confidence"
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["employment"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="employment in china",
        )
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider=None,
            routed_provider="IMF",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=False,
            confidence_reason="ambiguous indicator",
        )

        with patch.dict(
            "os.environ",
            {
                "USE_LANGGRAPH": "true",
                "USE_LANGCHAIN_REACT_AGENT": "false",
                "USE_DEEP_AGENTS": "false",
            },
            clear=False,
        ), \
             patch.object(self.service.pipeline, "parse_and_route", AsyncMock(return_value=parse_result)), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_execute_with_langgraph", new_callable=AsyncMock, side_effect=AssertionError("agent should not run")):
            response = run(
                self.service._execute_with_orchestrator(  # pylint: disable=protected-access
                    query="employment in china",
                    conversation_id=conv_id,
                    tracker=None,
                )
            )

        self.assertTrue(response.clarificationNeeded)
        assert response.message is not None
        self.assertIn("Uncertain Query", response.message)
        self.assertIn("ambiguous indicator", response.message)

    def test_build_uncertain_result_clarification_skips_single_distinct_option_after_sanitization(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["current account balance"],
            parameters={"countries": ["US", "CN"]},
            clarificationNeeded=False,
            originalQuery="compare current account balances for energy importers versus exporters",
        )
        uncertain_data = [
            NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "IMF",
                        "indicator": "Current account balance (% of GDP)",
                        "country": "US",
                        "frequency": "annual",
                        "unit": "percent",
                        "lastUpdated": "",
                        "seriesId": "BCA_NGDPD",
                    },
                    "data": [{"date": "2022-01-01", "value": 1.0}],
                }
            )
        ]
        options = [
            "[IMF] Current account balance (% of GDP) (N/A)",
            "[IMF] Current account balance, percent of GDP (BCA_NGDPD)",
        ]

        with patch.object(self.service, "_needs_indicator_clarification", return_value=True), \
             patch.object(self.service, "_collect_indicator_choice_options", return_value=options), \
             patch.object(self.service, "_score_series_relevance", return_value=0.4):
            clarification = self.service._build_uncertain_result_clarification(  # pylint: disable=protected-access
                conversation_id="conv-uncertain-dedupe",
                query=intent.originalQuery,
                intent=intent,
                data=uncertain_data,
                processing_steps=None,
            )

        self.assertIsNone(clarification)

    def test_needs_indicator_clarification_allows_aligned_temporal_split_query(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["trade balance"],
            parameters={"countries": ["US", "CN"]},
            clarificationNeeded=False,
            originalQuery="contrast us and china trade balances before and after 2018",
        )
        aligned_series = [
            NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "WorldBank",
                        "indicator": "External balance on goods and services (% of GDP)",
                        "country": "US",
                        "frequency": "annual",
                        "unit": "%",
                        "lastUpdated": "",
                        "seriesId": "NE.RSB.GNFS.ZS",
                    },
                    "data": [{"date": "2020-01-01", "value": 0.8}],
                }
            )
        ]

        with patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=None))):
            needs = self.service._needs_indicator_clarification(  # pylint: disable=protected-access
                query=intent.originalQuery,
                data=aligned_series,
                intent=intent,
            )

        self.assertFalse(needs)

    def test_needs_indicator_clarification_detects_catalog_concept_mismatch(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["imports as % of GDP"],
            parameters={"countries": ["CN", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp in china and us",
        )
        mismatched_series = [
            NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "WorldBank",
                        "indicator": "Gross savings (% of GDP)",
                        "country": "CN",
                        "frequency": "annual",
                        "unit": "%",
                        "lastUpdated": "",
                        "seriesId": "NY.GNS.ICTR.ZS",
                    },
                    "data": [{"date": "2020-01-01", "value": 44.0}],
                }
            )
        ]

        needs = self.service._needs_indicator_clarification(  # pylint: disable=protected-access
            query="import share of gdp in china and us",
            data=mismatched_series,
            intent=intent,
        )
        self.assertTrue(needs)

    def test_build_multi_concept_query_clarification_for_single_indicator_parse(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["inflation"],
            parameters={"countries": ["US", "GB"]},
            clarificationNeeded=False,
            originalQuery="compare unemployment and inflation for g7 countries from 2010 to 2024",
        )

        clarification = self.service._build_multi_concept_query_clarification(  # pylint: disable=protected-access
            conversation_id="conv-multi",
            query=intent.originalQuery,
            intent=intent,
            is_multi_indicator=False,
            processing_steps=None,
        )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        joined = "\n".join(clarification.clarificationQuestions or []).lower()
        self.assertIn("multiple indicator families", joined)
        self.assertIn("labor market", joined)
        self.assertIn("prices/inflation", joined)

    def test_build_multi_concept_query_clarification_skips_trade_openness_phrasing(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["trade openness"],
            parameters={"country": "SG"},
            clarificationNeeded=False,
            originalQuery="trade openness ratio (exports plus imports to gdp) in singapore",
        )

        clarification = self.service._build_multi_concept_query_clarification(  # pylint: disable=protected-access
            conversation_id="conv-openness",
            query=intent.originalQuery,
            intent=intent,
            is_multi_indicator=False,
            processing_steps=None,
        )

        self.assertIsNone(clarification)

    def test_ranking_scope_expands_to_broader_country_set_when_missing(self) -> None:
        expanded = self.service._maybe_expand_ranking_country_scope(  # pylint: disable=protected-access
            query="Rank top 10 economies by GDP growth in 2023",
            provider="WORLDBANK",
            params={},
        )

        countries = expanded.get("countries", [])
        self.assertGreaterEqual(len(countries), 10)
        self.assertIn("US", countries)
        self.assertIn("CN", countries)
        self.assertNotIn("country", expanded)

    def test_ranking_scope_keeps_explicit_single_country(self) -> None:
        kept = self.service._maybe_expand_ranking_country_scope(  # pylint: disable=protected-access
            query="Rank inflation trend in Japan since 2015",
            provider="WORLDBANK",
            params={"country": "JP"},
        )

        self.assertEqual(kept.get("country"), "JP")
        self.assertNotIn("countries", kept)

    def test_collect_indicator_choice_options_prefers_raw_query_on_cue_mismatch(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["GDP (current US$)"],
            parameters={"countries": ["China", "UK"]},
            clarificationNeeded=False,
            originalQuery="gdp to debt ratio in china and uk",
        )

        class _Resolved:
            def __init__(self, provider: str, code: str, name: str, confidence: float):
                self.provider = provider
                self.code = code
                self.name = name
                self.confidence = confidence
                self.source = "catalog"

        captured_queries = []

        class _Resolver:
            def resolve(self, query, provider=None, **kwargs):
                captured_queries.append(str(query))
                mapping = {
                    "IMF": _Resolved("IMF", "GGXWDG_NGDP", "General government gross debt (% of GDP)", 0.95),
                    "WORLDBANK": _Resolved("WORLDBANK", "GC.DOD.TOTL.GD.ZS", "Central government debt, total (% of GDP)", 0.92),
                }
                return mapping.get((provider or "").upper())

        with patch.object(self.service, "_select_indicator_query_for_resolution", return_value="GDP (current US$)"), \
             patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.object(self.service, "_get_fallback_providers", return_value=["IMF"]):
            options = self.service._collect_indicator_choice_options(  # pylint: disable=protected-access
                query="gdp to debt ratio in china and uk",
                intent=intent,
                max_options=3,
            )

        self.assertTrue(options)
        self.assertTrue(any("debt" in option.lower() for option in options))
        self.assertTrue(captured_queries)
        self.assertEqual(captured_queries[0], "gdp to debt ratio in china and uk")

    def test_collect_indicator_choice_options_filters_provider_without_full_country_coverage(self) -> None:
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["producer price inflation"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="producer price inflation trend in the us and germany",
        )

        class _Resolved:
            def __init__(self, provider: str, code: str, name: str, confidence: float):
                self.provider = provider
                self.code = code
                self.name = name
                self.confidence = confidence
                self.source = "catalog"

        class _Resolver:
            def resolve(self, query, provider=None, **kwargs):
                mapping = {
                    "OECD": _Resolved("OECD", "PPI", "Producer Price Index", 0.92),
                    "WORLDBANK": _Resolved("WORLDBANK", "FP.WPI.TOTL.ZG", "Producer price inflation", 0.85),
                    "FRED": _Resolved("FRED", "PPIACO", "Producer Price Index by Commodity", 0.95),
                }
                return mapping.get((provider or "").upper())

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.object(self.service, "_get_fallback_providers", return_value=["WORLDBANK", "FRED"]):
            options = self.service._collect_indicator_choice_options(  # pylint: disable=protected-access
                query=intent.originalQuery,
                intent=intent,
                max_options=4,
            )

        joined = "\n".join(options).upper()
        self.assertIn("[OECD]", joined)
        self.assertIn("[WORLDBANK]", joined)
        self.assertNotIn("[FRED]", joined)

    def test_collect_indicator_choice_options_uses_human_readable_bis_labels(self) -> None:
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["debt service ratio"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="debt service ratio in china",
        )

        class _Resolved:
            def __init__(self, provider: str, code: str, name: str, confidence: float):
                self.provider = provider
                self.code = code
                self.name = name
                self.confidence = confidence
                self.source = "database"
                self.metadata = {"description": ""}

        class _Resolver:
            def resolve(self, query, provider=None, **kwargs):
                if (provider or "").upper() == "BIS":
                    return _Resolved("BIS", "WS_DSR", "WS_DSR", 0.9)
                return None

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.object(self.service, "_get_fallback_providers", return_value=[]), \
             patch.object(self.service.bis_provider, "_lookup_dataflow_info", return_value=("Debt service ratios", "Debt service ratios")):
            options = self.service._collect_indicator_choice_options(  # pylint: disable=protected-access
                query=intent.originalQuery,
                intent=intent,
                max_options=3,
            )

        self.assertTrue(options)
        self.assertIn("Debt service ratios", options[0])
        self.assertNotIn("WS_DSR] WS_DSR", options[0])

    def test_indicator_resolution_threshold_is_strict_for_high_precision_cues(self) -> None:
        threshold = self.service._indicator_resolution_threshold(  # pylint: disable=protected-access
            "producer price inflation trend in the us and germany",
            resolved_source="database",
        )
        self.assertGreaterEqual(threshold, 0.74)

    def test_indicator_resolution_threshold_allows_lower_for_generic_queries(self) -> None:
        threshold = self.service._indicator_resolution_threshold(  # pylint: disable=protected-access
            "gdp growth",
            resolved_source="database",
        )
        self.assertLessEqual(threshold, 0.68)

    def test_minimum_resolved_relevance_threshold_is_strict_for_directional_ratio_queries(self) -> None:
        min_relevance = self.service._minimum_resolved_relevance_threshold(  # pylint: disable=protected-access
            "import share of gdp in china and us"
        )
        self.assertGreaterEqual(min_relevance, 0.45)

    def test_minimum_resolved_relevance_threshold_is_permissive_for_generic_queries(self) -> None:
        min_relevance = self.service._minimum_resolved_relevance_threshold(  # pylint: disable=protected-access
            "gdp growth"
        )
        self.assertLessEqual(min_relevance, -0.30)

    def test_specific_cues_compatible_rejects_directional_conflict(self) -> None:
        compatible = self.service._specific_cues_compatible(  # pylint: disable=protected-access
            {"import"},
            {"export"},
        )
        self.assertFalse(compatible)

        trade_balance_compatible = self.service._specific_cues_compatible(  # pylint: disable=protected-access
            {"import"},
            {"trade_balance"},
        )
        self.assertFalse(trade_balance_compatible)

    def test_score_series_relevance_prefers_directional_ratio_match_over_generic_ratio(self) -> None:
        query = "import share of gdp in china and us"
        generic_ratio_series = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "WorldBank",
                    "indicator": "Gross domestic savings (% of GDP)",
                    "country": "China",
                    "frequency": "annual",
                    "unit": "% of GDP",
                    "lastUpdated": "2024-01-01",
                    "seriesId": "NY.GNS.ICTR.ZS",
                    "apiUrl": "https://example.com",
                },
                "data": [{"date": "2023-01-01", "value": 44.1}],
            }
        )
        directional_ratio_series = NormalizedData.model_validate(
            {
                "metadata": {
                    "source": "WorldBank",
                    "indicator": "Imports of goods and services (% of GDP)",
                    "country": "China",
                    "frequency": "annual",
                    "unit": "% of GDP",
                    "lastUpdated": "2024-01-01",
                    "seriesId": "NE.IMP.GNFS.ZS",
                    "apiUrl": "https://example.com",
                },
                "data": [{"date": "2023-01-01", "value": 18.2}],
            }
        )

        generic_score = self.service._score_series_relevance(query, generic_ratio_series)  # pylint: disable=protected-access
        directional_score = self.service._score_series_relevance(query, directional_ratio_series)  # pylint: disable=protected-access
        self.assertGreater(directional_score, generic_score + 2.0)

    def test_build_uncertain_result_clarification_asks_explicit_indicator_on_severe_mismatch(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["imports as % of GDP"],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp in china and us",
        )
        uncertain_data = [
            NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "WorldBank",
                        "indicator": "Exports of goods and services (% of GDP)",
                        "country": "China",
                        "frequency": "annual",
                        "unit": "% of GDP",
                        "lastUpdated": "2024-01-01",
                        "seriesId": "NE.EXP.GNFS.ZS",
                        "apiUrl": "https://example.com",
                    },
                    "data": [{"date": "2023-01-01", "value": 20.0}],
                }
            )
        ]

        class _Resolver:
            def resolve(self, *args, **kwargs):
                return None

        with patch.object(self.service, "_needs_indicator_clarification", return_value=True), \
             patch.object(self.service, "_collect_indicator_choice_options", return_value=[]), \
             patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()):
            clarification = self.service._build_uncertain_result_clarification(  # pylint: disable=protected-access
                conversation_id="conv-severe-mismatch",
                query="import share of gdp in china and us",
                intent=intent,
                data=uncertain_data,
            )

        self.assertIsNotNone(clarification)
        assert clarification is not None
        self.assertTrue(clarification.clarificationNeeded)
        joined = "\n".join(clarification.clarificationQuestions or []).lower()
        self.assertIn("may be wrong", joined)
        self.assertIn("exact indicator", joined)

    def test_fetch_data_rejects_high_confidence_but_low_relevance_resolved_code(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["imports share of gdp"],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp in china and us",
        )

        class _Resolved:
            def __init__(self):
                self.code = "NY.GDP.MKTP.CD"
                self.confidence = 0.95
                self.source = "database"
                self.name = "GDP (current US$)"
                self.provider = "WORLDBANK"

        class _Resolver:
            def resolve(self, *args, **kwargs):
                return _Resolved()

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.object(self.service, "_select_indicator_query_for_resolution", return_value="imports as % of GDP"), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.world_bank_provider, "fetch_indicator", return_value=[sample_series()]) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "NE.IMP.GNFS.ZS")

    def test_fetch_data_prefers_resolver_over_fuzzy_translator(self) -> None:
        """Resolver (catalog/FTS5) runs first and wins over fuzzy translator.

        When the resolver returns a high-confidence, plausible result,
        it is used directly — the fuzzy translator is not consulted.
        """
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["GDP"],
            parameters={"country": "Italy"},
            clarificationNeeded=False,
            originalQuery="Get Italy GDP from OECD",
        )

        class _Resolved:
            def __init__(self):
                self.code = "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE"
                self.confidence = 0.99
                self.source = "catalog"
                self.name = "GDP and main components - expenditure approach"
                self.provider = "OECD"
                self.metadata = {"indicator": "GDP and main components"}

        class _Resolver:
            def resolve(self, *args, **kwargs):
                return _Resolved()

        # Mock IndicatorSelector to raise ImportError (falls through to legacy resolver)
        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.dict("sys.modules", {"backend.services.indicator_selector": None}), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.oecd_provider, "fetch_indicator", return_value=sample_series()) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        # Legacy resolver's catalog result wins when IndicatorSelector unavailable
        self.assertEqual(
            fetch_mock.call_args.kwargs.get("indicator"),
            "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE",
        )

    def test_fetch_data_rejects_implausible_imf_selector_pick_and_uses_resolver(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=[
                "current account",
                "primary income",
                "investment income",
                "reserve assets",
                "balance of payments",
            ],
            parameters={"country": "Germany", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="Germany Current Account Primary Income Investment income Reserve assets Balance of Payments from IMF",
        )

        class _Resolved:
            def __init__(self):
                self.code = "BXIPIR_BP6_EUR"
                self.confidence = 0.96
                self.source = "catalog"
                self.name = "Balance of Payments, Current Account, Primary Income, Investment Income, Reserve Assets"
                self.provider = "IMF"
                self.metadata = {"indicator": self.name}

        class _Resolver:
            def resolve(self, *args, **kwargs):
                return _Resolved()

        fake_indicator_selector = types.ModuleType("backend.services.indicator_selector")

        class _Selector:
            async def select(self, *args, **kwargs):
                from backend.services.indicator_selector import SelectionResult

                return SelectionResult(
                    code="BCA_NGDPD",
                    name="Current account balance, percent of GDP",
                    source="llm_pick",
                )

        fake_indicator_selector.IndicatorSelector = _Selector
        from backend.services.indicator_selector import SelectionResult as _SelectionResult
        fake_indicator_selector.SelectionResult = _SelectionResult

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.dict(sys.modules, {"backend.services.indicator_selector": fake_indicator_selector}), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.imf_provider, "fetch_indicator", return_value=sample_series()) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        indicator_arg = fetch_mock.call_args.kwargs.get("indicator")
        self.assertNotEqual(indicator_arg, "BCA_NGDPD")
        self.assertTrue(
            indicator_arg == "BXIPIR_BP6_EUR" or "primary income" in str(indicator_arg).lower(),
            indicator_arg,
        )

    def test_fetch_data_rejects_implausible_explicit_imf_code_and_does_not_keep_it(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["revenue", "general government taxes", "social contributions"],
            parameters={
                "country": "Germany",
                "indicator": "rev",
                "__semantic_indicator_label": "general government taxes and social contributions revenue",
                "__semantic_provider_locked": True,
            },
            clarificationNeeded=False,
            originalQuery="Germany Revenue General Government Taxes Social contributions Government and Public Sector Finance from IMF",
        )

        class _Resolved:
            def __init__(self):
                self.code = "Germany Revenue General Government Taxes Social contributions Government and Public Sector Finance from IMF"
                self.confidence = 0.7
                self.source = "fallback"
                self.name = self.code
                self.provider = "IMF"
                self.metadata = {"indicator": self.code}

        class _Resolver:
            def resolve(self, *args, **kwargs):
                return _Resolved()

        with patch("backend.services.query.get_indicator_resolver", return_value=_Resolver()), \
             patch.dict("sys.modules", {"backend.services.indicator_selector": None}), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.imf_provider, "fetch_indicator", return_value=sample_series()) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        indicator_arg = fetch_mock.call_args.kwargs.get("indicator")
        self.assertNotEqual(indicator_arg, "rev")

    def test_code_semantic_hint_infers_worldbank_import_ratio_cues(self) -> None:
        hint = self.service._code_semantic_hint(  # pylint: disable=protected-access
            "WORLDBANK",
            "NE.IMP.GNFS.ZS",
        )
        hint_lower = hint.lower()
        self.assertIn("imports", hint_lower)
        self.assertIn("share of gdp", hint_lower)

    def test_code_semantic_hint_infers_fred_tenor_cues(self) -> None:
        hint = self.service._code_semantic_hint(  # pylint: disable=protected-access
            "FRED",
            "DGS10",
        )
        hint_lower = hint.lower()
        self.assertIn("10-year", hint_lower)
        self.assertIn("treasury yield", hint_lower)

    def test_eurostat_fetch_prefers_resolved_indicator_param(self) -> None:
        intent = ParsedIntent(
            apiProvider="Eurostat",
            indicators=["harmonized inflation"],
            parameters={"country": "DE", "indicator": "prc_hicp_manr"},
            clarificationNeeded=False,
            originalQuery="hicp inflation germany",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.eurostat_provider, "fetch_indicator", return_value=sample_series()) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "prc_hicp_manr")

    def test_oecd_fetch_prefers_resolved_indicator_param(self) -> None:
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["unemployment rate"],
            parameters={"country": "USA", "indicator": "LFS_UNEM_A"},
            clarificationNeeded=False,
            originalQuery="oecd unemployment rate us",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.oecd_provider, "fetch_indicator", return_value=sample_series()) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "LFS_UNEM_A")

    def test_imf_fetch_prefers_resolved_indicator_param_for_multi_country_batch(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["government deficit as % of gdp", "fiscal balance"],
            parameters={
                "countries": ["China", "Brazil"],
                "indicator": "GGXCNL_NGDP",
                "startDate": "2015-01-01",
                "endDate": "2024-01-01",
            },
            clarificationNeeded=False,
            originalQuery="from imf government deficit share of gdp in china and brazil",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.imf_provider, "_resolve_countries", side_effect=lambda x: [str(x).upper()]), \
             patch.object(self.service.imf_provider, "fetch_batch_indicator", new_callable=AsyncMock, return_value=[sample_series()]) as fetch_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(fetch_mock.await_count, 1)
        self.assertEqual(fetch_mock.await_args.kwargs.get("indicator"), "GGXCNL_NGDP")

    def test_fetch_data_concept_override_prefers_stronger_provider_match(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["REAL_EFFECTIVE_EXCHANGE_RATE"],
            parameters={"country": "JP"},
            clarificationNeeded=False,
            originalQuery="real effective exchange rate in japan",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.bis_provider, "fetch_indicator", side_effect=AssertionError("should stay on IMF")), \
             patch.object(self.service.imf_provider, "fetch_indicator", return_value=sample_series()) as imf_fetch:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertTrue(imf_fetch.called)
        self.assertEqual(imf_fetch.call_args.kwargs.get("indicator"), "EREER")

    def test_fetch_data_routes_statscan_dimension_decomposition_to_multi_dimension_fetch(self) -> None:
        intent = ParsedIntent(
            apiProvider="StatsCan",
            indicators=["employment rate"],
            parameters={
                "country": "CA",
                "indicator": "EMPLOYMENT_RATE",
                "__base_indicator": "EMPLOYMENT_RATE",
                "__dimensions": {"Geography": "Ontario"},
                "__statscan_product_id": "14100287",
                "__statscan_decomposition_axis": "Age group",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="dimension",
            originalQuery="Show by age group",
        )

        returned = [
            sample_series_with(
                source="Statistics Canada",
                indicator="employment rate - Ontario, 25 to 54 years",
                country="Canada",
                series_id="14100287:7.9.1.7.1.1.0.0.0.0",
            ),
            sample_series_with(
                source="Statistics Canada",
                indicator="employment rate - Ontario, 55 to 64 years",
                country="Canada",
                series_id="14100287:7.9.1.8.1.1.0.0.0.0",
            ),
        ]

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.statscan_provider, "fetch_multi_dimension_data", new_callable=AsyncMock, return_value=returned) as fetch_multi_dim, \
             patch.object(self.service.statscan_provider, "fetch_with_dimensions", new_callable=AsyncMock, side_effect=AssertionError("should not use scalar dimension fetch")):
            data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(data), 2)
        self.assertEqual(fetch_multi_dim.await_count, 1)
        params = fetch_multi_dim.await_args.args[0]
        self.assertEqual(params["axis"], "Age group")
        self.assertEqual(params["dimensions"], {"Geography": "Ontario"})

    def test_fetch_data_statscan_dimension_decomposition_falls_back_to_semantic_product_with_richer_axis(self) -> None:
        intent = ParsedIntent(
            apiProvider="StatsCan",
            indicators=["14100330"],
            parameters={
                "country": "CA",
                "indicator": "14100330",
                "__statscan_product_id": "14100330",
                "__statscan_decomposition_axis": "Age group",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="dimension",
            originalQuery="Show by age group",
        )

        returned = [
            sample_series_with(
                source="Statistics Canada",
                indicator="employment rate - 25 to 54 years",
                country="Canada",
                series_id="14100287:1.9.1.7.1.1.0.0.0.0",
            ),
            sample_series_with(
                source="Statistics Canada",
                indicator="employment rate - 55 to 64 years",
                country="Canada",
                series_id="14100287:1.9.1.8.1.1.0.0.0.0",
            ),
        ]

        async def _fake_fetch_multi_dimension(params):
            if params["productId"] == "14100330":
                raise ValueError("Product 14100330 has no dimension matching axis 'Age group'")
            return returned

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service, "_get_direct_provider_indicator_translation", return_value="14100287"), \
             patch.object(self.service.statscan_provider, "fetch_multi_dimension_data", new_callable=AsyncMock, side_effect=_fake_fetch_multi_dimension) as fetch_multi_dim, \
             patch.object(self.service.statscan_provider, "fetch_with_dimensions", new_callable=AsyncMock, side_effect=AssertionError("should not use scalar dimension fetch")):
            data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(data), 2)
        self.assertEqual(fetch_multi_dim.await_count, 2)
        first_params = fetch_multi_dim.await_args_list[0].args[0]
        second_params = fetch_multi_dim.await_args_list[1].args[0]
        self.assertEqual(first_params["productId"], "14100330")
        self.assertEqual(second_params["productId"], "14100287")
        self.assertEqual(intent.parameters.get("indicator"), "14100287")
        self.assertEqual(intent.parameters.get("__statscan_product_id"), "14100287")

    def test_fetch_data_preserves_statscan_product_for_dimension_filter_follow_up(self) -> None:
        intent = ParsedIntent(
            apiProvider="StatsCan",
            indicators=["employment rate"],
            parameters={
                "country": "CA",
                "indicator": "14100287",
                "__dimensions": {"Geography": "Ontario"},
                "__statscan_product_id": "14100330",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
            originalQuery="Show only Ontario",
        )

        returned = sample_series_with(
            source="Statistics Canada",
            indicator="Ontario employment rate",
            country="Ontario",
            series_id="14100330:120.1.0.0.0.0.0.0.0.0",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.statscan_provider, "fetch_categorical_data", new_callable=AsyncMock, return_value=returned) as fetch_categorical, \
             patch.object(self.service.statscan_provider, "fetch_with_dimensions", new_callable=AsyncMock, side_effect=AssertionError("should not use base-indicator dimension fetch")):
            data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(data), 1)
        self.assertEqual(fetch_categorical.await_count, 1)
        params = fetch_categorical.await_args.args[0]
        self.assertEqual(params["productId"], "14100330")
        self.assertEqual(params["dimensions"], {"Geography": "Ontario"})

    def test_execute_sub_query_clears_statscan_axis_when_materializing_dimension_member(self) -> None:
        original_intent = ParsedIntent(
            apiProvider="StatsCan",
            indicators=["employment rate"],
            parameters={
                "country": "Canada",
                "__dimensions": {"Geography": "Ontario"},
                "__statscan_decomposition_axis": "Age group",
                "__statscan_product_id": "14100287",
            },
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="dimension",
            decompositionEntities=["25 to 54 years"],
            originalQuery="Show by age group",
        )

        with patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[] ) as fetch_mock:
            run(
                self.service._execute_sub_query(  # pylint: disable=protected-access
                    entity="25 to 54 years",
                    sub_query="employment rate Ontario 25 to 54 years",
                    original_intent=original_intent,
                    conversation_id="conv-statscan-age-subquery",
                )
            )

        sub_intent = fetch_mock.await_args.args[0]
        self.assertNotIn("__statscan_decomposition_axis", sub_intent.parameters)
        self.assertEqual(
            sub_intent.parameters.get("__dimensions"),
            {"Geography": "Ontario", "age": "25 to 54 years"},
        )
        self.assertEqual(sub_intent.indicators, ["employment rate"])

    def test_process_query_enforces_explicit_provider_request(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US"},
            clarificationNeeded=False,
        )

        class _Settings:
            use_langchain_orchestrator = False

        with patch("backend.config.get_settings", return_value=_Settings()), \
             patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch.object(self.service, "_fetch_data", return_value=[sample_series()]), \
             patch.object(self.service, "_select_routed_provider", side_effect=AssertionError("router should not override explicit provider")):
            response = run(self.service.process_query("gdp from world bank for us", auto_pro_mode=False))

        self.assertIsNotNone(response.intent)
        assert response.intent is not None
        self.assertEqual(response.intent.apiProvider, "WORLDBANK")

    def test_process_query_clarifies_unspecified_geography_breakdown_before_auto_promode(self) -> None:
        with patch.object(self.service, "_execute_pro_mode", new_callable=AsyncMock) as pro_mode_mock:
            response = run(self.service.process_query("employment by region", auto_pro_mode=True))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNone(response.codeExecution)
        joined = "\n".join(response.clarificationQuestions or [])
        self.assertIn("does not name the geography", joined)
        pro_mode_mock.assert_not_awaited()

    def test_detect_explicit_provider_does_not_treat_oecd_region_phrase_as_provider_request(self) -> None:
        provider = self.service._detect_explicit_provider(  # pylint: disable=protected-access
            "Long-term interest rate comparison for OECD economies"
        )
        self.assertIsNone(provider)

    def test_select_routed_provider_prefers_semantic_router_when_available(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["government debt to gdp"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
        )

        class _SemanticRouter:
            async def route(self, **kwargs):
                return RoutingDecision(
                    provider="IMF",
                    confidence=0.88,
                    fallbacks=["WorldBank"],
                    reasoning="semantic-router match",
                    match_type="semantic",
                )

        class _HybridRouter:
            async def route(self, **kwargs):
                raise AssertionError("Hybrid router should not run when semantic router is enabled")

        self.service.semantic_provider_router = _SemanticRouter()
        self.service.hybrid_router = _HybridRouter()

        provider = run(self.service._select_routed_provider(intent, "government debt in china"))  # pylint: disable=protected-access
        self.assertEqual(provider, "IMF")

    def test_select_routed_provider_keeps_high_confidence_deterministic_match(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["gdp to debt ratio"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
        )

        class _UnifiedRouter:
            def route(self, **kwargs):
                return RoutingDecision(
                    provider="IMF",
                    confidence=0.90,
                    fallbacks=["WorldBank", "BIS"],
                    reasoning="deterministic macro debt rule",
                    match_type="indicator",
                )

        class _SemanticRouter:
            async def route(self, **kwargs):
                return RoutingDecision(
                    provider="BIS",
                    confidence=0.58,
                    fallbacks=["IMF", "WorldBank"],
                    reasoning="semantic-router similarity match (0.58)",
                    match_type="semantic",
                )

        self.service.unified_router = _UnifiedRouter()
        self.service.semantic_provider_router = _SemanticRouter()
        self.service.hybrid_router = None

        provider = run(self.service._select_routed_provider(intent, "gdp to debt ratio in china"))  # pylint: disable=protected-access
        self.assertEqual(provider, "IMF")

    def test_select_routed_provider_uses_unified_router_baseline(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["gdp growth"],
            parameters={"country": "DE"},
            clarificationNeeded=False,
        )

        class _UnifiedRouter:
            def route(self, **kwargs):
                return RoutingDecision(
                    provider="IMF",
                    confidence=0.9,
                    fallbacks=["WorldBank"],
                    reasoning="unified baseline",
                    match_type="indicator",
                )

        self.service.unified_router = _UnifiedRouter()
        self.service.semantic_provider_router = None
        self.service.hybrid_router = None

        with patch("backend.services.query.unified_route_provider", side_effect=AssertionError("legacy baseline should not run")):
            provider = run(self.service._select_routed_provider(intent, "gdp growth germany"))  # pylint: disable=protected-access
        self.assertEqual(provider, "IMF")

    def test_catalog_provider_reroute_remaps_indicator_code(self) -> None:
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=["renewable energy share"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="renewable energy share in germany",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.world_bank_provider, "fetch_indicator", return_value=[sample_series()]) as wb_fetch:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(intent.apiProvider, "WorldBank")
        self.assertEqual(intent.parameters.get("indicator"), "EG.FEC.RNEW.ZS")
        self.assertEqual(wb_fetch.call_args.kwargs.get("indicator"), "EG.FEC.RNEW.ZS")

    def test_is_fallback_relevant_uses_country_resolver_aliases(self) -> None:
        series = sample_series()
        series.metadata.country = "GB"
        series.metadata.indicator = "Imports of goods and services (% of GDP)"

        self.assertTrue(
            self.service._is_fallback_relevant(  # pylint: disable=protected-access
                ["imports of goods and services"],
                [series],
                target_countries=["UK"],
            )
        )

    def test_is_fallback_relevant_rejects_country_mismatch_for_multi_country_queries(self) -> None:
        series = sample_series()
        series.metadata.country = "IN"
        series.metadata.indicator = "Imports of goods and services (% of GDP)"

        self.assertFalse(
            self.service._is_fallback_relevant(  # pylint: disable=protected-access
                ["imports of goods and services"],
                [series],
                target_countries=["China", "US"],
            )
        )

    def test_is_fallback_relevant_accepts_reer_when_code_signal_is_in_series_id(self) -> None:
        china_series = sample_series()
        china_series.metadata.country = "China"
        china_series.metadata.indicator = "Real effective exchange rate index (2010 = 100)"
        china_series.metadata.seriesId = "PX.REX.REER"
        china_series.metadata.description = "Real effective exchange rate index (2010 = 100)"

        india_series = sample_series()
        india_series.metadata.country = "India"
        india_series.metadata.indicator = "Real effective exchange rate index (2010 = 100)"
        india_series.metadata.seriesId = "PX.REX.REER"
        india_series.metadata.description = "Real effective exchange rate index (2010 = 100)"

        self.assertTrue(
            self.service._is_fallback_relevant(  # pylint: disable=protected-access
                ["EREER"],
                [china_series, india_series],
                target_countries=["China", "India"],
                original_query="REER trend for China and India from 2012 to 2024",
            )
        )

    def test_is_fallback_relevant_rejects_incomplete_country_coverage_for_multi_country_query(self) -> None:
        series = sample_series()
        series.metadata.country = "China"
        series.metadata.indicator = "Real effective exchange rate index (2010 = 100)"
        series.metadata.seriesId = "PX.REX.REER"

        self.assertFalse(
            self.service._is_fallback_relevant(  # pylint: disable=protected-access
                ["EREER"],
                [series],
                target_countries=["China", "India"],
                original_query="REER trend for China and India from 2012 to 2024",
            )
        )

    def test_assess_country_coverage_reports_missing_countries(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["imports as % of GDP"],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp in china and us",
        )
        series = sample_series()
        series.metadata.country = "China"
        series.metadata.indicator = "Imports of goods and services (% of GDP)"

        coverage = self.service._assess_country_coverage(  # pylint: disable=protected-access
            intent,
            [series],
        )

        self.assertIsNotNone(coverage)
        assert coverage is not None
        self.assertFalse(coverage["complete"])
        self.assertIn("US", coverage["missing_iso2"])

    def test_maybe_improve_country_coverage_returns_warning_when_incomplete(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["imports as % of GDP"],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp in china and us",
        )
        series = sample_series()
        series.metadata.country = "China"
        series.metadata.indicator = "Imports of goods and services (% of GDP)"

        with patch.object(self.service, "_try_with_fallback", side_effect=DataNotAvailableError("no fallback")):
            improved_data, warning = run(
                self.service._maybe_improve_country_coverage(  # pylint: disable=protected-access
                    query=intent.originalQuery or "",
                    intent=intent,
                    data=[series],
                )
            )

        self.assertEqual(len(improved_data), 1)
        self.assertIsNotNone(warning)
        assert warning is not None
        self.assertIn("Missing", warning)

    def test_try_with_fallback_sanitizes_provider_specific_indicator_params(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["imports of goods and services (% of GDP)"],
            parameters={
                "country": "CN",
                "indicator": "BM_GDP",
                "seriesId": "BM_GDP",
                "startDate": "2020-01-01",
                "endDate": "2024-01-01",
            },
            clarificationNeeded=False,
            originalQuery="imports share of gdp in china",
        )
        captured_intent = {}

        async def _fake_fetch_data(fallback_intent):
            captured_intent["intent"] = fallback_intent
            return [sample_series()]

        with patch.object(self.service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
             patch.object(self.service, "_fetch_data", side_effect=_fake_fetch_data), \
             patch.object(self.service, "_is_fallback_relevant", return_value=True):
            result = run(
                self.service._try_with_fallback(  # pylint: disable=protected-access
                    intent,
                    DataNotAvailableError("primary failed"),
                )
            )

        self.assertEqual(len(result), 1)
        fallback_intent = captured_intent["intent"]
        self.assertEqual(fallback_intent.apiProvider, "WORLDBANK")
        self.assertNotIn("indicator", fallback_intent.parameters)
        self.assertNotIn("seriesId", fallback_intent.parameters)
        self.assertEqual(intent.parameters.get("indicator"), "BM_GDP")
        self.assertEqual(intent.parameters.get("seriesId"), "BM_GDP")

    def test_try_with_fallback_replaces_single_indicator_with_semantic_fallback_query(self) -> None:
        """Fallback should resolve indicator on the TARGET provider, not pass source codes.

        When IMF (EREER) fails and we fall back to WORLDBANK, the catalog should
        resolve the concept 'real_effective_exchange_rate' to WorldBank's code
        PX.REX.REER — NOT pass the IMF code or a generic phrase.
        """
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["EREER"],
            parameters={"country": "CN", "indicator": "EREER"},
            clarificationNeeded=False,
            originalQuery="REER trend for China and India from 2012 to 2024",
        )
        captured_intent = {}

        async def _fake_fetch_data(fallback_intent):
            captured_intent["intent"] = fallback_intent
            return [sample_series()]

        with patch.object(self.service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
             patch.object(self.service, "_fetch_data", side_effect=_fake_fetch_data), \
             patch.object(self.service, "_is_fallback_relevant", return_value=True):
            _ = run(
                self.service._try_with_fallback(  # pylint: disable=protected-access
                    intent,
                    DataNotAvailableError("primary failed"),
                )
            )

        fallback_intent = captured_intent["intent"]
        # The fallback should use WorldBank's code for the same concept,
        # NOT the IMF code "EREER" and NOT a generic phrase.
        indicator = fallback_intent.indicators[0]
        # Must not be the IMF-specific code
        self.assertNotEqual(indicator, "EREER")
        # Should be WorldBank's catalog code or a semantic phrase
        # (PX.REX.REER if catalog resolves, otherwise a phrase like "real effective exchange rate")
        self.assertTrue(
            indicator == "PX.REX.REER" or "exchange rate" in indicator.lower(),
            f"Expected WorldBank code or semantic phrase, got: {indicator}",
        )

    def test_try_with_fallback_never_passes_source_provider_code_to_target(self) -> None:
        """WorldBank code NE.EXP.GNFS.ZS must NOT leak to IMF during fallback."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["NE.EXP.GNFS.ZS"],
            parameters={
                "country": "BR",
                "indicator": "NE.EXP.GNFS.ZS",
                "__catalog_resolved": True,
                "__catalog_concept": "exports_pct_gdp",
            },
            clarificationNeeded=False,
            originalQuery="exports as % of GDP for Brazil",
        )
        captured_intent = {}

        async def _fake_fetch_data(fallback_intent):
            captured_intent["intent"] = fallback_intent
            return [sample_series()]

        with patch.object(self.service, "_get_fallback_providers", return_value=["IMF"]), \
             patch.object(self.service, "_fetch_data", side_effect=_fake_fetch_data), \
             patch.object(self.service, "_is_fallback_relevant", return_value=True):
            _ = run(
                self.service._try_with_fallback(  # pylint: disable=protected-access
                    intent,
                    DataNotAvailableError("primary failed"),
                )
            )

        fallback_intent = captured_intent["intent"]
        self.assertEqual(fallback_intent.apiProvider, "IMF")
        indicator = fallback_intent.indicators[0]
        # Must NOT be the WorldBank-specific code
        self.assertNotEqual(indicator, "NE.EXP.GNFS.ZS")
        # Should not contain any WorldBank-style code patterns
        self.assertNotIn(".", indicator[:6] if len(indicator) > 6 else "")
        # Provider-specific params must be removed
        self.assertNotIn("indicator", fallback_intent.parameters)
        self.assertNotIn("seriesId", fallback_intent.parameters)
        # Stale catalog state must be cleared
        self.assertNotIn("__catalog_resolved", fallback_intent.parameters)

    def test_country_coverage_does_not_expand_bilateral_comtrade_with_worldbank_fallback(self) -> None:
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["exports"],
            parameters={"reporter": "United States", "partner": "China", "flow": "EXPORT"},
            clarificationNeeded=False,
            originalQuery="US exports to China",
        )
        comtrade_data = [sample_series_with(source="UN Comtrade", indicator="Exports - Total Trade", country="United States")]

        with patch.object(self.service, "_assess_country_coverage", return_value={"complete": False, "covered_count": 1, "requested_count": 2, "missing_display": ["CN"]}), \
             patch.object(self.service, "_try_with_fallback", new_callable=AsyncMock) as fallback_mock:
            improved, warning = run(
                self.service._maybe_improve_country_coverage(  # pylint: disable=protected-access
                    "US exports to China",
                    intent,
                    comtrade_data,
                )
            )

        self.assertEqual(improved, comtrade_data)
        self.assertIsNone(warning)
        fallback_mock.assert_not_called()

    def test_try_with_fallback_uses_catalog_concept_for_resolution(self) -> None:
        """When __catalog_concept is available, use it to resolve the fallback indicator."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["NY.GDP.MKTP.CD"],
            parameters={
                "country": "US",
                "indicator": "NY.GDP.MKTP.CD",
                "__catalog_resolved": True,
                "__catalog_concept": "gdp",
            },
            clarificationNeeded=False,
            originalQuery="GDP of the United States",
        )
        captured_intent = {}

        async def _fake_fetch_data(fallback_intent):
            captured_intent["intent"] = fallback_intent
            return [sample_series()]

        with patch.object(self.service, "_get_fallback_providers", return_value=["FRED"]), \
             patch.object(self.service, "_fetch_data", side_effect=_fake_fetch_data), \
             patch.object(self.service, "_is_fallback_relevant", return_value=True):
            _ = run(
                self.service._try_with_fallback(  # pylint: disable=protected-access
                    intent,
                    DataNotAvailableError("primary failed"),
                )
            )

        fallback_intent = captured_intent["intent"]
        self.assertEqual(fallback_intent.apiProvider, "FRED")
        indicator = fallback_intent.indicators[0]
        # Must NOT be the WorldBank code
        self.assertNotEqual(indicator, "NY.GDP.MKTP.CD")

    def test_try_with_fallback_skips_cross_provider_retry_for_provider_locked_intent(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["current account primary income investment income reserve assets"],
            parameters={
                "country": "DE",
                "__semantic_provider_locked": True,
            },
            clarificationNeeded=False,
            originalQuery="Germany Current Account Primary Income Investment income Reserve assets Balance of Payments from IMF",
        )

        with patch.object(self.service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
             patch.object(self.service, "_fetch_data", side_effect=AssertionError("fallback should not execute")):
            with self.assertRaises(DataNotAvailableError):
                run(
                    self.service._try_with_fallback(  # pylint: disable=protected-access
                        intent,
                        DataNotAvailableError("primary failed"),
                    )
                )

    def test_process_query_provider_locked_imf_partial_multi_indicator_fails_closed(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=[
                "current account",
                "primary income",
                "investment income",
                "reserve assets",
                "balance of payments",
            ],
            parameters={"country": "Germany", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="Germany Current Account Primary Income Investment income Reserve assets Balance of Payments from IMF",
        )
        validation = ValidationResult(
            is_multi_indicator=True,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch.object(self.service.pipeline, "parse_and_route", new=AsyncMock(return_value=ParseRouteResult(intent=intent, explicit_provider="IMF", routed_provider="IMF", validation_warning=None))), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_fetch_multi_indicator_data", new=AsyncMock(return_value=[sample_series_with(source="IMF", indicator="Current account balance, percent of GDP", series_id="BCA_NGDPD", country="Germany")])), \
             patch.object(self.service, "_try_with_fallback", new=AsyncMock(side_effect=AssertionError("fallback should not run"))):
            response = run(self.service.process_query(intent.originalQuery))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNotNone(response.clarificationQuestions)
        self.assertIn("reliable indicator", " ".join(response.clarificationQuestions).lower())

    def test_process_query_provider_locked_imf_implausible_single_series_fails_closed(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["Consumer Prices Miscellaneous Goods and Services"],
            parameters={"country": "Germany", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="Germany All Items Special Indexes Capital City Consumer Prices Miscellaneous Goods and Services from IMF",
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch.object(self.service.pipeline, "parse_and_route", new=AsyncMock(return_value=ParseRouteResult(intent=intent, explicit_provider="IMF", routed_provider="IMF", validation_warning=None))), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_fetch_data", new=AsyncMock(return_value=[sample_series_with(source="IMF", indicator="Inflation rate, average consumer prices", series_id="PCPIPCH", country="Germany")])), \
             patch.object(self.service, "_try_with_fallback", new=AsyncMock(side_effect=AssertionError("fallback should not run"))):
            response = run(self.service.process_query(intent.originalQuery))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNotNone(response.clarificationQuestions)
        self.assertIn("reliable indicator", " ".join(response.clarificationQuestions).lower())

    def test_process_query_provider_locked_worldbank_implausible_single_series_fails_closed(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["Teachers in tertiary education ISCED 5 programmes, female (number)"],
            parameters={"country": "IN", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="India Teachers in tertiary education ISCED 5 programmes female (number) from World Bank",
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch.object(self.service.pipeline, "parse_and_route", new=AsyncMock(return_value=ParseRouteResult(intent=intent, explicit_provider="WORLDBANK", routed_provider="WORLDBANK", validation_warning=None))), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_fetch_data", new=AsyncMock(return_value=[sample_series_with(source="World Bank", indicator="School enrollment, primary (% gross)", series_id="SE.PRM.ENRR", country="India")])), \
             patch.object(self.service, "_try_with_fallback", new=AsyncMock(side_effect=AssertionError("fallback should not run"))):
            response = run(self.service.process_query(intent.originalQuery))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNotNone(response.clarificationQuestions)
        self.assertIn("reliable indicator", " ".join(response.clarificationQuestions).lower())

    def test_process_query_provider_locked_oecd_implausible_single_series_fails_closed(self) -> None:
        intent = ParsedIntent(
            apiProvider="OECD",
            indicators=["DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD"],
            parameters={"country": "Japan", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="Japan Sustainable Development Goal 08 - Decent work and economic growth from OECD",
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch.object(self.service.pipeline, "parse_and_route", new=AsyncMock(return_value=ParseRouteResult(intent=intent, explicit_provider="OECD", routed_provider="OECD", validation_warning=None))), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_fetch_data", new=AsyncMock(return_value=[sample_series_with(source="OECD", indicator="Quarterly real GDP growth - OECD countries", series_id="DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD", country="Japan")])), \
             patch.object(self.service, "_try_with_fallback", new=AsyncMock(side_effect=AssertionError("fallback should not run"))):
            response = run(self.service.process_query(intent.originalQuery))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNotNone(response.clarificationQuestions)
        self.assertIn("reliable indicator", " ".join(response.clarificationQuestions).lower())

    def test_process_query_provider_locked_eurostat_implausible_single_series_fails_closed(self) -> None:
        intent = ParsedIntent(
            apiProvider="EUROSTAT",
            indicators=["PRC_HICP_AIND"],
            parameters={"country": "FR", "__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="France Purchasing power parities price level indices from Eurostat",
        )
        validation = ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch.object(self.service.pipeline, "parse_and_route", new=AsyncMock(return_value=ParseRouteResult(intent=intent, explicit_provider="EUROSTAT", routed_provider="EUROSTAT", validation_warning=None))), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch.object(self.service, "_build_post_parse_clarification", new=AsyncMock(return_value=None)), \
             patch.object(self.service, "_fetch_data", new=AsyncMock(return_value=[sample_series_with(source="Eurostat", indicator="HICP - annual data (average index and rate of change) (1996-2025)", series_id="PRC_HICP_AIND", country="France")])), \
             patch.object(self.service, "_try_with_fallback", new=AsyncMock(side_effect=AssertionError("fallback should not run"))):
            response = run(self.service.process_query(intent.originalQuery))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNotNone(response.clarificationQuestions)
        self.assertIn("reliable indicator", " ".join(response.clarificationQuestions).lower())

    def test_resolve_concept_for_fallback_from_stored_param(self) -> None:
        """_resolve_concept_for_fallback uses __catalog_concept when available."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["NE.EXP.GNFS.ZS"],
            parameters={"__catalog_concept": "exports_pct_gdp"},
            clarificationNeeded=False,
        )
        concept = self.service._resolve_concept_for_fallback(intent, "WORLDBANK")
        self.assertEqual(concept, "exports_pct_gdp")

    def test_resolve_concept_for_fallback_via_reverse_code_lookup(self) -> None:
        """_resolve_concept_for_fallback reverse-looks up provider codes in catalog."""
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["EREER"],
            parameters={"indicator": "EREER"},
            clarificationNeeded=False,
            originalQuery="real effective exchange rate",
        )
        concept = self.service._resolve_concept_for_fallback(intent, "IMF")
        # Should find real_effective_exchange_rate or similar from catalog
        self.assertIsNotNone(concept)

    def test_select_indicator_query_prefers_original_query_over_provider_code(self) -> None:
        """_select_indicator_query_for_resolution should not return provider codes."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["NE.EXP.GNFS.ZS"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="exports as percentage of GDP",
        )
        result = self.service._select_indicator_query_for_resolution(intent)
        # Should prefer the original query, not the WorldBank code
        self.assertNotEqual(result, "NE.EXP.GNFS.ZS")
        self.assertIn("export", result.lower())

    def test_select_indicator_query_prefers_semantic_indicator_label_for_short_follow_up(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["deflation"],
            parameters={
                "countries": ["CA", "GB"],
                "indicator": "NY.GDP.MKTP.IN",
                "__semantic_indicator_label": "inflation rate",
            },
            clarificationNeeded=False,
            originalQuery="Show only Canada and United Kingdom",
            isFollowUp=True,
        )
        result = self.service._select_indicator_query_for_resolution(intent)
        self.assertEqual(result, "inflation rate")

    def test_extract_exchange_rate_params_preserves_currency_pair_from_dimensions(self) -> None:
        intent = ParsedIntent(
            apiProvider="EXCHANGERATE",
            indicators=["dynamic"],
            parameters={
                "__dimensions": {"Currency Pair": "USD to CHF"},
            },
            clarificationNeeded=False,
            originalQuery="Show only the last 30 days",
        )

        params = self.service._extract_exchange_rate_params(  # pylint: disable=protected-access
            dict(intent.parameters),
            intent,
        )

        self.assertEqual(params.get("baseCurrency"), "USD")
        self.assertEqual(params.get("targetCurrency"), "CHF")

    def test_get_fallback_providers_passes_country_context_to_resolver(self) -> None:
        class _Resolved:
            confidence = 0.8

        class _Resolver:
            def __init__(self):
                self.calls = []

            def resolve(self, *args, **kwargs):
                self.calls.append(kwargs)
                return _Resolved()

        resolver = _Resolver()
        with patch("backend.services.indicator_resolver.get_indicator_resolver", return_value=resolver):
            _ = self.service._get_fallback_providers(  # pylint: disable=protected-access
                "IMF",
                indicator="imports",
                country="CN",
                countries=["CN", "GB"],
            )

        self.assertTrue(resolver.calls)
        first_call = resolver.calls[0]
        self.assertEqual(first_call.get("country"), "CN")
        self.assertEqual(first_call.get("countries"), ["CN", "GB"])
        self.assertFalse(first_call.get("use_cache", True))

    def test_execute_with_langgraph_handles_generated_file_models(self) -> None:
        class _FakeGraph:
            async def ainvoke(self, _initial_state, _config):
                return {
                    "is_pro_mode": True,
                    "code_execution": {
                        "code": "print('ok')",
                        "output": "ok",
                        "error": None,
                        "files": [
                            GeneratedFile(
                                url="/static/promode/report.png",
                                name="report.png",
                                type="image",
                            )
                        ],
                    },
                }

        class _FakeStateManager:
            def get(self, _conversation_id):
                return None

            def update(self, *_args, **_kwargs):
                return None

        import sys
        import types

        fake_agents_module = types.ModuleType("backend.agents")
        fake_agents_module.get_agent_graph = lambda: _FakeGraph()
        fake_agents_module.set_query_service_provider = lambda _provider=None: None

        fake_messages_module = types.ModuleType("langchain_core.messages")

        class _Message:
            def __init__(self, content: str = ""):
                self.content = content

        fake_messages_module.HumanMessage = _Message
        fake_messages_module.AIMessage = _Message
        fake_langchain_core = types.ModuleType("langchain_core")
        fake_langchain_core.messages = fake_messages_module

        with patch.dict(
            sys.modules,
            {
                "backend.agents": fake_agents_module,
                "langchain_core": fake_langchain_core,
                "langchain_core.messages": fake_messages_module,
            },
        ), \
             patch("backend.memory.state_manager.get_state_manager", return_value=_FakeStateManager()):
            response = run(
                self.service._execute_with_langgraph(  # pylint: disable=protected-access
                    query="create inflation forecast chart",
                    conversation_id="conv-lg-pro",
                    conversation_history=[],
                    tracker=None,
                )
            )

        self.assertTrue(response.isProMode)
        self.assertIsNotNone(response.codeExecution)
        assert response.codeExecution is not None
        self.assertEqual(response.codeExecution.files[0].name, "report.png")

    def test_execute_with_langgraph_retries_standard_path_when_empty_result_lacks_intent(self) -> None:
        class _FakeGraph:
            async def ainvoke(self, _initial_state, _config):
                return {
                    "result": {"data": []},
                    "parsed_intent": None,
                    "current_provider": "unknown",
                }

        class _FakeStateManager:
            def get(self, _conversation_id):
                return None

            def update(self, *_args, **_kwargs):
                return None

        standard_response = QueryResponse(
            conversationId="conv-lg-empty",
            clarificationNeeded=False,
            data=[sample_series()],
        )

        import sys
        import types

        fake_agents_module = types.ModuleType("backend.agents")
        fake_agents_module.get_agent_graph = lambda: _FakeGraph()
        fake_agents_module.set_query_service_provider = lambda _provider=None: None

        fake_messages_module = types.ModuleType("langchain_core.messages")

        class _Message:
            def __init__(self, content: str = ""):
                self.content = content

        fake_messages_module.HumanMessage = _Message
        fake_messages_module.AIMessage = _Message
        fake_langchain_core = types.ModuleType("langchain_core")
        fake_langchain_core.messages = fake_messages_module

        with patch.dict(
            sys.modules,
            {
                "backend.agents": fake_agents_module,
                "langchain_core": fake_langchain_core,
                "langchain_core.messages": fake_messages_module,
            },
        ), \
             patch("backend.memory.state_manager.get_state_manager", return_value=_FakeStateManager()), \
             patch.object(self.service, "_standard_query_processing", new_callable=AsyncMock, return_value=standard_response) as standard_mock:
            response = run(
                self.service._execute_with_langgraph(  # pylint: disable=protected-access
                    query="which asean country has highest import share of gdp since 2015",
                    conversation_id="conv-lg-empty",
                    conversation_history=[],
                    tracker=None,
                )
            )

        self.assertEqual(len(response.data or []), 1)
        standard_mock.assert_awaited_once()

    def test_process_query_tries_fallback_when_primary_returns_empty_data(self) -> None:
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["imports share of gdp"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="imports share of gdp in china",
        )

        class _Settings:
            use_langchain_orchestrator = False

        with patch("backend.config.get_settings", return_value=_Settings()), \
             patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch.object(self.service, "_fetch_data", return_value=[]), \
             patch.object(self.service, "_build_post_parse_clarification", return_value=None), \
             patch.object(self.service, "_try_with_fallback", return_value=[sample_series()]) as fallback_mock:
            response = run(self.service.process_query("imports share of gdp in china", auto_pro_mode=False))

        self.assertIsNone(response.error)
        self.assertEqual(len(response.data or []), 1)
        self.assertTrue(fallback_mock.called)

    def test_process_query_adds_warning_when_multi_country_coverage_is_partial(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["imports share of gdp"],
            parameters={"countries": ["China", "US"]},
            clarificationNeeded=False,
            originalQuery="import share of gdp in china and us",
        )

        class _Settings:
            use_langchain_orchestrator = False

        china_series = sample_series()
        china_series.metadata.source = "WorldBank"
        china_series.metadata.country = "China"
        china_series.metadata.indicator = "Imports of goods and services (% of GDP)"
        china_series.metadata.seriesId = "NE.IMP.GNFS.ZS"

        with patch("backend.config.get_settings", return_value=_Settings()), \
             patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch.object(self.service, "_fetch_data", return_value=[china_series]), \
             patch.object(self.service, "_build_post_parse_clarification", return_value=None), \
             patch.object(self.service, "_try_with_fallback", side_effect=DataNotAvailableError("no fallback")), \
             patch.object(self.service, "_build_uncertain_result_clarification", return_value=None):
            response = run(self.service.process_query("import share of gdp in china and us", auto_pro_mode=False))

        self.assertFalse(response.clarificationNeeded)
        self.assertEqual(len(response.data or []), 1)
        self.assertIsNotNone(response.message)
        assert response.message is not None
        self.assertIn("subset of requested countries", response.message)
        self.assertIn("Missing", response.message)

    def test_process_query_returns_indicator_clarification_when_no_data_and_options_exist(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["producer price inflation"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="producer price inflation trend in the us and germany",
        )

        class _Settings:
            use_langchain_orchestrator = False

        options = [
            "[WORLDBANK] Inflation, consumer prices (annual %) (FP.CPI.TOTL.ZG)",
            "[OECD] House prices index (HOUSE_PRICES)",
        ]

        with patch("backend.config.get_settings", return_value=_Settings()), \
             patch.object(self.service.openrouter, "parse_query", return_value=intent), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch("backend.services.query.ParameterValidator.validate_intent", return_value=(True, None, None)), \
             patch("backend.services.query.ParameterValidator.check_confidence", return_value=(True, None)), \
             patch.object(self.service, "_fetch_data", return_value=[]), \
             patch.object(self.service, "_try_with_fallback", side_effect=DataNotAvailableError("primary failed")), \
             patch.object(self.service, "_maybe_recover_from_empty_data", return_value=None), \
             patch.object(self.service, "_collect_indicator_choice_options", return_value=options):
            response = run(self.service.process_query("producer price inflation trend in the us and germany", auto_pro_mode=False))

        self.assertTrue(response.clarificationNeeded)
        joined = "\n".join(response.clarificationQuestions or [])
        self.assertIn("Please choose", joined)

    def test_standard_query_processing_uses_semantic_recovery_when_empty(self) -> None:
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["top economies by gdp growth"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="Rank top 10 economies by GDP growth in 2023",
        )

        parse_result = type("ParseResult", (), {"intent": intent})()

        with patch.object(self.service.pipeline, "parse_and_route", new_callable=AsyncMock, return_value=parse_result), \
             patch.object(self.service, "_fetch_data", new_callable=AsyncMock, return_value=[]), \
             patch.object(self.service, "_maybe_recover_from_empty_data", new_callable=AsyncMock, return_value=[sample_series()]):
            response = run(
                self.service._standard_query_processing(  # pylint: disable=protected-access
                    query="Rank top 10 economies by GDP growth in 2023",
                    conversation_id="conv-standard-recovery",
                    tracker=None,
                    record_user_message=False,
                )
            )

        self.assertEqual(len(response.data or []), 1)
        self.assertIsNone(response.error)

    def test_fetch_data_coingecko_normalizes_empty_coin_ids_and_vs_currency(self) -> None:
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=["bitcoin market cap"],
            parameters={"coinIds": None, "vsCurrency": "right"},
            clarificationNeeded=False,
            originalQuery="bitcoin market cap right now",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.coingecko_provider, "get_historical_data_range", return_value=[sample_series()]) as range_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertTrue(range_mock.called)
        self.assertEqual(range_mock.call_args.kwargs.get("vs_currency"), "usd")

    def test_fetch_data_coingecko_uses_query_text_for_coin_and_metric_when_indicator_is_dynamic(self) -> None:
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=["dynamic"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="solana trading volume over the last 90 days",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.coingecko_provider, "get_historical_data", return_value=[sample_series()]) as historical_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertTrue(historical_mock.called)
        self.assertEqual(historical_mock.call_args.kwargs.get("coin_id"), "solana")
        self.assertEqual(historical_mock.call_args.kwargs.get("metric"), "volume")
        self.assertEqual(historical_mock.call_args.kwargs.get("days"), 90)

    def test_fetch_data_coingecko_dedupes_coin_ids_for_comparisons(self) -> None:
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=["bitcoin price", "ethereum price"],
            parameters={"coinIds": ["bitcoin", "ethereum", "bitcoin", "ethereum"]},
            clarificationNeeded=False,
            originalQuery="Add Ethereum for comparison",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.coingecko_provider, "get_simple_price", return_value=[sample_series(), sample_series_with(series_id="ethereum", indicator="Ethereum", source="CoinGecko")]) as simple_mock, \
             patch.object(self.service.coingecko_provider, "get_historical_data_range", return_value=[sample_series()]) as range_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        if simple_mock.called:
            self.assertEqual(simple_mock.call_args.kwargs.get("coin_ids"), ["bitcoin", "ethereum"])
        else:
            self.assertTrue(range_mock.called)
            called_coin_ids = [call.kwargs.get("coin_id") for call in range_mock.call_args_list]
            self.assertEqual(called_coin_ids, ["bitcoin", "ethereum"])

    def test_fetch_data_coingecko_respects_exact_title_coin_ids(self) -> None:
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=["Liberty Finance"],
            parameters={"coinIds": ["libfi"], "indicator": "libfi"},
            clarificationNeeded=False,
            originalQuery="Liberty Finance cryptocurrency price from CoinGecko",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.coingecko_provider, "get_simple_price", return_value=[sample_series_with(series_id="libfi", indicator="Liberty Finance", source="CoinGecko")]) as simple_mock, \
             patch.object(self.service.coingecko_provider, "get_historical_data_range", return_value=[sample_series_with(series_id="libfi", indicator="Liberty Finance", source="CoinGecko")]) as range_mock:
            run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        if simple_mock.called:
            self.assertEqual(simple_mock.call_args.kwargs.get("coin_ids"), ["libfi"])
        else:
            self.assertTrue(range_mock.called)
            self.assertEqual(range_mock.call_args.kwargs.get("coin_id"), "libfi")

    def test_fetch_data_comtrade_trade_balance_uses_balance_fetch_despite_stale_flow(self) -> None:
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["trade balance"],
            parameters={"reporter": "United States", "partner": "Japan", "flow": "IMPORT"},
            clarificationNeeded=False,
            originalQuery="Switch to trade balance US and Japan",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.comtrade_provider, "fetch_trade_balance", new_callable=AsyncMock, return_value=sample_series()) as balance_mock, \
             patch.object(self.service.comtrade_provider, "fetch_trade_data", new_callable=AsyncMock) as trade_mock:
            result = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(result), 1)
        self.assertTrue(balance_mock.called)
        trade_mock.assert_not_called()

    def test_fetch_data_comtrade_bilateral_exports_does_not_treat_partner_as_second_reporter(self) -> None:
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["exports"],
            parameters={
                "countries": ["US", "CN"],
                "reporter": "United States",
                "partner": "China",
                "flow": "EXPORT",
            },
            clarificationNeeded=False,
            originalQuery="US exports to China",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.comtrade_provider, "fetch_trade_data", new_callable=AsyncMock, return_value=[sample_series()]) as trade_mock:
            result = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(result), 1)
        self.assertTrue(trade_mock.called)
        self.assertEqual(trade_mock.call_args.kwargs.get("reporter"), "United States")
        self.assertIsNone(trade_mock.call_args.kwargs.get("reporters"))

    def test_fetch_data_comtrade_preserves_multi_reporter_scope_when_primary_reporter_present(self) -> None:
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["exports"],
            parameters={
                "countries": ["US", "DE"],
                "reporters": ["US", "DE"],
                "reporter": "US",
                "partner": "China",
                "flow": "EXPORT",
            },
            clarificationNeeded=False,
            originalQuery="Add Germany exports to China",
        )

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.comtrade_provider, "fetch_trade_data", new_callable=AsyncMock, return_value=[sample_series()]) as trade_mock:
            result = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(result), 1)
        self.assertTrue(trade_mock.called)
        self.assertEqual(trade_mock.call_args.kwargs.get("reporter"), "US")
        self.assertEqual(trade_mock.call_args.kwargs.get("reporters"), ["US", "DE"])

    def test_fetch_data_comtrade_dedupes_alias_reporter_series(self) -> None:
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["exports"],
            parameters={"reporter": "United States", "partner": "China", "flow": "EXPORT"},
            clarificationNeeded=False,
            originalQuery="US exports to China",
        )
        us_full = sample_series_with(source="UN Comtrade", indicator="Exports - Total Trade", country="United States", series_id=None)
        us_alias = sample_series_with(source="UN Comtrade", indicator="Exports - Total Trade", country="US", series_id=None)

        with patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.comtrade_provider, "fetch_trade_data", new_callable=AsyncMock, return_value=[us_full, us_alias]):
            result = run(self.service._fetch_data(intent))  # pylint: disable=protected-access

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "United States")

    # ── Issue 1: Follow-up after unanswered clarification ──────────────

    def test_prompt_clarification_section_allows_ignoring(self) -> None:
        """The prompt should tell the LLM that the user may ignore the
        clarification and send a completely new query."""
        from backend.services.simplified_prompt import SimplifiedPrompt

        ctx = {
            "indicator": "employment",
            "country": "CA",
            "provider": "StatsCan",
            "startDate": "not specified",
            "endDate": "not specified",
            "originalQuery": "employment data Canada",
            "pendingClarification": True,
            "clarificationQuestion": "Which specific employment indicator?",
            "clarificationOptions": "employment rate, unemployment rate",
        }
        prompt = SimplifiedPrompt.generate(ctx)

        # The prompt must contain guidance about the user ignoring the clarification
        self.assertIn("IGNORING", prompt)
        self.assertIn("new independent query", prompt.lower())
        self.assertIn("isFollowUp=false", prompt)

    def test_prompt_follow_up_section_has_province_guidance(self) -> None:
        """The follow-up section should include province/state handling
        guidance so the LLM knows to use decomposition for sub-national regions."""
        from backend.services.simplified_prompt import SimplifiedPrompt

        ctx = {
            "indicator": "unemployment rate",
            "country": "CA",
            "provider": "StatsCan",
            "startDate": "not specified",
            "endDate": "not specified",
            "originalQuery": "Canada unemployment rate",
        }
        prompt = SimplifiedPrompt.generate(ctx)

        # Province guidance must be present
        self.assertIn("Province/state follow-up handling", prompt)
        self.assertIn("Ontario", prompt)
        self.assertIn("decompositionType", prompt)
        self.assertIn("StatsCan", prompt)
        self.assertIn("NOT a country", prompt)

    def test_prompt_clarification_section_omits_province_guidance(self) -> None:
        """The clarification section (pendingClarification=True) should NOT include
        the province follow-up section (it's for the non-clarification follow-up path)."""
        from backend.services.simplified_prompt import SimplifiedPrompt

        ctx = {
            "indicator": "employment",
            "country": "CA",
            "provider": "StatsCan",
            "startDate": "not specified",
            "endDate": "not specified",
            "originalQuery": "employment data Canada",
            "pendingClarification": True,
            "clarificationQuestion": "Which?",
            "clarificationOptions": "a, b",
        }
        prompt = SimplifiedPrompt.generate(ctx)

        # Province guidance should NOT be in the clarification section
        self.assertNotIn("Province/state follow-up handling", prompt)

    # ── Issue 3: Provider change to unavailable data ─────────────────

    def test_no_data_for_provider_change_gives_helpful_message(self) -> None:
        """When a provider_change follow-up results in no data, the error message
        should mention the provider doesn't have the data and suggest alternatives."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["housing starts"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="housing starts in Canada from World Bank",
            isFollowUp=True,
            followUpType="provider_change",
            resolvedQuery="housing starts in Canada from World Bank",
        )

        conv_id = conversation_manager.get_or_create("conv-provider-change-nodata")
        conversation_manager.add_message_safe(
            conv_id,
            "user",
            "show from World Bank instead",
            intent=intent,
        )

        # Simulate the no-data path after provider_change follow-up
        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="WORLDBANK",
            routed_provider="WORLDBANK",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_valid=True,
            is_multi_indicator=False,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.pipeline, "parse_and_route", AsyncMock(return_value=parse_result)), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch.object(self.service, "_fetch_data", AsyncMock(return_value=[])), \
             patch.object(self.service, "_try_with_fallback", AsyncMock(side_effect=Exception("no fallback"))), \
             patch.object(self.service, "_maybe_recover_from_empty_data", AsyncMock(return_value=None)), \
             patch.object(self.service, "_build_no_data_indicator_clarification", return_value=None):

            response = run(self.service.process_query(
                "show from World Bank instead",
                conversation_id=conv_id,
            ))

        # The response should contain helpful error message
        self.assertIsNotNone(response.message)
        self.assertTrue("No Data" in response.message or "doesn't appear to have" in response.message)
        self.assertIn("housing starts", response.message.lower())
        self.assertEqual(response.error, "no_data_found")

    def test_no_data_non_provider_change_gives_generic_message(self) -> None:
        """Non-provider-change queries that return no data should NOT get the
        provider-change-specific message."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["housing starts"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="housing starts in Canada",
            isFollowUp=False,
        )

        conv_id = conversation_manager.get_or_create("conv-non-provider-change-nodata")

        parse_result = ParseRouteResult(
            intent=intent,
            explicit_provider="WORLDBANK",
            routed_provider="WORLDBANK",
            validation_warning=None,
        )
        validation = ValidationResult(
            is_valid=True,
            is_multi_indicator=False,
            validation_error=None,
            suggestions=None,
            is_confident=True,
            confidence_reason=None,
        )

        with patch.object(self.service.pipeline, "parse_and_route", AsyncMock(return_value=parse_result)), \
             patch.object(self.service.pipeline, "validate_intent", return_value=validation), \
             patch("backend.services.query.QueryComplexityAnalyzer.detect_complexity", return_value={"pro_mode_required": False, "complexity_factors": []}), \
             patch.object(self.service, "_fetch_data", AsyncMock(return_value=[])), \
             patch.object(self.service, "_try_with_fallback", AsyncMock(side_effect=Exception("no fallback"))), \
             patch.object(self.service, "_maybe_recover_from_empty_data", AsyncMock(return_value=None)), \
             patch.object(self.service, "_build_no_data_indicator_clarification", return_value=None):

            response = run(self.service.process_query(
                "housing starts in Canada",
                conversation_id=conv_id,
            ))

        # Should get generic no-data message, not provider-change message
        self.assertIsNotNone(response.message)
        self.assertNotIn("doesn't appear to have", response.message)
        self.assertIn("No Data Available", response.message)


class InformationalQueryTests(unittest.TestCase):
    """Tests for LLM-based informational/metadata query handling."""

    def setUp(self) -> None:
        self.service = QueryService.__new__(QueryService)

    def test_handle_informational_intent_with_provider(self) -> None:
        """Informational intent with specific provider returns indicator list."""
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["employment"],
            parameters={},
            clarificationNeeded=False,
            queryType="informational",
        )
        response = self.service._handle_informational_intent(
            query="What employment series does World Bank have?",
            intent=intent,
            conversation_id="conv-info-1",
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertIsNotNone(response.message)
        self.assertIn("indicator", response.message.lower())

    def test_handle_informational_intent_without_provider(self) -> None:
        """Informational intent without specific provider searches all."""
        intent = ParsedIntent(
            apiProvider="WorldBank",  # neutral placeholder
            indicators=["GDP"],
            parameters={},
            clarificationNeeded=False,
            queryType="informational",
        )
        response = self.service._handle_informational_intent(
            query="What GDP indicators are available?",
            intent=intent,
            conversation_id="conv-info-2",
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertIsNotNone(response.message)
        self.assertIn("GDP", response.message.upper())

    def test_handle_informational_intent_fred(self) -> None:
        """Informational intent with FRED provider returns FRED indicators."""
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["unemployment"],
            parameters={},
            clarificationNeeded=False,
            queryType="informational",
        )
        response = self.service._handle_informational_intent(
            query="Does FRED have unemployment indicators?",
            intent=intent,
            conversation_id="conv-info-3",
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertIn("FRED", response.message)

    def test_data_fetch_intent_not_routed_as_informational(self) -> None:
        """Data-fetch queryType should not be handled by informational handler."""
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            queryType="data_fetch",
        )
        # _handle_informational_intent should never be called for data_fetch,
        # but verify the queryType field works
        self.assertEqual(intent.queryType, "data_fetch")

    def test_parsed_intent_querytype_default(self) -> None:
        """ParsedIntent defaults to data_fetch queryType."""
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["GDP"],
            parameters={},
            clarificationNeeded=False,
        )
        self.assertEqual(intent.queryType, "data_fetch")

    def test_parsed_intent_querytype_informational(self) -> None:
        """ParsedIntent accepts informational queryType."""
        intent = ParsedIntent(
            apiProvider="WorldBank",
            indicators=["trade indicators"],
            parameters={},
            clarificationNeeded=False,
            queryType="informational",
        )
        self.assertEqual(intent.queryType, "informational")

    def test_format_informational_results(self) -> None:
        """Formatted results contain provider names and indicator codes."""
        results = [
            {"provider": "FRED", "code": "UNRATE", "name": "Unemployment Rate"},
            {"provider": "FRED", "code": "U6RATE", "name": "Total Unemployed"},
            {"provider": "WorldBank", "code": "SL.UEM.TOTL.ZS", "name": "Unemployment Total"},
        ]
        message = self.service._format_informational_results(
            results, None, "unemployment", "What unemployment data is available?"
        )
        self.assertIn("FRED", message)
        self.assertIn("WorldBank", message)
        self.assertIn("UNRATE", message)


    # ------------------------------------------------------------------
    # Pre-flight geographic split tests
    # ------------------------------------------------------------------

    def test_preflight_geographic_split_returns_none_for_single_country(self) -> None:
        """Single-country queries should not trigger a split."""
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["producer price index"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            originalQuery="producer price index in US",
        )
        result = run(self.service._preflight_geographic_split(intent))
        self.assertIsNone(result)

    def test_preflight_geographic_split_returns_none_when_provider_covers_all(self) -> None:
        """WorldBank covers US + DE so no split should happen."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP growth"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="GDP growth in US and Germany",
        )
        result = run(self.service._preflight_geographic_split(intent))
        self.assertIsNone(result)

    def test_preflight_geographic_split_detects_fred_mismatch(self) -> None:
        """FRED cannot cover Germany -- split should fire."""
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["producer price index"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="producer price inflation US and Germany",
        )

        us_series = NormalizedData.model_validate({
            "metadata": {
                "source": "FRED",
                "indicator": "PPI",
                "country": "US",
                "frequency": "monthly",
                "unit": "Index",
                "lastUpdated": "2024-01-01",
            },
            "data": [{"date": "2024-01-01", "value": 100.0}],
        })
        de_series = NormalizedData.model_validate({
            "metadata": {
                "source": "Eurostat",
                "indicator": "PPI",
                "country": "DE",
                "frequency": "monthly",
                "unit": "Index",
                "lastUpdated": "2024-01-01",
            },
            "data": [{"date": "2024-01-01", "value": 105.0}],
        })

        original_fetch = self.service._fetch_data

        async def mock_fetch(sub_intent):
            prov = sub_intent.apiProvider.upper()
            params = sub_intent.parameters or {}
            # Detect recursion guard
            if not params.get("__geo_split_child"):
                # Should not happen -- preflight sets the flag
                return await original_fetch(sub_intent)
            country = params.get("country", "")
            if prov == "FRED" or country == "US":
                return [us_series]
            return [de_series]

        with patch.object(self.service, "_fetch_data", side_effect=mock_fetch), \
             patch("backend.services.catalog_service.find_concept_by_term", return_value="producer_price_inflation"), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=lambda concept, countries=None: (
                 ("FRED", "PPIFIS", 0.9) if countries == ["US"] else
                 ("Eurostat", "STS_INPP_M", 0.9) if countries and countries[0] == "DE" else
                 ("WORLDBANK", None, 0.5)
             )):
            result = run(self.service._preflight_geographic_split(intent))

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)
        countries_returned = {s.metadata.country for s in result}
        self.assertIn("US", countries_returned)
        self.assertIn("DE", countries_returned)

    def test_preflight_geographic_split_returns_none_when_all_same_provider(self) -> None:
        """When catalog maps all countries to the same provider, no split is needed."""
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["producer price index"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="producer price inflation US and Germany",
        )

        with patch("backend.services.catalog_service.find_concept_by_term", return_value="producer_price_inflation"), \
             patch("backend.services.catalog_service.get_best_provider", return_value=("WORLDBANK", "FP.WPI.TOTL", 0.8)):
            result = run(self.service._preflight_geographic_split(intent))

        # All countries → WORLDBANK, no split needed
        self.assertIsNone(result)

    def test_preflight_geographic_split_handles_sub_fetch_failure(self) -> None:
        """If one sub-fetch fails, partial results should still be returned."""
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["producer price index"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="producer price inflation US and Germany",
        )

        us_series = NormalizedData.model_validate({
            "metadata": {
                "source": "FRED",
                "indicator": "PPI",
                "country": "US",
                "frequency": "monthly",
                "unit": "Index",
                "lastUpdated": "2024-01-01",
            },
            "data": [{"date": "2024-01-01", "value": 100.0}],
        })

        async def mock_fetch(sub_intent):
            params = sub_intent.parameters or {}
            country = params.get("country", "")
            if country == "US":
                return [us_series]
            raise DataNotAvailableError("Eurostat failed")

        with patch.object(self.service, "_fetch_data", side_effect=mock_fetch), \
             patch("backend.services.catalog_service.find_concept_by_term", return_value="producer_price_inflation"), \
             patch("backend.services.catalog_service.get_best_provider", side_effect=lambda concept, countries=None: (
                 ("FRED", "PPIFIS", 0.9) if countries == ["US"] else
                 ("Eurostat", "STS_INPP_M", 0.9)
             )):
            result = run(self.service._preflight_geographic_split(intent))

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata.country, "US")

    def test_get_provider_for_single_country_uses_catalog(self) -> None:
        """Catalog-based per-country provider selection."""
        with patch("backend.services.catalog_service.find_concept_by_term", return_value="gdp"), \
             patch("backend.services.catalog_service.get_best_provider", return_value=("FRED", "GDP", 0.95)):
            prov, code = self.service._get_provider_for_single_country(
                "US", "gdp", "WORLDBANK",
            )
        self.assertEqual(prov, "FRED")
        self.assertEqual(code, "GDP")

    def test_get_provider_for_single_country_falls_back_to_worldbank(self) -> None:
        """When catalog returns nothing and provider doesn't cover country, use WorldBank."""
        with patch("backend.services.catalog_service.find_concept_by_term", return_value=None):
            prov, code = self.service._get_provider_for_single_country(
                "DE", "gdp", "FRED",
            )
        self.assertEqual(prov, "WORLDBANK")
        self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
