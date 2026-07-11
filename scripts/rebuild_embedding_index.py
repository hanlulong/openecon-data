#!/usr/bin/env python3
"""Rebuild the OpenAI embedding index with synonym-aware text, then A/B it.

The deliberate op: embeds name + synonyms for all ~330K indicators (the old
index embedded names only, so user vocabulary like "unemployment rate" could
never reach survey-titled StatsCan cubes, and "India population" missed
SP.POP.TOTL). The running service keeps its mmap'd old index until the next
restart, so this script is safe to run against production files:

  1. captures OLD top-10 retrieval for a fixed probe set (from the backup),
  2. rebuilds the index in place (atomic writes),
  3. prints OLD vs NEW top-10 side by side for human review.

DO NOT restart the backend until the A/B output looks sane.
Rollback: restore backend/data/openai_embeddings/backup_pre_synonyms/*.

Usage: python scripts/rebuild_embedding_index.py
"""

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.embedding_retrieval import (  # noqa: E402
    EMBEDDING_MODEL,
    EmbeddingRetrieval,
)

BACKUP = ROOT / "backend/data/openai_embeddings/backup_pre_synonyms"
NEW_NPZ = ROOT / "backend/data/openai_embeddings/indicator_embeddings.npz"
NEW_META = ROOT / "backend/data/openai_embeddings/indicator_metadata.json"

PROBES = [
    ("unemployment rate", "StatsCan"),
    ("unemployment rate canada", "StatsCan"),
    ("India population", "WorldBank"),
    ("population", "WorldBank"),
    ("US GDP", "FRED"),
    ("unemployment rate", "FRED"),
    ("M2 money supply", "FRED"),
    ("nonfarm payrolls", "FRED"),
    ("inflation rate", "Eurostat"),
    ("GDP growth", "WorldBank"),
]


def _load(npz_path, meta_path):
    embs = np.load(npz_path)["embeddings"].astype(np.float32, copy=False)
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms[norms == 0] = 1
    embs /= norms  # in place — a divided copy doubles peak RSS
    meta = json.loads(Path(meta_path).read_text())
    return embs, meta["codes"], meta["providers"]


def _top10(embs, codes, providers, qvec, provider):
    mask = np.array([p == provider for p in providers])
    if not mask.any():
        return []
    scores = embs[mask] @ qvec
    idx = np.argsort(-scores)[:10]
    sub_codes = [c for c, m in zip(codes, mask) if m]
    return [sub_codes[i] for i in idx]


def main() -> int:
    retrieval = EmbeddingRetrieval()
    client = retrieval._get_client()

    # Embed probe queries once (same model both sides).
    probe_vecs = {}
    for q, _prov in PROBES:
        if q not in probe_vecs:
            resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[q])
            v = np.array(resp.data[0].embedding, dtype=np.float32)
            probe_vecs[q] = v / (np.linalg.norm(v) or 1)

    old_embs, old_codes, old_providers = _load(
        BACKUP / "indicator_embeddings.npz", BACKUP / "indicator_metadata.json"
    )
    old_results = {
        (q, p): _top10(old_embs, old_codes, old_providers, probe_vecs[q], p)
        for q, p in PROBES
    }
    del old_embs

    print("=== REBUILDING (name + synonyms) — ~660 batches ===", flush=True)
    retrieval.build_index(batch_size=500)

    new_embs, new_codes, new_providers = _load(NEW_NPZ, NEW_META)
    print("\n=== A/B: OLD vs NEW top-10 ===")
    for q, p in PROBES:
        new = _top10(new_embs, new_codes, new_providers, probe_vecs[q], p)
        old = old_results[(q, p)]
        overlap = len(set(old) & set(new))
        print(f"\n[{p}] {q!r}  (overlap {overlap}/10)")
        print("  OLD:", old[:6])
        print("  NEW:", new[:6])
    print("\nDone. Review before restarting the backend.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
