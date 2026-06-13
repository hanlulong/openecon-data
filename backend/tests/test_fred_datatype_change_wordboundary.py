"""FRED dataType must not mislabel "exchange rate" / "unchanged" as a Change series.

The dataType heuristic matched the bare substring "change", which fires on
"exCHANGE rate" and "unCHANGEd" -- so all 400+ FRED foreign-exchange-rate series
were tagged dataType="Change". Word-boundary matching (\\bchanges?\\b) keeps
genuine change series ("Change in Private Inventories") while excluding the
embedded-substring false positives.
"""
from __future__ import annotations

import re

import pytest


def _data_type(title: str, unit: str = "") -> str:
    # Mirror the dataType branch in FREDProvider (backend/providers/fred.py).
    title_lower = title.lower()
    unit_lower = unit.lower()
    if "percent change" in title_lower or "growth rate" in title_lower:
        return "Percent Change"
    elif re.search(r"\bchanges?\b", title_lower):
        return "Change"
    elif re.search(r"\bindex(es)?\b", title_lower) or "index" in unit_lower:
        return "Index"
    elif ("rate" in title_lower or "yield" in title_lower) and "percent" in unit_lower:
        return "Rate"
    return "Level"


NOT_CHANGE = [
    "Brazilian Reals to U.S. Dollar Spot Exchange Rate",
    "Broad Effective Exchange Rate for China",
    "Austria / U.S. Foreign Exchange Rate (DISCONTINUED)",
    "3-Month Moving Average of Unweighted Unchanged Hourly Wage Growth: Overall",
]

GENUINE_CHANGE = [
    "Change in Private Inventories",
    "Real Change in Private Inventories",
    "Change in Nonfarm Payrolls",
    "Changes in Net Worth",
]


@pytest.mark.parametrize("title", NOT_CHANGE)
def test_exchange_and_unchanged_are_not_change(title):
    assert _data_type(title) != "Change", title


@pytest.mark.parametrize("title", GENUINE_CHANGE)
def test_genuine_change_series_stay_change(title):
    assert _data_type(title) == "Change", title


# Treasury / government-bond YIELDS are rates (titles say "Yield", unit is percent),
# and "inflation-INDEXed" must not be mistaken for an Index.
YIELD_RATES = [
    ("Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Quoted on an Investment Basis", "Percent"),  # DGS10
    ("Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Quoted on an Investment Basis, Inflation-Indexed", "Percent"),  # DFII10
    ("10-Year (Medium-Term) Government Bond Yields in the United Kingdom", "Percent per Annum"),
]

GENUINE_INDEX = [
    ("Producer Price Index by Commodity: All Commodities", "Index 1982=100"),
    ("S&P 500", "Index"),
    ("Consumer Price Index for All Urban Consumers", "Index 1982-1984=100"),
]


@pytest.mark.parametrize("title,unit", YIELD_RATES)
def test_yields_are_rate_not_index_or_level(title, unit):
    assert _data_type(title, unit) == "Rate", title


@pytest.mark.parametrize("title,unit", GENUINE_INDEX)
def test_genuine_indices_stay_index(title, unit):
    assert _data_type(title, unit) == "Index", title
