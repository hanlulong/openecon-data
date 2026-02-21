#!/usr/bin/env python3
"""
Test SDMX Data Retrieval - Corrected Version

Uses the correct sdmx1 API patterns based on official documentation.
Tests data retrieval from major SDMX providers.
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Add backend to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir.parent))

import sdmx


def test_list_sources():
    """Test listing available SDMX sources."""
    print("📋 Available SDMX Sources:")
    print("="*70)
    sources = sdmx.list_sources()
    for source in sorted(sources):
        print(f"  - {source}")
    print()
    return sources


def test_provider_data(provider: str, dataflow: str, key=None, params=None, description=""):
    """Test fetching data from a specific provider."""
    print(f"🔍 Test: {description}")
    print(f"   Provider: {provider}")
    print(f"   Dataflow: {dataflow}")
    if key:
        print(f"   Key: {key}")
    if params:
        print(f"   Params: {params}")

    try:
        # Create client
        client = sdmx.Client(provider)

        # Fetch data using correct API: client.get()
        kwargs = {'resource_type': 'data', 'resource_id': dataflow}
        if key:
            kwargs['key'] = key
        if params:
            kwargs['params'] = params

        msg = client.get(**kwargs)

        # Convert to pandas
        df = sdmx.to_pandas(msg)

        if df is not None:
            # Handle different DataFrame structures
            if isinstance(df, dict):
                # Multiple series returned as dict
                total_points = sum(len(series) for series in df.values())
                first_series = list(df.values())[0]
                sample = first_series.iloc[0] if len(first_series) > 0 else None
                print(f"   ✅ Success! {len(df)} series, {total_points} total points")
                print(f"   Sample value: {sample}")
            elif hasattr(df, '__len__'):
                # Single series or DataFrame
                print(f"   ✅ Success! {len(df)} data points")
                if len(df) > 0:
                    print(f"   Sample value: {df.iloc[0]}")
                print(f"   Date range: {df.index[0]} to {df.index[-1]}")
            else:
                print(f"   ✅ Success! Data retrieved")

            print()
            return ("PASS", description, f"Retrieved {len(df) if hasattr(df, '__len__') else '?'} points")
        else:
            print(f"   ⚠️ Empty dataset")
            print()
            return ("WARN", description, "Empty dataset")

    except Exception as e:
        error_msg = str(e)[:150]
        print(f"   ❌ Error: {error_msg}")
        print()
        return ("ERROR", description, error_msg)


def main():
    print("="*70)
    print("🧪 Testing SDMX Data Retrieval (Corrected API)")
    print("="*70)
    print()

    # List available sources
    sources = test_list_sources()

    # Test cases using correct API patterns
    test_cases = [
        # ECB - European Central Bank (known to work)
        {
            'provider': 'ECB',
            'dataflow': 'EXR',  # Exchange rates
            'key': {'CURRENCY': 'USD', 'FREQ': 'M'},  # Monthly USD exchange rate
            'params': {'startPeriod': '2020', 'endPeriod': '2024'},
            'description': 'ECB: USD Exchange Rate (Monthly)'
        },

        # BIS - Bank for International Settlements (known to work)
        {
            'provider': 'BIS',
            'dataflow': 'WS_CBPOL',  # Central Bank Policy Rates
            'key': 'M.US',  # Monthly, United States
            'params': {'startPeriod': '2020', 'endPeriod': '2024'},
            'description': 'BIS: US Policy Rate (Monthly)'
        },

        # OECD - Try with simpler key structure
        {
            'provider': 'OECD',
            'dataflow': 'DP_LIVE',  # Data Portal - Live dataset
            'key': {'LOCATION': 'USA', 'INDICATOR': 'UNEMP', 'FREQUENCY': 'A'},  # US unemployment, annual
            'params': {'startPeriod': '2015', 'endPeriod': '2024'},
            'description': 'OECD: US Unemployment Rate (Annual)'
        },

        # IMF - Try different approach
        {
            'provider': 'IMF',
            'dataflow': 'IFS',  # International Financial Statistics
            'key': 'A.US.PCP_IX',  # Annual, US, Consumer Price Index
            'params': {'startPeriod': '2015', 'endPeriod': '2024'},
            'description': 'IMF: US Consumer Price Index (Annual)'
        },

        # Test without keys to get all data
        {
            'provider': 'ECB',
            'dataflow': 'EXR',
            'key': '.USD....',  # All USD exchange rates
            'params': {'startPeriod': '2023', 'endPeriod': '2024'},
            'description': 'ECB: All USD Exchange Rates (2023-2024)'
        },

        # ESTAT - Eurostat
        {
            'provider': 'ESTAT',
            'dataflow': 'nama_10_gdp',  # GDP data
            'key': {'geo': 'DE', 'unit': 'CP_MEUR'},  # Germany, millions EUR
            'params': {'startPeriod': '2015', 'endPeriod': '2024'},
            'description': 'Eurostat: Germany GDP (Annual)'
        },
    ]

    results = []
    for test_case in test_cases:
        provider = test_case['provider']

        # Skip if provider not available
        if provider not in sources:
            print(f"⚠️  Skipping {provider} - not in available sources")
            print()
            results.append(("SKIP", test_case['description'], "Provider not available"))
            continue

        result = test_provider_data(
            provider=test_case['provider'],
            dataflow=test_case['dataflow'],
            key=test_case.get('key'),
            params=test_case.get('params'),
            description=test_case['description']
        )
        results.append(result)

    # Summary
    print("="*70)
    print("📊 Test Summary")
    print("="*70)

    passed = sum(1 for r in results if r[0] == "PASS")
    warnings = sum(1 for r in results if r[0] == "WARN")
    errors = sum(1 for r in results if r[0] == "ERROR")
    skipped = sum(1 for r in results if r[0] == "SKIP")

    for status, name, detail in results:
        icon = {
            "PASS": "✅",
            "WARN": "⚠️",
            "ERROR": "❌",
            "SKIP": "⏭️"
        }[status]
        print(f"{icon} {name}")
        if detail:
            print(f"   {detail}")

    print()
    print(f"Results: {passed} passed, {warnings} warnings, {errors} errors, {skipped} skipped")
    print()

    if passed >= 3:
        print("✅ SDMX data retrieval is working for multiple providers!")
        print()
        print("Key findings:")
        print(f"- Successfully fetched data from {passed} provider(s)")
        print("- Using correct sdmx1 API: client.get() with resource_type='data'")
        print("- Dict-based keys work well for dimensional filtering")
        print("- Ready to implement SDMX data fetching in production")
    else:
        print("⚠️ Some SDMX providers need further investigation")
        print()
        print("Notes:")
        print("- Each provider has unique dataflow structures")
        print("- Some providers may require registration or API keys")
        print("- Key formats vary significantly between providers")

    print("="*70)


if __name__ == '__main__':
    main()
