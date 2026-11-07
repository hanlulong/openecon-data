"""Intent-cache HIT path must not re-run the exact-title builder needlessly.

The exact-title builder is a deterministic function of the query text and the
catalog.  A cached intent the builder itself produced for the same query text,
stamped with the catalog snapshot token it read, already embodies the
builder's outcome while that token is unchanged, so re-probing every provider
on the HIT path only re-paid the shortcut's latency.  Any other cached intent
(an LLM parse, a case-variant of the text sharing the case-insensitive cache
key, an entry stamped under another or an unobservable catalog snapshot) must
still get the deterministic refresh check.
"""

from __future__ import annotations

import unittest
from typing import Optional
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

    def _run_hit(
        self,
        cached_intent: ParsedIntent,
        query: str = _QUERY,
        *,
        cached_version: Optional[str] = "snapshot-1",
        current_version: Optional[str] = "snapshot-1",
    ):
        cached = ParseRouteResult(
            intent=cached_intent,
            explicit_provider="FRED",
            routed_provider="FRED",
            validation_warning=None,
            catalog_version=cached_version,
        )
        with patch("backend.services.query._get_cached_parse_result", return_value=cached), \
             patch("backend.services.query._put_cached_parse_result"), \
             patch.object(self.service, "_build_explicit_provider_code_intent", return_value=None), \
             patch.object(self.service, "_exact_title_catalog_version", return_value=current_version), \
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

    def test_hit_after_catalog_change_still_runs_builder(self) -> None:
        builder, _response = self._run_hit(
            _title_shortcut_intent(_QUERY),
            cached_version="snapshot-1",
            current_version="snapshot-2",
        )
        builder.assert_called()

    def test_hit_with_unobservable_catalog_still_runs_builder(self) -> None:
        builder, _response = self._run_hit(
            _title_shortcut_intent(_QUERY),
            cached_version=None,
            current_version=None,
        )
        builder.assert_called()

    def test_hit_on_unstamped_entry_still_runs_builder(self) -> None:
        builder, _response = self._run_hit(_title_shortcut_intent(_QUERY), cached_version=None)
        builder.assert_called()

    def test_hit_on_llm_parsed_intent_still_runs_builder(self) -> None:
        builder, _response = self._run_hit(_llm_intent())
        builder.assert_called()

    def test_hit_on_case_variant_text_still_runs_builder(self) -> None:
        # Cache keys are case-insensitive; a builder intent stored for another
        # spelling is not proof of this spelling's outcome.
        builder, _response = self._run_hit(_title_shortcut_intent(_QUERY.lower()))
        builder.assert_called()

    def test_miss_stamps_builder_result_with_pre_build_snapshot(self) -> None:
        catalog = {"version": "snapshot-before-build"}
        stored: list[ParseRouteResult] = []

        def _builder(query: str) -> ParsedIntent:
            # The catalog changes while the builder runs: the stamp must be
            # the token read BEFORE it, so a later hit re-checks.
            catalog["version"] = "snapshot-after-build"
            return _title_shortcut_intent(query)

        with patch("backend.services.query._get_cached_parse_result", return_value=None), \
             patch(
                 "backend.services.query._put_cached_parse_result",
                 side_effect=lambda _key, result: stored.append(result),
             ), \
             patch.object(self.service, "_build_explicit_provider_code_intent", return_value=None), \
             patch.object(
                 self.service,
                 "_exact_title_catalog_version",
                 side_effect=lambda: catalog["version"],
             ), \
             patch.object(self.service, "_build_exact_indicator_title_intent", side_effect=_builder), \
             patch.object(
                 self.service.pipeline,
                 "parse_and_route",
                 new_callable=AsyncMock,
                 side_effect=AssertionError("the title shortcut should answer this parse"),
             ), \
             patch.object(self.service, "_get_from_cache", return_value=None), \
             patch.object(self.service.fred_provider, "fetch_series", return_value=sample_series()):
            run(self.service.process_query(_QUERY))

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].catalog_version, "snapshot-before-build")

    def test_real_lookup_exposes_a_stable_snapshot_token(self) -> None:
        first = self.service._exact_title_catalog_version()
        second = self.service._exact_title_catalog_version()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
