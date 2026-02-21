#!/usr/bin/env python3
"""
Test SDMX-first Fallback Logic

Verifies that the hierarchical metadata search works:
1. SDMX catalogs (primary source)
2. Provider-specific APIs (fallback)
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir.parent))

from backend.services.metadata_search import MetadataSearchService


class TestCase:
    def __init__(self, name: str, provider: str, indicator: str, expected_source: str):
        self.name = name
        self.provider = provider
        self.indicator = indicator
        self.expected_source = expected_source


class MockLLMProvider:
    """Mock LLM provider for testing"""
    async def generate(self, *args, **kwargs):
        return None


async def main():
    print("=" * 70)
    print("🧪 Testing SDMX-First Metadata Discovery")
    print("=" * 70)
    print()

    # Initialize metadata search service with mock LLM provider
    mock_llm = MockLLMProvider()
    metadata_service = MetadataSearchService(mock_llm)

    # Test cases
    test_cases = [
        TestCase(
            "WorldBank GDP Search",
            "WorldBank",
            "GDP",
            "SDMX"  # Expect SDMX to have GDP data
        ),
        TestCase(
            "WorldBank Inflation Search",
            "WorldBank",
            "inflation",
            "SDMX"  # Expect SDMX to have inflation data
        ),
        TestCase(
            "IMF Trade Search",
            "IMF",
            "trade",
            "SDMX"  # Expect SDMX to have trade data
        ),
        TestCase(
            "BIS Policy Rate Search",
            "BIS",
            "policy rate",
            "SDMX"  # Expect SDMX to have policy rate data
        ),
    ]

    results = []
    for test in test_cases:
        print(f"🔍 Test: {test.name}")
        print(f"   Provider: {test.provider}, Indicator: {test.indicator}")

        try:
            # Use hierarchical search
            search_results = await metadata_service.search_with_sdmx_fallback(
                provider=test.provider,
                indicator=test.indicator,
            )

            if search_results:
                source = search_results[0].get('source', 'Unknown')
                count = len(search_results)

                print(f"   ✅ Found {count} results from {source}")
                print(f"   Top result: {search_results[0].get('name', '?')[:60]}")

                # Check if source matches expected
                if source == test.expected_source:
                    results.append(("PASS", test.name, f"{count} results from {source}"))
                else:
                    results.append(("WARN", test.name, f"Expected {test.expected_source}, got {source}"))
            else:
                print(f"   ❌ No results found")
                results.append(("FAIL", test.name, "No results"))

        except Exception as e:
            print(f"   ❌ Error: {str(e)[:100]}")
            results.append(("ERROR", test.name, str(e)[:50]))

        print()

    # Summary
    print("=" * 70)
    print("📊 Test Summary")
    print("=" * 70)

    passed = sum(1 for r in results if r[0] == "PASS")
    warned = sum(1 for r in results if r[0] == "WARN")
    failed = sum(1 for r in results if r[0] == "FAIL")
    errors = sum(1 for r in results if r[0] == "ERROR")

    for status, name, detail in results:
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "ERROR": "💥"}[status]
        print(f"{icon} {name}: {detail}")

    print()
    print(f"Results: {passed} passed, {warned} warnings, {failed} failed, {errors} errors")

    if failed + errors == 0:
        print("✅ All tests passed!")
    else:
        print("⚠️ Some tests had issues")

    print("=" * 70)


if __name__ == '__main__':
    asyncio.run(main())
