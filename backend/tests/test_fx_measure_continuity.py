"""Changing one request operand must preserve the remaining measure contract."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.conversation_state_v2 import (
    AnswerSetMember,
    ConversationState,
    FollowUpDelta,
    build_measure_continuity,
    continuity_hint,
    extract_state_from_intent,
    materialize_intent,
    merge_new_state_with_previous,
    merge_state,
    update_answer_members_from_data,
)
from backend.services.data_fetcher import (
    extract_exchange_rate_params,
    fetch_exchange_rate_with_historical_fallback,
)
from backend.services.delta_extractor import DeltaExtractor
from backend.services.indicator_selector import IndicatorSelector, SelectionResult, build_continuity_kwargs


def fx_state(**overrides):
    values = dict(indicator="exchange rate", provider="EXCHANGERATE", base_currency="USD",
                  target_currency="EUR", start_date="2020-01-01", end_date="2024-12-31",
                  time_scope_source="user")
    values.update(overrides)
    return ConversationState(**values)


def series(unit="Current U.S. Dollars", price=None, country="US"):
    return NormalizedData(
        metadata=Metadata(source="FRED", indicator="GDP per capita", country=country,
                          frequency="annual", unit=unit, priceType=price, dataType="Level",
                          seasonalAdjustment="Not Seasonally Adjusted", seriesId="VERIFIED_CODE"),
        data=[{"date": "2024-01-01", "value": 50000.0}],
    )


def measure_state():
    state = ConversationState(indicator="GDP per capita", provider="FRED", country="US",
                              resolved_indicator_code="VERIFIED_CODE")
    update_answer_members_from_data(state, [series()])
    return state


@pytest.mark.parametrize("base,target,replacement", [("USD", "EUR", "JPY"), ("GBP", "CAD", "CHF"), ("AUD", "NZD", "SEK")])
def test_target_change_keeps_base_and_requested_time(base, target, replacement):
    original = fx_state(base_currency=base, target_currency=target, resolved_indicator_code="OLD_PAIR")
    changed = merge_state(original, FollowUpDelta(changed_target_currency=replacement, raw_query="quote it in another currency"))
    params = materialize_intent(changed).parameters
    assert (params["baseCurrency"], params["targetCurrency"]) == (base, replacement)
    assert params["startDate"] == original.start_date
    assert params["endDate"] == original.end_date
    assert params["__time_scope_authority"] == "user"
    assert changed.resolved_indicator_code is None
    assert original.target_currency == target
    # Both explicit operands bypass the single-currency first-turn defaults.
    result = extract_exchange_rate_params(params, ParsedIntent(
        apiProvider="EXCHANGERATE", indicators=["exchange rate"], parameters=params,
        originalQuery="in yen instead", clarificationNeeded=False,
    ))
    assert (result["baseCurrency"], result["targetCurrency"]) == (base, replacement)


def test_base_and_inverse_changes_have_distinct_roles():
    changed_base = merge_state(fx_state(), FollowUpDelta(changed_base_currency="GBP"))
    reversed_pair = merge_state(fx_state(), FollowUpDelta(reverse_currency_pair=True))
    assert (changed_base.base_currency, changed_base.target_currency) == ("GBP", "EUR")
    assert (reversed_pair.base_currency, reversed_pair.target_currency) == ("EUR", "USD")


def test_pair_survives_time_and_source_changes_but_not_new_concept():
    previous = fx_state()
    moved = merge_state(previous, FollowUpDelta(changed_provider="FRED", changed_start_date="2015-01-01"))
    assert (moved.base_currency, moved.target_currency) == ("USD", "EUR")
    assert moved.provider_locked  # The explicit source request retains its constraint.
    assert merge_state(previous, FollowUpDelta(changed_indicator="GDP")).base_currency is None
    assert merge_state(previous, FollowUpDelta(is_new_query=True, changed_indicator="GDP")).target_currency is None


def test_extraction_serialization_and_full_parse_merge_preserve_pair():
    intent = ParsedIntent(apiProvider="ExchangeRate", indicators=["exchange rate"],
                          parameters={"baseCurrency": "usd", "targetCurrency": "eur"},
                          clarificationNeeded=False)
    state = ConversationState.model_validate_json(extract_state_from_intent(intent).model_dump_json())
    carried = merge_new_state_with_previous(ConversationState(indicator="exchange rate", provider="FRED"), state)
    assert (carried.base_currency, carried.target_currency) == ("USD", "EUR")
    assert materialize_intent(carried).parameters["targetCurrency"] == "EUR"


def test_verified_answer_capture_records_executed_pair_without_changing_fred_provenance():
    state = ConversationState(indicator="exchange rate", provider="FRED")
    intent = ParsedIntent(apiProvider="ExchangeRate", indicators=["exchange rate"],
                          parameters={"baseCurrency": "USD", "targetCurrency": "JPY"},
                          clarificationNeeded=False)
    update_answer_members_from_data(state, [series("Japanese Yen to One U.S. Dollar")], intent=intent)
    assert (state.base_currency, state.target_currency) == ("USD", "JPY")
    assert state.provider == "FRED"
    assert state.active_answer_members[0].provider == "FRED"
    assert materialize_intent(state).parameters["targetCurrency"] == "JPY"


def test_unrelated_provider_does_not_introduce_an_fx_domain_from_incidental_slots():
    state = ConversationState(indicator="GDP", provider="FRED")
    intent = ParsedIntent(apiProvider="FRED", indicators=["GDP"],
                          parameters={"baseCurrency": "USD", "targetCurrency": "JPY"},
                          clarificationNeeded=False)
    update_answer_members_from_data(state, [series()], intent=intent)
    assert state.base_currency is None
    assert state.target_currency is None


def test_old_records_do_not_invent_a_counterpart():
    old = ConversationState.model_validate({"indicator": "exchange rate", "provider": "FRED"})
    changed = merge_state(old, FollowUpDelta(changed_target_currency="JPY"))
    assert changed.base_currency is None
    assert materialize_intent(changed).parameters["targetCurrency"] == "JPY"
    assert "baseCurrency" not in materialize_intent(changed).parameters


@pytest.mark.parametrize("raw,expected", [(" jpy ", "JPY"), ("CHF", "CHF"), ("yen", None), ("ZZZ", None), ("", None)])
def test_currency_role_slots_use_existing_iso_vocabulary(raw, expected):
    assert FollowUpDelta(changed_target_currency=raw).changed_target_currency == expected


def test_explicit_legacy_pair_dimension_updates_the_typed_operands():
    changed = merge_state(fx_state(), FollowUpDelta(added_dimensions={"Currency Pair": "GBP to CHF"}))
    assert (changed.base_currency, changed.target_currency) == ("GBP", "CHF")
    assert changed.dimensions["Currency Pair"] == "GBP to CHF"


@pytest.mark.asyncio
async def test_historical_fred_fetch_uses_the_changed_quote_without_inverting_roles():
    state = merge_state(fx_state(provider="FRED", resolved_indicator_code="OLD_EUR_PAIR"),
                        FollowUpDelta(changed_target_currency="JPY"))
    intent = materialize_intent(state)
    native = series("Japanese Yen to One U.S. Dollar")
    native.metadata.frequency = "daily"
    native.data[0].value = 150.0
    fred = AsyncMock(return_value=native)
    svc = SimpleNamespace(fred_provider=SimpleNamespace(fetch_series=fred))
    result = await fetch_exchange_rate_with_historical_fallback(svc, intent, intent.parameters)
    assert fred.await_args.args[0]["seriesId"] != "OLD_EUR_PAIR"
    assert result[0].metadata.indicator == "USD to JPY Exchange Rate"
    assert result[0].data[0].value == 150.0
    assert native.metadata.indicator == "GDP per capita"  # Source/cache object is untouched.


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [FollowUpDelta(changed_target_currency="JPY"), FollowUpDelta(reverse_currency_pair=True)])
async def test_llm_delta_has_ordered_currency_context_and_currency_only_change_counts(delta):
    create = AsyncMock(return_value=delta)
    qs = SimpleNamespace(openrouter=SimpleNamespace(instructor_model="test-model",
        instructor_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))
    parsed = await DeltaExtractor(qs).extract_with_llm("use another quote", fx_state(provider="FRED"))
    assert parsed is not None
    prompt = create.await_args.kwargs["messages"][0]["content"]
    assert "FX base currency (one unit of): USD" in prompt
    assert "FX target currency (quoted units): EUR" in prompt


@pytest.mark.asyncio
async def test_non_fx_measure_cannot_borrow_currency_operand_slots():
    create = AsyncMock(return_value=FollowUpDelta(changed_target_currency="GBP", reverse_currency_pair=True))
    qs = SimpleNamespace(openrouter=SimpleNamespace(instructor_model="test-model",
        instructor_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))
    state = ConversationState(indicator="GDP", provider="FRED", country="US")
    assert await DeltaExtractor(qs).extract_with_llm("in pounds instead", state) is None


def test_verified_measure_metadata_survives_country_and_frequency_changes():
    original = measure_state()
    changed = merge_state(original, FollowUpDelta(changed_country="CA", changed_frequency="quarterly"))
    params = materialize_intent(changed).parameters
    evidence = params["__measure_continuity"]["series"][0]
    assert evidence["unit"] == "Current U.S. Dollars"
    assert evidence["seasonalAdjustment"] == "Not Seasonally Adjusted"
    assert evidence["frequency"] == "annual"  # Prior evidence, not the changed request slot.
    assert params["frequency"] == "quarterly"
    assert json.loads(build_continuity_kwargs(params)["continuity_series"])["series"][0] == evidence
    params["__measure_continuity"]["series"][0]["unit"] = "mutated"
    assert original.measure_continuity["series"][0]["unit"] == "Current U.S. Dollars"


def test_source_change_carries_measure_but_concept_change_drops_it():
    state = measure_state()
    moved = merge_state(state, FollowUpDelta(changed_provider="IMF"))
    assert moved.measure_continuity == state.measure_continuity
    assert materialize_intent(moved).parameters["__measure_continuity"]["series"][0]["unit"] == "Current U.S. Dollars"
    assert merge_state(state, FollowUpDelta(changed_indicator="unemployment")).measure_continuity is None
    assert "__measure_continuity" not in materialize_intent(
        merge_state(state, FollowUpDelta(changed_indicator="unemployment"))).parameters


def test_old_member_evidence_is_not_fabricated_and_new_member_scope_can_be_selected():
    old = ConversationState(indicator="GDP", active_answer_members=[AnswerSetMember(provider="FRED", provider_code="GDPA")])
    assert build_measure_continuity(old) is None
    old.resolved_indicator_code = "GDPA"
    assert continuity_hint(old) == "GDP [GDPA]"
    state = measure_state()
    assert build_measure_continuity(state, members=[state.active_answer_members[0]])["series"][0]["country"] == "US"


@pytest.mark.asyncio
async def test_measure_context_participates_in_selector_cache_identity(monkeypatch):
    selector = IndicatorSelector(settings=SimpleNamespace())
    pick = AsyncMock(return_value=SelectionResult(code="ANSWER", name="same measure", source="llm_pick"))
    monkeypatch.setattr(selector, "_select_uncached", pick)
    nominal = measure_state()
    real = measure_state()
    update_answer_members_from_data(real, [series("2010 U.S. Dollars", "Real (inflation-adjusted)")])
    for state in (nominal, nominal, real):
        await selector.select("GDP per capita continuity regression", "FRED", country="CA",
                              **build_continuity_kwargs(materialize_intent(state).parameters))
    assert pick.await_count == 2
    hints = [call.kwargs["continuity_series"] for call in pick.await_args_list]
    assert "Current U.S. Dollars" in hints[0]
    assert "2010 U.S. Dollars" in hints[1]
