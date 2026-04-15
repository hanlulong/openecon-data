from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "build_next_review_batch.py"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_build_next_review_batch_allocates_batch_across_types_and_priority_queues(tmp_path: Path):
    progress_path = tmp_path / "progress.json"
    output_path = tmp_path / "batch.json"

    write_json(
        progress_path,
        {
            "snapshot_id": "snap-1",
            "dataset_type_progress": {
                "direct": {"remaining_n": 60},
                "multiround": {"remaining_n": 25},
                "ambiguity": {"remaining_n": 15},
            },
            "priority_queue": {
                "direct": [
                    {"name": "FRED", "class": "high_traffic", "floor": 0.98, "current_n": 1, "target_n": 26, "remaining_n": 25},
                    {"name": "IMF", "class": "high_traffic", "floor": 0.98, "current_n": 1, "target_n": 26, "remaining_n": 25},
                    {"name": "WorldBank", "class": "high_traffic", "floor": 0.98, "current_n": 1, "target_n": 26, "remaining_n": 10},
                ],
                "multiround": [
                    {"name": "transform_switch_chain", "class": "critical", "floor": 0.97, "current_n": 1, "target_n": 18, "remaining_n": 17},
                    {"name": "statscan_decomposition_chain", "class": "critical", "floor": 0.97, "current_n": 1, "target_n": 18, "remaining_n": 8},
                ],
                "ambiguity": [
                    {"name": "dominant_interpretation_cases", "class": "high_traffic", "floor": 0.98, "current_n": 1, "target_n": 29, "remaining_n": 14},
                    {"name": "terminology_ambiguity", "class": "critical", "floor": 0.97, "current_n": 1, "target_n": 15, "remaining_n": 1},
                ],
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--progress-report",
            str(progress_path),
            "--batch-size",
            "20",
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["batch_size"] == 20
    assert sum(report["dataset_type_batch_allocation"].values()) == 20
    assert report["dataset_type_batch_allocation"]["direct"] > report["dataset_type_batch_allocation"]["ambiguity"]
    direct_targets = report["allocation"]["direct"]["targets"]
    assert sum(item["planned_batch_sessions"] for item in direct_targets) == report["allocation"]["direct"]["planned_batch_sessions"]
    assert direct_targets[0]["name"] in {"FRED", "IMF"}
    ambiguity_targets = report["allocation"]["ambiguity"]["targets"]
    assert ambiguity_targets[0]["name"] == "dominant_interpretation_cases"


def test_build_next_review_batch_handles_zero_remaining(tmp_path: Path):
    progress_path = tmp_path / "progress.json"
    output_path = tmp_path / "batch.json"

    write_json(
        progress_path,
        {
            "snapshot_id": "snap-1",
            "dataset_type_progress": {
                "direct": {"remaining_n": 0},
            },
            "priority_queue": {
                "direct": [],
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--progress-report",
            str(progress_path),
            "--batch-size",
            "10",
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["dataset_type_batch_allocation"]["direct"] == 0
    assert report["allocation"]["direct"]["targets"] == []
