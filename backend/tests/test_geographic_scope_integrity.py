"""Geographic entities survive resolver, clarification and aggregate plumbing."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ParsedIntent
from backend.routing.country_resolver import CountryResolver
from backend.services.indicator_clarification import (
    build_group_scope_clarification,
    rewrite_group_scope_query,
)
from backend.services.query import QueryService


@pytest.mark.parametrize("wording", [
    "Europe", "European", "European countries", "European economies",
])
def test_continent_wording_never_expands_to_political_union(wording):
    assert "EU" not in CountryResolver.detect_regions_in_query(f"GDP in {wording}")
    assert CountryResolver.expand_regions_in_query(f"GDP in {wording}") == []
    assert not CountryResolver.expand_region(wording)


@pytest.mark.parametrize("wording,region,members", [
    ("European Union", "EU", CountryResolver.EU_MEMBERS),
    ("EU", "EU", CountryResolver.EU_MEMBERS),
    ("euro area", "EUROZONE", CountryResolver.EUROZONE_MEMBERS),
    ("eurozone", "EUROZONE", CountryResolver.EUROZONE_MEMBERS),
])
def test_named_groups_keep_existing_identity_and_members(wording, region, members):
    assert CountryResolver.detect_regions_in_query(f"GDP in {wording}") == [region]
    assert set(CountryResolver.expand_regions_in_query(f"GDP in {wording}")) == set(members)


@pytest.mark.parametrize("provider,wording,aggregate", [
    ("WorldBank", "European Union", "EUU"),
    ("Eurostat", "EU", "EU27_2020"),
    ("WorldBank", "euro area", "EMU"),
    ("Eurostat", "euro area", "EA20"),
])
def test_explicit_group_aggregate_is_preserved(provider, wording, aggregate):
    service = QueryService.__new__(QueryService)
    intent = ParsedIntent(
        apiProvider=provider, indicators=["GDP"], parameters={"country": aggregate}, clarificationNeeded=False,
    )
    assert build_group_scope_clarification(
        service, "isolated-aggregate", f"GDP in {wording}", intent, False,
    ) is None
    assert intent.parameters == {"country": aggregate}


@pytest.mark.parametrize("wording", ["EU", "European Union", "euro area"])
def test_explicit_member_comparison_does_not_become_one_aggregate(wording):
    service = QueryService.__new__(QueryService)
    intent = ParsedIntent(
        apiProvider="WorldBank", indicators=["GDP"],
        parameters={"countries": ["DE", "FR"]}, clarificationNeeded=False,
    )
    before = intent.model_copy(deep=True)
    assert build_group_scope_clarification(
        service, "isolated-members", f"compare GDP across {wording} member countries", intent, False,
    ) is None
    assert intent == before


def test_union_scope_rewrite_does_not_replace_plain_continent_wording():
    assert rewrite_group_scope_query("GDP in Europe", "EU", "compare_members") == "compare GDP in Europe"
    assert rewrite_group_scope_query("GDP in European Union", "EU", "compare_members") == "GDP across European Union member countries"


def test_parser_geographic_clarification_survives_standard_processing():
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    question = "Which geographic definition and member countries do you mean?"
    intent = ParsedIntent(
        apiProvider="WorldBank", indicators=["GDP"], parameters={"country": "Europe"},
        clarificationNeeded=True, clarificationQuestions=[question], originalQuery="GDP in Europe",
    )
    with patch.object(service.openrouter, "parse_query", return_value=intent), patch.object(
        service, "_fetch_data", new=AsyncMock(side_effect=AssertionError("Unresolved geography must not fetch")),
    ) as fetch:
        response = asyncio.run(service._standard_query_processing("GDP in Europe", "isolated-europe"))
    assert response.clarificationNeeded
    assert response.clarificationQuestions == [question]
    assert response.intent.parameters == {"country": "Europe"}
    assert response.data is None
    fetch.assert_not_awaited()
