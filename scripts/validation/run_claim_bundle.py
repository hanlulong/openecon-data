#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_raw_results.jsonl"
DEFAULT_SCORE_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_score.json"
DEFAULT_QUEUE_OUTPUT = ROOT / "validation_private" / "adjudication" / "claim_bundle_review_queue.jsonl"
DEFAULT_ADJUDICATION_SUMMARY = ROOT / "validation_private" / "adjudication" / "claim_bundle_adjudication_summary.json"
DEFAULT_CLAIM_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_decision.json"
DEFAULT_BASE_URL = "http://localhost:3001"
DEFAULT_PRODUCTION_BASE_URL = "https://data.openecon.ai"
DEFAULT_PRODUCTION_DATASET = ROOT / "validation_private" / "datasets" / "prod_replay" / "prod_replay-sessions.jsonl"
DEFAULT_PRODUCTION_RAW_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_production_raw.jsonl"
DEFAULT_PRODUCTION_SCORE_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_production_score.json"


def run(cmd: list[str], *, dry_run: bool) -> None:
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a local claim-bundle workflow: certification -> score -> adjudication artifacts -> claim decision."
    )
    parser.add_argument("--dataset", action="append", type=Path, required=True, help="JSONL dataset file; pass multiple times")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--raw-output", type=Path, default=DEFAULT_RAW_OUTPUT)
    parser.add_argument("--score-output", type=Path, default=DEFAULT_SCORE_OUTPUT)
    parser.add_argument("--queue-output", type=Path, default=DEFAULT_QUEUE_OUTPUT)
    parser.add_argument("--adjudication-summary-output", type=Path, default=DEFAULT_ADJUDICATION_SUMMARY)
    parser.add_argument("--claim-output", type=Path, default=DEFAULT_CLAIM_OUTPUT)
    parser.add_argument("--floor-policy", type=Path, default=ROOT / "validation" / "manifests" / "claim_gate_policy-v1.json")
    parser.add_argument("--existing-adjudication-records", type=Path, default=None)
    parser.add_argument("--production-score-report", type=Path, default=None)
    parser.add_argument("--run-production-replay", action="store_true")
    parser.add_argument("--production-dataset", type=Path, default=DEFAULT_PRODUCTION_DATASET)
    parser.add_argument("--production-base-url", default=DEFAULT_PRODUCTION_BASE_URL)
    parser.add_argument("--production-raw-output", type=Path, default=DEFAULT_PRODUCTION_RAW_OUTPUT)
    parser.add_argument("--production-score-output", type=Path, default=DEFAULT_PRODUCTION_SCORE_OUTPUT)
    parser.add_argument("--production-adjudication-records", type=Path, default=None)
    parser.add_argument("--production-max-sessions", type=int, default=None)
    parser.add_argument("--pass-sample-rate", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=20260414)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    datasets = [path.resolve() for path in args.dataset]
    raw_output = args.raw_output.resolve()
    score_output = args.score_output.resolve()
    queue_output = args.queue_output.resolve()
    adjudication_summary_output = args.adjudication_summary_output.resolve()
    claim_output = args.claim_output.resolve()
    floor_policy = args.floor_policy.resolve()
    existing_adjudication = args.existing_adjudication_records.resolve() if args.existing_adjudication_records else None
    production_score_report = args.production_score_report.resolve() if args.production_score_report else None
    production_dataset = args.production_dataset.resolve()
    production_raw_output = args.production_raw_output.resolve()
    production_score_output = args.production_score_output.resolve()
    production_adjudication_records = (
        args.production_adjudication_records.resolve()
        if args.production_adjudication_records is not None
        else existing_adjudication
    )

    run_cert_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "run_certification.py"),
    ]
    for dataset in datasets:
        run_cert_cmd += ["--dataset", str(dataset)]
    run_cert_cmd += ["--output", str(raw_output), "--base-url", args.base_url]
    if args.max_sessions is not None:
        run_cert_cmd += ["--max-sessions", str(args.max_sessions)]

    score_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "score_certification.py"),
    ]
    for dataset in datasets:
        score_cmd += ["--dataset", str(dataset)]
    score_cmd += [
        "--raw-results",
        str(raw_output),
        "--output",
        str(score_output),
        "--floor-policy",
        str(floor_policy),
    ]
    if args.max_sessions is not None:
        score_cmd += ["--max-sessions", str(args.max_sessions)]
    if existing_adjudication is not None:
        score_cmd += ["--adjudication-records", str(existing_adjudication)]

    queue_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "build_adjudication_queue.py"),
        "--score-report",
        str(score_output),
        "--output",
        str(queue_output),
        "--pass-sample-rate",
        str(args.pass_sample_rate),
        "--seed",
        str(args.seed),
    ]

    adjudication_summary_input = existing_adjudication if existing_adjudication is not None else queue_output
    adjud_summary_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "adjudication_summary.py"),
        "--queue",
        str(adjudication_summary_input),
        "--output",
        str(adjudication_summary_output),
    ]

    replay_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "replay_production_holdout.py"),
        "--dataset",
        str(production_dataset),
        "--base-url",
        args.production_base_url,
        "--raw-output",
        str(production_raw_output),
        "--score-output",
        str(production_score_output),
        "--floor-policy",
        str(floor_policy),
    ]
    if production_adjudication_records is not None:
        replay_cmd += ["--adjudication-records", str(production_adjudication_records)]
    if args.production_max_sessions is not None:
        replay_cmd += ["--max-sessions", str(args.production_max_sessions)]

    claim_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "certify_claim.py"),
        "--score-report",
        str(score_output),
        "--adjudication-summary",
        str(adjudication_summary_output),
        "--output",
        str(claim_output),
    ]
    effective_production_score_report = production_score_output if args.run_production_replay else production_score_report
    if effective_production_score_report is not None:
        claim_cmd += ["--production-score-report", str(effective_production_score_report)]

    plan = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run" if args.dry_run else "execute",
        "datasets": [str(path) for path in datasets],
        "raw_output": str(raw_output),
        "score_output": str(score_output),
        "queue_output": str(queue_output),
        "adjudication_summary_output": str(adjudication_summary_output),
        "claim_output": str(claim_output),
        "floor_policy": str(floor_policy),
        "existing_adjudication_records": str(existing_adjudication) if existing_adjudication is not None else None,
        "production_score_report": str(production_score_report) if production_score_report is not None else None,
        "run_production_replay": args.run_production_replay,
        "production_dataset": str(production_dataset),
        "production_base_url": args.production_base_url,
        "production_raw_output": str(production_raw_output),
        "production_score_output": str(production_score_output),
        "production_adjudication_records": str(production_adjudication_records) if production_adjudication_records is not None else None,
        "commands": {
            "run_certification": run_cert_cmd,
            "score_certification": score_cmd,
            "build_adjudication_queue": queue_cmd,
            "adjudication_summary": adjud_summary_cmd,
            "replay_production_holdout": replay_cmd if args.run_production_replay else None,
            "certify_claim": claim_cmd,
        },
    }

    print(json.dumps(plan, indent=2))
    if args.dry_run:
        return 0

    run(run_cert_cmd, dry_run=False)
    run(score_cmd, dry_run=False)
    if existing_adjudication is None:
        run(queue_cmd, dry_run=False)
    run(adjud_summary_cmd, dry_run=False)
    if args.run_production_replay:
        run(replay_cmd, dry_run=False)
    claim_proc = subprocess.run(claim_cmd, check=False, capture_output=True, text=True)
    if claim_proc.stdout.strip():
        print(claim_proc.stdout.strip())
    if claim_proc.stderr.strip():
        print(claim_proc.stderr.strip(), file=sys.stderr)
    return claim_proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
