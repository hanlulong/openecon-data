#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'adjudication' / 'review_queue.jsonl'


def main() -> int:
    parser = argparse.ArgumentParser(description='Build an adjudication queue from a certification score report.')
    parser.add_argument('--score-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--pass-sample-rate', type=float, default=0.10)
    parser.add_argument('--seed', type=int, default=20260414)
    args = parser.parse_args()

    report = json.loads(args.score_report.resolve().read_text(encoding='utf-8'))
    results: list[dict[str, Any]] = list(report.get('session_results') or [])
    rng = random.Random(args.seed)
    queue = []
    for row in results:
        include = False
        reason = None
        if not row.get('provisional_structural_pass'):
            include = True
            reason = 'all_failures'
        elif row.get('human_review_required'):
            if rng.random() < args.pass_sample_rate:
                include = True
                reason = 'random_pass_audit'
        if include:
            queue.append({
                'session_id': row['session_id'],
                'dataset_type': row['dataset_type'],
                'dataset_tier': row['dataset_tier'],
                'holdout_split': row.get('holdout_split'),
                'queue_reason': reason,
                'automated_label': 'pass' if row.get('provisional_structural_pass') else 'fail',
                'final_label': None,
                'notes': None,
            })

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8') as f:
        for row in queue:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

    print(json.dumps({
        'output': str(output),
        'records': len(queue),
        'seed': args.seed,
        'pass_sample_rate': args.pass_sample_rate,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
