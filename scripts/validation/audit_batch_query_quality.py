#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'batch_query_quality_audit.json'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.validation.common import audit_direct_query_shape  # noqa: E402


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


def audit_direct_query(row: dict[str, Any]) -> dict[str, Any]:
    return audit_direct_query_shape(row)


def main() -> int:
    parser = argparse.ArgumentParser(description='Audit candidate review-batch query quality for likely catalog-title/pathological prompt shapes.')
    parser.add_argument('--dataset', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows = []
    risk_counts = Counter()
    reason_counts = Counter()
    type_counts = Counter()

    for dataset in args.dataset:
        for row in iter_jsonl(dataset.resolve()):
            kind = dataset_type(row)
            type_counts[kind] += 1
            audit = audit_direct_query(row) if kind == 'direct' else {'risk_level': 'low', 'reasons': [], 'query_length': len(str(row.get('query') or '')), 'punctuation_hits': 0}
            risk_counts[audit['risk_level']] += 1
            for reason in audit['reasons']:
                reason_counts[reason] += 1
            rows.append(
                {
                    'id': row.get('id'),
                    'dataset_type': kind,
                    'query': row.get('query'),
                    **audit,
                }
            )

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'datasets': [str(path.resolve()) for path in args.dataset],
        'summary': {
            'row_count': len(rows),
            'by_type': dict(type_counts),
            'risk_counts': dict(risk_counts),
            'reason_counts': dict(reason_counts),
            'high_risk_rows': sum(1 for row in rows if row['risk_level'] == 'high'),
        },
        'flagged_rows': [row for row in rows if row['risk_level'] != 'low'],
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'high_risk_rows': payload['summary']['high_risk_rows']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
