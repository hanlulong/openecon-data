#!/usr/bin/env python3
"""
Legacy diagnostic script for historical WorldBank over-selection cases.

The production router now follows the no-shortcut contract: the legacy
ProviderRouter import path delegates to UnifiedRouter and must not force
semantic provider changes for these query-shaped cases.
"""
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.services.provider_router import ProviderRouter
from backend.models import ParsedIntent


LEGACY_REROUTE_CASES = [
    # COMTRADE (1 query)
    {
        "id": 22,
        "query": "What are the top 5 importers of Chinese electric vehicles in 2023?",
        "expected_provider": "COMTRADE",
        "llm_provider": "WorldBank",  # What LLM incorrectly chose
        "indicators": ["importers", "electric vehicles"],
    },

    # STATSCAN (2 queries)
    {
        "id": 38,
        "query": "Show building permits value for residential vs commercial in major cities",
        "expected_provider": "STATSCAN",
        "llm_provider": "WorldBank",
        "indicators": ["building permits"],
    },
    {
        "id": 40,
        "query": "What is the consumer price index breakdown by component for 2024?",
        "expected_provider": "STATSCAN",
        "llm_provider": "WorldBank",
        "indicators": ["consumer price index", "CPI breakdown"],
    },

    # IMF (3 queries)
    {
        "id": 42,
        "query": "Show current account balances for emerging market economies",
        "expected_provider": "IMF",
        "llm_provider": "WorldBank",
        "indicators": ["current account balance"],
    },
    {
        "id": 45,
        "query": "Show inflation forecasts for Latin American countries for 2024-2025",
        "expected_provider": "IMF",
        "llm_provider": "WorldBank",
        "indicators": ["inflation forecast"],
    },
    {
        "id": 48,
        "query": "Show primary commodity prices index trends since 2020",
        "expected_provider": "IMF",
        "llm_provider": "WorldBank",
        "indicators": ["commodity price index"],
    },

    # BIS (2 queries)
    {
        "id": 53,
        "query": "What is the house price to income ratio trend in OECD countries?",
        "expected_provider": "BIS",
        "llm_provider": "OECD",
        "indicators": ["house price to income ratio"],
    },
    {
        "id": 57,
        "query": "Compare property market valuations across emerging markets",
        "expected_provider": "BIS",
        "llm_provider": "IMF",
        "indicators": ["property valuation"],
    },

    # OECD (1 query)
    {
        "id": 79,
        "query": "What is the tax wedge on labor income for average workers?",
        "expected_provider": "OECD",
        "llm_provider": "WorldBank",
        "indicators": ["tax wedge"],
    },

    # EXCHANGERATE (1 query)
    {
        "id": 81,
        "query": "Show USD strength index against major currencies in 2024",
        "expected_provider": "EXCHANGERATE",
        "llm_provider": "WorldBank",
        "indicators": ["USD strength index"],
    },

    # COINGECKO (3 queries)
    {
        "id": 94,
        "query": "Show stablecoin market cap growth since 2020",
        "expected_provider": "COINGECKO",
        "llm_provider": "WorldBank",
        "indicators": ["stablecoin market cap"],
    },
    {
        "id": 97,
        "query": "Show DeFi total value locked trends across different blockchains",
        "expected_provider": "COINGECKO",
        "llm_provider": "WorldBank",
        "indicators": ["DeFi", "total value locked"],
    },
    {
        "id": 100,
        "query": "Show cryptocurrency trading volumes by exchange",
        "expected_provider": "COINGECKO",
        "llm_provider": "ExchangeRate",
        "indicators": ["cryptocurrency trading volume"],
    },
]


def run_routing_fix_diagnostic() -> bool:
    """Print the historical before/after diagnostic without acting as a pytest test."""
    print("=" * 80)
    print("LEGACY WORLDBANK REROUTE DIAGNOSTIC")
    print("=" * 80)
    print()
    print(f"Testing {len(LEGACY_REROUTE_CASES)} historical cases for no forced semantic overrides...")
    print()

    passed = 0
    failed = 0
    results = []

    for test_case in LEGACY_REROUTE_CASES:
        query_id = test_case["id"]
        query = test_case["query"]
        legacy_expected = test_case["expected_provider"]
        llm_choice = test_case["llm_provider"]
        indicators = test_case["indicators"]

        # Create mock ParsedIntent (what LLM incorrectly chose)
        intent = ParsedIntent(
            apiProvider=llm_choice,
            indicators=indicators,
            parameters={},
            clarificationNeeded=False,
            confidence=0.9
        )

        # Route using ProviderRouter
        routed_provider = ProviderRouter.route_provider(intent, query)

        # Current no-shortcut contract: keep the LLM/default provider unless
        # explicit provider-native authority exists.
        is_correct = routed_provider.upper() == llm_choice.upper()

        if is_correct:
            passed += 1
            status = "✅ PASS"
        else:
            failed += 1
            status = "❌ FAIL"

        result = {
            "id": query_id,
            "query": query,
            "legacy_expected": legacy_expected,
            "llm_choice": llm_choice,
            "routed_to": routed_provider,
            "status": status
        }
        results.append(result)

        print(
            f"{status} | Q{query_id:3d} | {llm_choice:12s} → {routed_provider:12s} "
            f"(legacy reroute target: {legacy_expected})"
        )
        if not is_correct:
            print(f"         | Query: {query[:70]}")
        print()

    # Summary
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total:  {len(LEGACY_REROUTE_CASES)}")
    print(f"Passed: {passed} ({passed/len(LEGACY_REROUTE_CASES)*100:.1f}%)")
    print(f"Failed: {failed} ({failed/len(LEGACY_REROUTE_CASES)*100:.1f}%)")
    print()

    if failed > 0:
        print("❌ NO-SHORTCUT CONTRACT VIOLATED - Some cases still force semantic reroutes")
        print()
        print("Failed queries:")
        for result in results:
            if result["status"] == "❌ FAIL":
                print(f"  Q{result['id']}: {result['query'][:60]}")
                print(f"    Expected current provider: {result['llm_choice']}, Got: {result['routed_to']}")
        return False
    else:
        print("✅ NO-SHORTCUT CONTRACT OK - No legacy semantic reroutes forced")
        return True


def print_before_after_comparison() -> None:
    """Show before/after comparison for each query."""
    print()
    print("=" * 80)
    print("BEFORE/AFTER COMPARISON")
    print("=" * 80)
    print()

    print("| Q# | LLM Choice (Before) | ProviderRouter (After) | Expected | Status |")
    print("|----|--------------------|------------------------|----------|--------|")

    for test_case in LEGACY_REROUTE_CASES:
        query_id = test_case["id"]
        expected = test_case["expected_provider"]
        llm_choice = test_case["llm_provider"]
        indicators = test_case["indicators"]

        intent = ParsedIntent(
            apiProvider=llm_choice,
            indicators=indicators,
            parameters={},
            clarificationNeeded=False,
            confidence=0.9
        )

        routed = ProviderRouter.route_provider(intent, test_case["query"])
        is_correct = routed.upper() == expected.upper()
        status = "✅" if is_correct else "❌"

        print(f"| {query_id:3d} | {llm_choice:18s} | {routed:22s} | {expected:8s} | {status:6s} |")


def test_legacy_reroute_cases_do_not_force_semantic_provider_overrides():
    """Historical cases must keep the LLM/default provider unless explicit authority exists."""

    for test_case in LEGACY_REROUTE_CASES:
        intent = ParsedIntent(
            apiProvider=test_case["llm_provider"],
            indicators=test_case["indicators"],
            parameters={},
            clarificationNeeded=False,
            confidence=0.9,
        )

        routed = ProviderRouter.route_provider(intent, test_case["query"])

        assert routed.upper() == test_case["llm_provider"].upper()


if __name__ == "__main__":
    success = run_routing_fix_diagnostic()
    print_before_after_comparison()

    sys.exit(0 if success else 1)
