from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "build_review_expansion_plan.py"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_build_review_expansion_plan_allocates_gap_across_types_and_strata(tmp_path: Path):
    score_path = tmp_path / "score.json"
    gap_path = tmp_path / "gap.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "plan.json"

    write_json(
        policy_path,
        {
            "required_direct_provider_floors": {
                "FRED": {"class": "high_traffic", "floor": 0.98},
                "IMF": {"class": "high_traffic", "floor": 0.98},
                "CoinGecko": {"class": "critical", "floor": 0.97},
            },
            "required_multiround_family_floors": {
                "provider_switch_chain": {"class": "critical", "floor": 0.97},
                "transform_switch_chain": {"class": "critical", "floor": 0.97},
            },
            "required_ambiguity_family_floors": {
                "transform_ambiguity": {"class": "critical", "floor": 0.97},
                "dominant_interpretation_cases": {"class": "high_traffic", "floor": 0.98},
            },
        },
    )
    write_json(
        score_path,
        {
            "snapshot_id": "snap-1",
            "floor_policy_path": str(policy_path),
            "snapshot": {
                "dataset_types": {
                    "direct": 10,
                    "multiround": 6,
                    "ambiguity": 6,
                }
            },
            "metrics": {
                "weighted_session_counts_by_type": {
                    "direct": 10,
                    "multiround": 6,
                    "ambiguity": 6,
                }
            },
            "strata": {
                "evaluated_provider_floors": {
                    "FRED": {"n": 1},
                    "IMF": {"n": 1},
                    "CoinGecko": {"n": 1},
                },
                "evaluated_multiround_family_floors": {
                    "provider_switch_chain": {"n": 1},
                    "transform_switch_chain": {"n": 1},
                },
                "evaluated_ambiguity_family_floors": {
                    "transform_ambiguity": {"n": 1},
                    "dominant_interpretation_cases": {"n": 1},
                },
            },
        },
    )
    write_json(
        gap_path,
        {
            "gap_estimate": {
                "additional_effective_n_needed_at_perfect_success": 36
            },
            "current": {
                "overall_weighted_effective_n": 22,
                "claim_lower95": 0.85,
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_path),
            "--gap-report",
            str(gap_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["current"]["additional_effective_n_needed"] == 36
    assert sum(report["allocation"]["by_dataset_type"].values()) == 36
    assert report["allocation"]["by_dataset_type"]["direct"] > report["allocation"]["by_dataset_type"]["multiround"]
    direct_targets = report["allocation"]["direct"]["targets"]
    assert direct_targets["FRED"]["additional_target_sessions"] >= direct_targets["CoinGecko"]["additional_target_sessions"]
    ambiguity_targets = report["allocation"]["ambiguity"]["targets"]
    assert ambiguity_targets["dominant_interpretation_cases"]["additional_target_sessions"] >= ambiguity_targets["transform_ambiguity"]["additional_target_sessions"]


def test_build_review_expansion_plan_handles_zero_gap(tmp_path: Path):
    score_path = tmp_path / "score.json"
    gap_path = tmp_path / "gap.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "plan.json"

    write_json(policy_path, {"required_direct_provider_floors": {}, "required_multiround_family_floors": {}, "required_ambiguity_family_floors": {}})
    write_json(score_path, {"snapshot_id": "snap-1", "floor_policy_path": str(policy_path), "metrics": {"weighted_session_counts_by_type": {"direct": 1}}})
    write_json(gap_path, {"gap_estimate": {"additional_effective_n_needed_at_perfect_success": 0}, "current": {}})

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_path),
            "--gap-report",
            str(gap_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["current"]["additional_effective_n_needed"] == 0
    assert report["allocation"]["by_dataset_type"]["direct"] == 0
