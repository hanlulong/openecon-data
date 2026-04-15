from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "estimate_certification_gap.py"


def test_estimate_certification_gap_quantifies_required_effective_n(tmp_path: Path):
    score_path = tmp_path / "score.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "gap.json"

    policy_path.write_text(
        json.dumps({"claim_thresholds": {"lower95_min": 0.99}}) + "\n",
        encoding="utf-8",
    )
    score_path.write_text(
        json.dumps(
            {
                "snapshot_id": "snap-1",
                "floor_policy_path": str(policy_path),
                "metrics": {
                    "claim_observed_success": 1.0,
                    "claim_lower95": 0.8513404742740388,
                    "overall_weighted_effective_n": 22.0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["required_lower95"] == 0.99
    assert report["current"]["overall_weighted_effective_n"] == 22.0
    assert report["gap_estimate"]["required_effective_n_at_perfect_success"] is not None
    assert report["gap_estimate"]["required_effective_n_at_perfect_success"] >= 300
    assert report["gap_estimate"]["additional_effective_n_needed_at_perfect_success"] > 0
    perfect_scenario = next(item for item in report["scenarios"] if item["observed_success"] == 1.0)
    assert perfect_scenario["required_effective_n"] == report["gap_estimate"]["required_effective_n_at_perfect_success"]


def test_estimate_certification_gap_respects_override_threshold(tmp_path: Path):
    score_path = tmp_path / "score.json"
    output_path = tmp_path / "gap.json"

    score_path.write_text(
        json.dumps(
            {
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.95,
                    "overall_weighted_effective_n": 100.0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_path),
            "--required-lower95",
            "0.97",
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["required_lower95"] == 0.97
    assert report["gap_estimate"]["required_effective_n_at_current_success"] is not None
