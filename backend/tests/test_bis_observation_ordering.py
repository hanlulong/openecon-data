"""FIX 3: the BIS standard and GLI paths must sort observations by period before
using data_points[0]/[-1] as start/end. BIS returns observation indices keyed
into TIME_PERIOD; iterating them in dict order (not chronological) put the wrong
period at start/end and fed a wrong endDate to the freshness sort key.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.tests.utils import MockAsyncResponse, run
from backend.providers.bis import BISProvider


def test_standard_path_sorts_observations_before_slicing():
    provider = BISProvider(metadata_search_service=None)

    # Observation keys deliberately inserted OUT of chronological order.
    payload = {
        "data": {
            "dataSets": [
                {
                    "series": {
                        "0:0": {
                            "observations": {
                                "2": ["3.0"],
                                "0": ["1.0"],
                                "1": ["2.0"],
                            }
                        }
                    }
                }
            ],
            "structure": {
                "dimensions": {
                    "series": [
                        {"id": "FREQ", "values": [{"id": "M", "name": "Monthly"}]},
                        {"id": "REF_AREA", "values": [{"id": "US", "name": "United States"}]},
                    ],
                    "observation": [
                        {
                            "id": "TIME_PERIOD",
                            "values": [
                                {"id": "2020-01"},
                                {"id": "2020-02"},
                                {"id": "2020-03"},
                            ],
                        }
                    ],
                }
            },
        }
    }

    with patch(
        "backend.providers.bis.get_http_client",
        return_value=_SingleResponseClient(MockAsyncResponse(payload)),
    ):
        series_list = run(provider.fetch_indicator(indicator="BIS_WS_CBPOL", country="US"))

    assert len(series_list) == 1
    series = series_list[0]
    dates = [dp.date for dp in series.data]
    values = [dp.value for dp in series.data]

    assert dates == ["2020-01-01", "2020-02-01", "2020-03-01"]
    assert values == [1.0, 2.0, 3.0]
    assert series.metadata.startDate == "2020-01-01"
    assert series.metadata.endDate == "2020-03-01"


class _SingleResponseClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, *, params=None, **_kwargs):
        return self._response
