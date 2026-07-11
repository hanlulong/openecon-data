#!/usr/bin/env python3
"""Append StatsCan cube MEMBER names to indicator synonyms.

StatsCan cube titles name the SURVEY, not the metrics: "Labour force
characteristics, monthly, seasonally adjusted" contains the members
"Unemployment rate", "Participation rate", "Employment" — the terms users
actually search — only inside its dimension metadata. Title-based synonym
enrichment therefore can't rank the workhorse cubes for metric queries.

This script reads the local metadata cache (backend/data/
statscan_metadata_cache.json, the 167 major products) and appends the
member names of CONTENT dimensions to each product's synonyms. Universal
cross-cutting axes (geography, gender, age, statistics, adjustment, data
type) are excluded structurally — their members (province names, "Men+",
"Estimate") are not metric vocabulary.

Idempotent: only appends terms not already present.

Usage:
    python scripts/enrich_statscan_member_synonyms.py [--dry-run]
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "backend" / "data" / "indicators.db"
CACHE_PATH = ROOT / "backend" / "data" / "statscan_metadata_cache.json"

# Universal cross-cutting axes present on most cubes; their members are
# slicers, not metrics.
_EXCLUDED_DIM_PATTERNS = re.compile(
    r"geograph|gender|\bsex\b|age group|statistics|data type|"
    r"seasonal adjustment|adjustment|reference period|type of student",
    re.IGNORECASE,
)

_MAX_MEMBER_TERMS = 25
_MAX_SYNONYMS_LEN = 2000


def member_terms(meta: dict) -> list[str]:
    terms: list[str] = []
    for dim in meta.get("dimension") or []:
        dim_name = str(dim.get("dimensionNameEn") or "")
        if _EXCLUDED_DIM_PATTERNS.search(dim_name):
            continue
        for member in dim.get("member") or []:
            name = str(member.get("memberNameEn") or "").strip()
            if 2 < len(name) <= 60 and name.lower() not in ("total", "all"):
                terms.append(name.lower())
            if len(terms) >= _MAX_MEMBER_TERMS:
                return terms
    return terms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cache = json.loads(CACHE_PATH.read_text())
    products = cache.get("products") or {}
    conn = sqlite3.connect(str(DB_PATH))

    updated = skipped = 0
    for product_id, meta in products.items():
        if not isinstance(meta, dict):
            continue
        terms = member_terms(meta)
        if not terms:
            skipped += 1
            continue
        row = conn.execute(
            "SELECT synonyms FROM indicators WHERE provider='StatsCan' AND code=?",
            (str(product_id),),
        ).fetchone()
        if row is None:
            skipped += 1
            continue
        existing = str(row[0] or "")
        existing_l = existing.lower()
        new_terms = [t for t in terms if t not in existing_l]
        if not new_terms:
            skipped += 1
            continue
        combined = (existing + ", " if existing else "") + ", ".join(new_terms)
        combined = combined[:_MAX_SYNONYMS_LEN]
        if args.dry_run:
            print(f"{product_id}: +{len(new_terms)} terms: {', '.join(new_terms[:6])}...")
        else:
            conn.execute(
                "UPDATE indicators SET synonyms=? WHERE provider='StatsCan' AND code=?",
                (combined, str(product_id)),
            )
        updated += 1

    if not args.dry_run:
        conn.commit()
    print(f"DONE: {updated} products updated, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
