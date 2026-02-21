#!/usr/bin/env python3
"""
Comprehensive SDMX Data Retrieval Test

Based on official sdmx1 documentation best practices:
- Explores dataflows first
- Passes DSD to minimize message size
- Uses proper key formats (dict and string)
- Tests data retrieval for each major provider
"""

import sys
from pathlib import Path
from typing import Optional, Dict, Any

# Add backend to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir.parent))

import sdmx
import pandas as pd


class SDMXTestCase:
    def __init__(
        self,
        name: str,
        provider: str,
        dataflow: str,
        key: Optional[Dict[str, Any]] = None,
        key_string: Optional[str] = None,
        expected_points_min: int = 5,
    ):
        self.name = name
        self.provider = provider
        self.dataflow = dataflow
        self.key = key
        self.key_string = key_string
        self.expected_points_min = expected_points_min


def test_sdmx_data_retrieval(test: SDMXTestCase) -> tuple[str, str, str]:
    """
    Test SDMX data retrieval using best practices from documentation.

    Returns: (status, name, detail)
    """
    print(f"\n🔍 Test: {test.name}")
    print(f"   Provider: {test.provider}")
    print(f"   Dataflow: {test.dataflow}")
    if test.key:
        print(f"   Key (dict): {test.key}")
    if test.key_string:
        print(f"   Key (string): {test.key_string}")

    try:
        # Step 1: Create client
        client = sdmx.Client(test.provider)

        # Step 2: Get dataflow metadata (optional but recommended)
        try:
            flow_msg = client.dataflow(test.dataflow)
            dsd = None
            # Try to extract DSD for structure-specific data
            if hasattr(flow_msg, 'structure') and flow_msg.structure:
                dsd_dict = dict(flow_msg.structure)
                if dsd_dict:
                    dsd = list(dsd_dict.values())[0]
                    print(f"   ✓ Using DSD for structure-specific data")
        except Exception as e:
            print(f"   ⚠️ Could not get DSD: {str(e)[:80]}")
            dsd = None

        # Step 3: Query data with proper parameters
        params = {'startPeriod': '2020', 'endPeriod': '2024'}

        # Use key_string if provided, otherwise use dict key
        key_param = test.key_string if test.key_string else test.key

        # Build query kwargs
        kwargs = {
            'resource_type': 'data',
            'resource_id': test.dataflow,
            'params': params
        }
        if key_param:
            kwargs['key'] = key_param
        if dsd:
            kwargs['dsd'] = dsd

        msg = client.get(**kwargs)

        # Step 4: Convert to pandas
        df = sdmx.to_pandas(msg)

        # Step 5: Analyze results
        if df is not None:
            if isinstance(df, dict):
                # Multiple series returned as dict
                total_points = sum(len(series) for series in df.values())
                first_series = list(df.values())[0]
                sample = first_series.iloc[0] if len(first_series) > 0 else None

                print(f"   ✅ Success! {len(df)} series, {total_points} total points")
                print(f"   Sample value: {sample}")

                if total_points >= test.expected_points_min:
                    return ("PASS", test.name, f"{total_points} points from {len(df)} series")
                else:
                    return ("WARN", test.name, f"Only {total_points} points (expected {test.expected_points_min}+)")

            elif isinstance(df, pd.DataFrame):
                # Single DataFrame
                points = len(df)
                print(f"   ✅ Success! {points} data points")
                if points > 0:
                    print(f"   Sample: {df.iloc[0].values[0] if len(df.columns) > 0 else 'N/A'}")
                    if hasattr(df.index, 'names'):
                        print(f"   Index: {df.index[0] if len(df) > 0 else 'empty'}")

                if points >= test.expected_points_min:
                    return ("PASS", test.name, f"{points} points")
                else:
                    return ("WARN", test.name, f"Only {points} points (expected {test.expected_points_min}+)")

            elif isinstance(df, pd.Series):
                # Single series
                points = len(df)
                print(f"   ✅ Success! {points} data points (Series)")
                if points > 0:
                    print(f"   Sample: {df.iloc[0]}")

                if points >= test.expected_points_min:
                    return ("PASS", test.name, f"{points} points")
                else:
                    return ("WARN", test.name, f"Only {points} points (expected {test.expected_points_min}+)")
            else:
                print(f"   ✅ Data retrieved (type: {type(df)})")
                return ("PASS", test.name, "Data retrieved")
        else:
            print(f"   ❌ Empty dataset")
            return ("FAIL", test.name, "Empty dataset")

    except Exception as e:
        error_msg = str(e)[:150]
        print(f"   ❌ Error: {error_msg}")
        return ("ERROR", test.name, error_msg)
    finally:
        print()


def main():
    print("="*70)
    print("🧪 Comprehensive SDMX Data Retrieval Test")
    print("="*70)
    print("\nBased on official sdmx1 documentation best practices")
    print()

    # List available sources
    print("📋 Available SDMX Sources:")
    print("="*70)
    sources = sdmx.list_sources()
    print(f"Total providers: {len(sources)}")
    print(f"Sample: {', '.join(sorted(sources)[:10])}")
    print()

    # Define comprehensive test cases
    test_cases = [
        # ECB - European Central Bank (known to work well)
        SDMXTestCase(
            "ECB: EUR/USD Exchange Rate (Monthly)",
            "ECB",
            "EXR",
            key={'CURRENCY': 'USD', 'FREQ': 'M'},
            expected_points_min=48  # 4 years monthly
        ),

        SDMXTestCase(
            "ECB: EUR/JPY Exchange Rate (Daily)",
            "ECB",
            "EXR",
            key={'CURRENCY': 'JPY', 'FREQ': 'D'},
            expected_points_min=800  # 4 years daily (approx)
        ),

        # BIS - Bank for International Settlements
        SDMXTestCase(
            "BIS: US Central Bank Policy Rate",
            "BIS",
            "WS_CBPOL",
            key_string="M.US",  # Monthly, United States
            expected_points_min=48
        ),

        SDMXTestCase(
            "BIS: Canada Central Bank Policy Rate",
            "BIS",
            "WS_CBPOL",
            key_string="M.CA",  # Monthly, Canada
            expected_points_min=48
        ),

        # OECD - Using documented approach
        SDMXTestCase(
            "OECD: Quarterly National Accounts",
            "OECD",
            "QNA",
            key_string="AUS.B1_GE.CUR.Q",  # Australia GDP, current prices, quarterly
            expected_points_min=16  # 4 years quarterly
        ),

        SDMXTestCase(
            "OECD: Monthly Economic Indicators",
            "OECD",
            "MEI",
            key_string="USA.LF.M",  # USA Labor Force, Monthly
            expected_points_min=48
        ),

        # IMF - Try different endpoint
        SDMXTestCase(
            "IMF: International Financial Statistics",
            "IMF",
            "IFS",
            key_string="M.US.PMP_IX",  # Monthly, US, Import Price Index
            expected_points_min=48
        ),

        # Try IMF_DATA endpoint
        SDMXTestCase(
            "IMF_DATA: Direction of Trade Statistics",
            "IMF_DATA",
            "DOT",
            key_string="A.US.TMG_CIF_USD",  # Annual, US, Total Merchandise Imports
            expected_points_min=4
        ),

        # Eurostat - Using ESTAT (not ESTAT3 for better compatibility)
        SDMXTestCase(
            "Eurostat: Harmonized Index of Consumer Prices",
            "ESTAT",
            "PRC_HICP_MIDX",
            key={'geo': 'DE', 'coicop': 'CP00'},  # Germany, All items
            expected_points_min=48
        ),

        SDMXTestCase(
            "Eurostat: Population Data",
            "ESTAT",
            "DEMO_PJAN",
            key={'geo': 'FR', 'sex': 'T', 'age': 'TOTAL'},  # France, Total
            expected_points_min=4
        ),

        # World Bank WDI
        SDMXTestCase(
            "World Bank: World Development Indicators",
            "WB_WDI",
            "WDI",
            key={'ref_area': 'USA', 'indicator': 'NY.GDP.MKTP.CD'},  # US GDP current USD
            expected_points_min=4
        ),
    ]

    # Run all tests
    results = []
    for test in test_cases:
        # Skip if provider not available
        if test.provider not in sources:
            print(f"⚠️  Skipping {test.provider} - not in available sources\n")
            results.append(("SKIP", test.name, "Provider not available"))
            continue

        result = test_sdmx_data_retrieval(test)
        results.append(result)

    # Summary
    print("="*70)
    print("📊 Test Summary")
    print("="*70)

    passed = sum(1 for r in results if r[0] == "PASS")
    warnings = sum(1 for r in results if r[0] == "WARN")
    failed = sum(1 for r in results if r[0] == "FAIL")
    errors = sum(1 for r in results if r[0] == "ERROR")
    skipped = sum(1 for r in results if r[0] == "SKIP")

    for status, name, detail in results:
        icon = {
            "PASS": "✅",
            "WARN": "⚠️",
            "FAIL": "❌",
            "ERROR": "💥",
            "SKIP": "⏭️"
        }[status]
        print(f"{icon} {name}")
        if detail:
            print(f"   {detail}")

    print()
    print(f"Results: {passed} passed, {warnings} warnings, {failed} failed, {errors} errors, {skipped} skipped")
    print()

    # Final assessment
    total_tests = passed + warnings + failed + errors
    success_rate = (passed / total_tests * 100) if total_tests > 0 else 0

    if success_rate >= 70:
        print(f"✅ EXCELLENT: {success_rate:.1f}% success rate!")
        print()
        print("Key findings:")
        print(f"- Successfully fetched data from {passed} dataflow(s)")
        print("- SDMX data retrieval is working for multiple providers")
        print("- Using best practices: DSD optimization, proper key formats")
        print("- Ready for production integration")
    elif success_rate >= 50:
        print(f"✓ GOOD: {success_rate:.1f}% success rate")
        print()
        print("SDMX data retrieval is partially working.")
        print("Some providers may need additional configuration.")
    else:
        print(f"⚠️ LIMITED: {success_rate:.1f}% success rate")
        print()
        print("Notes:")
        print("- Many providers require specific dataflow IDs and key formats")
        print("- Some providers only support metadata queries, not data queries")
        print("- Consider using provider-specific APIs for data retrieval")
        print("- SDMX is most reliable for metadata discovery")

    print("="*70)

    # Return exit code based on results
    return 0 if passed >= 5 else 1


if __name__ == '__main__':
    sys.exit(main())
