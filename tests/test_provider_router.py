"""
Unit Tests for ProviderRouter

Tests the deterministic routing logic without calling the LLM.
This validates that our code-based routing matches expected behavior.
"""
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.services.provider_router import ProviderRouter
from backend.models import ParsedIntent


def test_explicit_provider_detection():
    """Test detection of explicit provider mentions"""
    test_cases = [
        # OECD
        ("Show me Italy GDP from OECD", "OECD"),
        ("Get data from OECD for Germany", "OECD"),
        ("Using OECD, show me unemployment", "OECD"),
        ("OECD average GDP growth", "OECD"),

        # IMF
        ("Show me inflation from IMF", "IMF"),
        ("According to the IMF", "IMF"),
        ("Using IMF data", "IMF"),

        # Eurostat
        ("Get France GDP from Eurostat", "Eurostat"),
        ("Using Eurostat", "Eurostat"),

        # FRED
        ("Show me data from FRED", "FRED"),
        ("Using Federal Reserve data", "FRED"),

        # StatsCan
        ("From Statistics Canada", "StatsCan"),
        ("Using StatsCan", "StatsCan"),

        # Comtrade
        ("Get Russia imports from Comtrade", "Comtrade"),
        ("Using UN Comtrade", "Comtrade"),

        # No explicit mention
        ("Show me US GDP", None),
        ("Canada unemployment", None),
        ("China imports", None),
    ]

    print("\n" + "="*80)
    print("TEST: Explicit Provider Detection")
    print("="*80)

    passed = 0
    failed = 0

    for query, expected in test_cases:
        detected = ProviderRouter.detect_explicit_provider(query)
        matches = detected == expected

        status = "✅" if matches else "❌"
        print(f"{status} Query: '{query[:60]}'")
        print(f"   Expected: {expected}, Got: {detected}")

        if matches:
            passed += 1
        else:
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_us_only_indicators():
    """Test detection of US-only indicators"""
    test_cases = [
        (["Case-Shiller"], True),
        (["federal funds rate"], True),
        (["PCE"], True),
        (["nonfarm payrolls"], True),
        (["S&P 500"], True),
        (["prime lending rate"], True),
        (["GDP"], False),
        (["unemployment"], False),
        (["inflation"], False),
    ]

    print("\n" + "="*80)
    print("TEST: US-Only Indicator Detection")
    print("="*80)

    passed = 0
    failed = 0

    for indicators, expected in test_cases:
        result = ProviderRouter.is_us_only_indicator(indicators)
        matches = result == expected

        status = "✅" if matches else "❌"
        print(f"{status} Indicators: {indicators}, US-only: {expected}, Got: {result}")

        if matches:
            passed += 1
        else:
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_canadian_query_detection():
    """Test detection of Canadian queries"""
    test_cases = [
        ("Canada GDP", {}, True),
        ("Ontario unemployment", {}, True),
        ("Toronto population", {}, True),
        ("Canadian inflation", {}, True),
        ("Show me BC housing starts", {}, True),
        ("US GDP", {}, False),
        ("China imports", {}, False),
        ("Get data", {"country": "Canada"}, True),
        ("Get data", {"country": "US"}, False),
    ]

    print("\n" + "="*80)
    print("TEST: Canadian Query Detection")
    print("="*80)

    passed = 0
    failed = 0

    for query, params, expected in test_cases:
        result = ProviderRouter.is_canadian_query(query, params)
        matches = result == expected

        status = "✅" if matches else "❌"
        print(f"{status} Query: '{query}', Canadian: {expected}, Got: {result}")

        if matches:
            passed += 1
        else:
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_provider_routing():
    """Test complete provider routing logic"""
    test_cases = [
        # Explicit provider overrides (HIGHEST PRIORITY)
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["GDP"],
                parameters={"country": "Italy"},
                clarificationNeeded=False
            ),
            "Get Italy GDP from OECD",
            "OECD",  # MUST override WorldBank because user said "from OECD"
            "Explicit provider override"
        ),

        # US-only indicators
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["Case-Shiller"],
                parameters={},
                clarificationNeeded=False
            ),
            "Show me Case-Shiller index",
            "FRED",  # Case-Shiller is US-only
            "US-only indicator routing"
        ),

        # Canadian queries
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["GDP"],
                parameters={"country": "Canada"},
                clarificationNeeded=False
            ),
            "Canada GDP",
            "StatsCan",  # Canadian data → StatsCan
            "Canadian query routing"
        ),

        # Non-OECD countries
        (
            ParsedIntent(
                apiProvider="OECD",
                indicators=["GDP"],
                parameters={"country": "China"},
                clarificationNeeded=False
            ),
            "China GDP",
            "WorldBank",  # China is not OECD member
            "Non-OECD country routing"
        ),

        # Fiscal/debt indicators
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["government debt"],
                parameters={"country": "US"},
                clarificationNeeded=False
            ),
            "US government debt",
            "IMF",  # Debt data → IMF
            "Fiscal indicator routing"
        ),

        # Housing prices (non-US)
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["house prices"],
                parameters={"country": "Germany"},
                clarificationNeeded=False
            ),
            "Germany house prices",
            "BIS",  # Non-US housing → BIS
            "Non-US housing routing"
        ),

        # Housing prices (US)
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["house prices"],
                parameters={"country": "US"},
                clarificationNeeded=False
            ),
            "US house prices",
            "FRED",  # US housing → FRED
            "US housing routing"
        ),

        # Trade data
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["imports"],
                parameters={"country": "US"},
                clarificationNeeded=False
            ),
            "US imports",
            "Comtrade",  # Trade → Comtrade
            "Trade data routing"
        ),

        # Cryptocurrency
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["Bitcoin"],
                parameters={},
                clarificationNeeded=False
            ),
            "Bitcoin price",
            "CoinGecko",  # Crypto → CoinGecko
            "Cryptocurrency routing"
        ),

        # Exchange rates
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["exchange rate"],
                parameters={},
                clarificationNeeded=False
            ),
            "USD to EUR exchange rate",
            "ExchangeRate",  # Forex → ExchangeRate
            "Exchange rate routing"
        ),

        # Default US routing
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["GDP"],
                parameters={},
                clarificationNeeded=False
            ),
            "Show me GDP",  # No country specified
            "FRED",  # Default to US for common indicators
            "Default US routing"
        ),
    ]

    print("\n" + "="*80)
    print("TEST: Provider Routing Logic")
    print("="*80)

    passed = 0
    failed = 0

    for intent, query, expected_provider, description in test_cases:
        routed = ProviderRouter.route_provider(intent, query)
        matches = routed.upper() == expected_provider.upper()

        status = "✅" if matches else "❌"
        print(f"\n{status} {description}")
        print(f"   Query: '{query}'")
        print(f"   LLM suggested: {intent.apiProvider}")
        print(f"   Routed to: {routed}")
        print(f"   Expected: {expected_provider}")

        if matches:
            passed += 1
        else:
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def main():
    """Run all tests"""
    print("\n🚀 Starting ProviderRouter Unit Tests\n")

    all_passed = True

    # Run all test suites
    all_passed &= test_explicit_provider_detection()
    all_passed &= test_us_only_indicators()
    all_passed &= test_canadian_query_detection()
    all_passed &= test_provider_routing()

    # Summary
    print("\n" + "="*80)
    if all_passed:
        print("✅ ALL TESTS PASSED - ProviderRouter is working correctly!")
        print("="*80)
        return 0
    else:
        print("❌ SOME TESTS FAILED - Review failed cases above")
        print("="*80)
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
