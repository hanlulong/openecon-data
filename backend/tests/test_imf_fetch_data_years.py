"""IMFProvider._fetch_data passes the requested years to fetch_indicator.

start_year/end_year used to be dropped whenever startDate/endDate were
absent, because `a or b if c else None` parses as `(a or b) if c else None`.
"""

import asyncio

import pytest

from backend.providers.imf import IMFProvider


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"start_year": 2015, "end_year": 2020}, (2015, 2020)),
        ({"start_year": "2015"}, (2015, None)),
        ({"startDate": "2010-01-01", "endDate": "2012-12-31"}, (2010, 2012)),
        ({"start_year": 2015, "startDate": "2010-01-01"}, (2015, None)),
        ({}, (None, None)),
    ],
)
def test_years_reach_fetch_indicator(monkeypatch, params, expected):
    provider = IMFProvider()
    seen = {}

    async def fake_fetch_indicator(**kwargs):
        seen.update(kwargs)
        return "sentinel"

    monkeypatch.setattr(provider, "fetch_indicator", fake_fetch_indicator)
    result = asyncio.run(provider._fetch_data(indicator="NGDP_RPCH", country="US", **params))
    assert result == "sentinel"
    assert (seen["start_year"], seen["end_year"]) == expected
