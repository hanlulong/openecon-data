"""ChinaMacro must never present a lagging source's data as fresh.

Production Nov 2026: the social-financing (社融) live upstream had stalled at
2026-04 (~220 days, beyond the snapshot tool's own budget) yet every answer
said ``live: <source_org>``. The freshness budget now lives in the provider
(frequency budget + the registry DATA field ``staleness_extra_lag_days``) and
is evaluated for every series on both tiers.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.providers import chinamacro as cm
from backend.providers.chinamacro import (
    SERIES_REGISTRY,
    STALENESS_BUDGET_DAYS,
    ChinaMacroProvider,
    stale_latest_observation,
    staleness_budget,
)
from backend.utils.retry import DataNotAvailableError

TODAY = date(2026, 11, 7)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    monkeypatch.setattr(cm, "_today", lambda: TODAY)


def _monthly(last: str, n: int = 6):
    year, month = int(last[:4]), int(last[5:7])
    out = []
    for _ in range(n):
        out.append({"date": f"{year:04d}-{month:02d}-01", "value": 1.0})
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


def _install_live(monkeypatch, provider, points):
    async def _live(series):
        return points

    monkeypatch.setattr(provider, "_live_observations", _live)


# ---------------------------------------------------------------------------
# Every registry series is evaluated by the same budget mechanism
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("series", SERIES_REGISTRY, ids=lambda s: s["id"])
def test_every_registry_series_has_a_frequency_budget(series):
    assert series["frequency"] in STALENESS_BUDGET_DAYS, (
        f"{series['id']}: frequency {series['frequency']!r} has no staleness budget"
    )
    extra = series.get("staleness_extra_lag_days", 0)
    assert isinstance(extra, int) and extra >= 0
    assert staleness_budget(series) == timedelta(days=STALENESS_BUDGET_DAYS[series["frequency"]] + extra)


@pytest.mark.parametrize("series", SERIES_REGISTRY, ids=lambda s: s["id"])
def test_every_registry_series_flips_exactly_at_its_budget(series):
    points, _as_of = ChinaMacroProvider._csv_observations(series["id"])
    latest = date.fromisoformat(points[-1]["date"][:10])
    budget = staleness_budget(series)
    assert stale_latest_observation(series, points, today=latest + budget) is None
    assert stale_latest_observation(series, points, today=latest + budget + timedelta(days=1)) == latest


def test_unparseable_latest_date_is_not_called_stale():
    series = SERIES_REGISTRY[0]
    assert stale_latest_observation(series, [{"date": "n/a", "value": 1}]) is None
    assert stale_latest_observation(series, []) is None


def test_extra_lag_is_registry_data_not_code():
    lagged = [s for s in SERIES_REGISTRY if s.get("staleness_extra_lag_days")]
    assert lagged, "registry should carry at least one structurally lagged source"
    for series in lagged:
        base = STALENESS_BUDGET_DAYS[series["frequency"]]
        assert staleness_budget(series).days == base + series["staleness_extra_lag_days"]


# ---------------------------------------------------------------------------
# Served answers disclose lag on both tiers; fresh data is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("series", [s for s in SERIES_REGISTRY if s["frequency"] == "monthly"][:4] +
                         [s for s in SERIES_REGISTRY if s.get("staleness_extra_lag_days")],
                         ids=lambda s: s["id"])
async def test_stale_live_upstream_is_disclosed(monkeypatch, series):
    provider = ChinaMacroProvider()
    _install_live(monkeypatch, provider, _monthly("2026-04"))
    result = await provider.fetch_indicator(series["id"])
    assert "live: " in result.metadata.apiUrl
    assert "(source lagging: latest 2026-04)" in result.metadata.apiUrl
    notes = result.metadata.notes or []
    assert len(notes) == 1 and notes[0].startswith("Source lagging")
    assert "2026-04" in notes[0] and "has not published newer data yet" in notes[0]
    assert result.data[-1].date == "2026-04-01"  # data itself is never altered


@pytest.mark.asyncio
async def test_fresh_live_series_is_unchanged(monkeypatch):
    provider = ChinaMacroProvider()
    _install_live(monkeypatch, provider, _monthly("2026-10"))
    result = await provider.fetch_indicator("CN_PMI_MFG")
    assert result.metadata.apiUrl.endswith(f"[live: {cm._REGISTRY_BY_ID['CN_PMI_MFG']['source_org']}]")
    assert not result.metadata.notes


@pytest.mark.asyncio
async def test_lagged_source_within_its_extra_budget_is_fresh(monkeypatch):
    lagged = next(s for s in SERIES_REGISTRY if s.get("staleness_extra_lag_days"))
    provider = ChinaMacroProvider()
    # 2026-08-01 is 98 days old: over the plain monthly budget, inside the extra lag.
    _install_live(monkeypatch, provider, _monthly("2026-08"))
    result = await provider.fetch_indicator(lagged["id"])
    assert "lagging" not in result.metadata.apiUrl
    assert not result.metadata.notes


@pytest.mark.asyncio
async def test_daily_series_uses_daily_budget(monkeypatch):
    daily = next(s for s in SERIES_REGISTRY if s["frequency"] == "daily")
    provider = ChinaMacroProvider()
    _install_live(monkeypatch, provider, [{"date": "2026-10-01", "value": 1.8}])
    result = await provider.fetch_indicator(daily["id"])
    assert "(source lagging: latest 2026-10-01)" in result.metadata.apiUrl


@pytest.mark.asyncio
async def test_stale_snapshot_fallback_is_disclosed(monkeypatch):
    monkeypatch.setattr(cm, "_today", lambda: date(2099, 1, 1))
    provider = ChinaMacroProvider()

    async def _down(series):
        raise DataNotAvailableError("live endpoint down (test)")

    monkeypatch.setattr(provider, "_live_observations", _down)
    result = await provider.fetch_indicator("CN_PMI_MFG")
    assert "curated snapshot" in result.metadata.apiUrl
    assert "source lagging" in result.metadata.apiUrl
    assert "curated snapshot" in result.metadata.notes[0]
    assert "has not published" not in result.metadata.notes[0]


@pytest.mark.asyncio
async def test_closed_historical_window_is_not_flagged(monkeypatch):
    provider = ChinaMacroProvider()
    _install_live(monkeypatch, provider, _monthly("2026-04", n=60))
    result = await provider.fetch_indicator("CN_PMI_MFG", start_date="2023-01-01", end_date="2024-12-31")
    assert "lagging" not in result.metadata.apiUrl
    assert not result.metadata.notes
    open_ended = await provider.fetch_indicator("CN_PMI_MFG", start_date="2023-01-01", end_date="2026-12-31")
    assert open_ended.metadata.notes


@pytest.mark.asyncio
@pytest.mark.parametrize("end_date,flagged", [
    ("2026-04-30", False),  # fully covered by the 2026-04 monthly observation
    ("2026-04-15", False),
    ("2026-05-01", True),   # first day the lagging source cannot cover
])
async def test_window_end_is_compared_at_period_granularity(monkeypatch, end_date, flagged):
    provider = ChinaMacroProvider()
    _install_live(monkeypatch, provider, _monthly("2026-04", n=24))
    result = await provider.fetch_indicator("CN_PMI_MFG", start_date="2025-01-01", end_date=end_date)
    assert bool(result.metadata.notes) is flagged
    assert ("lagging" in result.metadata.apiUrl) is flagged


@pytest.mark.parametrize("observed,frequency,expected", [
    ("2026-04-01", "monthly", "2026-04-30"),
    ("2026-12-01", "monthly", "2026-12-31"),
    ("2024-02-01", "monthly", "2024-02-29"),
    ("2026-06-01", "quarterly", "2026-06-30"),  # quarter-end-month dating
    ("2026-04-01", "quarterly", "2026-06-30"),  # quarter-start dating
    ("2026-12-01", "quarterly", "2026-12-31"),
    ("2026-10-09", "daily", "2026-10-09"),
])
def test_observation_period_end(observed, frequency, expected):
    assert cm.observation_period_end(date.fromisoformat(observed), frequency) == date.fromisoformat(expected)


@pytest.mark.asyncio
async def test_window_after_a_lagging_latest_observation_explains_the_empty_answer(monkeypatch):
    provider = ChinaMacroProvider()
    _install_live(monkeypatch, provider, _monthly("2026-04"))
    with pytest.raises(DataNotAvailableError) as info:
        await provider.fetch_indicator("CN_PMI_MFG", start_date="2026-09-01")
    assert "no observations in the requested window" in str(info.value)
    assert "Source lagging" in str(info.value) and "2026-04" in str(info.value)


@pytest.mark.parametrize("series", SERIES_REGISTRY, ids=lambda s: s["id"])
def test_publication_cadence_alone_never_exceeds_the_budget(series):
    """A series' own regular gaps (e.g. NBS activity data has no Jan/Feb
    observation, so Dec -> Mar is ~90 days) must fit inside its budget, or the
    served answer would call a normal release pause "source lagging". A new
    series with such a cadence needs registry staleness_extra_lag_days."""
    points, as_of = ChinaMacroProvider._csv_observations(series["id"])
    dates = [date.fromisoformat(p["date"][:10]) for p in points]
    as_of_date = date.fromisoformat(str(as_of)[:10])
    recent = [d for d in dates if d >= as_of_date - timedelta(days=5 * 366)]
    largest_gap = max((b - a for a, b in zip(recent, recent[1:])), default=timedelta(0))
    assert largest_gap < staleness_budget(series), (
        f"{series['id']}: regular gap {largest_gap.days}d >= budget {staleness_budget(series).days}d"
    )
