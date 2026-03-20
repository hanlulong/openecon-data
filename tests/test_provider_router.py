"""
Pytest coverage for deterministic provider routing.

These tests validate the legacy ProviderRouter directly without relying on the
LLM parsing layer.
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
    ("indicators", "expected"),
    [
        (["Case-Shiller"], True),
        (["federal funds rate"], True),
        (["PCE"], True),
        (["nonfarm payrolls"], True),
        (["S&P 500"], True),
        (["prime lending rate"], True),
        (["GDP"], False),
        (["unemployment"], False),
        (["inflation"], False),
    ],
)
def test_us_only_indicator_detection(indicators: list[str], expected: bool) -> None:
    assert ProviderRouter.is_us_only_indicator(indicators) is expected


@pytest.mark.parametrize(
    ("query", "parameters", "expected"),
    [
        ("Canada GDP", {}, True),
        ("Ontario unemployment", {}, True),
        ("Toronto population", {}, True),
        ("Canadian inflation", {}, True),
        ("Show me BC housing starts", {}, True),
        ("US GDP", {}, False),
        ("China imports", {}, False),
        ("Get data", {"country": "Canada"}, True),
        ("Get data", {"country": "US"}, False),
    ],
)
def test_canadian_query_detection(query: str, parameters: dict[str, str], expected: bool) -> None:
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
            "FRED",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["GDP"],
                parameters={"country": "Canada"},
                clarificationNeeded=False,
            ),
            "Canada GDP",
            "StatsCan",
        ),
        (
            ParsedIntent(
                apiProvider="OECD",
                indicators=["GDP"],
                parameters={"country": "China"},
                clarificationNeeded=False,
            ),
            "China GDP",
            "WorldBank",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["government debt"],
                parameters={"country": "US"},
                clarificationNeeded=False,
            ),
            "US government debt",
            "IMF",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["house prices"],
                parameters={"country": "Germany"},
                clarificationNeeded=False,
            ),
            "Germany house prices",
            "BIS",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["house prices"],
                parameters={"country": "US"},
                clarificationNeeded=False,
            ),
            "US house prices",
            "BIS",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["imports"],
                parameters={"country": "US"},
                clarificationNeeded=False,
            ),
            "US imports",
            "Comtrade",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["Bitcoin"],
                parameters={},
                clarificationNeeded=False,
            ),
            "Bitcoin price",
            "CoinGecko",
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
