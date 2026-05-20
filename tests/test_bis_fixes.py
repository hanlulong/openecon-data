"""Integration coverage for BIS provider queries that previously regressed."""
from __future__ import annotations

import os
import sys

import pytest

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.providers.bis import BISProvider


def _assert_valid_results(results, *, expected_countries: tuple[str, ...] = ()) -> None:
    assert results, "Expected at least one BIS result"

    first = results[0]
    assert first.metadata is not None
    assert first.data, "Expected at least one data point"
    assert first.metadata.source == "BIS"
    assert first.metadata.indicator
    assert first.metadata.frequency
    assert first.metadata.unit

    if expected_countries:
        assert first.metadata.country
        actual_country = first.metadata.country.lower()
        assert any(expected.lower() in actual_country for expected in expected_countries)

    assert first.data[0].date is not None
    assert first.data[0].value is not None
    assert first.data[-1].date is not None
    assert first.data[-1].value is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_credit_to_gdp() -> None:
    provider = BISProvider()
    results = await provider.fetch_indicator(
        indicator="WS_TC",
        country="US",
        start_year=2020,
        end_year=2023,
    )
    _assert_valid_results(results, expected_countries=("United States",))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_policy_rate() -> None:
    provider = BISProvider()
    results = await provider.fetch_indicator(
        indicator="WS_CBPOL",
        country="Germany",
        start_year=2020,
        end_year=2023,
    )
    _assert_valid_results(results, expected_countries=("Germany", "Euro Area"))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_property_prices() -> None:
    provider = BISProvider()
    results = await provider.fetch_indicator(
        indicator="WS_SPP",
        country="UK",
        start_year=2020,
        end_year=2023,
    )
    _assert_valid_results(results)
