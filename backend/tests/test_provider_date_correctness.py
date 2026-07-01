"""Provider date/period conversions must be UTC-correct and frequency-aware.

- CoinGecko epoch<->date used naive local time (host is UTC-4), shifting every
  historical point by a day.
- Comtrade formatted every period as "{period}-01-01", producing malformed
  dates like "202301-01-01" for monthly/quarterly data.
"""

import pytest

from backend.providers.coingecko import _epoch_ms_to_iso_utc, _date_to_epoch_utc
from backend.providers.comtrade import _comtrade_period_to_iso


def test_coingecko_epoch_is_utc_no_day_shift():
    # UTC midnight 2024-06-14; naive local conversion would render 2024-06-13.
    ms = 1718323200000
    iso = _epoch_ms_to_iso_utc(ms)
    assert iso.startswith("2024-06-14T00:00:00")
    assert iso.endswith("+00:00")


def test_coingecko_date_epoch_roundtrip():
    ms = 1718323200000
    assert _date_to_epoch_utc("2024-06-14") == ms // 1000
    # A tz-aware input must not be double-shifted.
    assert _date_to_epoch_utc("2024-06-14T00:00:00+00:00") == ms // 1000


@pytest.mark.parametrize(
    "period,expected",
    [
        ("2023", "2023-01-01"),      # annual
        ("202301", "2023-01-01"),    # monthly YYYYMM
        ("202312", "2023-12-01"),
        ("20231", "2023-01-01"),     # quarterly YYYYQ
        ("20232", "2023-04-01"),
        ("20234", "2023-10-01"),
        ("2023Q3", "2023-07-01"),    # explicit quarter
        ("2023-06", "2023-06-01"),   # already ISO-ish
    ],
)
def test_comtrade_period_to_iso(period, expected):
    assert _comtrade_period_to_iso(period) == expected


def test_comtrade_monthly_not_malformed():
    # The exact regression: "202301" must not become "202301-01-01".
    assert _comtrade_period_to_iso("202301") == "2023-01-01"
    assert "202301-01-01" != _comtrade_period_to_iso("202301")
