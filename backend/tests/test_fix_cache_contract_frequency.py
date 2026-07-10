"""Guard tests for F1b — frequency belongs in the provider request identity.

``_provider_request_contract`` feeds ``_cache_identity``. Before the fix, only
Eurostat's catch-all ``filters`` carried frequency, so two FRED/WorldBank/IMF
requests that differed ONLY in frequency (annual vs quarterly GDP for the same
series/country/window) collided in the cache and the second request silently
received the first's cached slice. Frequency (and FRED's aggregation method) are
now included uniformly, but ONLY when actually set, so the common no-frequency
request keeps its pre-fix identity and is not needlessly cache-invalidated.
"""
from __future__ import annotations

import pytest

from backend.models import ParsedIntent
from backend.services.data_fetcher import _cache_identity, _provider_request_contract


def _contract(provider: str, *, frequency: str | None = None, aggregation: str | None = None):
    params = {
        "indicator": "GDP",
        "country": "US",
        "startDate": "2000-01-01",
        "endDate": "2020-01-01",
    }
    if frequency is not None:
        params["frequency"] = frequency
    if aggregation is not None:
        params["aggregation_method"] = aggregation
    intent = ParsedIntent(
        apiProvider=provider,
        indicators=["GDP"],
        parameters=dict(params),
        clarificationNeeded=False,
    )
    return _provider_request_contract(provider, intent, params)


def _identity(contract):
    return _cache_identity("provider_dispatch", contract, {})


@pytest.mark.parametrize("provider", ["FRED", "WORLDBANK", "IMF", "OECD", "STATSCAN"])
def test_frequency_differentiates_cache_identity(provider):
    annual = _identity(_contract(provider, frequency="annual"))
    quarterly = _identity(_contract(provider, frequency="quarterly"))
    assert annual != quarterly, f"{provider}: annual and quarterly must not collide"


@pytest.mark.parametrize("provider", ["FRED", "WORLDBANK", "IMF", "OECD", "STATSCAN"])
def test_frequency_present_in_contract_when_set(provider):
    contract = _contract(provider, frequency="quarterly")
    assert contract.get("frequency") == "quarterly"


@pytest.mark.parametrize("provider", ["FRED", "WORLDBANK", "IMF", "OECD", "STATSCAN"])
def test_absent_frequency_is_stable_and_omitted(provider):
    # No frequency set → the key must be absent so the identity is byte-identical
    # to the pre-fix contract (no cache-wipe for the common case).
    a = _contract(provider)
    b = _contract(provider)
    assert "frequency" not in a
    assert _identity(a) == _identity(b)


def test_fred_aggregation_method_differentiates_identity():
    avg = _identity(_contract("FRED", frequency="quarterly", aggregation="avg"))
    eop = _identity(_contract("FRED", frequency="quarterly", aggregation="eop"))
    assert avg != eop


def test_aggregation_method_omitted_when_absent():
    contract = _contract("FRED", frequency="annual")
    assert "aggregation_method" not in contract
