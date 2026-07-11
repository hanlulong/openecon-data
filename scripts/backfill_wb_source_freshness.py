#!/usr/bin/env python3
"""Backfill World Bank rows' last_updated from their SOURCE database.

Every WB row has NULL end_date/last_updated (the /v2/indicator metadata
endpoint carries no dates), so staleness ranking was a no-op for WB — a
2011-era "Country Partnership Strategy for India" population series could
out-rank the continuously-updated WDI SP.POP.TOTL (observed live).

The /v2/source endpoint returns per-DATABASE `lastupdated` in one call, and
every row's raw_metadata carries its source id — so one request + one
transaction gives all 29k rows a general freshness signal the ranker can use.

Usage:
    python scripts/backfill_wb_source_freshness.py [--dry-run]
"""

import argparse
import json
import sqlite3
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "data" / "indicators.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    resp = httpx.get(
        "https://api.worldbank.org/v2/source?format=json&per_page=200", timeout=30
    )
    resp.raise_for_status()
    sources = resp.json()[1]
    updated_by_id = {
        str(s["id"]): str(s.get("lastupdated") or "").strip()
        for s in sources
        if s.get("lastupdated")
    }
    print(f"{len(updated_by_id)} sources with lastupdated")

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")

    total = 0
    for source_id, last_updated in updated_by_id.items():
        if args.dry_run:
            n = conn.execute(
                "SELECT COUNT(*) FROM indicators WHERE provider='WorldBank' "
                "AND json_extract(raw_metadata, '$.source.id') = ?",
                (source_id,),
            ).fetchone()[0]
        else:
            cur = conn.execute(
                "UPDATE indicators SET last_updated=? WHERE provider='WorldBank' "
                "AND json_extract(raw_metadata, '$.source.id') = ?",
                (last_updated, source_id),
            )
            n = cur.rowcount
        total += n
    if not args.dry_run:
        conn.commit()
    print(f"DONE: {total} WorldBank rows stamped with source lastupdated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
