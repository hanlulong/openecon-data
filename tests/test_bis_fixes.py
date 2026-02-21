#!/usr/bin/env python3
"""Test BIS provider fixes for the three failing queries."""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path
parent_path = Path(__file__).parent.parent
sys.path.insert(0, str(parent_path))

from backend.providers.bis import BISProvider
from backend.services.metadata_search import MetadataSearchService


async def test_credit_to_gdp():
    """Test: Show me US credit to GDP ratio"""
    print("\n" + "="*80)
    print("TEST 1: US Credit to GDP Ratio")
    print("="*80)

    provider = BISProvider()
    try:
        results = await provider.fetch_indicator(
            indicator="CREDIT_TO_GDP",
            country="US",
            start_year=2020,
            end_year=2023
        )

        if results:
            print(f"✓ SUCCESS: Got {len(results)} result(s)")
            for result in results:
                print(f"  - {result.metadata.indicator} for {result.metadata.country}")
                print(f"    Unit: {result.metadata.unit}")
                print(f"    Frequency: {result.metadata.frequency}")
                print(f"    Data points: {len(result.data)}")
                if result.data:
                    print(f"    Sample: {result.data[0].date} = {result.data[0].value}")
                    print(f"    Latest: {result.data[-1].date} = {result.data[-1].value}")
        else:
            print("✗ FAILED: No results returned")

    except Exception as e:
        print(f"✗ FAILED: {type(e).__name__}: {e}")


async def test_policy_rate():
    """Test: What is the policy rate for Germany?"""
    print("\n" + "="*80)
    print("TEST 2: Germany Policy Rate")
    print("="*80)

    provider = BISProvider()
    try:
        results = await provider.fetch_indicator(
            indicator="POLICY_RATE",
            country="Germany",
            start_year=2020,
            end_year=2023
        )

        if results:
            print(f"✓ SUCCESS: Got {len(results)} result(s)")
            for result in results:
                print(f"  - {result.metadata.indicator} for {result.metadata.country}")
                print(f"    Unit: {result.metadata.unit}")
                print(f"    Frequency: {result.metadata.frequency}")
                print(f"    Data points: {len(result.data)}")
                if result.data:
                    print(f"    Sample: {result.data[0].date} = {result.data[0].value}")
                    print(f"    Latest: {result.data[-1].date} = {result.data[-1].value}")
        else:
            print("✗ FAILED: No results returned")

    except Exception as e:
        print(f"✗ FAILED: {type(e).__name__}: {e}")


async def test_property_prices():
    """Test: BIS property prices UK"""
    print("\n" + "="*80)
    print("TEST 3: UK Property Prices")
    print("="*80)

    provider = BISProvider()
    try:
        results = await provider.fetch_indicator(
            indicator="PROPERTY_PRICES",
            country="UK",
            start_year=2020,
            end_year=2023
        )

        if results:
            print(f"✓ SUCCESS: Got {len(results)} result(s)")
            for result in results:
                print(f"  - {result.metadata.indicator} for {result.metadata.country}")
                print(f"    Unit: {result.metadata.unit}")
                print(f"    Frequency: {result.metadata.frequency}")
                print(f"    Data points: {len(result.data)}")
                if result.data:
                    print(f"    Sample: {result.data[0].date} = {result.data[0].value}")
                    print(f"    Latest: {result.data[-1].date} = {result.data[-1].value}")
        else:
            print("✗ FAILED: No results returned")

    except Exception as e:
        print(f"✗ FAILED: {type(e).__name__}: {e}")


async def main():
    """Run all tests."""
    print("\nTesting BIS Provider Fixes")
    print("="*80)

    await test_credit_to_gdp()
    await test_policy_rate()
    await test_property_prices()

    print("\n" + "="*80)
    print("Tests Complete")
    print("="*80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
