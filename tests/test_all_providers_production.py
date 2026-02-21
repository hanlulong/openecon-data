#!/usr/bin/env python3
"""
Comprehensive Production Test Suite for All econ-data-mcp Providers
Tests diverse queries per provider against production with data verification
"""

import requests
import json
import time
import sys
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# Production API
API_URL = "https://openecon.ai/api/query"
TIMEOUT = 30

# Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

def test_query(query: str) -> Dict:
    """Test a query against production"""
    try:
        response = requests.post(API_URL, json={"query": query}, timeout=TIMEOUT)
        if response.status_code == 200:
            return response.json()
        else:
            return {"error": f"HTTP {response.status_code}"}
    except requests.exceptions.Timeout:
        return {"error": "Timeout"}
    except Exception as e:
        return {"error": str(e)}

def extract_latest_value(data: Dict) -> Optional[float]:
    """Extract latest data value"""
    try:
        if "data" in data and data["data"]:
            for dataset in data["data"]:
                points = dataset.get("data", [])
                if points:
                    # Get most recent
                    sorted_points = sorted(points, key=lambda x: x.get("date", ""), reverse=True)
                    for p in sorted_points:
                        if p.get("value") is not None:
                            return float(p["value"])
    except:
        pass
    return None

def verify_value(actual: float, expected: float, tolerance: float = 0.15) -> bool:
    """Verify if actual value is within tolerance of expected"""
    if expected == 0:
        return abs(actual) < 1.0
    return abs((actual - expected) / expected) <= tolerance

# Test queries for each provider
PROVIDER_TESTS = {
    "FRED": [
        # Core US indicators with verification
        ("US GDP", 27000000000000, "~$27 trillion (2023)"),
        ("US unemployment rate", 3.7, "~3.7% (2024)"),
        ("US inflation rate", 3.2, "~3.2% (2024)"),
        ("US federal funds rate", 5.33, "~5.33% (2024)"),
        ("US 10 year treasury yield", 4.3, "~4.3% (2024)"),
        
        # More indicators
        ("US GDP growth rate", 2.5, None),
        ("US CPI", 310, None),
        ("US retail sales", None, None),
        ("US housing starts", None, None),
        ("US industrial production", None, None),
        ("S&P 500 index", None, None),
        ("US mortgage rate 30 year", 7.0, None),
        ("US trade balance", None, None),
        ("US nonfarm payrolls", None, None),
        ("US consumer confidence", None, None),
    ],
    
    "WORLDBANK": [
        # Major economies with verification
        ("China GDP", 17700000000000, "~$17.7 trillion"),
        ("India GDP", 3700000000000, "~$3.7 trillion"),
        ("Brazil GDP", 2100000000000, "~$2.1 trillion"),
        ("World population", 8100000000, "~8.1 billion"),
        
        # More countries
        ("Indonesia GDP", None, None),
        ("Mexico GDP", None, None),
        ("Russia GDP", 2000000000000, None),
        ("Japan GDP", None, None),
        ("Germany GDP", None, None),
        ("UK GDP", None, None),
        ("France GDP", None, None),
        ("Italy GDP", None, None),
        ("South Africa GDP", None, None),
        ("Nigeria GDP", None, None),
        ("Egypt GDP", None, None),
    ],
    
    "COMTRADE": [
        # Trade flows
        ("US China trade", None, None),
        ("China imports from US", None, None),
        ("US exports to China", None, None),
        ("Germany France trade", None, None),
        ("Japan Korea trade", None, None),
        ("Canada US trade", None, None),
        ("Mexico US trade", None, None),
        ("EU trade with China", None, None),
        ("UK EU trade", None, None),
        ("India China trade", None, None),
    ],
    
    "STATSCAN": [
        # Canadian data with verification
        ("Canada GDP", None, None),
        ("Canada unemployment rate", 5.0, "~5.0%"),
        ("Canada inflation rate", 3.1, "~3.1%"),
        ("Canada population", 39000000, "~39 million"),
        
        # Provincial
        ("Ontario GDP", None, None),
        ("Quebec unemployment", None, None),
        ("Alberta population", None, None),
        ("BC housing starts", None, None),
        
        # Cities
        ("Toronto population", None, None),
        ("Vancouver housing", None, None),
        ("Montreal unemployment", None, None),
        
        # Sectors
        ("Canada retail sales", None, None),
        ("Canada exports", None, None),
        ("Canada housing starts", None, None),
    ],
    
    "IMF": [
        # International monetary data
        ("Spain GDP", None, None),
        ("Portugal debt to GDP", None, None),
        ("Greece debt", None, None),
        ("Italy inflation", None, None),
        ("France unemployment", None, None),
        ("Germany current account", None, None),
        ("Netherlands GDP", None, None),
        ("Belgium inflation", None, None),
        ("Austria unemployment", None, None),
        ("Ireland GDP", None, None),
    ],
    
    "BIS": [
        # Property prices
        ("US house prices", None, None),
        ("UK property prices", None, None),
        ("Germany house prices", None, None),
        ("France property prices", None, None),
        ("Japan house prices", None, None),
        ("Canada property prices", None, None),
        ("Australia house prices", None, None),
        ("Spain property prices", None, None),
        ("Italy house prices", None, None),
        ("Netherlands property prices", None, None),
    ],
    
    "EUROSTAT": [
        # EU data
        ("EU GDP", None, None),
        ("EU unemployment", None, None),
        ("EU inflation", None, None),
        ("Germany unemployment", None, None),
        ("France GDP", None, None),
        ("Italy unemployment", None, None),
        ("Spain inflation", None, None),
        ("Poland GDP", None, None),
        ("Netherlands unemployment", None, None),
        ("Belgium GDP", None, None),
    ],
    
    "OECD": [
        # OECD members
        ("Japan GDP", None, None),
        ("Korea unemployment", None, None),
        ("Mexico GDP", None, None),
        ("Chile inflation", None, None),
        ("Australia unemployment", None, None),
        ("New Zealand GDP", None, None),
        ("Canada GDP from OECD", None, None),
        ("Norway GDP", None, None),
        ("Switzerland unemployment", None, None),
        ("Iceland inflation", None, None),
    ],
    
    "EXCHANGERATE": [
        # Currency pairs with rough verification
        ("USD EUR exchange rate", 0.92, "~0.92"),
        ("GBP USD exchange rate", 1.27, "~1.27"),
        ("USD JPY exchange rate", 150, "~150"),
        ("EUR GBP exchange rate", 0.86, "~0.86"),
        ("USD CAD exchange rate", 1.36, "~1.36"),
        ("AUD USD exchange rate", 0.65, "~0.65"),
        ("USD CHF exchange rate", 0.88, "~0.88"),
        ("EUR JPY exchange rate", 163, "~163"),
        ("USD CNY exchange rate", 7.25, "~7.25"),
        ("USD INR exchange rate", 83, "~83"),
    ],
    
    "COINGECKO": [
        # Crypto prices (very volatile, use wide tolerance)
        ("Bitcoin price", 50000, "Volatile: $30k-70k range"),
        ("Ethereum price", 3000, "Volatile: $2k-4k range"),
        ("BNB price", None, None),
        ("XRP price", None, None),
        ("Cardano price", None, None),
        ("Solana price", None, None),
        ("Dogecoin price", None, None),
        ("Polygon price", None, None),
        ("Avalanche price", None, None),
        ("Chainlink price", None, None),
    ],
}

def test_provider(provider_name: str, queries: List[Tuple]) -> Dict:
    """Test a provider with its queries"""
    print(f"\n{BOLD}{BLUE}Testing {provider_name}{RESET}")
    print("=" * 60)
    
    results = {"pass": 0, "fail": 0, "error": 0, "clarification": 0}
    
    for i, test_data in enumerate(queries, 1):
        query = test_data[0]
        expected_value = test_data[1] if len(test_data) > 1 else None
        note = test_data[2] if len(test_data) > 2 else None
        
        print(f"[{i:2}/{len(queries)}] {query:40}", end=" ")
        
        result = test_query(query)
        
        # Check for errors
        if "error" in result:
            print(f"{RED}✗ ERROR{RESET}: {result['error']}")
            results["error"] += 1
        elif result.get("intent", {}).get("clarificationNeeded"):
            print(f"{YELLOW}? CLARIFICATION{RESET}")
            results["clarification"] += 1
        elif "data" not in result or not result["data"]:
            print(f"{YELLOW}✗ NO DATA{RESET}")
            results["fail"] += 1
        else:
            # Extract value if needed
            actual_value = extract_latest_value(result)
            provider = result.get("intent", {}).get("apiProvider", "?")
            
            if expected_value is not None and actual_value is not None:
                # Verify value
                tolerance = 0.5 if "Volatile" in str(note) else 0.15
                if verify_value(actual_value, expected_value, tolerance):
                    print(f"{GREEN}✓ PASS{RESET} [{provider}] Value: {actual_value:.2f}")
                    results["pass"] += 1
                else:
                    print(f"{RED}✗ VALUE{RESET} [{provider}] Got: {actual_value:.2f}, Expected: {expected_value:.2f}")
                    results["fail"] += 1
            else:
                # Just check data exists
                points = sum(len(d.get("data", [])) for d in result.get("data", []))
                print(f"{GREEN}✓ PASS{RESET} [{provider}] {points} points")
                results["pass"] += 1
        
        time.sleep(0.3)  # Rate limiting
    
    # Summary
    total = sum(results.values())
    pass_rate = (results["pass"] / total * 100) if total > 0 else 0
    
    print(f"\n{provider_name} Summary:")
    print(f"  Pass: {results['pass']}/{total} ({pass_rate:.1f}%)")
    print(f"  Fail: {results['fail']}, Error: {results['error']}, Clarification: {results['clarification']}")
    
    return {"provider": provider_name, "total": total, "pass": results["pass"], "rate": pass_rate}

def main():
    """Run comprehensive production tests"""
    print(f"{BOLD}{'=' * 80}{RESET}")
    print(f"{BOLD}econ-data-mcp Production Test Suite{RESET}")
    print(f"API: {API_URL}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{BOLD}{'=' * 80}{RESET}")
    
    all_results = []
    
    # Test each provider
    for provider, queries in PROVIDER_TESTS.items():
        result = test_provider(provider, queries)
        all_results.append(result)
    
    # Overall summary
    print(f"\n{BOLD}{'=' * 80}{RESET}")
    print(f"{BOLD}OVERALL RESULTS{RESET}")
    print(f"{BOLD}{'=' * 80}{RESET}")
    
    total_tests = sum(r["total"] for r in all_results)
    total_pass = sum(r["pass"] for r in all_results)
    overall_rate = (total_pass / total_tests * 100) if total_tests > 0 else 0
    
    for r in all_results:
        color = GREEN if r["rate"] >= 80 else YELLOW if r["rate"] >= 60 else RED
        status = "✓" if r["rate"] >= 80 else "⚠" if r["rate"] >= 60 else "✗"
        print(f"{color}{status}{RESET} {r['provider']:15} {r['pass']:3}/{r['total']:3} ({r['rate']:.1f}%)")
    
    print(f"\n{BOLD}Overall: {total_pass}/{total_tests} ({overall_rate:.1f}%){RESET}")
    
    if overall_rate >= 90:
        print(f"{GREEN}{BOLD}✅ EXCELLENT{RESET}")
    elif overall_rate >= 75:
        print(f"{YELLOW}{BOLD}⚠️ GOOD{RESET}")
    else:
        print(f"{RED}{BOLD}❌ NEEDS WORK{RESET}")
    
    # Save results
    results_file = f"test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(results_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "overall_pass": total_pass,
            "overall_total": total_tests,
            "overall_rate": overall_rate,
            "providers": all_results
        }, f, indent=2)
    
    print(f"\nResults saved to: {results_file}")
    return overall_rate >= 75

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
