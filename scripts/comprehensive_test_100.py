#!/usr/bin/env python3
"""
Comprehensive Test Suite - 100+ Diverse Queries
Based on TESTING_PROMPT.md requirements

Last Updated: 2025-12-26
Infrastructure improvements tested:
- NaN/infinity sanitization in DataPoint model
- HS code parsing in Comtrade provider
- Province decomposition handling
- Supabase constraint violation fixes
"""

import asyncio
import aiohttp
import json
import time
from datetime import datetime
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass, field, asdict
import sys

# Test configuration
API_URL = "http://localhost:3001/api/query"
DEFAULT_TIMEOUT = 60  # seconds per query
EXTENDED_TIMEOUT = 90  # for multi-country queries

# Expected failures - queries that fail due to external factors (not infrastructure)
EXPECTED_FAILURES: Set[str] = {
    # Ranking queries require Pro Mode with complex data processing
    "GDP per capita ranking top 10",
}

# Queries that need extended timeout (multi-country aggregations)
EXTENDED_TIMEOUT_QUERIES: Set[str] = {
    "ASEAN economic indicators",
    "Latin America inflation",
    "Nordic countries unemployment",
    "EU GDP growth rates",
}

@dataclass
class TestResult:
    query: str
    category: str
    subcategory: str
    status: str  # "pass", "fail", "error", "timeout", "expected_fail"
    provider: Optional[str] = None
    data_points: int = 0
    response_time: float = 0.0
    error_message: Optional[str] = None
    indicator_code: Optional[str] = None
    country: Optional[str] = None
    expected_failure: bool = False  # True if failure was expected

# =============================================================================
# TEST QUERIES - 100+ DIVERSE QUERIES
# =============================================================================

ECONOMIC_INDICATORS = [
    # GDP queries (10)
    ("US GDP for the last 5 years", "gdp", "usa"),
    ("Germany GDP growth rate", "gdp", "germany"),
    ("Japan real GDP quarterly", "gdp", "japan"),
    ("China GDP per capita", "gdp", "china"),
    ("UK nominal GDP 2020-2023", "gdp", "uk"),
    ("Brazil GDP annual growth", "gdp", "brazil"),
    ("India GDP in current USD", "gdp", "india"),
    ("France GDP deflator", "gdp", "france"),
    ("Australia GDP composition", "gdp", "australia"),
    ("Mexico real GDP growth", "gdp", "mexico"),

    # Unemployment (8)
    ("US unemployment rate", "unemployment", "usa"),
    ("Germany unemployment last 10 years", "unemployment", "germany"),
    ("Japan unemployment trend", "unemployment", "japan"),
    ("France jobless rate", "unemployment", "france"),
    ("Canada unemployment by province", "unemployment", "canada"),
    ("Spain youth unemployment", "unemployment", "spain"),
    ("Italy labor market", "unemployment", "italy"),
    ("South Korea unemployment statistics", "unemployment", "korea"),

    # Inflation (8)
    ("US inflation rate CPI", "inflation", "usa"),
    ("UK inflation 2023", "inflation", "uk"),
    ("Germany HICP inflation", "inflation", "germany"),
    ("Japan CPI monthly", "inflation", "japan"),
    ("Brazil inflation history", "inflation", "brazil"),
    ("Turkey inflation rate", "inflation", "turkey"),
    ("Argentina consumer prices", "inflation", "argentina"),
    ("India WPI inflation", "inflation", "india"),

    # Interest Rates (7)
    ("Federal funds rate", "interest_rate", "usa"),
    ("ECB interest rate", "interest_rate", "eurozone"),
    ("Bank of Japan policy rate", "interest_rate", "japan"),
    ("UK bank rate history", "interest_rate", "uk"),
    ("US 10 year treasury yield", "interest_rate", "usa"),
    ("Yield curve spread 10y 2y", "interest_rate", "usa"),
    ("SOFR rate", "interest_rate", "usa"),  # Replaced LIBOR (discontinued 2023)

    # Government/Fiscal (7)
    ("US government debt to GDP", "fiscal", "usa"),
    ("Japan fiscal deficit", "fiscal", "japan"),
    ("Germany government spending", "fiscal", "germany"),
    ("France public debt", "fiscal", "france"),
    ("Italy budget deficit", "fiscal", "italy"),
    ("Greece sovereign debt", "fiscal", "greece"),
    ("US tax revenue", "fiscal", "usa"),
]

TRADE_FLOWS = [
    # Bilateral trade (8)
    ("US exports to China", "bilateral", "us-china"),
    ("Germany imports from France", "bilateral", "germany-france"),
    ("Japan trade with Korea", "bilateral", "japan-korea"),
    ("UK exports to EU", "bilateral", "uk-eu"),
    ("China exports to US 2023", "bilateral", "china-us"),
    ("Canada US trade balance", "bilateral", "canada-us"),
    ("Mexico exports to United States", "bilateral", "mexico-us"),
    ("India trade with China", "bilateral", "india-china"),

    # Commodity trade (6)
    ("US oil imports", "commodity", "usa"),
    ("China semiconductor imports", "commodity", "china"),
    ("Germany auto exports", "commodity", "germany"),
    ("Brazil soybean exports", "commodity", "brazil"),
    ("Saudi Arabia oil exports", "commodity", "saudi"),
    ("Japan electronics exports", "commodity", "japan"),

    # HS code trade (6)
    ("HS 8703 automobile trade", "hs_code", "global"),
    ("Chapter 27 petroleum imports US", "hs_code", "usa"),
    ("HS 8542 semiconductor trade China", "hs_code", "china"),
    ("HS 1001 wheat exports", "hs_code", "global"),
    ("Pharmaceutical exports HS 30", "hs_code", "global"),
    ("Machinery imports HS 84", "hs_code", "global"),
]

FINANCIAL_DATA = [
    # Exchange rates (8)
    ("EUR USD exchange rate", "forex", "eur-usd"),
    ("Japanese yen to dollar", "forex", "jpy-usd"),
    ("British pound exchange rate", "forex", "gbp-usd"),
    ("Chinese yuan renminbi rate", "forex", "cny-usd"),
    ("Swiss franc exchange rate history", "forex", "chf-usd"),
    ("Indian rupee to USD", "forex", "inr-usd"),
    ("Brazilian real exchange rate", "forex", "brl-usd"),
    ("Euro to Japanese yen", "forex", "eur-jpy"),

    # Crypto (6)
    ("Bitcoin price USD", "crypto", "btc"),
    ("Ethereum price history", "crypto", "eth"),
    ("Bitcoin market cap", "crypto", "btc"),
    ("Dogecoin price", "crypto", "doge"),
    ("Solana price trend", "crypto", "sol"),
    ("XRP Ripple price", "crypto", "xrp"),

    # Credit/Banking (6)
    ("US private credit to GDP", "credit", "usa"),
    ("China credit gap", "credit", "china"),
    ("Global credit conditions", "credit", "global"),
    ("Bank lending rates US", "credit", "usa"),
    ("Non-performing loans ratio", "credit", "global"),
    ("Money supply M2 growth", "credit", "usa"),
]

MULTI_COUNTRY = [
    # G7 (3)
    ("G7 GDP comparison", "regional", "g7"),
    ("G7 unemployment rates", "regional", "g7"),
    ("G7 inflation comparison", "regional", "g7"),

    # BRICS (3)
    ("BRICS countries GDP", "regional", "brics"),
    ("BRICS economic growth", "regional", "brics"),
    ("BRICS inflation rates", "regional", "brics"),

    # Regional (4)
    ("EU GDP growth rates", "regional", "eu"),
    ("ASEAN economic indicators", "regional", "asean"),
    ("Nordic countries unemployment", "regional", "nordic"),
    ("Latin America inflation", "regional", "latam"),
]

COMPLEX_QUERIES = [
    # Comparative (4)
    ("Compare US and China GDP growth", "comparative", "us-china"),
    ("Which country has highest unemployment in EU", "comparative", "eu"),
    ("Japan vs Germany inflation comparison", "comparative", "japan-germany"),
    ("Compare Fed rate to ECB rate", "comparative", "us-eu"),

    # Trend/Historical (3)
    ("How has US GDP changed since 2008", "trend", "usa"),
    ("Historical inflation during COVID", "trend", "global"),
    ("Unemployment trend after 2008 crisis", "trend", "usa"),

    # Calculations (3)
    ("US trade deficit calculation", "calculation", "usa"),
    ("GDP per capita ranking top 10", "calculation", "global"),
    ("Debt to GDP ratio comparison", "calculation", "global"),
]

EDGE_CASES = [
    # Ambiguous (3)
    ("Korea GDP", "edge", "ambiguous"),  # North or South?
    ("Congo inflation", "edge", "ambiguous"),  # Which Congo?
    ("growth rate", "edge", "ambiguous"),  # GDP? Population?

    # Abbreviations (3)
    ("fed rate", "edge", "abbreviation"),
    ("forex EUR JPY", "edge", "abbreviation"),
    ("crypto BTC", "edge", "abbreviation"),

    # Natural language (4)
    ("what's the unemployment in germany", "edge", "natural"),
    ("show me us gdp", "edge", "natural"),
    ("I need inflation data for japan", "edge", "natural"),
    ("can you get me brazil trade data", "edge", "natural"),
]

def get_all_queries() -> List[tuple]:
    """Combine all query categories"""
    all_queries = []

    for q, subcat, country in ECONOMIC_INDICATORS:
        all_queries.append((q, "economic_indicators", subcat, country))

    for q, subcat, country in TRADE_FLOWS:
        all_queries.append((q, "trade_flows", subcat, country))

    for q, subcat, country in FINANCIAL_DATA:
        all_queries.append((q, "financial_data", subcat, country))

    for q, subcat, country in MULTI_COUNTRY:
        all_queries.append((q, "multi_country", subcat, country))

    for q, subcat, country in COMPLEX_QUERIES:
        all_queries.append((q, "complex", subcat, country))

    for q, subcat, country in EDGE_CASES:
        all_queries.append((q, "edge_cases", subcat, country))

    return all_queries


async def run_query(session: aiohttp.ClientSession, query: str, category: str,
                   subcategory: str, country: str) -> TestResult:
    """Execute a single query and return results"""
    start_time = time.time()

    # Use extended timeout for multi-country queries
    timeout = EXTENDED_TIMEOUT if query in EXTENDED_TIMEOUT_QUERIES else DEFAULT_TIMEOUT
    is_expected_failure = query in EXPECTED_FAILURES

    try:
        async with session.post(
            API_URL,
            json={"query": query},
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            elapsed = time.time() - start_time

            if response.status != 200:
                status = "expected_fail" if is_expected_failure else "error"
                return TestResult(
                    query=query,
                    category=category,
                    subcategory=subcategory,
                    status=status,
                    response_time=elapsed,
                    error_message=f"HTTP {response.status}",
                    country=country,
                    expected_failure=is_expected_failure
                )

            data = await response.json()

            # Check for errors in response
            if data.get("error"):
                status = "expected_fail" if is_expected_failure else "fail"
                return TestResult(
                    query=query,
                    category=category,
                    subcategory=subcategory,
                    status=status,
                    response_time=elapsed,
                    error_message=data.get("error"),
                    country=country,
                    expected_failure=is_expected_failure
                )

            # Extract info from response
            provider = None
            data_points = 0
            indicator_code = None

            if data.get("intent"):
                provider = data["intent"].get("apiProvider")
                indicators = data["intent"].get("indicators", [])
                if indicators:
                    indicator_code = indicators[0] if isinstance(indicators[0], str) else None

            if data.get("data"):
                for series in data["data"]:
                    if series.get("data"):
                        data_points += len(series["data"])

            # Determine pass/fail
            if data_points > 0:
                status = "pass"
            elif is_expected_failure:
                status = "expected_fail"
            else:
                status = "fail"

            return TestResult(
                query=query,
                category=category,
                subcategory=subcategory,
                status=status,
                provider=provider,
                data_points=data_points,
                response_time=elapsed,
                indicator_code=indicator_code,
                country=country,
                error_message=None if status == "pass" else "No data returned",
                expected_failure=is_expected_failure
            )

    except asyncio.TimeoutError:
        status = "expected_fail" if is_expected_failure else "timeout"
        return TestResult(
            query=query,
            category=category,
            subcategory=subcategory,
            status=status,
            response_time=timeout,
            error_message=f"Timeout after {timeout}s",
            country=country,
            expected_failure=is_expected_failure
        )
    except Exception as e:
        status = "expected_fail" if is_expected_failure else "error"
        return TestResult(
            query=query,
            category=category,
            subcategory=subcategory,
            status=status,
            response_time=time.time() - start_time,
            error_message=str(e),
            country=country,
            expected_failure=is_expected_failure
        )


async def run_category_tests(queries: List[tuple], batch_size: int = 5) -> List[TestResult]:
    """Run tests for a category with rate limiting"""
    results = []

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0, len(queries), batch_size):
            batch = queries[i:i+batch_size]
            tasks = [
                run_query(session, q, cat, subcat, country)
                for q, cat, subcat, country in batch
            ]
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)

            # Rate limiting between batches
            if i + batch_size < len(queries):
                await asyncio.sleep(1)

    return results


def print_summary(results: List[TestResult]):
    """Print test summary"""
    total = len(results)
    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    errors = sum(1 for r in results if r.status == "error")
    timeouts = sum(1 for r in results if r.status == "timeout")
    expected_fails = sum(1 for r in results if r.status == "expected_fail")

    # Calculate effective pass rate (excluding expected failures)
    effective_total = total - expected_fails
    effective_pass_rate = 100 * passed / effective_total if effective_total > 0 else 0

    print("\n" + "="*80)
    print("COMPREHENSIVE TEST RESULTS")
    print("="*80)
    print(f"\nTotal Queries: {total}")
    print(f"Passed: {passed} ({100*passed/total:.1f}%)")
    print(f"Failed: {failed} ({100*failed/total:.1f}%)")
    print(f"Errors: {errors} ({100*errors/total:.1f}%)")
    print(f"Timeouts: {timeouts} ({100*timeouts/total:.1f}%)")
    print(f"Expected Failures: {expected_fails}")
    print(f"\nEffective Pass Rate: {passed}/{effective_total} ({effective_pass_rate:.1f}%)")

    # By category
    print("\n" + "-"*40)
    print("Results by Category:")
    print("-"*40)

    categories = {}
    for r in results:
        if r.category not in categories:
            categories[r.category] = {"pass": 0, "fail": 0, "error": 0, "timeout": 0, "expected_fail": 0, "total": 0}
        categories[r.category][r.status] += 1
        categories[r.category]["total"] += 1

    for cat, stats in sorted(categories.items()):
        rate = 100 * stats["pass"] / stats["total"] if stats["total"] > 0 else 0
        print(f"  {cat:25s}: {stats['pass']:3d}/{stats['total']:3d} ({rate:5.1f}%)")

    # By provider
    print("\n" + "-"*40)
    print("Results by Provider:")
    print("-"*40)

    providers = {}
    for r in results:
        prov = r.provider or "unknown"
        if prov not in providers:
            providers[prov] = {"pass": 0, "fail": 0, "error": 0, "timeout": 0, "expected_fail": 0, "total": 0}
        providers[prov][r.status] += 1
        providers[prov]["total"] += 1

    for prov, stats in sorted(providers.items()):
        rate = 100 * stats["pass"] / stats["total"] if stats["total"] > 0 else 0
        print(f"  {prov:20s}: {stats['pass']:3d}/{stats['total']:3d} ({rate:5.1f}%)")

    # Expected failures
    expected_failure_queries = [r for r in results if r.status == "expected_fail"]
    if expected_failure_queries:
        print("\n" + "-"*40)
        print(f"Expected Failures ({len(expected_failure_queries)}):")
        print("-"*40)
        for r in expected_failure_queries:
            print(f"  [expected ] [{r.category:20s}] {r.query[:50]}")
            if r.error_message:
                print(f"             Reason: {r.error_message[:60]}")

    # Unexpected failures
    failed_queries = [r for r in results if r.status not in ("pass", "expected_fail")]
    if failed_queries:
        print("\n" + "-"*40)
        print(f"Unexpected Failed/Error Queries ({len(failed_queries)}):")
        print("-"*40)
        for r in failed_queries[:30]:  # Limit output
            print(f"  [{r.status:7s}] [{r.category:20s}] {r.query[:50]}")
            if r.error_message:
                print(f"           Error: {r.error_message[:60]}")

    # Average response times
    print("\n" + "-"*40)
    print("Average Response Times:")
    print("-"*40)

    for cat in sorted(categories.keys()):
        cat_results = [r for r in results if r.category == cat and r.status == "pass"]
        if cat_results:
            avg = sum(r.response_time for r in cat_results) / len(cat_results)
            print(f"  {cat:25s}: {avg:.2f}s")


async def main():
    """Main test runner"""
    print("="*80)
    print("econ-data-mcp Comprehensive Testing Suite")
    print(f"Started: {datetime.now().isoformat()}")
    print("="*80)

    # Get all queries
    all_queries = get_all_queries()
    print(f"\nTotal queries to test: {len(all_queries)}")

    # Run tests
    print("\nRunning tests...")
    start = time.time()

    results = await run_category_tests(all_queries, batch_size=5)

    elapsed = time.time() - start
    print(f"\nCompleted in {elapsed:.1f}s")

    # Print summary
    print_summary(results)

    # Calculate statistics
    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    errors = sum(1 for r in results if r.status == "error")
    timeouts = sum(1 for r in results if r.status == "timeout")
    expected_fails = sum(1 for r in results if r.status == "expected_fail")
    effective_total = len(all_queries) - expected_fails

    # Save results to JSON
    output_file = f"/home/hanlulong/econ-data-mcp/docs/testing/test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results_data = {
        "timestamp": datetime.now().isoformat(),
        "total_queries": len(all_queries),
        "duration_seconds": elapsed,
        "summary": {
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "timeouts": timeouts,
            "expected_failures": expected_fails,
            "effective_total": effective_total,
            "effective_pass_rate": round(100 * passed / effective_total, 1) if effective_total > 0 else 0
        },
        "results": [asdict(r) for r in results]
    }

    with open(output_file, "w") as f:
        json.dump(results_data, f, indent=2)
    print(f"\nResults saved to: {output_file}")

    # Return exit code based on effective pass rate (excluding expected failures)
    passed = sum(1 for r in results if r.status == "pass")
    expected_fails = sum(1 for r in results if r.status == "expected_fail")
    effective_total = len(results) - expected_fails
    effective_pass_rate = passed / effective_total if effective_total > 0 else 0
    return 0 if effective_pass_rate >= 0.95 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
