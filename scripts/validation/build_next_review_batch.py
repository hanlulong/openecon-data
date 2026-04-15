#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'next_review_batch_plan.json'
DEFAULT_BATCH_SIZE = 50


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def allocate_integer_budget(total: int, weighted_keys: list[tuple[str, float]]) -> dict[str, int]:
    if total <= 0 or not weighted_keys:
        return {key: 0 for key, _ in weighted_keys}
    total_weight = sum(max(weight, 0.0) for _, weight in weighted_keys)
    if total_weight <= 0:
        weighted_keys = [(key, 1.0) for key, _ in weighted_keys]
        total_weight = float(len(weighted_keys))

    raw = {key: (total * max(weight, 0.0) / total_weight) for key, weight in weighted_keys}
    base = {key: int(value) for key, value in raw.items()}
    remainder = total - sum(base.values())
    ranked = sorted(
        weighted_keys,
        key=lambda item: (raw[item[0]] - base[item[0]], item[0]),
        reverse=True,
    )
    for key, _ in ranked[:remainder]:
        base[key] += 1
    return base


def build_type_targets(batch_sessions: int, queue_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if batch_sessions <= 0 or not queue_rows:
        return []
    weighted = [(str(row['name']), float(row.get('remaining_n') or 0)) for row in queue_rows if int(row.get('remaining_n') or 0) > 0]
    allocated = allocate_integer_budget(batch_sessions, weighted)
    by_name = {str(row['name']): row for row in queue_rows}
    targets = []
    for name, planned in allocated.items():
        row = by_name[name]
        remaining = int(row.get('remaining_n') or 0)
        planned = min(planned, remaining)
        if planned <= 0:
            continue
        targets.append(
            {
                'name': name,
                'class': row.get('class'),
                'floor': row.get('floor'),
                'current_n': int(row.get('current_n') or 0),
                'target_n': int(row.get('target_n') or 0),
                'remaining_n': remaining,
                'planned_batch_sessions': planned,
            }
        )
    return sorted(targets, key=lambda item: (item['planned_batch_sessions'], item['remaining_n'], item['name']), reverse=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Build a concrete next review batch plan from the current progress report.')
    parser.add_argument('--progress-report', type=Path, required=True)
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    progress_report = load_json(args.progress_report.resolve())
    dataset_type_progress = dict(progress_report.get('dataset_type_progress') or {})
    priority_queue = dict(progress_report.get('priority_queue') or {})

    dataset_type_remaining = [
        (str(name), float((payload or {}).get('remaining_n') or 0))
        for name, payload in sorted(dataset_type_progress.items())
        if int((payload or {}).get('remaining_n') or 0) > 0
    ]
    allocated_nonzero = allocate_integer_budget(max(0, args.batch_size), dataset_type_remaining)
    by_type = {
        str(name): int(allocated_nonzero.get(str(name), 0))
        for name in sorted(dataset_type_progress)
    }

    allocation = {
        dataset_type: {
            'planned_batch_sessions': int(by_type.get(dataset_type, 0)),
            'targets': build_type_targets(int(by_type.get(dataset_type, 0)), list(priority_queue.get(dataset_type) or [])),
        }
        for dataset_type in sorted(dataset_type_progress)
    }

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'progress_report_path': str(args.progress_report.resolve()),
        'snapshot_id': progress_report.get('snapshot_id'),
        'batch_size': int(args.batch_size),
        'effective_n_progress': progress_report.get('effective_n_progress'),
        'dataset_type_batch_allocation': by_type,
        'allocation': allocation,
        'notes': [
            'Batch allocation is proportional to remaining session targets from the current progress report.',
            'Within each dataset type, sessions are allocated proportionally to the remaining_n of the highest-priority queue entries.',
            'This artifact plans the next review slice only; it does not itself generate dataset rows or adjudication labels.',
        ],
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'batch_size': int(args.batch_size)}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
