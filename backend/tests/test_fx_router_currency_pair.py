"""_is_exchange_rate_query must require a currency PAIR, not a bare currency word.

It matched bare single-currency substrings ("usd to", "to eur", ...) which fire
inside macro phrasing ("remittances in usd to gdp ratio", "exported to eurozone",
"exports to usd"). Because the FX route is a structural FINAL-AUTHORITY route, it
silently overrode the correct provider. The fix requires both sides of the pair to
be a currency token (ISO-4217 code or currency name).
"""
from __future__ import annotations

import pytest

from backend.routing.unified_router import UnifiedRouter


def _is_fx(query: str) -> bool:
    return UnifiedRouter()._is_exchange_rate_query(query.lower(), [])


# Macro queries containing a bare currency word must NOT route to FX.
@pytest.mark.parametrize("query", [
    "remittances received in usd to gdp ratio for India",
    "oil exported to eurozone",
    "US exports to USD",
    "exports to usd",
    "foreign reserves in usd",
    "GDP in us dollars",
    "real GDP",
    "US GDP",
])
def test_macro_queries_not_fx(query):
    assert _is_fx(query) is False


# Genuine currency-pair queries must route to FX.
@pytest.mark.parametrize("query", [
    "USD to EUR",
    "euro to USD",
    "convert 100 usd to jpy",
    "eur/usd rate",
    "gbp vs usd",
    "dollar to euro",
    "exchange rate",
    "USD/JPY",
    "Canadian dollar to US dollar",
    "yen to dollar",
    "swiss franc to euro",
    "convert us dollars to euros",
])
def test_currency_pairs_are_fx(query):
    assert _is_fx(query) is True
