#!/usr/bin/env python3
"""Backfill Eurostat rows' end_date/last_updated from Eurostat's table of contents.

Every Eurostat row in backend/data/indicators.db has NULL end_date and
last_updated, so neither the selector LLM ("last data: ..." annotation) nor
the staleness ranker can see that a dataset is CLOSED. Live consequence: the
legacy HICP datasets (prc_hicp_manr/_midx/...) froze at 2025-12 when Eurostat
moved HICP to ECOICOP ver.2 (prc_hicp_minr, data to 2026-10), yet they still
looked as current as any live dataset.

Eurostat's TOC (one ~2 MB TSV download) carries, per dataset/table code,
"last update of data" and "data end". One request + one transaction gives
every EXISTING Eurostat row a general freshness signal. Nothing else is
touched: no inserts, no other columns, no other providers.

Mapping (structural only):
  * row code == TOC code (case-insensitive)                -> exact match
  * row code "<base>$DV_nnnn" (Eurostat derived view)      -> inherits <base>
  * end_date     = TOC "data end" verbatim, an SDMX period ("2025-12",
                   "2026-Q2", "2025") so its frequency stays visible
  * last_updated = TOC "last update of data" as ISO YYYY-MM-DD

Usage:
    python scripts/backfill_eurostat_toc_freshness.py                 # dry run (default)
    python scripts/backfill_eurostat_toc_freshness.py --toc-file toc.txt
    python scripts/backfill_eurostat_toc_freshness.py --apply --backup /path/indicators.db.bak

--apply refuses to run without --backup; the DB is copied with the sqlite3
online-backup API before the single UPDATE transaction.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "data" / "indicators.db"
TOC_URL = "https://ec.europa.eu/eurostat/api/dissemination/catalogue/toc/txt?lang=en"
PROVIDER = "Eurostat"

# SDMX reporting-period shapes Eurostat uses for "data end".
_PERIOD_RE = re.compile(r"\d{4}(?:-(?:\d{2}|Q[1-4]|S[12]|W\d{2}|\d{2}-\d{2}))?")
_DATA_ROW_TYPES = {"dataset", "table"}


def _iso_date(value: str) -> Optional[str]:
    """Eurostat TOC dates are dd.mm.yyyy; return ISO YYYY-MM-DD or None."""
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _period(value: str) -> Optional[str]:
    text = str(value or "").strip()
    return text if _PERIOD_RE.fullmatch(text) else None


def parse_toc(text: str) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Return {CODE: (end_date, last_updated)} for dataset/table TOC rows.

    The TOC lists a code once per folder it appears in; duplicates carry the
    same values, and on any disagreement the most recently updated wins.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for row in reader:
        code = str(row.get("code") or "").strip().upper()
        if not code or str(row.get("type") or "").strip().lower() not in _DATA_ROW_TYPES:
            continue
        end_date = _period(row.get("data end") or "")
        last_updated = _iso_date(row.get("last update of data") or "")
        if end_date is None and last_updated is None:
            continue
        previous = out.get(code)
        if previous is None or (last_updated or "") > (previous[1] or ""):
            out[code] = (end_date, last_updated)
    return out


def plan_updates(
    rows: Iterable[Tuple[int, str, Optional[str], Optional[str]]],
    toc: Dict[str, Tuple[Optional[str], Optional[str]]],
) -> Tuple[list, Dict[str, int]]:
    """Match DB rows (id, code, end_date, last_updated) to TOC freshness."""
    updates = []
    summary = {
        "rows": 0,
        "matched_exact": 0,
        "matched_derived_view": 0,
        "unmatched": 0,
        "unchanged": 0,
        "to_update": 0,
    }
    for row_id, code, cur_end, cur_updated in rows:
        summary["rows"] += 1
        key = str(code or "").strip().upper()
        hit = toc.get(key)
        if hit is not None:
            summary["matched_exact"] += 1
        else:
            base = key.split("$", 1)[0]
            hit = toc.get(base) if base != key else None
            if hit is None:
                summary["unmatched"] += 1
                continue
            summary["matched_derived_view"] += 1
        end_date, last_updated = hit
        new_end = end_date or cur_end
        new_updated = last_updated or cur_updated
        if (new_end, new_updated) == (cur_end, cur_updated):
            summary["unchanged"] += 1
            continue
        updates.append((new_end, new_updated, row_id))
    summary["to_update"] = len(updates)
    return updates, summary


def _fetch_toc() -> str:
    import httpx

    resp = httpx.get(TOC_URL, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--apply", action="store_true", help="write end_date/last_updated")
    ap.add_argument("--backup", type=Path, help="required with --apply: sqlite backup destination")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--toc-file", type=Path, help="use a downloaded TOC TSV instead of fetching")
    ap.add_argument("--examples", type=int, default=8, help="sample planned updates to print")
    args = ap.parse_args(argv)

    if args.apply and not args.backup:
        print("REFUSING --apply without --backup <path>", file=sys.stderr)
        return 2
    if args.apply and args.backup.resolve() == args.db.resolve():
        print("REFUSING: --backup must differ from --db", file=sys.stderr)
        return 2
    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2

    text = args.toc_file.read_text(encoding="utf-8") if args.toc_file else _fetch_toc()
    toc = parse_toc(text)
    print(f"TOC: {len(toc)} dataset/table codes with freshness")

    if args.apply:
        conn = sqlite3.connect(str(args.db), timeout=60)
    else:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        rows = conn.execute(
            "SELECT id, code, end_date, last_updated FROM indicators WHERE provider = ?",
            (PROVIDER,),
        ).fetchall()
        updates, summary = plan_updates(rows, toc)
        for key, value in summary.items():
            print(f"  {key:>22}: {value}")
        by_id = {row_id: code for row_id, code, _, _ in rows}
        for new_end, new_updated, row_id in updates[: max(0, args.examples)]:
            print(f"    {by_id[row_id]:<32} end_date={new_end} last_updated={new_updated}")

        if not args.apply:
            print("DRY RUN: no changes written (use --apply --backup PATH)")
            return 0

        args.backup.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(args.backup))
        try:
            conn.backup(dest)
        finally:
            dest.close()
        print(f"Backup written: {args.backup}")

        with conn:  # single transaction; rolls back on any error
            conn.executemany(
                "UPDATE indicators SET end_date = ?, last_updated = ? "
                "WHERE id = ? AND provider = ?",
                [(end, upd, row_id, PROVIDER) for end, upd, row_id in updates],
            )
        print(f"DONE: {len(updates)} {PROVIDER} rows stamped from the TOC")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
