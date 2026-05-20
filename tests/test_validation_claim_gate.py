from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCORE_SCRIPT = ROOT / "scripts" / "validation" / "score_certification.py"
CLAIM_SCRIPT = ROOT / "scripts" / "validation" / "certify_claim.py"
MULTI_SAMPLER = ROOT / "scripts" / "validation" / "sample_multiround_cert_set.py"
AMBIG_SAMPLER = ROOT / "scripts" / "validation" / "sample_ambiguity_cert_set.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_score_certification_emits_split_and_provider_strata(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 2.0,
                },
            },
            {
                "id": "direct-imf-000002",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Japan GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-000001", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-imf-000002", "round_index": 1, "status_code": 500, "series_count": 0, "error": "boom"},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["snapshot_id"] == "snap-1"
    assert report["raw_results_path"] == str(raw_path.resolve())
    assert len(report["raw_results_sha256"]) == 64
    assert report["input_datasets"][0]["path"] == str(dataset_path.resolve())
    assert len(report["input_datasets"][0]["sha256"]) == 64
    assert len(report["floor_policy_sha256"]) == 64
    assert report["claim_grade_ready"] is False
    assert any("semantic metrics are still proxy-backed" in item for item in report["claim_grade_blockers"])
    assert report["snapshot"]["holdout_splits"] == {"cert_holdout": 2}
    assert report["metrics"]["provisional_structural_session_success"]["by_split"]["cert_holdout"] == 0.5
    assert report["strata"]["direct_provider_success"]["FRED"]["pass_rate"] == 1.0
    assert report["strata"]["direct_provider_success"]["IMF"]["pass_rate"] == 0.0
    assert report["strata"]["failing_strata"] == ["direct_provider:IMF 0.000 below high_traffic floor 0.980"]
    assert "direct_provider:WorldBank (high_traffic)" in report["strata"]["missing_required_strata"]


def test_score_certification_exposes_supportability_blockers(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-imf-unsupported",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Germany Merchandise Trade Value of Exports Chapter 60 from IMF",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            }
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-imf-unsupported",
                "round_index": 1,
                "status_code": None,
                "series_count": 0,
                "error": "supportability_blocked: imf_non_weo_public_surface_unsupported",
                "supportability_blocked": True,
                "supportability_reason": "imf_non_weo_public_surface_unsupported",
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metrics"]["supportability_blocked_sessions"] == 1
    assert report["metrics"]["supportability_blocked_by_provider"] == {"IMF": 1}
    assert report["metrics"]["supportability_blocked_reason_counts"] == {
        "imf_non_weo_public_surface_unsupported": 1
    }
    assert report["session_results"][0]["supportability_blocked"] is True
    assert report["session_results"][0]["provisional_structural_pass"] is False
    assert any("supportability-blocked certification sessions" in item for item in report["claim_grade_blockers"])


def test_score_certification_reports_user_answerability_target(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-user",
                "dataset_tier": "cert_holdout",
                "evaluation_target": "user_answerability",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                    "certification_target": "user_answerability",
                },
            }
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-user", "round_index": 1, "status_code": 200, "series_count": 1, "error": None}
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["certification_target"] == "user_answerability"
    assert report["snapshot"]["evaluation_targets"] == {"user_answerability": 1}
    assert report["session_results"][0]["evaluation_target"] == "user_answerability"
    assert report["metrics"]["provisional_structural_session_success"]["by_evaluation_target"] == {
        "user_answerability": 1.0
    }
    assert not any("certification target mismatch" in item for item in report["claim_grade_blockers"])


def test_score_certification_accepts_expected_fail_closed_provider_country_answer(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-non-us-contract",
                "dataset_tier": "contract_guard",
                "evaluation_target": "user_answerability",
                "provider_stratum": "FRED",
                "query": "Canada GDP from FRED",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "contract_guard",
                    "selection_weight": 1.0,
                    "certification_target": "user_answerability",
                },
                "gold": {
                    "clarification_expected": False,
                    "expected_outcome": "fail_closed_provider_country_unavailable",
                    "must_not_return_countries": ["US", "United States"],
                },
            }
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-fred-non-us-contract",
                "round_index": 1,
                "status_code": 200,
                "series_count": 0,
                "error": "data_not_available",
                "message": (
                    "📭 **Provider/Country Not Available**\n"
                    "FRED only covers United States country scope."
                ),
                "providers": [],
                "countries": [],
                "series_ids": [],
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    session = report["session_results"][0]
    assert session["expected_fail_closed_pass"] is True
    assert session["provisional_structural_pass"] is True
    assert report["strata"]["direct_provider_success"]["FRED"]["pass_rate"] == 1.0
    assert report["metrics"]["provisional_structural_session_success"]["by_evaluation_target"] == {
        "user_answerability": 1.0
    }


def test_score_certification_supportability_block_overrides_stale_adjudicated_pass(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-imf-unsupported",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Germany Merchandise Trade Value of Exports Chapter 60 from IMF",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            }
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-imf-unsupported",
                "round_index": 1,
                "status_code": None,
                "series_count": 0,
                "error": "supportability_blocked: imf_non_weo_public_surface_unsupported",
                "supportability_blocked": True,
                "supportability_reason": "imf_non_weo_public_surface_unsupported",
            }
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {
                "session_id": "direct-imf-unsupported",
                "final_label": "pass",
                "failure_class": None,
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    session = report["session_results"][0]
    assert session["final_label"] == "pass"
    assert session["supportability_blocked"] is True
    assert session["supportability_overrode_adjudication"] is True
    assert session["adjudicated_pass"] is False
    assert session["final_failure_class"] == "supportability_blocked"
    assert session["adjudicated_replay_conflict"] is None
    assert report["strata"]["adjudicated_replay_conflicts"] == []
    assert any("supportability-blocked certification sessions" in item for item in report["claim_grade_blockers"])


def test_score_certification_exposes_runtime_unavailable_blockers(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-runtime-down",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            }
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-fred-runtime-down",
                "round_index": 1,
                "status_code": None,
                "series_count": 0,
                "error": "runtime_unavailable: health probe timed out",
                "request_failed": True,
                "runtime_unavailable": True,
                "runtime_unavailable_reason": "health probe timed out",
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metrics"]["runtime_unavailable_sessions"] == 1
    assert report["metrics"]["runtime_unavailable_by_type"] == {"direct": 1}
    assert report["metrics"]["runtime_unavailable_reason_counts"] == {"health probe timed out": 1}
    assert report["session_results"][0]["runtime_unavailable"] is True
    assert report["session_results"][0]["provisional_structural_pass"] is False
    assert any("runtime-unavailable certification sessions" in item for item in report["claim_grade_blockers"])


def test_certify_claim_blocks_floor_failures_even_when_topline_passes(tmp_path: Path):
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    production_path = tmp_path / "production.json"
    output_path = tmp_path / "claim.json"

    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": ["FRED below 0.98 floor"],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    production_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--production-score-report",
            str(production_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert decision["claim_allowed"] is False
    assert "required strata failed: FRED below 0.98 floor" in decision["blockers"]


def test_certify_claim_blocks_missing_required_strata(tmp_path: Path):
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    production_path = tmp_path / "production.json"
    output_path = tmp_path / "claim.json"

    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": ["WorldBank (high_traffic)"],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    production_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--production-score-report",
            str(production_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert decision["claim_allowed"] is False
    assert "required strata missing: WorldBank (high_traffic)" in decision["blockers"]


def test_certify_claim_blocks_unresolved_framework_clusters_and_material_parity(tmp_path: Path):
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    triage_path = tmp_path / "triage.json"
    production_path = tmp_path / "production.json"
    parity_path = tmp_path / "parity.json"
    output_path = tmp_path / "claim.json"

    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "floor_policy_path": str((ROOT / "validation" / "manifests" / "claim_gate_policy-v1.json").resolve()),
                "floor_policy_sha256": "abc123",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                    "wrong_confident_answer_rate": 0.0,
                    "unnecessary_clarification_rate": 0.0,
                    "ambiguity_resolution_success": 1.0,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    triage_path.write_text(
        json.dumps(
            {
                "summary": {
                    "framework_bug_cluster_counts": {
                        "unexpected_clarification_on_direct_query": 2
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    production_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "floor_policy_sha256": "abc123",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                    "wrong_confident_answer_rate": 0.0,
                    "unnecessary_clarification_rate": 0.0,
                    "ambiguity_resolution_success": 1.0,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    parity_path.write_text(
        json.dumps(
            {
                "summary": {
                    "severity_counts": {
                        "material": 1,
                        "match": 9,
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--triage-report",
            str(triage_path),
            "--production-score-report",
            str(production_path),
            "--parity-report",
            str(parity_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert decision["framework_bug_cluster_counts"] == {"unexpected_clarification_on_direct_query": 2}
    assert "unresolved framework failure clusters present: unexpected_clarification_on_direct_query=2" in decision["blockers"]
    assert "production parity has 1 material drift session(s)" in decision["blockers"]


def test_certify_claim_prefers_overall_weighted_fallback_metrics(tmp_path: Path):
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    triage_path = tmp_path / "triage.json"
    production_path = tmp_path / "production.json"
    parity_path = tmp_path / "parity.json"
    output_path = tmp_path / "claim.json"

    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "overall_weighted_provisional_success": 0.995,
                    "overall_weighted_lower95_approx": 0.991,
                    "direct_weighted_provisional_success": 0.2,
                    "direct_weighted_lower95_approx": 0.1,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    triage_path.write_text(
        json.dumps({"summary": {"framework_bug_cluster_counts": {}}}) + "\n",
        encoding="utf-8",
    )
    production_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    parity_path.write_text(
        json.dumps({"summary": {"severity_counts": {"match": 10, "material": 0}}}) + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--triage-report",
            str(triage_path),
            "--production-score-report",
            str(production_path),
            "--parity-report",
            str(parity_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert decision["claim_allowed"] is True
    assert decision["observed_success"] == 0.995
    assert decision["lower95"] == 0.991


def test_claim_gate_enforces_policy_semantic_thresholds_for_claim_grade(tmp_path: Path):
    policy_path = tmp_path / "claim_gate_policy.json"
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    production_path = tmp_path / "production.json"
    output_path = tmp_path / "claim.json"

    policy_path.write_text(
        json.dumps(
            {
                "claim_thresholds": {
                    "wrong_confident_answer_rate_max": 0.005,
                    "unnecessary_clarification_rate_max": 0.05,
                    "ambiguity_resolution_success_min": 0.99,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "floor_policy_path": str(policy_path),
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                    "wrong_confident_answer_rate_proxy": 0.01,
                    "unnecessary_clarification_rate_proxy": 0.06,
                    "ambiguity_resolution_success_proxy": 0.95,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    production_path.write_text(json.dumps({"claim_grade_ready": True}) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--production-score-report",
            str(production_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert "wrong_confident_answer_rate 0.010000 is above required 0.005000" in decision["blockers"]
    assert "unnecessary_clarification_rate 0.060000 is above required 0.050000" in decision["blockers"]
    assert "ambiguity_resolution_success 0.950000 is below required 0.990000" in decision["blockers"]


def test_claim_gate_uses_policy_thresholds_by_default(tmp_path: Path):
    policy_path = tmp_path / "claim_gate_policy.json"
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    production_path = tmp_path / "production.json"
    output_path = tmp_path / "claim.json"

    policy_path.write_text(
        json.dumps(
            {
                "claim_thresholds": {
                    "weighted_session_success_min": 0.997,
                    "lower95_min": 0.994,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "floor_policy_path": str(policy_path),
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    production_path.write_text(json.dumps({"claim_grade_ready": True}) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--production-score-report",
            str(production_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert decision["required_observed"] == 0.997
    assert decision["required_lower95"] == 0.994
    assert "observed success 0.995000 is below required 0.997000" in decision["blockers"]
    assert "lower95 0.991000 is below required 0.994000" in decision["blockers"]


def test_multiround_and_ambiguity_samplers_emit_selection_weights(tmp_path: Path):
    multiround_output = tmp_path / "multiround.jsonl"
    ambiguity_output = tmp_path / "ambiguity.jsonl"

    subprocess.run(
        [
            sys.executable,
            str(MULTI_SAMPLER),
            "--output",
            str(multiround_output),
            "--scale",
            "0.01",
            "--dataset-tier",
            "shadow",
            "--holdout-split",
            "shadow",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(AMBIG_SAMPLER),
            "--output",
            str(ambiguity_output),
            "--scale",
            "0.01",
            "--dataset-tier",
            "shadow",
            "--holdout-split",
            "shadow",
        ],
        check=True,
    )

    multiround_row = json.loads(multiround_output.read_text(encoding="utf-8").splitlines()[0])
    ambiguity_rows = [
        json.loads(line)
        for line in ambiguity_output.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ambiguity_row = ambiguity_rows[0]
    canada_employment = next(row for row in ambiguity_rows if row["query"] == "Canada employment")

    assert multiround_row["provenance"]["sampling_probability"] is not None
    assert multiround_row["provenance"]["selection_weight"] is not None
    assert ambiguity_row["provenance"]["sampling_probability"] is not None
    assert ambiguity_row["provenance"]["selection_weight"] is not None
    assert canada_employment["expected_behavior"] == "direct_answer"
    assert canada_employment["gold"]["acceptable_outcomes"] == ["direct_answer_correct"]


def test_certify_claim_surfaces_production_score_blockers(tmp_path: Path):
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    production_path = tmp_path / "production.json"
    output_path = tmp_path / "claim.json"

    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    production_path.write_text(
        json.dumps(
            {
                "claim_grade_ready": False,
                "claim_grade_blockers": [
                    "adjudicated replay conflicts present: curated-amb-terminology-001: adjudicated pass expected clarification but replay did not clarify"
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--production-score-report",
            str(production_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert "production score report is not marked claim_grade_ready" in decision["blockers"]
    assert (
        "production score blocker: adjudicated replay conflicts present: curated-amb-terminology-001: adjudicated pass expected clarification but replay did not clarify"
        in decision["blockers"]
    )


def test_certify_claim_enforces_production_thresholds(tmp_path: Path):
    score_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.json"
    production_path = tmp_path / "production.json"
    output_path = tmp_path / "claim.json"

    score_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.995,
                    "claim_lower95": 0.991,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(json.dumps({"adjudication_complete": True}) + "\n", encoding="utf-8")
    production_path.write_text(
        json.dumps(
            {
                "scoring_mode": "claim_grade",
                "claim_grade_ready": True,
                "snapshot_id": "snap-1",
                "metrics": {
                    "claim_observed_success": 0.991,
                    "claim_lower95": 0.980,
                },
                "strata": {
                    "failing_strata": [],
                    "missing_required_strata": [],
                },
                "session_results": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(CLAIM_SCRIPT),
            "--score-report",
            str(score_path),
            "--adjudication-summary",
            str(adjudication_path),
            "--production-score-report",
            str(production_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1
    decision = json.loads(output_path.read_text(encoding="utf-8"))
    assert "production observed success 0.991000 is below required 0.992000" in decision["blockers"]
    assert "production lower95 0.980000 is below required 0.990000" in decision["blockers"]


def test_score_certification_emits_overall_weighted_metrics_across_types(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 2.0,
                },
            },
            {
                "id": "provider-switch-000001",
                "dataset_tier": "cert_holdout",
                "rounds": [
                    {"query": "US GDP"},
                    {"query": "Switch to GDP growth"},
                ],
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 4.0,
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-000001", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "provider-switch-000001", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "provider-switch-000001", "round_index": 2, "status_code": 200, "series_count": 1, "error": None},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metrics"]["overall_weighted_provisional_success"] == 1.0
    assert report["metrics"]["weighted_by_type"]["direct"] == 1.0
    assert report["metrics"]["weighted_by_type"]["multiround"] == 1.0


def test_score_certification_uses_adjudication_records_when_present(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 2.0,
                },
            },
            {
                "id": "amb-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "direct_answer",
                "query": "US inflation",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-000001", "round_index": 1, "status_code": 500, "series_count": 0, "error": "boom"},
            {"session_id": "amb-000001", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {"session_id": "direct-fred-000001", "final_label": "pass"},
            {"session_id": "amb-000001", "final_label": "fail", "failure_class": "unnecessary_clarification"},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["scoring_mode"] == "adjudicated_structural"
    assert report["snapshot"]["adjudicated_session_count"] == 2
    assert report["metrics"]["adjudicated_session_success"]["overall_unweighted"] == 0.5
    assert report["metrics"]["overall_weighted_adjudicated_success"] == (2.0 / 3.0)
    assert report["metrics"]["overall_adjudication_weight_coverage"] == 1.0
    assert report["metrics"]["claim_metric_source"] == "adjudicated_structural"
    assert report["metrics"]["claim_observed_success"] == (2.0 / 3.0)
    assert report["metrics"]["claim_lower95"] is not None
    assert report["claim_grade_blockers"]
    by_session = {row["session_id"]: row for row in report["session_results"]}
    assert by_session["direct-fred-000001"]["adjudicated_pass"] is True
    assert by_session["amb-000001"]["adjudicated_pass"] is False
    assert by_session["amb-000001"]["final_failure_class"] == "unnecessary_clarification"


def test_score_certification_emits_semantic_behavior_proxies(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "gold": {"clarification_expected": False},
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 2.0,
                },
            },
            {
                "id": "amb-clarify-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "clarify",
                "query": "trade balance",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
            {
                "id": "amb-direct-000002",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "direct_answer",
                "query": "Bitcoin price",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-fred-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "error": None,
                "clarification_detected": False,
                "response_text_present": True,
            },
            {
                "session_id": "amb-clarify-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 0,
                "error": None,
                "clarification_detected": True,
                "response_text_present": True,
            },
            {
                "session_id": "amb-direct-000002",
                "round_index": 1,
                "status_code": 200,
                "series_count": 0,
                "error": None,
                "clarification_detected": True,
                "response_text_present": True,
            },
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {"session_id": "direct-fred-000001", "final_label": "fail", "failure_class": "wrong_confident_answer"},
            {"session_id": "amb-clarify-000001", "final_label": "pass"},
            {"session_id": "amb-direct-000002", "final_label": "fail", "failure_class": "unnecessary_clarification"},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    metrics = report["metrics"]
    assert metrics["wrong_confident_answer_rate_proxy"] == (1 / 3)
    assert metrics["unnecessary_clarification_rate_proxy"] == (1 / 2)
    assert metrics["expected_clarification_rate_proxy"] == 1.0
    assert metrics["ambiguity_resolution_success_proxy"] == 1.0
    assert metrics["wrong_confident_answer_rate"] == (2.0 / 4.0)
    assert metrics["unnecessary_clarification_rate"] == (1.0 / 3.0)
    assert metrics["ambiguity_resolution_success"] == 1.0


def test_score_certification_claim_lower95_uses_design_strata(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "gold": {"clarification_expected": False},
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
            {
                "id": "direct-fred-000003",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US CPI",
                "gold": {"clarification_expected": False},
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
            {
                "id": "direct-imf-000002",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Japan GDP",
                "gold": {"clarification_expected": False},
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
            {
                "id": "direct-imf-000004",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Japan CPI",
                "gold": {"clarification_expected": False},
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-000001", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-fred-000003", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-imf-000002", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-imf-000004", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {"session_id": "direct-fred-000001", "final_label": "pass"},
            {"session_id": "direct-fred-000003", "final_label": "pass"},
            {"session_id": "direct-imf-000002", "final_label": "pass"},
            {"session_id": "direct-imf-000004", "final_label": "pass"},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    confidence = report["metrics"]["overall_weighted_adjudicated_design_confidence"]
    assert confidence["method"] == "stratified_weighted_wilson_by_design_stratum"
    assert confidence["strata_count"] == 2
    assert set(confidence["strata"]) == {"direct_provider:FRED", "direct_provider:IMF"}
    assert report["metrics"]["claim_confidence_method"] == confidence["method"]
    assert report["metrics"]["claim_lower95"] == confidence["lower95"]
    assert report["metrics"]["overall_weighted_adjudicated_lower95_approx"] > confidence["lower95"]


def test_score_certification_rescales_incremental_batch_weights_to_fixed_design_population(tmp_path: Path):
    batch1_path = tmp_path / "batch1.jsonl"
    batch2_path = tmp_path / "batch2.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    policy_path = tmp_path / "policy.json"
    snapshot_path = tmp_path / "snapshot.json"
    output_path = tmp_path / "score.json"

    snapshot_path.write_text(
        json.dumps(
            {
                "snapshot_date": "2026-05-16",
                "git_sha": "test",
                "indicator_count": 200,
                "provider_counts": {"FRED": 100, "IMF": 100},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy_path.write_text(
        json.dumps(
            {
                "snapshot_manifest_path": str(snapshot_path),
                "claim_thresholds": {},
                "required_direct_provider_floors": {},
                "required_multiround_family_floors": {},
                "required_ambiguity_family_floors": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    write_jsonl(
        batch1_path,
        [
            {
                "id": "direct-fred-b1-1",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {"snapshot_id": "2026-05-16:test:200", "holdout_split": "batch1", "selection_weight": 50.0},
            },
            {
                "id": "direct-fred-b1-2",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US CPI",
                "provenance": {"snapshot_id": "2026-05-16:test:200", "holdout_split": "batch1", "selection_weight": 50.0},
            },
            {
                "id": "direct-imf-b1-1",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Japan GDP",
                "provenance": {"snapshot_id": "2026-05-16:test:200", "holdout_split": "batch1", "selection_weight": 50.0},
            },
            {
                "id": "direct-imf-b1-2",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Japan CPI",
                "provenance": {"snapshot_id": "2026-05-16:test:200", "holdout_split": "batch1", "selection_weight": 50.0},
            },
        ],
    )
    write_jsonl(
        batch2_path,
        [
            {
                "id": "direct-fred-b2-1",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US employment",
                "provenance": {"snapshot_id": "2026-05-16:test:200", "holdout_split": "batch2", "selection_weight": 50.0},
            },
            {
                "id": "direct-fred-b2-2",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US unemployment",
                "provenance": {"snapshot_id": "2026-05-16:test:200", "holdout_split": "batch2", "selection_weight": 50.0},
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-b1-1", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-fred-b1-2", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-imf-b1-1", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-imf-b1-2", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-fred-b2-1", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-fred-b2-2", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(batch1_path),
            "--dataset",
            str(batch2_path),
            "--raw-results",
            str(raw_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    weighting = report["metrics"]["cumulative_design_weighting"]["strata"]
    assert weighting["direct_provider:FRED"]["raw_weight_total"] == 200.0
    assert weighting["direct_provider:FRED"]["scoring_weight_total"] == 100.0
    assert weighting["direct_provider:IMF"]["raw_weight_total"] == 100.0
    assert weighting["direct_provider:IMF"]["scoring_weight_total"] == 100.0

    confidence = report["metrics"]["overall_weighted_design_confidence"]
    assert confidence["strata"]["direct_provider:FRED"]["population_weight_share"] == 0.5
    assert confidence["strata"]["direct_provider:IMF"]["population_weight_share"] == 0.5


def test_score_certification_blocks_claim_grade_for_mixed_snapshots(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "gold": {"clarification_expected": False},
                "provenance": {"snapshot_id": "snap-1", "holdout_split": "cert_holdout", "selection_weight": 1.0},
            },
            {
                "id": "direct-fred-000002",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US CPI",
                "gold": {"clarification_expected": False},
                "provenance": {"snapshot_id": "snap-2", "holdout_split": "cert_holdout", "selection_weight": 1.0},
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-000001", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-fred-000002", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {"session_id": "direct-fred-000001", "final_label": "pass"},
            {"session_id": "direct-fred-000002", "final_label": "pass"},
        ],
    )
    policy_path.write_text(
        json.dumps(
            {
                "claim_thresholds": {
                    "weighted_session_success_min": 0.99,
                    "lower95_min": 0.1,
                },
                "required_direct_provider_floors": {"FRED": {"class": "critical", "floor": 0.97}},
                "required_multiround_family_floors": {},
                "required_ambiguity_family_floors": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["claim_grade_ready"] is False
    assert any("mixed snapshot_ids" in blocker for blocker in report["claim_grade_blockers"])


def test_score_certification_evaluates_multiround_and_ambiguity_family_floors(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "provider-switch-000001",
                "dataset_tier": "cert_holdout",
                "family": "provider_switch_chain",
                "rounds": [
                    {"query": "US GDP from FRED"},
                    {"query": "Switch to GDP growth"},
                ],
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 3.0,
                },
            },
            {
                "id": "amb-provider-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "direct_answer",
                "query": "Japan GDP growth rate",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 2.0,
                    "family": "provider_ambiguity",
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "provider-switch-000001", "round_index": 1, "status_code": 500, "series_count": 0, "error": "boom"},
            {"session_id": "provider-switch-000001", "round_index": 2, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "amb-provider-000001", "round_index": 1, "status_code": 500, "series_count": 0, "error": "boom"},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["strata"]["multiround_family_success"]["provider_switch_chain"]["pass_rate"] == 0.0
    assert report["strata"]["ambiguity_family_success"]["provider_ambiguity"]["pass_rate"] == 0.0
    assert "multiround_family:provider_switch_chain 0.000 below critical floor 0.970" in report["strata"]["failing_strata"]
    assert "ambiguity_family:provider_ambiguity 0.000 below critical floor 0.970" in report["strata"]["failing_strata"]
    assert "ambiguity_family:dominant_interpretation_cases (high_traffic)" in report["strata"]["missing_required_strata"]


def test_score_certification_counts_expected_clarification_as_provisional_ambiguity_success(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "amb-scope-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "clarify",
                "query": "GDP in Europe",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                    "family": "scope_ambiguity",
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "amb-scope-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 0,
                "error": None,
                "clarification_detected": True,
                "response_text_present": True,
            },
        ],
    )
    policy_path.write_text(
        json.dumps(
            {
                "required_direct_provider_floors": {},
                "required_multiround_family_floors": {},
                "required_ambiguity_family_floors": {
                    "scope_ambiguity": {"class": "critical", "floor": 0.97}
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    session = report["session_results"][0]
    assert session["expected_clarification"] is True
    assert session["clarification_detected"] is True
    assert session["provisional_structural_pass"] is True
    assert report["strata"]["ambiguity_family_success"]["scope_ambiguity"]["pass_rate"] == 1.0
    assert "ambiguity_family:scope_ambiguity" not in " ".join(report["strata"]["failing_strata"])


def test_score_certification_can_reach_claim_grade_with_full_coverage_and_custom_policy(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "gold": {"clarification_expected": False},
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 2.0,
                },
            },
            {
                "id": "amb-clarify-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "clarify",
                "query": "trade balance",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                    "family": "provider_ambiguity",
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-fred-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "error": None,
                "clarification_detected": False,
                "response_text_present": True,
            },
            {
                "session_id": "amb-clarify-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 0,
                "error": None,
                "clarification_detected": True,
                "response_text_present": True,
            },
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {"session_id": "direct-fred-000001", "final_label": "pass"},
            {"session_id": "amb-clarify-000001", "final_label": "pass"},
        ],
    )
    policy_path.write_text(
        json.dumps(
            {
                "claim_thresholds": {
                    "weighted_session_success_min": 0.99,
                    "lower95_min": 0.2,
                    "wrong_confident_answer_rate_max": 0.05,
                    "unnecessary_clarification_rate_max": 0.1,
                    "ambiguity_resolution_success_min": 0.99,
                },
                "required_direct_provider_floors": {
                    "FRED": {"class": "critical", "floor": 0.97}
                },
                "required_multiround_family_floors": {},
                "required_ambiguity_family_floors": {
                    "provider_ambiguity": {"class": "critical", "floor": 0.97}
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["scoring_mode"] == "claim_grade"
    assert report["claim_grade_ready"] is True
    assert report["claim_grade_blockers"] == []
    assert report["metrics"]["claim_metric_source"] == "adjudicated_structural"
    assert report["metrics"]["claim_observed_success"] == 1.0
    assert report["metrics"]["wrong_confident_answer_rate"] == 0.0
    assert report["metrics"]["unnecessary_clarification_rate"] == 0.0
    assert report["metrics"]["ambiguity_resolution_success"] == 1.0


def _write_oecd_supportability_inventory_fixture(
    tmp_path: Path,
    *,
    replacement_session_ids: list[str],
) -> tuple[Path, Path, Path, Path, Path]:
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    policy_path = tmp_path / "policy.json"
    inventory_path = tmp_path / "supportability_inventory.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-oecd-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "OECD",
                "query": "Population in the National Accounts from OECD",
                "gold": {"clarification_expected": False},
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 2.0,
                },
            },
            {
                "id": "amb-clarify-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "clarify",
                "query": "GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                    "family": "provider_ambiguity",
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-oecd-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "error": None,
                "clarification_detected": False,
                "response_text_present": True,
            },
            {
                "session_id": "amb-clarify-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 0,
                "error": None,
                "clarification_detected": True,
                "response_text_present": True,
            },
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {"session_id": "direct-oecd-000001", "final_label": "pass"},
            {"session_id": "amb-clarify-000001", "final_label": "pass"},
        ],
    )
    policy_path.write_text(
        json.dumps(
            {
                "claim_thresholds": {
                    "weighted_session_success_min": 0.99,
                    "lower95_min": 0.2,
                    "wrong_confident_answer_rate_max": 0.05,
                    "unnecessary_clarification_rate_max": 0.1,
                    "ambiguity_resolution_success_min": 0.99,
                },
                "required_direct_provider_floors": {
                    "OECD": {"class": "critical", "floor": 0.97}
                },
                "required_multiround_family_floors": {},
                "required_ambiguity_family_floors": {
                    "provider_ambiguity": {"class": "critical", "floor": 0.97}
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    inventory_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "session_id": "original-oecd-null",
                        "provider": "OECD",
                        "supportability_reason": "oecd_missing_valued_observations",
                        "disposition": "excluded_replaced",
                        "replacement_session_ids": replacement_session_ids,
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return dataset_path, raw_path, adjudication_path, policy_path, inventory_path


def test_score_certification_surfaces_resolved_supportability_inventory(tmp_path: Path):
    output_path = tmp_path / "score.json"
    dataset_path, raw_path, adjudication_path, policy_path, inventory_path = (
        _write_oecd_supportability_inventory_fixture(
            tmp_path,
            replacement_session_ids=["direct-oecd-000001"],
        )
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--supportability-inventory",
            str(inventory_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["claim_grade_ready"] is True
    assert report["claim_grade_blockers"] == []
    assert report["supportability_inventory_path"] == str(inventory_path.resolve())
    assert len(report["supportability_inventory_sha256"]) == 64
    assert report["supportability_inventory"]["total_items"] == 1
    assert report["supportability_inventory"]["resolved_items"] == 1
    assert report["supportability_inventory"]["unresolved_items"] == 0
    assert report["supportability_inventory"]["items"][0]["resolved"] is True
    assert report["supportability_inventory"]["items"][0]["replacement_results"] == [
        {
            "session_id": "direct-oecd-000001",
            "present": True,
            "provisional_structural_pass": True,
            "supportability_blocked": False,
            "runtime_unavailable": False,
        }
    ]
    assert report["metrics"]["supportability_inventory_items"] == 1
    assert report["metrics"]["supportability_inventory_resolved_items"] == 1
    assert report["metrics"]["supportability_inventory_unresolved_items"] == 0


def test_score_certification_enforces_policy_lower95_before_claim_grade_ready(tmp_path: Path):
    output_path = tmp_path / "score.json"
    dataset_path, raw_path, adjudication_path, policy_path, inventory_path = (
        _write_oecd_supportability_inventory_fixture(
            tmp_path,
            replacement_session_ids=["direct-oecd-000001"],
        )
    )
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["claim_thresholds"]["lower95_min"] = 0.99
    policy_path.write_text(json.dumps(policy) + "\n", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--supportability-inventory",
            str(inventory_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["claim_grade_ready"] is False
    assert report["scoring_mode"] == "adjudicated_structural"
    assert report["metrics"]["claim_lower95"] < 0.99
    assert any("claim_lower95" in blocker and "below required 0.990000" in blocker for blocker in report["claim_grade_blockers"])


def test_score_certification_blocks_unresolved_supportability_inventory(tmp_path: Path):
    output_path = tmp_path / "score.json"
    dataset_path, raw_path, adjudication_path, policy_path, inventory_path = (
        _write_oecd_supportability_inventory_fixture(
            tmp_path,
            replacement_session_ids=["missing-oecd-row"],
        )
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--supportability-inventory",
            str(inventory_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["claim_grade_ready"] is False
    assert report["supportability_inventory"]["unresolved_items"] == 1
    assert report["supportability_inventory"]["unresolved_session_ids"] == ["original-oecd-null"]
    assert report["metrics"]["supportability_inventory_unresolved_items"] == 1
    assert any(
        "supportability inventory exclusions lack passing replacement" in blocker
        for blocker in report["claim_grade_blockers"]
    )


def test_score_certification_respects_max_sessions_across_loaded_datasets(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {"snapshot_id": "snap-1", "holdout_split": "cert_holdout", "selection_weight": 1.0},
            },
            {
                "id": "direct-imf-000002",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "IMF",
                "query": "Japan GDP",
                "provenance": {"snapshot_id": "snap-1", "holdout_split": "cert_holdout", "selection_weight": 1.0},
            },
            {
                "id": "direct-wb-000003",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "WorldBank",
                "query": "China imports share of gdp",
                "provenance": {"snapshot_id": "snap-1", "holdout_split": "cert_holdout", "selection_weight": 1.0},
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {"session_id": "direct-fred-000001", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
            {"session_id": "direct-imf-000002", "round_index": 1, "status_code": 200, "series_count": 1, "error": None},
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--max-sessions",
            "2",
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["snapshot"]["session_count"] == 2


def test_score_certification_blocks_claim_grade_when_adjudicated_pass_conflicts_with_replay(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.jsonl"
    policy_path = tmp_path / "policy.json"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "direct-fred-000001",
                "dataset_tier": "cert_holdout",
                "provider_stratum": "FRED",
                "query": "US GDP",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                },
            },
            {
                "id": "amb-clarify-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "clarify",
                "query": "interest rate",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                    "family": "transform_ambiguity",
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "direct-fred-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "error": None,
                "clarification_detected": False,
                "response_text_present": True,
            },
            {
                "session_id": "amb-clarify-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "error": None,
                "clarification_detected": False,
                "response_text_present": True,
            },
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {"session_id": "direct-fred-000001", "final_label": "pass"},
            {"session_id": "amb-clarify-000001", "final_label": "pass"},
        ],
    )
    policy_path.write_text(
        json.dumps(
            {
                "claim_thresholds": {
                    "weighted_session_success_min": 0.99,
                    "lower95_min": 0.3,
                    "wrong_confident_answer_rate_max": 0.05,
                    "unnecessary_clarification_rate_max": 0.1,
                    "ambiguity_resolution_success_min": 0.99,
                },
                "required_direct_provider_floors": {
                    "FRED": {"class": "critical", "floor": 0.97}
                },
                "required_multiround_family_floors": {},
                "required_ambiguity_family_floors": {
                    "transform_ambiguity": {"class": "critical", "floor": 0.97}
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--floor-policy",
            str(policy_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["claim_grade_ready"] is False
    assert report["metrics"]["adjudicated_replay_conflict_count"] == 1
    assert any(
        "amb-clarify-000001: adjudicated pass expected clarification but replay did not clarify" in item
        for item in report["claim_grade_blockers"]
    )
    assert report["strata"]["adjudicated_replay_conflicts"] == [
        "amb-clarify-000001: adjudicated pass expected clarification but replay did not clarify"
    ]


def test_score_certification_accepts_reviewed_direct_answer_for_clarify_gold(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "score.json"
    adjudication_path = tmp_path / "adjudication.jsonl"

    write_jsonl(
        dataset_path,
        [
            {
                "id": "amb-transform-000001",
                "dataset_tier": "cert_holdout",
                "expected_behavior": "clarify",
                "query": "Canada employment",
                "provenance": {
                    "snapshot_id": "snap-1",
                    "holdout_split": "cert_holdout",
                    "selection_weight": 1.0,
                    "family": "transform_ambiguity",
                },
                "gold": {
                    "acceptable_outcomes": [
                        "clarification_with_correct_options",
                        "multiround_clarification_to_correct_answer",
                    ],
                    "human_review_required": True,
                },
            },
        ],
    )
    write_jsonl(
        raw_path,
        [
            {
                "session_id": "amb-transform-000001",
                "round_index": 1,
                "status_code": 200,
                "series_count": 1,
                "error": None,
                "clarification_detected": False,
                "response_text_present": False,
                "countries": ["Canada"],
                "indicators": ["employment"],
                "series_ids": ["14100375:1.3.1.1.1.0.0.0.0.0"],
                "api_urls": ["https://www150.statcan.gc.ca/t1/wds/rest/getDataFromCubePidCoordAndLatestNPeriods"],
                "source_urls": ["https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1410037501"],
            },
        ],
    )
    write_jsonl(
        adjudication_path,
        [
            {
                "session_id": "amb-transform-000001",
                "final_label": "pass",
                "accepted_outcome": "direct_answer_correct",
                "notes": "Direct national employment answer accepted after human review.",
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCORE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--raw-results",
            str(raw_path),
            "--adjudication-records",
            str(adjudication_path),
            "--output",
            str(output_path),
        ],
        check=True,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    session = report["session_results"][0]
    assert session["adjudicated_pass"] is True
    assert session["adjudicated_direct_answer_accepted"] is True
    assert session["adjudicated_replay_conflict"] is None
    assert report["metrics"]["adjudicated_replay_conflict_count"] == 0
    assert not any("adjudicated replay conflicts" in item for item in report["claim_grade_blockers"])
