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


def derive_failure_class(row: dict[str, Any]) -> str:
    if row.get('answer_present_without_clarification') and not row.get('clarification_detected'):
        return 'wrong_confident_answer_proxy'
    expected_clarification = row.get('expected_clarification')
    clarification_detected = row.get('clarification_detected')
    if expected_clarification is False and clarification_detected:
        return 'unnecessary_clarification_proxy'
    if expected_clarification is True and not clarification_detected:
        return 'missing_expected_clarification_proxy'
    if row.get('error'):
        return 'error_or_no_data'
    return 'needs_review'


def derive_review_focus_tags(row: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if row.get('dataset_type'):
        tags.append(f"dataset_type:{row['dataset_type']}")
    if row.get('provider_stratum') and row.get('provider_stratum') != '<missing>':
        tags.append(f"provider:{row['provider_stratum']}")
    if row.get('family_stratum'):
        tags.append(f"family:{row['family_stratum']}")
    expected_clarification = row.get('expected_clarification')
    clarification_detected = row.get('clarification_detected')
    if expected_clarification is True:
        tags.append('expected_clarification')
    elif expected_clarification is False:
        tags.append('expected_direct_answer')
    if clarification_detected:
        tags.append('clarification_detected')
    if row.get('answer_present_without_clarification'):
        tags.append('answer_present_without_clarification')
    if row.get('error'):
        tags.append('error_present')
    return tags


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
    eligible_pass_rows: list[dict[str, Any]] = []
    for row in results:
        include = False
        reason = None
        if not row.get('provisional_structural_pass'):
            include = True
            reason = 'all_failures'
        elif row.get('human_review_required'):
            eligible_pass_rows.append(row)
            if rng.random() < args.pass_sample_rate:
                include = True
                reason = 'random_pass_audit'
        if include:
            failure_class = derive_failure_class(row)
            queue.append({
                'session_id': row['session_id'],
                'dataset_type': row['dataset_type'],
                'dataset_tier': row['dataset_tier'],
                'holdout_split': row.get('holdout_split'),
                'provider_stratum': row.get('provider_stratum'),
                'family_stratum': row.get('family_stratum'),
                'queue_reason': reason,
                'automated_label': 'pass' if row.get('provisional_structural_pass') else 'fail',
                'final_label': None,
                'failure_class': failure_class,
                'review_focus_tags': derive_review_focus_tags(row),
                'clarification_detected': row.get('clarification_detected'),
                'expected_clarification': row.get('expected_clarification'),
                'answer_present_without_clarification': row.get('answer_present_without_clarification'),
                'notes': None,
            })

    if eligible_pass_rows and not any(row['queue_reason'] == 'random_pass_audit' for row in queue):
        row = eligible_pass_rows[0]
        failure_class = derive_failure_class(row)
        queue.append({
            'session_id': row['session_id'],
            'dataset_type': row['dataset_type'],
            'dataset_tier': row['dataset_tier'],
            'holdout_split': row.get('holdout_split'),
            'provider_stratum': row.get('provider_stratum'),
            'family_stratum': row.get('family_stratum'),
            'queue_reason': 'minimum_pass_audit',
            'automated_label': 'pass',
            'final_label': None,
            'failure_class': failure_class,
            'review_focus_tags': derive_review_focus_tags(row),
            'clarification_detected': row.get('clarification_detected'),
            'expected_clarification': row.get('expected_clarification'),
            'answer_present_without_clarification': row.get('answer_present_without_clarification'),
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
