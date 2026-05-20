from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validation.build_review_expansion_plan import (
    direct_capacity_addition_caps,
    greedy_design_additions,
    projection_meets_targets,
    upper_bound_projection_metrics,
)


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


def test_build_review_expansion_plan_uses_design_lower95_when_available(tmp_path: Path):
    score_path = tmp_path / "score.json"
    gap_path = tmp_path / "gap.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "plan.json"

    write_json(
        policy_path,
        {
            "claim_thresholds": {
                "weighted_session_success_min": 0.992,
                "lower95_min": 0.99,
            },
            "required_direct_provider_floors": {
                "FRED": {"class": "high_traffic", "floor": 0.98},
                "CoinGecko": {"class": "critical", "floor": 0.97},
            },
            "required_multiround_family_floors": {},
            "required_ambiguity_family_floors": {},
        },
    )
    write_json(
        score_path,
        {
            "snapshot_id": "snap-1",
            "floor_policy_path": str(policy_path),
            "metrics": {
                "weighted_session_counts_by_type": {"direct": 12},
                "overall_weighted_adjudicated_design_confidence": {
                    "observed_success": 1.0,
                    "lower95": 0.65,
                    "strata": {
                        "direct_provider:FRED": {
                            "n": 8,
                            "population_weight_share": 0.9,
                            "weighted_success": 1.0,
                            "effective_n": 8.0,
                            "rounded_effective_n": 8,
                            "effective_successes": 8,
                            "lower95": 0.6755843804891231,
                        },
                        "direct_provider:CoinGecko": {
                            "n": 4,
                            "population_weight_share": 0.1,
                            "weighted_success": 1.0,
                            "effective_n": 4.0,
                            "rounded_effective_n": 4,
                            "effective_successes": 4,
                            "lower95": 0.5100999795960008,
                        },
                    },
                },
            },
            "strata": {
                "evaluated_provider_floors": {
                    "FRED": {"n": 8},
                    "CoinGecko": {"n": 4},
                }
            },
        },
    )
    write_json(
        gap_path,
        {
            "required_lower95": 0.99,
            "gap_estimate": {
                "additional_effective_n_needed_at_perfect_success": 1
            },
            "current": {
                "overall_weighted_effective_n": 12,
                "claim_lower95": 0.65,
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
    design_plan = report["design_lower95_expansion"]
    assert design_plan["enabled"] is True
    assert design_plan["projected_lower95_after_plan"] >= 0.99
    assert report["current"]["additional_effective_n_needed"] > 1
    direct_targets = report["allocation"]["direct"]["targets"]
    assert direct_targets["FRED"]["additional_target_sessions"] > direct_targets["CoinGecko"]["additional_target_sessions"]
    assert report["allocation"]["by_dataset_type"]["direct"] == report["current"]["additional_effective_n_needed"]


def test_direct_capacity_caps_use_answerability_selectable_remaining_rows() -> None:
    caps, metadata = direct_capacity_addition_caps(
        capacities={
            "IMF": {
                "catalog_rows": 10,
                "answerability_selectable_rows": 3,
                "supportability_excluded_rows": 7,
                "supportability_exclusion_reasons": {
                    "imf_non_weo_public_surface_unsupported": 7
                },
            }
        },
        direct_policy={"IMF": {"class": "high_traffic", "floor": 0.98}},
        current_stats={"IMF": {"n": 2}},
        design_strata={},
    )

    assert caps == {"direct_provider:IMF": 1}
    assert metadata["IMF"]["max_additional_answerability_sessions"] == 1
    assert metadata["IMF"]["supportability_excluded_rows"] == 7


def test_greedy_design_additions_respects_capacity_caps() -> None:
    states = {
        "direct_provider:IMF": {
            "population_weight_share": 0.5,
            "effective_n": 1,
            "effective_successes": 1,
        },
        "direct_provider:FRED": {
            "population_weight_share": 0.5,
            "effective_n": 1,
            "effective_successes": 1,
        },
    }

    additions, projected_lower, _projected_observed, feasible, reason = greedy_design_additions(
        states,
        target_lower95=0.99,
        target_observed_success=0.992,
        max_additions_by_stratum={"direct_provider:IMF": 0},
        max_iterations=10,
    )

    assert additions["direct_provider:IMF"] == 0
    assert additions["direct_provider:FRED"] == 10
    assert projected_lower < 0.99
    assert feasible is False
    assert reason == "max_iterations_10_exhausted"


def test_upper_bound_projection_reports_capped_infeasibility() -> None:
    states = {
        "direct_provider:FRED": {
            "population_weight_share": 0.99,
            "raw_weight_total": 1000.0,
            "raw_weight_square_total": 1000.0 * 1000.0,
            "raw_pass_weight_total": 1000.0,
            "raw_wrong_confident_weight_total": 0.0,
            "future_weight_model": "direct_catalog_population",
            "future_population_count": 100.0,
        },
        "ambiguity_family:provider_ambiguity": {
            "population_weight_share": 0.01,
            "raw_weight_total": 1.0,
            "raw_weight_square_total": 1.0,
            "raw_pass_weight_total": 1.0,
            "raw_wrong_confident_weight_total": 0.0,
            "future_weight_model": "template_target_total",
            "future_current_n": 1,
        },
    }

    upper_bound = upper_bound_projection_metrics(
        states,
        max_additions_by_stratum={"direct_provider:FRED": 100},
    )

    assert upper_bound is not None
    assert upper_bound["lower95"] < 0.99
    assert upper_bound["uncapped_strata"] == ["ambiguity_family:provider_ambiguity"]
    assert not projection_meets_targets(
        lower95=upper_bound["lower95"],
        observed_success=upper_bound["observed_success"],
        wrong_confident_answer_rate=upper_bound["wrong_confident_answer_rate"],
        target_lower95=0.99,
        target_observed_success=0.992,
        target_wrong_confident_rate=0.005,
    )
