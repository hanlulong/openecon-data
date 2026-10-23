"""Framework defaults must never replace supplied Comtrade date windows."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services import time_range_defaults
from backend.services.data_fetcher import (
    _enforce_user_time_window,
    fetch_from_provider_dispatch,
    materialize_execution_plan,
)
from backend.services.parameter_validator import ParameterValidator


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 23, tzinfo=tz)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(time_range_defaults, "datetime", FrozenDatetime)
    monkeypatch.setattr("backend.services.parameter_validator.datetime", FrozenDatetime)


@pytest.mark.parametrize("year", [2020, 2021, 2022])
@pytest.mark.parametrize("authority", ["user", None, "unknown"])
def test_supplied_window_is_preserved(year, authority):
    params = {"startDate": f"{year}-01-01", "endDate": f"{year}-12-31"}
    if authority is not None:
        params["__time_scope_authority"] = authority
    before = params.copy()
    assert time_range_defaults.apply_default_time_range("COMTRADE", params) == before


@pytest.mark.parametrize("bound", ["startDate", "endDate"])
@pytest.mark.parametrize("authority", ["user", None])
def test_partial_window_is_preserved(bound, authority):
    params = {bound: "2021-07-01"}
    if authority is not None:
        params["__time_scope_authority"] = authority
    before = params.copy()
    assert time_range_defaults.apply_default_time_range("COMTRADE", params) == before


def test_missing_window_keeps_ten_year_default():
    params = time_range_defaults.apply_default_time_range("COMTRADE", {})
    assert (params["startDate"], params["endDate"]) == ("2016-01-01", "2026-12-31")
    assert (params["start_year"], params["end_year"]) == (2016, 2026)


def test_validator_framework_default_keeps_ten_year_policy():
    intent = ParsedIntent(
        apiProvider="COMTRADE", indicators=["total exports"], parameters={},
        clarificationNeeded=False, originalQuery="Canada total exports",
    )
    ParameterValidator.apply_default_time_periods(intent)
    assert intent.parameters["__time_scope_authority"] == "default"
    params = time_range_defaults.apply_default_time_range("COMTRADE", intent.parameters)
    assert (params["startDate"], params["endDate"]) == ("2016-01-01", "2026-12-31")
    assert params["__time_scope_authority"] == "default"


def test_explicit_year_reaches_dispatch_and_filters_returned_history():
    intent = ParsedIntent(
        apiProvider="COMTRADE", indicators=["total exports"],
        parameters={
            "reporter": "Canada", "partner": "World", "flow": "EXPORT",
            "startDate": "2021-01-01", "endDate": "2021-12-31",
        },
        clarificationNeeded=False, originalQuery="Canada exports in 2021",
    )
    ParameterValidator.apply_default_time_periods(intent)
    params = time_range_defaults.apply_default_time_range("COMTRADE", intent.parameters)
    plan = materialize_execution_plan(None, provider="COMTRADE", intent=intent, params=params)
    series = NormalizedData(
        metadata=Metadata(source="UN Comtrade", indicator="Exports - Total Trade",
                          country="Canada", frequency="annual", unit="US Dollars"),
        data=[{"date": f"{year}-01-01", "value": value}
              for year, value in [(2020, 100), (2021, 200), (2022, 300)]],
    )
    fetch = AsyncMock(return_value=[series])
    svc = SimpleNamespace(comtrade_provider=SimpleNamespace(fetch_trade_data=fetch))
    result = asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))
    assert fetch.call_args.kwargs["start_year"] == 2021
    assert fetch.call_args.kwargs["end_year"] == 2021
    result = _enforce_user_time_window(params, intent.originalQuery, result)
    assert len(result) == 1
    assert [(point.date, point.value) for point in result[0].data] == [("2021-01-01", 200)]
