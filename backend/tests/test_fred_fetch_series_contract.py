"""FRED fetch_series through a mocked FRED API.

- An exact-match series whose explicit, user-set window returns no rows stays
  empty; only a framework-default window is broadened to full history.
- A units transformation (e.g. CPIAUCSL:pc1) is reflected in dataType and the
  public apiUrl, and the apiUrl never carries the API key.
"""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.providers.fred import FREDProvider

KEY = "fake-fred-key-for-contract-tests"

SERIES_INFO = {
    "UNRATE": {
        "id": "UNRATE",
        "title": "Unemployment Rate",
        "frequency": "Monthly",
        "units": "Percent",
        "seasonal_adjustment_short": "SA",
        "observation_start": "1948-01-01",
        "observation_end": "2026-10-01",
        "last_updated": "2026-11-06",
    },
    "CPIAUCSL": {
        "id": "CPIAUCSL",
        "title": "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average",
        "frequency": "Monthly",
        "units": "Index 1982-1984=100",
        "seasonal_adjustment_short": "SA",
        "observation_start": "1947-01-01",
        "observation_end": "2026-09-01",
        "last_updated": "2026-10-15",
    },
}


@pytest.fixture(autouse=True)
def no_rate_limit_wait(monkeypatch):
    async def ready(_provider):
        pass

    monkeypatch.setattr("backend.providers.base.acquire_provider", ready)


def _fetch(monkeypatch, params, observations_for):
    """Run fetch_series; observations_for(query_params) gives the rows."""
    calls = []

    def respond(request):
        query = dict(request.url.params)
        calls.append((request.url.path, query))
        if request.url.path.endswith("/series"):
            return httpx.Response(200, json={"seriess": [SERIES_INFO[query["series_id"]]]})
        if request.url.path.endswith("/series/observations"):
            return httpx.Response(200, json={"observations": observations_for(query)})
        return httpx.Response(404, json={})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr("backend.providers.fred.get_http_client", lambda: client)
            return await FREDProvider(KEY).fetch_series(params)

    result = asyncio.run(run())
    observation_calls = [query for path, query in calls if path.endswith("/series/observations")]
    return result, observation_calls


def _rows_only_without_window(query):
    if "observation_start" in query or "observation_end" in query:
        return []
    return [{"date": "1950-01-01", "value": "6.5"}]


EXACT_2008 = {
    "seriesId": "UNRATE",
    "indicator": "UNRATE",
    "__exact_provider_code_match": True,
    "startDate": "2008-01-01",
    "endDate": "2008-12-31",
}


@pytest.mark.parametrize(
    "extra",
    [
        {"__original_query": "UNRATE in 2008"},
        {"__original_query": "UNRATE", "__time_scope_authority": "user"},
    ],
)
def test_explicit_window_with_no_rows_is_not_broadened(monkeypatch, extra):
    result, observation_calls = _fetch(monkeypatch, {**EXACT_2008, **extra}, _rows_only_without_window)
    assert len(observation_calls) == 1
    assert observation_calls[0]["observation_start"] == "2008-01-01"
    assert result.data == []


@pytest.mark.parametrize(
    "extra",
    [
        {"__original_query": "UNRATE"},
        {"__original_query": "UNRATE in 2008", "__time_scope_authority": "default"},
    ],
)
def test_default_window_with_no_rows_is_broadened(monkeypatch, extra):
    result, observation_calls = _fetch(monkeypatch, {**EXACT_2008, **extra}, _rows_only_without_window)
    assert len(observation_calls) == 2
    assert "observation_start" not in observation_calls[1]
    assert [(p.date, p.value) for p in result.data] == [("1950-01-01", 6.5)]


def _cpi_rows(query):
    return [{"date": "2026-08-01", "value": "2.9"}, {"date": "2026-09-01", "value": "3.0"}]


CPI_TITLE = SERIES_INFO["CPIAUCSL"]["title"]


@pytest.mark.parametrize(
    ("series_id", "data_type", "units", "indicator", "unit"),
    [
        ("CPIAUCSL:pc1", "Percent Change", "pc1", f"{CPI_TITLE} (YoY % Change)", "Percent Change from Year Ago"),
        ("CPIAUCSL:pch", "Percent Change", "pch", f"{CPI_TITLE} (% Change)", "Percent Change"),
        ("CPIAUCSL:ch1", "Change", "ch1", CPI_TITLE, "Index 1982-1984=100"),
        ("CPIAUCSL:lin", "Index", "lin", CPI_TITLE, "Index 1982-1984=100"),
        ("CPIAUCSL", "Index", None, CPI_TITLE, "Index 1982-1984=100"),
    ],
)
def test_transformation_sets_data_type_and_api_url(monkeypatch, series_id, data_type, units, indicator, unit):
    result, observation_calls = _fetch(
        monkeypatch,
        {"seriesId": series_id, "indicator": "CPI", "__original_query": "US CPI"},
        _cpi_rows,
    )
    metadata = result.metadata
    assert metadata.dataType == data_type
    # The indicator label names the transformation the unit already states.
    assert metadata.indicator == indicator
    assert metadata.unit == unit
    assert observation_calls[0].get("units") == units
    api_query = parse_qs(urlparse(metadata.apiUrl).query)
    assert api_query.get("units") == ([units] if units else None)
    assert api_query["api_key"] == ["***"]
    assert KEY not in metadata.apiUrl
