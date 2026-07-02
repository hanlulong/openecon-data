"""Eurostat must enforce the requested end year.

The SDMX dissemination endpoint ignores since/untilTimePeriod for several
datasets, so "France GDP 2010-2015" returned 2010→latest. Parsed points are
now trimmed to [start_year, end_year] in code.
"""

from backend.providers.eurostat import EurostatProvider


def _provider():
    return EurostatProvider.__new__(EurostatProvider)


def _points():
    pts = [{"date": str(y), "value": float(y)} for y in range(1975, 2026)]
    pts.append({"date": "2012-Q3", "value": 1.0})  # sub-annual token
    return pts


def test_trim_both_bounds():
    r = _provider()._trim_points_to_year_range(_points(), 2010, 2015)
    years = {int(str(p["date"])[:4]) for p in r}
    assert min(years) == 2010 and max(years) == 2015


def test_trim_end_only():
    r = _provider()._trim_points_to_year_range(_points(), None, 2015)
    assert max(int(str(p["date"])[:4]) for p in r) == 2015


def test_trim_start_only():
    r = _provider()._trim_points_to_year_range(_points(), 2020, None)
    assert min(int(str(p["date"])[:4]) for p in r) == 2020


def test_unparseable_date_kept():
    r = _provider()._trim_points_to_year_range([{"date": "N/A", "value": 1}], 2010, 2015)
    assert len(r) == 1


def test_string_years_accepted():
    r = _provider()._trim_points_to_year_range(_points(), "2010", "2011")
    assert {int(str(p["date"])[:4]) for p in r} == {2010, 2011}
