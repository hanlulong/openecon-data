#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'certification-raw-results.jsonl'
DEFAULT_BASE_URL = 'http://localhost:3001'


def detect_dataset_type(row: dict[str, Any]) -> str:
    if 'rounds' in row:
        return 'multiround'
    if 'expected_behavior' in row:
        return 'ambiguity'
    return 'direct'


def iter_jsonl(path: Path):
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def dry_run_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type = Counter(detect_dataset_type(row) for row in rows)
    by_tier = Counter(str(row.get('dataset_tier') or '<missing>') for row in rows)
    by_split = Counter(str((row.get('provenance') or {}).get('holdout_split') or '<missing>') for row in rows)
    return {
        'records': len(rows),
        'by_type': dict(by_type),
        'by_tier': dict(by_tier),
        'by_split': dict(by_split),
    }


def execute_rows(rows: list[dict[str, Any]], base_url: str) -> list[dict[str, Any]]:
    base = base_url.rstrip('/') + '/api/query'
    results = []
    for row in rows:
        dataset_type = detect_dataset_type(row)
        if dataset_type == 'multiround':
            conv = None
            for i, round_case in enumerate(row['rounds'], start=1):
                payload = {'query': round_case['query']}
                if conv:
                    payload['conversationId'] = conv
                t0 = time.time()
                resp = requests.post(base, json=payload, timeout=120)
                elapsed = time.time() - t0
                data = resp.json()
                conv = data.get('conversationId') or data.get('conversation_id') or conv
                results.append({
                    'session_id': row['id'],
                    'dataset_type': dataset_type,
                    'round_index': i,
                    'query': round_case['query'],
                    'status_code': resp.status_code,
                    'elapsed_seconds': round(elapsed, 3),
                    'error': data.get('error'),
                    'series_count': len(data.get('data') or []),
                    'providers': sorted({(s.get('metadata') or {}).get('source', '') for s in (data.get('data') or [])}),
                })
        else:
            payload = {'query': row['query']}
            t0 = time.time()
            resp = requests.post(base, json=payload, timeout=120)
            elapsed = time.time() - t0
            data = resp.json()
            results.append({
                'session_id': row['id'],
                'dataset_type': dataset_type,
                'round_index': 1,
                'query': row['query'],
                'status_code': resp.status_code,
                'elapsed_seconds': round(elapsed, 3),
                'error': data.get('error'),
                'series_count': len(data.get('data') or []),
                'providers': sorted({(s.get('metadata') or {}).get('source', '') for s in (data.get('data') or [])}),
            })
    return results


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description='Run or dry-run certification datasets and emit raw execution results.')
    parser.add_argument('--dataset', action='append', type=Path, required=True, help='JSONL dataset file; pass multiple times')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-sessions', type=int, default=None)
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for path in args.dataset:
        rows.extend(list(iter_jsonl(path.resolve())))
    if args.max_sessions is not None:
        rows = rows[:args.max_sessions]

    if args.dry_run:
        summary = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': 'dry_run',
            'summary': dry_run_summary(rows),
            'output': str(args.output.resolve()),
        }
        print(json.dumps(summary, indent=2))
        return 0

    results = execute_rows(rows, args.base_url)
    write_jsonl(args.output.resolve(), results)
    print(json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'execute',
        'records': len(results),
        'output': str(args.output.resolve()),
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
