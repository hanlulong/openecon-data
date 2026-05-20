"""
Focused integration tests for query-service provider selection.

These tests exercise QueryService's routing selection logic without hitting the
LLM or remote provider APIs.
"""
from __future__ import annotations

import os
import sys

import pytest

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.models import ParsedIntent
from backend.routing.unified_router import UnifiedRouter
from backend.services.query import QueryService


@pytest.fixture
def lightweight_query_service() -> QueryService:
    service = QueryService.__new__(QueryService)
    service.unified_router = UnifiedRouter()
    service.semantic_provider_router = None
    service.hybrid_router = None
    return service


@pytest.mark.asyncio
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
                indicators=["GDP"],
                parameters={"country": "Canada"},
                clarificationNeeded=False,
            ),
            "Canada GDP",
            "WORLDBANK",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["house prices"],
                parameters={"country": "Germany"},
                clarificationNeeded=False,
            ),
            "Germany house prices",
            "WORLDBANK",
        ),
        (
            ParsedIntent(
                apiProvider="WorldBank",
                indicators=["imports"],
                parameters={"country": "US"},
                clarificationNeeded=False,
            ),
            "US imports",
            "WORLDBANK",
        ),
    ],
)
async def test_query_service_selects_expected_provider(
    lightweight_query_service: QueryService,
    intent: ParsedIntent,
    query: str,
    expected_provider: str,
) -> None:
    provider = await lightweight_query_service._select_routed_provider(intent, query)
    assert provider == expected_provider


@pytest.mark.asyncio
async def test_query_service_applies_country_coverage_override(
    lightweight_query_service: QueryService,
) -> None:
    intent = ParsedIntent(
        apiProvider="Eurostat",
        indicators=["GDP"],
        parameters={"countries": ["France", "Japan"]},
        clarificationNeeded=False,
    )

    provider = await lightweight_query_service._select_routed_provider(
        intent,
        "compare gdp in france and japan",
    )

    assert provider == "WORLDBANK"
