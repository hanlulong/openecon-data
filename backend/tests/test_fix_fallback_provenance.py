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

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.conversation_state_v2 import ConversationState
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

    assert result is served
    # apiProvider now reflects the provider that actually served the data.
    assert intent.apiProvider == "WORLDBANK"
    # The FRED-namespace resolved code fields are cleared so the next turn
    # re-resolves in the World Bank namespace...
    assert "indicator" not in intent.parameters
    assert "seriesId" not in intent.parameters
    # ...but the human-readable semantic anchor is preserved.
    assert intent.parameters.get("__semantic_indicator_label") == "inflation"


def test_fallback_skips_restamp_when_no_provider_switch():
    # Fallback served the SAME provider the intent already names — nothing to
    # restamp, and the resolved code must survive untouched.
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

    run(svc._try_with_fallback(intent, Exception("primary failed")))

    assert intent.apiProvider == "FRED"
    assert intent.parameters.get("indicator") == "CPIAUCSL"


# ─── (ii) persist-time namespace guard ──────────────────────────────

def test_persist_drops_code_on_provider_mismatch():
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
    assert state.resolved_indicator_code is None


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
