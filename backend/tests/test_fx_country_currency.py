"""'<country> exchange rate' must resolve to that country's currency.

extract_exchange_rate_params previously only scanned the query TEXT for currency
codes/names, so "Brazil exchange rate" (no currency token) fell through to the
USD->EUR default and returned the euro rate. It now derives the target currency
from the parsed country. The upstream override emits either an English name or
an ISO-3166 alpha-2 code, so both forms must work.
"""

import pytest

from backend.models import ParsedIntent
from backend.services.data_fetcher import extract_exchange_rate_params


def _extract(query, country=None):
    intent = ParsedIntent(
        apiProvider="ExchangeRate",
        indicators=["exchange rate"],
        parameters={},
        clarificationNeeded=False,
        originalQuery=query,
    )
    params = {}
    if country:
        params["country"] = country
    return extract_exchange_rate_params(params, intent)


@pytest.mark.parametrize(
    "query,country,expected_target",
    [
        ("Brazil exchange rate", "Brazil", "BRL"),
        ("Brazil exchange rate", "BR", "BRL"),   # ISO-2 form
        ("Japan exchange rate", "JP", "JPY"),
        ("India exchange rate", "India", "INR"),
        ("exchange rate for Germany", "DE", "EUR"),
        ("China exchange rate", "CN", "CNY"),
    ],
)
def test_country_derives_target_currency(query, country, expected_target):
    params = _extract(query, country)
    assert params["baseCurrency"] == "USD"
    assert params["targetCurrency"] == expected_target


def test_us_country_keeps_usd_eur_default():
    # A USD country must not become USD->USD; the USD->EUR default stands.
    params = _extract("US exchange rate", "US")
    assert params["baseCurrency"] == "USD"
    assert params["targetCurrency"] == "EUR"


def test_explicit_pair_overrides_country():
    # An explicit "X to Y" in the text wins over the country derivation.
    params = _extract("GBP to JPY", "US")
    assert params["baseCurrency"] == "GBP"
    assert params["targetCurrency"] == "JPY"


def test_no_country_no_pair_uses_default():
    params = _extract("exchange rate", None)
    assert params["baseCurrency"] == "USD"
    assert params["targetCurrency"] == "EUR"
