#!/usr/bin/env python3
"""
Benchmark embedding models on OpenEcon metadata retrieval behavior.

This script compares:
- raw vector retrieval without provider filtering
- vector retrieval with provider filtering
- selector-style provider-filtered top-candidate evidence

Usage:
    OPENAI_API_KEY=... backend/.venv/bin/python scripts/benchmark_embedding_models.py
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

backend_dir = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_dir.parent))

from backend.embedding_utils import resolve_embedding_dimensions
from backend.services.faiss_vector_search import FAISSVectorSearch, VectorSearchResult
from scripts.rebuild_faiss_index import load_indicators_from_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


MODELS = [
    "sentence-transformers/all-MiniLM-L6-v2",
    "text-embedding-3-small",
    "text-embedding-3-large",
]

CASES = [
    {"query": "us jobless rate", "provider": "FRED", "terms": ["unemployment"]},
    {"query": "consumer price inflation usa", "provider": "FRED", "terms": ["consumer price index", "inflation, consumer prices", "cpi"]},
    {"query": "federal funds rate", "provider": "FRED", "terms": ["federal funds"]},
    {"query": "factory output index", "provider": "FRED", "terms": ["industrial production"]},
    {"query": "housing starts in the united states", "provider": "FRED", "terms": ["housing units started", "housing starts"]},
    {"query": "wages and salaries in the us", "provider": "FRED", "terms": ["wages and salaries"]},
    {"query": "average lifespan at birth", "provider": "WORLDBANK", "terms": ["life expectancy at birth"]},
    {"query": "extreme poverty headcount", "provider": "WORLDBANK", "terms": ["poverty headcount"]},
    {"query": "current account balance percent of gdp", "provider": "WORLDBANK", "terms": ["current account balance (% of gdp)", "current account balance"]},
    {"query": "research and development spending share of gdp", "provider": "WORLDBANK", "terms": ["research and development expenditure"]},
    {"query": "school life expectancy", "provider": "WORLDBANK", "terms": ["school life expectancy"]},
    {"query": "youth unemployment rate", "provider": "WORLDBANK", "terms": ["youth unemployment rate"]},
    {"query": "external current account as share of output", "provider": "IMF", "terms": ["current account", "external current account"]},
    {"query": "gross public debt percent of gdp", "provider": "IMF", "terms": ["gross public debt"]},
    {"query": "overall fiscal balance including grants", "provider": "IMF", "terms": ["overall fiscal balance"]},
    {"query": "real effective exchange rate", "provider": "IMF", "terms": ["real effective exchange rate", "real effective exchange rates"]},
    {"query": "unemployment rate imf", "provider": "IMF", "terms": ["unemployment rate", "unemployment"]},
    {"query": "consumer price inflation average", "provider": "IMF", "terms": ["consumer prices", "inflation rate, average consumer prices"]},
    {"query": "harmonised consumer prices monthly data", "provider": "EUROSTAT", "terms": ["harmonised index of consumer prices", "hicp"]},
    {"query": "unemployment rates by region in europe", "provider": "EUROSTAT", "terms": ["unemployment rates", "unemployment"]},
    {"query": "gdp per capita in pps", "provider": "EUROSTAT", "terms": ["gdp per capita in pps", "real gdp per capita", "gdp per capita"]},
    {"query": "employment rate by citizenship", "provider": "EUROSTAT", "terms": ["employment rate"]},
    {"query": "producer prices industry domestic market", "provider": "EUROSTAT", "terms": ["producer prices"]},
    {"query": "current account quarterly data", "provider": "EUROSTAT", "terms": ["current account"]},
    {"query": "subnational government tax revenue", "provider": "OECD", "terms": ["tax revenue"]},
    {"query": "business enterprise r and d expenditure", "provider": "OECD", "terms": ["business enterprise r&d expenditure", "research and development"]},
    {"query": "life expectancy regions", "provider": "OECD", "terms": ["life expectancy"]},
    {"query": "average annual wages", "provider": "OECD", "terms": ["average annual wages", "wages"]},
    {"query": "labour force participation rate", "provider": "OECD", "terms": ["labour force participation rate", "labour force participation"]},
    {"query": "consumer price index hicp", "provider": "OECD", "terms": ["consumer price", "hicp"]},
    {"query": "canada unemployment rate", "provider": "STATSCAN", "terms": ["unemployment rate"]},
    {"query": "canada consumer price index", "provider": "STATSCAN", "terms": ["consumer price index", "cpi"]},
    {"query": "canada life expectancy at birth", "provider": "STATSCAN", "terms": ["life expectancy"]},
    {"query": "canada business research and development expenditures", "provider": "STATSCAN", "terms": ["research and development"]},
    {"query": "canada wages and salaries", "provider": "STATSCAN", "terms": ["wages and salaries", "wages"]},
    {"query": "canada labour force participation", "provider": "STATSCAN", "terms": ["labour force participation", "participation rate"]},
    {"query": "residential property prices", "provider": "BIS", "terms": ["residential property prices", "property prices"]},
    {"query": "commercial property prices", "provider": "BIS", "terms": ["commercial property prices", "property prices"]},
    {"query": "debt service ratios", "provider": "BIS", "terms": ["debt service ratios", "debt service"]},
    {"query": "effective exchange rates", "provider": "BIS", "terms": ["effective exchange rates", "effective exchange rate"]},
    {"query": "credit to gdp gap", "provider": "BIS", "terms": ["credit-to-gdp gaps", "credit gap"]},
    {"query": "consumer prices statistics", "provider": "BIS", "terms": ["consumer prices statistics", "consumer prices"]},
]


def normalize_provider(provider: str | None) -> str:
    return str(provider or "").strip().replace(" ", "").replace("_", "").replace("-", "").upper()


def safe_model_name(model_name: str) -> str:
    return model_name.replace("/", "__")


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def text_matches_terms(text: str, terms: list[str]) -> bool:
    hay = normalize_text(text)
    return any(normalize_text(term) in hay for term in terms)


def result_text(result: Any) -> str:
    name = str(getattr(result, "name", "") or "")
    metadata = getattr(result, "metadata", None) or {}
    description = str(metadata.get("description") or "")
    return f"{name} {description}".strip()


class SearchAdapter:
    """Adapter exposing the provider-filtered search used by selector retrieval."""

    def __init__(self, backend: FAISSVectorSearch):
        self.backend = backend

    def search(
        self,
        query: str,
        limit: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        provider_filter = where.get("provider") if where else None
        return self.backend.search(query=query, limit=limit, provider_filter=provider_filter)

    def is_indexed(self) -> bool:
        return self.backend.is_indexed()


def aggregate_rate(hits: int, total: int) -> dict[str, float | int]:
    return {"hits": hits, "total": total, "rate": (hits / total) if total else 0.0}


def benchmark_vector_mode(
    search_adapter: SearchAdapter,
    cases: list[dict[str, Any]],
    filtered: bool,
) -> dict[str, Any]:
    top1_provider_hits = 0
    top1_relevance_hits = 0
    top3_relevance_hits = 0
    top5_relevance_hits = 0
    reciprocal_ranks: list[float] = []
    latencies_ms: list[float] = []
    by_provider: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "top1_relevance_hits": 0, "top5_relevance_hits": 0})
    failures: list[dict[str, Any]] = []

    for case in cases:
        where = {"provider": case["provider"]} if filtered else None
        start = time.time()
        results = search_adapter.search(case["query"], limit=5, where=where)
        latencies_ms.append((time.time() - start) * 1000)

        provider_stats = by_provider[case["provider"]]
        provider_stats["count"] += 1

        if results:
            top1_provider_hits += int(normalize_provider(results[0].provider) == case["provider"])

        found_rank = None
        for idx, result in enumerate(results, start=1):
            if normalize_provider(result.provider) != case["provider"]:
                continue
            if text_matches_terms(result_text(result), case["terms"]):
                found_rank = idx
                break

        top1_rel = int(found_rank == 1)
        top3_rel = int(found_rank is not None and found_rank <= 3)
        top5_rel = int(found_rank is not None and found_rank <= 5)
        top1_provider_hits += 0 if filtered else 0
        top1_relevance_hits += top1_rel
        top3_relevance_hits += top3_rel
        top5_relevance_hits += top5_rel
        provider_stats["top1_relevance_hits"] += top1_rel
        provider_stats["top5_relevance_hits"] += top5_rel
        reciprocal_ranks.append((1.0 / found_rank) if found_rank else 0.0)

        if not top1_rel or not top5_rel:
            failures.append({
                "query": case["query"],
                "provider": case["provider"],
                "found_rank": found_rank,
                "top_results": [
                    {
                        "provider": result.provider,
                        "code": result.code,
                        "name": result.name,
                    }
                    for result in results[:3]
                ],
            })

    total = len(cases)
    provider_breakdown = {}
    for provider, stats in by_provider.items():
        provider_breakdown[provider] = {
            "count": stats["count"],
            "top1_relevance": aggregate_rate(stats["top1_relevance_hits"], stats["count"]),
            "top5_relevance": aggregate_rate(stats["top5_relevance_hits"], stats["count"]),
        }

    return {
        "top1_provider": aggregate_rate(top1_provider_hits, total) if not filtered else None,
        "top1_relevance": aggregate_rate(top1_relevance_hits, total),
        "top3_relevance": aggregate_rate(top3_relevance_hits, total),
        "top5_relevance": aggregate_rate(top5_relevance_hits, total),
        "mrr": mean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "latency_ms": {
            "mean": mean(latencies_ms) if latencies_ms else 0.0,
            "median": median(latencies_ms) if latencies_ms else 0.0,
            "max": max(latencies_ms) if latencies_ms else 0.0,
        },
        "provider_breakdown": provider_breakdown,
        "failures": failures[:12],
    }


def benchmark_selector_retrieval_mode(
    search_adapter: SearchAdapter,
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    success_hits = 0
    latencies_ms: list[float] = []
    by_provider: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "success_hits": 0})
    failures: list[dict[str, Any]] = []

    for case in cases:
        provider_stats = by_provider[case["provider"]]
        provider_stats["count"] += 1
        start = time.time()
        results = search_adapter.search(
            query=case["query"],
            limit=5,
            where={"provider": case["provider"]},
        )
        latencies_ms.append((time.time() - start) * 1000)

        if not results:
            failures.append({
                "query": case["query"],
                "provider": case["provider"],
                "result": None,
            })
            continue

        result = results[0]
        matches_provider = normalize_provider(result.provider) == case["provider"]
        matches_terms = text_matches_terms(result_text(result), case["terms"])
        success = int(matches_provider and matches_terms)
        success_hits += success
        provider_stats["success_hits"] += success

        if not success:
            failures.append({
                "query": case["query"],
                "provider": case["provider"],
                "result": {
                    "provider": result.provider,
                    "code": result.code,
                    "name": result.name,
                },
            })

    provider_breakdown = {}
    for provider, stats in by_provider.items():
        provider_breakdown[provider] = {
            "count": stats["count"],
            "success": aggregate_rate(stats["success_hits"], stats["count"]),
        }

    return {
        "success": aggregate_rate(success_hits, len(cases)),
        "latency_ms": {
            "mean": mean(latencies_ms) if latencies_ms else 0.0,
            "median": median(latencies_ms) if latencies_ms else 0.0,
            "max": max(latencies_ms) if latencies_ms else 0.0,
        },
        "provider_breakdown": provider_breakdown,
        "failures": failures[:12],
    }


def build_backend(model_name: str, indicators: list[dict[str, Any]]) -> tuple[FAISSVectorSearch, float]:
    temp_dir = Path("/tmp/openecon_embedding_bench") / safe_model_name(model_name)
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    dimensions = resolve_embedding_dimensions(model_name)
    backend = FAISSVectorSearch(
        model_name=model_name,
        index_dir=str(temp_dir),
        embedding_dimensions=dimensions,
    )

    start = time.time()
    backend.index_indicators(indicators=indicators, batch_size=100, clear_existing=True)
    index_time_s = time.time() - start
    return backend, index_time_s


def benchmark_model(model_name: str, indicators: list[dict[str, Any]]) -> dict[str, Any]:
    logger.info("=== Benchmarking %s ===", model_name)
    backend, index_time_s = build_backend(model_name, indicators)
    search_adapter = SearchAdapter(backend)

    return {
        "index_time_s": index_time_s,
        "index_size": backend.get_index_size(),
        "embedding_dim": backend.embedding_dim,
        "vector_unfiltered": benchmark_vector_mode(search_adapter, CASES, filtered=False),
        "vector_filtered": benchmark_vector_mode(search_adapter, CASES, filtered=True),
        "selector_retrieval": benchmark_selector_retrieval_mode(search_adapter, CASES),
    }


def main() -> int:
    indicators, provider_counts = load_indicators_from_json()
    logger.info("Loaded %s benchmark indicators", len(indicators))
    logger.info("Provider counts: %s", provider_counts)

    results = {
        "case_count": len(CASES),
        "providers": provider_counts,
        "models": {},
    }

    for model_name in MODELS:
        results["models"][model_name] = benchmark_model(model_name, indicators)

    output_path = Path("/tmp/openecon_embedding_model_review.json")
    output_path.write_text(json.dumps(results, indent=2))
    logger.info("Wrote benchmark results to %s", output_path)

    for model_name, info in results["models"].items():
        logger.info(
            "%s | index %.1fs | vector filtered top1 %.1f%% | selector retrieval %.1f%%",
            model_name,
            info["index_time_s"],
            info["vector_filtered"]["top1_relevance"]["rate"] * 100,
            info["selector_retrieval"]["success"]["rate"] * 100,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
