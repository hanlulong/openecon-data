"""A CoinGecko MEASURE switch on a follow-up is a first-class delta slot.

Production regression (rc-20261108): "bitcoin market cap" -> "比特币价格呢"
re-served the market cap. The release persisted the parse's measure enum in
``ConversationState.crypto_metric``, but the follow-up delta had no slot for a
measure change, so the LLM filed the turn as ``language_change`` and the
stale persisted measure won. Mechanism under test:

* ``FollowUpDelta.changed_metric`` — LLM-filled, validated structurally against
  the fetch layer's ONE measure enum (invalid values dropped, never raised);
* ``merge_state`` — a measure switch keeps asset + provider, an ASSET switch
  keeps the measure, a non-asset indicator / provider change clears it;
* ``extract_with_llm`` — the slot counts as a change, the prompt shows the
  served measure, and the slot is CoinGecko-scoped;
* the structural Tier-1 crypto handler yields any utterance with residual
  content ("ethereum price", "ETH 成交量") to the LLM tier;
* the delta path dispatches the switched measure, and the provider_request
  cache contract differs from the previous turn's.

Assertions are on the dispatched MEASURE (intent satisfaction), never on a
pinned series id.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ParsedIntent, QueryResponse
from backend.services import query as query_module
from backend.services.cache import cache_service
from backend.services.conversation import conversation_manager
from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    materialize_intent,
    merge_state,
)
from backend.services.data_fetcher import (
    _COINGECKO_METRIC_ENUM,
    _provider_request_contract,
    fetch_from_coingecko,
)
from backend.services.delta_extractor import DeltaExtractor
from backend.services.query import QueryService
from backend.tests.test_data_fetcher_coingecko import _FakeCoinGeckoProvider
from backend.tests.utils import run


def _btc_state(metric: str | None = "market_cap", **overrides) -> ConversationState:
    fields = dict(
        indicator="Bitcoin",
        coin_ids=["bitcoin"],
        vs_currency="usd",
        provider="COINGECKO",
        routed_provider="COINGECKO",
        crypto_metric=metric,
        language="en",
    )
    fields.update(overrides)
    return ConversationState(**fields)


def _dispatched_metric(intent: ParsedIntent) -> str:
    """The provider metric fetch_from_coingecko actually requests."""
    provider = _FakeCoinGeckoProvider()
    params = dict(intent.parameters or {})
    run(fetch_from_coingecko(provider, intent, params))
    calls = provider.simple_calls + provider.range_calls + provider.history_calls
    assert calls, "CoinGecko was not called"
    return calls[0]["metric"]


# ---------------------------------------------------------------------------
# Schema: structural enum validation, invalid dropped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("price", "price"),
    ("market_cap", "market_cap"),
    ("Market Cap", "market_cap"),
    ("total_volume", "volume"),
    ("price_change", "price_change"),
    ("prices", "price"),
    ("tvl", None),
    ("price not market cap", None),  # equality, never substring
    ("", None),
    (None, None),
])
def test_changed_metric_is_validated_against_the_fetch_enum(raw, expected) -> None:
    assert FollowUpDelta(changed_metric=raw).changed_metric == expected
    # The Instructor path validates JSON: an off-enum value is dropped, not raised.
    assert FollowUpDelta.model_validate({"changed_metric": raw}).changed_metric == expected


def test_every_validated_metric_is_a_fetch_enum_member() -> None:
    for value in ("price", "market cap", "volume", "24h change", "price change percentage 24h"):
        member = FollowUpDelta(changed_metric=value).changed_metric
        assert member in _COINGECKO_METRIC_ENUM


# ---------------------------------------------------------------------------
# merge_state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    ("market_cap", "price"), ("price", "market_cap"), ("market_cap", "volume"),
    (None, "volume"), ("volume", "price_change"),
])
def test_metric_switch_keeps_asset_and_provider(before, after) -> None:
    merged = merge_state(_btc_state(before), FollowUpDelta(changed_metric=after, changed_language="zh"))
    assert merged.crypto_metric == after
    assert merged.coin_ids == ["bitcoin"]
    assert merged.provider == "COINGECKO"
    assert merged.language == "zh"
    assert materialize_intent(merged).parameters["metric"] == after


@pytest.mark.parametrize("new_asset,coin", [
    ("Ethereum", "ethereum"), ("ethereum price", "ethereum"), ("solana", "solana"), ("XRP", "ripple"),
])
def test_asset_switch_keeps_the_measure(new_asset, coin) -> None:
    merged = merge_state(_btc_state("market_cap"), FollowUpDelta(changed_indicator=new_asset))
    assert merged.coin_ids == [coin]
    assert merged.crypto_metric == "market_cap"
    assert materialize_intent(merged).parameters["metric"] == "market_cap"


def test_asset_and_measure_switch_together() -> None:
    merged = merge_state(
        _btc_state("price"), FollowUpDelta(changed_indicator="Ethereum", changed_metric="volume"),
    )
    assert merged.coin_ids == ["ethereum"] and merged.crypto_metric == "volume"


def test_renaming_the_same_asset_is_not_an_asset_switch() -> None:
    """'Bitcoin price' re-naming the SAME coin keeps the pre-release clear (the
    measure then comes from changed_metric or the fetch label/default chain)."""
    merged = merge_state(_btc_state("market_cap"), FollowUpDelta(changed_indicator="Bitcoin price"))
    assert merged.coin_ids == ["bitcoin"]
    assert merged.crypto_metric is None
    explicit = merge_state(
        _btc_state("market_cap"), FollowUpDelta(changed_indicator="Bitcoin", changed_metric="price"),
    )
    assert explicit.crypto_metric == "price"


def test_non_asset_indicator_or_provider_change_clears_the_measure() -> None:
    assert merge_state(_btc_state(), FollowUpDelta(changed_indicator="inflation rate")).crypto_metric is None
    assert merge_state(_btc_state(), FollowUpDelta(changed_provider="FRED")).crypto_metric is None
    # An explicit measure on a provider switch AWAY from CoinGecko is out of scope.
    moved = merge_state(_btc_state(), FollowUpDelta(changed_provider="FRED", changed_metric="price"))
    assert moved.crypto_metric is None


def test_measure_slot_is_ignored_off_coingecko() -> None:
    state = ConversationState(indicator="house prices", provider="EUROSTAT", country="DE")
    merged = merge_state(state, FollowUpDelta(changed_metric="price"))
    assert merged.crypto_metric is None
    assert "metric" not in materialize_intent(merged).parameters


def test_new_query_delta_carries_its_measure() -> None:
    merged = merge_state(
        _btc_state(), FollowUpDelta(is_new_query=True, changed_indicator="ethereum", changed_metric="volume"),
    )
    assert merged.crypto_metric == "volume"


def test_metric_switch_changes_the_cache_contract() -> None:
    turn1 = materialize_intent(_btc_state("market_cap"))
    turn2 = materialize_intent(merge_state(_btc_state("market_cap"), FollowUpDelta(changed_metric="price")))
    c1 = _provider_request_contract("COINGECKO", turn1, dict(turn1.parameters))
    c2 = _provider_request_contract("COINGECKO", turn2, dict(turn2.parameters))
    assert (c1["metric"], c2["metric"]) == ("market_cap", "price")
    assert c1 != c2


# ---------------------------------------------------------------------------
# extract_with_llm: slot is a change, prompt shows the measure, scoped
# ---------------------------------------------------------------------------

def _llm_extractor(delta: FollowUpDelta) -> tuple[DeltaExtractor, AsyncMock]:
    create = AsyncMock(return_value=delta)
    qs = SimpleNamespace(openrouter=SimpleNamespace(
        instructor_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        instructor_model="test-model",
    ))
    return DeltaExtractor(qs), create


def test_llm_metric_only_delta_is_a_parameter_delta() -> None:
    extractor, create = _llm_extractor(FollowUpDelta(
        changed_metric="price", changed_language="zh", delta_type="metric_change",
        query_type="parameter_delta",
    ))
    delta = run(extractor.extract_with_llm("比特币价格呢", _btc_state("market_cap")))
    assert delta is not None and delta.changed_metric == "price"
    assert not delta.is_new_query and delta.changed_indicator is None
    system_prompt = create.await_args.kwargs["messages"][0]["content"]
    assert "Crypto measure: market_cap" in system_prompt
    assert "changed_metric" in system_prompt


def test_llm_metric_alone_counts_as_change_without_language() -> None:
    extractor, _ = _llm_extractor(FollowUpDelta(changed_metric="volume", query_type="parameter_delta"))
    delta = run(extractor.extract_with_llm("what about trading volume", _btc_state("price")))
    assert delta is not None and delta.changed_metric == "volume"


def test_llm_invalid_metric_alone_is_no_change() -> None:
    extractor, _ = _llm_extractor(FollowUpDelta.model_validate(
        {"changed_metric": "tvl", "query_type": "parameter_delta"},
    ))
    assert run(extractor.extract_with_llm("tvl?", _btc_state("price"))) is None


def test_llm_metric_off_coingecko_is_dropped() -> None:
    extractor, create = _llm_extractor(FollowUpDelta(changed_metric="price", query_type="parameter_delta"))
    state = ConversationState(indicator="GDP", country="US", provider="FRED")
    assert run(extractor.extract_with_llm("what about the price", state)) is None
    # No state line (the rule text itself names the label in quotes).
    assert "  Crypto measure: " not in create.await_args.kwargs["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Tier-1 structural crypto handler yields residual content to the LLM
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "what about ethereum price",
    "ETH volume instead",
    "show ethereum market cap",
    "ethereum 成交量",
    "以太坊 eth 市值",
])
def test_structural_crypto_switch_defers_residual_content(query) -> None:
    assert DeltaExtractor(SimpleNamespace()).extract(query, _btc_state("price")) is None


@pytest.mark.parametrize("query,changed", [
    ("what about ethereum", "ethereum price"),
    ("switch to solana instead", "solana price"),
    ("show only Ethereum and Dogecoin", "ethereum price"),
])
def test_structural_crypto_switch_still_handles_pure_asset_switches(query, changed) -> None:
    delta = DeltaExtractor(SimpleNamespace()).extract(query, _btc_state("market_cap"))
    assert delta is not None and delta.changed_indicator == changed
    # ... and the measure survives the asset switch.
    assert merge_state(_btc_state("market_cap"), delta).crypto_metric == "market_cap"


# ---------------------------------------------------------------------------
# End-to-end delta path: the dispatched CoinGecko measure
# ---------------------------------------------------------------------------

@pytest.fixture
def service() -> QueryService:
    cache_service.clear()
    query_module._intent_cache.clear()
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    svc.settings.use_outcome_decision_stage = False
    svc.settings.use_post_fetch_semantic_judge = False
    svc.settings.use_staged_state_commit = False
    yield svc
    query_module._intent_cache.clear()


def _run_delta_turn(service: QueryService, conv_name: str, query: str,
                    state: ConversationState, delta: FollowUpDelta) -> ParsedIntent:
    conv_id = conversation_manager.get_or_create(conv_name)
    conversation_manager.set_conversation_state(conv_id, state)
    captured: dict = {}

    async def _fake_execute(**kwargs):
        captured["intent"] = kwargs["intent"]
        return QueryResponse(conversationId=conv_id, intent=kwargs["intent"], clarificationNeeded=False)

    with patch.object(DeltaExtractor, "extract", return_value=None), \
         patch.object(DeltaExtractor, "extract_with_llm", new_callable=AsyncMock, return_value=delta), \
         patch.object(service, "_execute_resolved_intent", new=AsyncMock(side_effect=_fake_execute)):
        run(service.process_query(query, conversation_id=conv_id))
    assert "intent" in captured, "delta path did not dispatch"
    return captured["intent"]


def test_delta_path_metric_switch_dispatches_the_new_measure(service: QueryService) -> None:
    """'bitcoin market cap' -> '比特币价格呢' must dispatch PRICE (the regression)."""
    delta = FollowUpDelta(
        changed_metric="price", changed_language="zh", delta_type="language_change",
        query_type="parameter_delta", raw_query="比特币价格呢",
    )
    intent = _run_delta_turn(service, "conv-crypto-metric-switch", "比特币价格呢", _btc_state("market_cap"), delta)
    assert intent.apiProvider == "COINGECKO"
    assert intent.parameters["coinIds"] == ["bitcoin"]
    assert intent.parameters["metric"] == "price"
    assert _dispatched_metric(intent) == "price"
    turn1 = materialize_intent(_btc_state("market_cap"))
    assert (
        _provider_request_contract("COINGECKO", intent, dict(intent.parameters))
        != _provider_request_contract("COINGECKO", turn1, dict(turn1.parameters))
    )


def test_delta_path_asset_switch_keeps_the_measure(service: QueryService) -> None:
    """'bitcoin market cap' -> '以太坊呢' keeps market cap for the new asset."""
    delta = FollowUpDelta(
        changed_indicator="Ethereum", changed_language="zh", delta_type="indicator_switch",
        query_type="parameter_delta", raw_query="以太坊呢",
    )
    intent = _run_delta_turn(service, "conv-crypto-asset-switch", "以太坊呢", _btc_state("market_cap"), delta)
    assert intent.parameters["coinIds"] == ["ethereum"]
    assert _dispatched_metric(intent) == "market_cap"
