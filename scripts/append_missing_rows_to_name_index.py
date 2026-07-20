#!/usr/bin/env python3
"""Append indicators.db rows missing from the base NAME embedding index.

The weighted-blend pipeline (build_weighted_synonym_index.py) blends synonym
vectors onto a fixed NAME-vector base (backup_pre_synonyms/). Any row added to
indicators.db AFTER that base was built — a new provider (e.g. ChinaMacro), a
catalog refresh — is invisible to the embedding retrieval arm until it joins
the base. This tool closes that gap GENERICALLY:

  1. Load the base metadata + matrix.
  2. Find db rows whose (provider, code) is absent from the base.
  3. Embed each missing row's NAME text (same convention as the base rows —
     names only; synonyms enter via the blend layer at their own weight).
  4. Append to the matrix + metadata, writing BOTH atomically, with a dated
     safety copy of the previous base alongside.

Then run build_weighted_synonym_index.py (--ab-only first) and restart to
adopt. Idempotent: a second run finds nothing to append.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.embedding_retrieval import (  # noqa: E402
    EMBEDDING_MODEL,
    EmbeddingRetrieval,
)

IDX_DIR = ROOT / "backend" / "data" / "openai_embeddings"
BASE = IDX_DIR / "backup_pre_synonyms"


def main() -> int:
    import sqlite3

    meta_path = BASE / "indicator_metadata.json"
    npz_path = BASE / "indicator_embeddings.npz"
    meta = json.loads(meta_path.read_text())
    codes, providers = meta["codes"], meta["providers"]
    known = set(zip(providers, codes))
    embs = np.load(npz_path)["embeddings"].astype(np.float32, copy=False)
    assert embs.shape[0] == len(codes), "base matrix/metadata row mismatch"
    print(f"base: {embs.shape[0]} rows")

    conn = sqlite3.connect(str(ROOT / "backend" / "data" / "indicators.db"))
    missing = [
        (p, c, str(n or "").strip())
        for p, c, n in conn.execute("SELECT provider, code, name FROM indicators")
        if (p, c) not in known and str(n or "").strip()
    ]
    if not missing:
        print("nothing to append — base is current.")
        return 0
    by_provider: dict[str, int] = {}
    for p, _c, _n in missing:
        by_provider[p] = by_provider.get(p, 0) + 1
    print(f"appending {len(missing)} rows: {by_provider}")

    client = EmbeddingRetrieval()._get_client()
    new_vecs = []
    B = 500
    for start in range(0, len(missing), B):
        chunk = missing[start : start + B]
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL, input=[n[:200] for _p, _c, n in chunk]
        )
        for d in resp.data:
            v = np.asarray(d.embedding, dtype=np.float32)
            v /= np.linalg.norm(v) or 1
            new_vecs.append(v)
        print(f"  embedded {min(start + B, len(missing))}/{len(missing)}", flush=True)

    safety = IDX_DIR / f"backup_pre_synonyms_before_{date.today().isoformat()}"
    if not safety.exists():
        shutil.copytree(BASE, safety)
        print(f"safety copy -> {safety}")

    appended = np.vstack([embs, np.stack(new_vecs)])
    for p, c, _n in missing:
        providers.append(p)
        codes.append(c)
    # NOTE: np.savez_compressed APPENDS ".npz" when the target doesn't end in
    # it — the temp name must already end in .npz or the atomic replace source
    # won't exist.
    tmp_npz = npz_path.with_name("indicator_embeddings.new.npz")
    np.savez_compressed(tmp_npz, embeddings=appended)
    tmp_npz.replace(npz_path)
    tmp_meta = meta_path.with_suffix(".json.tmp")
    tmp_meta.write_text(json.dumps(meta))
    tmp_meta.replace(meta_path)
    print(f"base is now {appended.shape[0]} rows. Next: "
          "python3 scripts/build_weighted_synonym_index.py --ab-only, review, "
          "then run without --ab-only and restart to adopt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
