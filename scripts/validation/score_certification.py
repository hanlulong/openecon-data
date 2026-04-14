#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'certification-score-summary.json'


def iter_jsonl(path: Path):
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def dataset_type(row: dict[str, Any]) -> str:
    if 'rounds' in row:
        return 'multiround'
    if 'expected_behavior' in row:
        return 'ambiguity'
    return 'direct'


def structural_pass(result: dict[str, Any]) -> bool:
    return int(result.get('status_code') or 0) == 200 and not result.get('error') and int(result.get('series_count') or 0) > 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Score raw certification execution results with a provisional structural scorer.')
    parser.add_argument('--dataset', action='append', type=Path, required=True)
    parser.add_argument('--raw-results', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    sessions: dict[str, dict[str, Any]] = {}
    for dataset in args.dataset:
        for row in iter_jsonl(dataset.resolve()):
            sid = str(row.get('id') or '')
            sessions[sid] = row

    raw_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in iter_jsonl(args.raw_results.resolve()):
        raw_by_session[str(row.get('session_id') or '')].append(row)

    session_results = []
    counts_by_type = Counter()
    counts_by_tier = Counter()
    provisional_pass_by_type = Counter()
    provisional_weight_pass = 0.0
    provisional_weight_total = 0.0

    for sid, session in sessions.items():
        kind = dataset_type(session)
        tier = str(session.get('dataset_tier') or '<missing>')
        rows = sorted(raw_by_session.get(sid, []), key=lambda r: int(r.get('round_index') or 0))
        all_pass = bool(rows) and all(structural_pass(r) for r in rows)
        any_error = next((r.get('error') for r in rows if r.get('error')), None)
        result_record = {
            'session_id': sid,
            'dataset_type': kind,
            'dataset_tier': tier,
            'holdout_split': str((session.get('provenance') or {}).get('holdout_split') or '<missing>'),
            'provisional_structural_pass': all_pass,
            'round_count_expected': len(session.get('rounds', [])) if kind == 'multiround' else 1,
            'round_count_observed': len(rows),
            'error': any_error,
            'human_review_required': bool((session.get('gold') or {}).get('human_review_required') or any((round_case.get('gold') or {}).get('human_review_required') for round_case in session.get('rounds', []))),
            'provenance': session.get('provenance'),
        }
        session_results.append(result_record)
        counts_by_type[kind] += 1
        counts_by_tier[tier] += 1
        if all_pass:
            provisional_pass_by_type[kind] += 1

        if kind == 'direct':
            weight = float(((session.get('provenance') or {}).get('selection_weight')) or 0.0)
            if weight > 0:
                provisional_weight_total += weight
                if all_pass:
                    provisional_weight_pass += weight

    metrics = {
        'provisional_structural_session_success': {
            'overall_unweighted': (sum(1 for r in session_results if r['provisional_structural_pass']) / len(session_results)) if session_results else 0.0,
            'by_type': {
                kind: (provisional_pass_by_type[kind] / counts_by_type[kind]) if counts_by_type[kind] else 0.0
                for kind in counts_by_type
            },
            'by_tier': {
                tier: (
                    sum(1 for r in session_results if r['dataset_tier'] == tier and r['provisional_structural_pass']) / counts_by_tier[tier]
                ) if counts_by_tier[tier] else 0.0
                for tier in counts_by_tier
            },
        },
        'direct_weighted_provisional_success': (
            provisional_weight_pass / provisional_weight_total if provisional_weight_total else None
        ),
    }

    report = {
        'run_id': f"score-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'scoring_mode': 'provisional_structural',
        'claim_grade_ready': False,
        'snapshot': {
            'session_count': len(session_results),
            'dataset_types': dict(counts_by_type),
            'dataset_tiers': dict(counts_by_tier),
        },
        'metrics': metrics,
        'session_results': session_results,
        'limitations': [
            'This scorer only checks provisional structural success (status/error/non-empty result).',
            'It does not yet perform semantic adjudication or claim-grade weighted inference.',
            'A public 99% claim must not rely on this report alone.'
        ],
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'output': str(output),
        'session_count': len(session_results),
        'overall_unweighted': metrics['provisional_structural_session_success']['overall_unweighted'],
        'direct_weighted_provisional_success': metrics['direct_weighted_provisional_success'],
        'claim_grade_ready': False,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
