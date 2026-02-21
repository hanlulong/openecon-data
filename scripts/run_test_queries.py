#!/usr/bin/env python3
"""
Test Runner for econ-data-mcp Query Testing

Usage:
    python3 scripts/run_test_queries.py --start 1 --end 20
    python3 scripts/run_test_queries.py --category gdp
    python3 scripts/run_test_queries.py --all
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# Configuration
PRODUCTION_URL = "https://openecon.ai/api/query"
LOCAL_URL = "http://localhost:3001/api/query"
TIMEOUT = 60  # seconds per query


async def run_query(
    client: httpx.AsyncClient,
    query: str,
    query_id: int,
    url: str = PRODUCTION_URL,
) -> Dict[str, Any]:
    """Run a single query and return results."""
    start_time = time.time()
    result = {
        "id": query_id,
        "query": query,
        "success": False,
        "error": None,
        "data_returned": False,
        "provider": None,
        "data_points": 0,
        "response_time_ms": 0,
        "timestamp": datetime.now().isoformat(),
    }

    try:
        response = await client.post(
            url,
            json={"query": query},
            timeout=TIMEOUT,
        )
        elapsed = (time.time() - start_time) * 1000
        result["response_time_ms"] = round(elapsed, 2)

        if response.status_code == 200:
            data = response.json()
            result["success"] = True
            result["data_returned"] = bool(data.get("data"))

            if data.get("data"):
                first_data = data["data"][0] if isinstance(data["data"], list) else data["data"]
                if isinstance(first_data, dict):
                    metadata = first_data.get("metadata", {})
                    result["provider"] = metadata.get("source", "UNKNOWN")
                    result["indicator"] = metadata.get("indicator", "")
                    result["country"] = metadata.get("country", "")
                    result["data_points"] = len(first_data.get("data", []))
                    result["series_count"] = len(data["data"]) if isinstance(data["data"], list) else 1

            if data.get("error"):
                result["success"] = False
                result["error"] = data["error"]

            if data.get("clarificationNeeded"):
                result["clarification_needed"] = True
                result["clarification_questions"] = data.get("clarificationQuestions", [])
        else:
            result["error"] = f"HTTP {response.status_code}: {response.text[:200]}"

    except httpx.TimeoutException:
        result["error"] = "Request timed out"
        result["response_time_ms"] = TIMEOUT * 1000
    except Exception as e:
        result["error"] = str(e)
        result["response_time_ms"] = (time.time() - start_time) * 1000

    return result


async def run_batch(
    queries: List[Dict],
    url: str = PRODUCTION_URL,
    concurrent_limit: int = 3,
) -> List[Dict[str, Any]]:
    """Run a batch of queries with concurrency limit."""
    results = []
    semaphore = asyncio.Semaphore(concurrent_limit)

    async with httpx.AsyncClient() as client:
        async def run_with_semaphore(q):
            async with semaphore:
                print(f"  Testing query {q['id']}: {q['query'][:50]}...")
                return await run_query(client, q["query"], q["id"], url)

        tasks = [run_with_semaphore(q) for q in queries]
        results = await asyncio.gather(*tasks)

    return results


def load_queries(filepath: str = "test_queries_100.json") -> List[Dict]:
    """Load test queries from JSON file."""
    with open(filepath, "r") as f:
        data = json.load(f)
    return data["queries"]


def filter_queries(
    queries: List[Dict],
    start: Optional[int] = None,
    end: Optional[int] = None,
    category: Optional[str] = None,
) -> List[Dict]:
    """Filter queries by ID range or category."""
    filtered = queries

    if category:
        filtered = [q for q in filtered if q.get("category") == category or category in q.get("category", "")]

    if start is not None:
        filtered = [q for q in filtered if q["id"] >= start]

    if end is not None:
        filtered = [q for q in filtered if q["id"] <= end]

    return filtered


def generate_report(results: List[Dict[str, Any]], output_file: str = None) -> str:
    """Generate a markdown report from test results."""
    total = len(results)
    passed = sum(1 for r in results if r["success"] and r["data_returned"])
    failed = sum(1 for r in results if not r["success"])
    no_data = sum(1 for r in results if r["success"] and not r["data_returned"])

    # Group by category
    by_category = {}
    for r in results:
        cat = r.get("category", "unknown")
        if cat not in by_category:
            by_category[cat] = {"passed": 0, "failed": 0, "no_data": 0, "total": 0}
        by_category[cat]["total"] += 1
        if r["success"] and r["data_returned"]:
            by_category[cat]["passed"] += 1
        elif not r["success"]:
            by_category[cat]["failed"] += 1
        else:
            by_category[cat]["no_data"] += 1

    # Group by provider
    by_provider = {}
    for r in results:
        prov = r.get("provider") or "NONE"
        if prov not in by_provider:
            by_provider[prov] = 0
        by_provider[prov] += 1

    report = f"""# econ-data-mcp Test Results - {datetime.now().strftime('%Y-%m-%d %H:%M')}

## Summary
| Metric | Count | Percentage |
|--------|-------|------------|
| **Total Queries** | {total} | 100% |
| **Passed (with data)** | {passed} | {passed/total*100:.1f}% |
| **Failed (error)** | {failed} | {failed/total*100:.1f}% |
| **No Data Returned** | {no_data} | {no_data/total*100:.1f}% |

## Results by Category
| Category | Passed | Failed | No Data | Total | Pass Rate |
|----------|--------|--------|---------|-------|-----------|
"""
    for cat, stats in sorted(by_category.items()):
        pass_rate = stats["passed"] / stats["total"] * 100 if stats["total"] > 0 else 0
        report += f"| {cat} | {stats['passed']} | {stats['failed']} | {stats['no_data']} | {stats['total']} | {pass_rate:.1f}% |\n"

    report += f"""
## Results by Provider
| Provider | Queries Served |
|----------|----------------|
"""
    for prov, count in sorted(by_provider.items(), key=lambda x: -x[1]):
        report += f"| {prov} | {count} |\n"

    # Failed queries details
    failed_queries = [r for r in results if not r["success"] or not r["data_returned"]]
    if failed_queries:
        report += f"""
## Failed/No Data Queries ({len(failed_queries)})
| ID | Query | Error | Response Time |
|----|-------|-------|---------------|
"""
        for r in failed_queries[:30]:  # Limit to first 30
            query_short = r["query"][:40] + "..." if len(r["query"]) > 40 else r["query"]
            error_short = (r.get("error") or "No data")[:50]
            report += f"| {r['id']} | {query_short} | {error_short} | {r['response_time_ms']:.0f}ms |\n"

    # Performance stats
    response_times = [r["response_time_ms"] for r in results if r["response_time_ms"] > 0]
    if response_times:
        avg_time = sum(response_times) / len(response_times)
        max_time = max(response_times)
        min_time = min(response_times)
        report += f"""
## Performance
| Metric | Value |
|--------|-------|
| Average Response Time | {avg_time:.0f}ms |
| Max Response Time | {max_time:.0f}ms |
| Min Response Time | {min_time:.0f}ms |
"""

    if output_file:
        with open(output_file, "w") as f:
            f.write(report)
        print(f"Report saved to {output_file}")

    return report


async def main():
    parser = argparse.ArgumentParser(description="Run econ-data-mcp test queries")
    parser.add_argument("--start", type=int, help="Start query ID")
    parser.add_argument("--end", type=int, help="End query ID")
    parser.add_argument("--category", type=str, help="Filter by category")
    parser.add_argument("--all", action="store_true", help="Run all queries")
    parser.add_argument("--local", action="store_true", help="Test against local server")
    parser.add_argument("--concurrent", type=int, default=3, help="Concurrent queries")
    parser.add_argument("--output", type=str, help="Output report file")
    args = parser.parse_args()

    # Load queries
    queries = load_queries()

    # Filter queries
    if not args.all:
        queries = filter_queries(queries, args.start, args.end, args.category)

    if not queries:
        print("No queries to run. Use --all, --start/--end, or --category")
        sys.exit(1)

    url = LOCAL_URL if args.local else PRODUCTION_URL
    print(f"Running {len(queries)} queries against {url}")
    print(f"Concurrent limit: {args.concurrent}")
    print("-" * 60)

    # Attach category to queries for reporting
    query_map = {q["id"]: q for q in load_queries()}
    for q in queries:
        q["_category"] = query_map.get(q["id"], {}).get("category", "unknown")

    # Run tests
    results = await run_batch(queries, url, args.concurrent)

    # Attach category to results
    for r in results:
        r["category"] = query_map.get(r["id"], {}).get("category", "unknown")

    # Generate report
    output_file = args.output or f"test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report = generate_report(results, output_file)

    print("\n" + "=" * 60)
    print(report)

    # Save raw results
    json_output = output_file.replace(".md", ".json")
    with open(json_output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Raw results saved to {json_output}")


if __name__ == "__main__":
    asyncio.run(main())
