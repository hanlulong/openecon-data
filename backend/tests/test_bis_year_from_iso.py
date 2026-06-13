"""BIS year-range filter must not crash on dashless quarter/month periods.

bis.py derived the filter year via int(time_period.split("-")[0]), which raised
ValueError on "2016Q1"/"2016M03" (BIS forces quarterly for credit/DSR/property/
GLI series) — silently dropping a whole country or failing the GLI fetch. The year
now comes from the already-parsed ISO date.
"""
import pytest
from backend.providers.bis import _bis_year_from_iso
from backend.providers._sdmx import period_to_iso_date


@pytest.mark.parametrize("iso,year", [
    ("2016-01-01", 2016), ("2016-10-01", 2016), ("2004-04-01", 2004),
    ("", None), (None, None), ("latest", None),
])
def test_bis_year_from_iso(iso, year):
    assert _bis_year_from_iso(iso) == year


@pytest.mark.parametrize("period", ["2016Q1", "2016Q4", "2016M03", "2016", "2016-01"])
def test_dashless_periods_do_not_crash(period):
    # The end-to-end path: parse the raw period to ISO, then take the year — never
    # int() the raw token (which crashed on Q/M forms).
    iso = period_to_iso_date(period)
    y = _bis_year_from_iso(iso)
    assert y is None or isinstance(y, int)
