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
            "--concurrency",
            "1",
            "--request-spacing",
            "2.5",
            "--rate-limit-retries",
            "2",
            "--classify-unsupported-direct",
            "--continue-on-error",
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
    assert payload["request_spacing"] == 2.5
    assert payload["rate_limit_retries"] == 2
    assert payload["classify_unsupported_direct"] is True
    assert payload["continue_on_error"] is True
    run_cmd = payload["run_certification_command"]
    assert "--request-spacing" in run_cmd
    assert "2.5" in run_cmd
    assert "--rate-limit-retries" in run_cmd
    assert "2" in run_cmd
    assert "--classify-unsupported-direct" in run_cmd
    assert "--continue-on-error" in run_cmd
