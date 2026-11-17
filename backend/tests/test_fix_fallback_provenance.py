"""Guard tests for F2 — fallback provenance must stay namespace-consistent.

The worst wrong-data class: a cross-provider fallback serves data from provider
B, but callers persist conversation state and build the response with the
ORIGINAL intent (provider A + provider-A resolved code). Next turn then sends a
provider-A code to provider B.

Two complementary defenses are tested:

* (i) ``_try_with_fallback`` restamps ``intent.apiProvider`` to the serving
  provider and clears the primary's provider-specific resolved-code fields at
  the outer return sites (reference semantics propagate to every caller).
* (ii) ``_persist_verified_conversation_state`` independently refuses to persist
  a resolved code / indicator list when the data-derived provider differs from
  the provider the code was resolved under.

Invariant under test: the PERSISTED ``(provider, resolved_indicator_code)`` pair
is always namespace-consistent.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.conversation_state_v2 import ConversationState, FollowUpDelta, materialize_intent, merge_state
from backend.services.query import QueryService
from backend.tests.utils import run


def _series(*, source: str, indicator: str, series_id: str, country: str = "US") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source,
            indicator=indicator,
            country=country,
            frequency="annual",
            unit="Index",
            seriesId=series_id,
        ),
        data=[{"date": "2020-01-01", "value": 100.0}],
    )


# ─── (i) restamp at the _try_with_fallback chokepoint ───────────────

def _fallback_service(served: list) -> QueryService:
    svc = QueryService.__new__(QueryService)
    svc._detect_explicit_provider = Mock(return_value="")
    svc._select_indicator_query_for_resolution = Mock(return_value="inflation")
    svc._collect_target_countries = Mock(return_value=["US"])
    svc._get_fallback_providers = Mock(return_value=["WORLDBANK"])
    svc._effective_original_query = Mock(return_value="US inflation")
    svc._looks_like_provider_indicator_code = Mock(return_value=False)
    svc._is_fallback_relevant = Mock(return_value=True)
    svc._judge_fallback_match = AsyncMock(return_value=True)
    svc._fetch_data = AsyncMock(return_value=served)
    return svc


def test_fallback_restamps_provider_and_clears_primary_code():
    served = [_series(source="World Bank", indicator="Inflation", series_id="FP.CPI.TOTL.ZG")]
    svc = _fallback_service(served)
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["CPIAUCSL"],
        parameters={
            "indicator": "CPIAUCSL",
            "seriesId": "CPIAUCSL",
            "__semantic_indicator_label": "inflation",
        },
        clarificationNeeded=False,
        originalQuery="US inflation",
    )

    result = run(svc._try_with_fallback(intent, Exception("primary failed")))

    # The served series, disclosed on a copy (the fetched object is untouched).
    assert [series.metadata.seriesId for series in result] == ["FP.CPI.TOTL.ZG"]
    assert result[0].metadata.notes == [
        "FRED could not serve this request, so this data comes from World Bank instead."
    ]
    assert served[0].metadata.notes is None
    # apiProvider now reflects the provider that actually served the data.
    assert intent.apiProvider == "WORLDBANK"
    # The FRED-namespace resolved code fields are cleared so the next turn
    # re-resolves in the World Bank namespace...
    assert "indicator" not in intent.parameters
    assert "seriesId" not in intent.parameters
    # ...but the human-readable semantic anchor is preserved.
    assert intent.parameters.get("__semantic_indicator_label") == "inflation"


def test_fallback_excludes_the_failed_primary_provider():
    # An inherited candidate list must never retry the failed primary source.
    served = [_series(source="FRED", indicator="Inflation", series_id="CPIAUCSL")]
    svc = _fallback_service(served)
    svc._get_fallback_providers = Mock(return_value=["FRED"])
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["CPIAUCSL"],
        parameters={"indicator": "CPIAUCSL"},
        clarificationNeeded=False,
        originalQuery="US inflation",
    )

    primary = Exception("primary failed")
    with pytest.raises(Exception) as failure:
        run(svc._try_with_fallback(intent, primary))
    assert failure.value is primary
    svc._fetch_data.assert_not_awaited()

    assert intent.apiProvider == "FRED"
    assert intent.parameters.get("indicator") == "CPIAUCSL"


# ─── (ii) persist-time namespace guard ──────────────────────────────

def test_persist_uses_verified_singleton_code_on_provider_mismatch():
    svc = QueryService.__new__(QueryService)
    state = ConversationState(indicator="inflation", provider="FRED", country="US", turn_number=1)
    # Intent still says FRED + a FRED code, but the data came from World Bank
    # (a fallback path that reached persist without restamping).
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["CPIAUCSL"],
        parameters={"indicator": "CPIAUCSL", "__semantic_indicator_label": "inflation"},
        clarificationNeeded=False,
        originalQuery="US inflation",
    )
    data = [_series(source="World Bank", indicator="Inflation", series_id="FP.CPI.TOTL.ZG")]

    svc._persist_verified_conversation_state("conv-mismatch", state, data, intent=intent)

    # Provider follows the data; the FRED code is NOT persisted under WORLDBANK.
    assert state.provider == "WORLDBANK"
    assert state.routed_provider == "WORLDBANK"
    assert state.resolved_indicator_code == "FP.CPI.TOTL.ZG"
    assert state.last_indicators_resolved == ["FP.CPI.TOTL.ZG"]


def test_persist_keeps_code_when_provider_consistent():
    svc = QueryService.__new__(QueryService)
    state = ConversationState(indicator="inflation", provider="FRED", country="US", turn_number=1)
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["CPIAUCSL"],
        parameters={"indicator": "CPIAUCSL"},
        clarificationNeeded=False,
        originalQuery="US inflation",
    )
    data = [_series(source="FRED", indicator="CPI", series_id="CPIAUCSL")]

    svc._persist_verified_conversation_state("conv-consistent", state, data, intent=intent)

    assert state.provider == "FRED"
    assert state.resolved_indicator_code == "CPIAUCSL"
    assert state.last_indicators_resolved == ["CPIAUCSL"]


def test_restamped_fallback_persists_returned_code_for_next_time_followup():
    svc = QueryService.__new__(QueryService)
    state = ConversationState(indicator="GDP", provider="WORLDBANK", country="France", resolved_indicator_code="GDP")
    intent = ParsedIntent(apiProvider="IMF", indicators=["GDP"], clarificationNeeded=False,
                          parameters={"__semantic_indicator_label": "GDP", "startDate": "2000-01-01",
                                      "endDate": "2015-12-31", "__time_scope_authority": "user"})
    data = [_series(source="IMF", indicator="GDP, current prices", series_id="NGDPD", country="France")]
    svc._persist_verified_conversation_state("conv-fallback-gdp", state, data, intent=intent)
    assert state.resolved_indicator_code == "NGDPD"
    assert state.provider == "IMF"
    assert state.indicator == "GDP"
    updated = merge_state(state, FollowUpDelta(changed_start_date="2020-01-01", changed_end_date="2026-11-17", delta_type="time_change"))
    next_intent = materialize_intent(updated)
    assert next_intent.parameters["indicator"] == "NGDPD"
    assert next_intent.parameters["startDate"] == "2020-01-01"


def test_consistent_execution_keeps_transformation_missing_from_metadata():
    svc = QueryService.__new__(QueryService)
    state = ConversationState(indicator="CPI inflation", provider="FRED", country="US")
    intent = ParsedIntent(apiProvider="FRED", indicators=["CPIAUCSL:pc1"], clarificationNeeded=False,
                          parameters={"indicator": "CPIAUCSL:pc1"})
    svc._persist_verified_conversation_state("conv-transformed", state,
        [_series(source="FRED", indicator="Consumer prices", series_id="CPIAUCSL")], intent=intent)
    assert state.resolved_indicator_code == "CPIAUCSL:pc1"


def test_restamped_fallback_without_native_identifier_discards_previous_code():
    svc = QueryService.__new__(QueryService)
    state = ConversationState(indicator="GDP", provider="WORLDBANK", resolved_indicator_code="NY.GDP.MKTP.CD")
    intent = ParsedIntent(apiProvider="IMF", indicators=["GDP"], parameters={}, clarificationNeeded=False)
    svc._persist_verified_conversation_state("conv-no-native-id", state,
        [_series(source="IMF", indicator="GDP", series_id="")], intent=intent)
    assert state.provider == "IMF"
    assert state.resolved_indicator_code is None
    assert state.last_indicators_resolved is None


@pytest.mark.parametrize("series_ids,title", [(["FP.CPI.TOTL.ZG", "NY.GDP.MKTP.CD"], "Inflation"), ([""], "Inflation"), ([""], "GDP")])
def test_ambiguous_or_empty_returned_identity_clears_foreign_state(series_ids, title):
    svc = QueryService.__new__(QueryService)
    state = ConversationState(indicator="inflation", provider="FRED", resolved_indicator_code="CPIAUCSL", last_indicators_resolved=["CPIAUCSL"])
    intent = ParsedIntent(apiProvider="FRED", indicators=["CPIAUCSL"], clarificationNeeded=False,
                          parameters={"indicator": "CPIAUCSL"})
    data = [_series(source="World Bank", indicator=title, series_id=code) for code in series_ids]
    svc._persist_verified_conversation_state("conv-no-single-code", state, data, intent=intent)
    assert state.resolved_indicator_code is None
    assert state.last_indicators_resolved is None
