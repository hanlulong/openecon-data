#!/usr/bin/env python3
"""
Comprehensive test suite for Statistics Canada metadata discovery.
Tests diverse queries across multiple sectors to identify gaps in indicator coverage.
"""
import asyncio
import json
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.providers.statscan import StatsCanProvider
from backend.services.metadata_search import MetadataSearchService
from backend.services.llm import create_llm_provider


# Test queries covering diverse StatsCan indicators
TEST_QUERIES = [
    # Basic indicators (should work - hardcoded)
    {"query": "Canada GDP", "expected": "GDP", "category": "basic"},
    {"query": "Canada unemployment", "expected": "UNEMPLOYMENT", "category": "basic"},
    {"query": "Canada inflation", "expected": "INFLATION", "category": "basic"},
    {"query": "Canada housing starts", "expected": "HOUSING_STARTS", "category": "basic"},

    # Provincial trade/exports
    {"query": "Canada exports by province", "expected": "exports", "category": "trade", "complex": True},
    {"query": "Ontario exports", "expected": "exports", "category": "trade"},
    {"query": "British Columbia trade", "expected": "trade", "category": "trade"},

    # Provincial economic indicators
    {"query": "Saskatchewan agriculture production", "expected": "agriculture", "category": "agriculture"},
    {"query": "Alberta oil production", "expected": "oil", "category": "energy"},
    {"query": "Quebec manufacturing sales", "expected": "manufacturing", "category": "manufacturing"},

    # Fishing/natural resources by province
    {"query": "New Brunswick fishing industry", "expected": "fishing", "category": "fishing"},
    {"query": "Nova Scotia fishing exports", "expected": "fishing", "category": "fishing"},
    {"query": "British Columbia forestry", "expected": "forestry", "category": "forestry"},

    # Housing by province
    {"query": "Canada housing price index", "expected": "housing price", "category": "housing"},
    {"query": "Toronto housing price index", "expected": "housing price", "category": "housing"},
    {"query": "Vancouver housing starts", "expected": "housing starts", "category": "housing"},
    {"query": "Ontario housing price index", "expected": "housing price", "category": "housing"},

    # Manufacturing/sales
    {"query": "Ontario manufacturing sales", "expected": "manufacturing", "category": "manufacturing"},
    {"query": "Canada retail sales", "expected": "retail", "category": "retail"},
    {"query": "Quebec retail trade", "expected": "retail", "category": "retail"},

    # Employment by sector
    {"query": "Canada employment by industry", "expected": "employment", "category": "employment", "complex": True},
    {"query": "Ontario employment", "expected": "employment", "category": "employment"},

    # Demographic indicators
    {"query": "Manitoba population", "expected": "population", "category": "demographics"},
    {"query": "Saskatchewan population by age", "expected": "population", "category": "demographics"},

    # Construction
    {"query": "Canada building permits", "expected": "building permits", "category": "construction"},
    {"query": "Alberta construction", "expected": "construction", "category": "construction"},
]


async def test_query(provider, query_info):
    """Test a single query and return results."""
    query = query_info["query"]
    expected = query_info["expected"]
    category = query_info["category"]
    is_complex = query_info.get("complex", False)

    result = {
        "query": query,
        "expected": expected,
        "category": category,
        "is_complex": is_complex,
        "success": False,
        "error": None,
        "metadata": None,
    }

    try:
        # Try search_vectors first to see what's available
        search_results = await provider.search_vectors(expected, limit=5)

        if search_results:
            result["search_results"] = [
                {
                    "productId": r.get("productId"),
                    "title": r.get("title", "")[:100],
                    "archived": r.get("archived"),
                    "endDate": r.get("endDate"),
                }
                for r in search_results[:3]
            ]

        # For complex queries, just verify search works
        if is_complex:
            if search_results:
                result["success"] = True
                result["metadata"] = {
                    "note": "Complex query - search found relevant cubes",
                    "cube_count": len(search_results)
                }
            else:
                result["error"] = "No search results found"
        else:
            # For simple queries, try to actually fetch data
            # This will test the full discovery pipeline
            indicator = expected.upper().replace(" ", "_")

            try:
                # Try direct fetch first
                data = await provider.fetch_series({"indicator": indicator, "periods": 12})
                result["success"] = True
                result["metadata"] = {
                    "indicator": data.metadata.indicator,
                    "frequency": data.metadata.frequency,
                    "unit": data.metadata.unit,
                    "data_points": len(data.data),
                }
            except Exception as fetch_error:
                # If direct fetch fails, try dynamic discovery
                try:
                    data = await provider.fetch_dynamic_data({
                        "indicator": indicator,
                        "periods": 12
                    })
                    result["success"] = True
                    result["metadata"] = {
                        "indicator": data.metadata.indicator,
                        "frequency": data.metadata.frequency,
                        "unit": data.metadata.unit,
                        "data_points": len(data.data),
                        "method": "dynamic_discovery"
                    }
                except Exception as dynamic_error:
                    result["error"] = f"Fetch failed: {str(fetch_error)}, Dynamic failed: {str(dynamic_error)}"

    except Exception as e:
        result["error"] = str(e)

    return result


async def main():
    """Run comprehensive test suite."""
    print("=" * 80)
    print("STATISTICS CANADA COMPREHENSIVE METADATA DISCOVERY TEST")
    print("=" * 80)
    print()

    # Initialize provider with metadata search
    llm_provider = create_llm_provider()
    metadata_search = MetadataSearchService(llm_provider)
    provider = StatsCanProvider(metadata_search_service=metadata_search)

    # Run tests
    results = []
    for i, query_info in enumerate(TEST_QUERIES, 1):
        print(f"[{i}/{len(TEST_QUERIES)}] Testing: {query_info['query']}")
        result = await test_query(provider, query_info)
        results.append(result)

        if result["success"]:
            print(f"  ✅ SUCCESS")
            if result["metadata"]:
                print(f"     {json.dumps(result['metadata'], indent=6)}")
        else:
            print(f"  ❌ FAILED: {result['error']}")

        if result.get("search_results"):
            print(f"  📊 Found {len(result['search_results'])} search results:")
            for sr in result["search_results"]:
                print(f"     - {sr['productId']}: {sr['title']}")

        print()

    # Summary statistics
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    total = len(results)
    successful = sum(1 for r in results if r["success"])
    failed = total - successful

    print(f"Total queries: {total}")
    print(f"Successful: {successful} ({successful/total*100:.1f}%)")
    print(f"Failed: {failed} ({failed/total*100:.1f}%)")
    print()

    # Breakdown by category
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"total": 0, "success": 0}
        categories[cat]["total"] += 1
        if r["success"]:
            categories[cat]["success"] += 1

    print("BY CATEGORY:")
    for cat, stats in sorted(categories.items()):
        success_rate = stats["success"] / stats["total"] * 100
        status = "✅" if success_rate >= 80 else "⚠️" if success_rate >= 50 else "❌"
        print(f"  {status} {cat:20s}: {stats['success']}/{stats['total']} ({success_rate:.1f}%)")
    print()

    # Failed queries
    failed_queries = [r for r in results if not r["success"]]
    if failed_queries:
        print("FAILED QUERIES:")
        for r in failed_queries:
            print(f"  ❌ {r['query']}")
            print(f"     Error: {r['error']}")
            if r.get("search_results"):
                print(f"     Search found: {len(r['search_results'])} cubes")
        print()

    # Save detailed results
    output_file = Path(__file__).parent / "statscan_test_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Detailed results saved to: {output_file}")

    # Metadata discovery gaps
    print("\nMETADATA DISCOVERY GAPS:")
    gaps = []
    for r in failed_queries:
        if r.get("search_results"):
            gaps.append({
                "query": r["query"],
                "category": r["category"],
                "issue": "Search found cubes but data fetch failed",
                "search_results": r["search_results"]
            })
        else:
            gaps.append({
                "query": r["query"],
                "category": r["category"],
                "issue": "No search results found",
            })

    if gaps:
        for gap in gaps:
            print(f"  🔍 {gap['query']} ({gap['category']})")
            print(f"     Issue: {gap['issue']}")
            if gap.get("search_results"):
                print(f"     Cubes found: {[r['productId'] for r in gap['search_results']]}")
    else:
        print("  ✅ No gaps detected!")

    print()
    return 0 if successful == total else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
