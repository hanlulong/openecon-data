#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / 'backend' / 'data' / 'indicators.db'
DEFAULT_OUTPUT_DIR = ROOT / 'validation_private' / 'frozen' / 'catalog_snapshot'


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_counts(cur: sqlite3.Cursor, sql: str) -> OrderedDict[str, int]:
    rows = cur.execute(sql).fetchall()
    return OrderedDict((str(key), int(value)) for key, value in rows)


def git_sha(root: Path) -> str:
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    except Exception:
        return 'UNKNOWN'


def main() -> int:
    parser = argparse.ArgumentParser(description='Export a frozen catalog snapshot manifest.')
    parser.add_argument('--db-path', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output', type=Path, default=None,
                        help='Explicit output path. Defaults to validation_private/frozen/catalog_snapshot/catalog_snapshot-<date>.json')
    parser.add_argument('--sampling-seed', type=int, default=20260414)
    args = parser.parse_args()

    db_path: Path = args.db_path.resolve()
    if not db_path.exists():
        raise SystemExit(f'Database not found: {db_path}')

    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    output_path = args.output.resolve() if args.output else (DEFAULT_OUTPUT_DIR / f'catalog_snapshot-{snapshot_date}.json')
    output_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    total_indicators = int(cur.execute('SELECT COUNT(*) FROM indicators').fetchone()[0])
    provider_counts = fetch_counts(cur, 'SELECT provider, COUNT(*) FROM indicators GROUP BY provider ORDER BY COUNT(*) DESC')
    category_counts = fetch_counts(cur, 'SELECT COALESCE(NULLIF(category, ""), "<EMPTY>"), COUNT(*) FROM indicators GROUP BY COALESCE(NULLIF(category, ""), "<EMPTY>") ORDER BY COUNT(*) DESC LIMIT 50')
    frequency_counts = fetch_counts(cur, 'SELECT COALESCE(frequency, "<NULL>"), COUNT(*) FROM indicators GROUP BY COALESCE(frequency, "<NULL>") ORDER BY COUNT(*) DESC LIMIT 50')
    coverage_counts = fetch_counts(cur, 'SELECT COALESCE(coverage, "<NULL>"), COUNT(*) FROM indicators GROUP BY COALESCE(coverage, "<NULL>") ORDER BY COUNT(*) DESC LIMIT 50')
    con.close()

    manifest: dict[str, Any] = {
        'snapshot_date': snapshot_date,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'git_sha': git_sha(ROOT),
        'catalog_db_path': str(db_path.relative_to(ROOT)),
        'catalog_db_sha256': sha256_file(db_path),
        'indicator_count': total_indicators,
        'provider_counts': provider_counts,
        'category_counts_top50': category_counts,
        'frequency_counts_top50': frequency_counts,
        'coverage_counts_top50': coverage_counts,
        'sampling_seed': args.sampling_seed,
    }

    output_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'output': str(output_path),
        'indicator_count': total_indicators,
        'provider_counts': provider_counts,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
