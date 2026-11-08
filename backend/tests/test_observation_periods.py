"""Calendar-structural freshness: is a series' latest period current?"""
from __future__ import annotations

from datetime import date

import pytest

from backend.services import data_fetcher as DF
from backend.utils.observation_periods import (
    LATEST_PERIOD_AGE_BUDGET_DAYS,
    frequency_code,
    latest_period_is_stale,
    observation_period,
)

AS_OF = date(2026, 11, 8)


@pytest.mark.parametrize(
    "latest,frequency,stale",
    [
        # Monthly: last month / two months back are current, five months not.
        ("2026-10-01", "monthly", False),
        ("2026-08", "Monthly", False),
        ("2026-06-01", "M", True),
        # Start- and end-of-period stamps of the same month agree.
        ("2026-07-01", "monthly", True), ("2026-07-31", "monthly", True),
        # Quarterly with a ~6-month publication lag (BIS total credit).
        ("2026-01-01", "quarterly", False),
        ("2025-10-01", "Quarterly, End of Period", True),
        # Annual: the previous year is current, two years back is not.
        ("2025", "annual", False), ("2024-01-01", "A", True),
        # Daily.
        ("2026-11-01", "daily", False), ("2026-10-01", "D", True),
        # A discontinued pre-euro national policy rate.
        ("1998-12-01", "monthly", True),
    ],
)
def test_latest_period_staleness_by_frequency(latest, frequency, stale):
    assert latest_period_is_stale(latest, frequency, as_of=AS_OF) is stale


@pytest.mark.parametrize("latest,frequency", [(None, "monthly"), ("", "monthly"), ("n/a", "monthly"),
                                              ("2001-01-01", None), ("2001-01-01", "irregular")])
def test_absence_of_evidence_is_never_staleness(latest, frequency):
    assert latest_period_is_stale(latest, frequency, as_of=AS_OF) is False


def test_past_window_end_is_the_reference():
    assert latest_period_is_stale("2024-12-01", "monthly", as_of=date(2024, 12, 31)) is False
    assert latest_period_is_stale("1998-12-01", "monthly", as_of=date(2024, 12, 31)) is True


def test_budget_table_covers_every_period_code():
    assert set(LATEST_PERIOD_AGE_BUDGET_DAYS) == {"D", "W", "M", "Q", "S", "A"}


def test_data_fetcher_keeps_using_the_shared_period_math():
    assert DF._frequency_code is frequency_code  # pylint: disable=protected-access
    assert DF._observation_period is observation_period  # pylint: disable=protected-access
    assert observation_period(date(2026, 5, 15), "Q") == (date(2026, 4, 1), date(2026, 6, 30))
