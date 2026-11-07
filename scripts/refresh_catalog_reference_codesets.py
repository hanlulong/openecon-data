#!/usr/bin/env python3
"""Refresh the official provider codesets the catalog read path decodes with.

OFFLINE ONLY. The request path never calls these endpoints; it loads the
committed JSON snapshots under ``backend/data/metadata/reference/``. Run this
script when a provider publishes a new codeset member, review the diff, and
commit the regenerated files.

Two reference tables are produced, both copied verbatim from the provider's
own published metadata (no hand-typed entries):

* ``statscan_frequency_codeset.json`` -- the ``frequency`` section of
  Statistics Canada's WDS ``getCodeSets`` response.  The indicator catalog
  stores StatsCan's raw numeric ``frequencyCode`` ("6", "12", ...), so this is
  what turns those codes into a frequency the selector can compare.
* ``worldbank_source_concepts.json`` -- for every World Bank source id present
  in the indicator catalog, the dimension (concept) list published by
  ``/v2/sources/{id}/concepts``.  A source whose dimensions include a
  ``Version`` concept is a vintage archive: each series is split by release
  vintage and the current-data endpoint cannot serve it.

Usage::

    python3 scripts/refresh_catalog_reference_codesets.py            # both
    python3 scripts/refresh_catalog_reference_codesets.py --only statscan
    python3 scripts/refresh_catalog_reference_codesets.py --only worldbank
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = ROOT / "backend" / "data" / "metadata" / "reference"
DB_PATH = ROOT / "backend" / "data" / "indicators.db"

STATSCAN_CODESETS_URL = "https://www150.statcan.gc.ca/t1/wds/rest/getCodeSets"
WB_SOURCE_CONCEPTS_URL = "https://api.worldbank.org/v2/sources/{source_id}/concepts?format=json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def refresh_statscan(client: httpx.Client) -> None:
    resp = client.get(STATSCAN_CODESETS_URL, timeout=60.0)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "SUCCESS":
        raise RuntimeError(f"getCodeSets returned status={data.get('status')!r}")
    rows = (data.get("object") or {}).get("frequency") or []
    codes = sorted(
        (
            {
                "frequencyCode": int(row["frequencyCode"]),
                "frequencyDescEn": str(row.get("frequencyDescEn") or ""),
                "frequencyDescFr": str(row.get("frequencyDescFr") or ""),
            }
            for row in rows
            if row.get("frequencyCode") is not None
        ),
        key=lambda row: row["frequencyCode"],
    )
    if not codes:
        raise RuntimeError("getCodeSets returned an empty frequency codeset")
    _write(
        REFERENCE_DIR / "statscan_frequency_codeset.json",
        {
            "source_url": STATSCAN_CODESETS_URL,
            "codeset": "frequency",
            "retrieved_at": _now(),
            "frequency": codes,
        },
    )


def _catalog_worldbank_source_ids() -> list[str]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        ids: set[str] = set()
        for (raw,) in conn.execute(
            "SELECT raw_metadata FROM indicators WHERE provider = 'WorldBank'"
        ):
            try:
                source = (json.loads(raw or "{}") or {}).get("source") or {}
            except (TypeError, ValueError):
                continue
            if isinstance(source, dict) and str(source.get("id") or "").strip():
                ids.add(str(source["id"]).strip())
        return sorted(ids, key=lambda value: (len(value), value))
    finally:
        conn.close()


def refresh_worldbank(client: httpx.Client) -> None:
    sources: dict[str, dict] = {}
    for source_id in _catalog_worldbank_source_ids():
        resp = client.get(WB_SOURCE_CONCEPTS_URL.format(source_id=source_id), timeout=60.0)
        resp.raise_for_status()
        payload = resp.json()
        entries = payload.get("source") if isinstance(payload, dict) else None
        if not entries:
            print(f"  source {source_id}: no concept list published; skipped", file=sys.stderr)
            continue
        entry = entries[0]
        sources[source_id] = {
            "name": str(entry.get("name") or "").strip(),
            "concepts": [str(c.get("id") or "") for c in entry.get("concept") or []],
        }
        time.sleep(0.2)  # be polite to the public API
    if not sources:
        raise RuntimeError("no World Bank source concept lists retrieved")
    _write(
        REFERENCE_DIR / "worldbank_source_concepts.json",
        {
            "source_url": WB_SOURCE_CONCEPTS_URL,
            "retrieved_at": _now(),
            "sources": sources,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=("statscan", "worldbank"))
    args = parser.parse_args()
    with httpx.Client(headers={"User-Agent": "openecon-reference-refresh"}) as client:
        if args.only in (None, "statscan"):
            refresh_statscan(client)
        if args.only in (None, "worldbank"):
            refresh_worldbank(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
