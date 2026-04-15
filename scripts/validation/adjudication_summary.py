#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'adjudication' / 'adjudication_summary.json'


def iter_jsonl(path: Path):
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main() -> int:
    parser = argparse.ArgumentParser(description='Summarize adjudication progress and final labels from a review queue JSONL file.')
    parser.add_argument('--queue', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows: list[dict[str, Any]] = list(iter_jsonl(args.queue.resolve()))
    total = len(rows)
    queue_reason_counts = Counter(str(row.get('queue_reason') or '<missing>') for row in rows)
    final_label_counts = Counter(str(row.get('final_label') or '<missing>') for row in rows)
    failure_class_counts = Counter(str(row.get('failure_class') or '<missing>') for row in rows)
    completed = sum(1 for row in rows if row.get('final_label'))
    pending = total - completed

    summary = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'queue_path': str(args.queue.resolve()),
        'total_records': total,
        'completed_records': completed,
        'pending_records': pending,
        'completion_rate': (completed / total) if total else 0.0,
        'queue_reason_counts': dict(queue_reason_counts),
        'failure_class_counts': dict(failure_class_counts),
        'final_label_counts': dict(final_label_counts),
        'adjudication_complete': pending == 0,
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
