#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'frozen' / 'split_manifest-latest.json'
DEFAULT_SPLITS = 'dev=0.5,shadow=0.2,cert_holdout=0.2,prod_replay=0.1'


def stable_ratio(*parts: object) -> float:
    digest = hashlib.sha256('||'.join(str(p) for p in parts).encode('utf-8')).hexdigest()
    return int(digest[:16], 16) / float(16**16 - 1)


def parse_splits(spec: str) -> list[tuple[str, float]]:
    pieces = []
    total = 0.0
    for part in spec.split(','):
        name, raw = part.split('=', 1)
        frac = float(raw)
        total += frac
        pieces.append((name.strip(), frac))
    if abs(total - 1.0) > 1e-9:
        raise SystemExit(f'Split fractions must sum to 1.0, got {total}')
    return pieces


def assign_split(session_id: str, snapshot_id: str, seed: int, splits: list[tuple[str, float]]) -> str:
    value = stable_ratio(snapshot_id, session_id, seed)
    cumulative = 0.0
    for name, frac in splits:
        cumulative += frac
        if value <= cumulative:
            return name
    return splits[-1][0]


def iter_jsonl(path: Path):
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description='Deterministically assign candidate sessions into non-overlapping validation splits.')
    parser.add_argument('--dataset', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--splits', default=DEFAULT_SPLITS)
    parser.add_argument('--seed', type=int, default=20260414)
    parser.add_argument('--write-split-files', action='store_true')
    parser.add_argument('--split-root', type=Path, default=ROOT / 'validation_private' / 'datasets')
    args = parser.parse_args()

    split_spec = parse_splits(args.splits)
    all_rows: list[dict[str, Any]] = []
    for dataset in args.dataset:
        all_rows.extend(list(iter_jsonl(dataset.resolve())))

    if not all_rows:
        raise SystemExit('No rows loaded')

    snapshot_ids = {str((row.get('provenance') or {}).get('snapshot_id') or '') for row in all_rows}
    if len(snapshot_ids) != 1:
        raise SystemExit(f'Expected exactly one snapshot_id across rows, got {sorted(snapshot_ids)}')
    snapshot_id = next(iter(snapshot_ids))

    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    split_counts: Counter[str] = Counter()
    split_type_counts: dict[str, Counter[str]] = defaultdict(Counter)
    split_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in all_rows:
        session_id = str(row.get('id') or '')
        if session_id in seen_ids:
            duplicate_ids.add(session_id)
        seen_ids.add(session_id)
        split_name = assign_split(session_id, snapshot_id, args.seed, split_spec)
        row.setdefault('provenance', {})['holdout_split'] = split_name
        row['dataset_tier'] = split_name
        split_counts[split_name] += 1
        kind = 'multiround' if 'rounds' in row else 'ambiguity' if 'expected_behavior' in row else 'direct'
        split_type_counts[split_name][kind] += 1
        split_rows[split_name].append(row)

    if args.write_split_files:
        for split_name, rows in split_rows.items():
            out = args.split_root / split_name / f'{split_name}-sessions.jsonl'
            write_jsonl(out, rows)

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'snapshot_id': snapshot_id,
        'seed': args.seed,
        'splits': [{'name': name, 'fraction': frac} for name, frac in split_spec],
        'input_datasets': [str(p.resolve()) for p in args.dataset],
        'total_rows': len(all_rows),
        'duplicate_ids': sorted(duplicate_ids),
        'duplicate_id_count': len(duplicate_ids),
        'split_counts': dict(split_counts),
        'split_type_counts': {k: dict(v) for k, v in split_type_counts.items()},
        'wrote_split_files': bool(args.write_split_files),
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
