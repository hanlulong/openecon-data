"""StatsCan rate-series unit normalization.

StatsCan rate tables (unemployment rate, participation rate) carry scalar
factor 0, which renders as the meaningless 'units' placeholder even though the
series is a percentage. The normalizer rewrites ONLY that placeholder for
rate-named series — real units and non-rate series are untouched.
"""
from __future__ import annotations

from backend.providers.statscan import StatsCanProvider


def _p() -> StatsCanProvider:
    return StatsCanProvider.__new__(StatsCanProvider)


def test_rate_placeholder_becomes_percent():
    p = _p()
    assert p._unit_with_rate_awareness(0, "Employment and unemployment rate") == "percent"
    assert p._unit_with_rate_awareness(0, "Labour force participation rate") == "percent"
    assert p._unit_with_rate_awareness(0, "Some indicator (percent)") == "percent"


def test_non_rate_placeholder_stays_units():
    p = _p()
    assert p._unit_with_rate_awareness(0, "Gross domestic product") == "units"
    assert p._unit_with_rate_awareness(0, "Number of employees") == "units"


def test_real_scalar_unit_is_never_overridden():
    # A 'rate'-named series that actually carries a real scalar factor (e.g.
    # an exchange rate reported in millions) must keep its real unit — the
    # rewrite only ever touches the bare 'units' placeholder.
    p = _p()
    real = p._unit_with_rate_awareness(6, "exchange rate")
    assert real != "percent"
    assert real == p._map_scalar_factor(6)


def test_missing_indicator_name_is_safe():
    p = _p()
    assert p._unit_with_rate_awareness(0, None) == "units"
    assert p._unit_with_rate_awareness(0, "") == "units"
