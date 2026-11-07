"""Geography-only follow-ups on FRED must keep the carried concept.

Mechanism under test (not a single query): FRED country-scope discovery is a
provider-specific dispatch rewrite.  It must

* search with ``<country> <carried concept>`` on a follow-up turn -- the raw
  follow-up utterance ("compare with Canada", "那日本呢", "add the UK") is a
  geography DELTA, never the concept;
* never rewrite the provider-neutral semantic keys (``__semantic_indicator_label``
  and ``indicator``) that conversation-state persistence and cross-provider
  fallback read;
* keep its keys dispatch-scoped, so a coverage backfill for another country
  (the U.S. leg of a comparison) fetches the verified request instead of
  re-running the non-U.S. discovery.

Graded on concept + country (+ honest failure), never on a pinned series id.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from backend.models import NormalizedData, ParsedIntent
from backend.services.cache import cache_service
from backend.services.conversation import conversation_manager
from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    materialize_intent,
    merge_state,
)
from backend.services import data_fetcher
from backend.services.data_fetcher import (
    _normalize_known_country_to_iso2,
    _prepare_fred_country_scope_params,
    _raise_if_fred_country_scope_unsupported,
    _text_targets_country,
)
from backend.services.query import QueryService
from backend.tests.utils import run
from backend.utils.retry import DataNotAvailableError


_COUNTRY_NAMES = {"CA": "Canada", "JP": "Japan", "DE": "Germany", "GB": "United Kingdom", "MX": "Mexico"}

# (turn-1 concept label, verified FRED code, turn-2 delta utterance, delta)
_ADDITIVE_CASES = [
    ("unemployment rate", "UNRATE", "compare with Canada", ["CA"]),
    ("unemployment rate", "UNRATE", "和加拿大比较", ["CA"]),
    ("gross domestic product", "GDP", "and Germany", ["DE"]),
    ("total nonfarm payroll employment", "PAYEMS", "add the UK", ["United Kingdom"]),
]
_SWITCH_CASES = [
    ("consumer price index", "CPIAUCSL", "what about Japan", ["JP"]),
    ("consumer price index", "CPIAUCSL", "那日本呢", ["JP"]),
]


def _series(*, indicator: str, country: str, series_id: str) -> NormalizedData:
    return NormalizedData.model_validate(
        {
            "metadata": {
                "source": "FRED",
                "indicator": indicator,
                "country": country,
                "frequency": "monthly",
                "unit": "Percent",
                "lastUpdated": "2026-10-01",
                "seriesId": series_id,
                "apiUrl": "https://example.com",
            },
            "data": [
                {"date": "2025-01-01", "value": 4.0},
                {"date": "2025-02-01", "value": 4.1},
            ],
        }
    )


def _fake_fred_catalog(calls: list[dict[str, Any]], *, has_non_us_series: bool = True):
    """Stub FRED search: a country-titled query returns that country's series;
    a verified code returns the U.S. series.  No codes or concepts are mapped --
    the stub only reflects whether the search text names a country."""

    def _fetch(params: dict[str, Any]) -> NormalizedData:
        calls.append(dict(params))
        text = str(params.get("indicator") or "")
        if has_non_us_series:
            for iso2, name in _COUNTRY_NAMES.items():
                if _text_targets_country(text, iso2):
                    return _series(indicator=f"{text} (FRED title for {name})", country=name, series_id=f"STUB_{iso2}")
        return _series(indicator="U.S. national series", country="US", series_id=text)

    return _fetch


def _turn1_state(label: str, code: str, utterance: str) -> ConversationState:
    return ConversationState(
        indicator=label,
        resolved_indicator_code=code,
        last_indicators_resolved=[code],
        country="United States",
        provider="FRED",
        routed_provider="FRED",
        original_query=utterance,
        turn_number=0,
    )


def _turn2_intent(label: str, code: str, utterance: str, *, added=None, changed=None) -> tuple[ConversationState, ParsedIntent]:
    state = _turn1_state(label, code, f"US {label} last 5 years")
    delta = FollowUpDelta(
        added_countries=added,
        changed_countries=changed,
        raw_query=utterance,
        delta_type="additive_country" if added else "country_switch",
    )
    merged = merge_state(state, delta)
    intent = materialize_intent(merged)
    # Mirror QueryService's delta path (query.py): a geography-only delta does
    # not change the indicator, so the carried code is reused as-is.
    intent.parameters["__delta_resolved"] = True
    intent.parameters["__delta_indicator_changed"] = False
    return merged, intent


class FredDiscoveryInvariantTests(unittest.TestCase):
    """Unit invariants on the provider-prep chokepoint."""

    def _prepare(self, params: dict[str, Any], intent: ParsedIntent) -> dict[str, Any]:
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            return _prepare_fred_country_scope_params({}, params, intent)

    def test_semantic_keys_are_never_rewritten(self) -> None:
        for label, code, utterance, added in _ADDITIVE_CASES:
            for follow_up in (True, False):
                with self.subTest(utterance=utterance, follow_up=follow_up):
                    params = {
                        "countries": ["United States", added[0]],
                        "indicator": code,
                        "__semantic_indicator_label": label,
                        "__original_query": utterance,
                    }
                    intent = ParsedIntent(
                        apiProvider="FRED", indicators=[code], parameters=dict(params), clarificationNeeded=False,
                        originalQuery=utterance, isFollowUp=follow_up,
                    )
                    prepared = self._prepare(dict(params), intent)
                    self.assertEqual(prepared["__semantic_indicator_label"], label)
                    self.assertEqual(prepared["indicator"], code)

    def test_follow_up_discovery_searches_carried_concept_plus_country(self) -> None:
        for label, code, utterance, added in _ADDITIVE_CASES:
            with self.subTest(utterance=utterance):
                _, intent = _turn2_intent(label, code, utterance, added=added)
                params = dict(intent.parameters or {})
                params["__original_query"] = utterance
                prepared = self._prepare(params, intent)
                query = prepared["__fred_discovery_query"]
                iso2 = _normalize_known_country_to_iso2(added[0])
                self.assertTrue(prepared["__fred_country_scope_discovery"])
                self.assertEqual(prepared["__fred_requested_country"], iso2)
                self.assertIn(label, query)
                self.assertTrue(_text_targets_country(query, iso2))
                self.assertNotIn(utterance, query)
                self.assertEqual(prepared["__semantic_indicator_label"], label)

    def test_country_switch_follow_up_discovery_is_utterance_independent(self) -> None:
        for label, code, utterance, changed in _SWITCH_CASES:
            with self.subTest(utterance=utterance):
                merged, intent = _turn2_intent(label, code, utterance, changed=changed)
                # REPLACE on a geography-encoded provider drops the U.S. code.
                self.assertIsNone(merged.resolved_indicator_code)
                self.assertEqual(intent.parameters["__semantic_indicator_label"], label)
                # Simulate resolution re-picking a U.S.-titled code.
                params = dict(intent.parameters or {}, indicator=code, __original_query=utterance)
                prepared = self._prepare(params, intent)
                self.assertEqual(prepared["__fred_discovery_query"], f"Japan {label}")
                self.assertEqual(prepared["__semantic_indicator_label"], label)

    def test_follow_up_without_concept_does_not_search_with_utterance(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED", indicators=["UNRATE"], clarificationNeeded=False,
            parameters={"countries": ["United States", "CA"], "indicator": "UNRATE"},
            originalQuery="compare with Canada", isFollowUp=True,
        )
        params = dict(intent.parameters, __original_query="compare with Canada")
        prepared = self._prepare(params, intent)
        # No discovery -> the fail-closed guard + fallback own the request.
        self.assertNotIn("__fred_country_scope_discovery", prepared)
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            with self.assertRaises(DataNotAvailableError):
                _raise_if_fred_country_scope_unsupported({}, prepared, intent)

    def test_first_turn_discovery_text_is_unchanged(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED", indicators=["GDP"], clarificationNeeded=False,
            parameters={"country": "CA", "indicator": "GDP", "__semantic_indicator_label": "GDP"},
            originalQuery="Canada GDP from FRED",
        )
        prepared = self._prepare(dict(intent.parameters), intent)
        self.assertEqual(prepared["__fred_discovery_query"], "Canada GDP")
        self.assertEqual(prepared["indicator"], "GDP")
        self.assertEqual(prepared["__semantic_indicator_label"], "GDP")

    def test_discovery_keys_are_dispatch_scoped_to_the_requested_country(self) -> None:
        _, intent = _turn2_intent("unemployment rate", "UNRATE", "compare with Canada", added=["CA"])
        params = dict(intent.parameters, __exact_provider_code_match=True)
        prepared = self._prepare(params, intent)
        self.assertTrue(prepared["__fred_country_scope_discovery"])
        self.assertNotIn("__exact_provider_code_match", prepared)
        # Same scope (dispatch-level re-entry) keeps the discovery.
        self.assertIs(self._prepare(prepared, intent), prepared)

        # Coverage child for the U.S. leg: restore the verified request.
        us_child = {k: v for k, v in prepared.items() if k != "countries"}
        us_child["country"] = "US"
        restored = self._prepare(us_child, intent)
        self.assertEqual(restored["indicator"], "UNRATE")
        self.assertTrue(restored["__exact_provider_code_match"])
        for key in ("__fred_country_scope_discovery", "__fred_requested_country", "__fred_discovery_query", "__fred_pre_discovery"):
            self.assertNotIn(key, restored)

        # Child for a different non-U.S. country re-derives its own discovery.
        mx_child = dict(us_child, country="Mexico")
        rediscovered = self._prepare(mx_child, intent)
        self.assertEqual(rediscovered["__fred_requested_country"], "MX")
        self.assertEqual(rediscovered["__fred_discovery_query"], "Mexico unemployment rate")

    def test_united_kingdom_is_a_real_country_for_the_fred_scope_guard(self) -> None:
        for spelling in ("United Kingdom", "UK", "GB"):
            with self.subTest(spelling=spelling):
                self.assertEqual(_normalize_known_country_to_iso2(spelling), "GB")
        intent = ParsedIntent(
            apiProvider="FRED", indicators=["UNRATE"], clarificationNeeded=False,
            parameters={"country": "United Kingdom", "indicator": "UNRATE"},
            originalQuery="UK unemployment from FRED",
        )
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            with self.assertRaises(DataNotAvailableError):
                _raise_if_fred_country_scope_unsupported({}, dict(intent.parameters), intent)
        self.assertIsNone(_normalize_known_country_to_iso2("1W"))


class FredFollowUpFetchTests(unittest.TestCase):
    """Turn-2 fetch through the real _fetch_data + coverage + persistence."""

    def setUp(self) -> None:
        cache_service.clear()
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
        self.service.settings.use_outcome_decision_stage = False
        self.service.settings.use_post_fetch_semantic_judge = False
        self.service.settings.use_staged_state_commit = False

    def _patches(self, calls, **kwargs):
        return (
            patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None),
            patch.object(self.service, "_save_to_cache", new_callable=AsyncMock),
            patch.object(self.service.fred_provider, "fetch_series", side_effect=_fake_fred_catalog(calls, **kwargs)),
            patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False),
        )

    def test_additive_follow_up_returns_both_countries_with_concept_label(self) -> None:
        for label, code, utterance, added in _ADDITIVE_CASES:
            with self.subTest(utterance=utterance):
                cache_service.clear()
                merged, intent = _turn2_intent(label, code, utterance, added=added)
                iso2 = _normalize_known_country_to_iso2(added[0])
                calls: list[dict[str, Any]] = []
                p1, p2, p3, p4 = self._patches(calls)
                with p1, p2, p3, p4:
                    data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access
                    data, _warning = run(self.service._maybe_improve_country_coverage(utterance, intent, data))  # pylint: disable=protected-access

                searched = [str(call.get("indicator") or "") for call in calls]
                self.assertTrue(searched)
                self.assertFalse(any(utterance in text for text in searched), searched)
                self.assertTrue(any(label in text and _text_targets_country(text, iso2) for text in searched), searched)
                # Both requested countries are answered; the U.S. leg used the
                # verified request, not the non-U.S. discovery text.
                countries = {_normalize_known_country_to_iso2(series.metadata.country) for series in data}
                self.assertEqual(countries, {"US", iso2})
                us_calls = [call for call in calls if call.get("country") == "US"]
                self.assertTrue(us_calls)
                self.assertTrue(all(not call.get("__fred_country_scope_discovery") for call in us_calls))
                self.assertTrue(all(call.get("indicator") == code for call in us_calls))
                # The provider-neutral semantic keys survive the FRED dispatch.
                self.assertEqual(intent.parameters["__semantic_indicator_label"], label)
                self.assertEqual(intent.parameters["indicator"], code)

                # Persisted conversation state carries the concept, not the
                # utterance or a country-prefixed search string.
                conv_id = conversation_manager.get_or_create(f"conv-fred-delta-{code}-{iso2}-{len(utterance)}")
                self.service._persist_verified_conversation_state(conv_id, merged, data, intent=intent)  # pylint: disable=protected-access
                self.assertEqual(merged.indicator, label)
                self.assertEqual(merged.resolved_indicator_code, code)

    def test_turn_three_switch_after_comparison_searches_one_country(self) -> None:
        merged, intent = _turn2_intent("unemployment rate", "UNRATE", "compare with Canada", added=["CA"])
        calls: list[dict[str, Any]] = []
        p1, p2, p3, p4 = self._patches(calls)
        with p1, p2, p3, p4:
            data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access
            data, _ = run(self.service._maybe_improve_country_coverage("compare with Canada", intent, data))  # pylint: disable=protected-access
            conv_id = conversation_manager.get_or_create("conv-fred-delta-turn3")
            self.service._persist_verified_conversation_state(conv_id, merged, data, intent=intent)  # pylint: disable=protected-access

            turn3 = merge_state(merged, FollowUpDelta(changed_countries=["JP"], raw_query="what about Japan"))
            intent3 = materialize_intent(turn3)
            self.assertEqual(intent3.parameters["__semantic_indicator_label"], "unemployment rate")
            params3 = dict(intent3.parameters, indicator="UNRATE", __original_query="what about Japan")
            prepared = _prepare_fred_country_scope_params({}, params3, intent3)
        query = prepared["__fred_discovery_query"]
        self.assertEqual(query, "Japan unemployment rate")
        self.assertFalse(_text_targets_country(query, "CA"))

    def test_missing_non_us_series_fails_closed_with_clean_fallback_label(self) -> None:
        _, intent = _turn2_intent("unemployment rate", "UNRATE", "compare with Canada", added=["CA"])
        calls: list[dict[str, Any]] = []
        p1, p2, p3, p4 = self._patches(calls, has_non_us_series=False)
        with p1, p2, p3, p4:
            with self.assertRaises(DataNotAvailableError):
                run(self.service._fetch_data(intent))  # pylint: disable=protected-access
        # FRED never served U.S. data for the Canada leg, and the fallback's
        # provider-neutral query is the carried concept, not the utterance.
        self.assertEqual(intent.parameters["__semantic_indicator_label"], "unemployment rate")
        fallback_query = self.service._select_indicator_query_for_resolution(intent)  # pylint: disable=protected-access
        self.assertIn("unemployment rate", fallback_query)
        self.assertNotIn("compare with", fallback_query)

    def test_first_turn_non_us_discovery_persists_concept_label(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["UNRATE"],
            parameters={
                "country": "CA",
                "indicator": "UNRATE",
                "__semantic_indicator_label": "unemployment rate",
                "__exact_provider_code_match": True,
            },
            clarificationNeeded=False,
            originalQuery="Canada unemployment rate last 5 years from FRED",
        )
        calls: list[dict[str, Any]] = []
        p1, p2, p3, p4 = self._patches(calls)
        with p1, p2, p3, p4:
            data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access
        self.assertEqual(_normalize_known_country_to_iso2(data[0].metadata.country), "CA")
        # First-turn search text still derives from the utterance (the dc0b1fa
        # behaviour the design gate kept byte-identical; time words included).
        # Assert only what matters here: it names the country and the concept.
        self.assertTrue(_text_targets_country(calls[-1]["indicator"], "CA"))
        self.assertIn("unemployment rate", calls[-1]["indicator"])
        # The persisted concept is the semantic label, NOT that search text.
        state = ConversationState(indicator="", country="CA", provider="FRED")
        self.service._persist_verified_conversation_state(  # pylint: disable=protected-access
            conversation_manager.get_or_create("conv-fred-first-turn-label"), state, data, intent=intent,
        )
        self.assertEqual(state.indicator, "unemployment rate")

    def test_process_query_geography_delta_materializes_carried_concept(self) -> None:
        """Pin the state layer: a geography-only delta after a FRED base series
        keeps the concept label and verified code, never the utterance."""
        from backend.models import QueryResponse

        for utterance in ("compare with Canada",):
            with self.subTest(utterance=utterance):
                conv_id = conversation_manager.get_or_create(f"conv-fred-geo-delta-{len(utterance)}")
                conversation_manager.add_message_safe(
                    conv_id,
                    "user",
                    "US unemployment rate last 5 years",
                    intent=ParsedIntent(
                        apiProvider="FRED",
                        indicators=["UNRATE"],
                        parameters={"country": "US", "indicator": "UNRATE", "__semantic_indicator_label": "unemployment rate"},
                        clarificationNeeded=False,
                        originalQuery="US unemployment rate last 5 years",
                    ),
                )
                conversation_manager.set_conversation_state(conv_id, _turn1_state(
                    "unemployment rate", "UNRATE", "US unemployment rate last 5 years",
                ))
                expected = QueryResponse(conversationId=conv_id, clarificationNeeded=False, message="ok")
                with patch.object(self.service, "_execute_resolved_intent", AsyncMock(return_value=expected)) as execute:
                    run(self.service.process_query(utterance, conversation_id=conv_id))
                execute.assert_awaited_once()
                intent = execute.call_args.kwargs["intent"]
                params = intent.parameters or {}
                self.assertEqual(params.get("__semantic_indicator_label"), "unemployment rate")
                self.assertNotEqual(params.get("indicator"), utterance)
                countries = params.get("countries") or [params.get("country")]
                self.assertEqual(
                    {_normalize_known_country_to_iso2(country) for country in countries},
                    {"US", "CA"},
                )



# A FRED code whose title names Canada.  The stubs below key only on whether a
# fetched series' metadata names the requested country -- no concept or country
# is mapped to a code.
_REPICK_CODE = "STUB_CANADA_TITLED_CODE"


def _search_miss_fred(calls: list[dict[str, Any]]):
    """Stub FRED: the country-titled re-pick returns a Canada series; any other
    request (including the discovery search text) misses and returns a U.S.
    series, which the result-level country validation must reject."""

    def _fetch(params: dict[str, Any]) -> NormalizedData:
        calls.append(dict(params))
        if params.get("indicator") == _REPICK_CODE:
            return _series(indicator="Unemployment rate for Canada", country="Canada", series_id=_REPICK_CODE)
        return _series(indicator="Unemployment Rate", country="US", series_id="US_SERIES")

    return _fetch


class FredDiscoveryRepickTests(unittest.TestCase):
    """A later same-request re-pick supersedes the discovery search text.

    Re-entry paths that copy ``intent.parameters`` and set a new ``indicator``
    (same-provider alternate retry, a clarification choice, uncertain-match
    recovery, option validation) must fetch the NEW pick, never silently
    re-run the stale discovery search for the same country.
    """

    def setUp(self) -> None:
        cache_service.clear()
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
        self.service.settings.use_outcome_decision_stage = False
        self.service.settings.use_post_fetch_semantic_judge = False
        self.service.settings.use_staged_state_commit = False

    def _discovered(self) -> tuple[dict[str, Any], ParsedIntent]:
        _, intent = _turn2_intent("unemployment rate", "UNRATE", "compare with Canada", added=["CA"])
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            prepared = _prepare_fred_country_scope_params({}, dict(intent.parameters), intent)
        self.assertTrue(data_fetcher._fred_discovery_search_is_current(prepared))  # pylint: disable=protected-access
        return prepared, intent

    def test_same_country_repick_disables_the_stale_search_and_is_idempotent(self) -> None:
        prepared, intent = self._discovered()
        repick = dict(prepared, indicator=_REPICK_CODE)
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            once = _prepare_fred_country_scope_params({}, repick, intent)
            twice = _prepare_fred_country_scope_params({}, once, intent)
        self.assertEqual(once["indicator"], _REPICK_CODE)
        self.assertIs(twice, once)
        self.assertFalse(data_fetcher._fred_discovery_search_is_current(once))  # pylint: disable=protected-access
        contract = data_fetcher._provider_request_contract("FRED", intent, once)  # pylint: disable=protected-access
        self.assertNotIn("discovery_query", contract)
        self.assertEqual(once["__semantic_indicator_label"], "unemployment rate")
        # The snapshot still restores the verified request for the U.S. leg.
        us_child = {k: v for k, v in once.items() if k != "countries"}
        us_child["country"] = "US"
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            restored = _prepare_fred_country_scope_params({}, us_child, intent)
        self.assertEqual(restored["indicator"], "UNRATE")
        self.assertNotIn("__fred_country_scope_discovery", restored)

    def test_changed_request_with_stale_keys_rediscovers_for_its_own_concept(self) -> None:
        prepared, _ = self._discovered()
        _, cpi_intent = _turn2_intent("consumer price index", "CPIAUCSL", "compare with Canada", added=["CA"])
        stale = dict(prepared, indicator="CPIAUCSL", __semantic_indicator_label="consumer price index")
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            rediscovered = _prepare_fred_country_scope_params({}, stale, cpi_intent)
        self.assertEqual(rediscovered["__fred_discovery_query"], "Canada consumer price index")
        self.assertEqual(rediscovered["__fred_pre_discovery"]["indicator"], "CPIAUCSL")
        self.assertTrue(data_fetcher._fred_discovery_search_is_current(rediscovered))  # pylint: disable=protected-access

    def _alternate_retry(self, catalog_proves_repick: bool):
        intent = ParsedIntent(
            apiProvider="FRED", indicators=["unemployment rate"], clarificationNeeded=False,
            parameters={"country": "CA"}, originalQuery="Canada unemployment rate from FRED",
        )
        picks = {"n": 0}

        async def _resolver(_provider, _intent, params):
            picks["n"] += 1
            pick = "UNRATE" if picks["n"] == 1 else _REPICK_CODE
            return {
                **params, "indicator": pick, "__decision_source": "llm_pick",
                "__semantic_authority": "llm_adjudication", "__semantic_indicator_label": "unemployment rate",
            }

        calls: list[dict[str, Any]] = []
        targets = (lambda sid, iso2: sid == _REPICK_CODE and iso2 == "CA") if catalog_proves_repick else (lambda *_: False)
        with patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_save_to_cache", new_callable=AsyncMock), \
             patch.object(self.service, "_resolve_indicator_for_fetch", side_effect=_resolver), \
             patch.object(self.service.fred_provider, "fetch_series", side_effect=_search_miss_fred(calls)), \
             patch.object(data_fetcher, "_fred_catalog_series_targets_country", side_effect=targets):
            data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access
        return calls, data, intent

    def test_alternate_retry_fetches_the_repick_after_a_failed_discovery(self) -> None:
        for catalog_proves_repick in (True, False):
            with self.subTest(catalog_proves_repick=catalog_proves_repick):
                cache_service.clear()
                calls, data, intent = self._alternate_retry(catalog_proves_repick)
                fetched = [str(call.get("indicator")) for call in calls]
                # One discovery search, then the re-adjudicated pick -- never the
                # same stale search text again.
                self.assertEqual(len(fetched), 2, fetched)
                self.assertTrue(_text_targets_country(fetched[0], "CA"))
                self.assertEqual(fetched[1], _REPICK_CODE)
                self.assertEqual({_normalize_known_country_to_iso2(s.metadata.country) for s in data}, {"CA"})
                self.assertEqual(intent.parameters["__semantic_indicator_label"], "unemployment rate")

    def _clarification_choice(self, *, chosen_is_canada: bool):
        prepared, _ = self._discovered()
        chosen = _REPICK_CODE if chosen_is_canada else "SOME_US_CODE"
        params = dict(prepared, indicator=chosen, __semantic_authority="exact_user_input", __decision_source="user_choice")
        intent = ParsedIntent(
            apiProvider="FRED", indicators=[chosen], clarificationNeeded=False, parameters=params,
            originalQuery="compare with Canada", isFollowUp=True, followUpType="clarification_answer",
        )

        async def _resolver(_provider, _intent, p):
            return dict(p)

        calls: list[dict[str, Any]] = []
        # The catalog cannot prove the chosen code's country: the fetched
        # series' own metadata is the evidence.
        with patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.service, "_save_to_cache", new_callable=AsyncMock), \
             patch.object(self.service, "_resolve_indicator_for_fetch", side_effect=_resolver), \
             patch.object(self.service.fred_provider, "fetch_series", side_effect=_search_miss_fred(calls)), \
             patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            try:
                result: Any = run(self.service._fetch_data(intent))  # pylint: disable=protected-access
            except DataNotAvailableError as exc:
                result = exc
        return calls, result, chosen

    def test_explicit_choice_is_fetched_instead_of_the_stale_search(self) -> None:
        calls, data, chosen = self._clarification_choice(chosen_is_canada=True)
        self.assertEqual([call.get("indicator") for call in calls], [chosen])
        self.assertEqual({_normalize_known_country_to_iso2(s.metadata.country) for s in data}, {"CA"})

    def test_explicit_choice_with_wrong_country_fails_closed_without_researching(self) -> None:
        calls, result, chosen = self._clarification_choice(chosen_is_canada=False)
        self.assertIsInstance(result, DataNotAvailableError)
        self.assertEqual([call.get("indicator") for call in calls], [chosen])


class FredFollowUpSwitchAndShapeTests(unittest.TestCase):
    """Fetch-level country switch, concept-source hygiene, multi-indicator."""

    def setUp(self) -> None:
        cache_service.clear()
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
        self.service.settings.use_outcome_decision_stage = False
        self.service.settings.use_post_fetch_semantic_judge = False
        self.service.settings.use_staged_state_commit = False

    def test_country_switch_fetch_searches_concept_for_the_new_country(self) -> None:
        for label, code, utterance, changed in _SWITCH_CASES:
            with self.subTest(utterance=utterance):
                cache_service.clear()
                _, intent = _turn2_intent(label, code, utterance, changed=changed)

                async def _resolver(_provider, _intent, params, _code=code):
                    # Resolution re-picks a U.S.-titled code for the concept.
                    return {
                        **params, "indicator": _code, "__decision_source": "llm_pick",
                        "__semantic_authority": "llm_adjudication",
                    }

                calls: list[dict[str, Any]] = []
                with patch.object(self.service, "_get_from_cache", new_callable=AsyncMock, return_value=None), \
                     patch.object(self.service, "_save_to_cache", new_callable=AsyncMock), \
                     patch.object(self.service, "_resolve_indicator_for_fetch", side_effect=_resolver), \
                     patch.object(self.service.fred_provider, "fetch_series", side_effect=_fake_fred_catalog(calls)), \
                     patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
                    data = run(self.service._fetch_data(intent))  # pylint: disable=protected-access
                searched = [str(call.get("indicator") or "") for call in calls]
                self.assertFalse(any(utterance in text for text in searched), searched)
                self.assertTrue(any(label in text and _text_targets_country(text, "JP") for text in searched), searched)
                self.assertFalse(any(_text_targets_country(text, "US") for text in searched), searched)
                self.assertEqual({_normalize_known_country_to_iso2(s.metadata.country) for s in data}, {"JP"})
                self.assertEqual(intent.parameters["__semantic_indicator_label"], label)

    def test_display_label_and_code_shaped_label_are_not_concepts(self) -> None:
        # The display label is the raw delta utterance on a follow-up without
        # resolvedQuery; a code-shaped label is a series id, not a concept.
        for params_extra, indicators in (
            ({"__display_indicator_label": "compare with Canada"}, ["UNRATE"]),
            ({"__semantic_indicator_label": "UNRATE"}, ["UNRATE"]),
        ):
            with self.subTest(params_extra=params_extra):
                intent = ParsedIntent(
                    apiProvider="FRED", indicators=indicators, clarificationNeeded=False,
                    parameters={"countries": ["United States", "CA"], "indicator": "UNRATE", **params_extra},
                    originalQuery="compare with Canada", isFollowUp=True,
                )
                with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
                    prepared = _prepare_fred_country_scope_params({}, dict(intent.parameters), intent)
                self.assertNotIn("__fred_discovery_query", prepared)
        # A code-shaped label falls through to a non-code parsed indicator.
        intent = ParsedIntent(
            apiProvider="FRED", indicators=["UNRATE", "unemployment rate"], clarificationNeeded=False,
            parameters={"country": "CA", "indicator": "UNRATE", "__semantic_indicator_label": "UNRATE"},
            originalQuery="and Canada", isFollowUp=True,
        )
        with patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            prepared = _prepare_fred_country_scope_params({}, dict(intent.parameters), intent)
        self.assertEqual(prepared["__fred_discovery_query"], "Canada unemployment rate")

    def test_multi_indicator_non_us_dispatch_fails_closed_instead_of_duplicating(self) -> None:
        intent = ParsedIntent(
            apiProvider="FRED", indicators=["UNRATE", "CPIAUCSL"], clarificationNeeded=False,
            parameters={
                "country": "CA", "indicator": "UNRATE", "__semantic_indicator_label": "unemployment rate",
                "__semantic_authority": "llm_adjudication",
            },
            originalQuery="Canada unemployment and inflation from FRED",
        )
        calls: list[dict[str, Any]] = []
        with patch.object(self.service.fred_provider, "fetch_series", side_effect=_fake_fred_catalog(calls)), \
             patch.object(data_fetcher, "_fred_catalog_series_targets_country", return_value=False):
            plan = data_fetcher.materialize_execution_plan(None, provider="FRED", intent=intent, params=dict(intent.parameters))
            with self.assertRaises(DataNotAvailableError):
                run(data_fetcher.fetch_from_provider_dispatch(self.service, intent, plan))
        # No per-indicator call searched with the discovery text.
        self.assertTrue(calls)
        self.assertFalse(any(_text_targets_country(str(call.get("indicator")), "CA") for call in calls))

    def test_cross_provider_fallback_drops_fred_dispatch_keys(self) -> None:
        from unittest.mock import Mock

        prepared, intent = FredDiscoveryRepickTests._discovered(self)  # type: ignore[arg-type]
        intent.parameters = prepared
        served = [NormalizedData.model_validate({
            "metadata": {
                "source": "World Bank", "indicator": "Unemployment, total", "country": "Canada",
                "frequency": "annual", "unit": "Percent", "lastUpdated": "2026-01-01",
                "seriesId": "WB_STUB", "apiUrl": "https://example.com",
            },
            "data": [{"date": "2024-01-01", "value": 6.4}],
        })]
        svc = QueryService.__new__(QueryService)
        svc._detect_explicit_provider = Mock(return_value="")
        svc._select_indicator_query_for_resolution = Mock(return_value="unemployment rate")
        svc._collect_target_countries = Mock(return_value=["US", "CA"])
        svc._get_fallback_providers = Mock(return_value=["WORLDBANK"])
        svc._effective_original_query = Mock(return_value="compare with Canada")
        svc._looks_like_provider_indicator_code = Mock(return_value=False)
        svc._is_fallback_relevant = Mock(return_value=True)
        svc._fetch_data = AsyncMock(return_value=served)
        run(svc._try_with_fallback(intent, DataNotAvailableError("FRED has no Canada series")))  # pylint: disable=protected-access
        fb_intent = svc._fetch_data.call_args.args[0]
        for key in data_fetcher._FRED_DISCOVERY_SCOPED_KEYS:  # pylint: disable=protected-access
            self.assertNotIn(key, fb_intent.parameters)
        self.assertEqual(fb_intent.parameters["__semantic_indicator_label"], "unemployment rate")


if __name__ == "__main__":
    unittest.main()
