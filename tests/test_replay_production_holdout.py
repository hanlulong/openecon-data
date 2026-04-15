from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPLAY_SCRIPT = ROOT / "scripts" / "validation" / "replay_production_holdout.py"


def test_replay_production_holdout_dry_run_includes_floor_policy_and_adjudication(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    floor_policy_path = tmp_path / "policy.json"
    adjudication_path = tmp_path / "adjudication.jsonl"
    raw_output = tmp_path / "raw.jsonl"
    score_output = tmp_path / "score.json"

    dataset_path.write_text(
        json.dumps({"id": "direct-fred-000001"}) + "\n",
        encoding="utf-8",
    )
    floor_policy_path.write_text(json.dumps({"version": 1}) + "\n", encoding="utf-8")
    adjudication_path.write_text(json.dumps({"session_id": "direct-fred-000001", "final_label": "pass"}) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(REPLAY_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--floor-policy",
            str(floor_policy_path),
            "--adjudication-records",
            str(adjudication_path),
            "--raw-output",
            str(raw_output),
            "--score-output",
            str(score_output),
            "--max-sessions",
            "3",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["mode"] == "dry_run"
    assert payload["floor_policy"] == str(floor_policy_path.resolve())
    assert payload["adjudication_records"] == str(adjudication_path.resolve())
