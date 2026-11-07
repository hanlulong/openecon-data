"""Comtrade period caps must be structural, never a guessed reporting lag.

Production Nov 2026: the annual end year was capped at ``now.year - 2`` (2024)
although UN Comtrade already had 2025 annual rows (US-China), so every
annual request silently lost its latest year. An unfiled period returns 200
with zero rows, not a 400, so only structurally impossible periods are capped
(annual: the year still in progress; quarterly: beyond the current year) and a
reporter that has not filed yet is DISCLOSED rather than guessed around.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from backend.models import DataPoint, Metadata, NormalizedData
from backend.providers import comtrade as comtrade_module
from backend.providers.comtrade import ComtradeProvider
from backend.utils.retry import DataNotAvailableError


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 7, tzinfo=tz)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(comtrade_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(comtrade_module, "get_http_client", lambda: None)
    monkeypatch.setattr(comtrade_module, "GLOBAL_REQUEST_MIN_INTERVAL_SECONDS", 0.0)


def _install_upstream(monkeypatch, provider, filed_periods):
    """Fake UN Comtrade: returns rows only for periods the reporter filed."""
    requested = []

    async def fake_single(client, reporter_raw, partner_code, commodity_code, flow_code, period_param, freq_code):
        periods = period_param.split(",")
        requested.extend(periods)
        rows = [p for p in periods if p in filed_periods]
        if not rows:
            return []
        if freq_code == "A":
            dates = rows
        else:
            dates = [f"{p[:4]}-{p[4:6]}" for p in rows]
        return [
            NormalizedData(
                metadata=Metadata(
                    source="UN Comtrade",
                    indicator="Exports - Total Trade",
                    country=f"Reporter {reporter_raw}",
                    frequency={"A": "annual", "M": "monthly", "Q": "quarterly"}[freq_code],
                    unit="US Dollars",
                ),
                data=[DataPoint(date=d, value=100.0 + i) for i, d in enumerate(dates)],
            )
        ]

    monkeypatch.setattr(provider, "_fetch_single_reporter_data", fake_single)
    return requested


def _fetch(provider, **kwargs):
    kwargs.setdefault("reporter", "842")
    kwargs.setdefault("partner", "156")
    kwargs.setdefault("flow", "EXPORT")
    return asyncio.run(provider.fetch_trade_data(**kwargs))


def test_annual_request_reaches_last_completed_year_and_returns_it(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    requested = _install_upstream(monkeypatch, provider, {"2023", "2024", "2025"})
    result = _fetch(provider, start_year=2023, end_year=2025)
    assert "2025" in requested
    assert [p.date for p in result[0].data] == ["2023", "2024", "2025"]
    assert not result[0].metadata.notes


def test_annual_year_in_progress_is_capped_structurally(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    requested = _install_upstream(monkeypatch, provider, {"2024", "2025"})
    result = _fetch(provider, start_year=2024, end_year=2026)
    assert "2026" not in requested  # annual 2026 cannot exist in Nov 2026
    assert [p.date for p in result[0].data] == ["2024", "2025"]


def test_default_ten_year_window_now_includes_latest_completed_year(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    filed = {str(y) for y in range(2016, 2026)}
    requested = _install_upstream(monkeypatch, provider, filed)
    result = _fetch(provider, start_year=2016, end_year=2026)
    assert result[0].data[-1].date == "2025"
    assert "2025" in requested


@pytest.mark.parametrize("filed_through", [2023, 2024])
def test_unfiled_trailing_years_are_disclosed_per_reporter(monkeypatch, filed_through):
    provider = ComtradeProvider(api_key="demo")
    filed = {str(y) for y in range(2020, filed_through + 1)}
    _install_upstream(monkeypatch, provider, filed)
    result = _fetch(provider, start_year=2020, end_year=2025)
    assert result[0].data[-1].date == str(filed_through)
    notes = result[0].metadata.notes or []
    assert len(notes) == 1
    missing = "2025" if filed_through == 2024 else "2024-2025"
    assert notes[0].startswith(f"UN Comtrade returned no annual rows for {missing} from Reporter 842")
    assert "not yet reported, or not available" in notes[0]
    assert f"latest period returned: {filed_through}." in notes[0]


def test_multi_reporter_disclosure_only_on_lagging_reporter(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    filed = {"2024", "2025"}

    async def fake_single(client, reporter_raw, partner_code, commodity_code, flow_code, period_param, freq_code):
        own = filed if reporter_raw == "842" else {"2024"}
        rows = [p for p in period_param.split(",") if p in own]
        if not rows:
            return []
        return [
            NormalizedData(
                metadata=Metadata(
                    source="UN Comtrade", indicator="Exports - Total Trade",
                    country=f"Reporter {reporter_raw}", frequency="annual", unit="US Dollars",
                ),
                data=[DataPoint(date=d, value=1.0) for d in rows],
            )
        ]

    monkeypatch.setattr(provider, "_fetch_single_reporter_data", fake_single)
    result = asyncio.run(
        provider.fetch_trade_data(reporters=["842", "124"], partner="156", flow="EXPORT", start_year=2024, end_year=2025)
    )
    by_country = {s.metadata.country: s for s in result}
    assert not by_country["Reporter 842"].metadata.notes
    assert by_country["Reporter 124"].metadata.notes[0].startswith(
        "UN Comtrade returned no annual rows for 2025 from Reporter 124 "
    )


def test_reporters_sharing_a_gap_get_one_identical_consolidated_note(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    _install_upstream(monkeypatch, provider, {"2023", "2024"})
    result = asyncio.run(
        provider.fetch_trade_data(
            reporters=["842", "124", "276"], partner="156", flow="EXPORT", start_year=2023, end_year=2025
        )
    )
    notes = {tuple(s.metadata.notes or []) for s in result}
    assert len(result) == 3 and len(notes) == 1  # one identical note -> rendered once
    (note,) = notes.pop()
    for reporter in ("842", "124", "276"):
        assert f"Reporter {reporter}" in note


def test_failed_trailing_chunk_is_never_disclosed_as_unreported(monkeypatch):
    """A 429/timeout on the chunk holding the trailing periods is an outage,
    not evidence that the reporter has not filed (same class as BIS)."""
    provider = ComtradeProvider(api_key="demo")

    async def fake_single(client, reporter_raw, partner_code, commodity_code, flow_code, period_param, freq_code):
        periods = period_param.split(",")
        if any(p.startswith("2026") for p in periods):
            raise DataNotAvailableError("Failed after 3 retries: Rate limited (429)")
        return [NormalizedData(
            metadata=Metadata(source="UN Comtrade", indicator="Exports - Total Trade",
                              country="USA", frequency="monthly", unit="US Dollars"),
            data=[DataPoint(date=f"{p[:4]}-{p[4:6]}", value=1.0) for p in periods],
        )]

    monkeypatch.setattr(provider, "_fetch_single_reporter_data", fake_single)
    result = _fetch(provider, start_year=2024, end_year=2026, frequency="monthly")
    assert result[0].data[-1].date == "2025-12"
    notes = result[0].metadata.notes or []
    assert len(notes) == 1
    assert "not yet reported" not in notes[0]
    assert "sub-request in this fetch failed" in notes[0] and "may exist" in notes[0]
    assert "2026" in notes[0] and "latest period returned: 2025-12." in notes[0]


def test_quarterly_gap_note_uses_a_quarter_label(monkeypatch):
    provider = ComtradeProvider(api_key="demo")

    async def fake_single(client, reporter_raw, partner_code, commodity_code, flow_code, period_param, freq_code):
        rows = [p for p in period_param.split(",") if p.startswith("2024")]
        if not rows:
            return []
        return [NormalizedData(
            metadata=Metadata(source="UN Comtrade", indicator="Exports - Total Trade",
                              country="USA", frequency="quarterly", unit="US Dollars"),
            data=[DataPoint(date=comtrade_module._comtrade_period_to_iso(p), value=1.0) for p in rows],
        )]

    monkeypatch.setattr(provider, "_fetch_single_reporter_data", fake_single)
    result = _fetch(provider, start_year=2024, end_year=2025, frequency="quarterly")
    (note,) = result[0].metadata.notes
    assert "quarterly rows for 2025" in note
    assert note.endswith("latest period returned: 2024-Q4.")


def test_requested_year_with_no_rows_anywhere_still_raises_no_data(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    _install_upstream(monkeypatch, provider, set())
    with pytest.raises(DataNotAvailableError):
        _fetch(provider, start_year=2025, end_year=2025)


def test_monthly_current_year_is_unchanged(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    filed = {f"2026{m:02d}" for m in range(1, 9)}
    requested = _install_upstream(monkeypatch, provider, filed)
    result = _fetch(provider, start_year=2026, end_year=2026, frequency="monthly")
    assert {f"2026{m:02d}" for m in range(1, 13)} <= set(requested)
    assert result[0].data[-1].date == "2026-08"
    assert not result[0].metadata.notes  # 2026 has rows; month-level lag is visible, not a missing year


def test_quarterly_current_year_is_requested(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    requested = _install_upstream(monkeypatch, provider, {"20261", "20262"})
    _fetch(provider, start_year=2026, end_year=2027, frequency="quarterly")
    assert "20261" in requested
    assert not any(p.startswith("2027") for p in requested)
