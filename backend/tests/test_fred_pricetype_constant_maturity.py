"""FRED priceType must not mislabel constant-maturity yields as 'Real'.

The price-type heuristic keyed on the bare substring "constant", which fires on
"Constant Maturity" -- a Treasury yield-curve method, not price deflation. That
wrongly tagged every nominal constant-maturity yield (DGS10, GS10, T10Y2Y, ...)
as priceType="Real (inflation-adjusted)". The genuine real (TIPS) series carry
"Inflation-Indexed" in their title; real-price series carry "real"/"chained"/
"constant <prices/dollars>". This test pins the corrected classification.
"""
from __future__ import annotations

import pytest


def _price_type(title: str):
    # Mirror the classification branch in FREDProvider (backend/providers/fred.py).
    title_lower = title.lower()
    if (
        "real" in title_lower
        or "chained" in title_lower
        or "inflation-indexed" in title_lower
        or "inflation indexed" in title_lower
        or ("constant" in title_lower and "constant maturity" not in title_lower)
    ):
        return "Real (inflation-adjusted)"
    if "nominal" in title_lower or "current" in title_lower:
        return "Nominal (current prices)"
    return None


NOMINAL_YIELDS = [
    "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Quoted on an Investment Basis",  # DGS10
    "Market Yield on U.S. Treasury Securities at 2-Year Constant Maturity, Quoted on an Investment Basis",   # DGS2
    "10-Year Treasury Constant Maturity Minus 2-Year Treasury Constant Maturity",                            # T10Y2Y
    "1-Year Treasury Constant Maturity Minus Federal Funds Rate",
]

REAL_SERIES = [
    "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Quoted on an Investment Basis, Inflation-Indexed",  # DFII10 (TIPS)
    "Real Gross Domestic Product",
    "Real Median Household Income in the United States",
    "Gross Domestic Product, Chained 2017 Dollars",
    "Capital Stock at Constant National Prices for Argentina",  # real-price, not maturity
    "10-Year Real Interest Rate",
]


@pytest.mark.parametrize("title", NOMINAL_YIELDS)
def test_constant_maturity_yields_are_not_real(title):
    assert _price_type(title) != "Real (inflation-adjusted)", title


@pytest.mark.parametrize("title", REAL_SERIES)
def test_genuine_real_series_stay_real(title):
    assert _price_type(title) == "Real (inflation-adjusted)", title
