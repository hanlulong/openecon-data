from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.models import GeneratedFile, NormalizedData, ParsedIntent, QueryResponse
from backend.routing.country_resolver import CountryResolver
from backend.routing.unified_router import RoutingDecision
from backend.services.cache import cache_service
from backend.services.conversation import conversation_manager
from backend.services.query_pipeline import ParseRouteResult, ValidationResult
from backend.services.query import QueryService
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


class QueryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        cache_service.clear()
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")

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

    def test_is_resolved_indicator_plausible_rejects_imf_cpi_for_producer_price_query(self) -> None:
        self.assertFalse(
            self.service._is_resolved_indicator_plausible(  # pylint: disable=protected-access
                provider="IMF",
                indicator_query="producer price inflation trend in us and germany",
                resolved_code="PCPIPCH",
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
        self.assertIn("IMF", joined)
        self.assertIn("WorldBank", joined)
        self.assertIsNotNone(clarification.clarificationOptions)
        options = clarification.clarificationOptions or []
        self.assertGreaterEqual(len(options), 2)
        self.assertTrue(all(option.id and option.value for option in options))
        self.assertIn("IMF", {option.provider for option in options})
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

    def test_build_semantic_ambiguity_clarification_for_broad_employment_query(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-semantic-employment")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["employment"],
            parameters={"countries": ["US", "DE"]},
            clarificationNeeded=False,
            originalQuery="total employment in G20",
        )

        clarification = self.service._build_semantic_ambiguity_clarification(  # pylint: disable=protected-access
            conversation_id=conv_id,
            query="total employment in G20",
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
                "number employed",
                "employment rate",
                "employment-to-population ratio",
            ],
        )
        self.assertEqual(options[1].value, "employment rate in G20")
        pending = conversation_manager.get_pending_semantic_clarification(conv_id)
        self.assertIsNotNone(pending)

    def test_build_group_scope_clarification_for_region_query(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-group-scope")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["employment rate"],
            parameters={"countries": ["AR", "AU", "BR"]},
            clarificationNeeded=False,
            originalQuery="employment rate in G20",
        )

        clarification = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id=conv_id,
            query="employment rate in G20",
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
        self.assertEqual(options[0].value, "employment rate across G20 member countries")
        self.assertEqual(options[1].value, "employment rate for the G20 group as a whole")
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
             patch("backend.services.query.get_indicator_resolver", return_value=Mock(resolve=Mock(return_value=_Resolved()))):
            clarification = self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                conversation_id=conv_id,
                query="gdp to debt ratio in china",
                intent=intent,
                explicit_provider=None,
                is_multi_indicator=False,
                processing_steps=None,
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
            clarification = self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                conversation_id="conv-prefetch-duplicate-labels",
                query="imports share of gdp in china",
                intent=intent,
                explicit_provider=None,
                is_multi_indicator=False,
                processing_steps=None,
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
            clarification = self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                conversation_id="conv-prefetch-strong-primary",
                query="imports share of gdp in china",
                intent=intent,
                explicit_provider=None,
                is_multi_indicator=False,
                processing_steps=None,
            )

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

    def test_try_resolve_pending_semantic_clarification_rewrites_short_custom_reply(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-semantic-reply")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        conversation_manager.set_pending_semantic_clarification(
            conv_id,
            {
                "kind": "employment_metric",
                "concept_label": "employment",
                "original_query": "total employment in G20",
                "question_lines": ["Choose the metric you want:"],
                "options": [
                    {
                        "id": "1",
                        "label": "number employed",
                        "value": "number employed in G20",
                    },
                    {
                        "id": "2",
                        "label": "employment rate",
                        "value": "employment rate in G20",
                    },
                ],
            },
        )

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )
        with patch.object(self.service, "process_query", AsyncMock(return_value=expected_response)) as process_query:
            response = run(
                self.service._try_resolve_pending_semantic_clarification(  # pylint: disable=protected-access
                    query="employment rate",
                    conversation_id=conv_id,
                    auto_pro_mode=False,
                    use_orchestrator=False,
                    allow_orchestrator=True,
                    tracker=None,
                )
            )

        self.assertEqual(response, expected_response)
        process_query.assert_awaited_once_with(
            query="employment rate in G20",
            conversation_id=conv_id,
            auto_pro_mode=False,
            use_orchestrator=False,
            allow_orchestrator=True,
        )
        self.assertIsNone(conversation_manager.get_pending_semantic_clarification(conv_id))

    def test_try_resolve_pending_semantic_clarification_matches_group_scope_keyword(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-group-scope-reply")
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        conversation_manager.set_pending_semantic_clarification(
            conv_id,
            {
                "kind": "group_scope",
                "region": "G20",
                "original_query": "employment rate in G20",
                "question_lines": ["Choose the scope you want:"],
                "options": [
                    {
                        "id": "1",
                        "label": "compare member countries",
                        "value": "compare employment rate across G20 member countries",
                    },
                    {
                        "id": "2",
                        "label": "one overall group value (aggregate/average if available)",
                        "value": "employment rate for the G20 group as a whole",
                    },
                ],
            },
        )

        expected_response = QueryResponse(
            conversationId=conv_id,
            clarificationNeeded=False,
            message="ok",
        )
        with patch.object(self.service, "process_query", AsyncMock(return_value=expected_response)) as process_query:
            response = run(
                self.service._try_resolve_pending_semantic_clarification(  # pylint: disable=protected-access
                    query="aggregate",
                    conversation_id=conv_id,
                    auto_pro_mode=False,
                    use_orchestrator=False,
                    allow_orchestrator=True,
                    tracker=None,
                )
            )

        self.assertEqual(response, expected_response)
        process_query.assert_awaited_once_with(
            query="employment rate for the G20 group as a whole",
            conversation_id=conv_id,
            auto_pro_mode=False,
            use_orchestrator=False,
            allow_orchestrator=True,
        )

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

    def test_process_query_returns_semantic_clarification_before_parse(self) -> None:
        conv_id = "conv-process-semantic"
        conversation_manager.clear_pending_semantic_clarification(conv_id)

        with patch.object(self.service.pipeline, "parse_and_route", side_effect=AssertionError("parse should not run")):
            response = run(self.service.process_query("total employment in G20", conversation_id=conv_id))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNone(response.intent)
        self.assertIsNotNone(response.clarificationOptions)
        options = response.clarificationOptions or []
        self.assertEqual(options[0].label, "number employed")
        self.assertEqual(options[1].value, "employment rate in G20")

    def test_process_query_returns_group_scope_clarification_before_parse(self) -> None:
        conv_id = "conv-process-group-scope"
        conversation_manager.clear_pending_semantic_clarification(conv_id)

        with patch.object(self.service.pipeline, "parse_and_route", side_effect=AssertionError("parse should not run")):
            response = run(self.service.process_query("employment rate in G20", conversation_id=conv_id))

        self.assertTrue(response.clarificationNeeded)
        self.assertIsNone(response.intent)
        self.assertIsNotNone(response.clarificationOptions)
        options = response.clarificationOptions or []
        self.assertEqual(options[0].label, "compare member countries")
        self.assertEqual(options[1].value, "employment rate for the G20 group as a whole")

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

        self.assertEqual(fetch_mock.call_args.kwargs.get("indicator"), "imports as % of GDP")

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

        with patch("backend.services.query.ProviderRouter.route_provider", side_effect=AssertionError("legacy baseline should not run")):
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

        with patch.object(self.service, "_select_indicator_query_for_resolution", return_value="real effective exchange rate"), \
             patch.object(self.service, "_get_fallback_providers", return_value=["WORLDBANK"]), \
             patch.object(self.service, "_fetch_data", side_effect=_fake_fetch_data), \
             patch.object(self.service, "_is_fallback_relevant", return_value=True):
            _ = run(
                self.service._try_with_fallback(  # pylint: disable=protected-access
                    intent,
                    DataNotAvailableError("primary failed"),
                )
            )

        fallback_intent = captured_intent["intent"]
        self.assertEqual(fallback_intent.indicators, ["real effective exchange rate"])

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


if __name__ == "__main__":
    unittest.main()
