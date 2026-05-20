from __future__ import annotations

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.data_fetcher import fetch_from_coingecko
from backend.services.time_range_defaults import apply_default_time_range
from backend.tests.utils import run


def _series(series_id: str = "genomesdao") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source="CoinGecko",
            indicator=series_id,
            frequency="daily",
            unit="USD",
            seriesId=series_id,
        ),
        data=[{"date": "2026-04-26", "value": 1.0}],
    )


class _FakeCoinGeckoProvider:
    def __init__(self) -> None:
        self.simple_calls: list[dict] = []
        self.range_calls: list[dict] = []
        self.history_calls: list[dict] = []
        self.market_calls: list[dict] = []

    async def get_simple_price(self, **kwargs):
        self.simple_calls.append(kwargs)
        return [_series(kwargs.get("coin_ids", ["unknown"])[0])]

    async def get_historical_data_range(self, **kwargs):
        self.range_calls.append(kwargs)
        return [_series(kwargs.get("coin_id", "unknown"))]

    async def get_historical_data(self, **kwargs):
        self.history_calls.append(kwargs)
        return [_series(kwargs.get("coin_id", "unknown"))]

    async def get_market_data(self, **kwargs):
        self.market_calls.append(kwargs)
        return [_series("market")]


def test_apply_default_time_range_marks_coingecko_default_window() -> None:
    params = apply_default_time_range("COINGECKO", {})

    assert params["startDate"]
    assert params["endDate"]
    assert params["__default_time_range_applied"] == "coingecko_30d"


def test_fetch_from_coingecko_uses_simple_price_when_only_default_window_was_applied() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["genomesdao"],
        "vsCurrency": "usd",
        "startDate": "2026-03-27",
        "endDate": "2026-04-26",
        "__default_time_range_applied": "coingecko_30d",
    }
    intent = ParsedIntent(
        apiProvider="CoinGecko",
        indicators=["GenomesDAO GENE"],
        parameters=params,
        clarificationNeeded=False,
        originalQuery="GenomesDAO GENE cryptocurrency price from CoinGecko",
    )

    result = run(fetch_from_coingecko(provider, intent, params))

    assert len(result) == 1
    assert provider.simple_calls == [
        {"coin_ids": ["genomesdao"], "vs_currency": "usd", "metric": "price"}
    ]
    assert provider.range_calls == []
    assert provider.history_calls == []


def test_fetch_from_coingecko_keeps_default_window_for_historical_query() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["archimedes"],
        "vsCurrency": "usd",
        "startDate": "2026-03-27",
        "endDate": "2026-04-26",
        "__default_time_range_applied": "coingecko_30d",
    }
    intent = ParsedIntent(
        apiProvider="CoinGecko",
        indicators=["Archimedes Finance"],
        parameters=params,
        clarificationNeeded=False,
        originalQuery="Archimedes Finance cryptocurrency price history from CoinGecko",
    )

    result = run(fetch_from_coingecko(provider, intent, params))

    assert len(result) == 1
    assert provider.simple_calls == []
    assert provider.range_calls == [
        {
            "coin_id": "archimedes",
            "vs_currency": "usd",
            "from_date": "2026-03-27",
            "to_date": "2026-04-26",
            "metric": "price",
        }
    ]


def test_fetch_from_coingecko_respects_explicit_date_params_without_default_marker() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["genomesdao"],
        "vsCurrency": "usd",
        "startDate": "2026-01-01",
        "endDate": "2026-04-26",
    }
    intent = ParsedIntent(
        apiProvider="CoinGecko",
        indicators=["GenomesDAO GENE"],
        parameters=params,
        clarificationNeeded=False,
        originalQuery="GenomesDAO GENE cryptocurrency price from CoinGecko",
    )

    result = run(fetch_from_coingecko(provider, intent, params))

    assert len(result) == 1
    assert provider.simple_calls == []
    assert provider.range_calls == [
        {
            "coin_id": "genomesdao",
            "vs_currency": "usd",
            "from_date": "2026-01-01",
            "to_date": "2026-04-26",
            "metric": "price",
        }
    ]


def test_fetch_from_coingecko_treats_snapshot_cues_as_current_price() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["bitcoin"],
        "vsCurrency": "right",
        "startDate": "2026-03-27",
        "endDate": "2026-04-26",
        "__default_time_range_applied": "coingecko_30d",
    }
    intent = ParsedIntent(
        apiProvider="CoinGecko",
        indicators=["bitcoin market cap"],
        parameters=params,
        clarificationNeeded=False,
        originalQuery="bitcoin market cap right now",
    )

    run(fetch_from_coingecko(provider, intent, params))

    assert provider.simple_calls == [
        {"coin_ids": ["bitcoin"], "vs_currency": "usd", "metric": "market_cap"}
    ]
    assert provider.range_calls == []


def test_fetch_from_coingecko_treats_year_like_asset_tokens_as_current_price() -> None:
    for query, coin_id in [
        ("1984-token cryptocurrency price from CoinGecko", "1984-token"),
        ("2004 PEPE cryptocurrency price from CoinGecko", "2004-pepe"),
        ("2025 TOKEN cryptocurrency price from CoinGecko", "2025-token"),
    ]:
        provider = _FakeCoinGeckoProvider()
        params = {
            "coinIds": [coin_id],
            "vsCurrency": "usd",
            "startDate": "2026-03-27",
            "endDate": "2026-04-26",
            "__default_time_range_applied": "coingecko_30d",
        }
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=[coin_id],
            parameters=params,
            clarificationNeeded=False,
            originalQuery=query,
        )

        run(fetch_from_coingecko(provider, intent, params))

        assert provider.simple_calls == [
            {"coin_ids": [coin_id], "vs_currency": "usd", "metric": "price"}
        ]
        assert provider.range_calls == []
        assert provider.history_calls == []


def test_fetch_from_coingecko_keeps_explicit_year_scope_historical() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["bitcoin"],
        "vsCurrency": "usd",
        "startDate": "2026-03-27",
        "endDate": "2026-04-26",
        "__default_time_range_applied": "coingecko_30d",
    }
    intent = ParsedIntent(
        apiProvider="CoinGecko",
        indicators=["bitcoin"],
        parameters=params,
        clarificationNeeded=False,
        originalQuery="Bitcoin price in 2024 from CoinGecko",
    )

    run(fetch_from_coingecko(provider, intent, params))

    assert provider.simple_calls == []
    assert provider.range_calls == [
        {
            "coin_id": "bitcoin",
            "vs_currency": "usd",
            "from_date": "2026-03-27",
            "to_date": "2026-04-26",
            "metric": "price",
        }
    ]


def test_fetch_from_coingecko_does_not_treat_asset_title_change_as_metric() -> None:
    for query, coin_id in [
        ("1-dog-can-change-your-life cryptocurrency price from CoinGecko", "1-dog-can-change-your-life"),
        ("1-year-can-change-your-life cryptocurrency price from CoinGecko", "1-year-can-change-your-life"),
    ]:
        provider = _FakeCoinGeckoProvider()
        params = {"coinIds": [coin_id], "vsCurrency": "usd"}
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=[coin_id],
            parameters=params,
            clarificationNeeded=False,
            originalQuery=query,
        )

        run(fetch_from_coingecko(provider, intent, params))

        assert provider.simple_calls == [
            {"coin_ids": [coin_id], "vs_currency": "usd", "metric": "price"}
        ]


def test_fetch_from_coingecko_preserves_explicit_change_metric() -> None:
    for query in [
        "Bitcoin 24h change from CoinGecko",
        "Bitcoin 24-hour change from CoinGecko",
        "Bitcoin price change from CoinGecko",
    ]:
        provider = _FakeCoinGeckoProvider()
        params = {"coinIds": ["bitcoin"], "vsCurrency": "usd"}
        intent = ParsedIntent(
            apiProvider="CoinGecko",
            indicators=["bitcoin"],
            parameters=params,
            clarificationNeeded=False,
            originalQuery=query,
        )

        run(fetch_from_coingecko(provider, intent, params))

        assert provider.simple_calls == [
            {"coin_ids": ["bitcoin"], "vs_currency": "usd", "metric": "24h_change"}
        ]
