#!/usr/bin/env python3
"""Blend synonym vectors into the embedding index WITHOUT diluting names.

The concatenated-text rebuild (name + synonyms as one string) improved
vocabulary recall but crowded flagships out: near-duplicate series sharing
enriched synonyms drifted toward one another, pushing UNRATE/GDPC1 out of
their queries' top-6 (see the rolled-back experimental index).

This variant keeps the EXISTING name embeddings (the production index) and
blends a separately-embedded synonyms vector only for the ~11K rows that
have meaningful synonyms:

    final = normalize(W_NAME * name_vec + W_SYN * syn_vec)

Name-dominant weighting preserves each series' distinct identity; the
synonym component adds user-vocabulary pull. Rows without synonyms are
byte-identical to production. Prints the same OLD/NEW A/B as the full
rebuild harness. DO NOT restart the backend until the A/B looks clean.

Usage:
    python scripts/build_weighted_synonym_index.py                 # 0.75/0.25, writes artifacts
    python scripts/build_weighted_synonym_index.py --w-syn 0.30 --ab-only
        # experiment with other weights WITHOUT touching production artifacts
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.embedding_retrieval import (  # noqa: E402
    EMBEDDING_MODEL,
    EmbeddingRetrieval,
    _write_normalized_npy_atomic,
)

IDX_DIR = ROOT / "backend/data/openai_embeddings"
BACKUP = IDX_DIR / "backup_pre_synonyms"
NPZ = IDX_DIR / "indicator_embeddings.npz"
META = IDX_DIR / "indicator_metadata.json"

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


def _norm_rows(m):
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return m / n


def _top10(embs, codes, providers, qvec, provider):
    mask = np.array([p == provider for p in providers])
    scores = embs[mask] @ qvec
    idx = np.argsort(-scores)[:10]
    sub = [c for c, m in zip(codes, mask) if m]
    return [sub[i] for i in idx]


def main() -> int:
    import sqlite3

    ap = argparse.ArgumentParser()
    ap.add_argument("--w-syn", type=float, default=0.25,
                    help="synonym-vector weight (name weight = 1 - w_syn)")
    ap.add_argument("--ab-only", action="store_true",
                    help="print the A/B comparison but do NOT write artifacts "
                         "(safe while a restart/deploy is pending)")
    args = ap.parse_args()
    w_syn = args.w_syn
    if not 0.0 < w_syn < 0.5:
        print(f"refusing w_syn={w_syn}: must stay name-dominant (0 < w_syn < 0.5), "
              "the concatenation experiment showed synonym-dominant vectors crowd "
              "flagships out")
        return 1
    w_name = 1.0 - w_syn

    meta = json.loads((BACKUP / "indicator_metadata.json").read_text())
    codes, providers = meta["codes"], meta["providers"]
    name_embs = np.load(BACKUP / "indicator_embeddings.npz")["embeddings"].astype(
        np.float32, copy=False
    )
    name_embs = _norm_rows(name_embs)
    print(f"name matrix: {name_embs.shape}")

    conn = sqlite3.connect(str(ROOT / "backend/data/indicators.db"))
    syn_by_key = {
        (p, c): s
        for p, c, s in conn.execute(
            "SELECT provider, code, synonyms FROM indicators "
            "WHERE synonyms IS NOT NULL AND LENGTH(synonyms) > 8"
        )
    }
    rows_to_embed = []
    for i, (c, p) in enumerate(zip(codes, providers)):
        syn = syn_by_key.get((p, c))
        if syn and len(str(syn).strip()) > 8:
            rows_to_embed.append((i, str(syn)[:300]))
    print(f"{len(rows_to_embed)} rows get a synonym vector "
          f"(weights {w_name}/{w_syn})")

    retrieval = EmbeddingRetrieval()
    client = retrieval._get_client()
    blended = name_embs.copy()
    B = 500
    for start in range(0, len(rows_to_embed), B):
        chunk = rows_to_embed[start : start + B]
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL, input=[t for _i, t in chunk]
        )
        for (row_idx, _t), d in zip(chunk, resp.data):
            sv = np.asarray(d.embedding, dtype=np.float32)
            sv /= np.linalg.norm(sv) or 1
            blended[row_idx] = w_name * name_embs[row_idx] + w_syn * sv
        if (start // B) % 5 == 0:
            print(f"  embedded {min(start + B, len(rows_to_embed))}/{len(rows_to_embed)}",
                  flush=True)
    blended = _norm_rows(blended)

    # A/B before writing anything
    probe_vecs = {}
    for q, _p in PROBES:
        if q not in probe_vecs:
            r = client.embeddings.create(model=EMBEDDING_MODEL, input=[q])
            v = np.asarray(r.data[0].embedding, dtype=np.float32)
            probe_vecs[q] = v / (np.linalg.norm(v) or 1)
    print("\n=== A/B: OLD (name-only) vs NEW (weighted blend) ===")
    for q, p in PROBES:
        old = _top10(name_embs, codes, providers, probe_vecs[q], p)
        new = _top10(blended, codes, providers, probe_vecs[q], p)
        print(f"\n[{p}] {q!r}  (overlap {len(set(old) & set(new))}/10)")
        print("  OLD:", old[:6])
        print("  NEW:", new[:6])

    if args.ab_only:
        print("\n--ab-only: no artifacts written.")
        return 0

    # Write artifacts (service picks them up on next restart only)
    np.savez_compressed(NPZ, embeddings=blended)
    META.write_text(json.dumps(meta))
    _write_normalized_npy_atomic(blended)
    print(f"\nArtifacts written (npz/meta/npy, weights {w_name}/{w_syn}). "
          "Review A/B, then restart to adopt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
