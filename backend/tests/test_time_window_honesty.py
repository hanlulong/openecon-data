"""User-requested time windows must be honored in what gets SERVED.

Observed live (battery probe wb-empty-note): "GDP of Tuvalu since 2030"
produced an inverted range (start 2030 > defaulted end), the provider's date
param fell through, and the full 56-point history was served as silent
success. Same class: a frozen series (data ends 2019) served for "last 3
months". _enforce_user_time_window filters at YEAR granularity (annual and
quarterly points are dated Jan-1/quarter-start, so point-level comparison
against mid-year bounds would wrongly drop boundary-year observations) and
fires ONLY when the user set the scope (__time_scope_authority provenance).
"""
from __future__ import annotations

from backend.models import Metadata, NormalizedData
from backend.services.data_fetcher import _enforce_user_time_window


def _series(dates: list[str]) -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source="WorldBank", indicator="GDP (current US$)", country="Tuvalu",
            frequency="annual", unit="USD",
        ),
        data=[{"date": d, "value": 1.0} for d in dates],
    )


def test_future_window_drops_all_history() -> None:
    params = {"startDate": "2030-01-01", "__time_scope_authority": "user"}
    result = _enforce_user_time_window(params, "GDP of Tuvalu since 2030",
                                       [_series(["1990-01-01", "2005-01-01", "2024-01-01"])])
    assert result == []


def test_boundary_year_points_kept_at_year_granularity() -> None:
    # User window starts mid-year 2021; the 2021-01-01 annual point must stay.
    params = {"startDate": "2021-07-18", "endDate": "2026-07-17",
              "__time_scope_authority": "user"}
    result = _enforce_user_time_window(params, "GDP last 5 years",
                                       [_series(["2020-01-01", "2021-01-01", "2024-01-01"])])
    assert len(result) == 1
    assert [p.date for p in result[0].data] == ["2021-01-01", "2024-01-01"]


def test_default_window_untouched() -> None:
    # Framework-default windows keep their existing edge behavior entirely.
    params = {"startDate": "2021-07-18", "endDate": "2026-07-17",
              "__time_scope_authority": "default"}
    series = _series(["1990-01-01", "2024-01-01"])
    result = _enforce_user_time_window(params, "US GDP", [series])
    assert result == [series]
    assert len(result[0].data) == 2


def test_frozen_series_dropped_for_recent_window() -> None:
    # "last 3 months" on a series whose data ends in 2019 → honest empty.
    params = {"startDate": "2026-04-17", "endDate": "2026-07-17",
              "__time_scope_authority": "user"}
    result = _enforce_user_time_window(params, "china m2 last 3 months",
                                       [_series(["2018-08-01", "2019-08-01"])])
    assert result == []


def test_unparseable_dates_kept() -> None:
    params = {"startDate": "2020-01-01", "__time_scope_authority": "user"}
    series = _series(["latest", "Q1/26"])
    result = _enforce_user_time_window(params, "since 2020", [series])
    assert len(result) == 1 and len(result[0].data) == 2


# ---------------------------------------------------------------------------
# no_data_in_requested_window is NON-RETRIABLE: the window, not the code, is
# the constraint — the alternate-code retry must not burn 3 fetch+LLM rounds
# (observed live: 24s of pointless re-adjudication for "since 2030"), and
# cross-provider fallback must not serve another provider's out-of-window data.
# ---------------------------------------------------------------------------
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ParsedIntent
from backend.services.query import QueryService
from backend.utils.retry import DataNotAvailableError


def _window_intent() -> ParsedIntent:
    return ParsedIntent(
        apiProvider="WorldBank",
        indicators=["GDP"],
        parameters={
            "country": "Tuvalu",
            "indicator": "NY.GDP.MKTP.CN",
            "startDate": "2030-01-01",
            "__time_scope_authority": "user",
            "__decision_source": "llm_pick",
        },
        clarificationNeeded=False,
        originalQuery="GDP of Tuvalu since 2030",
    )


def test_alternate_retry_skips_window_errors() -> None:
    svc = QueryService.__new__(QueryService)
    marker = DataNotAvailableError(
        "no_data_in_requested_window: the series exists but has no "
        "observations inside the user-requested time window."
    )
    with patch(
        "backend.services.query._df_fetch_data",
        new_callable=AsyncMock,
        side_effect=marker,
    ) as fetch_mock:
        with pytest.raises(DataNotAvailableError, match="no_data_in_requested_window"):
            asyncio.run(svc._fetch_data(_window_intent()))
    assert fetch_mock.call_count == 1, "must NOT re-adjudicate on a window error"


def test_cross_provider_fallback_skips_window_errors() -> None:
    svc = QueryService.__new__(QueryService)
    marker = DataNotAvailableError("no_data_in_requested_window: out of window")
    with pytest.raises(DataNotAvailableError, match="no_data_in_requested_window"):
        asyncio.run(svc._try_with_fallback(_window_intent(), marker))
