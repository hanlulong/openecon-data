#!/usr/bin/env python3
"""ChinaMacro curated-snapshot maintenance tool.

The ChinaMacro provider serves LIVE data (EastMoney datacenter / MOFCOM) with
a curated CSV snapshot under backend/data/chinamacro/ as the fallback tier.
This script maintains that snapshot. Modes:

  --seed         Fetch full history from every live source and (re)write
                 observations.csv + catalog.csv + AS_OF. Run quarterly by the
                 maintenance loop, or after verifying a live-source change.
  --verify-live  Fetch live values and DIFF them against the current snapshot
                 for overlapping dates (the honesty cross-check): any mismatch
                 means the upstream restated data OR a parser drifted — read
                 before re-seeding.
  --staleness    Report series whose latest snapshot observation is older than
                 its frequency + publication lag allows (default mode).

No network in tests — this is an operator tool. Uses the provider's own
registry and fetchers so a passing --seed exercises the production code path.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.providers.chinamacro import (  # noqa: E402
    DATA_DIR,
    SERIES_REGISTRY,
    ChinaMacroProvider,
)

# Allowed staleness per frequency: release lag + safety margin.
STALENESS_BUDGET = {
    "daily": timedelta(days=14),
    "monthly": timedelta(days=75),
    "quarterly": timedelta(days=140),
}
# The MOFCOM republication path structurally lags the PBoC release.
EXTRA_LAG_DAYS = {"CN_SF_INCREMENT": 60}


async def _collect_live() -> dict[str, list[dict]]:
    provider = ChinaMacroProvider()
    out: dict[str, list[dict]] = {}
    for series in SERIES_REGISTRY:
        try:
            out[series["id"]] = await provider._live_observations(series)
            print(f"  {series['id']}: {len(out[series['id']])} observations "
                  f"(latest {out[series['id']][-1]['date']})")
        except Exception as exc:  # noqa: BLE001 — operator tool, report and go on
            print(f"  {series['id']}: LIVE FETCH FAILED — {exc}")
    return out


def cmd_seed() -> int:
    print("Fetching full history from live sources…")
    live = asyncio.run(_collect_live())
    if not live:
        print("Nothing fetched; snapshot left untouched.")
        return 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    obs_path = DATA_DIR / "observations.csv"
    with obs_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["series_id", "date", "value"])
        for series_id in sorted(live):
            for point in live[series_id]:
                writer.writerow([series_id, point["date"], point["value"]])

    cat_path = DATA_DIR / "catalog.csv"
    with cat_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "series_id", "name_en", "name_zh", "unit", "frequency",
            "source_org", "source_kind", "notes",
        ])
        for series in SERIES_REGISTRY:
            writer.writerow([
                series["id"], series["name_en"], series["name_zh"],
                series["unit"], series["frequency"], series["source_org"],
                series["source"]["kind"], series["notes"],
            ])

    (DATA_DIR / "AS_OF").write_text(date.today().isoformat() + "\n")
    total = sum(len(v) for v in live.values())
    print(f"Snapshot written: {total} observations across {len(live)} series -> {obs_path}")
    return 0


def _load_snapshot() -> dict[str, list[dict]]:
    obs_path = DATA_DIR / "observations.csv"
    if not obs_path.exists():
        print(f"No snapshot at {obs_path}; run --seed first.")
        raise SystemExit(1)
    by_series: dict[str, list[dict]] = {}
    with obs_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            by_series.setdefault(row["series_id"], []).append(
                {"date": row["date"], "value": float(row["value"])}
            )
    for points in by_series.values():
        points.sort(key=lambda p: p["date"])
    return by_series


def cmd_verify_live() -> int:
    snapshot = _load_snapshot()
    print("Fetching live values for cross-check…")
    live = asyncio.run(_collect_live())
    mismatches = 0
    for series_id, live_points in live.items():
        snap = {p["date"]: p["value"] for p in snapshot.get(series_id, [])}
        for point in live_points:
            snap_value = snap.get(point["date"])
            if snap_value is None:
                continue
            if abs(float(point["value"]) - snap_value) > max(1e-9, abs(snap_value) * 1e-6):
                mismatches += 1
                print(f"  MISMATCH {series_id} {point['date']}: "
                      f"snapshot={snap_value} live={point['value']}")
    print(f"verify-live: {mismatches} mismatches" if mismatches
          else "verify-live: all overlapping observations agree")
    return 1 if mismatches else 0


def cmd_staleness() -> int:
    snapshot = _load_snapshot()
    today = datetime.now().date()
    stale = 0
    for series in SERIES_REGISTRY:
        points = snapshot.get(series["id"])
        if not points:
            print(f"  {series['id']}: MISSING from snapshot")
            stale += 1
            continue
        latest = datetime.strptime(points[-1]["date"], "%Y-%m-%d").date()
        budget = STALENESS_BUDGET.get(series["frequency"], timedelta(days=90))
        budget += timedelta(days=EXTRA_LAG_DAYS.get(series["id"], 0))
        status = "STALE" if today - latest > budget else "ok"
        if status == "STALE":
            stale += 1
        print(f"  {series['id']:>16} latest={latest} ({series['frequency']}) {status}")
    print(f"staleness: {stale} series need attention" if stale else "staleness: all fresh")
    return 1 if stale else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--seed", action="store_true")
    group.add_argument("--verify-live", action="store_true")
    group.add_argument("--staleness", action="store_true")
    args = parser.parse_args()
    if args.seed:
        return cmd_seed()
    if args.verify_live:
        return cmd_verify_live()
    return cmd_staleness()


if __name__ == "__main__":
    sys.exit(main())
