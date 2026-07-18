#!/usr/bin/env python3
"""Emit the IMF catalog codes the runtime can actually SERVE, one per line.

Feeds enrich_indicator_synonyms.py --codes-file: enriching unservable IMF
codes only polishes options the menu-supportability filter drops anyway
(112,637 of 115K rows are category='INDICATOR' and mostly unservable —
category scoping is useless; the dispatch predicate is the real gate).
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.utils.imf_supportability import (  # noqa: E402
    imf_catalog_surface_supportability_reason,
)

conn = sqlite3.connect(str(ROOT / "backend/data/indicators.db"))
supported = 0
for code, name, category in conn.execute(
    "SELECT code, name, COALESCE(category,'') FROM indicators WHERE provider='IMF'"
):
    if imf_catalog_surface_supportability_reason(
        code=code, name=name or "", category=category
    ) is None:
        print(code)
        supported += 1
print(f"# {supported} supported IMF codes", file=sys.stderr)
