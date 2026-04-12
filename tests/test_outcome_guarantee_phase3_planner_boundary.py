from __future__ import annotations

from backend.models import ParsedIntent
from backend.services.data_fetcher import materialize_execution_plan
from backend.services.execution_planner import build_minimal_execution_plan


def test_phase3_execution_plan_supports_provider_request_and_cache_identity() -> None:
    intent = ParsedIntent(
        apiProvider="Eurostat",
        indicators=["harmonized inflation"],
        parameters={"country": "DE", "indicator": "prc_hicp_manr"},
        clarificationNeeded=False,
        originalQuery="hicp inflation germany",
    )

    plan = build_minimal_execution_plan("hicp inflation germany", intent)
    plan = materialize_execution_plan(
        plan,
        provider="EUROSTAT",
        intent=intent,
        params={
            "country": "DE",
            "indicator": "prc_hicp_manr",
            "startDate": "2019-01-01",
            "endDate": "2020-12-31",
        },
    )

    assert plan.provider_request["provider"] == "EUROSTAT"
    assert plan.provider_request["dataset_code"] == "prc_hicp_manr"
    assert plan.provider_request["country_scope"] == ["DE"]
    assert plan.cache_identity["provider_request"]["code"] == "prc_hicp_manr"


def test_phase3_materialized_plan_uses_resolved_request_contract_not_query_text() -> None:
    intent = ParsedIntent(
        apiProvider="Eurostat",
        indicators=["inflation"],
        parameters={"country": "DE", "indicator": "prc_hicp_manr"},
        clarificationNeeded=False,
        originalQuery="show germany hicp inflation",
    )

    plan = build_minimal_execution_plan("some unrelated phrasing that should not matter", intent)
    plan = materialize_execution_plan(
        plan,
        provider="EUROSTAT",
        intent=intent,
        params={
            "country": "DE",
            "indicator": "prc_hicp_manr",
            "startDate": "2019-01-01",
            "endDate": "2020-12-31",
        },
    )

    assert plan.provider_request["dataset_code"] == "prc_hicp_manr"
    assert plan.params["indicator"] == "prc_hicp_manr"
    assert plan.expected_shape["requested_indicator"] == "inflation"
