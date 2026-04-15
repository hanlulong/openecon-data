from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUEUE_SCRIPT = ROOT / "scripts" / "validation" / "build_adjudication_queue.py"
SUMMARY_SCRIPT = ROOT / "scripts" / "validation" / "adjudication_summary.py"


def test_build_adjudication_queue_emits_failure_classes_and_focus_tags(tmp_path: Path):
    score_path = tmp_path / "score.json"
    queue_path = tmp_path / "queue.jsonl"

    score_path.write_text(
        json.dumps(
            {
                "session_results": [
                    {
                        "session_id": "direct-1",
                        "dataset_type": "direct",
                        "dataset_tier": "cert_holdout",
                        "holdout_split": "cert_holdout",
                        "provider_stratum": "FRED",
                        "family_stratum": None,
                        "provisional_structural_pass": False,
                        "clarification_detected": False,
                        "expected_clarification": False,
                        "answer_present_without_clarification": True,
                        "error": None,
                        "human_review_required": True,
                    },
                    {
                        "session_id": "amb-1",
                        "dataset_type": "ambiguity",
                        "dataset_tier": "cert_holdout",
                        "holdout_split": "cert_holdout",
                        "provider_stratum": "<missing>",
                        "family_stratum": "provider_ambiguity",
                        "provisional_structural_pass": False,
                        "clarification_detected": True,
                        "expected_clarification": False,
                        "answer_present_without_clarification": False,
                        "error": "clarify",
                        "human_review_required": True,
                    },
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(QUEUE_SCRIPT),
            "--score-report",
            str(score_path),
            "--output",
            str(queue_path),
            "--pass-sample-rate",
            "1.0",
        ],
        check=True,
    )

    rows = [json.loads(line) for line in queue_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_session = {row["session_id"]: row for row in rows}
    assert by_session["direct-1"]["failure_class"] == "wrong_confident_answer_proxy"
    assert "provider:FRED" in by_session["direct-1"]["review_focus_tags"]
    assert "answer_present_without_clarification" in by_session["direct-1"]["review_focus_tags"]
    assert by_session["amb-1"]["failure_class"] == "unnecessary_clarification_proxy"
    assert "family:provider_ambiguity" in by_session["amb-1"]["review_focus_tags"]
    assert by_session["amb-1"]["clarification_detected"] is True


def test_adjudication_summary_counts_failure_classes(tmp_path: Path):
    queue_path = tmp_path / "queue.jsonl"
    summary_path = tmp_path / "summary.json"

    queue_path.write_text(
        "".join(
            [
                json.dumps(
                    {
                        "session_id": "direct-1",
                        "queue_reason": "all_failures",
                        "failure_class": "wrong_confident_answer_proxy",
                        "final_label": None,
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "session_id": "amb-1",
                        "queue_reason": "all_failures",
                        "failure_class": "unnecessary_clarification_proxy",
                        "final_label": "fail",
                    }
                )
                + "\n",
            ]
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(SUMMARY_SCRIPT),
            "--queue",
            str(queue_path),
            "--output",
            str(summary_path),
        ],
        check=True,
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["failure_class_counts"]["wrong_confident_answer_proxy"] == 1
    assert summary["failure_class_counts"]["unnecessary_clarification_proxy"] == 1


def test_build_adjudication_queue_guarantees_minimum_pass_audit(tmp_path: Path):
    score_path = tmp_path / "score.json"
    queue_path = tmp_path / "queue.jsonl"

    score_path.write_text(
        json.dumps(
            {
                "session_results": [
                    {
                        "session_id": "direct-1",
                        "dataset_type": "direct",
                        "dataset_tier": "cert_holdout",
                        "holdout_split": "cert_holdout",
                        "provider_stratum": "FRED",
                        "family_stratum": None,
                        "provisional_structural_pass": True,
                        "clarification_detected": False,
                        "expected_clarification": False,
                        "answer_present_without_clarification": True,
                        "error": None,
                        "human_review_required": True,
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(QUEUE_SCRIPT),
            "--score-report",
            str(score_path),
            "--output",
            str(queue_path),
            "--pass-sample-rate",
            "0.0",
        ],
        check=True,
    )

    rows = [json.loads(line) for line in queue_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["queue_reason"] == "minimum_pass_audit"
    assert rows[0]["automated_label"] == "pass"
