"""
Integration Test: Verify ProviderRouter Integration

Tests that the ProviderRouter correctly overrides providers when integrated into the query pipeline.
"""
import asyncio
import json
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.services.query import QueryService
from backend.config import get_settings


async def test_integration():
    """Test ProviderRouter integration with real query processing"""

    settings = get_settings()
    query_service = QueryService(
        openrouter_key=settings.openrouter_api_key,
        fred_key=settings.fred_api_key,
        comtrade_key=settings.comtrade_api_key,
        coingecko_key=settings.coingecko_api_key,
        settings=settings
    )

    # Test queries that should trigger ProviderRouter overrides
    test_cases = [
        {
            "query": "Show me Canada GDP",
            "expected_provider": "StatsCan",
            "description": "Canadian query should route to StatsCan"
        },
        {
            "query": "Case-Shiller home price index",
            "expected_provider": "FRED",
            "description": "US-only indicator should route to FRED"
        },
        {
            "query": "Germany house prices",
            "expected_provider": "BIS",
            "description": "Non-US housing should route to BIS"
        },
        {
            "query": "Get Italy GDP from OECD",
            "expected_provider": "OECD",
            "description": "Explicit provider should be honored"
        },
    ]

    print("\n" + "="*80)
    print("INTEGRATION TEST: ProviderRouter in Query Pipeline")
    print("="*80)

    passed = 0
    failed = 0

    for i, test in enumerate(test_cases, 1):
        print(f"\n[Test {i}/{len(test_cases)}] {test['description']}")
        print(f"Query: \"{test['query']}\"")
        print(f"Expected provider: {test['expected_provider']}")

        try:
            # Process query
            response = await query_service.process_query(test['query'])

            if response.intent:
                actual_provider = response.intent.apiProvider
                print(f"Actual provider: {actual_provider}")

                # Check if routing is correct
                if actual_provider.upper() == test['expected_provider'].upper():
                    print("✅ PASS - Provider routing correct")
                    passed += 1
                else:
                    print(f"❌ FAIL - Expected {test['expected_provider']}, got {actual_provider}")
                    failed += 1
            else:
                print("❌ FAIL - No intent returned")
                failed += 1

        except Exception as e:
            print(f"❌ ERROR - {str(e)}")
            failed += 1

        # Small delay between requests
        await asyncio.sleep(1)

    # Summary
    print("\n" + "="*80)
    print(f"INTEGRATION TEST RESULTS")
    print("="*80)
    print(f"Passed: {passed}/{len(test_cases)}")
    print(f"Failed: {failed}/{len(test_cases)}")

    if failed == 0:
        print("\n✅ ALL INTEGRATION TESTS PASSED")
        print("ProviderRouter is working correctly in the query pipeline!")
        return 0
    else:
        print(f"\n❌ {failed} TEST(S) FAILED")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_integration())
    sys.exit(exit_code)
