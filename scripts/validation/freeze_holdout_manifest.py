#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'frozen' / 'holdout_manifest-latest.json'


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def count_lines(path: Path) -> int:
    with path.open('r', encoding='utf-8') as f:
        return sum(1 for _ in f)


def main() -> int:
    parser = argparse.ArgumentParser(description='Freeze a holdout manifest over generated dataset files.')
    parser.add_argument('--snapshot-manifest', type=Path, required=True)
    parser.add_argument('--dataset', action='append', type=Path, required=True, help='JSONL dataset file; pass multiple times')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--label', default='certification_holdout')
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot_manifest.resolve().read_text(encoding='utf-8'))
    dataset_entries = []
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    total_rows = 0

    for dataset_path in [p.resolve() for p in args.dataset]:
        rows = []
        with dataset_path.open('r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                rows.append(row)
                row_id = str(row.get('id') or '')
                if row_id in seen_ids:
                    duplicate_ids.append(row_id)
                seen_ids.add(row_id)
        total_rows += len(rows)
        dataset_entries.append({
            'path': str(dataset_path),
            'sha256': sha256_file(dataset_path),
            'row_count': len(rows),
        })

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'label': args.label,
        'snapshot_manifest_path': str(args.snapshot_manifest.resolve()),
        'snapshot_manifest_sha256': sha256_file(args.snapshot_manifest.resolve()),
        'snapshot_id': f"{snapshot['snapshot_date']}:{str(snapshot['git_sha'])[:8]}:{snapshot['indicator_count']}",
        'datasets': dataset_entries,
        'total_rows': total_rows,
        'duplicate_ids': sorted(set(duplicate_ids)),
        'duplicate_id_count': len(sorted(set(duplicate_ids))),
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
