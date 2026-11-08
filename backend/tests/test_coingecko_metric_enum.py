"""CoinGecko measure selection comes from the parse's ``metric`` enum, never
raw query text.

User-reported defect (169+ real queries): "return prices time series not
market caps" was served MARKET CAPS because a substring rule over the raw text
saw "market caps". The same rule could not see 市值/成交量 at all (≈90% of the
user base writes Chinese). The measure is semantic — it belongs to the parse
LLM's closed enum; the fetch layer only renames enum values to the provider's
vocabulary, and the cache contract keys on the SAME decision.
"""
from __future__ import annotations

import pytest

from backend.models import ParsedIntent
from backend.services.data_fetcher import (
    _coingecko_requested_metric,
    _provider_request_contract,
    fetch_from_coingecko,
)
from backend.tests.test_data_fetcher_coingecko import _FakeCoinGeckoProvider
from backend.tests.utils import run


def _intent(query: str, indicators, params: dict) -> ParsedIntent:
    return ParsedIntent(
        apiProvider="CoinGecko",
        indicators=list(indicators),
        parameters=dict(params),
        clarificationNeeded=False,
        originalQuery=query,
    )


# (query, LLM metric enum, LLM indicators, expected provider metric). The raw
# text deliberately contains the NEGATED measure word in several rows.
_NEGATED_AND_MULTILINGUAL = [
    ("return bitcoin prices not market caps", "price", ["price"], "price"),
    ("bitcoin market caps, not prices", "market_cap", ["market cap"], "market_cap"),
    ("ethereum trading volume not price", "volume", ["volume"], "volume"),
    ("比特币市值", "market_cap", ["market cap"], "market_cap"),
    ("以太坊成交量", "volume", ["trading volume"], "volume"),
    ("比特币价格，不要市值", "price", ["price"], "price"),
    ("bitcoin 24h change, not the price", "price_change", ["price change"], "24h_change"),
    # The measure lives ONLY in the enum (indicators carry just the coin):
    # the old English substring rule served PRICE for every one of these.
    ("比特币市值", "market_cap", ["bitcoin"], "market_cap"),
    ("以太坊成交量", "volume", ["ethereum"], "volume"),
    ("狗狗币24小时涨跌幅", "price_change", ["dogecoin"], "24h_change"),
]


@pytest.mark.parametrize("query,metric,indicators,expected", _NEGATED_AND_MULTILINGUAL)
def test_current_metric_follows_enum_not_raw_text(query, metric, indicators, expected) -> None:
    provider = _FakeCoinGeckoProvider()
    params = {"coinIds": ["bitcoin"], "vsCurrency": "usd", "metric": metric}
    run(fetch_from_coingecko(provider, _intent(query, indicators, params), params))
    assert provider.simple_calls == [{"coin_ids": ["bitcoin"], "vs_currency": "usd", "metric": expected}]


@pytest.mark.parametrize(
    "query,metric,indicators,expected",
    [row for row in _NEGATED_AND_MULTILINGUAL if row[1] != "price_change"],
)
def test_historical_metric_follows_enum_not_raw_text(query, metric, indicators, expected) -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["bitcoin"], "vsCurrency": "usd", "metric": metric,
        "startDate": "2026-01-01", "endDate": "2026-03-31",
    }
    run(fetch_from_coingecko(provider, _intent(query, indicators, params), params))
    calls = provider.range_calls + provider.history_calls
    assert [call["metric"] for call in calls] == [expected]


def test_indicator_label_fallback_is_exact_equality_never_substring() -> None:
    # No enum value (pre-schema intent): the LLM-normalized label decides...
    assert _coingecko_requested_metric(_intent("q", ["Market_Cap"], {}), {}) == "market_cap"
    assert _coingecko_requested_metric(_intent("q", ["prices"], {}), {}) == "price"
    # ...but only by exact normalized equality: a free-text label never matches.
    assert _coingecko_requested_metric(_intent("q", ["price not market cap"], {}), {}) == "price"
    assert _coingecko_requested_metric(_intent("q", ["bitcoin market cap"], {}), {}) == "price"
    # Provider-vocabulary spellings (CoinGecko field names / long forms) are
    # exact members too — still equality, never substring.
    assert _coingecko_requested_metric(_intent("q", ["market capitalization"], {}), {}) == "market_cap"
    assert _coingecko_requested_metric(_intent("q", ["total_volume"], {}), {}) == "volume"
    assert _coingecko_requested_metric(_intent("q", ["current price"], {}), {}) == "price"
    assert _coingecko_requested_metric(_intent("q", ["circulating market cap"], {}), {}) == "price"
    # Invalid enum values fall through to the labels, then to price.
    assert _coingecko_requested_metric(_intent("q", ["volume"], {}), {"metric": "tvl"}) == "volume"
    assert _coingecko_requested_metric(_intent("q", ["bitcoin"], {}), {"metric": None}) == "price"
    # Known limitation (documented): a multi-measure request serves the first
    # listed measure deterministically instead of "market cap always wins".
    assert _coingecko_requested_metric(_intent("q", ["price", "market cap"], {}), {}) == "price"


def test_raw_text_alone_never_selects_the_measure() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {"coinIds": ["bitcoin"], "vsCurrency": "usd"}
    run(fetch_from_coingecko(provider, _intent("bitcoin market cap and volume", ["bitcoin"], params), params))
    assert provider.simple_calls[0]["metric"] == "price"


@pytest.mark.parametrize(
    "query,metric,order",
    [
        ("top 10 coins by market cap", "market_cap", "market_cap_desc"),
        ("top 10 coins by 24h volume", "volume", "volume_desc"),
        # Chinese MEASURE (via the enum) with the English "top 10" trigger.
        ("市值前十的加密货币 top 10", "market_cap", "market_cap_desc"),
    ],
)
def test_ranking_uses_enum_measure_with_native_order(query, metric, order) -> None:
    provider = _FakeCoinGeckoProvider()
    params = {"vsCurrency": "usd", "metric": metric}
    run(fetch_from_coingecko(provider, _intent(query, ["cryptocurrency ranking"], params), params))
    assert provider.market_calls == [{"vs_currency": "usd", "order": order, "per_page": 10}]
    assert provider.simple_calls == []


def test_chinese_only_ranking_cue_is_a_known_limitation() -> None:
    """KNOWN LIMITATION (pinned, deferred): the ranking TRIGGER is still the
    pre-existing English cue list, so "市值前十" alone does not rank even though
    the enum carries the measure. Moving the trigger into the parse schema is
    a separate item; this pin keeps the gap visible."""
    provider = _FakeCoinGeckoProvider()
    params = {"vsCurrency": "usd", "metric": "market_cap"}
    run(fetch_from_coingecko(provider, _intent("市值前十的加密货币", ["cryptocurrency"], params), params))
    assert provider.market_calls == []


def test_eurostat_contract_never_turns_the_crypto_metric_into_a_dimension_filter() -> None:
    intent = ParsedIntent(apiProvider="Eurostat", indicators=["house prices"],
                          parameters={}, clarificationNeeded=False, originalQuery="euro area house prices")
    contract = _provider_request_contract(
        "EUROSTAT", intent, {"indicator": "prc_hpi_q", "metric": "price", "unit": "I15_Q"},
    )
    assert contract["filters"] == {"unit": "I15_Q"}


def test_historical_price_change_falls_back_to_price_with_disclosure() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["bitcoin"], "vsCurrency": "usd", "metric": "price_change",
        "startDate": "2026-01-01", "endDate": "2026-03-31",
    }
    result = run(fetch_from_coingecko(provider, _intent("bitcoin change history", ["price change"], params), params))
    assert [c["metric"] for c in provider.range_calls + provider.history_calls] == ["price"]
    assert any("no historical price-change" in note for note in result[0].metadata.notes)


def test_cache_contract_always_carries_the_served_metric() -> None:
    base = {"coinIds": ["bitcoin"], "vsCurrency": "usd", "startDate": "2026-01-01", "endDate": "2026-03-31"}
    # Same indicators[0] ("price") — the poisoned-cache case: these MUST differ.
    price = _provider_request_contract("COINGECKO", _intent("q", ["price"], base), {**base, "metric": "price"})
    mcap = _provider_request_contract("COINGECKO", _intent("q", ["price"], base), {**base, "metric": "market_cap"})
    default = _provider_request_contract("COINGECKO", _intent("q", ["price"], base), dict(base))

    assert price["metric"] == "price" and default["metric"] == "price"
    assert mcap["metric"] == "market_cap"
    assert price != mcap


def test_exact_user_phrasing_prices_time_series_not_market_caps_serves_prices() -> None:
    """Verbatim real-user complaint shape: a historical cue plus a NEGATED
    measure word. The LLM's enum says price; raw "market caps" is ignored."""
    provider = _FakeCoinGeckoProvider()
    params = {"coinIds": ["bitcoin"], "vsCurrency": "usd", "metric": "price"}
    query = "bitcoin last 90 days, return prices time series not market caps"
    run(fetch_from_coingecko(provider, _intent(query, ["price"], params), params))
    assert [c["metric"] for c in provider.range_calls + provider.history_calls] == ["price"]
    assert provider.history_calls[0]["days"] == 90


# ---------------------------------------------------------------------------
# CoinGecko disclosures are localized, and the cache keys on their language
# ---------------------------------------------------------------------------

def _zh(intent: ParsedIntent) -> ParsedIntent:
    intent.language = "zh"
    return intent


def test_historical_price_change_disclosure_is_localized() -> None:
    provider = _FakeCoinGeckoProvider()
    params = {
        "coinIds": ["bitcoin"], "vsCurrency": "usd", "metric": "price_change",
        "startDate": "2026-01-01", "endDate": "2026-03-31",
    }
    intent = _zh(_intent("比特币过去三个月的价格变化", ["bitcoin"], params))
    result = run(fetch_from_coingecko(provider, intent, params))
    notes = result[0].metadata.notes or []
    assert any("不提供历史价格涨跌幅" in note for note in notes)
    assert not any("CoinGecko publishes" in note for note in notes)


@pytest.mark.parametrize("language,marker", [(None, "entries per ranking"), ("zh", "每个排行榜最多返回 250 条")])
def test_ranking_page_cap_disclosure_is_localized(language, marker) -> None:
    provider = _FakeCoinGeckoProvider()
    params = {"vsCurrency": "usd", "metric": "market_cap"}
    intent = _intent("top 300 coins by market cap", ["cryptocurrency ranking"], params)
    intent.language = language
    result = run(fetch_from_coingecko(provider, intent, params))
    assert provider.market_calls[0]["per_page"] == 250
    notes = result[0].metadata.notes or []
    assert sum(marker in note for note in notes) == 1
    assert any("300" in note for note in notes)


def test_cache_contract_keys_on_the_notes_rendering_language() -> None:
    base = {"coinIds": ["bitcoin"], "vsCurrency": "usd", "metric": "price_change", "days": 30}
    en = _provider_request_contract("COINGECKO", _intent("q", ["bitcoin"], base), dict(base))
    zh = _provider_request_contract("COINGECKO", _zh(_intent("q", ["bitcoin"], base)), dict(base))
    zh_cn = _intent("q", ["bitcoin"], base)
    zh_cn.language = "zh-CN"
    zh_cn = _provider_request_contract("COINGECKO", zh_cn, dict(base))
    # An uncatalogued language renders English notes, so it shares the key.
    other = _intent("q", ["bitcoin"], base)
    other.language = "xx"
    other = _provider_request_contract("COINGECKO", other, dict(base))
    assert "notes_language" not in en, "English keys stay byte-identical"
    assert zh["notes_language"] == "zh" and zh != en
    assert zh_cn == zh
    assert other == en
