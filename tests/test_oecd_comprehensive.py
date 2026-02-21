#!/usr/bin/env python3
"""
Comprehensive test suite for OECD provider

Tests a broad range of queries across multiple countries and indicators
to measure overall accuracy improvement.
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_path.parent))

from backend.providers.oecd import OECDProvider
from backend.services.metadata_search import MetadataSearchService
from backend.services.openrouter import OpenRouterService
from backend.config import get_settings


async def test_oecd_comprehensive():
    """Comprehensive test of OECD provider."""

    # Load settings
    settings = get_settings()

    # Initialize LLM provider
    llm_provider = OpenRouterService(api_key=settings.openrouter_api_key)

    # Initialize metadata search service
    metadata_search = MetadataSearchService(llm_provider=llm_provider)

    # Initialize OECD provider with metadata search
    oecd = OECDProvider(metadata_search_service=metadata_search)

    test_cases = [
        # GDP queries
        {"name": "GDP for USA", "indicator": "GDP", "country": "USA"},
        {"name": "GDP growth for Italy", "indicator": "GDP_GROWTH", "country": "Italy"},
        {"name": "GDP for Germany", "indicator": "GDP", "country": "Germany"},
        {"name": "GDP growth for France", "indicator": "GDP_GROWTH", "country": "France"},
        {"name": "GDP for Japan", "indicator": "GDP", "country": "Japan"},
        {"name": "Real GDP for UK", "indicator": "REAL_GDP", "country": "UK"},

        # Unemployment queries
        {"name": "Unemployment for France", "indicator": "UNEMPLOYMENT", "country": "France"},
        {"name": "Unemployment rate for Italy", "indicator": "UNEMPLOYMENT_RATE", "country": "ITA"},
        {"name": "Unemployment for Spain", "indicator": "UNEMPLOYMENT", "country": "Spain"},
        {"name": "Unemployment for Germany", "indicator": "UNEMPLOYMENT", "country": "Germany"},
        {"name": "Unemployment for Canada", "indicator": "UNEMPLOYMENT", "country": "Canada"},
        {"name": "Jobless rate for Australia", "indicator": "JOBLESS_RATE", "country": "Australia"},

        # Inflation/Price queries
        {"name": "Inflation for USA", "indicator": "INFLATION", "country": "USA"},
        {"name": "CPI for Germany", "indicator": "CPI", "country": "Germany"},
        {"name": "Inflation for France", "indicator": "INFLATION", "country": "France"},
        {"name": "Consumer prices for Italy", "indicator": "CONSUMER_PRICE_INDEX", "country": "Italy"},

        # OECD average/group queries
        {"name": "OECD average inflation", "indicator": "INFLATION", "country": "OECD"},
        {"name": "OECD GDP growth", "indicator": "GDP_GROWTH", "country": "OECD_AVERAGE"},
        {"name": "G7 unemployment", "indicator": "UNEMPLOYMENT", "country": "G7"},
        {"name": "Euro Area GDP", "indicator": "GDP", "country": "EURO_AREA"},

        # Various countries
        {"name": "Netherlands unemployment", "indicator": "UNEMPLOYMENT", "country": "Netherlands"},
        {"name": "Belgium GDP", "indicator": "GDP", "country": "Belgium"},
        {"name": "Sweden inflation", "indicator": "INFLATION", "country": "Sweden"},
        {"name": "Norway GDP growth", "indicator": "GDP_GROWTH", "country": "Norway"},
        {"name": "Switzerland unemployment", "indicator": "UNEMPLOYMENT", "country": "Switzerland"},
        {"name": "Austria GDP", "indicator": "GDP", "country": "Austria"},

        # Edge cases with different time periods
        {"name": "Recent GDP for USA", "indicator": "GDP", "country": "USA", "start_year": 2022},
        {"name": "Historical unemployment Italy", "indicator": "UNEMPLOYMENT", "country": "Italy", "start_year": 2015, "end_year": 2018},
        {"name": "Long-term GDP growth UK", "indicator": "GDP_GROWTH", "country": "UK", "start_year": 2010, "end_year": 2024},
    ]

    results = {
        "passed": [],
        "failed": [],
    }

    print("=" * 80)
    print("OECD Provider Comprehensive Test Suite")
    print("=" * 80)
    print(f"Total tests: {len(test_cases)}")
    print()

    for i, test in enumerate(test_cases, 1):
        print(f"Test {i}/{len(test_cases)}: {test['name']}", end=" ... ", flush=True)

        try:
            data = await oecd.fetch_indicator(
                indicator=test["indicator"],
                country=test["country"],
                start_year=test.get("start_year"),
                end_year=test.get("end_year"),
            )

            # Validate response
            if not data.data:
                raise ValueError("No data points returned")

            if len(data.data) < 1:
                raise ValueError(f"Too few data points: {len(data.data)}")

            # Check that values are reasonable
            values = [d.value for d in data.data if d.value is not None]
            if not values:
                raise ValueError("All values are None")

            print(f"✅ PASSED ({len(data.data)} points)")
            results["passed"].append(test["name"])

        except Exception as e:
            print(f"❌ FAILED")
            print(f"   Error: {str(e)[:100]}")
            results["failed"].append({"name": test["name"], "error": str(e)})

    # Summary
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)
    print(f"Passed: {len(results['passed'])}/{len(test_cases)}")
    print(f"Failed: {len(results['failed'])}/{len(test_cases)}")

    success_rate = len(results["passed"]) / len(test_cases)
    print(f"Success rate: {success_rate * 100:.1f}%")
    print()

    if results["failed"]:
        print("Failed tests:")
        for failure in results["failed"]:
            print(f"  - {failure['name']}")
            print(f"    Error: {failure['error'][:150]}")
        print()

    # Return success if >90% passed
    if success_rate >= 0.9:
        print("🎉 SUCCESS: >90% accuracy achieved!")
        print(f"   Previous: 33% (10/30 tests)")
        print(f"   Current: {success_rate * 100:.1f}% ({len(results['passed'])}/{len(test_cases)} tests)")
        print(f"   Improvement: +{(success_rate - 0.33) * 100:.1f} percentage points")
        return 0
    else:
        print(f"⚠️  WARNING: {success_rate * 100:.1f}% accuracy (target: >90%)")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_oecd_comprehensive())
    sys.exit(exit_code)
