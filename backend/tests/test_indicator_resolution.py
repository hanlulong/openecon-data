from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.models import ParsedIntent
from backend.services.indicator_resolution import (
    find_exact_provider_title_match,
    resolve_indicator_for_fetch,
    select_indicator_query_for_resolution,
)
from backend.services.indicator_selector import SelectionResult


class IndicatorResolutionTests(unittest.TestCase):
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

    def test_implausible_selector_pick_skips_retired_fallback(self) -> None:
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
        ), patch(
            "backend.services.indicator_resolution.is_resolved_indicator_plausible",
            return_value=False,
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
        self.assertEqual(params.get("__indicator_selection_status"), "llm_pick")
        self.assertEqual(params.get("__indicator_rejection_reason"), "implausible_llm_pick")

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


if __name__ == "__main__":
    unittest.main()
