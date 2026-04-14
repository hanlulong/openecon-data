#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / 'backend' / 'data' / 'indicators.db'
DEFAULT_OUTPUT = ROOT / 'validation' / 'manifests' / 'provider_distribution-latest.json'


def main() -> int:
    parser = argparse.ArgumentParser(description='Build provider distribution summary from indicators.db')
    parser.add_argument('--db-path', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    db_path = args.db_path.resolve()
    if not db_path.exists():
        raise SystemExit(f'Database not found: {db_path}')

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    rows = cur.execute('SELECT provider, COUNT(*) FROM indicators GROUP BY provider ORDER BY COUNT(*) DESC').fetchall()
    total = int(cur.execute('SELECT COUNT(*) FROM indicators').fetchone()[0])
    con.close()

    distribution = []
    cumulative = 0
    for provider, count in rows:
        count = int(count)
        cumulative += count
        distribution.append({
            'provider': str(provider),
            'count': count,
            'share': count / total if total else 0.0,
            'cumulative_count': cumulative,
            'cumulative_share': cumulative / total if total else 0.0,
        })

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'catalog_db_path': str(db_path.relative_to(ROOT)),
        'indicator_count': total,
        'distribution': distribution,
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
