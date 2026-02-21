#!/usr/bin/env python3
"""
LLM Provider Comparison Script

Compares gpt-oss-120b (local vLLM) vs gpt-4o (OpenRouter) for query parsing.
Runs real queries, measures performance, and verifies results by fetching actual data.

Usage:
    python scripts/compare_llm_providers.py
    python scripts/compare_llm_providers.py --queries 5  # Limit number of queries
    python scripts/compare_llm_providers.py --no-fetch   # Skip data fetching
"""

import asyncio
import argparse
import json
import time
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.config import get_settings
from backend.services.llm import create_llm_provider
from backend.services.openrouter import OpenRouterService
from backend.services.query import QueryService


@dataclass
class QueryResult:
    """Result of a single query test"""
    query: str
    provider_name: str
    success: bool
    time_seconds: float
    parsed_provider: Optional[str] = None
    parsed_indicators: Optional[List[str]] = None
    parsed_parameters: Optional[Dict] = None
    clarification_needed: bool = False
    data_fetched: bool = False
    data_points: int = 0
    error: Optional[str] = None


@dataclass
class ComparisonResult:
    """Comparison between two providers for a query"""
    query: str
    gpt4o: Optional[QueryResult] = None
    vllm: Optional[QueryResult] = None

    @property
    def both_success(self) -> bool:
        return (self.gpt4o and self.gpt4o.success and
                self.vllm and self.vllm.success)

    @property
    def same_provider(self) -> bool:
        if not self.both_success:
            return False
        return (self.gpt4o.parsed_provider.upper() ==
                self.vllm.parsed_provider.upper())

    @property
    def speed_ratio(self) -> Optional[float]:
        if not self.both_success:
            return None
        return self.gpt4o.time_seconds / self.vllm.time_seconds


# Test queries covering different providers and complexity levels
TEST_QUERIES = [
    # FRED queries (US data)
    "Show me US GDP for the last 5 years",
    "What is the current US unemployment rate?",
    "Show me US inflation rate since 2020",

    # World Bank queries (international)
    "Compare GDP per capita between US and China",
    "What is the population of India?",
    "Show me CO2 emissions for Germany",

    # Statistics Canada
    "What is Canada's unemployment rate?",
    "Show me Canadian housing starts",

    # Multi-country comparisons
    "Compare inflation rates between US, UK, and Japan",
    "Show me GDP growth for G7 countries",

    # Complex queries
    "What are the top trading partners of the United States?",
    "Show me the exchange rate between USD and EUR",

    # Ambiguous queries (should ask for clarification or make reasonable choice)
    "Show me economic data for Brazil",
    "What's happening with interest rates?",
]


async def test_query_with_service(
    service: OpenRouterService,
    query: str,
    provider_name: str,
    fetch_data: bool = True
) -> QueryResult:
    """Test a single query with a service"""
    start = time.time()

    try:
        # Parse query
        intent = await service.parse_query(query)
        parse_time = time.time() - start

        result = QueryResult(
            query=query,
            provider_name=provider_name,
            success=True,
            time_seconds=parse_time,
            parsed_provider=intent.apiProvider,
            parsed_indicators=intent.indicators,
            parsed_parameters=intent.parameters,
            clarification_needed=intent.clarificationNeeded,
        )

        # Optionally fetch data to verify
        if fetch_data and not intent.clarificationNeeded:
            try:
                settings = get_settings()
                query_service = QueryService(
                    settings,
                    fred_key=settings.fred_api_key,
                    comtrade_key=settings.comtrade_api_key
                )
                data = await query_service._fetch_data(intent)
                result.data_fetched = bool(data and len(data) > 0)
                result.data_points = sum(len(d.data) for d in data) if data else 0
            except Exception as e:
                result.data_fetched = False
                result.error = f"Data fetch failed: {str(e)[:100]}"

        return result

    except Exception as e:
        return QueryResult(
            query=query,
            provider_name=provider_name,
            success=False,
            time_seconds=time.time() - start,
            error=str(e)[:200]
        )


async def run_comparison(
    queries: List[str],
    fetch_data: bool = True,
    vllm_url: str = "http://localhost:8000"
) -> List[ComparisonResult]:
    """Run comparison between gpt-4o and vLLM"""

    settings = get_settings()
    results = []

    # Check vLLM availability
    vllm_available = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{vllm_url}/v1/models")
            if response.status_code == 200:
                vllm_available = True
                models = response.json().get("data", [])
                vllm_model = models[0]["id"] if models else "unknown"
                print(f"✅ vLLM available: {vllm_model}")
    except Exception as e:
        print(f"❌ vLLM not available: {e}")

    # Create services
    # GPT-4o via OpenRouter
    gpt4o_service = OpenRouterService(settings.openrouter_api_key, settings)
    # Override to use gpt-4o specifically
    gpt4o_provider = create_llm_provider("openrouter", {
        "api_key": settings.openrouter_api_key,
        "model": "openai/gpt-4o",
        "timeout": 30
    })
    gpt4o_service.llm_provider = gpt4o_provider

    # vLLM service (if available)
    vllm_service = None
    if vllm_available:
        vllm_service = OpenRouterService(settings.openrouter_api_key, settings)
        vllm_provider = create_llm_provider("vllm", {
            "base_url": vllm_url,
            "model": vllm_model,
            "timeout": 120
        })
        vllm_service.llm_provider = vllm_provider

    print(f"\nRunning {len(queries)} queries...")
    print("=" * 80)

    for i, query in enumerate(queries, 1):
        print(f"\n[{i}/{len(queries)}] {query[:60]}...")

        comparison = ComparisonResult(query=query)

        # Test GPT-4o
        print("  Testing GPT-4o...", end=" ", flush=True)
        comparison.gpt4o = await test_query_with_service(
            gpt4o_service, query, "gpt-4o", fetch_data
        )
        if comparison.gpt4o.success:
            print(f"✅ {comparison.gpt4o.time_seconds:.2f}s → {comparison.gpt4o.parsed_provider}")
        else:
            print(f"❌ {comparison.gpt4o.error[:50]}")

        # Test vLLM
        if vllm_service:
            print("  Testing vLLM...", end=" ", flush=True)
            comparison.vllm = await test_query_with_service(
                vllm_service, query, "gpt-oss-120b", fetch_data
            )
            if comparison.vllm.success:
                print(f"✅ {comparison.vllm.time_seconds:.2f}s → {comparison.vllm.parsed_provider}")
            else:
                print(f"❌ {comparison.vllm.error[:50]}")

        results.append(comparison)

    return results


def print_summary(results: List[ComparisonResult]):
    """Print comparison summary"""

    print("\n" + "=" * 80)
    print("COMPARISON SUMMARY")
    print("=" * 80)

    # Calculate statistics
    gpt4o_success = sum(1 for r in results if r.gpt4o and r.gpt4o.success)
    vllm_success = sum(1 for r in results if r.vllm and r.vllm.success)
    both_success = sum(1 for r in results if r.both_success)
    same_provider = sum(1 for r in results if r.same_provider)

    gpt4o_times = [r.gpt4o.time_seconds for r in results if r.gpt4o and r.gpt4o.success]
    vllm_times = [r.vllm.time_seconds for r in results if r.vllm and r.vllm.success]

    gpt4o_data = sum(1 for r in results if r.gpt4o and r.gpt4o.data_fetched)
    vllm_data = sum(1 for r in results if r.vllm and r.vllm.data_fetched)

    print(f"\n📊 Success Rate:")
    print(f"   GPT-4o:      {gpt4o_success}/{len(results)} ({100*gpt4o_success/len(results):.0f}%)")
    if vllm_times:
        print(f"   gpt-oss-120b: {vllm_success}/{len(results)} ({100*vllm_success/len(results):.0f}%)")

    print(f"\n⏱️  Response Time (parsing only):")
    if gpt4o_times:
        print(f"   GPT-4o:       avg {sum(gpt4o_times)/len(gpt4o_times):.2f}s, min {min(gpt4o_times):.2f}s, max {max(gpt4o_times):.2f}s")
    if vllm_times:
        print(f"   gpt-oss-120b: avg {sum(vllm_times)/len(vllm_times):.2f}s, min {min(vllm_times):.2f}s, max {max(vllm_times):.2f}s")

    if gpt4o_times and vllm_times:
        avg_ratio = (sum(gpt4o_times)/len(gpt4o_times)) / (sum(vllm_times)/len(vllm_times))
        if avg_ratio > 1:
            print(f"\n   🚀 gpt-oss-120b is {avg_ratio:.1f}x faster on average")
        else:
            print(f"\n   🚀 GPT-4o is {1/avg_ratio:.1f}x faster on average")

    print(f"\n🎯 Agreement (when both succeed):")
    if both_success > 0:
        print(f"   Same API provider chosen: {same_provider}/{both_success} ({100*same_provider/both_success:.0f}%)")

    print(f"\n✅ Data Fetched Successfully:")
    print(f"   GPT-4o:       {gpt4o_data}/{gpt4o_success} queries returned data")
    if vllm_times:
        print(f"   gpt-oss-120b: {vllm_data}/{vllm_success} queries returned data")

    # Show disagreements
    disagreements = [r for r in results if r.both_success and not r.same_provider]
    if disagreements:
        print(f"\n⚠️  Provider Disagreements ({len(disagreements)}):")
        for r in disagreements:
            print(f"   Query: {r.query[:50]}...")
            print(f"     GPT-4o: {r.gpt4o.parsed_provider}, vLLM: {r.vllm.parsed_provider}")

    # Show failures
    failures = [r for r in results if (r.gpt4o and not r.gpt4o.success) or (r.vllm and not r.vllm.success)]
    if failures:
        print(f"\n❌ Failures ({len(failures)}):")
        for r in failures:
            if r.gpt4o and not r.gpt4o.success:
                print(f"   GPT-4o failed: {r.query[:40]}... - {r.gpt4o.error[:40]}")
            if r.vllm and not r.vllm.success:
                print(f"   vLLM failed: {r.query[:40]}... - {r.vllm.error[:40]}")


def print_detailed_results(results: List[ComparisonResult]):
    """Print detailed per-query results"""

    print("\n" + "=" * 80)
    print("DETAILED RESULTS")
    print("=" * 80)

    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r.query}")
        print("-" * 60)

        if r.gpt4o:
            status = "✅" if r.gpt4o.success else "❌"
            print(f"  GPT-4o: {status} {r.gpt4o.time_seconds:.2f}s")
            if r.gpt4o.success:
                print(f"    Provider: {r.gpt4o.parsed_provider}")
                print(f"    Indicators: {r.gpt4o.parsed_indicators}")
                print(f"    Data: {r.gpt4o.data_points} points" if r.gpt4o.data_fetched else "    Data: not fetched")
            else:
                print(f"    Error: {r.gpt4o.error}")

        if r.vllm:
            status = "✅" if r.vllm.success else "❌"
            print(f"  vLLM: {status} {r.vllm.time_seconds:.2f}s")
            if r.vllm.success:
                print(f"    Provider: {r.vllm.parsed_provider}")
                print(f"    Indicators: {r.vllm.parsed_indicators}")
                print(f"    Data: {r.vllm.data_points} points" if r.vllm.data_fetched else "    Data: not fetched")
            else:
                print(f"    Error: {r.vllm.error}")


async def main():
    parser = argparse.ArgumentParser(description="Compare LLM providers for query parsing")
    parser.add_argument("--queries", type=int, default=None,
                        help="Limit number of queries to test")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip data fetching (faster, less thorough)")
    parser.add_argument("--vllm-url", default="http://localhost:8000",
                        help="vLLM server URL")
    parser.add_argument("--detailed", action="store_true",
                        help="Show detailed per-query results")
    args = parser.parse_args()

    queries = TEST_QUERIES[:args.queries] if args.queries else TEST_QUERIES

    print("=" * 80)
    print("LLM Provider Comparison: GPT-4o vs gpt-oss-120b (vLLM)")
    print("=" * 80)
    print(f"\nQueries: {len(queries)}")
    print(f"Fetch data: {not args.no_fetch}")

    results = await run_comparison(
        queries,
        fetch_data=not args.no_fetch,
        vllm_url=args.vllm_url
    )

    print_summary(results)

    if args.detailed:
        print_detailed_results(results)

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
