#!/usr/bin/env python3
"""Load/refresh the ChinaMacro series catalog into indicators.db (idempotent).

The provider's SERIES_REGISTRY is the single source of truth; this script
upserts one row per series (provider='ChinaMacro') so FTS5 retrieval and the
indicator selector can discover them in English and Chinese. The
indicators_fts external-content table is trigger-synced — plain INSERT/UPDATE
on `indicators` keeps FTS current. Rerun after any registry change:

    python3 scripts/load_chinamacro_catalog.py
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.providers.chinamacro import DATA_DIR, SERIES_REGISTRY  # noqa: E402

DB_PATH = REPO / "backend" / "data" / "indicators.db"


def _date_ranges() -> dict[str, tuple[str, str]]:
    """Observation date ranges from the curated snapshot (best-effort)."""
    ranges: dict[str, tuple[str, str]] = {}
    obs = DATA_DIR / "observations.csv"
    if not obs.exists():
        return ranges
    with obs.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sid, d = row["series_id"], row["date"]
            lo, hi = ranges.get(sid, (d, d))
            ranges[sid] = (min(lo, d), max(hi, d))
    return ranges


def main() -> int:
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=15000")
    ranges = _date_ranges()
    today = date.today().isoformat()

    with db:
        for series in SERIES_REGISTRY:
            lo, hi = ranges.get(series["id"], ("", ""))
            name = f"{series['name_en']} ({series['name_zh']})"
            keywords = "china, chinese, 中国, macro, " + series["frequency"]
            existing = db.execute(
                "SELECT id FROM indicators WHERE provider='ChinaMacro' AND code=?",
                (series["id"],),
            ).fetchone()
            if existing:
                db.execute(
                    """UPDATE indicators SET name=?, description=?, unit=?,
                       frequency=?, coverage=?, start_date=?, end_date=?,
                       keywords=?, synonyms=?, last_updated=? WHERE id=?""",
                    (name, series["notes"], series["unit"], series["frequency"],
                     "China", lo, hi, keywords, series["synonyms"], today,
                     existing[0]),
                )
            else:
                db.execute(
                    """INSERT INTO indicators
                       (provider, code, name, description, category, unit,
                        frequency, coverage, start_date, end_date, keywords,
                        synonyms, popularity, last_updated)
                       VALUES ('ChinaMacro', ?, ?, ?, 'MACRO', ?, ?, 'China',
                               ?, ?, ?, ?, 50, ?)""",
                    (series["id"], name, series["notes"], series["unit"],
                     series["frequency"], lo, hi, keywords, series["synonyms"],
                     today),
                )
    count = db.execute(
        "SELECT COUNT(*) FROM indicators WHERE provider='ChinaMacro'"
    ).fetchone()[0]
    print(f"ChinaMacro catalog rows in indicators.db: {count}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
