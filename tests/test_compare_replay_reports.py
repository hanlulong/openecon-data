from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPARE_SCRIPT = ROOT / "scripts" / "validation" / "compare_replay_reports.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_compare_replay_reports_detects_clarification_and_provider_drift(tmp_path: Path):
    local_raw = tmp_path / "local.jsonl"
    production_raw = tmp_path / "production.jsonl"
    local_score = tmp_path / "local-score.json"
    production_score = tmp_path / "production-score.json"
    output = tmp_path / "parity.json"

    write_jsonl(
        local_raw,
        [
            {
                "session_id": "amb-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 0,
                "clarification_detected": True,
                "providers": [],
                "countries": [],
                "series_ids": [],
                "error": None,
            },
            {
                "session_id": "direct-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["FRED"],
                "countries": ["US"],
                "series_ids": ["GDP"],
                "error": None,
            },
        ],
    )
    write_jsonl(
        production_raw,
        [
            {
                "session_id": "amb-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["FRED"],
                "countries": ["US"],
                "series_ids": ["REAINTRATREARAT10Y"],
                "error": None,
            },
            {
                "session_id": "direct-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["FRED"],
                "countries": ["US"],
                "series_ids": ["GDP"],
                "error": None,
            },
        ],
    )
    local_score.write_text(json.dumps({"claim_grade_ready": True, "metrics": {"claim_lower95": 0.99}}) + "\n", encoding="utf-8")
    production_score.write_text(json.dumps({"claim_grade_ready": False, "metrics": {"claim_lower95": 0.85}}) + "\n", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(COMPARE_SCRIPT),
            "--local-raw",
            str(local_raw),
            "--production-raw",
            str(production_raw),
            "--local-score",
            str(local_score),
            "--production-score",
            str(production_score),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["session_count"] == 2
    assert report["summary"]["sessions_with_differences"] == 1
    assert report["summary"]["difference_counts"]["clarification_mismatch"] == 1
    assert report["summary"]["difference_counts"]["provider_mismatch"] == 1
    assert report["summary"]["severity_counts"]["material"] == 1
    assert report["summary"]["severity_counts"]["match"] == 1
    assert report["summary"]["local_score"]["claim_grade_ready"] is True
    assert report["summary"]["production_score"]["claim_grade_ready"] is False
    differing = next(item for item in report["session_reports"] if item["session_id"] == "amb-1")
    assert differing["severity"] == "material"
    assert "clarification_mismatch" in differing["difference_types"]
    assert "provider_mismatch" in differing["difference_types"]


def test_compare_replay_reports_detects_missing_rounds(tmp_path: Path):
    local_raw = tmp_path / "local.jsonl"
    production_raw = tmp_path / "production.jsonl"
    output = tmp_path / "parity.json"

    write_jsonl(
        local_raw,
        [
            {
                "session_id": "multi-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["IMF"],
                "countries": ["DE"],
                "series_ids": ["NGDPD"],
                "error": None,
            },
            {
                "session_id": "multi-1",
                "round_index": 2,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["IMF"],
                "countries": ["DE"],
                "series_ids": ["NGDP_RPCH"],
                "error": None,
            },
        ],
    )
    write_jsonl(
        production_raw,
        [
            {
                "session_id": "multi-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["IMF"],
                "countries": ["DE"],
                "series_ids": ["NGDPD"],
                "error": None,
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(COMPARE_SCRIPT),
            "--local-raw",
            str(local_raw),
            "--production-raw",
            str(production_raw),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["sessions_with_differences"] == 1
    assert report["summary"]["difference_counts"]["round_count_mismatch"] == 1
    assert report["summary"]["difference_counts"]["missing_production_round"] == 1
    differing = report["session_reports"][0]
    assert differing["severity"] == "material"
    assert "round_count_mismatch" in differing["difference_types"]
    assert any(round_report["status"] == "missing_production" for round_report in differing["round_reports"])


def test_compare_replay_reports_ignores_equivalent_country_labels(tmp_path: Path):
    local_raw = tmp_path / "local.jsonl"
    production_raw = tmp_path / "production.jsonl"
    output = tmp_path / "parity.json"

    write_jsonl(
        local_raw,
        [
            {
                "session_id": "direct-comtrade-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["UN Comtrade"],
                "countries": ["Japan"],
                "series_ids": [],
                "error": None,
            },
        ],
    )
    write_jsonl(
        production_raw,
        [
            {
                "session_id": "direct-comtrade-1",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "clarification_detected": False,
                "providers": ["UN Comtrade"],
                "countries": ["JP"],
                "series_ids": [],
                "error": None,
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(COMPARE_SCRIPT),
            "--local-raw",
            str(local_raw),
            "--production-raw",
            str(production_raw),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["sessions_with_differences"] == 0
    assert report["summary"]["severity_counts"]["match"] == 1
    assert "country_mismatch" not in report["summary"]["difference_counts"]
