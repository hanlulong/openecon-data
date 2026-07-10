"""FIX 2: a BIS WS_* dataflow with no country dimension serves a single global
aggregate. The per-country fan-out asks for it once per requested country, so
stamping the requested country name minted N identical series labeled US/GB/FR.
The series must instead be labeled from its own dims ("Global") and returned
ONCE, and a country-less series must never be asserted as the requested country.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.tests.utils import MockAsyncResponse, run
from backend.providers.bis import BISProvider


# A WS_* dataflow whose series dimensions contain NO country/area dimension.
_COUNTRY_LESS_PAYLOAD = {
    "data": {
        "dataSets": [
            {"series": {"0:0": {"observations": {"0": [42.0], "1": [43.5]}}}}
        ],
        "structure": {
            "dimensions": {
                "series": [
                    {"id": "FREQ", "values": [{"id": "A", "name": "Annual"}]},
                    {"id": "MEASURE", "values": [{"id": "A", "name": "All"}]},
                ],
                "observation": [
                    {"id": "TIME_PERIOD", "values": [{"id": "2020"}, {"id": "2021"}]},
                ],
            }
        },
    }
}


class _RoutingBISClient:
    """Route by URL so parallel fan-out ordering does not matter.

    Country-keyed standard-path URLs (".../WS_X/A.US") return no data; the bare
    exact-dataflow fallback URL (".../WS_X/A") returns the country-less payload.
    """

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, *, params=None, **_kwargs):
        text = str(url)
        tail = text.rsplit("/data/", 1)[-1]
        key = tail.split("/", 1)[1] if "/" in tail else ""
        if "." in key:  # freq.country -> standard path, no country-specific data
            return MockAsyncResponse({"errors": [{"code": 404}]}, status_code=404)
        return MockAsyncResponse(self._payload)  # bare freq -> country-less aggregate


def test_country_less_dataflow_labeled_global_and_deduped():
    provider = BISProvider(metadata_search_service=None)
    client = _RoutingBISClient(_COUNTRY_LESS_PAYLOAD)

    with patch("backend.providers.bis.get_http_client", return_value=client):
        series_list = run(
            provider.fetch_indicator(
                indicator="BIS_WS_CASHLESS",
                countries=["US", "GB", "FR"],
                frequency="A",
            )
        )

    # One global aggregate, not three country-stamped duplicates.
    assert len(series_list) == 1
    meta = series_list[0].metadata
    assert meta.country == "Global"
    # Never mislabeled as any of the requested countries.
    assert meta.country not in {"United States", "United Kingdom", "France"}


def test_country_less_single_country_not_stamped_with_request():
    provider = BISProvider(metadata_search_service=None)
    client = _RoutingBISClient(_COUNTRY_LESS_PAYLOAD)

    with patch("backend.providers.bis.get_http_client", return_value=client):
        series_list = run(
            provider.fetch_indicator(
                indicator="BIS_WS_CASHLESS",
                country="Japan",
                frequency="A",
            )
        )

    assert len(series_list) == 1
    assert series_list[0].metadata.country == "Global"


def test_country_partitioned_fallback_labels_served_country():
    # When the dataflow DOES have a country dimension, the label follows the
    # served series' own country value, not "Global".
    provider = BISProvider(metadata_search_service=None)
    partitioned_payload = {
        "data": {
            "dataSets": [
                {"series": {"0:0:0": {"observations": {"0": [10.0], "1": [11.0]}}}}
            ],
            "structure": {
                "dimensions": {
                    "series": [
                        {"id": "FREQ", "values": [{"id": "A", "name": "Annual"}]},
                        {
                            "id": "REP_CTY",
                            "name": "Reporting country",
                            "values": [{"id": "US", "name": "United States"}],
                        },
                        {"id": "MEASURE", "values": [{"id": "A", "name": "All"}]},
                    ],
                    "observation": [{"id": "TIME_PERIOD", "values": [{"id": "2020"}, {"id": "2021"}]}],
                }
            },
        }
    }

    result = run(
        _fetch_fallback_with_client(provider, partitioned_payload, requested="US")
    )
    assert result is not None
    assert result.metadata.country == "United States"


async def _fetch_fallback_with_client(provider, payload, requested):
    client = _RoutingBISClient(payload)
    with patch("backend.providers.bis.get_http_client", return_value=client):
        return await provider._fetch_exact_dataflow_fallback(  # pylint: disable=protected-access
            indicator_code="WS_CASHLESS",
            indicator_label=None,
            country_code_raw=requested,
            start_year=None,
            end_year=None,
            preferred_frequency="A",
        )
