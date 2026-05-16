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
DEFAULT_TRIAGE_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_triage.json"
DEFAULT_CLAIM_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_decision.json"
DEFAULT_BASE_URL = "http://localhost:3001"
DEFAULT_PRODUCTION_BASE_URL = "https://data.openecon.ai"
DEFAULT_PRODUCTION_DATASET = ROOT / "validation_private" / "datasets" / "prod_replay" / "prod_replay-sessions.jsonl"
DEFAULT_PRODUCTION_RAW_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_production_raw.jsonl"
DEFAULT_PRODUCTION_SCORE_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_production_score.json"
DEFAULT_PARITY_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_parity.json"
DEFAULT_EVIDENCE_PACKAGE_OUTPUT = ROOT / "validation_private" / "reports" / "claim_bundle_evidence_package.json"
DEFAULT_CATALOG_SNAPSHOT = ROOT / "validation" / "manifests" / "catalog_snapshot-2026-04-14.json"
DEFAULT_PROVIDER_DISTRIBUTION = ROOT / "validation" / "manifests" / "provider_distribution-latest.json"
DEFAULT_STRATA_DEFINITION = ROOT / "validation" / "manifests" / "strata_definition-v2.json"


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
    parser.add_argument("--triage-output", type=Path, default=DEFAULT_TRIAGE_OUTPUT)
    parser.add_argument("--claim-output", type=Path, default=DEFAULT_CLAIM_OUTPUT)
    parser.add_argument("--floor-policy", type=Path, default=ROOT / "validation" / "manifests" / "claim_gate_policy-v1.json")
    parser.add_argument("--existing-adjudication-records", type=Path, default=None)
    parser.add_argument(
        "--supportability-inventory",
        type=Path,
        default=None,
        help=(
            "Optional supportability/backfill inventory passed through to "
            "score_certification.py for explicit replacement accounting. "
            "This is local scoring provenance only; production replay does "
            "not consume it unless replay_production_holdout.py grows a "
            "separate explicit inventory flag."
        ),
    )
    parser.add_argument("--production-score-report", type=Path, default=None)
    parser.add_argument("--run-production-replay", action="store_true")
    parser.add_argument("--production-dataset", type=Path, default=DEFAULT_PRODUCTION_DATASET)
    parser.add_argument("--production-base-url", default=DEFAULT_PRODUCTION_BASE_URL)
    parser.add_argument("--production-raw-output", type=Path, default=DEFAULT_PRODUCTION_RAW_OUTPUT)
    parser.add_argument("--production-score-output", type=Path, default=DEFAULT_PRODUCTION_SCORE_OUTPUT)
    parser.add_argument("--production-adjudication-records", type=Path, default=None)
    parser.add_argument("--production-parity-report", type=Path, default=None)
    parser.add_argument("--production-max-sessions", type=int, default=None)
    parser.add_argument("--parity-output", type=Path, default=DEFAULT_PARITY_OUTPUT)
    parser.add_argument("--evidence-package-output", type=Path, default=DEFAULT_EVIDENCE_PACKAGE_OUTPUT)
    parser.add_argument("--catalog-snapshot", type=Path, default=DEFAULT_CATALOG_SNAPSHOT)
    parser.add_argument("--provider-distribution", type=Path, default=DEFAULT_PROVIDER_DISTRIBUTION)
    parser.add_argument("--strata-definition", type=Path, default=DEFAULT_STRATA_DEFINITION)
    parser.add_argument("--pass-sample-rate", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=20260414)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--start-index", type=int, default=0, help="0-based session index passed to run_certification.py and score_certification.py.")
    parser.add_argument("--run-concurrency", type=int, default=None, help="Pass --concurrency to run_certification.py.")
    parser.add_argument("--run-request-timeout", type=float, default=None, help="Pass --request-timeout to run_certification.py.")
    parser.add_argument("--run-request-spacing", type=float, default=None, help="Pass --request-spacing to run_certification.py.")
    parser.add_argument("--run-rate-limit-retries", type=int, default=None, help="Pass --rate-limit-retries to run_certification.py.")
    parser.add_argument("--run-rate-limit-backoff", type=float, default=None, help="Pass --rate-limit-backoff to run_certification.py.")
    parser.add_argument("--run-resume", action="store_true", help="Pass --resume to run_certification.py.")
    parser.add_argument("--run-start-index", type=int, default=None, help="Deprecated alias: pass --start-index to run_certification.py and score_certification.py.")
    parser.add_argument("--run-skip-completed", action="store_true", help="Pass --skip-completed to run_certification.py.")
    parser.add_argument(
        "--run-classify-unsupported-direct",
        action="store_true",
        help="Pass --classify-unsupported-direct to run_certification.py for explicit supportability-blocked rows.",
    )
    parser.add_argument(
        "--run-continue-on-error",
        action="store_true",
        help="Pass --continue-on-error to run_certification.py so long baselines record transport failures and continue.",
    )
    parser.add_argument(
        "--run-runtime-unavailable-reason",
        default=None,
        help="Pass --runtime-unavailable-reason to run_certification.py for a fail-closed no-HTTP baseline.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    datasets = [path.resolve() for path in args.dataset]
    raw_output = args.raw_output.resolve()
    score_output = args.score_output.resolve()
    queue_output = args.queue_output.resolve()
    adjudication_summary_output = args.adjudication_summary_output.resolve()
    triage_output = args.triage_output.resolve()
    claim_output = args.claim_output.resolve()
    floor_policy = args.floor_policy.resolve()
    existing_adjudication = args.existing_adjudication_records.resolve() if args.existing_adjudication_records else None
    supportability_inventory = args.supportability_inventory.resolve() if args.supportability_inventory else None
    production_score_report = args.production_score_report.resolve() if args.production_score_report else None
    production_dataset = args.production_dataset.resolve()
    production_raw_output = args.production_raw_output.resolve()
    production_score_output = args.production_score_output.resolve()
    production_parity_report = args.production_parity_report.resolve() if args.production_parity_report is not None else None
    parity_output = args.parity_output.resolve()
    evidence_package_output = args.evidence_package_output.resolve()
    catalog_snapshot = args.catalog_snapshot.resolve()
    provider_distribution = args.provider_distribution.resolve()
    strata_definition = args.strata_definition.resolve()
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
    effective_start_index = args.run_start_index if args.run_start_index is not None else args.start_index
    if args.max_sessions is not None:
        run_cert_cmd += ["--max-sessions", str(args.max_sessions)]
    if args.run_concurrency is not None:
        run_cert_cmd += ["--concurrency", str(args.run_concurrency)]
    if args.run_request_timeout is not None:
        run_cert_cmd += ["--request-timeout", str(args.run_request_timeout)]
    if args.run_request_spacing is not None:
        run_cert_cmd += ["--request-spacing", str(args.run_request_spacing)]
    if args.run_rate_limit_retries is not None:
        run_cert_cmd += ["--rate-limit-retries", str(args.run_rate_limit_retries)]
    if args.run_rate_limit_backoff is not None:
        run_cert_cmd += ["--rate-limit-backoff", str(args.run_rate_limit_backoff)]
    if effective_start_index:
        run_cert_cmd += ["--start-index", str(effective_start_index)]
    if args.run_resume:
        run_cert_cmd += ["--resume"]
    if args.run_skip_completed:
        run_cert_cmd += ["--skip-completed"]
    if args.run_classify_unsupported_direct:
        run_cert_cmd += ["--classify-unsupported-direct"]
    if args.run_continue_on_error:
        run_cert_cmd += ["--continue-on-error"]
    if args.run_runtime_unavailable_reason:
        run_cert_cmd += ["--runtime-unavailable-reason", args.run_runtime_unavailable_reason]

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
    if effective_start_index:
        score_cmd += ["--start-index", str(effective_start_index)]
    if existing_adjudication is not None:
        score_cmd += ["--adjudication-records", str(existing_adjudication)]
    if supportability_inventory is not None:
        score_cmd += ["--supportability-inventory", str(supportability_inventory)]

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

    triage_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "build_adjudication_triage_report.py"),
        "--queue",
        str(queue_output),
        "--raw-results",
        str(raw_output),
        "--output",
        str(triage_output),
    ]
    for dataset in datasets:
        triage_cmd += ["--dataset", str(dataset)]

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

    compare_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "compare_replay_reports.py"),
        "--local-raw",
        str(raw_output),
        "--production-raw",
        str(production_raw_output),
        "--local-score",
        str(score_output),
        "--production-score",
        str(production_score_output),
        "--output",
        str(parity_output),
    ]

    claim_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validation" / "certify_claim.py"),
        "--score-report",
        str(score_output),
        "--adjudication-summary",
        str(adjudication_summary_output),
        "--triage-report",
        str(triage_output),
        "--output",
        str(claim_output),
    ]
    effective_production_score_report = production_score_output if args.run_production_replay else production_score_report
    effective_parity_report = parity_output if args.run_production_replay else production_parity_report
    if effective_production_score_report is not None:
        claim_cmd += ["--production-score-report", str(effective_production_score_report)]
    if effective_parity_report is not None:
        claim_cmd += ["--parity-report", str(effective_parity_report)]

    evidence_cmd = None
    if effective_production_score_report is not None:
        evidence_cmd = [
            sys.executable,
            str(ROOT / "scripts" / "validation" / "build_evidence_package.py"),
            "--catalog-snapshot",
            str(catalog_snapshot),
            "--provider-distribution",
            str(provider_distribution),
            "--strata-definition",
            str(strata_definition),
            "--local-score-report",
            str(score_output),
            "--adjudication-summary",
            str(adjudication_summary_output),
            "--triage-report",
            str(triage_output),
            "--production-score-report",
            str(effective_production_score_report),
            "--claim-decision",
            str(claim_output),
            "--deployment-base-url",
            args.production_base_url,
            "--output",
            str(evidence_package_output),
        ]
        if args.run_production_replay:
            evidence_cmd += ["--parity-report", str(parity_output)]
        elif effective_parity_report is not None:
            evidence_cmd += ["--parity-report", str(effective_parity_report)]

    plan = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run" if args.dry_run else "execute",
        "datasets": [str(path) for path in datasets],
        "raw_output": str(raw_output),
        "score_output": str(score_output),
        "queue_output": str(queue_output),
        "adjudication_summary_output": str(adjudication_summary_output),
        "triage_output": str(triage_output),
        "claim_output": str(claim_output),
        "floor_policy": str(floor_policy),
        "existing_adjudication_records": str(existing_adjudication) if existing_adjudication is not None else None,
        "supportability_inventory": str(supportability_inventory) if supportability_inventory is not None else None,
        "production_supportability_inventory_supported": False,
        "production_score_report": str(production_score_report) if production_score_report is not None else None,
        "run_production_replay": args.run_production_replay,
        "production_dataset": str(production_dataset),
        "production_base_url": args.production_base_url,
        "production_raw_output": str(production_raw_output),
        "production_score_output": str(production_score_output),
        "production_adjudication_records": str(production_adjudication_records) if production_adjudication_records is not None else None,
        "production_parity_report": str(production_parity_report) if production_parity_report is not None else None,
        "parity_output": str(parity_output),
        "evidence_package_output": str(evidence_package_output),
        "commands": {
            "run_certification": run_cert_cmd,
            "score_certification": score_cmd,
            "build_adjudication_queue": queue_cmd,
            "adjudication_summary": adjud_summary_cmd,
            "build_adjudication_triage_report": triage_cmd,
            "replay_production_holdout": replay_cmd if args.run_production_replay else None,
            "compare_replay_reports": compare_cmd if args.run_production_replay else None,
            "certify_claim": claim_cmd,
            "build_evidence_package": evidence_cmd,
        },
    }

    print(json.dumps(plan, indent=2))
    if args.dry_run:
        return 0

    run(run_cert_cmd, dry_run=False)
    run(score_cmd, dry_run=False)
    run(queue_cmd, dry_run=False)
    run(triage_cmd, dry_run=False)
    run(adjud_summary_cmd, dry_run=False)
    if args.run_production_replay:
        run(replay_cmd, dry_run=False)
        run(compare_cmd, dry_run=False)
    claim_proc = subprocess.run(claim_cmd, check=False, capture_output=True, text=True)
    if claim_proc.stdout.strip():
        print(claim_proc.stdout.strip())
    if claim_proc.stderr.strip():
        print(claim_proc.stderr.strip(), file=sys.stderr)
    if evidence_cmd is not None and claim_proc.returncode == 0 and claim_output.exists():
        run(evidence_cmd, dry_run=False)
    return claim_proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
