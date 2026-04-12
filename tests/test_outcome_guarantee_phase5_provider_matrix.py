from __future__ import annotations

from backend.models import ParsedIntent
from backend.services.data_fetcher import materialize_execution_plan
from backend.services.execution_planner import build_minimal_execution_plan


def _materialized_plan(provider: str, params: dict, *, query: str, indicators: list[str], query_type: str = "data_fetch"):
    intent = ParsedIntent(
        apiProvider=provider,
        indicators=indicators,
        parameters=params,
        clarificationNeeded=False,
        originalQuery=query,
        queryType=query_type,
    )
    plan = build_minimal_execution_plan(query, intent)
    return materialize_execution_plan(
        plan,
        provider=provider,
        intent=intent,
        params=params,
    )


def test_phase5_fred_provider_request_contract_is_materialized() -> None:
    plan = _materialized_plan(
        "FRED",
        {"country": "US", "seriesId": "GDP", "indicator": "GDP"},
        query="show me US GDP",
        indicators=["GDP"],
    )
    assert plan.provider_request["provider"] == "FRED"
    assert plan.provider_request["series_id"] == "GDP"


def test_phase5_worldbank_provider_request_contract_is_materialized() -> None:
    plan = _materialized_plan(
        "WORLDBANK",
        {"countries": ["US", "DE"], "indicator": "FP.CPI.TOTL.ZG", "startDate": "2019-01-01", "endDate": "2020-12-31"},
        query="compare inflation in the US and Germany",
        indicators=["inflation"],
        query_type="comparison",
    )
    assert plan.provider_request["provider"] == "WORLDBANK"
    assert plan.provider_request["indicator"] == "FP.CPI.TOTL.ZG"
    assert plan.provider_request["countries"] == ["US", "DE"]


def test_phase5_eurostat_provider_request_contract_is_materialized() -> None:
    plan = _materialized_plan(
        "EUROSTAT",
        {"country": "DE", "indicator": "prc_hicp_manr", "startDate": "2019-01-01", "endDate": "2020-12-31"},
        query="hicp inflation germany",
        indicators=["harmonized inflation"],
    )
    assert plan.provider_request["provider"] == "EUROSTAT"
    assert plan.provider_request["dataset_code"] == "prc_hicp_manr"
    assert plan.provider_request["country_scope"] == ["DE"]
