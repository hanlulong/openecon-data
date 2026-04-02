#!/usr/bin/env python3
"""
Test script: Measure recall@k for embedding retrieval.

Picks 50 random indicators from the embedding index, uses each indicator's
NAME as the search query, and checks whether the exact indicator code appears
in the top-k results at k=50, k=100, k=200.

This tells us how much recall we gain by increasing top_k in the selector.

Usage:
    cd /home/hanlulong/OpenEcon
    source backend/.venv/bin/activate
    python3 scripts/test_topk_recall.py

Optional flags:
    --n 100         Number of random indicators to test (default: 50)
    --provider FRED Only test indicators from a specific provider
    --seed 42       Random seed for reproducibility
    --verbose       Print each query result
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import numpy as np

# Add project root to path so we can import backend modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EMBEDDING_DIR = PROJECT_ROOT / "backend" / "data" / "openai_embeddings"
INDEX_FILE = EMBEDDING_DIR / "indicator_embeddings.npz"
META_FILE = EMBEDDING_DIR / "indicator_metadata.json"

# The k values we want to compare
K_VALUES = [50, 100, 200]


def load_index():
    """Load the embedding index and metadata."""
    print(f"Loading embedding index from {INDEX_FILE}...")
    data = np.load(INDEX_FILE)
    embeddings = data["embeddings"].astype(np.float32)

    with open(META_FILE) as f:
        meta = json.load(f)

    codes = meta["codes"]
    names = meta["names"]
    providers = meta["providers"]

    # Normalize for cosine similarity
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    embeddings = embeddings / norms

    print(f"Loaded {len(codes)} indicators, embedding dim={embeddings.shape[1]}")
    return embeddings, codes, names, providers


def embed_queries(queries: list[str], batch_size: int = 100) -> np.ndarray:
    """Embed a batch of queries using OpenAI API."""
    import openai

    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = None
    if not api_key:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        base_url = "https://openrouter.ai/api/v1"

    if not api_key:
        print("ERROR: Set OPENAI_API_KEY or OPENROUTER_API_KEY environment variable")
        sys.exit(1)

    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    model = "text-embedding-3-small"

    all_embeddings = []
    for i in range(0, len(queries), batch_size):
        batch = queries[i : i + batch_size]
        batch = [q[:200] for q in batch]  # Truncate long names
        resp = client.embeddings.create(model=model, input=batch)
        batch_embs = [d.embedding for d in resp.data]
        all_embeddings.extend(batch_embs)
        if len(queries) > batch_size:
            print(f"  Embedded {min(i + batch_size, len(queries))}/{len(queries)}...")

    result = np.array(all_embeddings, dtype=np.float32)
    # Normalize
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    norms[norms == 0] = 1
    result = result / norms
    return result


def run_recall_test(
    n_samples: int = 50,
    provider_filter: str | None = None,
    seed: int = 42,
    verbose: bool = False,
):
    """Main test: measure recall@k for different k values."""

    embeddings, codes, names, providers = load_index()

    # Build candidate pool (optionally filtered by provider)
    pool_indices = list(range(len(codes)))
    if provider_filter:
        pf = provider_filter.upper()
        pool_indices = [i for i in pool_indices if providers[i].upper() == pf]
        print(f"Filtered to provider={provider_filter}: {len(pool_indices)} indicators")

    if len(pool_indices) < n_samples:
        print(f"WARNING: Only {len(pool_indices)} indicators available, using all of them")
        n_samples = len(pool_indices)

    # Sample random indicators
    rng = random.Random(seed)
    sample_indices = rng.sample(pool_indices, n_samples)

    sample_names = [names[i] for i in sample_indices]
    sample_codes = [codes[i] for i in sample_indices]
    sample_providers = [providers[i] for i in sample_indices]

    print(f"\nSampled {n_samples} random indicators (seed={seed})")
    print(f"Embedding queries...")

    start = time.time()
    query_embs = embed_queries(sample_names)
    embed_time = time.time() - start
    print(f"Embedded {n_samples} queries in {embed_time:.1f}s")

    # For each k value, compute recall
    max_k = max(K_VALUES)

    # Compute all similarities at once: (n_samples, n_indicators)
    print(f"\nComputing similarities...")
    sims = query_embs @ embeddings.T  # (n_samples, n_indicators)

    # Results tracking
    recall_at_k = {k: 0 for k in K_VALUES}
    rank_distribution = []  # rank at which the correct indicator appears
    missed_at_max = []  # indicators not found even at max_k

    for idx in range(n_samples):
        target_code = sample_codes[idx]
        target_provider = sample_providers[idx]
        target_name = sample_names[idx]

        sim_row = sims[idx]

        # Apply provider filter (same as EmbeddingRetrieval.search does)
        # We filter by the indicator's own provider to simulate real usage
        provider_upper = target_provider.upper()
        mask = np.array([p.upper() == provider_upper for p in providers])
        filtered_sims = np.where(mask, sim_row, -1)

        # Get top max_k indices
        top_indices = np.argsort(-filtered_sims)[:max_k]
        top_codes = [codes[i] for i in top_indices]

        # Find rank of target code
        rank = None
        for r, c in enumerate(top_codes):
            if c == target_code:
                rank = r + 1  # 1-indexed
                break

        rank_distribution.append(rank)

        # Check recall at each k
        for k in K_VALUES:
            if rank is not None and rank <= k:
                recall_at_k[k] += 1

        if verbose:
            found_str = f"rank={rank}" if rank else f"NOT FOUND in top-{max_k}"
            print(f"  [{target_provider}] {target_name[:60]:<60} -> {found_str}")

        if rank is None:
            missed_at_max.append((target_code, target_name, target_provider))

    # Print results
    print("\n" + "=" * 70)
    print("RECALL RESULTS")
    print("=" * 70)

    for k in K_VALUES:
        pct = recall_at_k[k] / n_samples * 100
        print(f"  recall@{k:<4d} = {recall_at_k[k]:>4d}/{n_samples} = {pct:6.1f}%")

    # Rank statistics
    found_ranks = [r for r in rank_distribution if r is not None]
    not_found = sum(1 for r in rank_distribution if r is None)

    print(f"\n  Found:     {len(found_ranks)}/{n_samples}")
    print(f"  Not found: {not_found}/{n_samples} (not in top-{max_k})")

    if found_ranks:
        print(f"\n  Rank statistics (among found):")
        print(f"    Mean rank:   {sum(found_ranks) / len(found_ranks):.1f}")
        print(f"    Median rank: {sorted(found_ranks)[len(found_ranks) // 2]}")
        print(f"    Min rank:    {min(found_ranks)}")
        print(f"    Max rank:    {max(found_ranks)}")

        # Rank distribution buckets
        buckets = [(1, 1), (2, 5), (6, 10), (11, 20), (21, 50), (51, 100), (101, 200)]
        print(f"\n  Rank distribution:")
        for lo, hi in buckets:
            count = sum(1 for r in found_ranks if lo <= r <= hi)
            bar = "#" * count
            if lo == hi:
                label = f"    rank={lo}"
            else:
                label = f"    rank {lo:>3d}-{hi:<3d}"
            print(f"{label}: {count:>3d}  {bar}")

    # Show missed indicators
    if missed_at_max and verbose:
        print(f"\n  Missed at top-{max_k}:")
        for code, name, prov in missed_at_max[:10]:
            print(f"    [{prov}] {code}: {name[:70]}")

    # Marginal gain analysis
    print(f"\n  Marginal gains:")
    prev_recall = 0
    for k in K_VALUES:
        gain = recall_at_k[k] - prev_recall
        gain_pct = gain / n_samples * 100
        print(f"    k={prev_recall if prev_recall == 0 else K_VALUES[K_VALUES.index(k) - 1]}->k={k}: +{gain} indicators (+{gain_pct:.1f}%)")
        prev_recall = recall_at_k[k]

    print(f"\n  Estimated API cost: ~${n_samples * 0.00002:.4f} (embedding only)")
    print("=" * 70)

    return recall_at_k, n_samples


def main():
    parser = argparse.ArgumentParser(description="Test embedding recall@k")
    parser.add_argument("--n", type=int, default=50, help="Number of random indicators to test")
    parser.add_argument("--provider", type=str, default=None, help="Filter to a specific provider")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print each query result")
    args = parser.parse_args()

    run_recall_test(
        n_samples=args.n,
        provider_filter=args.provider,
        seed=args.seed,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
