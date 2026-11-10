"""The derived ``__semantic_indicator_label`` is the CONCEPT, never retrieval text.

The label is persisted in conversation state, re-composed with a new
geography on follow-ups ("<Country> <concept>") and read by the fallback judge
as "the requested concept". The retrieval-text chooser makes two retrieval
decisions that must not leak into it: the exact-title branch returns the raw
query (geography prefix included), and the wrapper appends/prepends the
country/region for region-as-series providers (FRED). Live failure: turn 1
"US unemployment rate" persisted "US unemployment rate", and "compare with
Canada" then searched FRED for "Canada US unemployment rate" (no match), fell
back with a US-titled concept and ended in a menu.

The catalog is faked: which strings count as provider-native titles is the
input to the mechanism under test, not its subject.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ParsedIntent
from backend.services import data_fetcher
from backend.services import indicator_resolution as ir
from backend.services.cache import cache_service
from backend.services.conversation_state_v2 import (
    FollowUpDelta,
    extract_state_from_intent,
    materialize_intent,
    merge_state,
)
from backend.services.indicator_selector import SelectionResult
from backend.services.query import QueryService
from backend.tests.utils import run

# Provider-native titles as the (faked) catalog sees them. The real detector
# also accepts a geography-prefixed title ("US unemployment rate").
_TITLES = {
    "FRED": {
        "unemployment rate",
        "us unemployment rate",
        "u.s. unemployment rate",
        "japan unemployment rate",
        "unemployment rate - black or african american",
    },
    "STATSCAN": {
        "consumer price index, monthly, not seasonally adjusted",
        "canada pension plan contributions",
    },
}


def _fake_exact_title(text: str, provider: str) -> bool:
    key = "STATSCAN" if str(provider or "").upper() in {"STATSCAN", "STATISTICS CANADA"} else str(provider or "").upper()
    return str(text or "").strip().lower() in _TITLES.get(key, set())


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    monkeypatch.setattr(ir, "looks_like_exact_provider_title_match", _fake_exact_title)


_SVC = SimpleNamespace(_looks_like_provider_indicator_code=lambda provider, text: False)


def _intent(
    provider: str,
    parsed: str,
    original: str,
    params: Dict[str, Any],
    region: Optional[str] = None,
) -> ParsedIntent:
    return ParsedIntent(
        apiProvider=provider,
        indicators=[parsed],
        parameters=dict(params),
        clarificationNeeded=False,
        originalQuery=original,
        subnationalRegion=region,
    )


@pytest.mark.parametrize(
    "original, params",
    [
        ("US unemployment rate", {"country": "US"}),
        ("US unemployment rate", {"country": "United States"}),
        ("U.S. unemployment rate", {"country": "US"}),
        ("US unemployment rate", {"countries": ["United States"]}),
        ("Japan unemployment rate", {"country": "Japan"}),
    ],
)
def test_exact_title_with_scoped_country_prefix_records_the_parsed_concept(original, params):
    intent = _intent("FRED", "unemployment rate", original, params)

    assert ir.select_indicator_concept_label(_SVC, intent) == "unemployment rate"
    # Retrieval text is unchanged: the exact title still drives retrieval.
    assert ir._select_indicator_query_for_resolution_unqualified(_SVC, intent) == original


@pytest.mark.parametrize(
    "provider, parsed, original, params",
    [
        # The extra text is a title qualifier, not the scoped country.
        ("FRED", "unemployment rate", "Unemployment Rate - Black or African American", {"country": "US"}),
        # The prefix is a country, but not the one in the structured scope.
        ("FRED", "unemployment rate", "US unemployment rate", {"country": "Canada"}),
        # A product title whose parsed phrase is not itself a title.
        ("StatsCan", "Consumer Price Index", "Consumer Price Index, monthly, not seasonally adjusted", {"country": "Canada"}),
        # A name that starts with a country: the remainder is not a title.
        ("StatsCan", "Pension Plan contributions", "Canada Pension Plan contributions", {"country": "Canada"}),
    ],
)
def test_exact_title_keeps_raw_text_unless_only_the_scoped_country_differs(provider, parsed, original, params):
    intent = _intent(provider, parsed, original, params)

    assert ir.select_indicator_concept_label(_SVC, intent) == original


def test_country_qualifier_for_non_home_fred_country_never_becomes_the_label():
    intent = _intent("FRED", "unemployment rate", "unemployment rate in Japan", {"country": "Japan"})

    assert ir.select_indicator_query_for_resolution(_SVC, intent) == "unemployment rate Japan"
    assert ir.select_indicator_concept_label(_SVC, intent) == "unemployment rate"


def test_region_qualifier_never_becomes_the_label():
    intent = _intent("FRED", "unemployment rate", "jobless rate across the state", {"country": "US"}, region="Texas")

    assert ir.select_indicator_query_for_resolution(_SVC, intent).startswith("Texas ")
    assert ir.select_indicator_concept_label(_SVC, intent) == "unemployment rate"


def _resolver_svc() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(),
        statscan_provider=SimpleNamespace(),
        _looks_like_provider_indicator_code=lambda provider, text: False,
        _verify_semantic_discriminators=lambda *args, **kwargs: True,
    )


def _resolve_without_selector_decision(intent: ParsedIntent) -> dict:
    with patch(
        "backend.services.indicator_selector.IndicatorSelector.select",
        new=AsyncMock(return_value=SelectionResult(code=None, source="no_decision")),
    ):
        return run(ir.resolve_indicator_for_fetch(_resolver_svc(), "FRED", intent, dict(intent.parameters or {})))


def test_resolution_derives_the_concept_label_not_the_qualified_retrieval_text():
    intent = _intent("FRED", "unemployment rate", "unemployment rate in Japan", {"country": "Japan"})

    params = _resolve_without_selector_decision(intent)

    assert params.get("__semantic_indicator_label") == "unemployment rate"


def test_resolution_never_rederives_a_carried_label():
    intent = _intent(
        "FRED",
        "unemployment rate",
        "unemployment rate in Japan",
        {"country": "Japan", "__semantic_indicator_label": "harmonised unemployment rate"},
    )

    params = _resolve_without_selector_decision(intent)

    assert params.get("__semantic_indicator_label") == "harmonised unemployment rate"


def test_prefetch_selector_pick_records_the_concept_label():
    cache_service.clear()
    service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    service.settings.use_outcome_decision_stage = False
    intent = _intent("FRED", "unemployment rate", "unemployment rate in Japan", {"country": "Japan"})
    select = AsyncMock(return_value=SelectionResult(code="LRUNTTTTJPM156S", name="Unemployment Rate for Japan", source="llm_pick"))

    with patch("backend.services.indicator_selector.IndicatorSelector.select", select), patch(
        "backend.services.indicator_clarification._is_resolved_indicator_plausible", return_value=True
    ):
        clarification = run(
            service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                conversation_id="conv-concept-label",
                query="unemployment rate in Japan",
                intent=intent,
                explicit_provider=None,
                is_multi_indicator=False,
                processing_steps=None,
            )
        )

    assert clarification is None
    assert intent.parameters.get("indicator") == "LRUNTTTTJPM156S"
    # The selector still retrieved with the country-qualified text ...
    assert select.await_args.args[0] == "unemployment rate Japan"
    # ... but the persisted label is the concept.
    assert intent.parameters.get("__semantic_indicator_label") == "unemployment rate"


@pytest.mark.parametrize(
    "utterance, added, iso2, expected",
    [
        ("compare with Canada", ["CA"], "CA", "Canada unemployment rate"),
        ("和日本比较", ["Japan"], "JP", "Japan unemployment rate"),
        ("what about Mexico?", None, "MX", "Mexico unemployment rate"),
    ],
)
def test_country_follow_up_composes_new_country_with_the_clean_concept(utterance, added, iso2, expected):
    turn1 = _intent("FRED", "unemployment rate", "US unemployment rate", {"country": "United States"})
    turn1.parameters["__semantic_indicator_label"] = ir.select_indicator_concept_label(_SVC, turn1)
    turn1.parameters["indicator"] = "UNRATE"
    state = extract_state_from_intent(turn1)
    assert state.indicator == "unemployment rate"

    delta = FollowUpDelta(
        added_countries=added,
        changed_countries=None if added else [iso2],
        raw_query=utterance,
        delta_type="additive_country" if added else "country_switch",
    )
    intent = materialize_intent(merge_state(state, delta))

    text = data_fetcher._fred_country_context_query(intent.parameters, intent, iso2, iso2)
    assert text == expected
