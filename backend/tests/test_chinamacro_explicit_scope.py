"""Explicit ChinaMacro requests for an uncovered geography degrade honestly.

Mechanism: the explicit-provider route bypasses the router's coverage
override, so a single-country provider's dispatch must fail closed on a
requested country outside its coverage (shared coverage predicate) and let
the fallback/disclosure path run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Explicit ChinaMacro for an uncovered geography must degrade honestly
# ("US GDP from ChinaMacro", "Japan GDP from ChinaMacro"): the explicit route
# bypasses the router's coverage override, so the dispatch fails closed and
# the fallback/disclosure path runs instead of serving China data.
# ---------------------------------------------------------------------------

def _chinamacro_dispatch(params: dict, provider_request: dict | None = None):
    import asyncio

    from backend.models import ExecutionPlan, Metadata, NormalizedData, ParsedIntent
    from backend.services.data_fetcher import fetch_from_provider_dispatch

    calls: list[str] = []

    class _FakeChinaMacro:
        async def fetch_indicator(self, indicator, start_date=None, end_date=None):
            calls.append(indicator)
            return NormalizedData(
                metadata=Metadata(
                    source="ChinaMacro (test)", indicator="GDP", country="China",
                    frequency="quarterly", unit="%", lastUpdated="2026-09-30",
                ),
                data=[{"date": "2026-09-30", "value": 5.0}],
            )

    svc = SimpleNamespace(chinamacro_provider=_FakeChinaMacro())
    intent = ParsedIntent(
        apiProvider="CHINAMACRO",
        indicators=["GDP"],
        parameters={"indicator": "GDP", **params},
        clarificationNeeded=False,
    )
    plan = ExecutionPlan(
        provider="CHINAMACRO",
        candidate_id="test",
        fetch_strategy="provider_dispatch",
        params=dict(intent.parameters),
        provider_request=dict(provider_request or {}),
    )
    result = asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))
    return result, calls


@pytest.mark.parametrize(
    "params",
    [{"country": "US"}, {"country": "Japan"}, {"countries": ["CN", "JP"]}, {"country": "DE"}],
)
def test_chinamacro_dispatch_fails_closed_for_uncovered_geography(params) -> None:
    from backend.utils.retry import DataNotAvailableError

    with pytest.raises(DataNotAvailableError, match="does not cover the requested geography"):
        _chinamacro_dispatch(params)


def test_chinamacro_dispatch_reads_the_materialized_country_scope() -> None:
    from backend.utils.retry import DataNotAvailableError

    with pytest.raises(DataNotAvailableError):
        _chinamacro_dispatch({}, provider_request={"country_scope": ["Japan"]})


@pytest.mark.parametrize(
    "params",
    [{}, {"country": "China"}, {"country": "CN"}, {"countries": ["CN"]}, {"country": "1W"}],
)
def test_chinamacro_dispatch_serves_china_and_unscoped_requests(params) -> None:
    result, calls = _chinamacro_dispatch(params)
    assert calls == ["GDP"] and result[0].metadata.country == "China"


def test_chinamacro_lock_release_predicate_follows_served_labels() -> None:
    # Multi-round release ("China PMI from ChinaMacro" -> "what about Japan")
    # keys on provider_covers_country_list with the persisted provider label.
    from backend.services.provider_fallback import provider_covers_country_list

    for label in ("ChinaMacro", "CHINAMACRO", "ChinaMacro (NBS via EastMoney datacenter)"):
        assert provider_covers_country_list(label, ["CN"]) is True
        assert provider_covers_country_list(label, ["JP"]) is False
