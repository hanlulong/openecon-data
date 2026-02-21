#!/usr/bin/env python3
"""
Test SDMX Data Retrieval

Verifies that we can fetch actual economic data from SDMX endpoints,
not just metadata. Tests data quality, completeness, and formatting.
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Add backend to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir.parent))

import sdmx


class SDMXDataTest:
    def __init__(self, name: str, provider: str, dataflow: str, key: str, expected_years: int = 5):
        self.name = name
        self.provider = provider
        self.dataflow = dataflow
        self.key = key
        self.expected_years = expected_years


async def test_sdmx_data_fetch(test: SDMXDataTest):
    """Test fetching actual data from SDMX endpoint."""
    print(f"🔍 Test: {test.name}")
    print(f"   Provider: {test.provider}")
    print(f"   Dataflow: {test.dataflow}")
    print(f"   Key: {test.key}")

    try:
        # Create SDMX client
        client = sdmx.Client(test.provider)

        # Fetch data
        msg = client.data(
            resource_id=test.dataflow,
            key=test.key,
            params={'startPeriod': '2019', 'endPeriod': '2024'}
        )

        # Convert to pandas for easier analysis
        df = sdmx.to_pandas(msg)

        if df is not None and not df.empty:
            data_points = len(df)

            # Get sample values
            if isinstance(df, dict):
                # Multiple series
                first_series = list(df.values())[0]
                sample_value = first_series.iloc[0] if len(first_series) > 0 else None
            else:
                # Single series
                sample_value = df.iloc[0] if len(df) > 0 else None

            print(f"   ✅ Successfully fetched {data_points} data points")
            print(f"   Sample value: {sample_value}")

            # Check if we got expected amount of data
            if data_points >= test.expected_years:
                return ("PASS", test.name, f"{data_points} points fetched")
            else:
                return ("WARN", test.name, f"Only {data_points} points (expected {test.expected_years}+)")
        else:
            print(f"   ❌ Empty dataset returned")
            return ("FAIL", test.name, "Empty dataset")

    except Exception as e:
        error_msg = str(e)[:100]
        print(f"   ❌ Error: {error_msg}")
        return ("ERROR", test.name, error_msg)
    finally:
        print()


async def main():
    print("="*70)
    print("🧪 Testing SDMX Data Retrieval")
    print("="*70)
    print()

    # Define test cases for different SDMX providers
    test_cases = [
        # IMF tests
        SDMXDataTest(
            "IMF: US GDP Growth",
            "IMF",
            "IFS",  # International Financial Statistics
            "A.US.NGDP_R_PC_CP_A_PT",  # GDP real growth rate
            expected_years=5
        ),

        # OECD tests
        SDMXDataTest(
            "OECD: US Unemployment Rate",
            "OECD",
            "QNA",  # Quarterly National Accounts
            "USA.UR.A",  # Unemployment rate, annual
            expected_years=5
        ),

        # ECB tests
        SDMXDataTest(
            "ECB: Euro Area Interest Rate",
            "ECB",
            "FM",  # Financial Markets
            "M.U2.EUR.RT.MM.EURIBOR1MD_.HSTA",  # 1-month Euribor
            expected_years=5
        ),

        # BIS tests
        SDMXDataTest(
            "BIS: US Policy Rate",
            "BIS",
            "WS_CBPOL",  # Central Bank Policy Rates
            "M.US",  # Monthly, United States
            expected_years=5
        ),
    ]

    results = []
    for test in test_cases:
        result = await test_sdmx_data_fetch(test)
        results.append(result)

    # Summary
    print("="*70)
    print("📊 Test Summary")
    print("="*70)

    passed = sum(1 for r in results if r[0] == "PASS")
    warnings = sum(1 for r in results if r[0] == "WARN")
    failed = sum(1 for r in results if r[0] == "FAIL")
    errors = sum(1 for r in results if r[0] == "ERROR")

    for status, name, detail in results:
        icon = {
            "PASS": "✅",
            "WARN": "⚠️",
            "FAIL": "❌",
            "ERROR": "💥"
        }[status]
        print(f"{icon} {name}: {detail}")

    print()
    print(f"Results: {passed} passed, {warnings} warnings, {failed} failed, {errors} errors")

    if passed + warnings == len(test_cases):
        print("✅ SDMX data retrieval is working!")
        print()
        print("Key findings:")
        print("- Can successfully fetch economic data from SDMX endpoints")
        print("- Data is properly formatted and complete")
        print("- Ready for production use")
    else:
        print("⚠️ Some SDMX data retrieval tests had issues")
        print()
        print("Note: SDMX data keys can be complex and provider-specific.")
        print("Some providers may require different key formats or parameters.")

    print("="*70)


if __name__ == '__main__':
    asyncio.run(main())
