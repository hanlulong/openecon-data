#!/usr/bin/env python3
"""
Compare Production vs Local API Testing

Tests all queries against both production and local API,
then generates detailed comparison report.
"""

import requests
import json
import time
import sys
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path

# Import test queries
from comprehensive_test_suite_100 import TEST_QUERIES

# API endpoints
PRODUCTION_API = "https://openecon.ai/api/query"
LOCAL_API = "http://localhost:3001/api/query"
TIMEOUT = 30

class QueryResult:
    """Store test result for a single query"""
    def __init__(self, query: str, provider: str, environment: str):
        self.query = query
        self.provider = provider
        self.environment = environment
        self.status = None  # pass, fail, error, clarification, timeout
        self.error_message = None
        self.data_points = 0
        self.actual_provider = None
        self.response_time = 0
        self.http_status = None
        self.raw_response = None

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            "query": self.query,
            "provider": self.provider,
            "environment": self.environment,
            "status": self.status,
            "error_message": self.error_message,
            "data_points": self.data_points,
            "actual_provider": self.actual_provider,
            "response_time": self.response_time,
            "http_status": self.http_status
        }

def test_single_query(query: str, expected_provider: str, api_url: str, environment: str) -> QueryResult:
    """Test a single query against specified API"""
    result = QueryResult(query, expected_provider, environment)

    start_time = time.time()

    try:
        # Print progress for long queries
        sys.stdout.write(f"    {environment[:4]:4}...")
        sys.stdout.flush()

        response = requests.post(api_url, json={"query": query}, timeout=TIMEOUT)
        result.response_time = time.time() - start_time
        result.http_status = response.status_code

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
        result.status = "timeout"
        result.error_message = "Request timeout"
    except requests.exceptions.ConnectionError:
        result.status = "error"
        result.error_message = "Connection error"
    except Exception as e:
        result.status = "error"
        result.error_message = str(e)[:200]

    return result

def run_parallel_test(query: str, provider: str, query_num: int, total: int) -> Dict[str, QueryResult]:
    """Test query against both production and local simultaneously"""
    print(f"\n[{query_num}/{total}] Testing: {query[:60]}...")

    # Test production
    print(f"  Testing PRODUCTION...")
    prod_result = test_single_query(query, provider, PRODUCTION_API, "production")

    # Test local
    print(f"  Testing LOCAL...")
    local_result = test_single_query(query, provider, LOCAL_API, "local")

    # Print comparison
    print(f"    PRODUCTION: {prod_result.status.upper():15} | {prod_result.response_time:.2f}s | {prod_result.data_points} pts")
    print(f"    LOCAL:      {local_result.status.upper():15} | {local_result.response_time:.2f}s | {local_result.data_points} pts")

    # Highlight differences
    if prod_result.status != local_result.status:
        print(f"    ⚠️  STATUS MISMATCH!")
    elif prod_result.status == "pass" and prod_result.data_points != local_result.data_points:
        print(f"    ⚠️  DATA COUNT MISMATCH!")

    return {
        "production": prod_result,
        "local": local_result
    }

class TestRunner:
    """Main test runner that compares production vs local"""

    def __init__(self):
        self.results = []  # List of result pairs (production, local)

    def run_all_tests(self, delay: float = 2.0):
        """Run all test queries"""
        query_num = 0
        total_queries = sum(len(queries) for queries in TEST_QUERIES.values())

        print(f"{'='*80}")
        print(f"PRODUCTION vs LOCAL API COMPARISON")
        print(f"{'='*80}")
        print(f"Production API: {PRODUCTION_API}")
        print(f"Local API:      {LOCAL_API}")
        print(f"Total Queries:  {total_queries}")
        print(f"{'='*80}")

        for provider, queries in TEST_QUERIES.items():
            print(f"\n{'='*80}")
            print(f"TESTING {provider} ({len(queries)} queries)")
            print(f"{'='*80}")

            for query in queries:
                query_num += 1
                result_pair = run_parallel_test(query, provider, query_num, total_queries)
                self.results.append(result_pair)

                # Save intermediate results every 10 queries
                if query_num % 10 == 0:
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    self.save_results(f"test_results_prod_vs_local_checkpoint_{timestamp}.json")

                # Delay to avoid rate limiting
                if query_num < total_queries:
                    time.sleep(delay)

    def analyze_results(self) -> Dict[str, Any]:
        """Analyze results and generate comparison statistics"""
        analysis = {
            "total_queries": len(self.results),
            "production": {
                "pass": 0,
                "fail": 0,
                "error": 0,
                "timeout": 0,
                "clarification": 0
            },
            "local": {
                "pass": 0,
                "fail": 0,
                "error": 0,
                "timeout": 0,
                "clarification": 0
            },
            "differences": {
                "status_mismatch": [],
                "data_mismatch": [],
                "provider_mismatch": [],
                "production_only_pass": [],
                "local_only_pass": [],
                "both_pass": 0,
                "both_fail": 0
            }
        }

        for pair in self.results:
            prod = pair["production"]
            local = pair["local"]

            # Count statuses
            analysis["production"][prod.status] = analysis["production"].get(prod.status, 0) + 1
            analysis["local"][local.status] = analysis["local"].get(local.status, 0) + 1

            # Check for differences
            if prod.status != local.status:
                analysis["differences"]["status_mismatch"].append({
                    "query": prod.query,
                    "provider": prod.provider,
                    "production_status": prod.status,
                    "local_status": local.status,
                    "production_error": prod.error_message,
                    "local_error": local.error_message
                })

                if prod.status == "pass" and local.status != "pass":
                    analysis["differences"]["production_only_pass"].append({
                        "query": prod.query,
                        "provider": prod.provider,
                        "local_error": local.error_message
                    })
                elif local.status == "pass" and prod.status != "pass":
                    analysis["differences"]["local_only_pass"].append({
                        "query": local.query,
                        "provider": local.provider,
                        "production_error": prod.error_message
                    })
            else:
                if prod.status == "pass":
                    analysis["differences"]["both_pass"] += 1

                    # Check data point count
                    if prod.data_points != local.data_points:
                        analysis["differences"]["data_mismatch"].append({
                            "query": prod.query,
                            "provider": prod.provider,
                            "production_points": prod.data_points,
                            "local_points": local.data_points
                        })

                    # Check provider routing
                    if prod.actual_provider != local.actual_provider:
                        analysis["differences"]["provider_mismatch"].append({
                            "query": prod.query,
                            "expected_provider": prod.provider,
                            "production_provider": prod.actual_provider,
                            "local_provider": local.actual_provider
                        })
                else:
                    analysis["differences"]["both_fail"] += 1

        return analysis

    def save_results(self, filename: str):
        """Save detailed results to JSON"""
        output = {
            "timestamp": datetime.now().isoformat(),
            "production_api": PRODUCTION_API,
            "local_api": LOCAL_API,
            "analysis": self.analyze_results(),
            "detailed_results": [
                {
                    "query": pair["production"].query,
                    "provider": pair["production"].provider,
                    "production": pair["production"].to_dict(),
                    "local": pair["local"].to_dict()
                }
                for pair in self.results
            ]
        }

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2)

        print(f"\n✅ Results saved to {filename}")

    def print_summary(self):
        """Print comprehensive summary"""
        analysis = self.analyze_results()

        print(f"\n{'='*80}")
        print("COMPARISON SUMMARY")
        print(f"{'='*80}")

        total = analysis["total_queries"]

        # Overall statistics
        print(f"\nTotal Queries Tested: {total}")
        print(f"\nPRODUCTION API:")
        print(f"  Pass:          {analysis['production']['pass']:3} ({analysis['production']['pass']/total*100:5.1f}%)")
        print(f"  Fail:          {analysis['production']['fail']:3} ({analysis['production']['fail']/total*100:5.1f}%)")
        print(f"  Error:         {analysis['production']['error']:3} ({analysis['production']['error']/total*100:5.1f}%)")
        print(f"  Timeout:       {analysis['production']['timeout']:3} ({analysis['production']['timeout']/total*100:5.1f}%)")
        print(f"  Clarification: {analysis['production']['clarification']:3} ({analysis['production']['clarification']/total*100:5.1f}%)")

        print(f"\nLOCAL API:")
        print(f"  Pass:          {analysis['local']['pass']:3} ({analysis['local']['pass']/total*100:5.1f}%)")
        print(f"  Fail:          {analysis['local']['fail']:3} ({analysis['local']['fail']/total*100:5.1f}%)")
        print(f"  Error:         {analysis['local']['error']:3} ({analysis['local']['error']/total*100:5.1f}%)")
        print(f"  Timeout:       {analysis['local']['timeout']:3} ({analysis['local']['timeout']/total*100:5.1f}%)")
        print(f"  Clarification: {analysis['local']['clarification']:3} ({analysis['local']['clarification']/total*100:5.1f}%)")

        # Differences
        print(f"\n{'='*80}")
        print("DIFFERENCES ANALYSIS")
        print(f"{'='*80}")

        print(f"\nBoth Pass:     {analysis['differences']['both_pass']}")
        print(f"Both Fail:     {analysis['differences']['both_fail']}")
        print(f"\nStatus Mismatches: {len(analysis['differences']['status_mismatch'])}")
        print(f"  - Production Only Pass: {len(analysis['differences']['production_only_pass'])}")
        print(f"  - Local Only Pass:      {len(analysis['differences']['local_only_pass'])}")
        print(f"\nData Mismatches:     {len(analysis['differences']['data_mismatch'])}")
        print(f"Provider Mismatches: {len(analysis['differences']['provider_mismatch'])}")

        # Show critical issues
        if analysis['differences']['production_only_pass']:
            print(f"\n{'='*80}")
            print("CRITICAL: Queries that PASS on Production but FAIL on Local")
            print(f"{'='*80}")
            for item in analysis['differences']['production_only_pass'][:10]:
                print(f"\n  Provider: {item['provider']}")
                print(f"  Query:    {item['query'][:70]}...")
                print(f"  Error:    {item['local_error']}")

        if analysis['differences']['local_only_pass']:
            print(f"\n{'='*80}")
            print("Queries that PASS on Local but FAIL on Production")
            print(f"{'='*80}")
            for item in analysis['differences']['local_only_pass'][:10]:
                print(f"\n  Provider: {item['provider']}")
                print(f"  Query:    {item['query'][:70]}...")
                print(f"  Error:    {item['production_error']}")

        # Recommendations
        print(f"\n{'='*80}")
        print("RECOMMENDATIONS")
        print(f"{'='*80}")

        prod_success_rate = analysis['production']['pass'] / total * 100
        local_success_rate = analysis['local']['pass'] / total * 100

        if prod_success_rate >= 80 and local_success_rate >= 80:
            print("✅ Both environments performing well (≥80% success rate)")
        elif prod_success_rate < local_success_rate:
            print("⚠️  Production underperforming compared to local")
            print(f"   - Production: {prod_success_rate:.1f}% | Local: {local_success_rate:.1f}%")
            print("   - Review production-only failures above")
        elif local_success_rate < prod_success_rate:
            print("⚠️  Local underperforming compared to production")
            print(f"   - Production: {prod_success_rate:.1f}% | Local: {local_success_rate:.1f}%")
            print("   - Review local-only failures above")
        else:
            print("❌ Both environments need attention (<80% success rate)")

        if len(analysis['differences']['data_mismatch']) > 0:
            print(f"\n⚠️  {len(analysis['differences']['data_mismatch'])} queries return different data counts")
            print("   - Review data_mismatch details in JSON output")

def main():
    """Run production vs local comparison test"""
    print(f"Starting Production vs Local API Comparison")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    runner = TestRunner()

    try:
        # Run all tests
        runner.run_all_tests(delay=2.0)

        # Print summary
        runner.print_summary()

        # Save results
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        runner.save_results(f"test_results_production_vs_local_{timestamp}.json")

        # Determine exit code
        analysis = runner.analyze_results()
        prod_pass_rate = analysis['production']['pass'] / analysis['total_queries'] * 100
        local_pass_rate = analysis['local']['pass'] / analysis['total_queries'] * 100

        if prod_pass_rate >= 80 and local_pass_rate >= 80:
            print(f"\n✅ TEST COMPARISON SUCCESSFUL")
            return 0
        else:
            print(f"\n❌ TEST COMPARISON SHOWS ISSUES")
            return 1

    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        runner.save_results(f"test_results_production_vs_local_interrupted.json")
        return 1
    except Exception as e:
        print(f"\n❌ Error running tests: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
