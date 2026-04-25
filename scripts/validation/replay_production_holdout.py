#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / 'validation_private' / 'datasets' / 'prod_replay' / 'prod_replay-sessions.jsonl'
DEFAULT_RAW = ROOT / 'validation_private' / 'reports' / 'production_holdout_raw.jsonl'
DEFAULT_SCORE = ROOT / 'validation_private' / 'reports' / 'production_holdout_score.json'
DEFAULT_BASE_URL = 'https://data.openecon.ai'
DEFAULT_FLOOR_POLICY = ROOT / 'validation' / 'manifests' / 'claim_gate_policy-v1.json'


def run(cmd: list[str], *, capture_output: bool = False) -> str | None:
    result = subprocess.run(cmd, check=True, capture_output=capture_output, text=True)
    if capture_output:
        return result.stdout
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description='Replay the production holdout dataset against data.openecon.ai or another target.')
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--raw-output', type=Path, default=DEFAULT_RAW)
    parser.add_argument('--score-output', type=Path, default=DEFAULT_SCORE)
    parser.add_argument('--floor-policy', type=Path, default=DEFAULT_FLOOR_POLICY)
    parser.add_argument('--adjudication-records', type=Path, default=None)
    parser.add_argument('--max-sessions', type=int, default=None)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--request-timeout', type=float, default=120)
    parser.add_argument('--request-spacing', type=float, default=0)
    parser.add_argument('--rate-limit-retries', type=int, default=0)
    parser.add_argument('--rate-limit-backoff', type=float, default=10.0)
    parser.add_argument('--classify-unsupported-direct', action='store_true')
    parser.add_argument('--continue-on-error', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--skip-completed', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    dataset = args.dataset.resolve()
    if not dataset.exists():
        raise SystemExit(f'Dataset not found: {dataset}')

    run_cmd = [sys.executable, str(ROOT / 'scripts' / 'validation' / 'run_certification.py'), '--dataset', str(dataset), '--output', str(args.raw_output.resolve()), '--base-url', args.base_url]
    if args.max_sessions is not None:
        run_cmd += ['--max-sessions', str(args.max_sessions)]
    run_cmd += [
        '--concurrency', str(args.concurrency),
        '--request-timeout', str(args.request_timeout),
        '--request-spacing', str(args.request_spacing),
        '--rate-limit-retries', str(args.rate_limit_retries),
        '--rate-limit-backoff', str(args.rate_limit_backoff),
    ]
    if args.classify_unsupported_direct:
        run_cmd.append('--classify-unsupported-direct')
    if args.continue_on_error:
        run_cmd.append('--continue-on-error')
    if args.resume:
        run_cmd.append('--resume')
    if args.skip_completed:
        run_cmd.append('--skip-completed')
    if args.dry_run:
        run_cmd.append('--dry-run')
        run_preview = run(run_cmd, capture_output=True)
    else:
        run(run_cmd)
        run_preview = None

    if args.dry_run:
        payload = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': 'dry_run',
            'dataset': str(dataset),
            'base_url': args.base_url,
            'raw_output': str(args.raw_output.resolve()),
            'score_output': str(args.score_output.resolve()),
            'floor_policy': str(args.floor_policy.resolve()),
            'adjudication_records': str(args.adjudication_records.resolve()) if args.adjudication_records else None,
            'concurrency': args.concurrency,
            'request_timeout': args.request_timeout,
            'request_spacing': args.request_spacing,
            'rate_limit_retries': args.rate_limit_retries,
            'rate_limit_backoff': args.rate_limit_backoff,
            'classify_unsupported_direct': args.classify_unsupported_direct,
            'continue_on_error': args.continue_on_error,
            'resume': args.resume,
            'skip_completed': args.skip_completed,
            'run_certification_command': run_cmd,
            'run_certification_preview': json.loads(run_preview) if run_preview else None,
        }
        print(json.dumps(payload, indent=2))
        return 0

    score_cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'validation' / 'score_certification.py'),
        '--dataset', str(dataset),
        '--raw-results', str(args.raw_output.resolve()),
        '--output', str(args.score_output.resolve()),
        '--floor-policy', str(args.floor_policy.resolve()),
    ]
    if args.adjudication_records is not None:
        score_cmd += ['--adjudication-records', str(args.adjudication_records.resolve())]
    if args.max_sessions is not None:
        score_cmd += ['--max-sessions', str(args.max_sessions)]
    run(score_cmd)

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'execute',
        'dataset': str(dataset),
        'base_url': args.base_url,
        'raw_output': str(args.raw_output.resolve()),
        'score_output': str(args.score_output.resolve()),
        'floor_policy': str(args.floor_policy.resolve()),
        'adjudication_records': str(args.adjudication_records.resolve()) if args.adjudication_records else None,
        'concurrency': args.concurrency,
        'request_timeout': args.request_timeout,
        'request_spacing': args.request_spacing,
        'rate_limit_retries': args.rate_limit_retries,
        'rate_limit_backoff': args.rate_limit_backoff,
        'classify_unsupported_direct': args.classify_unsupported_direct,
        'continue_on_error': args.continue_on_error,
        'resume': args.resume,
        'skip_completed': args.skip_completed,
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
