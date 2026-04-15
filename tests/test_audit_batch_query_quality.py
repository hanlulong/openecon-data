from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "audit_batch_query_quality.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_audit_batch_query_quality_flags_catalog_like_direct_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany Learning Deprivation Gap;PISA 2018 for grade 15Y using MPL Level 2 for reading, Fourth Quintile",
                "origin": {
                    "name": "Learning Deprivation Gap;PISA 2018 for grade 15Y using MPL Level 2 for reading, Fourth Quintile"
                },
            },
            {
                "id": "direct-2",
                "query": "US GDP",
                "origin": {
                    "name": "Gross Domestic Product"
                },
            },
            {
                "id": "amb-1",
                "query": "interest rate",
                "expected_behavior": "clarify",
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["row_count"] == 3
    assert report["summary"]["high_risk_rows"] == 1
    assert report["summary"]["reason_counts"]["catalog_jargon"] >= 1
    flagged = report["flagged_rows"][0]
    assert flagged["id"] == "direct-1"
    assert flagged["risk_level"] == "high"
    assert "provider_title_like" in flagged["reasons"] or "catalog_jargon" in flagged["reasons"]


def test_audit_batch_query_quality_handles_multiple_datasets(tmp_path: Path):
    dataset_a = tmp_path / "a.jsonl"
    dataset_b = tmp_path / "b.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(dataset_a, [{"id": "direct-1", "query": "US GDP", "origin": {"name": "Gross Domestic Product"}}])
    write_jsonl(dataset_b, [{"id": "direct-2", "query": "Canada MICS: Something, Urban", "origin": {"name": "MICS: Something, Urban"}}])

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset_a),
            "--dataset",
            str(dataset_b),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["row_count"] == 2
    assert report["summary"]["by_type"]["direct"] == 2
    assert report["summary"]["high_risk_rows"] == 1
