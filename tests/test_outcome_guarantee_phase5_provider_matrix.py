from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.models import ParsedIntent
from backend.services.data_fetcher import fetch_from_provider_dispatch, materialize_execution_plan
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


def test_phase5_imf_provider_request_contract_is_materialized() -> None:
    plan = _materialized_plan(
        "IMF",
        {"country": "US", "indicator": "NGDP_RPCH", "startDate": "2019-01-01", "endDate": "2020-12-31"},
        query="US real GDP growth from IMF",
        indicators=["real GDP growth"],
    )
    assert plan.provider_request["provider"] == "IMF"
    assert plan.provider_request["indicator"] == "NGDP_RPCH"
    assert plan.provider_request["country"] == "US"
    assert plan.provider_request["country_scope"] == ["US"]


def test_phase5_oecd_provider_request_contract_is_materialized() -> None:
    plan = _materialized_plan(
        "OECD",
        {"countries": ["ALL_OECD"], "indicator": "UNRTE", "startDate": "2019-01-01", "endDate": "2020-12-31"},
        query="OECD unemployment",
        indicators=["unemployment"],
    )
    assert plan.provider_request["provider"] == "OECD"
    assert plan.provider_request["indicator"] == "UNRTE"
    assert plan.provider_request["countries"] == ["ALL_OECD"]
    assert plan.provider_request["country"] is None


def test_phase5_coingecko_provider_request_contract_is_materialized() -> None:
    plan = _materialized_plan(
        "COINGECKO",
        {"coinIds": ["bitcoin", "ethereum"], "vsCurrency": "eur", "days": 30},
        query="bitcoin and ethereum price over the last 30 days",
        indicators=["price"],
    )
    assert plan.provider_request["provider"] == "COINGECKO"
    assert plan.provider_request["coin_ids"] == ["bitcoin", "ethereum"]
    assert plan.provider_request["vs_currency"] == "eur"
    assert plan.provider_request["days"] == 30


class _FakeIMFProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _resolve_countries(self, value: str) -> list[str]:
        return [str(value)]

    async def fetch_indicator(self, **kwargs):
        self.calls.append(kwargs)
        return {"provider": "IMF", **kwargs}

    async def fetch_batch_indicator(self, **kwargs):
        self.calls.append(kwargs)
        return [{"provider": "IMF", **kwargs}]


class _FakeOECDProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def expand_countries(self, value: str) -> list[str]:
        return [str(value)]

    async def fetch_indicator(self, **kwargs):
        self.calls.append(kwargs)
        return {"provider": "OECD", **kwargs}

    async def fetch_multi_country(self, **kwargs):
        self.calls.append(kwargs)
        return [{"provider": "OECD", **kwargs}]


def test_phase5_imf_dispatch_prefers_provider_request_contract() -> None:
    plan = _materialized_plan(
        "IMF",
        {"country": "US", "indicator": "NGDP_RPCH", "startDate": "2019-01-01", "endDate": "2020-12-31"},
        query="US real GDP growth from IMF",
        indicators=["real GDP growth"],
    )
    plan.provider_request["country"] = "FR"
    plan.provider_request["country_scope"] = ["FR"]
    svc = SimpleNamespace(imf_provider=_FakeIMFProvider())
    intent = ParsedIntent(
        apiProvider="IMF",
        indicators=["real GDP growth"],
        parameters={**dict(plan.params), "__semantic_authority": "exact_user_input"},
        clarificationNeeded=False,
        originalQuery="US real GDP growth from IMF",
        queryType="data_fetch",
    )
    plan.params["__semantic_authority"] = "exact_user_input"
    result = asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))
    assert svc.imf_provider.calls[0]["countries"] == ["FR"]
    assert result == [{"provider": "IMF", **svc.imf_provider.calls[0]}]


def test_phase5_oecd_dispatch_prefers_provider_request_contract() -> None:
    plan = _materialized_plan(
        "OECD",
        {"country": "US", "indicator": "UNRTE", "startDate": "2019-01-01", "endDate": "2020-12-31"},
        query="US unemployment from OECD",
        indicators=["unemployment"],
    )
    plan.provider_request["country"] = "OECD"
    svc = SimpleNamespace(oecd_provider=_FakeOECDProvider())
    intent = ParsedIntent(
        apiProvider="OECD",
        indicators=["unemployment"],
        parameters=dict(plan.params),
        clarificationNeeded=False,
        originalQuery="US unemployment from OECD",
        queryType="data_fetch",
    )
    plan.params["__semantic_authority"] = "exact_user_input"
    result = asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))
    assert svc.oecd_provider.calls[0]["country"] == "OECD"
    assert result == [{"provider": "OECD", **svc.oecd_provider.calls[0]}]
