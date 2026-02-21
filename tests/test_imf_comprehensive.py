#!/usr/bin/env python3
"""
Comprehensive test of IMF provider improvements.
Tests country mappings, indicator mappings, and error messages.
"""
import asyncio
import sys
import os

# Add backend to path
backend_path = os.path.join(os.path.dirname(__file__), 'backend')
sys.path.insert(0, backend_path)

from backend.providers.imf import IMFProvider


async def test_all_improvements():
    """Test all IMF provider improvements"""
    imf = IMFProvider(metadata_search_service=None)

    print("=" * 80)
    print("IMF Provider Comprehensive Test Suite")
    print("=" * 80)

    # Test 1: Previously failing queries
    print("\n[Test 1] Previously Failing Queries")
    print("-" * 80)

    test_cases = [
        ("Spain", "GDP", "ESP", "NGDP_RPCH"),
        ("Portugal", "debt to GDP", "PRT", "GGXWDG_NGDP"),
        ("Greece", "debt", "GRC", "GGXWDG_NGDP"),  # Was failing: wrong country code + missing indicator
        ("Italy", "inflation", "ITA", "PCPIPCH"),
    ]

    failures = []
    for country, indicator, expected_code, expected_indicator in test_cases:
        try:
            result = await imf.fetch_indicator(
                indicator=indicator,
                country=country,
                start_year=2022,
                end_year=2024
            )
            print(f"✅ {country} {indicator}: {len(result.data)} data points")
        except Exception as e:
            failures.append((country, indicator, str(e)))
            print(f"❌ {country} {indicator}: {e}")

    # Test 2: New country mappings
    print("\n[Test 2] New Country Mappings")
    print("-" * 80)

    new_countries = [
        ("Greece", "GRC"),
        ("Netherlands", "NLD"),
        ("Belgium", "BEL"),
        ("Czech Republic", "CZE"),
        ("Czechia", "CZE"),
        ("South Korea", "KOR"),
        ("Korea", "KOR"),
    ]

    for country, expected in new_countries:
        mapped = imf._country_code(country)
        status = "✅" if mapped == expected else "❌"
        print(f"{status} {country:20s} → {mapped:4s} (expected: {expected})")

    # Test 3: New indicator mappings
    print("\n[Test 3] New Indicator Mappings")
    print("-" * 80)

    new_indicators = [
        ("debt", "GGXWDG_NGDP"),
        ("debt ratio", "GGXWDG_NGDP"),
        ("national debt", "GGXWDG_NGDP"),
        ("sovereign debt", "GGXWDG_NGDP"),
    ]

    for indicator, expected in new_indicators:
        mapped = imf._indicator_code(indicator)
        status = "✅" if mapped == expected else "❌"
        print(f"{status} {indicator:20s} → {mapped or 'None':15s} (expected: {expected})")

    # Test 4: Multi-country batch query
    print("\n[Test 4] Multi-Country Batch Query")
    print("-" * 80)

    countries = ["Spain", "Portugal", "Greece", "Italy"]
    try:
        results = await imf.fetch_batch_indicator(
            indicator="GDP",
            countries=countries,
            start_year=2023,
            end_year=2024
        )
        print(f"✅ Batch query successful: {len(results)} countries")
        for result in results:
            print(f"   - {result.metadata.country}: {len(result.data)} data points")
    except Exception as e:
        print(f"❌ Batch query failed: {e}")
        failures.append(("Batch", "GDP", str(e)))

    # Test 5: Error message quality (intentionally failing query)
    print("\n[Test 5] Error Message Quality")
    print("-" * 80)

    try:
        # Try to fetch data for a non-existent country code
        await imf.fetch_indicator(
            indicator="GDP",
            country="FAKECOUNTRY",
            start_year=2023,
            end_year=2024
        )
        print("❌ Should have failed with clear error message")
    except Exception as e:
        error_msg = str(e)
        # Check if error message is informative
        has_available_countries = "available" in error_msg.lower()
        has_indicator_info = "GDP" in error_msg or "NGDP_RPCH" in error_msg

        if has_available_countries and has_indicator_info:
            print(f"✅ Error message is informative")
            print(f"   Preview: {error_msg[:200]}...")
        else:
            print(f"⚠️  Error message could be more informative")
            print(f"   Message: {error_msg}")

    # Summary
    print("\n" + "=" * 80)
    print("Test Summary")
    print("=" * 80)

    if failures:
        print(f"\n❌ {len(failures)} tests failed:\n")
        for country, indicator, error in failures:
            print(f"  - {country} {indicator}: {error[:100]}")
        return False
    else:
        print("\n✅ All tests passed!")
        return True


async def test_edge_cases():
    """Test edge cases and error handling"""
    imf = IMFProvider(metadata_search_service=None)

    print("\n" + "=" * 80)
    print("Edge Case Tests")
    print("=" * 80)

    # Test case-insensitive country matching
    print("\n[Edge Case 1] Case-Insensitive Country Matching")
    print("-" * 80)

    for country in ["greece", "GREECE", "Greece", "GrEeCe"]:
        code = imf._country_code(country)
        status = "✅" if code == "GRC" else "❌"
        print(f"{status} '{country}' → {code}")

    # Test case-insensitive indicator matching
    print("\n[Edge Case 2] Case-Insensitive Indicator Matching")
    print("-" * 80)

    for indicator in ["debt", "DEBT", "Debt", "DeBt"]:
        code = imf._indicator_code(indicator)
        status = "✅" if code == "GGXWDG_NGDP" else "❌"
        print(f"{status} '{indicator}' → {code}")

    # Test abbreviations
    print("\n[Edge Case 3] Country Abbreviations")
    print("-" * 80)

    abbreviations = [
        ("GR", "GRC"),
        ("ES", "ESP"),
        ("PT", "PRT"),
        ("IT", "ITA"),
        ("DE", "DEU"),
        ("FR", "FRA"),
    ]

    for abbrev, expected in abbreviations:
        code = imf._country_code(abbrev)
        status = "✅" if code == expected else "❌"
        print(f"{status} {abbrev} → {code} (expected: {expected})")


if __name__ == "__main__":
    success = asyncio.run(test_all_improvements())
    asyncio.run(test_edge_cases())

    sys.exit(0 if success else 1)
