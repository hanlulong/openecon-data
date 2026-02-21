"""
Comprehensive 100-query test suite for econ-data-mcp.
Tests complex economic variables and trade flow questions across all providers.
"""

import json
import requests
import time
from typing import List, Dict, Any
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

API_URL = "http://localhost:3001/api/query"

# 100 Complex Queries organized by category
QUERIES = [
    # === FRED (US Economic Data) - 15 queries ===
    {"id": 1, "query": "US unemployment rate and inflation from 2019 to 2024", "provider": "FRED"},
    {"id": 2, "query": "Federal funds rate history for the last 10 years", "provider": "FRED"},
    {"id": 3, "query": "US GDP growth rate quarterly from 2020 to 2024", "provider": "FRED"},
    {"id": 4, "query": "US 10-year treasury yield vs 2-year treasury yield 2020-2024", "provider": "FRED"},
    {"id": 5, "query": "US housing starts monthly data for 2023", "provider": "FRED"},
    {"id": 6, "query": "US consumer confidence index last 5 years", "provider": "FRED"},
    {"id": 7, "query": "US industrial production index 2019-2024", "provider": "FRED"},
    {"id": 8, "query": "US M2 money supply growth 2020-2024", "provider": "FRED"},
    {"id": 9, "query": "US personal savings rate monthly 2020-2024", "provider": "FRED"},
    {"id": 10, "query": "US labor force participation rate 2015-2024", "provider": "FRED"},
    {"id": 11, "query": "US retail sales monthly 2022-2024", "provider": "FRED"},
    {"id": 12, "query": "US producer price index 2020-2024", "provider": "FRED"},
    {"id": 13, "query": "US initial jobless claims weekly 2023-2024", "provider": "FRED"},
    {"id": 14, "query": "US core PCE inflation rate 2020-2024", "provider": "FRED"},
    {"id": 15, "query": "US real GDP per capita 2010-2023", "provider": "FRED"},

    # === World Bank (Global Development) - 15 queries ===
    {"id": 16, "query": "GDP per capita for BRICS countries 2015-2023", "provider": "WORLDBANK"},
    {"id": 17, "query": "Life expectancy at birth for G7 countries 2000-2022", "provider": "WORLDBANK"},
    {"id": 18, "query": "Population growth rate for African countries 2010-2022", "provider": "WORLDBANK"},
    {"id": 19, "query": "CO2 emissions per capita for top 10 emitters 2015-2020", "provider": "WORLDBANK"},
    {"id": 20, "query": "Literacy rate for South Asian countries 2010-2022", "provider": "WORLDBANK"},
    {"id": 21, "query": "Foreign direct investment inflows to Southeast Asia 2015-2022", "provider": "WORLDBANK"},
    {"id": 22, "query": "Government debt to GDP ratio for European countries 2015-2023", "provider": "WORLDBANK"},
    {"id": 23, "query": "Unemployment rate for Latin American countries 2018-2023", "provider": "WORLDBANK"},
    {"id": 24, "query": "Internet users percentage for developing countries 2015-2022", "provider": "WORLDBANK"},
    {"id": 25, "query": "Renewable energy consumption share for Nordic countries 2010-2022", "provider": "WORLDBANK"},
    {"id": 26, "query": "Inflation rate for Middle Eastern countries 2018-2023", "provider": "WORLDBANK"},
    {"id": 27, "query": "GDP growth rate for East Asian economies 2015-2023", "provider": "WORLDBANK"},
    {"id": 28, "query": "Poverty headcount ratio for Sub-Saharan Africa 2010-2022", "provider": "WORLDBANK"},
    {"id": 29, "query": "Current account balance for oil exporting countries 2015-2022", "provider": "WORLDBANK"},
    {"id": 30, "query": "Health expenditure as percentage of GDP for OECD countries 2015-2021", "provider": "WORLDBANK"},

    # === UN Comtrade (International Trade) - 15 queries ===
    {"id": 31, "query": "US imports from China 2018-2023", "provider": "COMTRADE"},
    {"id": 32, "query": "Germany exports to France 2019-2023", "provider": "COMTRADE"},
    {"id": 33, "query": "Japan semiconductor exports 2020-2023", "provider": "COMTRADE"},
    {"id": 34, "query": "China steel exports to the world 2018-2023", "provider": "COMTRADE"},
    {"id": 35, "query": "India pharmaceutical exports 2019-2023", "provider": "COMTRADE"},
    {"id": 36, "query": "Brazil agricultural exports 2018-2023", "provider": "COMTRADE"},
    {"id": 37, "query": "South Korea electronics exports 2019-2023", "provider": "COMTRADE"},
    {"id": 38, "query": "Australia mineral exports to China 2018-2023", "provider": "COMTRADE"},
    {"id": 39, "query": "Canada oil exports to US 2019-2023", "provider": "COMTRADE"},
    {"id": 40, "query": "Mexico auto parts exports 2018-2023", "provider": "COMTRADE"},
    {"id": 41, "query": "Vietnam textile exports 2019-2023", "provider": "COMTRADE"},
    {"id": 42, "query": "Saudi Arabia petroleum exports 2018-2023", "provider": "COMTRADE"},
    {"id": 43, "query": "UK imports from EU countries 2019-2023", "provider": "COMTRADE"},
    {"id": 44, "query": "Netherlands agricultural imports 2018-2023", "provider": "COMTRADE"},
    {"id": 45, "query": "Taiwan semiconductor chip exports 2020-2023", "provider": "COMTRADE"},

    # === Statistics Canada - 10 queries ===
    {"id": 46, "query": "Canada unemployment rate by province 2020-2024", "provider": "STATSCAN"},
    {"id": 47, "query": "Canada housing price index 2018-2024", "provider": "STATSCAN"},
    {"id": 48, "query": "Canada GDP by industry 2019-2023", "provider": "STATSCAN"},
    {"id": 49, "query": "Canada consumer price index 2020-2024", "provider": "STATSCAN"},
    {"id": 50, "query": "Canada retail sales by sector 2022-2024", "provider": "STATSCAN"},
    {"id": 51, "query": "Canada immigration statistics 2015-2023", "provider": "STATSCAN"},
    {"id": 52, "query": "Canada employment by age group 2019-2024", "provider": "STATSCAN"},
    {"id": 53, "query": "Canada interest rates 2020-2024", "provider": "STATSCAN"},
    {"id": 54, "query": "Canada merchandise trade balance 2018-2023", "provider": "STATSCAN"},
    {"id": 55, "query": "Canada population by province 2020-2024", "provider": "STATSCAN"},

    # === IMF (International Monetary Fund) - 10 queries ===
    {"id": 56, "query": "IMF world economic outlook GDP projections 2024-2028", "provider": "IMF"},
    {"id": 57, "query": "Global inflation forecast 2024-2026", "provider": "IMF"},
    {"id": 58, "query": "Emerging markets current account balance 2018-2023", "provider": "IMF"},
    {"id": 59, "query": "Advanced economies government debt 2015-2023", "provider": "IMF"},
    {"id": 60, "query": "World trade volume growth 2018-2023", "provider": "IMF"},
    {"id": 61, "query": "China economic growth forecast 2024-2028", "provider": "IMF"},
    {"id": 62, "query": "Eurozone GDP growth projections 2024-2026", "provider": "IMF"},
    {"id": 63, "query": "Global commodity price index 2019-2024", "provider": "IMF"},
    {"id": 64, "query": "Developing economies inflation 2018-2023", "provider": "IMF"},
    {"id": 65, "query": "G20 fiscal deficit 2019-2023", "provider": "IMF"},

    # === BIS (Bank for International Settlements) - 10 queries ===
    {"id": 66, "query": "Global property prices index 2015-2024", "provider": "BIS"},
    {"id": 67, "query": "US residential property prices 2018-2024", "provider": "BIS"},
    {"id": 68, "query": "UK house prices 2015-2024", "provider": "BIS"},
    {"id": 69, "query": "Germany real estate prices 2018-2024", "provider": "BIS"},
    {"id": 70, "query": "Japan property price index 2015-2024", "provider": "BIS"},
    {"id": 71, "query": "Australia housing market index 2018-2024", "provider": "BIS"},
    {"id": 72, "query": "Canada residential property prices 2015-2024", "provider": "BIS"},
    {"id": 73, "query": "Switzerland property prices 2018-2024", "provider": "BIS"},
    {"id": 74, "query": "France housing price index 2015-2024", "provider": "BIS"},
    {"id": 75, "query": "Spain real estate market 2018-2024", "provider": "BIS"},

    # === Eurostat (European Statistics) - 10 queries ===
    {"id": 76, "query": "EU unemployment rate by country 2019-2024", "provider": "EUROSTAT"},
    {"id": 77, "query": "Eurozone inflation rate 2020-2024", "provider": "EUROSTAT"},
    {"id": 78, "query": "Germany GDP growth 2018-2024", "provider": "EUROSTAT"},
    {"id": 79, "query": "France employment rate 2019-2024", "provider": "EUROSTAT"},
    {"id": 80, "query": "Italy government debt 2015-2023", "provider": "EUROSTAT"},
    {"id": 81, "query": "Spain youth unemployment 2018-2024", "provider": "EUROSTAT"},
    {"id": 82, "query": "Netherlands trade balance 2019-2023", "provider": "EUROSTAT"},
    {"id": 83, "query": "Poland GDP per capita 2015-2023", "provider": "EUROSTAT"},
    {"id": 84, "query": "EU energy consumption by country 2018-2022", "provider": "EUROSTAT"},
    {"id": 85, "query": "EU population by member state 2020-2024", "provider": "EUROSTAT"},

    # === OECD - 10 queries ===
    {"id": 86, "query": "OECD average unemployment rate 2018-2024", "provider": "OECD"},
    {"id": 87, "query": "OECD GDP growth rates 2019-2024", "provider": "OECD"},
    {"id": 88, "query": "OECD inflation comparison 2020-2024", "provider": "OECD"},
    {"id": 89, "query": "OECD education spending by country 2018-2022", "provider": "OECD"},
    {"id": 90, "query": "OECD healthcare expenditure 2015-2022", "provider": "OECD"},
    {"id": 91, "query": "OECD productivity growth 2018-2023", "provider": "OECD"},
    {"id": 92, "query": "OECD tax revenue to GDP ratio 2018-2022", "provider": "OECD"},
    {"id": 93, "query": "OECD R&D spending by country 2015-2022", "provider": "OECD"},
    {"id": 94, "query": "OECD income inequality indicators 2018-2022", "provider": "OECD"},
    {"id": 95, "query": "OECD labor force participation 2018-2024", "provider": "OECD"},

    # === ExchangeRate-API (Currency) - 3 queries ===
    {"id": 96, "query": "EUR to USD exchange rate 2020-2024", "provider": "EXCHANGERATE"},
    {"id": 97, "query": "Japanese Yen to US Dollar rate 2022-2024", "provider": "EXCHANGERATE"},
    {"id": 98, "query": "British Pound to Euro exchange rate 2021-2024", "provider": "EXCHANGERATE"},

    # === CoinGecko (Cryptocurrency) - 2 queries ===
    {"id": 99, "query": "Bitcoin price history 2023-2024", "provider": "COINGECKO"},
    {"id": 100, "query": "Ethereum market cap 2023-2024", "provider": "COINGECKO"},
]


@dataclass
class TestResult:
    query_id: int
    query: str
    provider: str
    success: bool
    data_points: int
    error: str
    response_time: float
    metadata: Dict[str, Any]


def test_query(query_info: Dict[str, Any]) -> TestResult:
    """Test a single query and return the result."""
    query_id = query_info["id"]
    query = query_info["query"]
    provider = query_info["provider"]

    start_time = time.time()
    try:
        response = requests.post(
            API_URL,
            json={"query": query},
            headers={"Content-Type": "application/json"},
            timeout=60
        )
        response_time = time.time() - start_time

        if response.status_code != 200:
            return TestResult(
                query_id=query_id,
                query=query,
                provider=provider,
                success=False,
                data_points=0,
                error=f"HTTP {response.status_code}: {response.text[:200]}",
                response_time=response_time,
                metadata={}
            )

        data = response.json()

        # Check for errors
        if data.get("error"):
            return TestResult(
                query_id=query_id,
                query=query,
                provider=provider,
                success=False,
                data_points=0,
                error=data["error"],
                response_time=response_time,
                metadata={}
            )

        # Check for clarification needed
        if data.get("clarificationNeeded"):
            return TestResult(
                query_id=query_id,
                query=query,
                provider=provider,
                success=False,
                data_points=0,
                error=f"Clarification needed: {data.get('clarificationQuestions', [])}",
                response_time=response_time,
                metadata={}
            )

        # Check for data
        if not data.get("data"):
            return TestResult(
                query_id=query_id,
                query=query,
                provider=provider,
                success=False,
                data_points=0,
                error="No data returned",
                response_time=response_time,
                metadata={}
            )

        # Count data points and extract metadata
        total_points = 0
        metadata = {}
        for i, dataset in enumerate(data["data"]):
            points = len(dataset.get("data", []))
            total_points += points
            if i == 0:
                metadata = {
                    "source": dataset.get("metadata", {}).get("source", ""),
                    "indicator": dataset.get("metadata", {}).get("indicator", ""),
                    "country": dataset.get("metadata", {}).get("country", ""),
                }

        # Validate data points have values
        has_values = False
        for dataset in data["data"]:
            for point in dataset.get("data", []):
                if point.get("value") is not None:
                    has_values = True
                    break
            if has_values:
                break

        if not has_values:
            return TestResult(
                query_id=query_id,
                query=query,
                provider=provider,
                success=False,
                data_points=total_points,
                error="All data points have null values",
                response_time=response_time,
                metadata=metadata
            )

        return TestResult(
            query_id=query_id,
            query=query,
            provider=provider,
            success=True,
            data_points=total_points,
            error="",
            response_time=response_time,
            metadata=metadata
        )

    except requests.exceptions.Timeout:
        return TestResult(
            query_id=query_id,
            query=query,
            provider=provider,
            success=False,
            data_points=0,
            error="Request timeout (60s)",
            response_time=60.0,
            metadata={}
        )
    except Exception as e:
        return TestResult(
            query_id=query_id,
            query=query,
            provider=provider,
            success=False,
            data_points=0,
            error=str(e),
            response_time=time.time() - start_time,
            metadata={}
        )


def run_batch(queries: List[Dict], batch_num: int, total_batches: int) -> List[TestResult]:
    """Run a batch of queries with parallel execution."""
    print(f"\n{'='*60}")
    print(f"BATCH {batch_num}/{total_batches} - Testing {len(queries)} queries")
    print(f"{'='*60}")

    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_query = {executor.submit(test_query, q): q for q in queries}
        for future in as_completed(future_to_query):
            result = future.result()
            results.append(result)

            status = "✅" if result.success else "❌"
            print(f"{status} [{result.query_id:3d}] {result.provider:12s} | {result.query[:50]:50s} | {result.response_time:.1f}s")
            if not result.success:
                print(f"    Error: {result.error[:80]}")

    return results


def main():
    """Run all 100 queries in batches."""
    print("="*70)
    print("OPENECON COMPREHENSIVE 100-QUERY TEST SUITE")
    print("="*70)

    # Batch size
    batch_size = 20
    all_results = []

    # Run queries in batches
    for i in range(0, len(QUERIES), batch_size):
        batch = QUERIES[i:i+batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (len(QUERIES) + batch_size - 1) // batch_size

        results = run_batch(batch, batch_num, total_batches)
        all_results.extend(results)

        # Brief pause between batches to avoid overwhelming the server
        if i + batch_size < len(QUERIES):
            time.sleep(2)

    # Summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)

    passed = [r for r in all_results if r.success]
    failed = [r for r in all_results if not r.success]

    print(f"\nTotal: {len(all_results)} | Passed: {len(passed)} | Failed: {len(failed)}")
    print(f"Pass Rate: {len(passed)/len(all_results)*100:.1f}%")

    # Group by provider
    print("\nResults by Provider:")
    providers = {}
    for r in all_results:
        if r.provider not in providers:
            providers[r.provider] = {"passed": 0, "failed": 0}
        if r.success:
            providers[r.provider]["passed"] += 1
        else:
            providers[r.provider]["failed"] += 1

    for provider, stats in sorted(providers.items()):
        total = stats["passed"] + stats["failed"]
        pct = stats["passed"] / total * 100 if total > 0 else 0
        print(f"  {provider:15s}: {stats['passed']:2d}/{total:2d} ({pct:.0f}%)")

    # List all failures
    if failed:
        print("\n" + "="*70)
        print("FAILED QUERIES")
        print("="*70)
        for r in failed:
            print(f"\n[{r.query_id}] {r.provider}: {r.query}")
            print(f"    Error: {r.error}")

    # Save results to JSON
    results_json = {
        "summary": {
            "total": len(all_results),
            "passed": len(passed),
            "failed": len(failed),
            "pass_rate": len(passed) / len(all_results) * 100
        },
        "by_provider": providers,
        "failed_queries": [
            {
                "id": r.query_id,
                "query": r.query,
                "provider": r.provider,
                "error": r.error
            }
            for r in failed
        ],
        "all_results": [
            {
                "id": r.query_id,
                "query": r.query,
                "provider": r.provider,
                "success": r.success,
                "data_points": r.data_points,
                "error": r.error,
                "response_time": r.response_time,
                "metadata": r.metadata
            }
            for r in all_results
        ]
    }

    with open("test_results_100.json", "w") as f:
        json.dump(results_json, f, indent=2)

    print(f"\nResults saved to test_results_100.json")

    return len(failed) == 0


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
