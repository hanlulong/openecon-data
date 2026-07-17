"""Subnational discard must trigger ONE re-adjudication, not finalize empty.

Observed live ("California GDP"): the adjudicator picked a national FRED
series, the response-stage subnational check discarded it, and the national
pick stayed in the selection cache — repeats fast-failed on it (~1.6s) for
6h. process_query now: captures the served (provider, code) before
enforcement, evicts matching selection-cache entries, and re-runs the impl
once with the code excluded (contextvar conduit consumed at the _fetch_data
chokepoint — the impl has many intent-acquisition paths). A retry that also
fails the check leaves the first honest fail-closed response standing.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from backend.models import Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.services.indicator_selector import (
    _SELECTION_CACHE,
    _selection_cache_put,
    invalidate_selection_cache_entry,
)
from backend.services.query import _SUBNATIONAL_RETRY_EXCLUDES, QueryService


def _series(indicator: str, country: str, source: str = "FRED", series_id: str = "GDP") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country=country,
            frequency="quarterly", unit="USD", seriesId=series_id,
        ),
        data=[{"date": "2025-01-01", "value": 1.0}],
    )


def _intent(region: str | None = "California") -> ParsedIntent:
    return ParsedIntent(
        apiProvider="FRED",
        indicators=["GDP"],
        parameters={"country": "US", "indicator": "GDP"},
        clarificationNeeded=False,
        subnationalRegion=region,
        originalQuery="California GDP",
    )


def _resp(data, intent=None) -> QueryResponse:
    return QueryResponse(
        conversationId="c1",
        intent=intent or _intent(),
        data=data,
        clarificationNeeded=False,
    )


def test_invalidate_selection_cache_entry_pops_matching_only() -> None:
    _SELECTION_CACHE.clear()
    _selection_cache_put(("FRED", "gdp", "US", "", "", "california"), {"code": "GDP"})
    _selection_cache_put(("FRED", "gdp", "US", "", "", ""), {"code": "GDPC1"})
    _selection_cache_put(("WORLDBANK", "gdp", "US", "", "", ""), {"code": "GDP"})

    removed = invalidate_selection_cache_entry("FRED", "gdp")  # case-insensitive
    assert removed == 1
    remaining_codes = {p["code"] for _ts, p in _SELECTION_CACHE.values()}
    assert remaining_codes == {"GDPC1", "GDP"}  # WB entry untouched
    _SELECTION_CACHE.clear()


def _run_process_query(svc, impl_mock):
    with patch.object(QueryService, "_process_query_impl", impl_mock):
        return asyncio.run(svc.process_query("California GDP"))


def test_discard_triggers_single_retry_with_exclusion() -> None:
    svc = QueryService.__new__(QueryService)
    seen_ctx: list = []

    national = _resp([_series("Gross Domestic Product", "US", series_id="GDP")])
    state = _resp([_series("GDP in California", "US", series_id="CANGSP")])

    async def impl(self, query, **kwargs):  # noqa: ANN001, ARG001
        seen_ctx.append(_SUBNATIONAL_RETRY_EXCLUDES.get())
        return national if len(seen_ctx) == 1 else state

    response = _run_process_query(svc, impl)
    assert len(seen_ctx) == 2, "exactly one retry"
    assert seen_ctx[0] is None
    assert seen_ctx[1] == ["GDP"], "retry must carry the discarded code"
    assert response.data and "California" in response.data[0].metadata.indicator
    assert _SUBNATIONAL_RETRY_EXCLUDES.get() is None, "contextvar must be reset"


def test_retry_that_also_fails_keeps_first_honest_response() -> None:
    svc = QueryService.__new__(QueryService)
    calls: list = []

    async def impl(self, query, **kwargs):  # noqa: ANN001, ARG001
        calls.append(1)
        return _resp([_series("Gross Domestic Product", "US", series_id="GDP")])

    response = _run_process_query(svc, impl)
    assert len(calls) == 2, "one retry, never a loop"
    assert response.data is None
    assert response.error == "subnational_data_unavailable"


def test_no_retry_when_check_passes() -> None:
    svc = QueryService.__new__(QueryService)
    calls: list = []

    async def impl(self, query, **kwargs):  # noqa: ANN001, ARG001
        calls.append(1)
        return _resp([_series("GDP in California", "US", series_id="CANGSP")])

    response = _run_process_query(svc, impl)
    assert len(calls) == 1
    assert response.data
