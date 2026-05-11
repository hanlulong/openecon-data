from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.models import Metadata, NormalizedData
from backend.tests.utils import run
from backend.utils.retry import DataNotAvailableError


try:
    import pybreaker as _pybreaker  # noqa: F401
except ModuleNotFoundError:
    fake_pybreaker = types.ModuleType("pybreaker")

    class _CircuitBreakerError(Exception):  # pragma: no cover - test shim only
        pass

    class _CircuitBreaker:  # pragma: no cover - test shim only
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def call_async(self, func, *args, **kwargs):
            return await func(*args, **kwargs)

    fake_pybreaker.CircuitBreaker = _CircuitBreaker
    fake_pybreaker.CircuitBreakerError = _CircuitBreakerError
    import sys

    sys.modules["pybreaker"] = fake_pybreaker

from backend.providers.imf import IMFProvider


class _MockHTTPResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", json_data=None, headers=None) -> None:
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}
        self.headers = headers or {}
        self.request = SimpleNamespace(url="https://api.imf.org/test")

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


def _bop_structure_metadata(**overrides):
    metadata = {
        "dimension_ids": ["COUNTRY", "BOP_ACCOUNTING_ENTRY", "INDICATOR", "UNIT", "FREQUENCY"],
        "allowed_values_by_dimension": {
            "COUNTRY": {"USA"},
            "BOP_ACCOUNTING_ENTRY": {"BX"},
            "INDICATOR": {"SRLO"},
            "UNIT": {"USD"},
            "FREQUENCY": {"A"},
        },
    }
    metadata.update(overrides)
    return metadata


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
    assert {"dimensionId": "FREQUENCY", "values": ["A"]} in payload["filters"]


def test_validate_bop_structure_candidate_rejects_absent_legacy_dimension_value() -> None:
    provider = IMFProvider(metadata_search_service=None)

    error = provider._validate_bop_structure_candidate(  # pylint: disable=protected-access
        _bop_structure_metadata(),
        indicator_code="BXC_375_XDC",
        countries=["USA"],
    )

    assert error == "INDICATOR value(s) not present in BOP structure: C_375"


def test_build_bop_sdmx_candidates_from_structure_codelist_names() -> None:
    provider = IMFProvider(metadata_search_service=None)
    metadata = _bop_structure_metadata(
        codelist_entries_by_dimension={
            "INDICATOR": [
                {
                    "id": "IN2_S1W",
                    "name": "Secondary income, Financial corporations, nonfinancial corporations, households, and NPISHs",
                },
                {"id": "SC", "name": "Transport"},
            ]
        }
    )

    candidates = provider._build_bop_sdmx_series_candidates(  # pylint: disable=protected-access
        indicator_code="BXISON_BP6_USD",
        indicator_label=(
            "Balance of Payments, Supplementary Items, Current Account, Secondary Income, "
            "Financial corporations, nonfinancial corporations, households, and NPISHs, Credit, NGO's [BPM6], US Dollars"
        ),
        countries=["USA"],
        structure=metadata,
    )

    assert candidates[0]["flow"] == "BOP"
    assert candidates[0]["key"] == "USA.CD_T.IN2_S1W.USD.A"


def test_build_bop_sdmx_candidates_trusts_structure_country_prefix() -> None:
    provider = IMFProvider(metadata_search_service=None)
    metadata = _bop_structure_metadata(
        allowed_values_by_dimension={
            "COUNTRY": {"BRA", "MDV"},
            "BOP_ACCOUNTING_ENTRY": {"A_NFA_T"},
            "INDICATOR": {"O_FL1_S122"},
            "UNIT": {"USD"},
            "FREQUENCY": {"A"},
        },
        codelist_entries_by_dimension={
            "INDICATOR": [
                {
                    "id": "O_FL1_S122",
                    "name": "Other investment, Debt instruments, Deposit-taking corporations",
                }
            ]
        },
    )

    candidates = provider._build_bop_sdmx_series_candidates(  # pylint: disable=protected-access
        indicator_code="MDV_BOP_BFOADDC_USD",
        indicator_label=(
            "Brazil Other investment Debt Instruments Maldives Definition Balance of Payments "
            "Summary of Balance of Payments Financial account Net Acquisition of Financial Assets "
            "Deposit Taking Corporations"
        ),
        countries=["Brazil"],
        structure=metadata,
    )

    assert candidates
    assert {candidate["country"] for candidate in candidates} == {"MDV"}
    assert all(candidate["key"].startswith("MDV.") for candidate in candidates)


def test_fetch_bop_family_uses_public_sdmx_candidate_before_engine() -> None:
    provider = IMFProvider(metadata_search_service=None)
    metadata = _bop_structure_metadata(
        codelist_entries_by_dimension={
            "INDICATOR": [
                {
                    "id": "IN2_S1W",
                    "name": "Secondary income, Financial corporations, nonfinancial corporations, households, and NPISHs",
                }
            ]
        }
    )
    csv_payload = (
        "DATAFLOW,COUNTRY,BOP_ACCOUNTING_ENTRY,INDICATOR,UNIT,FREQUENCY,TIME_PERIOD,OBS_VALUE\n"
        "IMF.STA:BOP(21.0.0),USA,CD_T,IN2_S1W,USD,A,2020,123.4\n"
        "IMF.STA:BOP(21.0.0),USA,CD_T,IN2_S1W,USD,A,2021,125.6\n"
    )
    data_client = _MockHTTPClient(
        post_response=_MockHTTPResponse(status_code=500, text="engine should not be used"),
        get_response=_MockHTTPResponse(status_code=200, text=csv_payload, headers={"content-type": "text/csv"}),
    )
    engine_client = _MockHTTPClient(
        post_response=_MockHTTPResponse(status_code=500, text="engine should not be used"),
        get_response=_MockHTTPResponse(status_code=500, text="engine should not be used"),
    )

    with patch.object(
        provider,
        "_get_imf_dataflow_structure",
        AsyncMock(return_value=metadata),
    ), patch("backend.providers.imf.get_http1_client", return_value=data_client), patch(
        "backend.providers.imf.get_http_client",
        return_value=engine_client,
    ):
        result = run(
            provider._fetch_bop_family(  # pylint: disable=protected-access
                indicator_code="BXISON_BP6_USD",
                indicator_label=(
                    "Balance of Payments, Supplementary Items, Current Account, Secondary Income, "
                    "Financial corporations, nonfinancial corporations, households, and NPISHs, Credit, NGO's [BPM6], US Dollars"
                ),
                countries=["USA"],
                start_year=2020,
                end_year=2021,
            )
        )

    assert len(result) == 1
    assert result[0].metadata.seriesId == "BXISON_BP6_USD"
    assert result[0].metadata.apiUrl.endswith("IMF.STA,BOP/USA.CD_T.IN2_S1W.USD.A?startPeriod=2020&endPeriod=2021")
    assert [(point.date, point.value) for point in result[0].data] == [
        ("2020-01-01", 123.4),
        ("2021-01-01", 125.6),
    ]
    assert data_client.get_calls
    assert engine_client.post_calls == []


def test_xml_bop_structure_codelists_drive_fail_closed_validation() -> None:
    xml = """<?xml version='1.0' encoding='UTF-8'?>
    <mes:Structure xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
        xmlns:str="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure"
        xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common">
      <mes:Structures>
        <str:DataStructures>
          <str:DataStructure agencyID="IMF.STA" id="DSD_BOP" version="24.0.0">
            <str:DataStructureComponents>
              <str:DimensionList>
                <str:Dimension id="COUNTRY" position="0"><str:ConceptIdentity><Ref id="COUNTRY"/></str:ConceptIdentity></str:Dimension>
                <str:Dimension id="BOP_ACCOUNTING_ENTRY" position="1"><str:ConceptIdentity><Ref id="BOP_ACCOUNTING_ENTRY"/></str:ConceptIdentity></str:Dimension>
                <str:Dimension id="INDICATOR" position="2"><str:ConceptIdentity><Ref id="INDICATOR"/></str:ConceptIdentity></str:Dimension>
                <str:Dimension id="UNIT" position="3"><str:ConceptIdentity><Ref id="UNIT"/></str:ConceptIdentity></str:Dimension>
                <str:Dimension id="FREQUENCY" position="4"><str:ConceptIdentity><Ref id="FREQ"/></str:ConceptIdentity></str:Dimension>
              </str:DimensionList>
            </str:DataStructureComponents>
          </str:DataStructure>
        </str:DataStructures>
        <str:Codelists>
          <str:Codelist id="CL_COUNTRY"><str:Code id="USA"/></str:Codelist>
          <str:Codelist id="CL_BOP_ACCOUNTING_ENTRY"><str:Code id="BX"/></str:Codelist>
          <str:Codelist id="CL_BOP_INDICATOR"><str:Code id="SRLO"/></str:Codelist>
          <str:Codelist id="CL_UNIT"><str:Code id="USD"/></str:Codelist>
          <str:Codelist id="CL_FREQ"><str:Code id="A"/></str:Codelist>
        </str:Codelists>
      </mes:Structures>
    </mes:Structure>
    """
    provider = IMFProvider(metadata_search_service=None)
    metadata = IMFProvider._parse_imf_dataflow_structure(xml)  # pylint: disable=protected-access

    assert metadata["allowed_values_by_dimension"]["INDICATOR"] == {"SRLO"}
    error = provider._validate_bop_structure_candidate(  # pylint: disable=protected-access
        metadata,
        indicator_code="BXC_375_XDC",
        countries=["USA"],
    )

    assert error == "INDICATOR value(s) not present in BOP structure: C_375"


def test_fetch_batch_indicator_does_not_route_bop_hint_into_disabled_label_lane() -> None:
    provider = IMFProvider(metadata_search_service=None)

    with patch.object(
        provider,
        "_resolve_indicator_code",
        AsyncMock(return_value=("BXSRLO_USD", "Balance of Payments ... Royalties and License Fees")),
    ), patch.object(
        provider,
        "_fetch_bop_family",
        AsyncMock(side_effect=AssertionError("BOP label bridge must stay disabled")),
    ) as fetch_bop_mock:
        try:
            run(
                provider.fetch_batch_indicator(
                    indicator="ignored",
                    countries=["USA"],
                    start_year=2020,
                    end_year=2021,
                )
            )
        except DataNotAvailableError as exc:
            message = str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected DataNotAvailableError")

    assert "non-DataMapper IMF family" in message
    fetch_bop_mock.assert_not_awaited()


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

    with patch.object(
        provider,
        "_get_imf_dataflow_structure",
        AsyncMock(return_value=_bop_structure_metadata()),
    ), patch("backend.providers.imf.get_http_client", return_value=client):
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
    assert "filter_dimensions=COUNTRY,BOP_ACCOUNTING_ENTRY,INDICATOR,UNIT,FREQUENCY,TIME_PERIOD" in message
    assert client.post_calls
    assert client.get_calls


def test_fetch_bop_family_reports_embedded_engine_error_explicitly() -> None:
    provider = IMFProvider(metadata_search_service=None)
    client = _MockHTTPClient(
        post_response=_MockHTTPResponse(status_code=200, text="test-ott"),
        get_response=_MockHTTPResponse(
            status_code=200,
            text=(
                '{"meta":{},"data":{"dataSets":[{"structure":0}],"structures":[{"dimensions":{"series":[{"id":"COUNTRY","values":[]},{"id":"INDICATOR","values":[]}]}}]}}'
                '{"status":500,"message":"Internal Server Error"}'
            ),
        ),
    )

    with patch.object(
        provider,
        "_get_imf_dataflow_structure",
        AsyncMock(return_value=_bop_structure_metadata()),
    ), patch("backend.providers.imf.get_http_client", return_value=client):
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
    assert "ott_parts=2" in message
    assert "series_dimensions=COUNTRY" in message


def test_fetch_bop_family_fails_closed_when_structure_rejects_candidate() -> None:
    provider = IMFProvider(metadata_search_service=None)
    client = _MockHTTPClient(
        post_response=_MockHTTPResponse(status_code=200, text="test-ott"),
        get_response=_MockHTTPResponse(status_code=200, text="{}"),
    )

    with patch.object(
        provider,
        "_get_imf_dataflow_structure",
        AsyncMock(return_value=_bop_structure_metadata()),
    ), patch("backend.providers.imf.get_http_client", return_value=client):
        try:
            run(
                provider._fetch_bop_family(  # pylint: disable=protected-access
                    indicator_code="BXC_375_XDC",
                    indicator_label="Balance of Payments detail",
                    countries=["USA"],
                    start_year=2020,
                    end_year=2021,
                )
            )
        except DataNotAvailableError as exc:
            message = str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected DataNotAvailableError")

    assert "cannot structurally validate BXC_375_XDC" in message
    assert "INDICATOR value(s) not present" in message
    assert client.post_calls == []
    assert client.get_calls == []


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


def test_decode_engine_ott_parts_handles_concatenated_json_objects() -> None:
    provider = IMFProvider(metadata_search_service=None)
    response_text = (
        '{"meta":{},"data":{"structures":[{"dimensions":{"series":[{"id":"COUNTRY","values":[]}]}}]}}'
        '{"status":500,"message":"Internal Server Error"}'
    )

    parts = provider._decode_engine_ott_parts(response_text)  # pylint: disable=protected-access

    assert len(parts) == 2
    assert parts[0]["data"]["structures"][0]["dimensions"]["series"][0]["id"] == "COUNTRY"
    assert parts[1]["status"] == 500


def test_classify_bop_ott_response_extracts_structure_summary_and_error() -> None:
    provider = IMFProvider(metadata_search_service=None)
    response_text = (
        '{"meta":{},"data":{"structures":[{"dimensions":{"series":[{"id":"COUNTRY","values":[]},{"id":"INDICATOR","values":[]}]}}]}}'
        '{"status":500,"message":"Internal Server Error"}'
    )

    classification = provider._classify_bop_ott_response(response_text)  # pylint: disable=protected-access

    assert classification["kind"] == "embedded_error"
    assert classification["parts"] == 2
    assert classification["error"]["status"] == 500
    assert classification["structure_summary"]["series_dimensions"] == ["COUNTRY", "INDICATOR"]
