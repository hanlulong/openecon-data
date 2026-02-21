#!/usr/bin/env python3
"""
Comprehensive production testing for all econ-data-mcp data providers
Tests each provider with 20 diverse queries on https://openecon.ai
"""

import requests
import json
import time
from typing import Dict, List, Tuple
from datetime import datetime

# Production API endpoint
PRODUCTION_API = "https://openecon.ai/api"

class ComprehensiveProviderTester:
    def __init__(self):
        self.results = {}
        self.total_tests = 0
        self.total_passed = 0
        self.all_failures = []

    def test_query(self, query: str, provider_name: str, test_name: str) -> Tuple[bool, str, dict]:
        """Test a single query and return detailed results"""
        try:
            response = requests.post(
                f"{PRODUCTION_API}/query",
                json={"query": query},
                timeout=60
            )

            result_data = {
                "status_code": response.status_code,
                "response": None
            }

            if response.status_code != 200:
                return False, f"HTTP {response.status_code}", result_data

            data = response.json()
            result_data["response"] = data

            # Check if we got data
            if data.get("data") and len(data["data"]) > 0:
                # Check if correct provider was used
                actual_provider = data.get("intent", {}).get("apiProvider", "").upper()
                expected_upper = provider_name.upper()

                # Flexible provider matching
                provider_matches = {
                    "WORLD BANK": ["WORLDBANK", "WORLD BANK"],
                    "STATISTICS CANADA": ["STATSCAN", "STATISTICS CANADA"],
                    "UN COMTRADE": ["COMTRADE"],
                    "EXCHANGERATE-API": ["EXCHANGERATE", "EXCHANGERATE-API"],
                }

                matched = False
                for expected, variants in provider_matches.items():
                    if expected_upper in expected:
                        if any(v in actual_provider for v in variants):
                            matched = True
                            break

                if not matched:
                    if expected_upper in actual_provider or actual_provider in expected_upper:
                        matched = True

                if matched:
                    return True, f"✓ {len(data['data'])} dataset(s)", result_data
                else:
                    return False, f"Wrong provider: {actual_provider}", result_data

            # Check for clarification needed
            if data.get("clarificationNeeded"):
                questions = data.get("clarificationQuestions", [])
                return False, f"Clarification: {questions[0] if questions else 'needed'}", result_data

            # Check for errors
            if data.get("error"):
                error_msg = data.get("message", data.get("error"))
                return False, f"Error: {data.get('error')}", result_data

            return False, "No data returned", result_data

        except requests.Timeout:
            return False, "Timeout (60s)", {}
        except Exception as e:
            return False, f"Exception: {str(e)[:50]}", {}

    def test_provider(self, provider_name: str, test_cases: List[Tuple[str, str]]):
        """Test a provider with multiple queries"""
        print(f"\n{'='*80}")
        print(f"TESTING {provider_name} ({len(test_cases)} queries)")
        print(f"{'='*80}")

        provider_results = []
        passed = 0

        for i, (query, test_name) in enumerate(test_cases, 1):
            success, message, result_data = self.test_query(query, provider_name, test_name)
            self.total_tests += 1

            status = "✅" if success else "❌"
            print(f"{status} [{i:2d}/20] {test_name:30s}: {message}")

            if success:
                passed += 1
                self.total_passed += 1
            else:
                self.all_failures.append({
                    "provider": provider_name,
                    "test": test_name,
                    "query": query,
                    "message": message,
                    "result_data": result_data
                })

            provider_results.append({
                "test": test_name,
                "query": query,
                "success": success,
                "message": message,
                "result": result_data
            })

            time.sleep(0.3)  # Rate limiting

        accuracy = (passed / len(test_cases) * 100) if test_cases else 0
        self.results[provider_name] = {
            "passed": passed,
            "total": len(test_cases),
            "accuracy": accuracy,
            "details": provider_results
        }

        print(f"\n{provider_name} Results: {accuracy:.1f}% ({passed}/{len(test_cases)})")

    def run_all_tests(self):
        """Run comprehensive tests for all providers"""
        print(f"\n{'='*80}")
        print(f"COMPREHENSIVE OPENECON PRODUCTION TESTING - 20 QUERIES PER PROVIDER")
        print(f"Testing: {PRODUCTION_API}")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*80}")

        # Test health endpoint
        try:
            response = requests.get(f"{PRODUCTION_API}/health", timeout=5)
            if response.status_code == 200:
                print(f"✅ API Health: OK\n")
            else:
                print(f"❌ API Health: Failed\n")
                return
        except Exception as e:
            print(f"❌ API Health: {e}\n")
            return

        # FRED (Federal Reserve Economic Data) - 20 queries
        self.test_provider("FRED", [
            ("Show me US GDP for the last 5 years", "US GDP"),
            ("What is the US unemployment rate?", "Unemployment Rate"),
            ("Show me US inflation rate", "Inflation/CPI"),
            ("US federal funds rate", "Federal Funds Rate"),
            ("Show me S&P 500 index", "S&P 500"),
            ("US industrial production", "Industrial Production"),
            ("US housing starts", "Housing Starts"),
            ("US retail sales", "Retail Sales"),
            ("US consumer confidence", "Consumer Confidence"),
            ("US manufacturing index", "Manufacturing PMI"),
            ("US trade deficit", "Trade Balance"),
            ("US labor force participation", "Labor Force"),
            ("US personal income", "Personal Income"),
            ("US savings rate", "Savings Rate"),
            ("US M2 money supply", "Money Supply M2"),
            ("US treasury yields", "Treasury Yields"),
            ("US median home price", "Home Prices"),
            ("US corporate profits", "Corporate Profits"),
            ("US capacity utilization", "Capacity Utilization"),
            ("US durable goods orders", "Durable Goods"),
        ])

        # World Bank - 20 queries
        self.test_provider("World Bank", [
            ("Show me global GDP growth", "Global GDP Growth"),
            ("China GDP last 10 years", "China GDP"),
            ("India population growth", "India Population"),
            ("Brazil inflation rate", "Brazil Inflation"),
            ("World poverty rate", "Poverty Rate"),
            ("Germany GDP per capita", "Germany GDP/Capita"),
            ("Japan life expectancy", "Life Expectancy"),
            ("Mexico unemployment", "Mexico Unemployment"),
            ("South Africa GDP", "South Africa GDP"),
            ("Indonesia economic growth", "Indonesia Growth"),
            ("Nigeria population", "Nigeria Population"),
            ("Russia GDP", "Russia GDP"),
            ("Turkey inflation", "Turkey Inflation"),
            ("Argentina GDP", "Argentina GDP"),
            ("Thailand exports", "Thailand Exports"),
            ("Vietnam GDP growth", "Vietnam Growth"),
            ("Philippines population", "Philippines Pop"),
            ("Egypt unemployment", "Egypt Unemployment"),
            ("Pakistan GDP", "Pakistan GDP"),
            ("Bangladesh poverty rate", "Bangladesh Poverty"),
        ])

        # UN Comtrade - 20 queries
        self.test_provider("UN Comtrade", [
            ("US imports from China", "US-China Imports"),
            ("Germany exports to France", "Germany-France"),
            ("Japan trade with Korea", "Japan-Korea"),
            ("US oil imports", "US Oil Imports"),
            ("China steel exports", "China Steel"),
            ("UK imports from EU", "UK-EU Trade"),
            ("Canada exports to US", "Canada-US"),
            ("Mexico trade balance", "Mexico Balance"),
            ("India exports", "India Exports"),
            ("Brazil commodity exports", "Brazil Commodities"),
            ("Australia mineral exports", "Australia Minerals"),
            ("South Korea electronics exports", "Korea Electronics"),
            ("Singapore trade", "Singapore Trade"),
            ("Vietnam exports to US", "Vietnam-US"),
            ("Thailand rice exports", "Thailand Rice"),
            ("Indonesia palm oil exports", "Indonesia Palm Oil"),
            ("Malaysia exports", "Malaysia Exports"),
            ("Philippines imports", "Philippines Imports"),
            ("Taiwan technology exports", "Taiwan Tech"),
            ("Hong Kong trade", "Hong Kong Trade"),
        ])

        # Statistics Canada - 20 queries
        self.test_provider("Statistics Canada", [
            ("Canada unemployment rate", "Unemployment"),
            ("Canada housing starts", "Housing Starts"),
            ("Canada inflation rate", "CPI/Inflation"),
            ("Canada GDP", "GDP"),
            ("Toronto population", "Toronto Pop"),
            ("Canada employment", "Employment"),
            ("Canada retail sales", "Retail Sales"),
            ("Canada manufacturing", "Manufacturing"),
            ("Canada exports", "Exports"),
            ("Canada imports", "Imports"),
            ("Vancouver housing prices", "Vancouver Housing"),
            ("Ontario GDP", "Ontario GDP"),
            ("Quebec unemployment", "Quebec Unemployment"),
            ("Alberta employment", "Alberta Employment"),
            ("British Columbia population", "BC Population"),
            ("Canada labor force", "Labor Force"),
            ("Canada wage growth", "Wages"),
            ("Canada construction", "Construction"),
            ("Canada business investment", "Investment"),
            ("Canada productivity", "Productivity"),
        ])

        # IMF (International Monetary Fund) - 20 queries
        self.test_provider("IMF", [
            ("IMF world economic outlook", "World Outlook"),
            ("France current account balance", "France Current Acct"),
            ("Italy inflation IMF data", "Italy Inflation"),
            ("Spain GDP growth IMF", "Spain GDP"),
            ("Greece unemployment IMF", "Greece Unemployment"),
            ("Portugal debt to GDP", "Portugal Debt"),
            ("Ireland GDP IMF", "Ireland GDP"),
            ("Belgium current account", "Belgium Current"),
            ("Netherlands GDP", "Netherlands GDP"),
            ("Austria unemployment", "Austria Unemployment"),
            ("Finland GDP growth", "Finland GDP"),
            ("Denmark inflation", "Denmark Inflation"),
            ("Sweden unemployment", "Sweden Unemployment"),
            ("Norway GDP", "Norway GDP"),
            ("Switzerland current account", "Swiss Current"),
            ("Poland GDP growth", "Poland GDP"),
            ("Czech Republic inflation", "Czech Inflation"),
            ("Hungary unemployment", "Hungary Unemployment"),
            ("Romania GDP", "Romania GDP"),
            ("Croatia current account", "Croatia Current"),
        ])

        # BIS (Bank for International Settlements) - 20 queries
        self.test_provider("BIS", [
            ("UK property prices", "UK Property"),
            ("US house prices BIS", "US Property"),
            ("Eurozone credit to GDP ratio", "EZ Credit/GDP"),
            ("Germany residential property prices", "Germany Property"),
            ("Japan housing prices", "Japan Property"),
            ("France property prices", "France Property"),
            ("Canada house prices", "Canada Property"),
            ("Australia property prices", "Australia Property"),
            ("Switzerland property prices", "Swiss Property"),
            ("Sweden house prices", "Sweden Property"),
            ("Spain property prices", "Spain Property"),
            ("Italy house prices", "Italy Property"),
            ("Netherlands property prices", "Netherlands Property"),
            ("Belgium house prices", "Belgium Property"),
            ("Norway property prices", "Norway Property"),
            ("Denmark house prices", "Denmark Property"),
            ("Finland property prices", "Finland Property"),
            ("Ireland property prices", "Ireland Property"),
            ("New Zealand property prices", "NZ Property"),
            ("South Korea house prices", "Korea Property"),
        ])

        # Eurostat - 20 queries
        self.test_provider("Eurostat", [
            ("Germany GDP Eurostat", "Germany GDP"),
            ("France unemployment rate", "France Unemployment"),
            ("Italy inflation Eurostat", "Italy Inflation"),
            ("Spain GDP growth", "Spain GDP"),
            ("EU GDP", "EU GDP"),
            ("Eurozone inflation", "EZ Inflation"),
            ("Poland GDP", "Poland GDP"),
            ("Netherlands unemployment", "Netherlands Unemp"),
            ("Belgium GDP", "Belgium GDP"),
            ("Austria inflation", "Austria Inflation"),
            ("Sweden GDP", "Sweden GDP"),
            ("Denmark unemployment", "Denmark Unemp"),
            ("Finland GDP", "Finland GDP"),
            ("Ireland inflation", "Ireland Inflation"),
            ("Portugal GDP", "Portugal GDP"),
            ("Greece unemployment", "Greece Unemp"),
            ("Czech Republic GDP", "Czech GDP"),
            ("Romania inflation", "Romania Inflation"),
            ("Hungary GDP", "Hungary GDP"),
            ("Bulgaria unemployment", "Bulgaria Unemp"),
        ])

        # OECD - 20 queries
        self.test_provider("OECD", [
            ("OECD unemployment rate", "OECD Unemployment"),
            ("Japan GDP growth OECD", "Japan GDP"),
            ("Korea inflation OECD", "Korea Inflation"),
            ("Australia unemployment", "Australia Unemp"),
            ("Canada GDP OECD", "Canada GDP"),
            ("New Zealand GDP", "NZ GDP"),
            ("Chile GDP growth", "Chile GDP"),
            ("Mexico unemployment", "Mexico Unemp"),
            ("Turkey inflation OECD", "Turkey Inflation"),
            ("Israel GDP", "Israel GDP"),
            ("Switzerland unemployment", "Swiss Unemp"),
            ("Norway GDP OECD", "Norway GDP"),
            ("Iceland inflation", "Iceland Inflation"),
            ("Luxembourg GDP", "Luxembourg GDP"),
            ("Slovenia unemployment", "Slovenia Unemp"),
            ("Estonia GDP", "Estonia GDP"),
            ("Latvia inflation", "Latvia Inflation"),
            ("Lithuania GDP", "Lithuania GDP"),
            ("Slovakia unemployment", "Slovakia Unemp"),
            ("Colombia GDP OECD", "Colombia GDP"),
        ])

        # ExchangeRate-API - 20 queries
        self.test_provider("ExchangeRate-API", [
            ("USD to EUR exchange rate", "USD/EUR"),
            ("GBP to USD rate", "GBP/USD"),
            ("JPY to USD", "JPY/USD"),
            ("CAD to USD", "CAD/USD"),
            ("EUR to GBP", "EUR/GBP"),
            ("AUD to USD", "AUD/USD"),
            ("CHF to EUR", "CHF/EUR"),
            ("CNY to USD", "CNY/USD"),
            ("INR to USD", "INR/USD"),
            ("BRL to USD", "BRL/USD"),
            ("MXN to USD", "MXN/USD"),
            ("ZAR to USD", "ZAR/USD"),
            ("KRW to USD", "KRW/USD"),
            ("SGD to USD", "SGD/USD"),
            ("HKD to USD", "HKD/USD"),
            ("NZD to USD", "NZD/USD"),
            ("SEK to EUR", "SEK/EUR"),
            ("NOK to EUR", "NOK/EUR"),
            ("DKK to EUR", "DKK/EUR"),
            ("PLN to EUR", "PLN/EUR"),
        ])

        # CoinGecko - 20 queries
        self.test_provider("CoinGecko", [
            ("Bitcoin price", "Bitcoin"),
            ("Ethereum price", "Ethereum"),
            ("BTC price history", "Bitcoin History"),
            ("ETH market cap", "Ethereum Cap"),
            ("Crypto market overview", "Market Overview"),
            ("Cardano price", "Cardano"),
            ("Solana price", "Solana"),
            ("Polkadot price", "Polkadot"),
            ("Ripple XRP price", "XRP"),
            ("Dogecoin price", "Dogecoin"),
            ("Litecoin price", "Litecoin"),
            ("Chainlink price", "Chainlink"),
            ("Uniswap price", "Uniswap"),
            ("Avalanche price", "Avalanche"),
            ("Polygon price", "Polygon"),
            ("Stellar price", "Stellar"),
            ("VeChain price", "VeChain"),
            ("Algorand price", "Algorand"),
            ("Cosmos price", "Cosmos"),
            ("Tezos price", "Tezos"),
        ])

        self.print_summary()
        self.save_detailed_report()

    def print_summary(self):
        """Print comprehensive summary"""
        print(f"\n{'='*80}")
        print(f"COMPREHENSIVE TEST SUMMARY - 200 TOTAL QUERIES")
        print(f"{'='*80}\n")

        overall_accuracy = (self.total_passed / self.total_tests * 100) if self.total_tests > 0 else 0

        print(f"Overall Accuracy: {overall_accuracy:.1f}% ({self.total_passed}/{self.total_tests})")
        print(f"\nPer-Provider Results:")
        print(f"{'-'*80}")

        # Sort by accuracy
        sorted_providers = sorted(
            self.results.items(),
            key=lambda x: x[1]["accuracy"],
            reverse=True
        )

        for provider, result in sorted_providers:
            status = "✅" if result["accuracy"] >= 80 else "⚠️" if result["accuracy"] >= 60 else "❌"
            print(f"{status} {provider:25s}: {result['accuracy']:5.1f}% ({result['passed']:2d}/20)")

        print(f"\n{'='*80}")
        print(f"Total Failures: {len(self.all_failures)}")
        print(f"{'='*80}")

    def save_detailed_report(self):
        """Save detailed JSON report"""
        report = {
            "timestamp": datetime.now().isoformat(),
            "total_tests": self.total_tests,
            "total_passed": self.total_passed,
            "overall_accuracy": (self.total_passed / self.total_tests * 100) if self.total_tests > 0 else 0,
            "provider_results": self.results,
            "all_failures": self.all_failures
        }

        filename = f"test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = f"/home/hanlulong/econ-data-mcp/scripts/{filename}"

        with open(filepath, 'w') as f:
            json.dump(report, f, indent=2)

        print(f"\n📊 Detailed results saved to: {filepath}")


if __name__ == "__main__":
    tester = ComprehensiveProviderTester()
    tester.run_all_tests()
