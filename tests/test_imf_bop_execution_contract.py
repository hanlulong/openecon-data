from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.models import Metadata, NormalizedData
from backend.tests.utils import run
from backend.utils.retry import DataNotAvailableError


if "pybreaker" not in sys.modules:
    fake_pybreaker = types.ModuleType("pybreaker")

    class _CircuitBreaker:  # pragma: no cover - test shim only
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    fake_pybreaker.CircuitBreaker = _CircuitBreaker
    sys.modules["pybreaker"] = fake_pybreaker

from backend.providers.imf import IMFProvider


class _MockHTTPResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", json_data=None) -> None:
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _MockHTTPClient:
    def __init__(self, *, post_response: _MockHTTPResponse, get_response: _MockHTTPResponse) -> None:
        self.post_response = post_response
        self.get_response = get_response
        self.post_calls = []
        self.get_calls = []

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self.post_response

    async def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return self.get_response


def _sample_series(series_id: str = "NGDP_RPCH") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source="IMF",
            indicator="Real GDP growth",
            country="United States",
            frequency="annual",
            unit="percent",
            lastUpdated="",
            seriesId=series_id,
        ),
        data=[{"date": "2020-01-01", "value": 1.0}],
    )


def test_build_bop_query_payload_splits_code_into_dimensions() -> None:
    provider = IMFProvider(metadata_search_service=None)

    payload = provider._build_bop_query_payload(  # pylint: disable=protected-access
        indicator_code="BXSRLO_USD",
        countries=["USA"],
        start_year=2020,
        end_year=2021,
    )

    assert payload["agencyID"] == "IMF.STA"
    assert payload["resourceID"] == "BOP"
    assert payload["version"] == "21.0.0"
    assert payload["_type"] == "SdmxDataQueryV3"
    assert {"dimensionId": "COUNTRY", "values": ["USA"]} in payload["filters"]
    assert {"dimensionId": "BOP_ACCOUNTING_ENTRY", "values": ["BX"]} in payload["filters"]
    assert {"dimensionId": "INDICATOR", "values": ["SRLO"]} in payload["filters"]
    assert {"dimensionId": "UNIT", "values": ["USD"]} in payload["filters"]


def test_fetch_batch_indicator_routes_bop_hint_into_bop_lane() -> None:
    provider = IMFProvider(metadata_search_service=None)
    fake_result = [_sample_series("BXSRLO_USD")]

    with patch.object(
        provider,
        "_resolve_indicator_code",
        AsyncMock(return_value=("BXSRLO_USD", "Balance of Payments ... Royalties and License Fees")),
    ), patch.object(
        provider,
        "_fetch_bop_family",
        AsyncMock(return_value=fake_result),
    ) as fetch_bop_mock:
        result = run(
            provider.fetch_batch_indicator(
                indicator="ignored",
                countries=["USA"],
                start_year=2020,
                end_year=2021,
            )
        )

    assert result == fake_result
    fetch_bop_mock.assert_awaited_once()


def test_fetch_batch_indicator_keeps_weo_on_datamapper_path() -> None:
    provider = IMFProvider(metadata_search_service=None)
    response = _MockHTTPResponse(
        status_code=200,
        json_data={"values": {"NGDP_RPCH": {"USA": {"2020": 1.1, "2021": 2.2}}}},
    )
    client = _MockHTTPClient(post_response=response, get_response=response)

    with patch.object(
        provider,
        "_resolve_indicator_code",
        AsyncMock(return_value=("NGDP_RPCH", "Real GDP growth")),
    ), patch("backend.providers.imf.get_http_client", return_value=client):
        result = run(
            provider.fetch_batch_indicator(
                indicator="ignored",
                countries=["USA"],
                start_year=2020,
                end_year=2021,
            )
        )

    assert len(result) == 1
    assert result[0].metadata.seriesId == "NGDP_RPCH"
    assert client.post_calls == []


def test_fetch_bop_family_reports_ott_retrieval_failure_explicitly() -> None:
    provider = IMFProvider(metadata_search_service=None)
    client = _MockHTTPClient(
        post_response=_MockHTTPResponse(status_code=200, text="test-ott"),
        get_response=_MockHTTPResponse(status_code=503, text="maintenance"),
    )

    with patch("backend.providers.imf.get_http_client", return_value=client):
        try:
            run(
                provider._fetch_bop_family(  # pylint: disable=protected-access
                    indicator_code="BXSRLO_USD",
                    indicator_label="Balance of Payments ... Royalties and License Fees",
                    countries=["USA"],
                    start_year=2020,
                    end_year=2021,
                )
            )
        except DataNotAvailableError as exc:
            message = str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected DataNotAvailableError")

    assert "OTT retrieval is currently unavailable" in message
    assert "payload_fingerprint=" in message
    assert "filter_dimensions=COUNTRY,BOP_ACCOUNTING_ENTRY,INDICATOR,UNIT,TIME_PERIOD" in message
    assert client.post_calls
    assert client.get_calls


def test_fetch_bop_family_reports_embedded_engine_error_explicitly() -> None:
    provider = IMFProvider(metadata_search_service=None)
    client = _MockHTTPClient(
        post_response=_MockHTTPResponse(status_code=200, text="test-ott"),
        get_response=_MockHTTPResponse(
            status_code=200,
            text='{"meta":{},"data":{"dataSets":[]}}{"status":500,"message":"Internal Server Error"}',
        ),
    )

    with patch("backend.providers.imf.get_http_client", return_value=client):
        try:
            run(
                provider._fetch_bop_family(  # pylint: disable=protected-access
                    indicator_code="BXSRLO_USD",
                    indicator_label="Balance of Payments ... Royalties and License Fees",
                    countries=["USA"],
                    start_year=2020,
                    end_year=2021,
                )
            )
        except DataNotAvailableError as exc:
            message = str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected DataNotAvailableError")

    assert "embedded error 500" in message
    assert "payload_fingerprint=" in message


def test_payload_fingerprint_is_stable_for_equivalent_payloads() -> None:
    provider = IMFProvider(metadata_search_service=None)
    payload_a = provider._build_bop_query_payload(  # pylint: disable=protected-access
        indicator_code="BXSRLO_USD",
        countries=["USA"],
        start_year=2020,
        end_year=2021,
    )
    payload_b = provider._build_bop_query_payload(  # pylint: disable=protected-access
        indicator_code="BXSRLO_USD",
        countries=["USA"],
        start_year=2020,
        end_year=2021,
    )

    assert provider._payload_fingerprint(payload_a) == provider._payload_fingerprint(payload_b)  # pylint: disable=protected-access
