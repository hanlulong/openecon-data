from __future__ import annotations

import importlib.util
import sys
import json
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validation" / "run_certification.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_certification_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_response_signals_detects_clarification_payload():
    module = load_module()

    signals = module.extract_response_signals(
        {
            "clarificationNeeded": True,
            "clarificationOptions": [{"label": "GDP growth"}, {"label": "GDP per capita"}],
            "clarificationQuestions": ["Which GDP variant?"],
            "response": "Could you clarify which GDP variant you want?",
        }
    )

    assert signals["clarification_detected"] is True
    assert signals["clarification_options_count"] == 2
    assert signals["clarification_questions_count"] == 1
    assert signals["response_text_present"] is True
    assert signals["series_count"] == 0


def test_extract_response_signals_counts_nested_datasets_with_values():
    module = load_module()

    signals = module.extract_response_signals(
        {
            "data": {
                "datasets": [
                    {"metadata": {"source": "FRED", "country": "United States", "seriesId": "GDP"}, "observations": [{"date": "2024", "value": 1}]},
                    {"metadata": {"source": "IMF", "country": "Japan", "seriesId": "NGDP"}, "data": [{"x": 1}]},
                ]
            },
            "response": "Here is the comparison.",
        }
    )

    assert signals["clarification_detected"] is False
    assert signals["series_count"] == 2
    assert signals["response_text_present"] is True
    assert signals["providers"] == ["FRED", "IMF"]
    assert signals["countries"] == ["Japan", "United States"]
    assert signals["series_ids"] == ["GDP", "NGDP"]


def test_runtime_supportability_reason_classifies_eurostat_not_disseminated():
    module = load_module()

    reason = module.runtime_supportability_reason(
        {
            "id": "direct-eurostat-not-disseminated",
            "provider_stratum": "Eurostat",
            "query": "Historical one-off dataset from Eurostat",
        },
        {
            "error": "data_not_available",
            "message": (
                "fail-closed supportability block: "
                "reason=eurostat_dataset_not_disseminated; "
                "dataset=lfso_19fxwt05; country=ALL_AVAILABLE"
            ),
        },
    )

    assert reason == "eurostat_dataset_not_disseminated"


def test_runtime_supportability_reason_classifies_imf_detailed_surface_message():
    module = load_module()

    reason = module.runtime_supportability_reason(
        {
            "id": "direct-imf-detail",
            "provider_stratum": "IMF",
            "query": "Detailed IMF public surface from IMF",
        },
        {
            "error": "data_not_available",
            "message": (
                "IMF query targets a detailed IMF public-data surface that is "
                "not yet executable by OpenEcon's production IMF dataset-family "
                "routing. This is a fail-closed supportability block."
            ),
        },
    )

    assert reason == "imf_non_weo_public_surface_unsupported"


def test_extract_response_signals_preserves_answer_surface_metadata():
    module = load_module()

    signals = module.extract_response_signals(
        {
            "data": [
                {
                    "metadata": {
                        "source": "Statistics Canada",
                        "country": "Ontario",
                        "indicator": "employment - Ontario, Men+",
                        "seriesId": "14100375:7.3.1.2.1.0.0.0.0.0",
                        "apiUrl": "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromCubePidCoordAndLatestNPeriods",
                        "sourceUrl": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1410037501",
                    },
                    "data": [{"date": "2025-01-01", "value": 100.0}],
                }
            ]
        }
    )

    assert signals["countries"] == ["Ontario"]
    assert signals["indicators"] == ["employment - Ontario, Men+"]
    assert signals["api_urls"] == ["https://www150.statcan.gc.ca/t1/wds/rest/getDataFromCubePidCoordAndLatestNPeriods"]
    assert signals["source_urls"] == ["https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1410037501"]


def test_extract_response_signals_records_dataset_time_ranges_from_metadata_and_points():
    module = load_module()

    signals = module.extract_response_signals(
        {
            "data": [
                {
                    "metadata": {
                        "source": "IMF",
                        "country": "Canada",
                        "indicator": "Real GDP growth",
                        "seriesId": "NGDP_RPCH",
                        "startDate": "2020",
                        "endDate": "2025",
                    },
                    "data": [{"date": "2020", "value": 1.0}, {"date": "2025", "value": 2.0}],
                },
                {
                    "metadata": {
                        "source": "FRED",
                        "country": "United States",
                        "seriesId": "GDP",
                    },
                    "observations": [{"date": "2019-01-01", "value": 1}, {"date": "2024-01-01", "value": 2}],
                },
            ]
        }
    )

    assert sorted(signals["dataset_time_ranges"], key=lambda item: item["provider"]) == [
        {
            "country": "United States",
            "endDate": "2024-01-01",
            "indicator": "",
            "provider": "FRED",
            "seriesId": "GDP",
            "startDate": "2019-01-01",
        },
        {
            "country": "Canada",
            "endDate": "2025",
            "indicator": "Real GDP growth",
            "provider": "IMF",
            "seriesId": "NGDP_RPCH",
            "startDate": "2020",
        },
    ]


def test_progress_sidecar_paths_and_summary(tmp_path: Path):
    module = load_module()

    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    meta_path = module.progress_meta_path(output_path)

    assert progress_path.name == "results.jsonl.inprogress"
    assert meta_path.name == "results.jsonl.progress.json"

    module.write_progress_summary(
        meta_path,
        completed_sessions=2,
        total_sessions=5,
        results_written=7,
        done=False,
        last_session_id="session-2",
        last_dataset_type="multiround",
    )
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["updated_at_utc"]
    payload.pop("updated_at_utc")
    assert payload == {
        "completed_sessions": 2,
        "total_sessions": 5,
        "results_written": 7,
        "done": False,
        "last_session_id": "session-2",
        "last_dataset_type": "multiround",
    }


def test_execute_rows_preserves_inprogress_when_resume_enabled(tmp_path: Path, monkeypatch):
    module = load_module()
    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    progress_path.write_text(json.dumps({"session_id": "existing"}) + "\n", encoding="utf-8")

    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    rows = [{"id": "direct-1", "query": "US GDP"}]
    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=module.progress_meta_path(output_path),
        existing_results=[{"session_id": "existing"}],
        preserve_progress_output=True,
    )

    assert len(calls) == 1
    assert results[0]["session_id"] == "existing"
    lines = progress_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"session_id": "existing"}
    assert json.loads(lines[1])["session_id"] == "direct-1"


def test_execute_rows_passes_custom_request_timeout(monkeypatch):
    module = load_module()
    timeouts: list[float] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        timeouts.append(timeout)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    module.execute_rows(
        [{"id": "direct-1", "query": "US GDP"}],
        "http://localhost:3001",
        request_timeout=12.5,
    )

    assert timeouts == [12.5]


def test_execute_rows_retries_429_and_applies_request_spacing(monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {"Retry-After": "0.25"} if status_code == 429 else {}

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    responses = [FakeResponse(429), FakeResponse(200)]

    def fake_post(url, json, timeout):
        calls.append(json)
        return responses.pop(0)

    monkeypatch.setattr(module.requests, "post", fake_post)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleeps.append(seconds))

    results = module.execute_rows(
        [{"id": "direct-1", "query": "US GDP"}],
        "http://localhost:3001",
        request_spacing=0.5,
        rate_limit_retries=1,
        rate_limit_backoff=10,
    )

    assert calls == [{"query": "US GDP"}, {"query": "US GDP"}]
    assert sleeps == [0.25, 0.5]
    assert results[0]["status_code"] == 200
    assert results[0]["series_count"] == 1


def test_execute_rows_concurrent_passes_custom_request_timeout(monkeypatch):
    module = load_module()
    timeouts: list[float] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        timeouts.append(timeout)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    module.execute_rows(
        [{"id": "direct-1", "query": "US GDP"}],
        "http://localhost:3001",
        concurrency=2,
        request_timeout=9,
    )

    assert timeouts == [9]


def test_select_rows_applies_start_index_before_max_sessions():
    module = load_module()
    rows = [
        {"id": "direct-1", "query": "first"},
        {"id": "direct-2", "query": "second"},
        {"id": "direct-3", "query": "third"},
        {"id": "direct-4", "query": "fourth"},
    ]

    selected = module.select_rows(rows, start_index=2, max_sessions=1)

    assert [row["id"] for row in selected] == ["direct-3"]


def test_execute_rows_rejects_negative_start_index(tmp_path: Path):
    module = load_module()

    try:
        module.execute_rows([], "http://localhost:3001", start_index=-1)
    except ValueError as exc:
        assert "start_index" in str(exc)
    else:
        raise AssertionError("negative start_index should fail")


def test_execute_rows_rejects_invalid_concurrency():
    module = load_module()

    with pytest.raises(ValueError, match="concurrency"):
        module.execute_rows([], "http://localhost:3001", concurrency=0)


def test_execute_rows_concurrent_preserves_session_outputs(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, source: str):
            self.source = source

        def json(self):
            return {
                "data": [
                    {
                        "metadata": {"source": self.source, "seriesId": self.source.upper()},
                        "observations": [{"date": "2024", "value": 1}],
                    }
                ]
            }

    def fake_post(url, json, timeout):
        calls.append(json["query"])
        return FakeResponse(json["query"])

    monkeypatch.setattr(module.requests, "post", fake_post)
    output_path = tmp_path / "results.jsonl"
    rows = [
        {"id": "direct-1", "query": "fred"},
        {"id": "direct-2", "query": "imf"},
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=module.progress_output_path(output_path),
        progress_meta=module.progress_meta_path(output_path),
        concurrency=2,
    )

    assert sorted(calls) == ["fred", "imf"]
    assert {record["session_id"] for record in results} == {"direct-1", "direct-2"}
    assert {record["series_count"] for record in results} == {1}
    progress = json.loads(module.progress_meta_path(output_path).read_text(encoding="utf-8"))
    assert progress["done"] is True
    assert progress["concurrency"] == 2


def test_preflight_can_separate_unsupported_imf_from_executable_high_risk(tmp_path: Path):
    module = load_module()
    unsupported_imf = {
        "id": "direct-imf-1",
        "provider_stratum": "IMF",
        "query": "Germany Merchandise Trade Value of Exports Chapter 60 from IMF",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "TXG_H5_60_EUR",
            "category": "INDICATOR",
            "name": "Merchandise Trade, Value of Exports. Chapter 60- Knitted goods, Euros",
        },
    }
    executable_high_risk = {
        "id": "direct-fred-1",
        "provider_stratum": "FRED",
        "query": "US AL Market Hotness: Page View Count per Property in Jefferson County from FRED",
        "origin": {
            "source_provider": "FRED",
            "name": "AL Market Hotness: Page View Count per Property in Jefferson County",
        },
    }

    unsupported_only = module.preflight_audit_rows(
        [unsupported_imf],
        classify_unsupported_direct=True,
    )
    assert unsupported_only["summary"]["high_risk_rows"] == 1
    assert unsupported_only["summary"]["supportability_blocked_rows"] == 1
    assert unsupported_only["summary"]["execution_high_risk_rows"] == 0
    assert unsupported_only["flagged_rows_sample"][0]["execution_mode"] == "supportability_blocked"
    module.enforce_preflight_audit(
        tmp_path / "unsupported.json",
        [unsupported_imf],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )

    mixed = module.preflight_audit_rows(
        [unsupported_imf, executable_high_risk],
        classify_unsupported_direct=True,
    )
    assert mixed["summary"]["high_risk_rows"] == 2
    assert mixed["summary"]["supportability_blocked_rows"] == 1
    assert mixed["summary"]["execution_high_risk_rows"] == 1
    with pytest.raises(RuntimeError, match="executable high-risk direct rows"):
        module.enforce_preflight_audit(
            tmp_path / "mixed.json",
            [unsupported_imf, executable_high_risk],
            allow_high_risk_direct=False,
            classify_unsupported_direct=True,
        )


def test_preflight_classifies_oecd_non_production_dataflow_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    unsupported_oecd = {
        "id": "direct-oecd-1",
        "provider_stratum": "OECD",
        "query": "America Hourly earnings ratio of formal to informal employees from OECD",
        "origin": {
            "source_provider": "OECD",
            "source_indicator_code": "DSD_KIIBIH@DF_B18",
            "name": "Hourly earnings ratio of formal to informal employees",
            "raw_metadata": json.dumps(
                {
                    "annotations": [
                        {"type": "NonProductionDataflow", "text": "true"},
                    ]
                }
            ),
        },
    }

    audit = module.preflight_audit_rows(
        [unsupported_oecd],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "oecd_non_production_dataflow_unsupported": 1
    }
    assert audit["flagged_rows_sample"][0]["execution_mode"] == "supportability_blocked"
    module.enforce_preflight_audit(
        tmp_path / "unsupported-oecd.json",
        [unsupported_oecd],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_preflight_classifies_imf_price_or_memorandum_family_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    unsupported_imf = {
        "id": "direct-imf-price",
        "provider_stratum": "IMF",
        "query": "Germany Other Manufacturing Producer Price Index Commodities by Activity from IMF",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "PPI_OMFG_IX",
            "name": "Producer Price Index, Commodities by Activity, Other Manufacturing",
            "category": "INDICATOR",
        },
    }

    audit = module.preflight_audit_rows(
        [unsupported_imf],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "imf_non_weo_public_surface_unsupported": 1
    }
    assert audit["flagged_rows_sample"][0]["execution_mode"] == "supportability_blocked"
    module.enforce_preflight_audit(
        tmp_path / "unsupported-imf-price.json",
        [unsupported_imf],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_preflight_classifies_high_risk_non_weo_imf_surface_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    unsupported_imf = {
        "id": "direct-imf-gfs",
        "provider_stratum": "IMF",
        "query": (
            "Brazil General Government Memorandum items Debt items Domestic creditors Government and Public Sector "
            "Finance Stocks in Assets and Liabilities Gross debt Maastricht debt liabilities Long-term by original "
            "maturity with payment due in more than one year from IMF"
        ),
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "GALDDEU_L_1YP_G14_GG_XDC",
            "name": (
                "General Government Memorandum items Debt items Domestic creditors Government and Public Sector "
                "Finance Stocks in Assets and Liabilities Gross debt Maastricht debt liabilities Long-term by original "
                "maturity with payment due in more than one year"
            ),
            "category": "INDICATOR",
        },
    }

    audit = module.preflight_audit_rows(
        [unsupported_imf],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "imf_non_weo_public_surface_unsupported": 1
    }
    module.enforce_preflight_audit(
        tmp_path / "unsupported-imf-gfs.json",
        [unsupported_imf],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_preflight_classifies_imf_bop_detail_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    unsupported_bop = {
        "id": "direct-imf-bop",
        "provider_stratum": "IMF",
        "query": (
            "Panama Current account Balance of Payments Primary Income Investment Income "
            "Reserve Assets Credit from IMF"
        ),
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "PAN_BOP6_C_375_XDC",
            "name": (
                "Current account Balance of Payments Primary Income Investment Income "
                "Reserve Assets Credit"
            ),
            "category": "INDICATOR",
        },
    }

    audit = module.preflight_audit_rows(
        [unsupported_bop],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "imf_non_weo_public_surface_unsupported": 1
    }
    flagged = audit["flagged_rows_sample"][0]
    assert flagged["execution_mode"] == "supportability_blocked"
    assert "imf_complex_finance_family" in flagged["reasons"]
    assert "imf_low_viability_family" in flagged["reasons"]
    module.enforce_preflight_audit(
        tmp_path / "unsupported-imf-bop.json",
        [unsupported_bop],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_execute_rows_keeps_imf_bop_detail_off_runtime(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    def fake_post(url, json, timeout):
        calls.append(json)
        raise AssertionError("unsupported IMF BOP row should not hit runtime")

    monkeypatch.setattr(module.requests, "post", fake_post)
    row = {
        "id": "direct-imf-bop",
        "provider_stratum": "IMF",
        "query": (
            "Panama Current account Balance of Payments Primary Income Investment Income "
            "Reserve Assets Credit from IMF"
        ),
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "PAN_BOP6_C_375_XDC",
            "category": "INDICATOR",
            "name": (
                "Current account Balance of Payments Primary Income Investment Income "
                "Reserve Assets Credit"
            ),
        },
    }

    results = module.execute_rows(
        [row],
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        classify_unsupported_direct=True,
        runtime_unavailable_reason="health probe timed out",
    )

    assert calls == []
    assert len(results) == 1
    record = results[0]
    assert record["session_id"] == "direct-imf-bop"
    assert record["supportability_blocked"] is True
    assert record["supportability_reason"] == "imf_non_weo_public_surface_unsupported"
    assert "runtime_unavailable" not in record


def test_user_answerability_preblocks_explicit_imf_selection_supportability_probe():
    module = load_module()
    row = {
        "id": "batch-direct-imf-unsupported",
        "provider_stratum": "IMF",
        "evaluation_target": "user_answerability",
        "query": "NXG_H5_XII_FOB_USD from IMF",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "NXG_H5_XII_FOB_USD",
            "category": "INDICATOR",
            "name": "National Accounts, External Sector, Exports of Goods, HS 2017 Section XII",
        },
        "provenance": {
            "certification_target": "user_answerability",
            "selection_supportability_reason": "imf_non_weo_public_surface_unsupported",
            "supportability_probe_query": "imf_exact_provider_code",
        },
    }
    audit = module.audit_direct_query_shape(row)

    assert module.unsupported_direct_surface_reason(row, audit) == "imf_non_weo_public_surface_unsupported"
    blocked = module.supportability_blocked_record(row)
    assert blocked is not None
    assert blocked["supportability_blocked"] is True
    assert blocked["supportability_reason"] == "imf_non_weo_public_surface_unsupported"


def test_preflight_keeps_runnable_oecd_non_production_dataflow_executable():
    module = load_module()
    runnable_oecd = {
        "id": "direct-oecd-runnable",
        "provider_stratum": "OECD",
        "query": "Canada Average usual weekly hours worked on the main job from OECD",
        "origin": {
            "source_provider": "OECD",
            "source_indicator_code": "DSD_HW@DF_AVG_USL_WK_WKD",
            "name": "Average usual weekly hours worked on the main job",
            "raw_metadata": json.dumps(
                {
                    "annotations": [
                        {"type": "NonProductionDataflow", "text": "true"},
                    ]
                }
            ),
        },
    }

    audit = module.preflight_audit_rows(
        [runnable_oecd],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_rows"] == 0
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["flagged_rows_sample"][0]["execution_mode"] == "ordinary_runtime"



def test_preflight_allows_worldbank_source_endpoint_indicators_to_execute(tmp_path: Path):
    module = load_module()
    niche_worldbank = {
        "id": "direct-worldbank-niche",
        "provider_stratum": "WorldBank",
        "query": "China Annualized Mean Income Growth Bottom 40 Percent 2004-2014 from World Bank",
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "5.0.AMeanIncGr.B40",
            "name": "Annualized Mean Income Growth Bottom 40 Percent (2004-2014)",
            "category": "LAC Equity Lab",
        },
    }
    specialized_worldbank = {
        "id": "direct-worldbank-specialized",
        "provider_stratum": "WorldBank",
        "query": "Brazil Time to export days DB06-15 methodology from World Bank",
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "TRD.ACRS.BRDR.EXPT.DURS.DY.DB0615",
            "name": "Time to export (days) (DB06-15 methodology)",
            "category": "Doing Business",
        },
    }

    audit = module.preflight_audit_rows(
        [niche_worldbank, specialized_worldbank],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 2
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "worldbank_niche_catalog_unsupported": 1,
    }
    modes_by_id = {row["id"]: row["execution_mode"] for row in audit["flagged_rows_sample"]}
    assert modes_by_id == {
        "direct-worldbank-niche": "supportability_blocked",
        "direct-worldbank-specialized": "ordinary_runtime",
    }
    with pytest.raises(RuntimeError, match="1 executable high-risk direct rows"):
        module.enforce_preflight_audit(
            tmp_path / "unsupported-worldbank.json",
            [niche_worldbank, specialized_worldbank],
            allow_high_risk_direct=False,
            classify_unsupported_direct=True,
        )


def test_preflight_classifies_worldbank_country_availability_surface_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    cpia_unavailable = {
        "id": "direct-worldbank-cpia",
        "provider_stratum": "WorldBank",
        "query": "Japan CPIA structural policies cluster average from World Bank",
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "IQ.CPA.STRC.XQ",
            "name": "CPIA structural policies cluster average (1=low to 6=high)",
            "category": "World Development Indicators",
        },
    }

    audit = module.preflight_audit_rows(
        [cpia_unavailable],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "worldbank_country_availability_surface": 1,
    }
    assert audit["flagged_rows_sample"][0]["execution_mode"] == "supportability_blocked"
    module.enforce_preflight_audit(
        tmp_path / "unsupported-worldbank-country-availability.json",
        [cpia_unavailable],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_preflight_classifies_worldbank_low_viability_families_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    ddh_worldbank = {
        "id": "direct-worldbank-ddh",
        "provider_stratum": "WorldBank",
        "query": "Brazil Persons owing a mobility phone (% of persons with any degree of functional difficulty) from World Bank",
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "ocel_any_dfcl_all",
            "name": "Persons owing a mobility phone (% of persons with any degree of functional difficulty)",
            "category": "Disability Data Hub (DDH)",
        },
    }
    education_worldbank = {
        "id": "direct-worldbank-education",
        "provider_stratum": "WorldBank",
        "query": "India Government expenditure on tertiary education PPP$ (millions) from World Bank",
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "UIS.X.PPP.5T8.FSGOV",
            "name": "Government expenditure on tertiary education, PPP$ (millions)",
            "category": "Education Statistics",
        },
    }
    demographic_worldbank = {
        "id": "direct-worldbank-demographic",
        "provider_stratum": "WorldBank",
        "query": "India ages 11-16 female Population from World Bank",
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "SP.POP.1116.FE.UN",
            "name": "Population, ages 11-16, female",
            "category": "Education Statistics",
        },
    }
    policy_worldbank = {
        "id": "direct-worldbank-policy",
        "provider_stratum": "WorldBank",
        "query": "033_Are commercial banks permitted to distribute insurance?_#VGDA_07 from World Bank",
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "FB.INC.INST.PA.CB.IN",
            "name": "033_Are commercial banks permitted to distribute insurance?_#VGDA_07",
            "category": "Global Financial Inclusion and Consumer Protection Survey",
        },
    }

    audit = module.preflight_audit_rows(
        [ddh_worldbank, education_worldbank, demographic_worldbank, policy_worldbank],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 4
    assert audit["summary"]["supportability_blocked_rows"] == 4
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "worldbank_niche_catalog_unsupported": 4,
    }
    assert {row["execution_mode"] for row in audit["flagged_rows_sample"]} == {"supportability_blocked"}
    module.enforce_preflight_audit(
        tmp_path / "unsupported-worldbank-low-viability.json",
        [ddh_worldbank, education_worldbank, demographic_worldbank, policy_worldbank],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_preflight_classifies_high_risk_worldbank_ddh_category_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    ddh_worldbank = {
        "id": "direct-worldbank-ddh-shock",
        "provider_stratum": "WorldBank",
        "query": (
            "Brazil Persons aged 15 to 29 years in households that experienced a shock recently "
            "(% of persons aged 15 to 29 years with none or some degree of functional difficulty) from World Bank"
        ),
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "shck_nosome_dfcl_1529",
            "name": (
                "Persons aged 15 to 29 years in households that experienced a shock recently "
                "(% of persons aged 15 to 29 years with none or some degree of functional difficulty)"
            ),
            "category": "Disability Data Hub (DDH)",
        },
    }

    audit = module.preflight_audit_rows(
        [ddh_worldbank],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "worldbank_niche_catalog_unsupported": 1,
    }
    module.enforce_preflight_audit(
        tmp_path / "unsupported-worldbank-ddh-category.json",
        [ddh_worldbank],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_preflight_classifies_worldbank_ddh_any_difficulty_family_as_supportability_blocked(tmp_path: Path):
    module = load_module()
    ddh_worldbank = {
        "id": "direct-worldbank-ddh-informal-work",
        "provider_stratum": "WorldBank",
        "query": (
            "Brazil Persons in informal work (% of persons with any degree of functional difficulty) "
            "from World Bank"
        ),
        "origin": {
            "source_provider": "WorldBank",
            "source_indicator_code": "ifrm_any_dfcl_all",
            "name": "Persons in informal work (% of persons with any degree of functional difficulty)",
            "category": "Disability Data Hub (DDH)",
        },
    }

    audit = module.preflight_audit_rows(
        [ddh_worldbank],
        classify_unsupported_direct=True,
    )

    assert audit["summary"]["high_risk_rows"] == 1
    assert audit["summary"]["supportability_blocked_rows"] == 1
    assert audit["summary"]["execution_high_risk_rows"] == 0
    assert audit["summary"]["supportability_blocked_reason_counts"] == {
        "worldbank_niche_catalog_unsupported": 1,
    }
    assert audit["flagged_rows_sample"][0]["execution_mode"] == "supportability_blocked"
    module.enforce_preflight_audit(
        tmp_path / "unsupported-worldbank-ddh-any-difficulty.json",
        [ddh_worldbank],
        allow_high_risk_direct=False,
        classify_unsupported_direct=True,
    )


def test_execute_rows_classifies_unsupported_direct_without_runtime_call(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    def fake_post(url, json, timeout):
        calls.append(json)
        raise AssertionError("unsupported IMF row should not hit runtime")

    monkeypatch.setattr(module.requests, "post", fake_post)
    row = {
        "id": "direct-imf-1",
        "provider_stratum": "IMF",
        "query": "Germany Merchandise Trade Value of Exports Chapter 60 from IMF",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "TXG_H5_60_EUR",
            "category": "INDICATOR",
            "name": "Merchandise Trade, Value of Exports. Chapter 60- Knitted goods, Euros",
        },
    }

    results = module.execute_rows(
        [row],
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        classify_unsupported_direct=True,
    )

    assert calls == []
    assert len(results) == 1
    record = results[0]
    assert record["session_id"] == "direct-imf-1"
    assert record["supportability_blocked"] is True
    assert record["supportability_reason"] == "imf_non_weo_public_surface_unsupported"
    assert record["status_code"] is None
    assert record["series_count"] == 0
    assert "imf_low_viability_family" in record["query_quality_reasons"]
    assert module.completed_session_ids_from_results([row], results) == {"direct-imf-1"}



def test_record_response_marks_coingecko_provider_no_price_as_supportability_blocked():
    module = load_module()

    class Resp:
        status_code = 200

    row = {
        "id": "direct-coingecko-rgb",
        "provider_stratum": "CoinGecko",
        "query": "Rgb cryptocurrency price from CoinGecko",
        "origin": {"source_provider": "CoinGecko", "source_indicator_code": "rgb"},
    }
    response_payload = {
        "error": "data_not_available",
        "message": (
            "No CoinGecko Price Available. Provider evidence: "
            "reason=coingecko_price_unavailable; requested_ids=rgb"
        ),
    }

    record = module.record_response(row, "direct", 1, row["query"], Resp(), 0.25, response_payload)

    assert record["supportability_blocked"] is True
    assert record["supportability_reason"] == "coingecko_price_unavailable"
    assert record["error"] == "data_not_available"
    assert record["message"] == response_payload["message"]
    assert record["series_count"] == 0


def test_record_response_marks_statscan_required_dimension_missing_as_supportability_blocked():
    module = load_module()

    class Resp:
        status_code = 200

    row = {
        "id": "direct-statscan-first-name",
        "provider_stratum": "StatsCan",
        "query": "Canada selected indicators First names at birth by sex at birth from Statistics Canada",
        "origin": {"source_provider": "StatsCan", "source_indicator_code": "17100147"},
    }
    response_payload = {
        "error": "data_not_available",
        "message": (
            "fail-closed supportability block: "
            "reason=statscan_required_dimension_missing; "
            "product=17100147; "
            "missing_dimensions=First name at birth"
        ),
    }

    record = module.record_response(row, "direct", 1, row["query"], Resp(), 0.25, response_payload)

    assert record["supportability_blocked"] is True
    assert record["supportability_reason"] == "statscan_required_dimension_missing"
    assert record["error"] == "data_not_available"
    assert record["series_count"] == 0


def test_record_response_marks_eurostat_response_too_large_as_supportability_blocked():
    module = load_module()

    class Resp:
        status_code = 200

    row = {
        "id": "direct-eurostat-large",
        "provider_stratum": "Eurostat",
        "query": "EF_RD_LEG from Eurostat",
        "origin": {"source_provider": "Eurostat", "source_indicator_code": "EF_RD_LEG"},
    }
    response_payload = {
        "error": "data_not_available",
        "message": (
            "fail-closed supportability block: "
            "reason=eurostat_response_too_large; "
            "dataset=ef_rd_leg; "
            "country=ALL_AVAILABLE"
        ),
    }

    record = module.record_response(row, "direct", 1, row["query"], Resp(), 0.25, response_payload)

    assert record["supportability_blocked"] is True
    assert record["supportability_reason"] == "eurostat_response_too_large"
    assert record["error"] == "data_not_available"
    assert record["series_count"] == 0


def test_record_response_marks_eurostat_requested_geo_unavailable_as_supportability_blocked():
    module = load_module()

    class Resp:
        status_code = 200

    row = {
        "id": "direct-eurostat-country-miss",
        "provider_stratum": "Eurostat",
        "query": "Germany HSW_HP_SVCLN from Eurostat",
        "origin": {"source_provider": "Eurostat", "source_indicator_code": "HSW_HP_SVCLN"},
    }
    response_payload = {
        "error": "data_not_available",
        "message": (
            "fail-closed supportability block: "
            "reason=eurostat_requested_geo_unavailable; "
            "dataset=hsw_hp_svcln; "
            "country=DE; "
            "available_geo=EU_V"
        ),
    }

    record = module.record_response(row, "direct", 1, row["query"], Resp(), 0.25, response_payload)

    assert record["supportability_blocked"] is True
    assert record["supportability_reason"] == "eurostat_requested_geo_unavailable"
    assert record["error"] == "data_not_available"
    assert record["series_count"] == 0


def test_record_response_marks_oecd_missing_valued_observations_as_supportability_blocked():
    module = load_module()

    class Resp:
        status_code = 200

    row = {
        "id": "direct-oecd-all-null",
        "provider_stratum": "OECD",
        "query": "United States Population in the National Accounts from OECD",
        "origin": {
            "source_provider": "OECD",
            "source_indicator_code": "DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE",
        },
    }
    response_payload = {
        "error": "data_not_available",
        "message": (
            "oecd_missing_valued_observations: OECD returned 36 observations for "
            "USA DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE, but all observation "
            "values were null/missing; OBS_STATUS=L."
        ),
    }

    record = module.record_response(row, "direct", 1, row["query"], Resp(), 0.25, response_payload)

    assert record["supportability_blocked"] is True
    assert record["supportability_reason"] == "oecd_missing_valued_observations"
    assert record["error"] == "data_not_available"
    assert record["series_count"] == 0
    assert "runtime_unavailable" not in record


def test_record_response_marks_comtrade_quota_as_runtime_unavailable():
    module = load_module()

    class Resp:
        status_code = 200

    row = {
        "id": "direct-comtrade-quota",
        "provider_stratum": "Comtrade",
        "query": "France exports of HS722860 from Comtrade",
        "origin": {"source_provider": "Comtrade", "source_indicator_code": "722860"},
    }
    response_payload = {
        "error": "data_not_available",
        "message": (
            "🚦 **Rate Limit Reached**\n"
            "Error: Comtrade API quota exhausted for reporter FR: HTTP 403; "
            "retry_after_seconds=36305; provider_message=Out of call volume quota."
        ),
    }

    record = module.record_response(row, "direct", 1, row["query"], Resp(), 0.25, response_payload)

    assert record["request_failed"] is True
    assert record["runtime_unavailable"] is True
    assert record["runtime_unavailable_reason"] == "comtrade_api_quota_or_forbidden"
    assert "supportability_blocked" not in record
    assert record["error"] == "data_not_available"


def test_execute_rows_can_fail_closed_when_runtime_unavailable(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    def fake_post(url, json, timeout):
        calls.append(json)
        raise AssertionError("runtime-unavailable mode should not hit HTTP")

    monkeypatch.setattr(module.requests, "post", fake_post)
    rows = [
        {"id": "direct-1", "query": "US GDP"},
        {
            "id": "multi-1",
            "rounds": [
                {"query": "first"},
                {"query": "second"},
            ],
        },
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        runtime_unavailable_reason="health probe timed out",
    )

    assert calls == []
    assert [record["session_id"] for record in results] == ["direct-1", "multi-1", "multi-1"]
    assert [record["round_index"] for record in results] == [1, 1, 2]
    assert all(record["request_failed"] for record in results)
    assert all(record["runtime_unavailable"] for record in results)
    assert {record["runtime_unavailable_reason"] for record in results} == {"health probe timed out"}
    progress = json.loads((tmp_path / "results.jsonl.progress.json").read_text(encoding="utf-8"))
    assert progress["done"] is True
    assert progress["completed_sessions"] == 2


def test_runtime_unavailable_records_count_complete_for_resume():
    module = load_module()
    rows = [
        {"id": "direct-1", "query": "US GDP"},
        {
            "id": "multi-1",
            "rounds": [
                {"query": "first"},
                {"query": "second"},
            ],
        },
    ]
    records = [
        module.record_runtime_unavailable(rows[0], "direct", 1, "US GDP", "health probe timed out"),
        module.record_runtime_unavailable(rows[1], "multiround", 1, "first", "health probe timed out"),
        module.record_runtime_unavailable(rows[1], "multiround", 2, "second", "health probe timed out"),
    ]

    assert module.completed_session_ids_from_results(rows, records) == {"direct-1", "multi-1"}
    assert [record["session_id"] for record in module.keep_complete_session_records(rows, records)] == [
        "direct-1",
        "multi-1",
        "multi-1",
    ]


def test_runtime_unavailable_mode_preserves_supportability_classification_first(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    def fake_post(url, json, timeout):
        calls.append(json)
        raise AssertionError("runtime-unavailable mode should not hit HTTP")

    monkeypatch.setattr(module.requests, "post", fake_post)
    row = {
        "id": "direct-imf-1",
        "provider_stratum": "IMF",
        "query": "Germany Merchandise Trade Value of Exports Chapter 60 from IMF",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "TXG_H5_60_EUR",
            "category": "INDICATOR",
            "name": "Merchandise Trade, Value of Exports. Chapter 60- Knitted goods, Euros",
        },
    }

    results = module.execute_rows(
        [row],
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        classify_unsupported_direct=True,
        runtime_unavailable_reason="health probe timed out",
    )

    assert calls == []
    assert len(results) == 1
    assert results[0]["supportability_blocked"] is True
    assert "runtime_unavailable" not in results[0]


def test_execute_rows_concurrent_can_fail_closed_when_runtime_unavailable(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    def fake_post(url, json, timeout):
        calls.append(json)
        raise AssertionError("runtime-unavailable mode should not hit HTTP")

    monkeypatch.setattr(module.requests, "post", fake_post)
    results = module.execute_rows(
        [{"id": "direct-1", "query": "US GDP"}],
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        concurrency=2,
        runtime_unavailable_reason="health probe timed out",
    )

    assert calls == []
    assert len(results) == 1
    assert results[0]["runtime_unavailable"] is True
    progress = json.loads((tmp_path / "results.jsonl.progress.json").read_text(encoding="utf-8"))
    assert progress["done"] is True
    assert progress["concurrency"] == 2


def test_execute_rows_concurrent_skips_unsupported_direct_but_runs_supported(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)
    rows = [
        {
            "id": "direct-imf-1",
            "provider_stratum": "IMF",
            "query": "Germany Merchandise Trade Value of Exports Chapter 60 from IMF",
            "origin": {
                "source_provider": "IMF",
                "source_indicator_code": "TXG_H5_60_EUR",
                "category": "INDICATOR",
                "name": "Merchandise Trade, Value of Exports. Chapter 60- Knitted goods, Euros",
            },
        },
        {"id": "direct-fred-1", "provider_stratum": "FRED", "query": "US GDP"},
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        concurrency=2,
        classify_unsupported_direct=True,
    )

    assert calls == [{"query": "US GDP"}]
    by_session = {record["session_id"]: record for record in results}
    assert by_session["direct-imf-1"]["supportability_blocked"] is True
    assert by_session["direct-fred-1"]["series_count"] == 1


def test_execute_rows_concurrent_keeps_multiround_session_order(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, conversation_id: str):
            self.conversation_id = conversation_id

        def json(self):
            return {
                "conversationId": self.conversation_id,
                "data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}],
            }

    def fake_post(url, json, timeout):
        calls.append(dict(json))
        return FakeResponse("conv-1")

    monkeypatch.setattr(module.requests, "post", fake_post)
    rows = [
        {
            "id": "multi-1",
            "rounds": [
                {"query": "first"},
                {"query": "second"},
            ],
        }
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        concurrency=2,
    )

    assert [call["query"] for call in calls] == ["first", "second"]
    assert calls[1]["conversationId"] == "conv-1"
    assert [record["round_index"] for record in results] == [1, 2]


def test_completed_session_ids_requires_all_multiround_rounds():
    module = load_module()
    rows = [
        {"id": "multi-1", "rounds": [{"query": "first"}, {"query": "second"}]},
        {"id": "direct-1", "query": "US GDP"},
    ]
    partial_records = [
        {"session_id": "multi-1", "round_index": 1},
        {"session_id": "direct-1", "round_index": 1},
    ]

    assert module.completed_session_ids_from_results(rows, partial_records) == {"direct-1"}

    complete_records = partial_records + [{"session_id": "multi-1", "round_index": 2}]
    assert module.completed_session_ids_from_results(rows, complete_records) == {"multi-1", "direct-1"}


def test_completed_session_ids_ignores_failed_request_records():
    module = load_module()
    rows = [{"id": "direct-1", "query": "US GDP"}]
    records = [{"session_id": "direct-1", "round_index": 1, "request_failed": True}]

    assert module.completed_session_ids_from_results(rows, records) == set()
    assert module.keep_complete_session_records(rows, records) == []


def test_skip_completed_does_not_skip_partial_multiround_session(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"conversationId": "conv", "data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    rows = [{"id": "multi-1", "rounds": [{"query": "first"}, {"query": "second"}]}]
    partial_records = [{"session_id": "multi-1", "round_index": 1}]
    completed_ids = module.completed_session_ids_from_results(rows, partial_records)
    complete_existing = module.keep_complete_session_records(rows, partial_records)

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        skip_completed_session_ids=completed_ids,
        existing_results=complete_existing,
        preserve_progress_output=True,
    )

    assert completed_ids == set()
    assert complete_existing == []
    assert [call["query"] for call in calls] == ["first", "second"]
    assert [record["round_index"] for record in results] == [1, 2]


def test_resume_loads_existing_complete_results_before_appending(tmp_path: Path, monkeypatch):
    module = load_module()
    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    progress_path.write_text(json.dumps({"session_id": "direct-1", "round_index": 1}) + "\n", encoding="utf-8")
    existing = module.load_existing_results(progress_path)
    rows = [
        {"id": "direct-1", "query": "first"},
        {"id": "direct-2", "query": "second"},
    ]
    completed_ids = module.completed_session_ids_from_results(rows, existing)
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=module.progress_meta_path(output_path),
        skip_completed_session_ids=completed_ids,
        existing_results=existing,
        preserve_progress_output=True,
    )

    assert completed_ids == {"direct-1"}
    assert [call["query"] for call in calls] == ["second"]
    assert [record["session_id"] for record in results] == ["direct-1", "direct-2"]


def test_execute_rows_concurrency_runs_sessions_and_preserves_multiround_order(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, query: str):
            self.query = query

        def json(self):
            return {
                "conversationId": f"conv-{self.query}",
                "data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}],
            }

    def fake_post(url, json, timeout):
        calls.append(dict(json))
        return FakeResponse(json["query"])

    monkeypatch.setattr(module.requests, "post", fake_post)
    rows = [
        {"id": "multi-1", "rounds": [{"query": "first"}, {"query": "second"}]},
        {"id": "direct-1", "query": "direct"},
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        concurrency=2,
    )

    assert sorted(record["session_id"] for record in results) == ["direct-1", "multi-1", "multi-1"]
    first_payload = next(call for call in calls if call["query"] == "first")
    second_payload = next(call for call in calls if call["query"] == "second")
    assert "conversationId" not in first_payload
    assert second_payload["conversationId"] == "conv-first"
    progress = json.loads((tmp_path / "results.jsonl.progress.json").read_text(encoding="utf-8"))
    assert progress["done"] is True
    assert progress["concurrency"] == 2


def test_execute_rows_concurrency_writes_failure_checkpoint_before_reraising(tmp_path: Path, monkeypatch):
    module = load_module()

    def fake_post(url, json, timeout):
        raise module.requests.Timeout("timed out")

    monkeypatch.setattr(module.requests, "post", fake_post)
    progress_path = tmp_path / "results.jsonl.inprogress"
    meta_path = tmp_path / "results.jsonl.progress.json"

    with pytest.raises(module.requests.Timeout):
        module.execute_rows(
            [{"id": "direct-1", "query": "US GDP"}],
            "http://localhost:3001",
            progress_output=progress_path,
            progress_meta=meta_path,
            concurrency=2,
        )

    record = json.loads(progress_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["session_id"] == "direct-1"
    assert record["request_failed"] is True
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["done"] is False
    assert payload["concurrency"] == 2
    assert "timed out" in payload["last_error"]


def test_execute_rows_concurrency_can_continue_after_request_failure(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json["query"])
        if json["query"] == "bad":
            raise module.requests.Timeout("timed out")
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)
    progress_path = tmp_path / "results.jsonl.inprogress"
    meta_path = tmp_path / "results.jsonl.progress.json"

    results = module.execute_rows(
        [
            {"id": "direct-bad", "query": "bad"},
            {"id": "direct-good", "query": "good"},
        ],
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=meta_path,
        concurrency=2,
        continue_on_error=True,
    )

    assert sorted(calls) == ["bad", "good"]
    by_session = {record["session_id"]: record for record in results}
    assert by_session["direct-bad"]["request_failed"] is True
    assert by_session["direct-good"]["series_count"] == 1
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["done"] is True
    assert payload["completed_sessions"] == 2


def test_load_resume_records_prefers_nonempty_inprogress(tmp_path: Path):
    module = load_module()
    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    output_path.write_text(json.dumps({"session_id": "final", "round_index": 1}) + "\n", encoding="utf-8")
    progress_path.write_text(json.dumps({"session_id": "progress", "round_index": 1}) + "\n", encoding="utf-8")

    records = module.load_resume_records(output_path, progress_path)

    assert [record["session_id"] for record in records] == ["progress"]


def test_keep_complete_session_records_drops_duplicate_rounds():
    module = load_module()
    rows = [{"id": "direct-1", "query": "US GDP"}]
    records = [
        {"session_id": "direct-1", "round_index": 1, "value": "first"},
        {"session_id": "direct-1", "round_index": 1, "value": "dupe"},
    ]

    assert module.completed_session_ids_from_results(rows, records) == set()
    assert module.keep_complete_session_records(rows, records) == []


def test_execute_rows_writes_failure_checkpoint_before_reraising(tmp_path: Path, monkeypatch):
    module = load_module()

    def fake_post(url, json, timeout):
        raise module.requests.Timeout("timed out")

    monkeypatch.setattr(module.requests, "post", fake_post)
    progress_path = tmp_path / "results.jsonl.inprogress"
    meta_path = tmp_path / "results.jsonl.progress.json"

    with pytest.raises(module.requests.Timeout):
        module.execute_rows(
            [{"id": "direct-1", "query": "US GDP"}],
            "http://localhost:3001",
            progress_output=progress_path,
            progress_meta=meta_path,
        )

    record = json.loads(progress_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["session_id"] == "direct-1"
    assert record["round_index"] == 1
    assert record["request_failed"] is True
    assert "timed out" in record["error"]

    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["done"] is False
    assert payload["completed_sessions"] == 0
    assert payload["last_session_id"] == "direct-1"
    assert "timed out" in payload["last_error"]


def test_execute_rows_can_continue_after_request_failure(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json["query"])
        if json["query"] == "bad":
            raise module.requests.Timeout("timed out")
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)
    progress_path = tmp_path / "results.jsonl.inprogress"
    meta_path = tmp_path / "results.jsonl.progress.json"

    results = module.execute_rows(
        [
            {"id": "direct-bad", "query": "bad"},
            {"id": "direct-good", "query": "good"},
        ],
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=meta_path,
        continue_on_error=True,
    )

    assert calls == ["bad", "good"]
    assert [record["session_id"] for record in results] == ["direct-bad", "direct-good"]
    assert results[0]["request_failed"] is True
    assert results[1]["series_count"] == 1
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["done"] is True
    assert payload["completed_sessions"] == 2


def test_resume_retries_failed_request_records(tmp_path: Path, monkeypatch):
    module = load_module()
    progress_path = tmp_path / "results.jsonl.inprogress"
    progress_path.write_text(
        json.dumps({"session_id": "direct-1", "round_index": 1, "request_failed": True}) + "\n",
        encoding="utf-8",
    )
    rows = [{"id": "direct-1", "query": "US GDP"}]
    existing = module.load_existing_results(progress_path)
    completed_ids = module.completed_session_ids_from_results(rows, existing)
    complete_existing = module.keep_complete_session_records(rows, existing)
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=tmp_path / "results.jsonl.progress.json",
        skip_completed_session_ids=completed_ids,
        existing_results=complete_existing,
        preserve_progress_output=True,
    )

    assert completed_ids == set()
    assert complete_existing == []
    assert [call["query"] for call in calls] == ["US GDP"]
    assert [record["session_id"] for record in results] == ["direct-1"]


def test_write_progress_summary_is_atomic_json(tmp_path: Path):
    module = load_module()
    meta_path = tmp_path / "results.jsonl.progress.json"

    module.write_progress_summary(
        meta_path,
        completed_sessions=1,
        total_sessions=2,
        results_written=1,
        done=False,
        last_session_id="direct-1",
        last_dataset_type="direct",
        start_index=1,
        skipped_sessions=0,
        completed_session_ids_count=1,
    )

    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["completed_sessions"] == 1
    assert payload["start_index"] == 1
    assert payload["completed_session_ids_count"] == 1
    assert not meta_path.with_name(meta_path.name + ".tmp").exists()



def test_record_failure_marks_request_timeout_runtime_unavailable():
    module = load_module()

    record = module.record_failure(
        {
            "id": "batch-direct-statscan-timeout",
            "query": "StatsCan archived table",
            "provider_stratum": "STATSCAN",
        },
        "direct",
        1,
        "StatsCan archived table",
        120.1,
        module.requests.exceptions.ReadTimeout("read timed out"),
    )

    assert record["request_failed"] is True
    assert record["runtime_unavailable"] is True
    assert record["runtime_unavailable_reason"] == "statscan_request_timeout"
