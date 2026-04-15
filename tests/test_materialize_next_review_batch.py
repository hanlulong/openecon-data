from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validation.materialize_next_review_batch import select_quality_screened_direct_records


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "materialize_next_review_batch.py"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_materialize_next_review_batch_writes_expected_counts(tmp_path: Path):
    batch_plan = tmp_path / "batch.json"
    snapshot = tmp_path / "snapshot.json"
    output_dir = tmp_path / "out"

    write_json(
        batch_plan,
        {
            "allocation": {
                "direct": {
                    "targets": [
                        {"name": "FRED", "planned_batch_sessions": 2, "target_n": 10},
                        {"name": "IMF", "planned_batch_sessions": 1, "target_n": 8},
                    ]
                },
                "multiround": {
                    "targets": [
                        {"name": "transform_switch_chain", "planned_batch_sessions": 2, "target_n": 10},
                    ]
                },
                "ambiguity": {
                    "targets": [
                        {"name": "dominant_interpretation_cases", "planned_batch_sessions": 3, "target_n": 12},
                    ]
                },
            }
        },
    )
    write_json(
        snapshot,
        {
            "snapshot_date": "2026-04-14",
            "git_sha": "abc12345",
            "indicator_count": 330050,
            "provider_counts": {
                "FRED": 138774,
                "IMF": 115381,
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--batch-plan",
            str(batch_plan),
            "--snapshot",
            str(snapshot),
            "--output-dir",
            str(output_dir),
            "--dataset-tier",
            "dev",
            "--holdout-split",
            "batch_review",
        ],
        check=True,
    )

    direct_rows = read_jsonl(output_dir / "next_batch_direct.jsonl")
    multiround_rows = read_jsonl(output_dir / "next_batch_multiround.jsonl")
    ambiguity_rows = read_jsonl(output_dir / "next_batch_ambiguity.jsonl")

    assert len(direct_rows) == 3
    assert len(multiround_rows) == 2
    assert len(ambiguity_rows) == 3
    assert direct_rows[0]["provenance"]["holdout_split"] == "batch_review"
    assert direct_rows[0]["provenance"]["batch_plan"] == "next_review_batch"
    assert multiround_rows[0]["family"] == "transform_switch_chain"
    assert ambiguity_rows[0]["provenance"]["family"] == "dominant_interpretation_cases"


def test_materialize_next_review_batch_handles_empty_targets(tmp_path: Path):
    batch_plan = tmp_path / "batch.json"
    snapshot = tmp_path / "snapshot.json"
    output_dir = tmp_path / "out"

    write_json(batch_plan, {"allocation": {"direct": {"targets": []}, "multiround": {"targets": []}, "ambiguity": {"targets": []}}})
    write_json(snapshot, {"snapshot_date": "2026-04-14", "git_sha": "abc12345", "indicator_count": 330050, "provider_counts": {}})

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--batch-plan",
            str(batch_plan),
            "--snapshot",
            str(snapshot),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
    )

    assert read_jsonl(output_dir / "next_batch_direct.jsonl") == []
    assert read_jsonl(output_dir / "next_batch_multiround.jsonl") == []
    assert read_jsonl(output_dir / "next_batch_ambiguity.jsonl") == []


def test_select_quality_screened_direct_records_prefers_low_risk():
    records = [
        {"id": "high-1", "query": "very long", "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["catalog_jargon"]}},
        {"id": "medium-1", "query": "medium", "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]}},
        {"id": "low-1", "query": "short", "provenance": {"query_quality_risk": "low", "query_quality_reasons": []}},
        {"id": "low-2", "query": "shorter", "provenance": {"query_quality_risk": "low", "query_quality_reasons": []}},
    ]

    selected = select_quality_screened_direct_records(records, 2)

    assert [row["id"] for row in selected] == ["low-1", "low-2"]


def test_select_quality_screened_direct_records_prefers_more_specific_low_risk_rows():
    records = [
        {
            "id": "generic-1",
            "query": "France Immigration from Eurostat",
            "origin": {"name": "Immigration", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
        {
            "id": "specific-1",
            "query": "Italy Practising dentists from Eurostat",
            "origin": {"name": "Practising dentists", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["specific-1"]


def test_select_quality_screened_direct_records_avoids_high_risk_oecd_methodology_titles():
    records = [
        {
            "id": "oecd-dense",
            "query": "US $ current prices current PPPs Annual net national income per capita from OECD",
            "origin": {
                "name": "Annual net national income per capita, US $, current prices, current PPPs",
                "description": "",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["methodology_dense"]},
        },
        {
            "id": "oecd-clear",
            "query": "Germany Water use from OECD",
            "origin": {"name": "Water use", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["oecd-clear"]
