from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_SCRIPT = ROOT / "scripts" / "validation" / "run_claim_bundle.py"


def test_run_claim_bundle_dry_run_wires_expected_steps(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    dataset_path.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["mode"] == "dry_run"
    assert "run_certification" in payload["commands"]
    assert "score_certification" in payload["commands"]
    assert "build_adjudication_queue" in payload["commands"]
    assert "adjudication_summary" in payload["commands"]
    assert "build_adjudication_triage_report" in payload["commands"]
    assert "certify_claim" in payload["commands"]
    assert "build_evidence_package" in payload["commands"]
    assert payload["existing_adjudication_records"] is None
    assert payload["commands"]["build_adjudication_triage_report"] is not None
    assert payload["commands"]["build_evidence_package"] is None
    assert "--triage-report" in payload["commands"]["certify_claim"]


def test_run_claim_bundle_uses_existing_adjudication_for_summary(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    dataset_path.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(
        json.dumps({"session_id": "direct-fred-000001", "final_label": "pass"}) + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--existing-adjudication-records",
            str(adjudication_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    summary_cmd = payload["commands"]["adjudication_summary"]
    queue_cmd = payload["commands"]["build_adjudication_queue"]
    triage_cmd = payload["commands"]["build_adjudication_triage_report"]
    assert payload["existing_adjudication_records"] == str(adjudication_path.resolve())
    assert str(adjudication_path.resolve()) in summary_cmd
    assert str(adjudication_path.resolve()) not in queue_cmd
    assert triage_cmd is not None


def test_run_claim_bundle_propagates_max_sessions_to_score_step(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    dataset_path.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--max-sessions",
            "2",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert "--max-sessions" in payload["commands"]["run_certification"]
    assert "--max-sessions" in payload["commands"]["score_certification"]


def test_run_claim_bundle_passes_resume_controls_to_run_certification_and_start_to_score(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    dataset_path.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--run-resume",
            "--start-index",
            "29",
            "--run-concurrency",
            "8",
            "--run-request-timeout",
            "45",
            "--run-skip-completed",
            "--run-classify-unsupported-direct",
            "--run-continue-on-error",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    run_cmd = payload["commands"]["run_certification"]
    score_cmd = payload["commands"]["score_certification"]
    assert "--resume" in run_cmd
    assert "--start-index" in run_cmd
    assert "29" in run_cmd
    assert "--concurrency" in run_cmd
    assert "8" in run_cmd
    assert "--request-timeout" in run_cmd
    assert "45.0" in run_cmd
    assert "--skip-completed" in run_cmd
    assert "--classify-unsupported-direct" in run_cmd
    assert "--continue-on-error" in run_cmd
    assert "--start-index" in score_cmd
    assert "29" in score_cmd


def test_run_claim_bundle_can_plan_production_replay(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    production_dataset_path = tmp_path / "prod_replay.jsonl"
    dataset_path.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    production_dataset_path.write_text(dataset_path.read_text(encoding="utf-8"), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--run-production-replay",
            "--production-dataset",
            str(production_dataset_path),
            "--production-max-sessions",
            "3",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    replay_cmd = payload["commands"]["replay_production_holdout"]
    compare_cmd = payload["commands"]["compare_replay_reports"]
    evidence_cmd = payload["commands"]["build_evidence_package"]
    claim_cmd = payload["commands"]["certify_claim"]
    assert payload["run_production_replay"] is True
    assert replay_cmd is not None
    assert compare_cmd is not None
    assert evidence_cmd is not None
    assert "--production-score-report" in claim_cmd
    assert "--triage-report" in claim_cmd
    assert "--parity-report" in claim_cmd
    assert "--max-sessions" in replay_cmd
    assert "--parity-report" in evidence_cmd
    assert "--triage-report" in evidence_cmd
    assert str(Path(payload["parity_output"]).resolve()) in compare_cmd


def test_run_claim_bundle_reuses_existing_adjudication_for_production_replay(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    adjudication_path = tmp_path / "adjudication.jsonl"
    dataset_path.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adjudication_path.write_text(
        json.dumps({"session_id": "direct-fred-000001", "final_label": "pass"}) + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--existing-adjudication-records",
            str(adjudication_path),
            "--run-production-replay",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    replay_cmd = payload["commands"]["replay_production_holdout"]
    evidence_cmd = payload["commands"]["build_evidence_package"]
    triage_cmd = payload["commands"]["build_adjudication_triage_report"]
    assert payload["production_adjudication_records"] == str(adjudication_path.resolve())
    assert str(adjudication_path.resolve()) in replay_cmd
    assert evidence_cmd is not None
    assert triage_cmd is not None


def test_run_claim_bundle_forwards_existing_parity_report_when_production_score_is_supplied(tmp_path: Path):
    dataset_path = tmp_path / "dataset.jsonl"
    production_score_path = tmp_path / "production-score.json"
    parity_path = tmp_path / "parity.json"
    dataset_path.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    production_score_path.write_text(json.dumps({"snapshot_id": "snap-1"}) + "\n", encoding="utf-8")
    parity_path.write_text(json.dumps({"summary": {"severity_counts": {"material": 0}}}) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(BUNDLE_SCRIPT),
            "--dataset",
            str(dataset_path),
            "--production-score-report",
            str(production_score_path),
            "--production-parity-report",
            str(parity_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    claim_cmd = payload["commands"]["certify_claim"]
    evidence_cmd = payload["commands"]["build_evidence_package"]
    assert payload["run_production_replay"] is False
    assert "--production-score-report" in claim_cmd
    assert "--parity-report" in claim_cmd
    assert str(parity_path.resolve()) in claim_cmd
    assert evidence_cmd is not None
    assert str(parity_path.resolve()) in evidence_cmd
