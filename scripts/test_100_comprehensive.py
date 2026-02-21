#!/usr/bin/env python3
"""
Comprehensive 100-Query Test Suite for econ-data-mcp
Following TESTING_PROMPT.md guidelines:
- 40 Economic Indicators
- 20 Trade Flows
- 20 Financial Data
- 10 Multi-Country
- 10 Sequential/Complex
"""

import asyncio
import aiohttp
import json
import time
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from pathlib import Path
import argparse

# Configuration
API_BASE_URL = "https://openecon.ai"  # Production
# API_BASE_URL = "http://localhost:3001"  # Local dev

@dataclass
class TestResult:
    query: str
    category: str
    provider_expected: Optional[str]
    status: str  # "pass", "fail", "error", "timeout"
    provider_returned: Optional[str]
    has_data: bool
    data_points: int
    response_time_ms: float
    error_message: Optional[str]
    notes: Optional[str]

# ============================================================================
# ECONOMIC INDICATORS (40 queries)
# ============================================================================
ECONOMIC_INDICATORS = [
    # GDP queries (10)
    {"query": "US GDP for the last 5 years", "category": "gdp", "provider": "FRED"},
    {"query": "China GDP growth rate 2020-2024", "category": "gdp", "provider": "WorldBank"},
    {"query": "Japan real GDP quarterly", "category": "gdp", "provider": "IMF"},
    {"query": "Germany GDP per capita in euros", "category": "gdp", "provider": "Eurostat"},
    {"query": "India nominal GDP annual data", "category": "gdp", "provider": "WorldBank"},
    {"query": "Brazil GDP growth since 2015", "category": "gdp", "provider": "WorldBank"},
    {"query": "UK GDP seasonally adjusted", "category": "gdp", "provider": "IMF"},
    {"query": "France GDP in constant prices", "category": "gdp", "provider": "Eurostat"},
    {"query": "Canada GDP monthly", "category": "gdp", "provider": "StatsCan"},
    {"query": "Australia GDP annual growth", "category": "gdp", "provider": "WorldBank"},

    # Unemployment queries (8)
    {"query": "US unemployment rate monthly", "category": "unemployment", "provider": "FRED"},
    {"query": "Germany unemployment 2019-2024", "category": "unemployment", "provider": "Eurostat"},
    {"query": "Japan unemployment rate", "category": "unemployment", "provider": "IMF"},
    {"query": "Spain youth unemployment rate", "category": "unemployment", "provider": "Eurostat"},
    {"query": "Italy unemployment trend", "category": "unemployment", "provider": "Eurostat"},
    {"query": "Canada unemployment seasonally adjusted", "category": "unemployment", "provider": "StatsCan"},
    {"query": "South Africa unemployment rate", "category": "unemployment", "provider": "WorldBank"},
    {"query": "Mexico labor force participation", "category": "unemployment", "provider": "WorldBank"},

    # Inflation queries (8)
    {"query": "US CPI inflation monthly", "category": "inflation", "provider": "FRED"},
    {"query": "Eurozone HICP inflation", "category": "inflation", "provider": "Eurostat"},
    {"query": "UK inflation rate 2023-2024", "category": "inflation", "provider": "IMF"},
    {"query": "Japan core inflation", "category": "inflation", "provider": "IMF"},
    {"query": "Turkey inflation rate annual", "category": "inflation", "provider": "WorldBank"},
    {"query": "Argentina consumer price index", "category": "inflation", "provider": "IMF"},
    {"query": "India wholesale price index", "category": "inflation", "provider": "WorldBank"},
    {"query": "Brazil IPCA inflation", "category": "inflation", "provider": "WorldBank"},

    # Interest rates (6)
    {"query": "Federal Reserve interest rate history", "category": "interest_rate", "provider": "FRED"},
    {"query": "ECB main refinancing rate", "category": "interest_rate", "provider": "BIS"},
    {"query": "Bank of Japan policy rate", "category": "interest_rate", "provider": "BIS"},
    {"query": "US 10-year treasury yield", "category": "interest_rate", "provider": "FRED"},
    {"query": "US 2 year treasury constant maturity", "category": "interest_rate", "provider": "FRED"},
    {"query": "Bank of England base rate", "category": "interest_rate", "provider": "BIS"},

    # Other indicators (8)
    {"query": "US trade balance monthly", "category": "trade_balance", "provider": "FRED"},
    {"query": "China current account balance", "category": "trade_balance", "provider": "IMF"},
    {"query": "US government debt to GDP ratio", "category": "debt", "provider": "FRED"},
    {"query": "Japan public debt as percentage of GDP", "category": "debt", "provider": "IMF"},
    {"query": "Germany industrial production index", "category": "production", "provider": "Eurostat"},
    {"query": "US retail sales monthly change", "category": "consumption", "provider": "FRED"},
    {"query": "China foreign exchange reserves", "category": "reserves", "provider": "IMF"},
    {"query": "US housing starts monthly", "category": "housing", "provider": "FRED"},
]

# ============================================================================
# TRADE FLOWS (20 queries)
# ============================================================================
TRADE_FLOWS = [
    # Bilateral trade (8)
    {"query": "US exports to China 2023", "category": "bilateral_trade", "provider": "Comtrade"},
    {"query": "Germany imports from France", "category": "bilateral_trade", "provider": "Comtrade"},
    {"query": "Japan trade with South Korea", "category": "bilateral_trade", "provider": "Comtrade"},
    {"query": "UK exports to EU countries", "category": "bilateral_trade", "provider": "Comtrade"},
    {"query": "India imports from UAE", "category": "bilateral_trade", "provider": "Comtrade"},
    {"query": "Brazil exports to Argentina", "category": "bilateral_trade", "provider": "Comtrade"},
    {"query": "Canada trade with Mexico 2022-2024", "category": "bilateral_trade", "provider": "Comtrade"},
    {"query": "Australia exports to Japan", "category": "bilateral_trade", "provider": "Comtrade"},

    # Commodity trade (6)
    {"query": "US crude oil imports 2023", "category": "commodity_trade", "provider": "Comtrade"},
    {"query": "China semiconductor imports", "category": "commodity_trade", "provider": "Comtrade"},
    {"query": "Germany automobile exports", "category": "commodity_trade", "provider": "Comtrade"},
    {"query": "Brazil soybean exports", "category": "commodity_trade", "provider": "Comtrade"},
    {"query": "Russia natural gas exports", "category": "commodity_trade", "provider": "Comtrade"},
    {"query": "India pharmaceutical exports", "category": "commodity_trade", "provider": "Comtrade"},

    # HS code queries (6)
    {"query": "US imports HS 8703 motor vehicles", "category": "hs_code", "provider": "Comtrade"},
    {"query": "China exports chapter 85 electronics", "category": "hs_code", "provider": "Comtrade"},
    {"query": "Germany HS 3004 medicaments exports", "category": "hs_code", "provider": "Comtrade"},
    {"query": "Japan HS 8471 computer parts exports", "category": "hs_code", "provider": "Comtrade"},
    {"query": "UK chapter 27 energy imports", "category": "hs_code", "provider": "Comtrade"},
    {"query": "France HS 2204 wine exports", "category": "hs_code", "provider": "Comtrade"},
]

# ============================================================================
# FINANCIAL DATA (20 queries)
# ============================================================================
FINANCIAL_DATA = [
    # Exchange rates (10)
    {"query": "EUR to USD exchange rate", "category": "forex", "provider": "ExchangeRate"},
    {"query": "USD to JPY historical rates", "category": "forex", "provider": "ExchangeRate"},
    {"query": "GBP to EUR exchange rate 2024", "category": "forex", "provider": "ExchangeRate"},
    {"query": "USD to CNY yuan rate", "category": "forex", "provider": "ExchangeRate"},
    {"query": "Swiss franc to dollar rate", "category": "forex", "provider": "ExchangeRate"},
    {"query": "Australian dollar exchange rate", "category": "forex", "provider": "ExchangeRate"},
    {"query": "Canadian dollar to USD", "category": "forex", "provider": "ExchangeRate"},
    {"query": "Indian rupee to dollar rate", "category": "forex", "provider": "ExchangeRate"},
    {"query": "Brazilian real exchange rate", "category": "forex", "provider": "ExchangeRate"},
    {"query": "Mexican peso to USD", "category": "forex", "provider": "ExchangeRate"},

    # Cryptocurrency (6)
    {"query": "Bitcoin price USD", "category": "crypto", "provider": "CoinGecko"},
    {"query": "Ethereum price history 2024", "category": "crypto", "provider": "CoinGecko"},
    {"query": "Solana cryptocurrency price", "category": "crypto", "provider": "CoinGecko"},
    {"query": "XRP ripple current price", "category": "crypto", "provider": "CoinGecko"},
    {"query": "Dogecoin price in dollars", "category": "crypto", "provider": "CoinGecko"},
    {"query": "Cardano ADA price", "category": "crypto", "provider": "CoinGecko"},

    # Credit and banking (4)
    {"query": "US credit to GDP ratio", "category": "credit", "provider": "BIS"},
    {"query": "Global credit to private sector", "category": "credit", "provider": "BIS"},
    {"query": "US bank lending rate", "category": "banking", "provider": "FRED"},
    {"query": "Commercial bank interest spread", "category": "banking", "provider": "WorldBank"},
]

# ============================================================================
# MULTI-COUNTRY (10 queries)
# ============================================================================
MULTI_COUNTRY = [
    {"query": "G7 GDP growth comparison 2024", "category": "g7", "provider": None},
    {"query": "BRICS inflation rates", "category": "brics", "provider": None},
    {"query": "EU unemployment rates by country", "category": "eu", "provider": "Eurostat"},
    {"query": "ASEAN countries GDP 2023", "category": "regional", "provider": "WorldBank"},
    {"query": "Nordic countries GDP per capita", "category": "regional", "provider": "WorldBank"},
    {"query": "Top 10 economies by GDP", "category": "ranking", "provider": "WorldBank"},
    {"query": "Emerging markets inflation comparison", "category": "comparison", "provider": None},
    {"query": "Eurozone interest rates", "category": "eurozone", "provider": "BIS"},
    {"query": "G20 unemployment rates", "category": "g20", "provider": None},
    {"query": "Latin America GDP growth", "category": "regional", "provider": "WorldBank"},
]

# ============================================================================
# SEQUENTIAL/COMPLEX (10 queries)
# ============================================================================
SEQUENTIAL_COMPLEX = [
    {"query": "Compare US and China GDP over last decade", "category": "comparison", "provider": None},
    {"query": "Show unemployment trend during COVID pandemic", "category": "trend", "provider": None},
    {"query": "Calculate year-over-year inflation change for Germany", "category": "calculation", "provider": None},
    {"query": "Which country has highest GDP growth in 2023", "category": "ranking", "provider": None},
    {"query": "Compare pre and post Brexit UK trade", "category": "historical", "provider": None},
    {"query": "Show correlation between oil price and inflation", "category": "correlation", "provider": None},
    {"query": "US economic indicators during 2008 crisis", "category": "historical", "provider": "FRED"},
    {"query": "Japan deflation period analysis", "category": "analysis", "provider": None},
    {"query": "EU debt to GDP ratios ranked", "category": "ranking", "provider": None},
    {"query": "Global trade volume growth trend", "category": "global", "provider": None},
]

# Combine all queries
ALL_QUERIES = (
    ECONOMIC_INDICATORS +
    TRADE_FLOWS +
    FINANCIAL_DATA +
    MULTI_COUNTRY +
    SEQUENTIAL_COMPLEX
)

async def test_query(session: aiohttp.ClientSession, query_info: Dict, timeout: int = 60) -> TestResult:
    """Test a single query against the API."""
    query = query_info["query"]
    category = query_info["category"]
    expected_provider = query_info.get("provider")

    start_time = time.time()

    try:
        async with session.post(
            f"{API_BASE_URL}/api/query",
            json={"query": query},
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            elapsed_ms = (time.time() - start_time) * 1000

            if response.status != 200:
                return TestResult(
                    query=query,
                    category=category,
                    provider_expected=expected_provider,
                    status="error",
                    provider_returned=None,
                    has_data=False,
                    data_points=0,
                    response_time_ms=elapsed_ms,
                    error_message=f"HTTP {response.status}",
                    notes=None
                )

            data = await response.json()

            # Check for error
            if data.get("error"):
                return TestResult(
                    query=query,
                    category=category,
                    provider_expected=expected_provider,
                    status="error",
                    provider_returned=None,
                    has_data=False,
                    data_points=0,
                    response_time_ms=elapsed_ms,
                    error_message=data.get("error"),
                    notes=None
                )

            # Extract results from 'data' field (API response format)
            results = data.get("data", [])
            has_data = len(results) > 0
            provider_returned = None
            total_points = 0

            for result in results:
                # Get provider from metadata.source
                metadata = result.get("metadata", {})
                if metadata.get("source"):
                    provider_returned = metadata.get("source")
                points = result.get("data", [])
                total_points += len(points) if isinstance(points, list) else 0

            # Determine pass/fail
            status = "pass" if has_data and total_points > 0 else "fail"

            return TestResult(
                query=query,
                category=category,
                provider_expected=expected_provider,
                status=status,
                provider_returned=provider_returned,
                has_data=has_data,
                data_points=total_points,
                response_time_ms=elapsed_ms,
                error_message=None,
                notes=None
            )

    except asyncio.TimeoutError:
        elapsed_ms = (time.time() - start_time) * 1000
        return TestResult(
            query=query,
            category=category,
            provider_expected=expected_provider,
            status="timeout",
            provider_returned=None,
            has_data=False,
            data_points=0,
            response_time_ms=elapsed_ms,
            error_message=f"Timeout after {timeout}s",
            notes=None
        )
    except Exception as e:
        elapsed_ms = (time.time() - start_time) * 1000
        return TestResult(
            query=query,
            category=category,
            provider_expected=expected_provider,
            status="error",
            provider_returned=None,
            has_data=False,
            data_points=0,
            response_time_ms=elapsed_ms,
            error_message=str(e),
            notes=None
        )

async def run_tests(queries: List[Dict], concurrency: int = 5, timeout: int = 60) -> List[TestResult]:
    """Run all tests with limited concurrency."""
    results = []
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_test(session, query_info):
        async with semaphore:
            return await test_query(session, query_info, timeout)

    async with aiohttp.ClientSession() as session:
        tasks = [bounded_test(session, q) for q in queries]
        total = len(tasks)

        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            result = await coro
            results.append(result)
            status_emoji = {"pass": "✅", "fail": "❌", "error": "⚠️", "timeout": "⏱️"}.get(result.status, "?")
            print(f"[{i}/{total}] {status_emoji} {result.query[:60]}... ({result.response_time_ms:.0f}ms)")

    return results

def generate_report(results: List[TestResult]) -> Dict:
    """Generate a comprehensive test report."""
    # Summary statistics
    total = len(results)
    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    errors = sum(1 for r in results if r.status == "error")
    timeouts = sum(1 for r in results if r.status == "timeout")

    # By category
    by_category = {}
    for r in results:
        if r.category not in by_category:
            by_category[r.category] = {"total": 0, "pass": 0, "fail": 0, "error": 0, "timeout": 0}
        by_category[r.category]["total"] += 1
        by_category[r.category][r.status] += 1

    # By provider
    by_provider = {}
    for r in results:
        provider = r.provider_expected or "Unknown"
        if provider not in by_provider:
            by_provider[provider] = {"total": 0, "pass": 0, "fail": 0, "error": 0, "timeout": 0}
        by_provider[provider]["total"] += 1
        by_provider[provider][r.status] += 1

    # Failed queries
    failures = [asdict(r) for r in results if r.status != "pass"]

    # Average response time
    response_times = [r.response_time_ms for r in results]
    avg_response_time = sum(response_times) / len(response_times) if response_times else 0

    return {
        "timestamp": datetime.now().isoformat(),
        "api_base_url": API_BASE_URL,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "timeouts": timeouts,
            "pass_rate": f"{(passed/total)*100:.1f}%" if total > 0 else "N/A",
            "avg_response_time_ms": round(avg_response_time, 2)
        },
        "by_category": by_category,
        "by_provider": by_provider,
        "failures": failures,
        "all_results": [asdict(r) for r in results]
    }

def print_summary(report: Dict):
    """Print a formatted summary of the test results."""
    print("\n" + "="*80)
    print("TEST RESULTS SUMMARY")
    print("="*80)

    s = report["summary"]
    print(f"\nTotal Tests: {s['total']}")
    print(f"  ✅ Passed:   {s['passed']}")
    print(f"  ❌ Failed:   {s['failed']}")
    print(f"  ⚠️  Errors:   {s['errors']}")
    print(f"  ⏱️  Timeouts: {s['timeouts']}")
    print(f"\nPass Rate: {s['pass_rate']}")
    print(f"Avg Response Time: {s['avg_response_time_ms']:.0f}ms")

    print("\n" + "-"*40)
    print("BY CATEGORY:")
    print("-"*40)
    for cat, stats in sorted(report["by_category"].items()):
        rate = (stats['pass'] / stats['total'] * 100) if stats['total'] > 0 else 0
        print(f"  {cat:25s}: {stats['pass']}/{stats['total']} ({rate:.0f}%)")

    print("\n" + "-"*40)
    print("BY PROVIDER:")
    print("-"*40)
    for prov, stats in sorted(report["by_provider"].items()):
        rate = (stats['pass'] / stats['total'] * 100) if stats['total'] > 0 else 0
        print(f"  {prov:20s}: {stats['pass']}/{stats['total']} ({rate:.0f}%)")

    if report["failures"]:
        print("\n" + "-"*40)
        print(f"FAILED QUERIES ({len(report['failures'])}):")
        print("-"*40)
        for f in report["failures"][:20]:  # Show first 20
            print(f"  [{f['status']}] {f['query'][:60]}...")
            if f['error_message']:
                print(f"       Error: {f['error_message'][:60]}")

async def main():
    parser = argparse.ArgumentParser(description="Run comprehensive econ-data-mcp tests")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrent requests")
    parser.add_argument("--timeout", type=int, default=60, help="Request timeout in seconds")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file path")
    parser.add_argument("--category", type=str, default=None, help="Test only specific category")
    parser.add_argument("--local", action="store_true", help="Test against localhost:3001")
    args = parser.parse_args()

    global API_BASE_URL
    if args.local:
        API_BASE_URL = "http://localhost:3001"

    queries = ALL_QUERIES
    if args.category:
        queries = [q for q in ALL_QUERIES if q["category"] == args.category]
        print(f"Filtering to category: {args.category} ({len(queries)} queries)")

    print(f"\n🧪 econ-data-mcp Comprehensive Test Suite")
    print(f"📍 API: {API_BASE_URL}")
    print(f"📊 Running {len(queries)} queries (concurrency: {args.concurrency})")
    print("="*80 + "\n")

    start = time.time()
    results = await run_tests(queries, args.concurrency, args.timeout)
    total_time = time.time() - start

    report = generate_report(results)
    report["total_time_seconds"] = round(total_time, 2)

    print_summary(report)

    # Save results
    output_path = args.output or f"docs/testing/test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\n📁 Results saved to: {output_path}")
    print(f"⏱️  Total time: {total_time:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())
