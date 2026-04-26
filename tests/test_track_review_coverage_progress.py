from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "track_review_coverage_progress.py"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_track_review_coverage_progress_reports_remaining_targets(tmp_path: Path):
    score_path = tmp_path / "score.json"
    expansion_path = tmp_path / "expansion.json"
    output_path = tmp_path / "progress.json"

    write_json(
        score_path,
        {
            "snapshot_id": "snap-1",
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
                    "IMF": {"n": 2},
                },
                "evaluated_multiround_family_floors": {
                    "provider_switch_chain": {"n": 1},
                },
                "evaluated_ambiguity_family_floors": {
                    "dominant_interpretation_cases": {"n": 1},
                },
            },
        },
    )
    write_json(
        expansion_path,
        {
            "snapshot_id": "snap-1",
            "current": {
                "effective_n": 22.0,
                "additional_effective_n_needed": 100,
            },
            "allocation": {
                "by_dataset_type": {
                    "direct": 50,
                    "multiround": 25,
                    "ambiguity": 25,
                },
                "direct": {
                    "targets": {
                        "FRED": {"class": "high_traffic", "floor": 0.98, "additional_target_sessions": 10, "recommended_total_n": 11},
                        "IMF": {"class": "high_traffic", "floor": 0.98, "additional_target_sessions": 8, "recommended_total_n": 10},
                    }
                },
                "multiround": {
                    "targets": {
                        "provider_switch_chain": {"class": "critical", "floor": 0.97, "additional_target_sessions": 5, "recommended_total_n": 6},
                    }
                },
                "ambiguity": {
                    "targets": {
                        "dominant_interpretation_cases": {"class": "high_traffic", "floor": 0.98, "additional_target_sessions": 7, "recommended_total_n": 8},
                    }
                },
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_path),
            "--expansion-plan",
            str(expansion_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["effective_n_progress"]["current_effective_n"] == 22.0
    assert report["effective_n_progress"]["target_effective_n"] == 122.0
    assert report["dataset_type_progress"]["direct"]["target_n"] == 60
    assert report["strata_progress"]["direct"]["FRED"]["remaining_n"] == 10
    assert report["strata_progress"]["direct"]["IMF"]["remaining_n"] == 8
    assert report["priority_queue"]["direct"][0]["name"] == "FRED"


def test_track_review_coverage_progress_handles_zero_targets(tmp_path: Path):
    score_path = tmp_path / "score.json"
    expansion_path = tmp_path / "expansion.json"
    output_path = tmp_path / "progress.json"

    write_json(score_path, {"snapshot_id": "snap-1", "metrics": {"weighted_session_counts_by_type": {"direct": 1}}, "strata": {}})
    write_json(expansion_path, {"snapshot_id": "snap-1", "current": {"effective_n": 1.0, "additional_effective_n_needed": 0}, "allocation": {"by_dataset_type": {"direct": 0}, "direct": {"targets": {}}, "multiround": {"targets": {}}, "ambiguity": {"targets": {}}}})

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_path),
            "--expansion-plan",
            str(expansion_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["effective_n_progress"]["remaining_effective_n"] == 0
    assert report["dataset_type_progress"]["direct"]["remaining_n"] == 0


def test_track_review_coverage_progress_accepts_batch_plan_list_targets(tmp_path: Path):
    score_path = tmp_path / "score.json"
    expansion_path = tmp_path / "expansion.json"
    output_path = tmp_path / "progress.json"

    write_json(
        score_path,
        {
            "snapshot_id": "snap-1",
            "session_results": [
                {
                    "session_id": "direct-fred-1",
                    "dataset_type": "direct",
                    "provider_stratum": "FRED",
                    "family_stratum": None,
                    "provenance": {"selection_weight": 1.0},
                }
            ],
        },
    )
    write_json(
        expansion_path,
        {
            "snapshot_id": "snap-1",
            "effective_n_progress": {
                "current_effective_n": 1.0,
                "target_effective_n": 10.0,
                "remaining_effective_n": 9.0,
                "progress_ratio": 0.1,
            },
            "dataset_type_batch_allocation": {"direct": 10},
            "allocation": {
                "direct": {
                    "targets": [
                        {
                            "name": "FRED",
                            "class": "high_traffic",
                            "floor": 0.98,
                            "current_n": 1,
                            "target_n": 11,
                            "remaining_n": 10,
                            "planned_batch_sessions": 10,
                        }
                    ]
                },
                "multiround": {"targets": []},
                "ambiguity": {"targets": []},
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_path),
            "--expansion-plan",
            str(expansion_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["effective_n_progress"]["target_effective_n"] == 10.0
    assert report["dataset_type_progress"]["direct"]["current_n"] == 1
    assert report["dataset_type_progress"]["direct"]["target_n"] == 11
    assert report["strata_progress"]["direct"]["FRED"]["current_n"] == 1
    assert report["strata_progress"]["direct"]["FRED"]["target_n"] == 11
    assert report["strata_progress"]["direct"]["FRED"]["additional_target_sessions"] == 10


def test_track_review_coverage_progress_aggregates_unique_sessions_across_multiple_scores(tmp_path: Path):
    score_a = tmp_path / "score-a.json"
    score_b = tmp_path / "score-b.json"
    expansion_path = tmp_path / "expansion.json"
    output_path = tmp_path / "progress.json"

    write_json(
        score_a,
        {
            "snapshot_id": "snap-1",
            "session_results": [
                {
                    "session_id": "direct-fred-1",
                    "dataset_type": "direct",
                    "provider_stratum": "FRED",
                    "family_stratum": None,
                    "provenance": {"selection_weight": 1.0},
                },
                {
                    "session_id": "amb-dom-1",
                    "dataset_type": "ambiguity",
                    "provider_stratum": "<missing>",
                    "family_stratum": "dominant_interpretation_cases",
                    "provenance": {"selection_weight": 1.0},
                },
            ],
        },
    )
    write_json(
        score_b,
        {
            "snapshot_id": "snap-1",
            "session_results": [
                {
                    "session_id": "direct-fred-1",
                    "dataset_type": "direct",
                    "provider_stratum": "FRED",
                    "family_stratum": None,
                    "provenance": {"selection_weight": 1.0},
                },
                {
                    "session_id": "direct-imf-1",
                    "dataset_type": "direct",
                    "provider_stratum": "IMF",
                    "family_stratum": None,
                    "provenance": {"selection_weight": 1.0},
                },
                {
                    "session_id": "multi-provider-switch-1",
                    "dataset_type": "multiround",
                    "provider_stratum": "<missing>",
                    "family_stratum": "provider_switch_chain",
                    "provenance": {"selection_weight": 1.0},
                },
            ],
        },
    )
    write_json(
        expansion_path,
        {
            "snapshot_id": "snap-1",
            "current": {
                "effective_n": 1.0,
                "additional_effective_n_needed": 9,
            },
            "allocation": {
                "by_dataset_type": {
                    "direct": 10,
                    "multiround": 5,
                    "ambiguity": 5,
                },
                "direct": {
                    "targets": {
                        "FRED": {"class": "high_traffic", "floor": 0.98, "additional_target_sessions": 10, "recommended_total_n": 11},
                        "IMF": {"class": "high_traffic", "floor": 0.98, "additional_target_sessions": 8, "recommended_total_n": 9},
                    }
                },
                "multiround": {
                    "targets": {
                        "provider_switch_chain": {"class": "critical", "floor": 0.97, "additional_target_sessions": 5, "recommended_total_n": 6},
                    }
                },
                "ambiguity": {
                    "targets": {
                        "dominant_interpretation_cases": {"class": "high_traffic", "floor": 0.98, "additional_target_sessions": 7, "recommended_total_n": 8},
                    }
                },
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--score-report",
            str(score_a),
            "--score-report",
            str(score_b),
            "--expansion-plan",
            str(expansion_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["effective_n_progress"]["current_effective_n"] == 4.0
    assert report["effective_n_progress"]["remaining_effective_n"] == 6.0
    assert report["dataset_type_progress"]["direct"]["current_n"] == 2
    assert report["dataset_type_progress"]["multiround"]["current_n"] == 1
    assert report["dataset_type_progress"]["ambiguity"]["current_n"] == 1
    assert report["strata_progress"]["direct"]["FRED"]["current_n"] == 1
    assert report["strata_progress"]["direct"]["IMF"]["current_n"] == 1
    assert report["strata_progress"]["multiround"]["provider_switch_chain"]["current_n"] == 1
    assert report["strata_progress"]["ambiguity"]["dominant_interpretation_cases"]["current_n"] == 1
