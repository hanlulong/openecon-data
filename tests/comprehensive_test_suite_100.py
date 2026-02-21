#!/usr/bin/env python3
"""
Comprehensive Test Suite for econ-data-mcp - 100 Complex Queries
Tests all providers with edge cases, complex queries, and challenging scenarios
"""

import requests
import json
import time
import sys
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import csv

# Production API
API_URL = "https://openecon.ai/api/query"
TIMEOUT = 30

# Providers to skip during testing
# OECD is skipped due to strict rate limits (60 req/hour)
# See CLAUDE.md "OECD PROVIDER - LOW PRIORITY" section for details
SKIP_PROVIDERS = {"OECD"}

# Define 100 complex test queries covering all providers and edge cases
TEST_QUERIES = {
    # FRED Provider Tests (10 complex queries)
    "FRED": [
        "Compare US GDP growth rate with unemployment rate for the last 20 years",
        "Show me US federal funds rate vs inflation rate from 2000 to 2024",
        "What was the US trade deficit in Q3 2023 compared to Q3 2022?",
        "Calculate year-over-year change in US industrial production for 2023",
        "Show US housing starts seasonally adjusted annual rate for the last 5 years",
        "Compare US consumer confidence index with retail sales growth since 2020",
        "What is the correlation between US 10-year treasury yield and mortgage rates?",
        "Show me US labor force participation rate by quarter from 2019 to 2024",
        "Calculate the average US personal savings rate for each year since 2015",
        "What was the peak US unemployment rate during COVID-19 compared to 2008 crisis?",
    ],

    # World Bank Provider Tests (10 complex queries)
    "WORLDBANK": [
        "Compare GDP per capita between China, India, and Brazil from 2010 to 2023",
        "Show me population growth rate for all BRICS countries since 2000",
        "What is the poverty headcount ratio at $2.15 a day for Sub-Saharan Africa?",
        "Compare female labor force participation rates in Nordic countries vs global average",
        "Show CO2 emissions per capita for top 10 economies in 2022",
        "What is the literacy rate trend in South Asia from 1990 to present?",
        "Compare infant mortality rates between developed and developing nations",
        "Show me foreign direct investment net inflows as % of GDP for emerging markets",
        "What is the urban population percentage for countries with over 100 million people?",
        "Calculate average life expectancy improvement in Africa from 2000 to 2022",
    ],

    # UN Comtrade Tests (10 complex trade flow queries)
    "COMTRADE": [
        "Show total semiconductor exports from Taiwan to all countries in 2023",
        "What are the top 5 importers of Chinese electric vehicles in 2023?",
        "Compare oil imports between EU and China from Middle East countries",
        "Show bilateral trade balance between US and Mexico for automotive sector",
        "What is the total value of agricultural exports from Brazil to Asia?",
        "Show rare earth elements exports from China to US and EU since 2020",
        "Compare textile imports of US from Bangladesh, Vietnam, and India",
        "What are Germany's machinery exports to Eastern European countries?",
        "Show Japan's technology exports to Southeast Asian nations in 2023",
        "Calculate total pharmaceutical trade between India and Africa",
    ],

    # Statistics Canada Tests (10 complex Canadian queries)
    "STATSCAN": [
        "Compare unemployment rates across all Canadian provinces for 2024",
        "Show housing price index changes in Toronto, Vancouver, and Montreal since 2020",
        "What is the interprovincial migration pattern between Ontario and Alberta?",
        "Calculate average weekly earnings growth by industry sector in Canada",
        "Show Canadian wheat and canola production for Prairie provinces",
        "Compare retail sales growth between Quebec and British Columbia",
        "What is the labor force participation rate for Indigenous peoples in Canada?",
        "Show building permits value for residential vs commercial in major cities",
        "Calculate Canada's merchandise trade balance with US, China, and EU",
        "What is the consumer price index breakdown by component for 2024?",
    ],

    # IMF Provider Tests (10 complex international queries)
    "IMF": [
        "Compare government debt to GDP ratios for G7 countries in 2023",
        "Show current account balances for emerging market economies",
        "What are the foreign exchange reserves for Asian central banks?",
        "Compare fiscal deficits across European Union member states",
        "Show inflation forecasts for Latin American countries for 2024-2025",
        "What is the real effective exchange rate index for major currencies?",
        "Compare external debt levels of African countries as % of exports",
        "Show primary commodity prices index trends since 2020",
        "Calculate average GDP growth rates for oil-exporting countries",
        "What are the capital flow patterns to emerging markets in 2023?",
    ],

    # BIS Provider Tests (10 property and banking queries)
    "BIS": [
        "Compare residential property price indices for major global cities",
        "Show commercial real estate prices in financial centers since 2019",
        "What is the house price to income ratio trend in OECD countries?",
        "Compare property price growth rates between Asia and Europe",
        "Show long-term house price cycles for US, UK, and Japan",
        "What are the real property prices adjusted for inflation since 2010?",
        "Compare property market valuations across emerging markets",
        "Show correlation between interest rates and property prices globally",
        "What is the property price index for countries with housing bubbles?",
        "Calculate average annual property price appreciation by region",
    ],

    # Eurostat Provider Tests (10 complex EU queries)
    "EUROSTAT": [
        "Compare youth unemployment rates across all EU member states",
        "Show harmonized inflation rates for Eurozone countries in 2024",
        "What is the energy dependency ratio for EU countries?",
        "Compare R&D expenditure as % of GDP across European regions",
        "Show migration flows between EU countries since Brexit",
        "What are the greenhouse gas emissions per capita by EU country?",
        "Compare digital economy indices across European Union",
        "Show gender pay gap statistics for all EU member states",
        "What is the railway freight transport volume by country?",
        "Calculate average household disposable income in purchasing power standards",
    ],

    # OECD Provider Tests (10 complex OECD queries)
    "OECD": [
        "Compare productivity growth rates across OECD countries since 2015",
        "Show healthcare expenditure per capita for all OECD members",
        "What is the PISA education score ranking for mathematics in 2022?",
        "Compare income inequality (Gini coefficient) trends in OECD",
        "Show pension expenditure projections for aging OECD societies",
        "What are the environmental tax revenues as % of GDP by country?",
        "Compare broadband penetration rates across OECD nations",
        "Show foreign-born population percentages in OECD countries",
        "What is the tax wedge on labor income for average workers?",
        "Calculate R&D intensity trends for OECD innovation leaders",
    ],

    # Exchange Rate Provider Tests (10 complex currency queries)
    "EXCHANGERATE": [
        "Show USD strength index against major currencies in 2024",
        "Calculate volatility of EUR/USD exchange rate over last 6 months",
        "What was the biggest single-day move in GBP/USD since Brexit?",
        "Compare emerging market currencies performance against USD in 2023",
        "Show correlation between oil prices and CAD/USD exchange rate",
        "What are the real exchange rates adjusted for inflation differentials?",
        "Calculate average monthly exchange rates for JPY/USD in 2024",
        "Show impact of interest rate differentials on currency pairs",
        "What is the purchasing power parity exchange rate for major currencies?",
        "Compare Asian currency movements during Fed rate hike cycles",
    ],

    # CoinGecko Provider Tests (10 cryptocurrency queries)
    "COINGECKO": [
        "Show Bitcoin dominance percentage vs altcoins market share over time",
        "What is the correlation between Bitcoin and Ethereum prices in 2024?",
        "Compare market capitalization of top 10 cryptocurrencies",
        "Show stablecoin market cap growth since 2020",
        "What was the maximum drawdown for major cryptocurrencies in 2023?",
        "Calculate 30-day volatility for Bitcoin, Ethereum, and Solana",
        "Show DeFi total value locked trends across different blockchains",
        "What is the Bitcoin hash rate and mining difficulty trend?",
        "Compare layer-1 blockchain token performances in 2024",
        "Show cryptocurrency trading volumes by exchange",
    ],
}

class TestResult:
    """Store test result information"""
    def __init__(self, query_id: str, query: str, provider: str):
        self.query_id = query_id
        self.query = query
        self.provider = provider
        self.status = None  # pass, fail, error, clarification
        self.error_message = None
        self.data_points = 0
        self.actual_provider = None
        self.response_time = 0
        self.raw_response = None

def test_query(query: str, expected_provider: str) -> TestResult:
    """Test a single query against production"""
    result = TestResult(
        query_id=f"{expected_provider}_{datetime.now().strftime('%H%M%S')}",
        query=query,
        provider=expected_provider
    )

    start_time = time.time()

    try:
        response = requests.post(API_URL, json={"query": query}, timeout=TIMEOUT)
        result.response_time = time.time() - start_time

        if response.status_code == 200:
            data = response.json()
            result.raw_response = data

            # Check for errors
            if data.get("error"):
                result.status = "error"
                result.error_message = data.get("error")
                if "Rate limit" in str(data.get("message", "")):
                    result.error_message = "rate_limited"
            elif data.get("clarificationNeeded") or data.get("intent", {}).get("clarificationNeeded"):
                result.status = "clarification"
                result.error_message = "Clarification needed"
            elif "data" not in data or not data["data"]:
                result.status = "fail"
                result.error_message = "No data returned"
            else:
                # Success case
                result.status = "pass"
                result.actual_provider = data.get("intent", {}).get("apiProvider", "Unknown")
                result.data_points = sum(len(d.get("data", [])) for d in data.get("data", []))
        else:
            result.status = "error"
            result.error_message = f"HTTP {response.status_code}"

    except requests.exceptions.Timeout:
        result.status = "error"
        result.error_message = "Timeout"
    except Exception as e:
        result.status = "error"
        result.error_message = str(e)[:100]

    return result

def run_test_batch(provider: str, queries: List[str], delay: float = 3.0) -> List[TestResult]:
    """Run a batch of tests for a provider"""
    results = []

    print(f"\n{'='*80}")
    print(f"Testing {provider} Provider ({len(queries)} queries)")
    print(f"{'='*80}")

    for i, query in enumerate(queries, 1):
        print(f"[{i:2}/{len(queries)}] Testing: {query[:60]}...")

        result = test_query(query, provider)
        results.append(result)

        # Print result
        if result.status == "pass":
            print(f"  ✅ PASS [{result.actual_provider}] {result.data_points} points")
        elif result.status == "clarification":
            print(f"  ❓ CLARIFICATION NEEDED")
        elif result.status == "error" and result.error_message == "rate_limited":
            print(f"  ⏱️  RATE LIMITED")
        else:
            print(f"  ❌ {result.status.upper()}: {result.error_message}")

        # Delay to avoid rate limiting
        if i < len(queries):
            time.sleep(delay)

    return results

def save_results(all_results: Dict[str, List[TestResult]], filename: str):
    """Save test results to CSV and JSON"""
    # Save to CSV
    csv_filename = filename.replace('.json', '.csv')
    with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Provider', 'Query', 'Status', 'Error', 'Data Points', 'Actual Provider', 'Response Time'])

        for provider, results in all_results.items():
            for r in results:
                writer.writerow([
                    provider, r.query[:100], r.status, r.error_message,
                    r.data_points, r.actual_provider, f"{r.response_time:.2f}"
                ])

    # Save to JSON
    json_data = {
        "timestamp": datetime.now().isoformat(),
        "total_tests": sum(len(results) for results in all_results.values()),
        "summary": {},
        "details": {}
    }

    for provider, results in all_results.items():
        passed = sum(1 for r in results if r.status == "pass")
        failed = sum(1 for r in results if r.status == "fail")
        errors = sum(1 for r in results if r.status == "error")
        clarifications = sum(1 for r in results if r.status == "clarification")

        json_data["summary"][provider] = {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "clarifications": clarifications,
            "success_rate": f"{(passed/len(results)*100):.1f}%" if results else "0%"
        }

        json_data["details"][provider] = [
            {
                "query": r.query,
                "status": r.status,
                "error": r.error_message,
                "data_points": r.data_points,
                "actual_provider": r.actual_provider,
                "response_time": r.response_time
            }
            for r in results
        ]

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2)

    print(f"\nResults saved to {csv_filename} and {filename}")

def print_summary(all_results: Dict[str, List[TestResult]]):
    """Print summary of all test results"""
    print("\n" + "="*80)
    print("COMPREHENSIVE TEST SUMMARY")
    print("="*80)

    total_pass = 0
    total_fail = 0
    total_error = 0
    total_clarification = 0

    for provider, results in all_results.items():
        passed = sum(1 for r in results if r.status == "pass")
        failed = sum(1 for r in results if r.status == "fail")
        errors = sum(1 for r in results if r.status == "error")
        clarifications = sum(1 for r in results if r.status == "clarification")

        total_pass += passed
        total_fail += failed
        total_error += errors
        total_clarification += clarifications

        success_rate = (passed/len(results)*100) if results else 0

        print(f"\n{provider:15} | Total: {len(results):3} | Pass: {passed:3} | Fail: {failed:3} | Error: {errors:3} | Clarify: {clarifications:3} | Success: {success_rate:5.1f}%")

    total_tests = total_pass + total_fail + total_error + total_clarification
    overall_success = (total_pass / total_tests * 100) if total_tests > 0 else 0

    print(f"\n{'='*80}")
    print(f"OVERALL RESULTS")
    print(f"{'='*80}")
    print(f"Total Tests:     {total_tests}")
    print(f"Passed:          {total_pass} ({total_pass/total_tests*100:.1f}%)")
    print(f"Failed:          {total_fail} ({total_fail/total_tests*100:.1f}%)")
    print(f"Errors:          {total_error} ({total_error/total_tests*100:.1f}%)")
    print(f"Clarifications:  {total_clarification} ({total_clarification/total_tests*100:.1f}%)")
    print(f"\nOVERALL SUCCESS RATE: {overall_success:.1f}%")

    # Identify providers with issues
    print(f"\n{'='*80}")
    print("PROVIDERS NEEDING ATTENTION")
    print(f"{'='*80}")

    for provider, results in all_results.items():
        passed = sum(1 for r in results if r.status == "pass")
        success_rate = (passed/len(results)*100) if results else 0

        if success_rate < 80:
            failed_queries = [r.query for r in results if r.status != "pass"]
            print(f"\n{provider}: {success_rate:.1f}% success rate")
            print(f"  Failed queries ({len(failed_queries)}):")
            for q in failed_queries[:3]:  # Show first 3
                print(f"    - {q[:70]}...")

def main():
    """Run comprehensive test suite"""
    print(f"{'='*80}")
    print(f"econ-data-mcp Comprehensive Test Suite - 100 Complex Queries")
    print(f"API: {API_URL}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}")

    all_results = {}

    if SKIP_PROVIDERS:
        print(f"⚠️  Skipping providers: {SKIP_PROVIDERS} (see CLAUDE.md for reasons)")

    # Test each provider
    for provider, queries in TEST_QUERIES.items():
        if provider in SKIP_PROVIDERS:
            print(f"\n{'='*60}")
            print(f"⏭️  SKIPPING {provider} - rate limit constraints")
            print("="*60)
            continue

        results = run_test_batch(provider, queries, delay=3.0)
        all_results[provider] = results

        # Save intermediate results
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        save_results(all_results, f"comprehensive_test_results_{timestamp}.json")

        # Brief pause between providers
        print(f"\nCompleted {provider}. Pausing before next provider...")
        time.sleep(5)

    # Print final summary
    print_summary(all_results)

    # Save final results
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    save_results(all_results, f"comprehensive_test_results_{timestamp}.json")

    # Return success if >80% pass rate
    overall_pass = sum(sum(1 for r in results if r.status == "pass") for results in all_results.values())
    overall_total = sum(len(results) for results in all_results.values())
    success_rate = (overall_pass / overall_total * 100) if overall_total > 0 else 0

    if success_rate >= 80:
        print(f"\n✅ TEST SUITE PASSED with {success_rate:.1f}% success rate")
        return 0
    else:
        print(f"\n❌ TEST SUITE FAILED with only {success_rate:.1f}% success rate")
        return 1

if __name__ == "__main__":
    sys.exit(main())