"""A served aggregate composition code keeps the requested aggregate identity.

Eurostat serves the composition a dataset actually publishes (EA21 since
Bulgaria joined on 2026-01-01; une_rt_* no longer carry EA20/EA). Every
pipeline geography check (plan verification, coverage, cache admission) and a
later turn's provider switch must read EA21/EA19/EU28 as the euro area / the
EU by the aggregate code grammar, not by a fixed code set, while a different
family or a member country is still rejected. Nothing here pins series ids.
"""
from __future__ import annotations

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.execution_planner import build_minimal_execution_plan
from backend.services.geography_validation import assess_country_coverage
from backend.services.provider_strategy import (
    canonical_aggregate_region,
    normalize_aggregate_geography,
)
from backend.services.query import QueryService


def _intent(params, provider="Eurostat", query="euro area unemployment rate monthly since 2025"):
    return ParsedIntent(
        apiProvider=provider,
        indicators=["unemployment rate"],
        parameters={"startDate": "2025-01-01", **params},
        originalQuery=query,
        clarificationNeeded=False,
        confidence=0.95,
    )


def _series(country):
    return NormalizedData(
        metadata=Metadata(
            source="Eurostat", indicator="Unemployment by sex and age - monthly data",
            country=country, frequency="monthly", unit="percent", seriesId="une_rt_m",
        ),
        data=[{"date": "2026-09-01", "value": 6.3}],
    )


@pytest.mark.parametrize("code,region", [
    ("EA21", "EUROZONE"), ("EA19", "EUROZONE"), ("EA22", "EUROZONE"), ("ea21", "EUROZONE"),
    ("EA20", "EUROZONE"), ("EA", "EUROZONE"), ("Euro area", "EUROZONE"), ("EMU", "EUROZONE"),
    ("EU28", "EU"), ("EU27_2020", "EU"), ("EU15", "EU"), ("EU", "EU"),
])
def test_composition_codes_share_their_family_identity(code, region):
    assert canonical_aggregate_region(code) == region


@pytest.mark.parametrize("code", ["DE", "EE", "EL", "ES", "BG", "EAS", "EUU_X", "EA_20X", ""])
def test_countries_and_unrelated_codes_are_not_aggregates(code):
    assert canonical_aggregate_region(code) is None


@pytest.mark.parametrize("requested", ["Euro area", "Eurozone", "EA20", "EA", "EUROZONE"])
def test_verification_accepts_the_composition_the_dataset_publishes(requested):
    svc = QueryService.__new__(QueryService)
    intent = _intent({"country": requested})
    plan = build_minimal_execution_plan(intent.originalQuery, intent)
    assert svc._verify_execution_plan_structure(plan, [_series("EA21")]) is None


@pytest.mark.parametrize("served", ["EU27_2020", "DE", "FR"])
def test_verification_still_rejects_other_families_and_members(served):
    svc = QueryService.__new__(QueryService)
    intent = _intent({"country": "Euro area"})
    plan = build_minimal_execution_plan(intent.originalQuery, intent)
    assert svc._verify_execution_plan_structure(plan, [_series(served)])


@pytest.mark.parametrize("served", ["EU", "EU28", "EU27_2020"])
def test_eu_request_accepts_eu_compositions_but_not_the_euro_area(served):
    svc = QueryService.__new__(QueryService)
    intent = _intent({"country": "EU27_2020"}, query="EU unemployment rate")
    plan = build_minimal_execution_plan(intent.originalQuery, intent)
    assert svc._verify_execution_plan_structure(plan, [_series(served)]) is None
    assert svc._verify_execution_plan_structure(plan, [_series("EA21")])


def test_coverage_and_cache_admission_count_the_served_composition():
    intent = _intent({"countries": ["EA20", "FR"]})
    coverage = assess_country_coverage(intent, [_series("EA21"), _series("FR")])
    assert coverage is not None and coverage["complete"] is True
    svc = QueryService.__new__(QueryService)
    assert svc._cached_result_has_complete_country_coverage({"country": "EA20"}, [_series("EA21")])
    assert not svc._cached_result_has_complete_country_coverage({"country": "EA20"}, [_series("EU27_2020")])


@pytest.mark.parametrize("provider,value,expected", [
    # A composition code carried from an Eurostat turn maps to the other
    # provider's aggregate on a provider switch.
    ("WorldBank", "EA21", "EMU"),
    ("FRED", "EA21", "euro area"),
    # Eurostat resolves compositions itself: specific codes pass through (an
    # explicit EA19/EU28 is honoured where published), logical names map to
    # the logical first-attempt token.
    ("Eurostat", "EA19", "EA19"),
    ("Eurostat", "EA21", "EA21"),
    ("Eurostat", "EU28", "EU28"),
    ("Eurostat", "EA20", "EA20"),
    ("Eurostat", "Euro area", "EA"),
    ("Eurostat", "EMU", "EA"),
    ("Eurostat", "EA", "EA"),
    ("Eurostat", "EU", "EU27_2020"),
    ("Eurostat", "European Union", "EU27_2020"),
    ("Eurostat", "Germany", "Germany"),
])
def test_provider_native_normalization_by_family(provider, value, expected):
    assert normalize_aggregate_geography(provider, {"country": value})["country"] == expected
