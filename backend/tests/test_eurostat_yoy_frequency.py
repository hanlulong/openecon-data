"""Eurostat year-over-year must lag by a full year, not one array element.

_calculate_year_over_year_change diffed adjacent points regardless of
frequency, so a monthly index produced a month-over-month change and a
quarterly index a quarter-over-quarter change, both mislabeled year-over-year.
The lag now spans a year for the series' frequency (12 / 4 / 1).
"""

import pytest

from backend.providers.eurostat import EurostatProvider


@pytest.fixture
def provider():
    return EurostatProvider.__new__(EurostatProvider)


def test_monthly_uses_12_month_lag(provider):
    # Index compounding 1% per month; true YoY over 12 months ~= 12.68%.
    data = [{"date": f"m{i}", "value": 100 * (1.01 ** i)} for i in range(13)]
    result = provider._calculate_year_over_year_change(data, "monthly")
    assert len(result) == 1
    assert abs(result[0]["value"] - 12.68) < 0.05  # YoY, not the 1% MoM


def test_quarterly_uses_4_quarter_lag(provider):
    data = [{"date": f"q{i}", "value": 100 + i} for i in range(8)]
    result = provider._calculate_year_over_year_change(data, "quarterly")
    assert len(result) == 4
    assert result[0]["value"] == 4.0  # (104-100)/100, i.e. 4 quarters back


def test_annual_uses_1_year_lag(provider):
    data = [{"date": "2020", "value": 100}, {"date": "2021", "value": 105}]
    result = provider._calculate_year_over_year_change(data, "annual")
    assert result[0]["value"] == 5.0


def test_insufficient_history_returns_empty(provider):
    data = [{"date": f"m{i}", "value": 100 + i} for i in range(5)]
    assert provider._calculate_year_over_year_change(data, "monthly") == []
