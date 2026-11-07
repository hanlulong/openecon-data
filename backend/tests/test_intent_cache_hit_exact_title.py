"""Intent-cache HIT path must not re-run the exact-title builder needlessly.

A cached intent the exact-title builder itself produced for the same query
text already embodies the builder's outcome, so re-probing every provider on
the HIT path only re-paid the shortcut's latency.  Any other cached intent (an
LLM parse, or a case-variant of the text sharing the case-insensitive cache
key) must still get the deterministic refresh check.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.models import ParsedIntent
from backend.services.query import QueryService
from backend.services.query_pipeline import ParseRouteResult
from backend.tests.test_query_service import sample_series
from backend.tests.utils import run

_QUERY = "Real Gross Domestic Product from FRED"


def _title_shortcut_intent(original_query: str) -> ParsedIntent:
    return ParsedIntent(
        apiProvider="FRED",
        indicators=["Real Gross Domestic Product"],
        parameters={
            "indicator": "GDPC1",
            "__semantic_indicator_label": "Real Gross Domestic Product",
            "__semantic_provider_locked": True,
            "__exact_indicator_title_match": True,
        },
        clarificationNeeded=False,
        confidence=0.99,
        recommendedChartType="line",
        queryType="data_fetch",
        originalQuery=original_query,
    )


def _llm_intent() -> ParsedIntent:
    return ParsedIntent(
        apiProvider="FRED",
        indicators=["GDP"],
        parameters={"indicator": "GDPC1"},
        clarificationNeeded=False,
        confidence=0.8,
        queryType="data_fetch",
        originalQuery=_QUERY,
    )


class IntentCacheHitExactTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")

    def _run_hit(self, cached_intent: ParsedIntent, query: str = _QUERY):
        cached = ParseRouteResult(
            intent=cached_intent,
            explicit_provider="FRED",
            routed_provider="FRED",
            validation_warning=None,
        )
        with patch("backend.services.query._get_cached_parse_result", return_value=cached), \
             patch("backend.services.query._put_cached_parse_result"), \
             patch.object(self.service, "_build_explicit_provider_code_intent", return_value=None), \
             patch.object(
                 self.service,
                 "_build_exact_indicator_title_intent",
                 return_value=_title_shortcut_intent(query),
             ) as builder, \
             patch.object(
                 self.service.pipeline,
                 "parse_and_route",
                 new_callable=AsyncMock,
                 side_effect=AssertionError("LLM parse should be skipped on a cache HIT"),
             ), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()):
            response = run(self.service.process_query(query))
        return builder, response

    def test_hit_on_builder_produced_intent_skips_builder(self) -> None:
        builder, response = self._run_hit(_title_shortcut_intent(_QUERY))
        builder.assert_not_called()
        self.assertFalse(response.clarificationNeeded)

    def test_hit_on_llm_parsed_intent_still_runs_builder(self) -> None:
        builder, _response = self._run_hit(_llm_intent())
        builder.assert_called()

    def test_hit_on_case_variant_text_still_runs_builder(self) -> None:
        # Cache keys are case-insensitive; a builder intent stored for another
        # spelling is not proof of this spelling's outcome.
        builder, _response = self._run_hit(_title_shortcut_intent(_QUERY.lower()))
        builder.assert_called()


if __name__ == "__main__":
    unittest.main()
