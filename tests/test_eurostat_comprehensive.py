#!/usr/bin/env python3
"""
Comprehensive test of Eurostat provider to verify 95%+ accuracy.
Tests multiple query patterns to ensure proper routing and data retrieval.
"""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from backend.config import get_settings
from backend.services.query import QueryService


async def test_query(query_service: QueryService, query: str, expected_provider: str = "Eurostat") -> dict:
    """Test a single query and return result."""
    print(f"\n{'='*80}")
    print(f"Testing: {query}")
    print('='*80)

    try:
        result = await query_service.process_query(query)

        # Check if clarification was requested
        if hasattr(result, 'clarificationNeeded') and result.clarificationNeeded:
            print("❌ FAILED: Clarification requested (should not ask)")
            print(f"   Clarification questions: {result.clarificationQuestions}")
            return {
                "query": query,
                "status": "FAILED",
                "reason": "clarification_requested",
                "clarification_questions": result.clarificationQuestions
            }

        # Check if we got data
        if hasattr(result, 'data') and result.data:
            data_points = len(result.data)
            provider = result.data[0].metadata.source if data_points > 0 else None

            # Verify it used the expected provider
            if provider != expected_provider:
                print(f"⚠️  WARNING: Used {provider} instead of {expected_provider}")

            print(f"✅ SUCCESS: Retrieved {data_points} data points from {provider}")

            # Show sample data
            if data_points > 0:
                sample = result.data[0]
                print(f"   Indicator: {sample.metadata.indicator}")
                print(f"   Country: {sample.metadata.country}")
                print(f"   Data points: {len(sample.data)}")
                if sample.data:
                    print(f"   Sample values: {sample.data[:3]}")

            return {
                "query": query,
                "status": "SUCCESS",
                "data_points": data_points,
                "provider": provider,
                "country": result.data[0].metadata.country if data_points > 0 else None
            }

        # No data returned
        print("❌ FAILED: No data returned")
        return {
            "query": query,
            "status": "FAILED",
            "reason": "no_data"
        }

    except Exception as e:
        print(f"❌ FAILED: Exception raised")
        print(f"   Error: {str(e)}")
        return {
            "query": query,
            "status": "FAILED",
            "reason": "exception",
            "error": str(e)
        }


async def main():
    """Run comprehensive Eurostat tests."""
    settings = get_settings()
    query_service = QueryService(
        openrouter_key=settings.openrouter_api_key,
        fred_key=settings.fred_api_key,
        comtrade_key=settings.comtrade_api_key,
        coingecko_key=settings.coingecko_api_key,
        settings=settings
    )

    # Comprehensive test queries covering different patterns
    test_queries = [
        # Unemployment queries
        "What is Germany unemployment rate?",
        "Show me France unemployment",
        "Italy unemployment 2023",
        "Unemployment in Spain",

        # GDP queries
        "Show me EU GDP growth",
        "Germany GDP",
        "France GDP from 2020 to 2024",
        "What is Italy GDP?",

        # Inflation queries
        "Germany inflation",
        "France inflation rate",
        "EU inflation",

        # House prices
        "Germany house prices",
        "Spain property prices",

        # Other indicators
        "Belgium retail trade",
        "Netherlands industrial production",
    ]

    results = []
    for query in test_queries:
        result = await test_query(query_service, query)
        results.append(result)
        # Small delay to avoid rate limiting
        await asyncio.sleep(0.5)

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print('='*80)

    success_count = sum(1 for r in results if r['status'] == 'SUCCESS')
    total_count = len(results)
    accuracy = (success_count / total_count * 100) if total_count > 0 else 0

    print(f"Total queries: {total_count}")
    print(f"Successful: {success_count}")
    print(f"Failed: {total_count - success_count}")
    print(f"Accuracy: {accuracy:.1f}%")

    # Show provider distribution
    providers = {}
    for r in results:
        if r['status'] == 'SUCCESS':
            provider = r.get('provider', 'unknown')
            providers[provider] = providers.get(provider, 0) + 1

    print(f"\nProvider distribution:")
    for provider, count in sorted(providers.items()):
        print(f"  {provider}: {count}")

    # Show failures
    failures = [r for r in results if r['status'] == 'FAILED']
    if failures:
        print(f"\nFailed queries:")
        for result in failures:
            print(f"  - {result['query']}")
            print(f"    Reason: {result.get('reason', 'unknown')}")
            if 'error' in result:
                print(f"    Error: {result['error']}")

    # Return exit code
    if accuracy >= 95:
        print("\n🎉 SUCCESS: 95%+ accuracy achieved!")
        return 0
    else:
        print(f"\n⚠️  WARNING: Accuracy {accuracy:.1f}% below target 95%")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
