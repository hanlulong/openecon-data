"""FX queries mixing a currency NAME with a 3-letter CODE must pair correctly.

extract_exchange_rate_params' single-code branch defaulted the second operand to
EUR/USD and, because both slots were then truthy, skipped the currency-NAME map.
So "euro to USD" / "canadian dollar to USD" silently returned USD->EUR. The fix
resolves a differently-named currency in the query and pairs it with the code in
query order before falling back to the default.
"""
from __future__ import annotations

import pytest

from backend.models import ParsedIntent
from backend.services.data_fetcher import extract_exchange_rate_params


def _pair(query: str) -> str:
    intent = ParsedIntent(
        apiProvider="EXCHANGERATE", indicators=[query], parameters={},
        clarificationNeeded=False, originalQuery=query,
    )
    p = extract_exchange_rate_params({}, intent)
    return f"{p.get('baseCurrency')}->{p.get('targetCurrency')}"


# The fix: NAME -> CODE now keeps the named currency.
@pytest.mark.parametrize("query,expected", [
    ("euro to USD", "EUR->USD"),
    ("canadian dollar to USD", "CAD->USD"),
    ("swiss franc to USD", "CHF->USD"),
    ("british pound to USD", "GBP->USD"),
])
def test_name_to_code_keeps_named_currency(query, expected):
    assert _pair(query) == expected


# Zero regression: code-first, both-code, both-name, and bare cases unchanged.
@pytest.mark.parametrize("query,expected", [
    ("USD to euro", "USD->EUR"),
    ("USD to JPY", "USD->JPY"),
    ("EUR to USD", "EUR->USD"),
    ("euro to yen", "EUR->JPY"),
    ("dollar to euro", "USD->EUR"),
    ("Canadian dollar to US dollar", "CAD->USD"),
    ("CAD to USD", "CAD->USD"),
    ("USD", "USD->EUR"),
    ("euro", "EUR->USD"),
])
def test_existing_fx_pairs_unchanged(query, expected):
    assert _pair(query) == expected
