"""Eurostat _should_calculate_rate must not transform LEVEL series into a YoY %.

A bare "change" substring matched "exCHANGE rate" and "CHANGES in inventories"
(real published level series), so the YoY-transform path silently replaced their
values and overwrote unit='percent'. The match now requires word-boundaried,
qualified growth/change-rate phrasing.
"""
from __future__ import annotations

import pytest

from backend.providers.eurostat import EurostatProvider


def _p() -> EurostatProvider:
    return EurostatProvider.__new__(EurostatProvider)


# Level series whose title contains "change"/"exchange" must NOT be transformed.
@pytest.mark.parametrize("indicator", [
    "Changes in inventories",
    "Exchange rate",
    "exchange rate index",
    "Foreign exchange reserves",
    "Unemployment rate",
])
def test_level_series_not_rate_transformed(indicator):
    assert _p()._should_calculate_rate(indicator, "") is False


# Genuine growth/percent-change requests still transform.
@pytest.mark.parametrize("indicator", [
    "Real GDP growth",
    "GDP growth rate",
    "percent change in GDP",
    "% change in prices",
    "percentage change",
    "year-over-year change",
    "GDP yoy",
])
def test_growth_requests_still_transform(indicator):
    assert _p()._should_calculate_rate(indicator, "") is True


def test_query_side_growth_intent_also_triggers():
    p = _p()
    assert p._should_calculate_rate("GDP index", "GDP growth") is True
    assert p._should_calculate_rate("GDP index", "GDP") is False
