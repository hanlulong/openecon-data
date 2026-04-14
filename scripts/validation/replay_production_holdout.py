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


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Replay the production holdout dataset against data.openecon.ai or another target.')
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--raw-output', type=Path, default=DEFAULT_RAW)
    parser.add_argument('--score-output', type=Path, default=DEFAULT_SCORE)
    parser.add_argument('--max-sessions', type=int, default=None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    dataset = args.dataset.resolve()
    if not dataset.exists():
        raise SystemExit(f'Dataset not found: {dataset}')

    run_cmd = [sys.executable, str(ROOT / 'scripts' / 'validation' / 'run_certification.py'), '--dataset', str(dataset), '--output', str(args.raw_output.resolve()), '--base-url', args.base_url]
    if args.max_sessions is not None:
        run_cmd += ['--max-sessions', str(args.max_sessions)]
    if args.dry_run:
        run_cmd.append('--dry-run')
    run(run_cmd)

    if args.dry_run:
        payload = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': 'dry_run',
            'dataset': str(dataset),
            'base_url': args.base_url,
            'raw_output': str(args.raw_output.resolve()),
            'score_output': str(args.score_output.resolve()),
        }
        print(json.dumps(payload, indent=2))
        return 0

    score_cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'validation' / 'score_certification.py'),
        '--dataset', str(dataset),
        '--raw-results', str(args.raw_output.resolve()),
        '--output', str(args.score_output.resolve()),
    ]
    run(score_cmd)

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'execute',
        'dataset': str(dataset),
        'base_url': args.base_url,
        'raw_output': str(args.raw_output.resolve()),
        'score_output': str(args.score_output.resolve()),
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
