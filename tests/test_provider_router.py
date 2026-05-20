"""
Pytest coverage for deterministic provider routing.

These tests validate the legacy ProviderRouter import path without relying on
the LLM parsing layer. The compatibility surface now delegates final routing
to the no-shortcut UnifiedRouter: explicit/mechanical routes may override, but
semantic coverage hints must not force provider changes.
"""
from __future__ import annotations

import os
import sys

import pytest

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.models import ParsedIntent
from backend.services.provider_router import ProviderRouter


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Show me Italy GDP from OECD", "OECD"),
        ("Get data from OECD for Germany", "OECD"),
        ("Using OECD, show me unemployment", "OECD"),
        ("OECD average GDP growth", None),
        ("Show me inflation from IMF", "IMF"),
        ("According to the IMF", "IMF"),
        ("Using IMF data", "IMF"),
        ("Get France GDP from Eurostat", "Eurostat"),
        ("Using Eurostat", "Eurostat"),
        ("Show me data from FRED", "FRED"),
        ("Using Federal Reserve data", "FRED"),
        ("From Statistics Canada", "StatsCan"),
        ("Using StatsCan", "StatsCan"),
        ("Get Russia imports from Comtrade", "Comtrade"),
        ("Using UN Comtrade", "Comtrade"),
        ("Show me US GDP", None),
        ("Canada unemployment", None),
        ("China imports", None),
    ],
)
def test_explicit_provider_detection(query: str, expected: str | None) -> None:
    assert ProviderRouter.detect_explicit_provider(query) == expected


@pytest.mark.parametrize(
    "indicators",
    [
        ["Case-Shiller"],
        ["federal funds rate"],
        ["PCE"],
        ["nonfarm payrolls"],
        ["S&P 500"],
        ["prime lending rate"],
        ["GDP"],
        ["unemployment"],
        ["inflation"],
    ],
)
def test_legacy_us_only_indicator_hook_is_not_semantic_authority(indicators: list[str]) -> None:
    assert ProviderRouter.is_us_only_indicator(indicators) is False


@pytest.mark.parametrize(
    ("query", "parameters", "expected"),
    [
        ("Canada GDP", {}, False),
        ("Ontario unemployment", {}, False),
        ("Toronto population", {}, False),
        ("Canadian inflation", {}, False),
        ("Show me BC housing starts", {}, False),
        ("US GDP", {}, False),
        ("China imports", {}, False),
        ("Get data", {"country": "Canada"}, True),
        ("Get data", {"country": "CA"}, True),
        ("Get data", {"countries": ["US", "CA"]}, True),
        ("Get data", {"country": "US"}, False),
    ],
)
def test_canadian_query_detection_uses_explicit_parameters_only(
    query: str, parameters: dict[str, str], expected: bool
) -> None:
    assert ProviderRouter.is_canadian_query(query, parameters) is expected


@pytest.mark.parametrize(
    ("intent", "query", "expected_provider"),
    [
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["GDP"],
                parameters={"country": "Italy"},
                clarificationNeeded=False,
            ),
            "Get Italy GDP from OECD",
            "OECD",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["Case-Shiller"],
                parameters={},
                clarificationNeeded=False,
            ),
            "Show me Case-Shiller index",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["GDP"],
                parameters={"country": "Canada"},
                clarificationNeeded=False,
            ),
            "Canada GDP",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="OECD",
                indicators=["GDP"],
                parameters={"country": "China"},
                clarificationNeeded=False,
            ),
            "China GDP",
            "OECD",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["government debt"],
                parameters={"country": "US"},
                clarificationNeeded=False,
            ),
            "US government debt",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["house prices"],
                parameters={"country": "Germany"},
                clarificationNeeded=False,
            ),
            "Germany house prices",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["house prices"],
                parameters={"country": "US"},
                clarificationNeeded=False,
            ),
            "US house prices",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["imports"],
                parameters={"country": "US"},
                clarificationNeeded=False,
            ),
            "US imports",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["Bitcoin"],
                parameters={},
                clarificationNeeded=False,
            ),
            "Bitcoin price",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["exchange rate"],
                parameters={},
                clarificationNeeded=False,
            ),
            "USD to EUR exchange rate",
            "ExchangeRate",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["GDP"],
                parameters={},
                clarificationNeeded=False,
            ),
            "Show me GDP",
            "WorldBank",
        ),
    ],
)
def test_provider_routing(intent: ParsedIntent, query: str, expected_provider: str) -> None:
    assert ProviderRouter.route_provider(intent, query).upper() == expected_provider.upper()
