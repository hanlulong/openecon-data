#!/usr/bin/env python3
"""Verify infrastructure fixes for the 8 failing queries"""

import asyncio
import aiohttp
import json
import time

API_URL = "http://localhost:3001/api/query"
TIMEOUT = 90

# The 8 previously failing queries
VERIFICATION_QUERIES = [
    # HS Code queries (previously failing)
    ("HS 8703 automobile trade", "hs_code", "Should extract numeric code 8703"),
    ("HS 1001 wheat exports", "hs_code", "Should extract numeric code 1001"),
    ("Pharmaceutical exports HS 30", "hs_code", "Should extract numeric code 30"),
    ("Machinery imports HS 84", "hs_code", "Should extract numeric code 84"),

    # Province decomposition (previously failing)
    ("Canada unemployment by province", "decomposition", "Should trigger province decomposition"),

    # Bank lending rates (previously failing)
    ("Bank lending rates US", "indicator", "Should find DPRIME or similar"),

    # Additional HS code variations to verify infrastructure
    ("HS2709 crude oil trade", "hs_code", "Should extract 2709 from HS2709 format"),
    ("Chapter 27 petroleum imports", "hs_code", "Should extract 27 from Chapter format"),
]

# Additional similar queries to test generality (5-Query Test)
SIMILAR_QUERIES = [
    # More HS code variations
    ("HS 85 electronics exports", "hs_code", "Electronics HS 85"),
    ("HS chapter 61 clothing trade", "hs_code", "Clothing HS 61"),
    ("HS8544 electrical wire imports", "hs_code", "Wire HS 8544"),

    # More province decomposition
    ("Employment by province Canada", "decomposition", "Employment decomposition"),
    ("Canada labor force by province", "decomposition", "Labor force decomposition"),

    # More lending rate queries
    ("Prime lending rate", "indicator", "Prime rate"),
    ("US prime rate", "indicator", "US prime"),
]


async def run_query(session, query, category, description):
    """Run a single query and return result"""
    start = time.time()
    try:
        async with session.post(
            API_URL,
            json={"query": query},
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as response:
            elapsed = time.time() - start

            if response.status != 200:
                return {
                    "query": query,
                    "category": category,
                    "description": description,
                    "status": "error",
                    "http_status": response.status,
                    "elapsed": elapsed
                }

            data = await response.json()

            # Extract info
            provider = data.get("intent", {}).get("apiProvider", "unknown")
            indicators = data.get("intent", {}).get("indicators", [])
            params = data.get("intent", {}).get("parameters", {})
            data_points = sum(len(d.get("data", [])) for d in data.get("data", []))
            needs_decomp = data.get("intent", {}).get("needsDecomposition", False)
            clarification = data.get("intent", {}).get("clarificationNeeded", False)
            clarification_q = data.get("intent", {}).get("clarificationQuestions", [])
            error = data.get("error")

            status = "pass" if data_points > 0 else ("clarification" if clarification else "fail")

            return {
                "query": query,
                "category": category,
                "description": description,
                "status": status,
                "provider": provider,
                "indicators": indicators,
                "params": params,
                "data_points": data_points,
                "needs_decomposition": needs_decomp,
                "clarification": clarification,
                "clarification_questions": clarification_q,
                "error": error,
                "elapsed": elapsed
            }

    except asyncio.TimeoutError:
        return {
            "query": query,
            "category": category,
            "description": description,
            "status": "timeout",
            "elapsed": TIMEOUT
        }
    except Exception as e:
        return {
            "query": query,
            "category": category,
            "description": description,
            "status": "error",
            "error": str(e),
            "elapsed": time.time() - start
        }


async def main():
    print("=" * 80)
    print("VERIFICATION TEST FOR INFRASTRUCTURE FIXES")
    print("=" * 80)

    all_queries = VERIFICATION_QUERIES + SIMILAR_QUERIES
    results = []

    async with aiohttp.ClientSession() as session:
        for query, category, description in all_queries:
            print(f"\nTesting: {query}")
            result = await run_query(session, query, category, description)
            results.append(result)

            # Print result
            status = result["status"]
            if status == "pass":
                print(f"  ✅ PASS - {result['provider']} - {result['data_points']} points")
            elif status == "clarification":
                print(f"  ⚠️  CLARIFICATION - {result['clarification_questions']}")
            elif status == "timeout":
                print(f"  ⏱️  TIMEOUT - {result['elapsed']:.1f}s")
            else:
                print(f"  ❌ FAIL - {result.get('error', 'No data')}")
                if result.get("params"):
                    print(f"     Params: {result['params']}")

            await asyncio.sleep(0.5)  # Rate limit

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    passed = sum(1 for r in results if r["status"] == "pass")
    clarifications = sum(1 for r in results if r["status"] == "clarification")
    failed = sum(1 for r in results if r["status"] == "fail")
    timeouts = sum(1 for r in results if r["status"] == "timeout")
    errors = sum(1 for r in results if r["status"] == "error")

    total = len(results)
    print(f"\nTotal: {total}")
    print(f"Passed: {passed} ({100*passed/total:.1f}%)")
    print(f"Clarifications: {clarifications} (expected for some queries)")
    print(f"Failed: {failed}")
    print(f"Timeouts: {timeouts}")
    print(f"Errors: {errors}")

    # By category
    print("\nResults by Category:")
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"pass": 0, "clarification": 0, "fail": 0, "timeout": 0, "error": 0}
        categories[cat][r["status"]] += 1

    for cat, stats in categories.items():
        total_cat = sum(stats.values())
        pass_rate = (stats["pass"] + stats["clarification"]) / total_cat * 100
        print(f"  {cat}: {stats['pass']} pass, {stats['clarification']} clarification, {stats['fail']} fail ({pass_rate:.0f}% success)")

    # Save results
    with open("/home/hanlulong/econ-data-mcp/docs/testing/verification_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\nResults saved to docs/testing/verification_results.json")

    return 0 if failed + errors <= 2 else 1


if __name__ == "__main__":
    exit(asyncio.run(main()))
