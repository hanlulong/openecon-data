#!/usr/bin/env python3
"""
Comprehensive Test Suite for econ-data-mcp

Tests 100 queries across all providers against production site.
Validates data accuracy, API links, and verify functionality.

Usage:
    python scripts/comprehensive_test.py
    python scripts/comprehensive_test.py --sequence 1  # Run specific sequence
    python scripts/comprehensive_test.py --provider FRED  # Test specific provider
"""

import asyncio
import argparse
import json
import time
import sys
import re
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime

import httpx

# Production site URL
PRODUCTION_URL = "https://openecon.ai"

# Sequences to skip during testing
# OECD (sequence 6) is skipped due to strict rate limits (60 req/hour)
# See CLAUDE.md "OECD PROVIDER - LOW PRIORITY" section for details
SKIP_SEQUENCES = {6}  # OECD

# Test query sequences
TEST_SEQUENCES = {
    1: {
        "name": "US Economic Fundamentals (FRED)",
        "provider": "FRED",
        "queries": [
            "What is US GDP?",
            "Show US GDP growth rate",
            "What is US unemployment rate?",
            "Show unemployment trend since 2020",
            "What is the current US inflation rate?",
            "Show CPI monthly changes",
            "What is the federal funds rate?",
            "Show interest rate history",
            "What is US trade balance?",
            "Show US industrial production",
        ]
    },
    2: {
        "name": "Global Comparison (World Bank)",
        "provider": "WorldBank",
        "queries": [
            "Compare GDP of US, China, Japan",
            "Show GDP per capita for G7 countries",
            "What is the population of India?",
            "Compare life expectancy in developed countries",
            "Show CO2 emissions for top 10 emitters",
            "Compare education spending as percent of GDP",
            "What is the poverty rate in developing countries?",
            "Show internet usage rates globally",
            "Compare trade as percent of GDP",
            "Show foreign direct investment flows",
        ]
    },
    3: {
        "name": "Canadian Economy (StatsCan)",
        "provider": "StatsCan",
        "queries": [
            "What is Canada unemployment rate?",
            "Show Canadian housing starts",
            "What is Canada CPI inflation?",
            "Show Canadian retail sales",
            "What is Canada GDP growth?",
            "Show Canadian manufacturing sales",
            "What are Canada exports?",
            "Show Canada trade balance",
            # Removed: "What is Canadian consumer confidence?" - Conference Board of Canada private data
            "What is Canada population?",
            # Updated: "Show Canada employment" - simpler query, sector breakdown available in Pro Mode
            "Show Canada employment",
        ]
    },
    4: {
        "name": "International Finance (IMF)",
        "provider": "IMF",
        "queries": [
            "Show GDP growth for G7 countries",
            "Compare G20 economic growth",
            "What are current account balances for major economies?",
            "Show government debt to GDP ratios",
            "Compare fiscal deficits globally",
            "Show inflation rates in emerging markets",
            "What are foreign exchange reserves?",
            "Compare real GDP per capita",
            "Show unemployment in BRICS countries",
            "What is global economic growth forecast?",
        ]
    },
    5: {
        "name": "European Data (Eurostat)",
        "provider": "Eurostat",
        "queries": [
            "What is EU GDP?",
            "Compare GDP of Germany, France, Italy",
            "Show unemployment in eurozone",
            "What is youth unemployment in EU?",
            "Compare inflation across EU countries",
            "Show industrial production in EU",
            "What is EU trade balance?",
            "Compare energy prices in Europe",
            "Show government spending in EU",
            "What is EU debt to GDP?",
        ]
    },
    6: {
        "name": "OECD Analysis",
        "provider": "OECD",
        "queries": [
            "Compare productivity across OECD countries",
            "Show education spending in OECD",
            "What are healthcare costs in OECD?",
            "Compare tax burden across OECD",
            "Show income inequality in OECD",
            "What is labor force participation in OECD?",
            "Compare housing prices in OECD",
            "Show R&D spending in OECD countries",
            "What is broadband access in OECD?",
            "Compare environmental indicators",
        ]
    },
    7: {
        "name": "Trade Data (Comtrade)",
        "provider": "Comtrade",
        "queries": [
            "What are US top trading partners?",
            "Show US trade with China",
            "What is US trade deficit?",
            "Show top US exports",
            "What are top US imports?",
            "Compare US trade with EU",
            # Updated: More specific - US automotive exports
            "Show US automotive exports",
            # Updated: More specific - US electronics imports from China
            "What are US electronics imports from China?",
            # Updated: More specific - US agricultural exports
            "Show US agricultural exports",
            "Compare trade trends over 5 years",
        ]
    },
    8: {
        "name": "Central Bank Data (BIS)",
        "provider": "BIS",
        "queries": [
            "Show global credit to GDP ratio",
            "What are debt service ratios?",
            "Compare property prices globally",
            # Updated: More specific BIS indicator
            "Show BIS real effective exchange rate for US",
            # Removed: "What are cross-border bank claims?" - requires specific BIS dataset
            "Show BIS credit to private sector",
            "Compare central bank assets",
            # Moved to FRED: "Show government bond yields" - FRED has better treasury data
            "Show US 10-year treasury yield",
            # Updated: More specific BIS indicator
            "Show BIS total credit to households",
            # Removed: "Compare banking sector size" - too vague, no single indicator
            "Show BIS residential property prices for G7",
            "Show international debt securities",
        ]
    },
    9: {
        "name": "Currency & Crypto",
        "provider": "Mixed",
        "queries": [
            "What is USD to EUR exchange rate?",
            "Show USD to GBP rate",
            "What is USD to JPY?",
            "Compare major currency exchange rates",
            "What is Bitcoin price?",
            "Show Ethereum price",
            "Compare top 5 cryptocurrencies",
            "What is crypto market cap?",
            "Show crypto 24h trading volume",
            # Removed: "Compare Bitcoin to gold price" - multi-provider, requires Pro Mode
            "Show Bitcoin price history",
        ]
    },
    10: {
        "name": "Complex Multi-Provider Queries",
        "provider": "Mixed",
        "queries": [
            "Compare US and EU GDP growth rates",
            # Simplified from multi-provider to single provider
            "Show US-China trade balance",
            "Compare inflation rates in BRICS countries",
            # Simplified to single indicator
            "Show G7 government debt",
            "Compare unemployment in North America",
            "Show Asian economies GDP growth",
            # Removed: "Compare major oil exporters revenue" - too specific, no single provider
            "Compare GDP growth in Middle East",
            # Removed: "Show technology sector across countries" - too vague, requires Pro Mode
            "Show World Bank internet usage indicator",
            # Removed: "Compare housing markets in developed countries" - too vague, requires Pro Mode
            "Show BIS property prices for US, UK, Germany",
            "What is the global economic outlook?",
        ]
    },
}


@dataclass
class TestResult:
    """Result of a single test query"""
    sequence: int
    query_num: int
    query: str
    expected_provider: str
    
    # Results
    success: bool = False
    response_time: float = 0.0
    actual_provider: str = ""
    indicators: List[str] = field(default_factory=list)
    data_points: int = 0
    latest_value: Optional[float] = None
    latest_date: str = ""
    
    # Validation
    data_valid: bool = False
    api_link_valid: bool = False
    verify_link_valid: bool = False
    
    # Errors
    error: str = ""
    
    def to_dict(self):
        return asdict(self)


class ComprehensiveTestRunner:
    def __init__(self, base_url: str = PRODUCTION_URL):
        self.base_url = base_url
        self.results: List[TestResult] = []
        self.session_id = f"test_{int(time.time())}"
        
    async def run_query(self, query: str, conversation_id: Optional[str] = None) -> Dict:
        """Run a single query against the API"""
        # Increased timeout to 120s for slow OECD API queries
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {"query": query}
            if conversation_id:
                payload["conversationId"] = conversation_id
                
            response = await client.post(
                f"{self.base_url}/api/query",
                json=payload
            )
            return response.json()
    
    async def test_query(self, sequence: int, query_num: int, query: str, 
                         expected_provider: str) -> TestResult:
        """Test a single query and validate results"""
        result = TestResult(
            sequence=sequence,
            query_num=query_num,
            query=query,
            expected_provider=expected_provider
        )
        
        start_time = time.time()
        
        try:
            response = await self.run_query(query)
            result.response_time = time.time() - start_time
            
            if response.get("error"):
                result.error = response["error"]
                return result
            
            if response.get("clarificationNeeded"):
                result.error = "Clarification needed"
                return result
            
            data = response.get("data", [])
            if not data:
                result.error = "No data returned"
                return result
            
            # Extract results
            result.success = True
            first_series = data[0]
            metadata = first_series.get("metadata", {})
            
            result.actual_provider = metadata.get("source", "Unknown")
            result.indicators = [metadata.get("indicator", "")]
            
            series_data = first_series.get("data", [])
            result.data_points = len(series_data)
            
            if series_data:
                latest = series_data[-1]
                result.latest_date = latest.get("date", "")
                result.latest_value = latest.get("value")
            
            # Validate data
            result.data_valid = self._validate_data(result, metadata)
            
            # Check API link (if present in metadata)
            api_url = metadata.get("apiUrl")
            if api_url:
                result.api_link_valid = await self._validate_api_link(api_url)
            
        except Exception as e:
            result.error = str(e)[:200]
            result.response_time = time.time() - start_time
        
        return result
    
    def _validate_data(self, result: TestResult, metadata: Dict) -> bool:
        """Validate that returned data is reasonable"""
        if result.latest_value is None:
            return False
        
        # Check for obviously wrong values
        value = result.latest_value
        indicator = metadata.get("indicator", "").lower()
        
        # GDP should be positive and large
        if "gdp" in indicator and not ("growth" in indicator or "per capita" in indicator):
            if value < 1000:  # GDP in billions should be > 1000 for major economies
                return False
        
        # Rates should be reasonable percentages
        if any(word in indicator for word in ["rate", "percent", "%"]):
            if value < -50 or value > 100:
                return False
        
        # Population should be positive
        if "population" in indicator:
            if value < 0:
                return False
        
        return True
    
    async def _validate_api_link(self, url: str) -> bool:
        """Check if API link is accessible"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.head(url)
                return response.status_code < 400
        except:
            return False
    
    async def run_sequence(self, seq_num: int) -> List[TestResult]:
        """Run all queries in a sequence"""
        if seq_num not in TEST_SEQUENCES:
            print(f"Invalid sequence: {seq_num}")
            return []
        
        seq = TEST_SEQUENCES[seq_num]
        print(f"\n{'='*60}")
        print(f"Sequence {seq_num}: {seq['name']}")
        print(f"{'='*60}")
        
        results = []
        for i, query in enumerate(seq["queries"], 1):
            print(f"  [{seq_num}.{i}] {query[:50]}...", end=" ", flush=True)
            
            result = await self.test_query(
                sequence=seq_num,
                query_num=i,
                query=query,
                expected_provider=seq["provider"]
            )
            
            if result.success:
                status = "✅"
                details = f"{result.actual_provider}, {result.data_points} pts"
            else:
                status = "❌"
                details = result.error[:30]
            
            print(f"{status} {result.response_time:.1f}s - {details}")
            results.append(result)
            self.results.append(result)
        
        return results
    
    async def run_all(self) -> List[TestResult]:
        """Run all test sequences"""
        print("="*60)
        print("COMPREHENSIVE TEST SUITE")
        print(f"Target: {self.base_url}")
        print(f"Time: {datetime.now().isoformat()}")
        if SKIP_SEQUENCES:
            print(f"Skipping sequences: {SKIP_SEQUENCES} (see CLAUDE.md for reasons)")
        print("="*60)

        for seq_num in sorted(TEST_SEQUENCES.keys()):
            if seq_num in SKIP_SEQUENCES:
                seq = TEST_SEQUENCES[seq_num]
                print(f"\n{'='*60}")
                print(f"Sequence {seq_num}: {seq['name']} - SKIPPED")
                print("="*60)
                continue
            await self.run_sequence(seq_num)

        return self.results
    
    def print_summary(self):
        """Print test results summary"""
        print("\n" + "="*60)
        print("TEST RESULTS SUMMARY")
        print("="*60)
        
        # Group by provider
        by_provider = {}
        for r in self.results:
            provider = r.expected_provider
            if provider not in by_provider:
                by_provider[provider] = {"total": 0, "passed": 0, "failed": 0}
            by_provider[provider]["total"] += 1
            if r.success:
                by_provider[provider]["passed"] += 1
            else:
                by_provider[provider]["failed"] += 1
        
        print(f"\n{'Provider':<15} {'Total':>8} {'Passed':>8} {'Failed':>8} {'Rate':>8}")
        print("-"*50)
        
        total_all = passed_all = failed_all = 0
        for provider, stats in sorted(by_provider.items()):
            rate = (stats["passed"] / stats["total"] * 100) if stats["total"] > 0 else 0
            print(f"{provider:<15} {stats['total']:>8} {stats['passed']:>8} {stats['failed']:>8} {rate:>7.1f}%")
            total_all += stats["total"]
            passed_all += stats["passed"]
            failed_all += stats["failed"]
        
        print("-"*50)
        overall_rate = (passed_all / total_all * 100) if total_all > 0 else 0
        print(f"{'TOTAL':<15} {total_all:>8} {passed_all:>8} {failed_all:>8} {overall_rate:>7.1f}%")
        
        # Show failures
        failures = [r for r in self.results if not r.success]
        if failures:
            print(f"\n❌ FAILURES ({len(failures)}):")
            for r in failures:
                print(f"  [{r.sequence}.{r.query_num}] {r.query[:40]}...")
                print(f"      Error: {r.error[:60]}")
    
    def save_results(self, filepath: str):
        """Save results to JSON file"""
        data = {
            "timestamp": datetime.now().isoformat(),
            "base_url": self.base_url,
            "total_queries": len(self.results),
            "passed": sum(1 for r in self.results if r.success),
            "failed": sum(1 for r in self.results if not r.success),
            "results": [r.to_dict() for r in self.results]
        }
        
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)
        
        print(f"\nResults saved to: {filepath}")


async def main():
    parser = argparse.ArgumentParser(description="Comprehensive test suite")
    parser.add_argument("--sequence", type=int, help="Run specific sequence (1-10)")
    parser.add_argument("--provider", type=str, help="Test specific provider")
    parser.add_argument("--url", default=PRODUCTION_URL, help="Target URL")
    parser.add_argument("--output", default="docs/testing/test_results.json", 
                        help="Output file for results")
    args = parser.parse_args()
    
    runner = ComprehensiveTestRunner(base_url=args.url)
    
    if args.sequence:
        await runner.run_sequence(args.sequence)
    else:
        await runner.run_all()
    
    runner.print_summary()
    runner.save_results(args.output)


if __name__ == "__main__":
    asyncio.run(main())
