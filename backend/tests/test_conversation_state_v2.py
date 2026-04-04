"""Tests for FollowUpDelta + Merge conversation state architecture.

Covers:
- ConversationState and FollowUpDelta model creation
- merge_state() with all follow-up patterns
- materialize_intent() conversion
- extract_state_from_intent() backward-compat helper
- DeltaExtractor deterministic handlers
"""
from __future__ import annotations

import pytest

from backend.models import ParsedIntent
from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    extract_state_from_intent,
    materialize_intent,
    merge_state,
)


# ─── merge_state ────────────────────────────────────────────────────

class TestMergeState:
    def test_country_change_preserves_indicator_and_time(self):
        state = ConversationState(
            indicator="GDP", country="US", start_date="2020-01-01"
        )
        delta = FollowUpDelta(
            changed_country="DE", delta_type="country_change"
        )
        merged = merge_state(state, delta)
        assert merged.indicator == "GDP"
        assert merged.country == "DE"
        assert merged.start_date == "2020-01-01"
        assert merged.decomposition is None  # Cleared by country change

    def test_country_change_clears_decomposition(self):
        state = ConversationState(
            indicator="GDP",
            country="CA",
            decomposition={"type": "provinces", "entities": ["Ontario"]},
        )
        delta = FollowUpDelta(changed_country="US")
        merged = merge_state(state, delta)
        assert merged.decomposition is None

    def test_indicator_switch_preserves_country_clears_dimensions(self):
        state = ConversationState(
            indicator="CPI",
            country="CA",
            dimensions={"product": "food"},
        )
        delta = FollowUpDelta(
            changed_indicator="unemployment rate",
            delta_type="indicator_switch",
        )
        merged = merge_state(state, delta)
        assert merged.indicator == "unemployment rate"
        assert merged.country == "CA"
        assert merged.dimensions is None  # Cleared by indicator change

    def test_dimension_modifier_preserves_indicator(self):
        state = ConversationState(
            indicator="CPI",
            country="CA",
            dimensions={"product": "food"},
        )
        delta = FollowUpDelta(
            added_dimensions={"product": "energy"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.indicator == "CPI"  # Preserved
        assert merged.dimensions == {"product": "energy"}  # Updated

    def test_additive_country(self):
        state = ConversationState(
            indicator="GDP", countries=["US", "DE"]
        )
        delta = FollowUpDelta(
            added_countries=["FR"],
            delta_type="additive_country",
        )
        merged = merge_state(state, delta)
        assert merged.countries == ["US", "DE", "FR"]

    def test_additive_country_deduplication(self):
        state = ConversationState(
            indicator="GDP", countries=["US", "DE"]
        )
        delta = FollowUpDelta(added_countries=["DE", "FR"])
        merged = merge_state(state, delta)
        assert merged.countries == ["US", "DE", "FR"]

    def test_additive_from_single_country(self):
        state = ConversationState(indicator="GDP", country="US")
        delta = FollowUpDelta(added_countries=["FR"])
        merged = merge_state(state, delta)
        assert merged.countries == ["US", "FR"]
        assert merged.country is None  # Multi-country mode

    def test_removed_countries(self):
        state = ConversationState(
            indicator="GDP", countries=["US", "DE", "FR"]
        )
        delta = FollowUpDelta(removed_countries=["DE"])
        merged = merge_state(state, delta)
        assert merged.countries == ["US", "FR"]

    def test_removed_countries_to_single(self):
        state = ConversationState(
            indicator="GDP", countries=["US", "DE"]
        )
        delta = FollowUpDelta(removed_countries=["DE"])
        merged = merge_state(state, delta)
        assert merged.country == "US"
        assert merged.countries is None

    def test_new_query_clean_slate(self):
        state = ConversationState(
            indicator="GDP",
            country="US",
            start_date="2020-01-01",
        )
        delta = FollowUpDelta(
            is_new_query=True,
            changed_indicator="inflation",
            changed_country="JP",
        )
        merged = merge_state(state, delta)
        assert merged.indicator == "inflation"
        assert merged.country == "JP"
        assert merged.start_date is None  # Not carried over
        assert merged.turn_number == state.turn_number + 1

    def test_provider_change_clears_resolved_indicators(self):
        state = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            last_indicators_resolved=["GDP"],
        )
        delta = FollowUpDelta(changed_provider="WORLDBANK")
        merged = merge_state(state, delta)
        assert merged.provider == "WORLDBANK"
        assert merged.last_indicators_resolved is None

    def test_time_change_preserves_everything_else(self):
        state = ConversationState(
            indicator="GDP",
            country="DE",
            provider="WORLDBANK",
            start_date="2020-01-01",
            end_date="2023-12-31",
        )
        delta = FollowUpDelta(
            changed_start_date="2010-01-01",
            changed_end_date="2024-12-31",
        )
        merged = merge_state(state, delta)
        assert merged.indicator == "GDP"
        assert merged.country == "DE"
        assert merged.provider == "WORLDBANK"
        assert merged.start_date == "2010-01-01"
        assert merged.end_date == "2024-12-31"

    def test_turn_number_increments(self):
        state = ConversationState(indicator="GDP", turn_number=3)
        delta = FollowUpDelta(changed_country="US")
        merged = merge_state(state, delta)
        assert merged.turn_number == 4

    def test_changed_countries_replaces_single_country(self):
        state = ConversationState(indicator="GDP", country="US")
        delta = FollowUpDelta(changed_countries=["DE", "FR"])
        merged = merge_state(state, delta)
        assert merged.countries == ["DE", "FR"]
        assert merged.country is None

    def test_added_dimensions_merge(self):
        state = ConversationState(
            indicator="CPI",
            dimensions={"product": "food", "sex": "male"},
        )
        delta = FollowUpDelta(
            added_dimensions={"age": "youth"},
        )
        merged = merge_state(state, delta)
        assert merged.dimensions == {
            "product": "food",
            "sex": "male",
            "age": "youth",
        }

    def test_removed_dimensions(self):
        state = ConversationState(
            indicator="CPI",
            dimensions={"product": "food", "sex": "male"},
        )
        delta = FollowUpDelta(removed_dimensions=["sex"])
        merged = merge_state(state, delta)
        assert merged.dimensions == {"product": "food"}

    def test_removed_all_dimensions_becomes_none(self):
        state = ConversationState(
            indicator="CPI",
            dimensions={"product": "food"},
        )
        delta = FollowUpDelta(removed_dimensions=["product"])
        merged = merge_state(state, delta)
        assert merged.dimensions is None

    def test_trade_field_changes(self):
        state = ConversationState(
            indicator="trade",
            trade_flow="EXPORT",
            trade_reporter="US",
            trade_partner="CN",
        )
        delta = FollowUpDelta(changed_trade_flow="IMPORT")
        merged = merge_state(state, delta)
        assert merged.trade_flow == "IMPORT"
        assert merged.trade_reporter == "US"  # Preserved
        assert merged.trade_partner == "CN"  # Preserved

    def test_chart_type_change(self):
        state = ConversationState(indicator="GDP", chart_type="line")
        delta = FollowUpDelta(changed_chart_type="bar")
        merged = merge_state(state, delta)
        assert merged.chart_type == "bar"

    def test_raw_query_updates_original(self):
        state = ConversationState(
            indicator="GDP",
            original_query="GDP in US",
        )
        delta = FollowUpDelta(
            changed_country="DE",
            raw_query="show Germany",
        )
        merged = merge_state(state, delta)
        assert merged.original_query == "show Germany"

    def test_indicator_change_with_dimension_modifier_preserves_dimensions(self):
        """When is_dimension_modifier_change=True, changed_indicator does NOT clear dimensions."""
        state = ConversationState(
            indicator="CPI",
            dimensions={"product": "food"},
        )
        delta = FollowUpDelta(
            changed_indicator="CPI energy",
            is_dimension_modifier_change=True,
            added_dimensions={"product": "energy"},
        )
        merged = merge_state(state, delta)
        assert merged.indicator == "CPI energy"
        # dimensions NOT cleared because is_dimension_modifier_change=True
        # Then added_dimensions merges on top
        assert merged.dimensions == {"product": "energy"}


# ─── materialize_intent ─────────────────────────────────────────────

class TestMaterializeIntent:
    def test_basic_materialization(self):
        state = ConversationState(
            indicator="GDP",
            country="US",
            start_date="2020-01-01",
            end_date="2024-12-31",
            provider="FRED",
            original_query="GDP in US 2020-2024",
        )
        intent = materialize_intent(state)
        assert intent.apiProvider == "FRED"
        assert intent.indicators == ["GDP"]
        assert intent.parameters["country"] == "US"
        assert intent.parameters["startDate"] == "2020-01-01"
        assert intent.parameters["endDate"] == "2024-12-31"
        assert intent.originalQuery == "GDP in US 2020-2024"
        assert intent.clarificationNeeded is False

    def test_multi_country(self):
        state = ConversationState(
            indicator="inflation",
            countries=["US", "DE", "FR"],
            provider="WORLDBANK",
        )
        intent = materialize_intent(state)
        assert intent.parameters["countries"] == ["US", "DE", "FR"]
        assert "country" not in intent.parameters

    def test_single_country_in_list(self):
        state = ConversationState(
            indicator="GDP",
            countries=["JP"],
        )
        intent = materialize_intent(state)
        assert intent.parameters["country"] == "JP"
        assert "countries" not in intent.parameters

    def test_no_indicator_defaults_to_unknown(self):
        state = ConversationState(country="US")
        intent = materialize_intent(state)
        assert intent.indicators == ["unknown"]

    def test_default_provider(self):
        state = ConversationState(indicator="GDP")
        intent = materialize_intent(state)
        assert intent.apiProvider == "WorldBank"

    def test_trade_parameters(self):
        state = ConversationState(
            indicator="trade",
            trade_reporter="US",
            trade_partner="CN",
            trade_flow="EXPORT",
            trade_commodity="electronics",
        )
        intent = materialize_intent(state)
        assert intent.parameters["reporter"] == "US"
        assert intent.parameters["partner"] == "CN"
        assert intent.parameters["flow"] == "EXPORT"
        assert intent.parameters["commodity"] == "electronics"

    def test_crypto_parameters(self):
        state = ConversationState(
            indicator="bitcoin price",
            coin_ids=["bitcoin"],
            vs_currency="usd",
        )
        intent = materialize_intent(state)
        assert intent.parameters["coinIds"] == ["bitcoin"]
        assert intent.parameters["vsCurrency"] == "usd"

    def test_decomposition(self):
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            decomposition={"type": "provinces", "entities": ["Ontario", "Quebec"]},
        )
        intent = materialize_intent(state)
        assert intent.needsDecomposition is True
        assert intent.decompositionType == "provinces"
        assert intent.decompositionEntities == ["Ontario", "Quebec"]

    def test_follow_up_flag(self):
        state = ConversationState(indicator="GDP", turn_number=0)
        intent = materialize_intent(state)
        assert intent.isFollowUp is False

        state2 = ConversationState(indicator="GDP", turn_number=2)
        intent2 = materialize_intent(state2)
        assert intent2.isFollowUp is True


# ─── extract_state_from_intent ──────────────────────────────────────

class TestExtractStateFromIntent:
    def test_basic_extraction(self):
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US", "startDate": "2020-01-01"},
            clarificationNeeded=False,
            originalQuery="GDP in US",
        )
        state = extract_state_from_intent(intent)
        assert state.indicator == "GDP"
        assert state.country == "US"
        assert state.start_date == "2020-01-01"
        assert state.provider == "FRED"
        assert state.routed_provider == "FRED"
        assert state.original_query == "GDP in US"

    def test_multi_country_extraction(self):
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["inflation"],
            parameters={"countries": ["US", "DE", "FR"]},
            clarificationNeeded=False,
        )
        state = extract_state_from_intent(intent)
        assert state.countries == ["US", "DE", "FR"]
        assert state.country is None

    def test_single_country_in_list(self):
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP"],
            parameters={"countries": ["JP"]},
            clarificationNeeded=False,
        )
        state = extract_state_from_intent(intent)
        assert state.country == "JP"
        assert state.countries is None

    def test_trade_extraction(self):
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["trade"],
            parameters={
                "reporter": "US",
                "partner": "CN",
                "flow": "EXPORT",
                "commodity": "electronics",
            },
            clarificationNeeded=False,
        )
        state = extract_state_from_intent(intent)
        assert state.trade_reporter == "US"
        assert state.trade_partner == "CN"
        assert state.trade_flow == "EXPORT"
        assert state.trade_commodity == "electronics"

    def test_decomposition_extraction(self):
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["unemployment rate"],
            parameters={"country": "CA"},
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="provinces",
            decompositionEntities=["Ontario", "Quebec"],
        )
        state = extract_state_from_intent(intent)
        assert state.decomposition == {
            "type": "provinces",
            "entities": ["Ontario", "Quebec"],
        }


# ─── DeltaExtractor ─────────────────────────────────────────────────

class TestDeltaExtractor:
    """Tests for deterministic delta extraction handlers."""

    @pytest.fixture
    def extractor(self):
        """Create a DeltaExtractor with a mock QueryService."""
        # We only need the extractor, which needs minimal QueryService methods
        from unittest.mock import MagicMock
        mock_qs = MagicMock()
        from backend.services.delta_extractor import DeltaExtractor
        return DeltaExtractor(mock_qs)

    def test_country_only_follow_up(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("show Germany", state)
        assert delta is not None
        assert delta.delta_type == "country_change"
        assert delta.changed_country == "DE"

    def test_country_only_with_just(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("just Japan", state)
        assert delta is not None
        assert delta.changed_country == "JP"

    def test_additive_country(self, extractor):
        state = ConversationState(indicator="GDP", countries=["US"])
        delta = extractor.extract("add France", state)
        assert delta is not None
        assert delta.delta_type == "additive_country"
        assert delta.added_countries == ["FR"]

    def test_multi_country_replacement(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("show US and Germany", state)
        assert delta is not None
        assert delta.delta_type == "country_change"
        assert delta.changed_countries == ["US", "DE"]

    def test_indicator_switch_with_marker(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("what about inflation", state)
        assert delta is not None
        assert delta.delta_type == "indicator_switch"
        assert delta.changed_indicator == "inflation"

    def test_bare_indicator_short(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("unemployment", state)
        assert delta is not None
        assert delta.delta_type == "indicator_switch"
        assert delta.changed_indicator == "unemployment"

    def test_same_indicator_not_switch(self, extractor):
        state = ConversationState(indicator="gdp", country="US")
        delta = extractor.extract("gdp", state)
        # Should return None since the indicator hasn't changed
        assert delta is None

    def test_long_query_not_indicator_switch(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract(
            "I want to see a detailed analysis of the inflation rate trends "
            "across all OECD countries over the past two decades",
            state,
        )
        assert delta is None  # Too long for deterministic detection

    def test_country_in_query_not_indicator_switch(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        # Mentions a country -> should be caught by country handler, not indicator
        delta = extractor.extract("inflation in Germany", state)
        # The country handler should match first, but "inflation" is also present.
        # Since "Germany" is a country, _try_country_only won't match (non-geography token "inflation").
        # _try_indicator_switch won't match because a country is extracted.
        assert delta is None

    def test_provider_change(self, extractor):
        state = ConversationState(indicator="GDP", country="US", provider="WORLDBANK")
        delta = extractor.extract("use FRED", state)
        assert delta is not None
        assert delta.delta_type == "provider_change"
        assert delta.changed_provider == "FRED"

    def test_time_change_from_to(self, extractor):
        state = ConversationState(
            indicator="GDP", country="US",
            start_date="2020-01-01", end_date="2023-12-31",
        )
        delta = extractor.extract("from 2010 to 2024", state)
        assert delta is not None
        assert delta.delta_type == "time_change"
        assert delta.changed_start_date == "2010-01-01"
        assert delta.changed_end_date == "2024-12-31"

    def test_time_change_since(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("since 2015", state)
        assert delta is not None
        assert delta.changed_start_date == "2015-01-01"

    def test_time_change_last_n_years(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("last 20 years", state)
        assert delta is not None
        assert delta.delta_type == "time_change"
        assert delta.changed_start_date is not None

    def test_no_prior_state_returns_none(self, extractor):
        state = ConversationState()  # No indicator
        delta = extractor.extract("show GDP", state)
        assert delta is None

    def test_empty_query_returns_none(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("", state)
        assert delta is None


# ─── Integration: merge + materialize round-trip ────────────────────

class TestMergeAndMaterialize:
    """End-to-end: build state from intent, apply delta, materialize back."""

    def test_country_follow_up_round_trip(self):
        # Initial query result
        initial_intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US", "startDate": "2020-01-01"},
            clarificationNeeded=False,
            originalQuery="GDP in US since 2020",
        )
        state = extract_state_from_intent(initial_intent)
        assert state.country == "US"

        # Follow-up: change country
        delta = FollowUpDelta(
            changed_country="DE",
            raw_query="show Germany",
            delta_type="country_change",
        )
        merged = merge_state(state, delta)
        assert merged.country == "DE"
        assert merged.indicator == "GDP"
        assert merged.start_date == "2020-01-01"

        # Materialize back to intent
        intent = materialize_intent(merged)
        assert intent.parameters["country"] == "DE"
        assert intent.indicators == ["GDP"]
        assert intent.parameters["startDate"] == "2020-01-01"

    def test_three_turn_chain(self):
        """GDP US -> same for Germany -> last 20 years"""
        # Turn 1
        state = ConversationState(
            indicator="GDP",
            country="US",
            start_date="2020-01-01",
            end_date="2024-12-31",
            provider="FRED",
            turn_number=0,
        )

        # Turn 2: country change
        delta2 = FollowUpDelta(
            changed_country="DE",
            raw_query="same for Germany",
        )
        state = merge_state(state, delta2)
        assert state.indicator == "GDP"
        assert state.country == "DE"
        assert state.turn_number == 1

        # Turn 3: time change
        delta3 = FollowUpDelta(
            changed_start_date="2005-01-01",
            changed_end_date="2024-12-31",
            raw_query="last 20 years",
        )
        state = merge_state(state, delta3)
        assert state.indicator == "GDP"
        assert state.country == "DE"
        assert state.start_date == "2005-01-01"
        assert state.turn_number == 2

        intent = materialize_intent(state)
        assert intent.parameters["country"] == "DE"
        assert intent.indicators == ["GDP"]
        assert intent.parameters["startDate"] == "2005-01-01"

    def test_new_query_mid_conversation(self):
        """GDP US -> completely unrelated inflation Japan"""
        state = ConversationState(
            indicator="GDP",
            country="US",
            start_date="2020-01-01",
            provider="FRED",
            turn_number=2,
        )

        delta = FollowUpDelta(
            is_new_query=True,
            changed_indicator="inflation",
            changed_country="JP",
            raw_query="inflation in Japan",
        )
        merged = merge_state(state, delta)
        assert merged.indicator == "inflation"
        assert merged.country == "JP"
        assert merged.start_date is None  # Clean slate
        assert merged.provider is None
        assert merged.turn_number == 3


# ─── ConversationManager state methods ──────────────────────────────

class TestConversationManagerState:
    """Test the new state methods on ConversationManager."""

    @pytest.fixture(autouse=True)
    def _disable_redis(self, monkeypatch):
        import backend.services.conversation as conv_mod
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: None)

    def test_set_and_get_state(self):
        from backend.services.conversation import ConversationManager
        mgr = ConversationManager()
        cid = mgr.get_or_create(None)

        state = ConversationState(
            indicator="GDP", country="US", turn_number=1
        )
        mgr.set_conversation_state(cid, state)

        retrieved = mgr.get_conversation_state(cid)
        assert retrieved is not None
        assert retrieved.indicator == "GDP"
        assert retrieved.country == "US"

    def test_get_state_returns_none_when_absent(self):
        from backend.services.conversation import ConversationManager
        mgr = ConversationManager()
        cid = mgr.get_or_create(None)
        assert mgr.get_conversation_state(cid) is None

    def test_get_state_returns_deep_copy(self):
        from backend.services.conversation import ConversationManager
        mgr = ConversationManager()
        cid = mgr.get_or_create(None)

        state = ConversationState(indicator="GDP", countries=["US", "DE"])
        mgr.set_conversation_state(cid, state)

        copy1 = mgr.get_conversation_state(cid)
        copy1.countries.append("FR")  # Mutate the copy
        copy2 = mgr.get_conversation_state(cid)
        assert copy2.countries == ["US", "DE"]  # Original unchanged

    def test_restore_state(self):
        from backend.services.conversation import ConversationManager
        mgr = ConversationManager()
        cid = mgr.get_or_create(None)

        original = ConversationState(indicator="GDP", country="US")
        mgr.set_conversation_state(cid, original)

        new_state = ConversationState(indicator="inflation", country="JP")
        mgr.set_conversation_state(cid, new_state)

        # Restore
        mgr.restore_conversation_state(cid, original)
        retrieved = mgr.get_conversation_state(cid)
        assert retrieved.indicator == "GDP"

    def test_state_persists_through_redis_round_trip(self, monkeypatch):
        """Test that conversation_state survives Redis serialization."""
        import backend.services.conversation as conv_mod

        class _FakeRedis:
            def __init__(self):
                self._store = {}
            def setex(self, key, ttl, value):
                self._store[key] = value
            def get(self, key):
                return self._store.get(key)
            def delete(self, key):
                self._store.pop(key, None)

        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        from backend.services.conversation import ConversationManager
        mgr1 = ConversationManager()
        cid = mgr1.get_or_create(None)

        state = ConversationState(
            indicator="GDP",
            country="US",
            start_date="2020-01-01",
            turn_number=3,
            dimensions={"product": "food"},
        )
        mgr1.set_conversation_state(cid, state)

        # Simulate restart
        mgr2 = ConversationManager()
        assert cid not in mgr2._conversations

        # get_or_create loads from Redis
        mgr2.get_or_create(cid)
        retrieved = mgr2.get_conversation_state(cid)
        assert retrieved is not None
        assert retrieved.indicator == "GDP"
        assert retrieved.country == "US"
        assert retrieved.turn_number == 3
        assert retrieved.dimensions == {"product": "food"}
