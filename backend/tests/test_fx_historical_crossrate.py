"""Historical FX via FRED must not relabel a USD leg as a cross rate.

Every FRED FX series is USD-based, so fetch_historical_exchange_from_fred can
only serve pairs with exactly one USD leg. The old code, when neither leg was
USD, returned a single USD leg (e.g. DEXUSUK = USD/GBP) stamped with the
requested cross-rate label ("EUR to GBP") — wrong data shown as success. It now
fails closed for cross rates.
"""

import asyncio

from backend.models import ParsedIntent
from backend.services.data_fetcher import fetch_historical_exchange_from_fred


class _FakeFred:
    async def fetch_series(self, params):
        meta = type("M", (), {"indicator": "", "source": ""})()
        return type("S", (), {"metadata": meta, "data": []})()


class _FakeSvc:
    fred_provider = _FakeFred()


def _intent(base, tgt):
    return ParsedIntent(
        apiProvider="ExchangeRate",
        indicators=["exchange rate"],
        parameters={},
        clarificationNeeded=False,
        originalQuery=f"{base} to {tgt} 2020 to 2022",
    )


def _run(base, tgt):
    params = {
        "baseCurrency": base,
        "targetCurrency": tgt,
        "startDate": "2020-01-01",
        "endDate": "2022-01-01",
    }
    return asyncio.run(
        fetch_historical_exchange_from_fred(_FakeSvc(), _intent(base, tgt), params)
    )


def test_cross_rate_fails_closed():
    assert _run("EUR", "GBP") is None
    assert _run("EUR", "JPY") is None


def test_usd_legs_still_served():
    assert _run("USD", "GBP") is not None
    assert _run("GBP", "USD") is not None
