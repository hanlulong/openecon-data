from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.models import ParsedIntent
from backend.services.indicator_resolution import (
    _country_reordered_exact_title_variants,
    _extract_country_codes_from_text,
    exact_title_search_inputs,
    find_exact_provider_title_match,
    resolve_indicator_for_fetch,
    select_indicator_query_for_resolution,
)
from backend.services.indicator_selector import SelectionResult


class IndicatorResolutionTests(unittest.TestCase):
    def test_country_alias_exact_title_helpers_preserve_behavior_and_stay_fast(self) -> None:
        self.assertEqual(_extract_country_codes_from_text("US GDP"), {"US"})
        self.assertEqual(_extract_country_codes_from_text("United States GDP and Canada GDP"), {"US", "CA"})
        self.assertIn("United States GDP", _country_reordered_exact_title_variants("GDP for United States"))
        self.assertIn("GDP", exact_title_search_inputs("US GDP from FRED", "FRED"))

        start = time.perf_counter()
        for _ in range(200):
            self.assertEqual(
                _extract_country_codes_from_text("US GDP and Canada GDP for United States"),
                {"US", "CA"},
            )
        self.assertLess(time.perf_counter() - start, 1.5)

    def test_find_exact_provider_title_match_prefers_closest_worldbank_completion_variant(self) -> None:
        match = find_exact_provider_title_match(
            "Completion rate, upper secondary education, female (%)",
            "WorldBank",
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.get("code"), "UIS.CR.3.F")

    def test_statscan_selector_uses_distilled_indicator_phrase_not_full_query(self) -> None:
        svc = SimpleNamespace(
            statscan_provider=SimpleNamespace(
            ),
            _looks_like_provider_indicator_code=lambda _provider, _indicator: False,
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["number of households"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="number of households in Canada",
        )

        with patch(
            "backend.services.indicator_selector.IndicatorSelector.select",
            new=AsyncMock(
                return_value=SelectionResult(
                    code="17100159",
                    name="Estimates of the number of private households by size on July 1st",
                    source="llm_pick",
                )
            ),
        ) as select_mock:
            params = asyncio.run(
                resolve_indicator_for_fetch(
                svc,
                "STATSCAN",
                intent,
                dict(intent.parameters or {}),
                )
            )

        self.assertEqual(params.get("indicator"), "17100159")
        select_mock.assert_awaited_once()
        self.assertEqual(select_mock.await_args.args[0], "number of households")

    def test_new_path_fails_closed_when_selector_has_no_decision(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(use_outcome_decision_stage=True),
            statscan_provider=SimpleNamespace(
            ),
            _looks_like_provider_indicator_code=lambda _provider, _indicator: False,
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["number of households"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="number of households in Canada",
        )

        with patch(
            "backend.services.indicator_selector.IndicatorSelector.select",
            new=AsyncMock(
                return_value=SelectionResult(
                    code=None,
                    source="no_decision",
                )
            ),
        ):
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "STATSCAN",
                    intent,
                    dict(intent.parameters or {})
                )
            )

        self.assertEqual(params.get("indicator"), "number of households")
        self.assertEqual(params.get("__indicator_selection_status"), "no_decision")

    def test_default_path_fails_closed_when_selector_has_no_decision(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(),
            statscan_provider=SimpleNamespace(
            ),
            _looks_like_provider_indicator_code=lambda _provider, _indicator: False,
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["number of households"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="number of households in Canada",
        )

        with patch(
            "backend.services.indicator_selector.IndicatorSelector.select",
            new=AsyncMock(
                return_value=SelectionResult(
                    code=None,
                    source="no_decision",
                )
            ),
        ):
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "STATSCAN",
                    intent,
                    dict(intent.parameters or {})
                )
            )

        self.assertEqual(params.get("indicator"), "number of households")
        self.assertEqual(params.get("__indicator_selection_status"), "no_decision")

    def test_retired_shortcut_final_authority_has_no_settings_escape_hatch(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(allow_retired_indicator_shortcut_final_authority=True),
            statscan_provider=SimpleNamespace(
            ),
            _looks_like_provider_indicator_code=lambda _provider, _indicator: False,
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["retired shortcut"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="retired shortcut in Canada",
        )

        with patch(
            "backend.services.indicator_selector.IndicatorSelector.select",
            new=AsyncMock(return_value=SelectionResult(code=None, source="no_decision")),
        ):
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "STATSCAN",
                    intent,
                    dict(intent.parameters or {})
                )
            )

        self.assertEqual(params.get("indicator"), "retired shortcut")
        self.assertEqual(params.get("__indicator_selection_status"), "no_decision")

    def test_selector_llm_pick_is_not_overruled_by_rule_plausibility(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(),
            statscan_provider=SimpleNamespace(
            ),
            _looks_like_provider_indicator_code=lambda _provider, _indicator: False,
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["number of households"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            originalQuery="number of households in Canada",
        )

        with patch(
            "backend.services.indicator_selector.IndicatorSelector.select",
            new=AsyncMock(
                return_value=SelectionResult(
                    code="42100012",
                    name="Number of children in Canada",
                    source="llm_pick",
                )
            ),
        ), patch("backend.services.indicator_resolution.is_resolved_indicator_plausible") as plausibility_mock:
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "STATSCAN",
                    intent,
                    dict(intent.parameters or {})
                )
            )

        self.assertEqual(params.get("indicator"), "42100012")
        self.assertEqual(params.get("__semantic_authority"), "llm_adjudication")
        self.assertEqual(params.get("__decision_source"), "llm_pick")
        plausibility_mock.assert_not_called()

    def test_provider_looking_code_not_promoted_without_literal_user_input(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(),
            fred_provider=SimpleNamespace(),
            _looks_like_provider_indicator_code=lambda provider, indicator: (
                provider.upper() == "FRED" and str(indicator).strip().upper() == "BADCODE"
            ),
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["BADCODE"],
            parameters={"indicator": "BADCODE"},
            clarificationNeeded=False,
            originalQuery="inflation rate in the United States from FRED",
        )

        with patch(
            "backend.services.indicator_selector.IndicatorSelector.select",
            new=AsyncMock(
                return_value=SelectionResult(
                    code="CPIAUCSL",
                    name="Consumer Price Index for All Urban Consumers",
                    source="llm_pick",
                )
            ),
        ):
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "FRED",
                    intent,
                    dict(intent.parameters or {})
                )
            )

        self.assertEqual(params.get("indicator"), "CPIAUCSL")
        self.assertEqual(params.get("__semantic_authority"), "llm_adjudication")
        self.assertEqual(params.get("__decision_source"), "llm_pick")

    def test_literal_provider_code_remains_mechanical_exact_input(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(),
            fred_provider=SimpleNamespace(),
            _looks_like_provider_indicator_code=lambda provider, indicator: (
                provider.upper() == "FRED" and str(indicator).strip().upper() == "CPIAUCSL"
            ),
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["CPIAUCSL"],
            parameters={"indicator": "CPIAUCSL"},
            clarificationNeeded=False,
            originalQuery="FRED CPIAUCSL",
        )

        with patch("backend.services.indicator_selector.IndicatorSelector.select") as select_mock:
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "FRED",
                    intent,
                    dict(intent.parameters or {})
                )
            )

        self.assertEqual(params.get("indicator"), "CPIAUCSL")
        self.assertEqual(params.get("__semantic_authority"), "exact_user_input")
        self.assertEqual(params.get("__decision_source"), "exact_code")
        select_mock.assert_not_called()

    def test_provider_lock_does_not_force_noisy_query_for_provider_code(self) -> None:
        svc = SimpleNamespace(
            _looks_like_provider_indicator_code=lambda provider, indicator: (
                provider.upper() == "STATSCAN" and str(indicator).isdigit()
            ),
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100375"],
            parameters={
                "country": "CA",
                "indicator": "14100375",
                "__semantic_provider_locked": True,
                "__semantic_indicator_label": "unemployment rate",
            },
            clarificationNeeded=False,
            originalQuery="unemployment rate in Canada in 2021",
        )

        self.assertEqual(
            select_indicator_query_for_resolution(svc, intent),
            "unemployment rate",
        )

    def test_provider_locked_exact_title_miss_falls_through_to_selector(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(),
            _looks_like_provider_indicator_code=lambda _provider, _indicator: False,
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["total credit"],
            parameters={
                "country": "JP",
                "__semantic_provider_locked": True,
            },
            clarificationNeeded=False,
            originalQuery="Japan Total credit from BIS",
        )

        with patch(
            "backend.services.indicator_resolution.looks_like_exact_provider_title_match",
            return_value=True,
        ), patch(
            "backend.services.indicator_resolution.find_exact_provider_title_match",
            return_value=None,
        ), patch(
            "backend.services.indicator_selector.IndicatorSelector.select",
            new=AsyncMock(
                return_value=SelectionResult(
                    code="WS_TC",
                    name="Total credit",
                    source="llm_pick",
                )
            ),
        ) as select_mock:
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "BIS",
                    intent,
                    dict(intent.parameters or {}),
                )
            )

        self.assertEqual(params.get("indicator"), "WS_TC")
        self.assertEqual(params.get("__semantic_authority"), "llm_adjudication")
        self.assertEqual(params.get("__decision_source"), "llm_pick")
        select_mock.assert_awaited_once()
        self.assertEqual(select_mock.await_args.args[0], "total credit")
        self.assertEqual(select_mock.await_args.args[1], "BIS")

    def test_unlocked_exact_title_miss_still_fails_closed_without_selector(self) -> None:
        svc = SimpleNamespace(
            settings=SimpleNamespace(),
            _looks_like_provider_indicator_code=lambda _provider, _indicator: False,
            _verify_semantic_discriminators=lambda *_args, **_kwargs: True,
        )
        intent = ParsedIntent(
            apiProvider="BIS",
            indicators=["total credit"],
            parameters={"country": "JP"},
            clarificationNeeded=False,
            originalQuery="Japan Total credit from BIS",
        )

        with patch(
            "backend.services.indicator_resolution.looks_like_exact_provider_title_match",
            return_value=True,
        ), patch(
            "backend.services.indicator_resolution.find_exact_provider_title_match",
            return_value=None,
        ), patch("backend.services.indicator_selector.IndicatorSelector.select") as select_mock:
            params = asyncio.run(
                resolve_indicator_for_fetch(
                    svc,
                    "BIS",
                    intent,
                    dict(intent.parameters or {}),
                )
            )

        self.assertEqual(params.get("__indicator_selection_status"), "exact_title_unresolved")
        select_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
