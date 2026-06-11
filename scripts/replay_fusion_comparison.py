#!/usr/bin/env python3
"""Offline replay harness: legacy vs RRF candidate fusion in IndicatorSelector.

Replays gold-labelled direct_single_series queries from the private dev
datasets through ``IndicatorSelector._get_candidates_with_scores`` under BOTH
fusion modes (``legacy`` and ``rrf``) and compares retrieval quality against
the gold indicator codes — fully offline with respect to the LLM (the
``select()`` adjudication step is never invoked).

Fairness guarantees:
  * The per-query embedding search is memoized, so both fusion modes consume
    byte-identical retrieval inputs (and OpenAI/OpenRouter embedding API
    calls are halved).
  * Queries where embedding retrieval silently degraded to FTS-only (empty
    embedding result) are flagged and EXCLUDED from metrics — an FTS-only
    comparison would not measure fusion behaviour at all.
  * FTS5 + sqlite metadata passes are local and deterministic; modes only
    differ in the fusion sort (``_effective_rank`` vs ``_rrf_rank``).

Gold matching is structural: case-insensitive exact match on the provider
code (same normalization as scripts/benchmark_query_framework.py). No
semantic mappings are involved.

Usage:
    source backend/.venv/bin/activate
    python3 scripts/replay_fusion_comparison.py --per-provider 5 \
        --output validation_private/replay_fusion_smoke_2026-06-10.json
    python3 scripts/replay_fusion_comparison.py --per-provider 60 \
        --output validation_private/replay_fusion_results_2026-06-10.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEV_DATASET_DIR = REPO_ROOT / "validation_private" / "datasets" / "dev"
DEFAULT_OUTPUT = REPO_ROOT / "validation_private" / "replay_fusion_results_2026-06-10.json"

# OECD skipped (rate-limit policy), Comtrade skipped (not in the selector gate).
TARGET_PROVIDERS = ("FRED", "WORLDBANK", "STATSCAN", "IMF", "EUROSTAT", "BIS")

# Dataset files whose names indicate failure-biased probe/retry corpora would
# skew the sample toward known-hard queries; exclude them structurally.
EXCLUDED_FILE_PATTERN = re.compile(r"fail|residual|retry|probe|rate.?limit", re.IGNORECASE)

FUSION_MODES = ("legacy", "rrf")
TOP_K = 50

logger = logging.getLogger("replay_fusion")


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from .env into os.environ (no override)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_code(code: Optional[str]) -> str:
    """Normalize code values for case-insensitive comparison.

    Same approach as scripts/benchmark_query_framework.py::normalize_code.
    """
    if not code:
        return ""
    return str(code).strip().upper()


@dataclass
class GoldCase:
    case_id: str
    provider_stratum: str  # db spelling, e.g. "WorldBank" — passed to selector
    provider_upper: str  # canonical uppercase for grouping/reporting
    query: str
    gold_code: str
    gold_name: str
    source_file: str


def load_gold_pool(providers: Tuple[str, ...]) -> Dict[str, List[GoldCase]]:
    """Load deduped direct_single_series gold cases per provider.

    Dedupes by (provider, lowercased query text); first occurrence in sorted
    file order wins so the pool is deterministic. Rows flagged
    query_quality_risk == "high" are skipped.
    """
    pool: Dict[Tuple[str, str], GoldCase] = {}
    files = sorted(DEV_DATASET_DIR.glob("*.jsonl"))
    used_files = 0
    for path in files:
        if EXCLUDED_FILE_PATTERN.search(path.name):
            continue
        used_files += 1
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("query_family") != "direct_single_series":
                    continue
                stratum = str(row.get("provider_stratum") or "").strip()
                provider_upper = stratum.upper()
                if provider_upper not in providers:
                    continue
                query = str(row.get("query") or "").strip()
                origin = row.get("origin") or {}
                gold_code = str(origin.get("source_indicator_code") or "").strip()
                if not query or not gold_code:
                    continue
                provenance = row.get("provenance") or {}
                if provenance.get("query_quality_risk") == "high":
                    continue
                key = (provider_upper, query.lower())
                if key in pool:
                    continue
                pool[key] = GoldCase(
                    case_id=str(row.get("id") or f"{path.stem}:{len(pool)}"),
                    provider_stratum=stratum,
                    provider_upper=provider_upper,
                    query=query,
                    gold_code=gold_code,
                    gold_name=str(origin.get("name") or ""),
                    source_file=path.name,
                )
    logger.info("Scanned %d dataset files; pool size %d", used_files, len(pool))

    by_provider: Dict[str, List[GoldCase]] = {p: [] for p in providers}
    for case in pool.values():
        by_provider[case.provider_upper].append(case)
    for cases in by_provider.values():
        cases.sort(key=lambda c: (c.provider_upper, c.query.lower(), c.gold_code))
    return by_provider


def sample_cases(
    by_provider: Dict[str, List[GoldCase]], per_provider: int, seed: int,
) -> List[GoldCase]:
    """Deterministically sample up to per_provider cases for each provider."""
    rng = random.Random(seed)
    sampled: List[GoldCase] = []
    for provider in sorted(by_provider):
        cases = by_provider[provider]
        take = min(per_provider, len(cases))
        if take < per_provider:
            logger.info(
                "Provider %s has only %d unique gold cases (< %d requested)",
                provider, len(cases), per_provider,
            )
        sampled.extend(rng.sample(cases, take) if take else [])
    return sampled


class MemoizedEmbeddingSearch:
    """Wrap EmbeddingRetrieval.search with a result cache.

    Guarantees both fusion modes see byte-identical embedding retrieval input
    for a query, halves embedding API calls, and records per-query whether
    the embedding stage returned any candidates (degradation detection).
    """

    def __init__(self) -> None:
        from backend.services.embedding_retrieval import get_embedding_retrieval

        self._retrieval = get_embedding_retrieval()
        self._original_search = self._retrieval.search
        self._cache: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
        self.by_query_nonempty: Dict[str, bool] = {}
        self.api_calls = 0
        self.total_calls = 0

    def install(self) -> None:
        self._retrieval.search = self._cached_search  # type: ignore[method-assign]

    def _cached_search(
        self, query: str, provider: Optional[str] = None, top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        self.total_calls += 1
        key = (query, str(provider or "").upper(), int(top_k))
        if key not in self._cache:
            self.api_calls += 1
            self._cache[key] = self._original_search(query, provider=provider, top_k=top_k)
        results = self._cache[key]
        self.by_query_nonempty[query] = bool(results)
        return results


def gold_rank(candidates: List[Tuple[str, str]], gold_code: str) -> Optional[int]:
    """1-based rank of the gold code in the candidate list, or None."""
    target = normalize_code(gold_code)
    for index, (code, _name) in enumerate(candidates):
        if normalize_code(code) == target:
            return index + 1
    return None


def run_replay(
    cases: List[GoldCase],
    rrf_k: int,
    checkpoint_every: int,
    output_path: Path,
    meta: Dict[str, Any],
) -> List[Dict[str, Any]]:
    from backend.services.indicator_selector import IndicatorSelector

    settings = SimpleNamespace(
        indicator_fusion="legacy",
        indicator_rrf_k=rrf_k,
        indicator_telemetry_enabled=False,
    )
    selector = IndicatorSelector(settings=settings)

    embed = MemoizedEmbeddingSearch()
    embed.install()

    records: List[Dict[str, Any]] = []
    started = time.time()

    for index, case in enumerate(cases, start=1):
        record: Dict[str, Any] = {
            "id": case.case_id,
            "provider": case.provider_upper,
            "provider_stratum": case.provider_stratum,
            "query": case.query,
            "gold_code": case.gold_code,
            "gold_name": case.gold_name,
            "source_file": case.source_file,
            "modes": {},
        }
        for mode in FUSION_MODES:
            settings.indicator_fusion = mode
            mode_started = time.time()
            try:
                candidates, _scores = selector._get_candidates_with_scores(  # pylint: disable=protected-access
                    case.query, case.provider_stratum, top_k=TOP_K,
                )
                error = None
            except Exception as exc:  # defensive: selector catches internally
                candidates = []
                error = f"{type(exc).__name__}: {exc}"
            record["modes"][mode] = {
                "top10_codes": [code for code, _name in candidates[:10]],
                "gold_rank": gold_rank(candidates, case.gold_code),
                "n_candidates": len(candidates),
                "elapsed_s": round(time.time() - mode_started, 3),
                "error": error,
            }
        record["embedding_nonempty"] = bool(embed.by_query_nonempty.get(case.query, False))
        records.append(record)

        if index % 10 == 0 or index == len(cases):
            elapsed = time.time() - started
            logger.info(
                "[%d/%d] %.0fs elapsed, %d embed API calls, last=%s/%s",
                index, len(cases), elapsed, embed.api_calls,
                case.provider_upper, case.query[:60],
            )
        if checkpoint_every and (index % checkpoint_every == 0 or index == len(cases)):
            write_output(output_path, meta, records, partial=index < len(cases))

    meta["embedding_api_calls"] = embed.api_calls
    meta["embedding_total_calls"] = embed.total_calls
    return records


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-provider and overall metrics for both fusion modes."""

    def empty_bucket() -> Dict[str, Any]:
        return {
            "n": 0,
            "degraded_excluded": 0,
            "identical_top10": 0,
            "modes": {
                mode: {"top1": 0, "top5": 0, "top10": 0, "anywhere": 0, "rr_sum": 0.0}
                for mode in FUSION_MODES
            },
        }

    buckets: Dict[str, Dict[str, Any]] = {"OVERALL": empty_bucket()}
    for record in records:
        provider = record["provider"]
        buckets.setdefault(provider, empty_bucket())
        for bucket in (buckets[provider], buckets["OVERALL"]):
            if not record["embedding_nonempty"]:
                bucket["degraded_excluded"] += 1
                continue
            bucket["n"] += 1
            legacy_top10 = record["modes"]["legacy"]["top10_codes"]
            rrf_top10 = record["modes"]["rrf"]["top10_codes"]
            if legacy_top10 == rrf_top10:
                bucket["identical_top10"] += 1
            for mode in FUSION_MODES:
                rank = record["modes"][mode]["gold_rank"]
                stats = bucket["modes"][mode]
                if rank is not None:
                    stats["anywhere"] += 1
                    stats["rr_sum"] += 1.0 / rank
                    if rank <= 1:
                        stats["top1"] += 1
                    if rank <= 5:
                        stats["top5"] += 1
                    if rank <= 10:
                        stats["top10"] += 1

    summary: Dict[str, Any] = {}
    for provider, bucket in buckets.items():
        n = bucket["n"]
        summary[provider] = {
            "n_scored": n,
            "degraded_excluded": bucket["degraded_excluded"],
            "identical_top10_pct": round(100.0 * bucket["identical_top10"] / n, 1) if n else None,
            "modes": {},
        }
        for mode in FUSION_MODES:
            stats = bucket["modes"][mode]
            summary[provider]["modes"][mode] = {
                "top1": round(stats["top1"] / n, 4) if n else None,
                "top5": round(stats["top5"] / n, 4) if n else None,
                "top10": round(stats["top10"] / n, 4) if n else None,
                "anywhere": round(stats["anywhere"] / n, 4) if n else None,
                "mrr": round(stats["rr_sum"] / n, 4) if n else None,
            }
    return summary


def format_summary_table(summary: Dict[str, Any]) -> str:
    header = (
        f"{'provider':<10} {'mode':<7} {'n':>4} {'top1':>6} {'top5':>6} "
        f"{'top10':>6} {'any':>6} {'mrr':>6} {'same10%':>8} {'degr':>5}"
    )
    lines = [header, "-" * len(header)]
    providers = sorted(p for p in summary if p != "OVERALL") + ["OVERALL"]
    for provider in providers:
        bucket = summary[provider]
        for mode in FUSION_MODES:
            stats = bucket["modes"][mode]

            def fmt(value: Optional[float]) -> str:
                return f"{value:.3f}" if value is not None else "  n/a"

            same = (
                f"{bucket['identical_top10_pct']:.1f}"
                if bucket["identical_top10_pct"] is not None
                else "n/a"
            )
            lines.append(
                f"{provider:<10} {mode:<7} {bucket['n_scored']:>4} "
                f"{fmt(stats['top1']):>6} {fmt(stats['top5']):>6} "
                f"{fmt(stats['top10']):>6} {fmt(stats['anywhere']):>6} "
                f"{fmt(stats['mrr']):>6} {same:>8} {bucket['degraded_excluded']:>5}"
            )
    return "\n".join(lines)


def write_output(
    path: Path,
    meta: Dict[str, Any],
    records: List[Dict[str, Any]],
    partial: bool,
) -> None:
    payload = {
        "meta": {**meta, "partial": partial, "records_written": len(records)},
        "summary": summarize(records),
        "records": records,
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    os.replace(tmp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay gold queries through IndicatorSelector candidate "
        "fusion (legacy vs rrf) and compare retrieval metrics.",
    )
    parser.add_argument(
        "--per-provider", type=int, default=60,
        help="Max sampled queries per provider (default: 60)",
    )
    parser.add_argument(
        "--seed", type=int, default=20260610,
        help="RNG seed for reproducible sampling (default: 20260610)",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--providers", nargs="*", default=list(TARGET_PROVIDERS),
        help="Provider strata to include (uppercase canonical names)",
    )
    parser.add_argument(
        "--rrf-k", type=int, default=60,
        help="RRF k constant (default: 60, matching production default)",
    )
    parser.add_argument(
        "--checkpoint-every", type=int, default=50,
        help="Write partial results to --output every N queries (default: 50)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Keep backend internals quiet except for degradation warnings.
    logging.getLogger("backend").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    load_env_file(REPO_ROOT / ".env")
    if not os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        logger.error("No OPENROUTER_API_KEY/OPENAI_API_KEY available; embedding "
                     "retrieval would silently degrade to FTS-only. Aborting.")
        return 2

    providers = tuple(p.upper() for p in args.providers)
    by_provider = load_gold_pool(providers)
    cases = sample_cases(by_provider, args.per_provider, args.seed)
    if not cases:
        logger.error("No gold cases sampled; check dataset directory %s", DEV_DATASET_DIR)
        return 2
    logger.info(
        "Sampled %d cases: %s", len(cases),
        dict(Counter(c.provider_upper for c in cases)),
    )

    meta: Dict[str, Any] = {
        "generated_at": "2026-06-10",
        "seed": args.seed,
        "per_provider": args.per_provider,
        "providers": list(providers),
        "rrf_k": args.rrf_k,
        "top_k": TOP_K,
        "fusion_modes": list(FUSION_MODES),
        "dataset_dir": str(DEV_DATASET_DIR),
        "excluded_file_pattern": EXCLUDED_FILE_PATTERN.pattern,
        "sampled_counts": dict(Counter(c.provider_upper for c in cases)),
    }

    started = time.time()
    records = run_replay(cases, args.rrf_k, args.checkpoint_every, args.output, meta)
    meta["runtime_s"] = round(time.time() - started, 1)

    write_output(args.output, meta, records, partial=False)

    summary = summarize(records)
    degraded_total = summary["OVERALL"]["degraded_excluded"]
    print()
    print(format_summary_table(summary))
    print()
    print(f"records: {len(records)}  degraded(excluded): {degraded_total}  "
          f"runtime: {meta['runtime_s']}s  output: {args.output}")
    if degraded_total == len(records):
        logger.error("ALL queries degraded to FTS-only — embedding retrieval is "
                     "not working; the fusion comparison is meaningless.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
