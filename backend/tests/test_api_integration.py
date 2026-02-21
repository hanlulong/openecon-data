#!/usr/bin/env python3
"""
Test SDMX-First Integration with Backend API

Makes actual API queries to verify that SDMX-first metadata discovery
works correctly in the full backend system.
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir.parent))

import httpx


class APITestCase:
    def __init__(self, name: str, query: str, expected_provider: str = None):
        self.name = name
        self.query = query
        self.expected_provider = expected_provider


async def run_api_query(client: httpx.AsyncClient, test_case: APITestCase):
    """Test a single API query."""
    print(f"🔍 Test: {test_case.name}")
    print(f"   Query: \"{test_case.query}\"")

    try:
        response = await client.post(
            "http://localhost:3001/api/query",
            json={"query": test_case.query},
            timeout=30.0
        )
        response.raise_for_status()
        result = response.json()

        if result.get("clarificationNeeded"):
            print(f"   ⚠️ Clarification needed: {result.get('clarificationQuestions', [])}")
            return ("CLARIFICATION", test_case.name, "Needs clarification")

        if result.get("error"):
            print(f"   ❌ Error: {result['error']}")
            return ("ERROR", test_case.name, result['error'][:50])

        if result.get("data"):
            data = result["data"][0]
            provider = data["metadata"]["source"]
            indicator = data["metadata"]["indicator"]
            points = len(data["data"])

            print(f"   ✅ Success!")
            print(f"      Provider: {provider}")
            print(f"      Indicator: {indicator[:60]}")
            print(f"      Data points: {points}")

            if test_case.expected_provider and provider != test_case.expected_provider:
                print(f"      ⚠️ Expected {test_case.expected_provider}, got {provider}")
                return ("WARN", test_case.name, f"Provider mismatch: expected {test_case.expected_provider}, got {provider}")

            return ("PASS", test_case.name, f"{provider} with {points} points")

        print(f"   ❌ Unexpected response format")
        return ("ERROR", test_case.name, "Unexpected response")

    except httpx.HTTPStatusError as e:
        print(f"   ❌ HTTP Error: {e.response.status_code}")
        return ("ERROR", test_case.name, f"HTTP {e.response.status_code}")
    except Exception as e:
        print(f"   ❌ Exception: {str(e)[:50]}")
        return ("ERROR", test_case.name, str(e)[:50])
    finally:
        print()


async def main():
    print("="*70)
    print("🧪 Testing SDMX-First API Integration")
    print("="*70)
    print()

    # Check if backend is running
    async with httpx.AsyncClient() as client:
        try:
            health = await client.get("http://localhost:3001/api/health", timeout=5.0)
            health.raise_for_status()
            print("✅ Backend is running")
            print()
        except Exception as e:
            print("❌ Backend is not running!")
            print(f"   Error: {e}")
            print()
            print("Please start the backend with:")
            print("   source backend/.venv/bin/activate")
            print("   uvicorn backend.main:app --host 0.0.0.0 --port 3001 --reload")
            print()
            sys.exit(1)

    # Define test cases covering different providers
    test_cases = [
        # WorldBank tests (should use SDMX if available, fallback to REST API)
        APITestCase(
            "WorldBank: US GDP",
            "Show me US GDP for the last 5 years"
        ),
        APITestCase(
            "WorldBank: China Population",
            "What is China's population over the last 10 years?"
        ),

        # IMF tests (should use SDMX for most indicators)
        APITestCase(
            "IMF: US Unemployment",
            "Show me US unemployment rate from IMF for the last 5 years"
        ),
        APITestCase(
            "IMF: Government Debt",
            "What is Germany's government debt from IMF?"
        ),

        # BIS tests (should use SDMX)
        APITestCase(
            "BIS: Policy Rate",
            "Show me US policy rate from BIS"
        ),

        # Eurostat tests (should use SDMX with 7,986 dataflows available)
        APITestCase(
            "Eurostat: EU GDP",
            "Show me EU GDP from Eurostat for the last 5 years"
        ),
        APITestCase(
            "Eurostat: Germany Unemployment",
            "What is Germany's unemployment rate from Eurostat?"
        ),

        # StatsCan tests (might use SDMX fallback)
        APITestCase(
            "StatsCan: Canadian GDP",
            "Show me Canadian GDP for the last 5 years"
        ),
        APITestCase(
            "StatsCan: Immigration",
            "What is Canada's immigration data?"
        ),
    ]

    results = []
    async with httpx.AsyncClient() as client:
        for test_case in test_cases:
            result = await run_api_query(client, test_case)
            results.append(result)

    # Summary
    print("="*70)
    print("📊 Test Summary")
    print("="*70)

    passed = sum(1 for r in results if r[0] == "PASS")
    warnings = sum(1 for r in results if r[0] == "WARN")
    clarifications = sum(1 for r in results if r[0] == "CLARIFICATION")
    errors = sum(1 for r in results if r[0] == "ERROR")

    for status, name, detail in results:
        icon = {
            "PASS": "✅",
            "WARN": "⚠️",
            "CLARIFICATION": "💭",
            "ERROR": "❌"
        }[status]
        print(f"{icon} {name}: {detail}")

    print()
    print(f"Results: {passed} passed, {warnings} warnings, {clarifications} clarifications, {errors} errors")

    if errors == 0 and clarifications == 0:
        print("✅ All tests passed successfully!")
    elif errors == 0:
        print("⚠️ Tests passed but some need clarification")
    else:
        print("❌ Some tests failed")

    print("="*70)

    # Check backend logs for SDMX activity
    print()
    print("📋 Check backend logs for SDMX activity:")
    print("   grep -E 'Searching metadata|Found.*results from SDMX|falling back' /tmp/test-backend.log")
    print()


if __name__ == '__main__':
    asyncio.run(main())
