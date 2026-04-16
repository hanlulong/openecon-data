from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "build_adjudication_triage_report.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_build_adjudication_triage_report_buckets_mixed_failures(tmp_path: Path):
    queue_path = tmp_path / "queue.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    direct_dataset = tmp_path / "direct.jsonl"
    ambiguity_dataset = tmp_path / "ambiguity.jsonl"
    output_path = tmp_path / "triage.json"

    write_jsonl(
        queue_path,
        [
            {
                "session_id": "batch-direct-worldbank-000001",
                "dataset_type": "direct",
                "provider_stratum": "WorldBank",
                "family_stratum": None,
                "queue_reason": "all_failures",
                "automated_label": "fail",
                "failure_class": "error_or_no_data",
                "review_focus_tags": ["provider:WorldBank"],
            },
            {
                "session_id": "batch-direct-fred-000002",
                "dataset_type": "direct",
                "provider_stratum": "FRED",
                "family_stratum": None,
                "queue_reason": "all_failures",
                "automated_label": "fail",
                "failure_class": "unnecessary_clarification_proxy",
                "review_focus_tags": ["provider:FRED"],
            },
            {
                "session_id": "amb-provider-000003",
                "dataset_type": "ambiguity",
                "provider_stratum": "<missing>",
                "family_stratum": "provider_ambiguity",
                "queue_reason": "random_pass_audit",
                "automated_label": "pass",
                "failure_class": "wrong_confident_answer_proxy",
                "review_focus_tags": ["family:provider_ambiguity"],
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "batch-direct-worldbank-000001",
                "round_index": 1,
                "error": "data_not_available",
                "clarification_detected": False,
            },
            {
                "session_id": "batch-direct-fred-000002",
                "round_index": 1,
                "error": None,
                "clarification_detected": True,
            },
            {
                "session_id": "amb-provider-000003",
                "round_index": 1,
                "error": None,
                "clarification_detected": False,
            },
        ],
    )
    write_jsonl(
        direct_dataset,
        [
            {
                "id": "batch-direct-worldbank-000001",
                "provider_stratum": "WorldBank",
                "query": "India tertiary teachers from World Bank",
                "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
            },
            {
                "id": "batch-direct-fred-000002",
                "provider_stratum": "FRED",
                "query": "Illinois hunting and fishing tax collections from FRED",
                "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
            },
        ],
    )
    write_jsonl(
        ambiguity_dataset,
        [
            {
                "id": "amb-provider-000003",
                "query": "Japan GDP growth rate",
                "expected_behavior": "direct_answer",
                "provenance": {"family": "provider_ambiguity"},
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--queue",
            str(queue_path),
            "--raw-results",
            str(raw_path),
            "--dataset",
            str(direct_dataset),
            "--dataset",
            str(ambiguity_dataset),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    items = {item["session_id"]: item for item in report["items"]}

    assert report["summary"]["bucket_counts"] == {
        "likely_dataset_or_query_surface": 1,
        "likely_framework_bug": 1,
        "human_adjudication": 1,
    }
    assert report["summary"]["framework_bug_cluster_counts"] == {
        "unexpected_clarification_on_direct_query": 1
    }
    assert report["summary"]["dataset_or_query_surface_cluster_counts"] == {
        "provider_data_availability_surface": 1
    }
    assert report["summary"]["human_adjudication_cluster_counts"] == {
        "pass_audit_review": 1
    }
    assert items["batch-direct-worldbank-000001"]["cluster"] == "provider_data_availability_surface"
    assert items["batch-direct-worldbank-000001"]["bucket"] == "likely_dataset_or_query_surface"
    assert items["batch-direct-fred-000002"]["cluster"] == "unexpected_clarification_on_direct_query"
    assert items["batch-direct-fred-000002"]["bucket"] == "likely_framework_bug"
    assert items["amb-provider-000003"]["cluster"] == "pass_audit_review"
    assert items["amb-provider-000003"]["bucket"] == "human_adjudication"


def test_build_adjudication_triage_report_flags_statscan_multiround_carryover(tmp_path: Path):
    queue_path = tmp_path / "queue.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    dataset_path = tmp_path / "multiround.jsonl"
    output_path = tmp_path / "triage.json"

    write_jsonl(
        queue_path,
        [
            {
                "session_id": "batch-statscan_decomposition_chain-000001",
                "dataset_type": "multiround",
                "provider_stratum": "<missing>",
                "family_stratum": "statscan_decomposition_chain",
                "queue_reason": "all_failures",
                "automated_label": "fail",
                "failure_class": "needs_review",
                "review_focus_tags": ["family:statscan_decomposition_chain"],
            }
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "batch-statscan_decomposition_chain-000001",
                "round_index": 1,
                "query": "Canada unemployment rate",
                "clarification_detected": True,
                "error": None,
            },
            {
                "session_id": "batch-statscan_decomposition_chain-000001",
                "round_index": 2,
                "query": "Show by province",
                "clarification_detected": False,
                "error": None,
            },
        ],
    )
    write_jsonl(
        dataset_path,
        [
            {
                "id": "batch-statscan_decomposition_chain-000001",
                "family": "statscan_decomposition_chain",
                "rounds": [
                    {"query": "Canada unemployment rate"},
                    {"query": "Show by province"},
                ],
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--queue",
            str(queue_path),
            "--raw-results",
            str(raw_path),
            "--dataset",
            str(dataset_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    item = report["items"][0]
    assert item["bucket"] == "likely_framework_bug"
    assert item["cluster"] == "multiround_state_carryover"
    assert "carryover" in item["rationale"]
    assert report["summary"]["framework_bug_cluster_counts"] == {
        "multiround_state_carryover": 1
    }
