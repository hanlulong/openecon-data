#!/usr/bin/env python3
"""Seed catalog `popularity` from OUR OWN query-log demand (catalog data).

The MOST-USED adjudication marker and the FTS-cut popularity term only work
where popularity exists — FRED ships it natively; IMF/WB/Eurostat/StatsCan
rows are NULL, so headline concepts there can't out-rank same-vocabulary
noise (live: 'US debt to GDP' served household debt because GGXWDG_NGDP had
no demand signal). This seeder maps 90-day resolved-code serve counts to a
0-100 popularity via log scaling and fills ONLY NULL/0 rows — provider-native
values are never lowered or overwritten. Idempotent; rerun quarterly with a
fresh aggregate (see HANDOVER maintenance loop).

Usage: python scripts/seed_popularity_from_logs.py <aggregate.json>
       aggregate.json = [{"provider": "IMF", "code": "GGXWDG_NGDP", "n": 25}, ...]
"""
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "data" / "indicators.db"

PROVIDER_ALIASES = {"WORLDBANK": "WorldBank", "STATSCAN": "StatsCan", "EUROSTAT": "Eurostat",
                    "IMF": "IMF", "FRED": "FRED", "BIS": "BIS", "OECD": "OECD"}


def popularity_from_count(n: int) -> int:
    # log scale: n=2 -> 22, 10 -> 48, 25 -> 65, 100 -> 92, 520+ -> 100
    return min(100, round(20 * math.log1p(n)))


def main() -> int:
    rows = json.loads(Path(sys.argv[1]).read_text())
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    seeded = skipped = 0
    for row in rows:
        provider = PROVIDER_ALIASES.get(str(row.get("provider") or "").upper())
        code = str(row.get("code") or "").strip()
        n = int(row.get("n") or 0)
        if not provider or not code or n < 2:
            continue
        cur = conn.execute(
            "UPDATE indicators SET popularity = ? "
            "WHERE provider = ? AND code = ? AND COALESCE(popularity, 0) = 0",
            (popularity_from_count(n), provider, code),
        )
        if cur.rowcount:
            seeded += cur.rowcount
        else:
            skipped += 1
    conn.commit()
    print(f"seeded {seeded} rows (skipped {skipped}: non-catalog phrases, "
          f"already-populated, or unknown codes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
