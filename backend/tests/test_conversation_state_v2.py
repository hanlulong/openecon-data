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

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.conversation_state_v2 import (
    AnswerSetMember,
    ConversationState,
    FollowUpDelta,
    build_answer_set_members_from_data,
    extract_state_from_intent,
    materialize_intent,
    merge_state,
    merge_new_state_with_previous,
    update_answer_members_from_data,
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

    def test_geography_breakdown_promotes_to_first_class_decomposition(self):
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
        )
        delta = FollowUpDelta(
            added_dimensions={"Geography": "province"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition == {
            "type": "provinces",
            "entities": None,
            "axis": "Geography",
        }
        assert merged.dimensions is None

    def test_changed_geography_decomposition_clears_stale_geography_dimension_filters(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
            dimensions={"Geography": "Ontario"},
        )
        delta = FollowUpDelta(
            changed_decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition == {
            "type": "provinces",
            "entities": None,
            "axis": "Geography",
        }
        assert merged.dimensions is None

    def test_specific_geography_filter_clears_first_class_decomposition(self):
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
        )
        delta = FollowUpDelta(
            added_dimensions={"Geography": "Ontario"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition is None
        assert merged.dimensions == {"Geography": "Ontario"}

    def test_age_breakdown_promotes_to_first_class_dimension_decomposition(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
            dimensions={"Geography": "Ontario"},
        )
        delta = FollowUpDelta(
            added_dimensions={"Age group": "age group"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition == {
            "type": "dimension",
            "entities": None,
            "axis": "Age group",
        }
        assert merged.dimensions == {"Geography": "Ontario"}

    def test_specific_age_filter_clears_dimension_decomposition(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
            dimensions={"Geography": "Ontario"},
            decomposition={"type": "dimension", "entities": None, "axis": "Age group"},
        )
        delta = FollowUpDelta(
            added_dimensions={"Age group": "25 to 54 years"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition is None
        assert merged.dimensions == {
            "Geography": "Ontario",
            "Age group": "25 to 54 years",
        }

    def test_specific_sex_filter_clears_dimension_decomposition_even_when_axis_is_sex(self):
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            decomposition={"type": "dimension", "entities": None, "axis": "Sex"},
        )
        delta = FollowUpDelta(
            added_dimensions={"sex": "female"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition is None
        assert merged.dimensions == {"sex": "female"}

    def test_specific_age_filter_clears_generic_demographic_decomposition(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
            dimensions={"Geography": "Ontario"},
            decomposition={"type": "dimension", "entities": None, "axis": "Demographic"},
        )
        delta = FollowUpDelta(
            added_dimensions={"Age group": "25 to 54 years"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition is None
        assert merged.dimensions == {
            "Geography": "Ontario",
            "Age group": "25 to 54 years",
        }

    def test_age_group_decomposition_payload_is_canonicalized_to_dimension_axis(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
        )
        delta = FollowUpDelta(
            changed_decomposition={"type": "age groups", "entities": None, "axis": "Age"},
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition == {
            "type": "dimension",
            "entities": None,
            "axis": "Age group",
        }

    def test_age_groups_decomposition_payload_with_underscore_is_canonicalized(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
        )
        delta = FollowUpDelta(
            changed_decomposition={"type": "age_groups", "entities": None, "axis": "Age group"},
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition == {
            "type": "dimension",
            "entities": None,
            "axis": "Age group",
        }

    def test_age_group_singular_decomposition_payload_is_canonicalized(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
        )
        delta = FollowUpDelta(
            changed_decomposition={"type": "age group", "entities": None, "axis": "Geography"},
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.decomposition == {
            "type": "dimension",
            "entities": None,
            "axis": "Age group",
        }

    def test_frequency_change_is_merged_into_state(self):
        state = ConversationState(
            indicator="inflation rate",
            country="US",
            frequency="annual",
        )
        delta = FollowUpDelta(
            changed_frequency="monthly",
            delta_type="parameter_change",
        )
        merged = merge_state(state, delta)
        assert merged.frequency == "monthly"

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
        assert merged.provider_locked is True
        assert merged.last_indicators_resolved is None

    def test_geography_mutation_preserves_resolution_snapshot_until_runtime_validates_scope(self):
        state = ConversationState(
            indicator="GDP growth rate",
            countries=["US"],
            provider="FRED",
            provider_locked=False,
            resolved_indicator_code="A191RL1Q225SBEA",
            last_indicators_resolved=["A191RL1Q225SBEA"],
        )
        delta = FollowUpDelta(added_countries=["CA"])
        merged = merge_state(state, delta)
        assert merged.countries == ["US", "CA"]
        assert merged.resolved_indicator_code == "A191RL1Q225SBEA"
        assert merged.last_indicators_resolved == ["A191RL1Q225SBEA"]

    def test_geography_mutation_preserves_resolution_snapshot_when_provider_locked(self):
        state = ConversationState(
            indicator="GDP growth rate",
            countries=["US"],
            provider="IMF",
            provider_locked=True,
            resolved_indicator_code="NGDP_RPCH",
            last_indicators_resolved=["NGDP_RPCH"],
        )
        delta = FollowUpDelta(added_countries=["CA"])
        merged = merge_state(state, delta)
        assert merged.countries == ["US", "CA"]
        assert merged.resolved_indicator_code == "NGDP_RPCH"
        assert merged.last_indicators_resolved == ["NGDP_RPCH"]

    def test_provider_lock_carries_forward_in_context_merge(self):
        previous = ConversationState(
            indicator="GDP growth rate",
            provider="IMF",
            provider_locked=True,
            country="US",
        )
        new_state = ConversationState(
            indicator="GDP growth rate",
            provider="IMF",
            country="US",
        )
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.provider == "IMF"
        assert merged.provider_locked is True

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

    def test_chart_type_alias_index_normalizes_to_line(self):
        state = ConversationState(indicator="GDP", chart_type="scatter")
        delta = FollowUpDelta(changed_chart_type="index")
        merged = merge_state(state, delta)
        assert merged.chart_type == "line"

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

    def test_crypto_indicator_with_suffix_matches_map(self):
        """'ethereum price', 'bitcoin token', etc. should still auto-detect crypto."""
        state = ConversationState(indicator="GDP", country="US")
        for indicator, expected_coin in [
            ("ethereum price", "ethereum"),
            ("bitcoin crypto", "bitcoin"),
            ("dogecoin token", "dogecoin"),
            ("solana coin", "solana"),
            ("cardano", "cardano"),       # no suffix, still works
            ("btc price", "bitcoin"),     # alias + suffix
            ("xrp", "ripple"),            # alias, no suffix
        ]:
            delta = FollowUpDelta(changed_indicator=indicator)
            merged = merge_state(state, delta)
            assert merged.coin_ids == [expected_coin], f"Failed for '{indicator}'"
            assert merged.provider == "COINGECKO", f"Failed for '{indicator}'"

    def test_indicator_change_to_non_crypto_clears_stale_coin_ids(self):
        state = ConversationState(
            indicator="bitcoin price",
            coin_ids=["bitcoin"],
            provider="COINGECKO",
        )
        merged = merge_state(state, FollowUpDelta(changed_indicator="inflation rate"))
        assert merged.coin_ids is None

    def test_additive_crypto_indicators_merge_coin_ids(self):
        state = ConversationState(
            indicator="bitcoin price",
            coin_ids=["bitcoin"],
            provider="COINGECKO",
        )
        merged = merge_state(
            state,
            FollowUpDelta(added_indicators=["ethereum price", "dogecoin price"]),
        )
        assert merged.coin_ids == ["bitcoin", "ethereum", "dogecoin"]
        assert merged.last_indicators_resolved == [
            "bitcoin price",
            "ethereum price",
            "dogecoin price",
        ]


# ─── materialize_intent ─────────────────────────────────────────────

class TestMaterializeIntent:
    def test_basic_materialization(self):
        state = ConversationState(
            indicator="GDP",
            country="US",
            start_date="2020-01-01",
            end_date="2024-12-31",
            frequency="quarterly",
            provider="FRED",
            original_query="GDP in US 2020-2024",
        )
        intent = materialize_intent(state)
        assert intent.apiProvider == "FRED"
        assert intent.indicators == ["GDP"]
        assert intent.parameters["country"] == "US"
        assert intent.parameters["startDate"] == "2020-01-01"
        assert intent.parameters["endDate"] == "2024-12-31"
        assert intent.parameters["frequency"] == "quarterly"
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

    def test_provider_lock_materializes_to_parameters(self):
        state = ConversationState(
            indicator="GDP growth rate",
            provider="IMF",
            provider_locked=True,
            country="US",
        )
        intent = materialize_intent(state)
        assert intent.apiProvider == "IMF"
        assert intent.parameters["__semantic_provider_locked"] is True
        assert intent.parameters["__semantic_indicator_label"] == "GDP growth rate"

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

    def test_crypto_parameters_derive_coin_ids_from_indicator_state(self):
        state = ConversationState(
            indicator="bitcoin price",
            last_indicators_resolved=["bitcoin price", "ethereum price"],
            provider="COINGECKO",
        )
        intent = materialize_intent(state)
        assert intent.parameters["coinIds"] == ["bitcoin", "ethereum"]

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

    def test_dimension_decomposition_materializes_axis_contract(self):
        state = ConversationState(
            indicator="employment rate",
            country="CA",
            dimensions={"Geography": "Ontario"},
            decomposition={"type": "dimension", "entities": None, "axis": "Age group"},
        )
        intent = materialize_intent(state)
        assert intent.needsDecomposition is True
        assert intent.decompositionType == "dimension"
        assert intent.parameters["__statscan_decomposition_axis"] == "Age group"
        assert intent.parameters["__dimensions"] == {"Geography": "Ontario"}

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
            parameters={"country": "US", "startDate": "2020-01-01", "frequency": "quarterly"},
            clarificationNeeded=False,
            originalQuery="GDP in US",
        )
        state = extract_state_from_intent(intent)
        assert state.indicator == "GDP"
        assert state.country == "US"
        assert state.start_date == "2020-01-01"
        assert state.frequency == "quarterly"
        assert state.provider == "FRED"
        assert state.routed_provider == "FRED"
        assert state.original_query == "GDP in US"

    def test_extract_state_prefers_semantic_indicator_from_query_over_provider_code(self):
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["A191RL1Q225SBEA"],
            parameters={"country": "US", "indicator": "A191RL1Q225SBEA"},
            clarificationNeeded=False,
            originalQuery="US GDP growth rate",
        )
        state = extract_state_from_intent(intent)
        assert state.indicator == "GDP growth rate"
        assert state.resolved_indicator_code == "A191RL1Q225SBEA"

    def test_extract_state_prefers_query_semantic_indicator_when_parser_label_drifts(self):
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["deflation"],
            parameters={"countries": ["US", "Canada", "United Kingdom"], "indicator": "FP.CPI.TOTL.ZG"},
            clarificationNeeded=False,
            originalQuery="Add United Kingdom inflation rate",
        )
        state = extract_state_from_intent(intent)
        assert state.indicator == "inflation rate"
        assert state.resolved_indicator_code == "FP.CPI.TOTL.ZG"

    def test_extract_state_prefers_explicit_semantic_indicator_label_param(self):
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["NGDP_RPCH"],
            parameters={"country": "CA", "indicator": "NGDP_RPCH", "__semantic_indicator_label": "GDP growth rate"},
            clarificationNeeded=False,
            originalQuery="Show only Canada",
        )
        state = extract_state_from_intent(intent)
        assert state.indicator == "GDP growth rate"
        assert state.resolved_indicator_code == "NGDP_RPCH"

    def test_extract_state_canonicalizes_statscan_dimension_axis_from_intent(self):
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={"country": "CA", "__statscan_decomposition_axis": "Sex"},
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="dimension",
            decompositionEntities=["Males", "Females"],
            originalQuery="unemployment in Canada by sex",
        )

        state = extract_state_from_intent(intent)

        assert state.decomposition == {
            "type": "dimension",
            "entities": ["Males", "Females"],
            "axis": "Gender",
        }

    def test_extract_state_prefers_explicit_statscan_product_id_over_drifted_indicator_code(self):
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={
                "country": "CA",
                "indicator": "17100024",
                "__statscan_product_id": "14100287",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
            originalQuery="Show all provinces",
        )

        state = extract_state_from_intent(intent)

        assert state.resolved_indicator_code == "14100287"
        assert state.statscan_product_id == "14100287"

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

    def test_provider_lock_extraction(self):
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["GDP growth rate"],
            parameters={"__semantic_provider_locked": True},
            clarificationNeeded=False,
            originalQuery="GDP growth rate from IMF",
        )
        state = extract_state_from_intent(intent)
        assert state.provider == "IMF"
        assert state.provider_locked is True

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

    def test_dimension_category_extraction_promotes_to_decomposition(self):
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["unemployment rate"],
            parameters={"country": "CA", "__dimensions": {"Geography": "province"}},
            clarificationNeeded=False,
        )
        state = extract_state_from_intent(intent)
        assert state.decomposition == {
            "type": "provinces",
            "entities": None,
            "axis": "Geography",
        }
        assert state.dimensions is None

    def test_dimension_axis_extraction_preserves_generic_decomposition_contract(self):
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["employment rate"],
            parameters={
                "country": "CA",
                "__dimensions": {"Geography": "Ontario"},
                "__statscan_decomposition_axis": "Age group",
            },
            clarificationNeeded=False,
            needsDecomposition=True,
            decompositionType="dimension",
        )
        state = extract_state_from_intent(intent)
        assert state.decomposition == {
            "type": "dimension",
            "entities": None,
            "axis": "Age group",
        }
        assert state.dimensions == {"Geography": "Ontario"}

    def test_statscan_product_id_extraction_from_numeric_indicator_parameters(self):
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100330"],
            parameters={
                "country": "CA",
                "indicator": "14100330",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
        )
        state = extract_state_from_intent(intent)
        assert state.statscan_product_id == "14100330"


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

    def test_additive_country_with_current_indicator_reaffirmation(self, extractor):
        state = ConversationState(indicator="real GDP", country="US")
        delta = extractor.extract("Add UK real GDP", state)
        assert delta is not None
        assert delta.delta_type == "additive_country"
        assert delta.added_countries == ["GB"]

    def test_ranking_follow_up_preserves_multi_country_state(self, extractor):
        state = ConversationState(
            indicator="GDP growth rate",
            countries=["US", "DE", "JP"],
            provider="IMF",
        )
        delta = extractor.extract("Show only top 3", state)
        assert delta is not None
        assert delta.delta_type == "chart_change"
        assert delta.changed_chart_type == "line"

    def test_annualized_rate_follow_up_preserves_already_rate_like_series(self, extractor):
        state = ConversationState(
            indicator="inflation rate",
            countries=["US", "GB"],
            provider="WORLDBANK",
        )
        delta = extractor.extract("Convert to annualized rate", state)
        assert delta is not None
        assert delta.delta_type == "chart_change"
        assert delta.changed_chart_type == "line"

    def test_convert_to_billions_follow_up_preserves_rate_like_series(self, extractor):
        state = ConversationState(
            indicator="GDP growth rate",
            countries=["US", "DE", "JP"],
            provider="IMF",
            resolved_indicator_code="NGDP_RPCH",
        )
        delta = extractor.extract("Convert to billions", state)
        assert delta is not None
        assert delta.delta_type == "chart_change"
        assert delta.changed_chart_type == "line"

    def test_crypto_all_time_high_follow_up_preserves_active_asset_context(self, extractor):
        state = ConversationState(
            indicator="bitcoin price",
            provider="COINGECKO",
            coin_ids=["bitcoin"],
        )
        delta = extractor.extract("Compare price to all-time high", state)
        assert delta is not None
        assert delta.delta_type == "chart_change"
        assert delta.changed_chart_type == "line"

    def test_country_and_provider_follow_up_with_indicator_reaffirmation(self, extractor):
        state = ConversationState(indicator="GDP", country="US", provider="FRED")
        delta = extractor.extract("Japan GDP from World Bank", state)
        assert delta is not None
        assert delta.delta_type == "compound_change"
        assert delta.changed_country == "JP"
        assert delta.changed_provider == "WORLDBANK"

    def test_country_and_provider_follow_up_with_indicator_reaffirmation_handles_common_acronyms(self, extractor):
        state = ConversationState(
            indicator="Gross Domestic Product",
            country="US",
            provider="FRED",
        )
        delta = extractor.extract("India GDP from World Bank", state)
        assert delta is not None
        assert delta.delta_type == "compound_change"
        assert delta.changed_country == "IN"
        assert delta.changed_provider == "WORLDBANK"
        assert delta.changed_indicator is None

    def test_multi_country_replacement(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("show US and Germany", state)
        assert delta is not None
        assert delta.delta_type == "country_change"
        assert delta.changed_countries == ["US", "DE"]

    def test_indicator_switch_deferred_to_llm(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("what about inflation", state)
        assert delta is None

    def test_fill_explicit_country_scope_for_switch_delta_sets_changed_country(self, extractor):
        delta = FollowUpDelta(
            changed_indicator="US inflation",
            changed_provider="FRED",
            delta_type="indicator_switch",
        )

        updated = extractor._fill_explicit_country_scope_for_switch_delta(  # pylint: disable=protected-access
            delta,
            "Switch to US inflation from FRED",
        )

        assert updated.changed_country == "US"

    def test_fill_explicit_country_scope_for_switch_delta_keeps_existing_geo_delta(self, extractor):
        delta = FollowUpDelta(
            changed_indicator="GDP",
            changed_provider="WORLDBANK",
            added_countries=["DE"],
            delta_type="compound_change",
        )

        updated = extractor._fill_explicit_country_scope_for_switch_delta(  # pylint: disable=protected-access
            delta,
            "Add Germany GDP from World Bank",
        )

        assert updated.added_countries == ["DE"]
        assert updated.changed_country is None

    def test_fill_explicit_country_scope_for_provider_change_recovers_country_when_llm_misses_it(self, extractor):
        delta = FollowUpDelta(
            changed_provider="WORLDBANK",
            delta_type="country_change",
        )

        updated = extractor._fill_explicit_country_scope_for_switch_delta(  # pylint: disable=protected-access
            delta,
            "Japan GDP from World Bank",
        )

        assert updated.changed_country == "JP"

    def test_fill_explicit_provider_for_switch_delta_recovers_provider_when_llm_misses_it(self, extractor):
        delta = FollowUpDelta(
            changed_indicator="GDP growth rate",
            delta_type="indicator_switch",
        )

        updated = extractor._fill_explicit_provider_for_switch_delta(  # pylint: disable=protected-access
            delta,
            "Switch to IMF GDP growth rate",
        )

        assert updated.changed_provider == "IMF"

    def test_bare_indicator_without_switch_marker_stays_with_llm(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("unemployment", state)
        assert delta is None

    def test_additive_crypto_indicator_detected_structurally(self, extractor):
        state = ConversationState(indicator="bitcoin price", provider="COINGECKO")
        delta = extractor.extract("add Ethereum for comparison", state)
        assert delta is not None
        assert delta.added_indicators == ["ethereum price"]

    def test_multi_crypto_replacement_detected_structurally(self, extractor):
        state = ConversationState(indicator="solana price", provider="COINGECKO")
        delta = extractor.extract("show only Ethereum and Dogecoin", state)
        assert delta is not None
        assert delta.changed_indicator == "ethereum price"
        assert delta.added_indicators == ["dogecoin price"]

    def test_long_query_not_matched(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract(
            "I want to see a detailed analysis of the inflation rate trends "
            "across all OECD countries over the past two decades",
            state,
        )
        assert delta is None  # Too complex for regex, deferred to LLM

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

    def test_provider_reaffirmation_returns_parameter_delta(self, extractor):
        state = ConversationState(indicator="inflation", countries=["US", "GB"], provider="WORLDBANK")
        delta = extractor.extract("Switch to World Bank data", state)
        assert delta is not None
        assert delta.delta_type == "provider_change"
        assert delta.changed_provider == "WORLDBANK"

    def test_frequency_change_detected_structurally(self, extractor):
        state = ConversationState(indicator="inflation rate", country="US", frequency="annual")
        delta = extractor.extract("Change to monthly frequency", state)
        assert delta is not None
        assert delta.changed_frequency == "monthly"

    def test_breakdown_follow_up_detected_structurally(self, extractor):
        state = ConversationState(
            indicator="employment rate",
            provider="STATSCAN",
            country="CA",
            dimensions={"Geography": "Ontario"},
        )
        delta = extractor.extract("Show by age group", state)
        assert delta is not None
        assert delta.changed_decomposition == {
            "type": "dimension",
            "entities": None,
            "axis": "Age group",
        }

    def test_provider_change_geo_artifacts_are_sanitized(self, extractor):
        delta = FollowUpDelta(
            changed_provider="WORLDBANK",
            changed_country="1W",
            delta_type="provider_change",
        )
        updated = extractor._sanitize_provider_change_geography_artifacts(  # pylint: disable=protected-access
            delta,
            "Switch to World Bank data",
        )
        assert updated.changed_country is None

    def test_provider_change_with_country_and_metric_is_handled_structurally_when_indicator_is_reaffirmed(self, extractor):
        state = ConversationState(indicator="GDP", country="US", provider="FRED")
        delta = extractor.extract("Japan GDP from World Bank", state)
        assert delta is not None
        assert delta.changed_country == "JP"
        assert delta.changed_provider == "WORLDBANK"

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

    def test_time_change_last_n_days(self, extractor):
        state = ConversationState(indicator="exchange rate", provider="EXCHANGERATE")
        delta = extractor.extract("show only the last 90 days", state)
        assert delta is not None
        assert delta.delta_type == "time_change"
        assert delta.changed_start_date is not None
        assert delta.changed_end_date is not None

    def test_time_change_last_year_singular_phrase(self, extractor):
        state = ConversationState(indicator="exchange rate", provider="EXCHANGERATE")
        delta = extractor.extract("change to last year", state)
        assert delta is not None
        assert delta.delta_type == "time_change"
        assert delta.changed_start_date is not None
        assert delta.changed_end_date is not None

    def test_time_change_guard_rejects_contentful_queries(self, extractor):
        assert extractor._looks_like_pure_time_change_query("last 20 years") is True
        assert (
            extractor._looks_like_pure_time_change_query(
                "unemployment in Canada by sex in last 20 years"
            )
            is False
        )

    def test_exchange_rate_pair_change_detected_structurally(self, extractor):
        state = ConversationState(indicator="exchange rate", provider="EXCHANGERATE")
        delta = extractor.extract("Switch to USD to JPY", state)
        assert delta is not None
        assert delta.delta_type == "dimension_change"
        assert delta.added_dimensions == {"Currency Pair": "USD to JPY"}
        assert delta.is_dimension_modifier_change is True

    def test_exchange_rate_pair_slash_notation_detected_structurally(self, extractor):
        state = ConversationState(indicator="exchange rate", provider="EXCHANGERATE")
        delta = extractor.extract("Switch to EUR/GBP", state)
        assert delta is not None
        assert delta.added_dimensions == {"Currency Pair": "EUR to GBP"}

    def test_year_over_year_change_on_growth_series_is_noop_chart_change(self, extractor):
        state = ConversationState(
            indicator="GDP growth rate",
            provider="IMF",
            resolved_indicator_code="NGDP_RPCH",
        )
        delta = extractor.extract("Show year-over-year change", state)
        assert delta is not None
        assert delta.delta_type == "chart_change"
        assert delta.changed_chart_type == "line"

    def test_trade_flow_indicator_sync_detected_structurally(self, extractor):
        state = ConversationState(
            indicator="trade balance",
            provider="COMTRADE",
            trade_flow="EXPORT",
        )
        delta = extractor.extract("Switch back to US exports", state)
        assert delta is not None
        assert delta.changed_indicator == "exports"
        assert delta.changed_trade_flow == "EXPORT"

    def test_comtrade_additive_reporter_follow_up_detected_structurally(self, extractor):
        state = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            country="US",
            trade_reporter="US",
            trade_partner="CN",
            trade_flow="EXPORT",
        )
        delta = extractor.extract("Add Germany exports to China", state)
        assert delta is not None
        assert delta.added_countries == ["DE"]
        assert delta.changed_indicator == "exports"
        assert delta.changed_trade_flow == "EXPORT"

    def test_comtrade_partner_change_detected_structurally(self, extractor):
        state = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            country="US",
            trade_reporter="US",
            trade_partner="CN",
            trade_flow="EXPORT",
        )
        delta = extractor.extract("Change partner to Canada", state)
        assert delta is not None
        assert delta.changed_trade_partner == "CA"

    def test_comtrade_change_to_country_exports_sets_partner_from_phrase(self, extractor):
        state = ConversationState(
            indicator="trade balance",
            provider="COMTRADE",
            country="US",
            trade_reporter="US",
            trade_partner="JP",
            trade_flow=None,
        )
        delta = extractor.extract("Change to Germany exports to China", state)
        assert delta is not None
        assert delta.changed_country == "DE"
        assert delta.changed_trade_partner == "CN"
        assert delta.changed_indicator == "exports"
        assert delta.changed_trade_flow == "EXPORT"

    def test_comtrade_total_trade_switch_detected_structurally(self, extractor):
        state = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            country="US",
            trade_reporter="US",
            trade_partner="CA",
            trade_flow="EXPORT",
        )
        delta = extractor.extract("Show total trade US and Canada", state)
        assert delta is not None
        assert delta.changed_country == "US"
        assert delta.changed_indicator == "total trade"
        assert delta.changed_trade_flow is None

    def test_no_prior_state_returns_none(self, extractor):
        state = ConversationState()  # No indicator
        delta = extractor.extract("show GDP", state)
        assert delta is None

    def test_empty_query_returns_none(self, extractor):
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor.extract("", state)
        assert delta is None


# ─── DeltaExtractor Phase 3: Context-aware dimension modifiers ──────

class TestDeltaExtractorDimensionModifier:
    """Tests for context-aware dimension modifier detection (Phase 3).

    These tests verify that when the conversation state points to a
    StatsCan indicator with dimensional tables, follow-up terms like
    "female", "shelter", "Ontario" are correctly classified as dimension
    modifiers rather than indicator switches.
    """

    @pytest.fixture
    def _build_extractor(self):
        """Factory for a DeltaExtractor with a mocked StatsCan provider."""
        from unittest.mock import MagicMock, AsyncMock

        def _make(
            vector_mappings=None,
            coord_mappings=None,
            product_id_cache=None,
            cube_metadata=None,
            extracted_modifiers=None,
        ):
            mock_qs = MagicMock()
            mock_statscan = MagicMock()

            mock_statscan.PRODUCT_ID_CACHE = product_id_cache or {}
            mock_statscan._normalize_metadata_product_id = (
                lambda pid: "".join(ch for ch in str(pid) if ch.isdigit())[:8]
            )

            # Mock _get_cube_metadata as async
            mock_statscan._get_cube_metadata = AsyncMock(
                return_value=cube_metadata or {}
            )

            # Mock extract_dimension_modifiers as sync
            mock_statscan.extract_dimension_modifiers = MagicMock(
                return_value=extracted_modifiers or {}
            )

            mock_qs.statscan_provider = mock_statscan

            from backend.services.delta_extractor import DeltaExtractor
            return DeltaExtractor(mock_qs)

        return _make

    def test_female_after_unemployment_is_dimension(self, _build_extractor):
        """'show female' after unemployment rate resolves as a dimension modifier."""
        _cube = {"dimension": [{"dimensionNameEn": "Sex", "member": []}]}
        extractor = _build_extractor(
            vector_mappings={"UNEMPLOYMENT_RATE": 2062815},
            product_id_cache={2062815: "1410028702"},
            cube_metadata=_cube,
            extracted_modifiers={"sex": "female"},
        )
        state = ConversationState(
            indicator="unemployment rate",
            base_indicator="UNEMPLOYMENT_RATE",
            provider="StatsCan",
            country="CA",
            statscan_product_id="14100287",
            statscan_cube_metadata=_cube,
        )
        delta = extractor.extract("show female", state)
        assert delta is not None
        assert delta.added_dimensions == {"sex": "female"}

    def test_shelter_after_cpi_is_dimension_modifier(self, _build_extractor):
        """StatsCan product/category filters resolve as dimension modifiers."""
        _cube = {"dimension": [{"dimensionNameEn": "Products and product groups", "member": []}]}
        extractor = _build_extractor(
            vector_mappings={"CPI": 41690973},
            product_id_cache={41690973: "1810000401"},
            cube_metadata=_cube,
            extracted_modifiers={"products": "Shelter"},
        )
        state = ConversationState(
            indicator="CPI",
            base_indicator="CPI",
            provider="StatsCan",
            country="CA",
            statscan_product_id="18100004",
            statscan_cube_metadata=_cube,
        )
        delta = extractor.extract("show shelter", state)
        assert delta is not None
        assert delta.added_dimensions == {"products": "Shelter"}

    def test_inflation_after_unemployment_deferred_to_llm(self, _build_extractor):
        """Generic non-crypto indicator switches remain on the LLM path."""
        _cube = {"dimension": [{"dimensionNameEn": "Sex", "member": []}]}
        extractor = _build_extractor(
            vector_mappings={"UNEMPLOYMENT_RATE": 2062815},
            product_id_cache={2062815: "1410028702"},
            cube_metadata=_cube,
            extracted_modifiers={},
        )
        state = ConversationState(
            indicator="unemployment rate",
            base_indicator="UNEMPLOYMENT_RATE",
            provider="StatsCan",
            country="CA",
            statscan_product_id="14100287",
            statscan_cube_metadata=_cube,
        )
        delta = extractor.extract("show inflation", state)
        assert delta is None

    def test_non_statscan_provider_deferred_to_llm(self, _build_extractor):
        """Non-structural follow-ups are now handled by LLM."""
        extractor = _build_extractor()
        state = ConversationState(
            indicator="GDP",
            provider="FRED",
            country="US",
        )
        delta = extractor.extract("female", state)
        assert delta is None  # Deferred to LLM

    def test_dimension_modifier_youth_is_structural(self, _build_extractor):
        """Age-style StatsCan follow-ups resolve structurally."""
        _cube = {"dimension": []}
        extractor = _build_extractor(
            vector_mappings={"UNEMPLOYMENT_RATE": 2062815},
            product_id_cache={2062815: "1410028702"},
            cube_metadata=_cube,
            extracted_modifiers={"age": "youth"},
        )
        state = ConversationState(
            indicator="unemployment rate",
            base_indicator="UNEMPLOYMENT_RATE",
            provider="STATSCAN",
            country="CA",
            statscan_product_id="14100287",
            statscan_cube_metadata=_cube,
        )
        delta = extractor.extract("show youth", state)
        assert delta is not None
        assert delta.added_dimensions == {"age": "youth"}

    def test_coordinate_mapping_geography_filter_is_structural(self, _build_extractor):
        """Province-based StatsCan filters resolve structurally as dimensions."""
        _cube = {"dimension": [{"dimensionNameEn": "Geography", "member": []}]}
        extractor = _build_extractor(
            coord_mappings={"HOUSING_PRICE_INDEX": ("18100205", "1.1.0.0.0.0.0.0.0.0", "desc")},
            cube_metadata=_cube,
            extracted_modifiers={"geography": "Ontario"},
        )
        state = ConversationState(
            indicator="housing price index",
            base_indicator="HOUSING_PRICE_INDEX",
            provider="StatsCan",
            country="CA",
            statscan_product_id="18100205",
            statscan_cube_metadata=_cube,
        )
        delta = extractor.extract("show Ontario", state)
        assert delta is not None
        assert delta.added_dimensions == {"geography": "Ontario"}

    def test_dimension_modifier_uses_statscan_product_id_even_without_known_base_indicator(self, _build_extractor):
        _cube = {"dimension": [{"dimensionNameEn": "Geography", "member": []}]}
        extractor = _build_extractor(
            cube_metadata=_cube,
            extracted_modifiers={"geography": "Alberta"},
        )
        state = ConversationState(
            indicator="employment rate",
            provider="StatsCan",
            country="CA",
            statscan_product_id="14100287",
            statscan_cube_metadata=_cube,
        )
        delta = extractor.extract("Switch to Alberta", state)
        assert delta is not None
        assert delta.added_dimensions == {"geography": "Alberta"}

    def test_breakdown_phrase_wins_over_misleading_dimension_member_match(self, _build_extractor):
        _cube = {"dimension": [{"dimensionNameEn": "Geography", "member": []}]}
        extractor = _build_extractor(
            cube_metadata=_cube,
            extracted_modifiers={"geography": "Peer group A"},
        )
        state = ConversationState(
            indicator="employment rate",
            provider="StatsCan",
            country="CA",
            statscan_product_id="14100330",
            statscan_cube_metadata=_cube,
        )
        delta = extractor.extract("Show by age group", state)
        assert delta is not None
        assert delta.changed_decomposition == {
            "type": "dimension",
            "entities": None,
            "axis": "Age group",
        }


# ─── materialize_intent with dimensions ─────────────────────────────

class TestMaterializeIntentWithDimensions:
    """Phase 3: materialize_intent passes dimensions to intent parameters."""

    def test_dimensions_included_in_parameters(self):
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            dimensions={"sex": "female"},
        )
        intent = materialize_intent(state)
        assert "__dimensions" in intent.parameters
        assert intent.parameters["__dimensions"] == {"sex": "female"}

    def test_no_dimensions_no_key(self):
        state = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
        )
        intent = materialize_intent(state)
        assert "__dimensions" not in intent.parameters

    def test_dimensions_round_trip_through_merge(self):
        """Dimension modifier delta → merge → materialize includes dimensions."""
        initial = ConversationState(
            indicator="CPI",
            country="CA",
            provider="STATSCAN",
            turn_number=1,
        )
        delta = FollowUpDelta(
            added_dimensions={"products": "Shelter"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
            raw_query="show shelter",
        )
        merged = merge_state(initial, delta)
        assert merged.dimensions == {"products": "Shelter"}

        intent = materialize_intent(merged)
        assert intent.parameters["__dimensions"] == {"products": "Shelter"}
        assert intent.indicators == ["CPI"]  # Indicator preserved
        assert intent.isFollowUp is True


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

    def test_merge_new_state_with_previous_preserves_decomposition_on_time_change(self):
        previous = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
            turn_number=1,
        )
        new_state = ConversationState(
            indicator="unemployment rate",
            provider="STATSCAN",
            start_date="2010-01-01",
            end_date="2024-12-31",
        )

        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.decomposition == previous.decomposition
        intent = materialize_intent(merged)
        assert intent.needsDecomposition is True
        assert intent.decompositionType == "provinces"

    def test_merge_new_state_with_previous_does_not_preserve_decomposition_when_geo_filter_added(self):
        previous = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
            turn_number=1,
        )
        new_state = ConversationState(
            indicator="unemployment rate",
            provider="STATSCAN",
            dimensions={"Geography": "Ontario"},
        )

        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.decomposition is None
        assert merged.dimensions == {"Geography": "Ontario"}

    def test_merge_new_state_with_previous_does_not_preserve_dimension_decomposition_when_axis_filter_added(self):
        previous = ConversationState(
            indicator="employment rate",
            country="CA",
            provider="STATSCAN",
            dimensions={"Geography": "Ontario"},
            decomposition={"type": "dimension", "entities": None, "axis": "Age group"},
            turn_number=1,
        )
        new_state = ConversationState(
            indicator="employment rate",
            provider="STATSCAN",
            dimensions={"Geography": "Ontario", "Age group": "25 to 54 years"},
        )

        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.decomposition is None
        assert merged.dimensions == {
            "Geography": "Ontario",
            "Age group": "25 to 54 years",
        }


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


# ─── Issue 1: CPI subcategory loses context on 3rd+ round ────────────

class TestCubeMetadataPreservedAcrossDimensionRounds:
    """Regression: "CPI Canada" → "show food" → "show energy" must keep cube metadata."""

    def test_merge_preserves_statscan_cube_metadata(self):
        """merge_state deep-copies cube metadata across dimension changes."""
        _cube = {
            "dimension": [
                {
                    "dimensionNameEn": "Products and product groups",
                    "member": [
                        {"memberNameEn": "Food"},
                        {"memberNameEn": "Energy"},
                        {"memberNameEn": "Shelter"},
                    ],
                }
            ]
        }
        # Turn 1 state (after "CPI Canada")
        state = ConversationState(
            indicator="CPI",
            base_indicator="CPI",
            provider="STATSCAN",
            country="CA",
            statscan_product_id="18100004",
            statscan_cube_metadata=_cube,
            turn_number=0,
        )

        # Turn 2: "show food" (dimension change)
        delta_food = FollowUpDelta(
            added_dimensions={"products": "Food"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
            raw_query="show food",
        )
        state_t2 = merge_state(state, delta_food)
        assert state_t2.statscan_cube_metadata is not None
        assert state_t2.dimensions == {"products": "Food"}
        assert state_t2.indicator == "CPI"
        assert state_t2.turn_number == 1

        # Turn 3: "show energy" (another dimension change)
        delta_energy = FollowUpDelta(
            added_dimensions={"products": "Energy"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
            raw_query="show energy",
        )
        state_t3 = merge_state(state_t2, delta_energy)
        assert state_t3.statscan_cube_metadata is not None
        assert state_t3.dimensions == {"products": "Energy"}
        assert state_t3.indicator == "CPI"
        assert state_t3.base_indicator == "CPI"
        assert state_t3.statscan_product_id == "18100004"
        assert state_t3.turn_number == 2

    def test_merge_preserves_product_id(self):
        """statscan_product_id survives dimension delta merges."""
        state = ConversationState(
            indicator="unemployment rate",
            base_indicator="UNEMPLOYMENT_RATE",
            provider="STATSCAN",
            country="CA",
            statscan_product_id="14100287",
            statscan_cube_metadata={"dimension": []},
        )
        delta = FollowUpDelta(
            added_dimensions={"sex": "Female"},
            is_dimension_modifier_change=True,
        )
        merged = merge_state(state, delta)
        assert merged.statscan_product_id == "14100287"
        assert merged.statscan_cube_metadata is not None


# ─── Issue 2: "Add Brazil" to GDP per capita loses "per capita" ──────

class TestResolvedIndicatorCodePreservation:
    """Regression: resolved_indicator_code prevents re-resolution drift."""

    def test_resolved_code_preserved_across_country_add(self):
        """Adding a country should keep the resolved indicator code."""
        state = ConversationState(
            indicator="GDP per capita",
            resolved_indicator_code="NY.GDP.PCAP.CD",
            provider="WORLDBANK",
            countries=["US", "CA", "MX"],
            turn_number=1,
        )
        delta = FollowUpDelta(
            added_countries=["BR"],
            delta_type="additive_country",
            raw_query="add Brazil",
        )
        merged = merge_state(state, delta)
        assert merged.resolved_indicator_code == "NY.GDP.PCAP.CD"
        assert merged.indicator == "GDP per capita"
        assert "BR" in merged.countries
        assert len(merged.countries) == 4

        intent = materialize_intent(merged)
        # Intent should use the resolved code, not the human-readable name
        assert intent.indicators == ["NY.GDP.PCAP.CD"]
        assert "BR" in intent.parameters["countries"]

    def test_resolved_code_cleared_on_indicator_change(self):
        """Switching indicator should clear the stale resolved code."""
        state = ConversationState(
            indicator="GDP per capita",
            resolved_indicator_code="NY.GDP.PCAP.CD",
            provider="WORLDBANK",
            country="US",
        )
        delta = FollowUpDelta(
            changed_indicator="unemployment rate",
            delta_type="indicator_switch",
        )
        merged = merge_state(state, delta)
        assert merged.resolved_indicator_code is None
        assert merged.indicator == "unemployment rate"

        intent = materialize_intent(merged)
        assert intent.indicators == ["unemployment rate"]

    def test_resolved_code_cleared_on_provider_change(self):
        """Switching provider should clear the stale resolved code."""
        state = ConversationState(
            indicator="GDP",
            resolved_indicator_code="NY.GDP.MKTP.CD",
            provider="WORLDBANK",
            country="US",
        )
        delta = FollowUpDelta(
            changed_provider="FRED",
            delta_type="provider_change",
        )
        merged = merge_state(state, delta)
        assert merged.resolved_indicator_code is None

    def test_resolved_code_used_by_materialize_intent(self):
        """materialize_intent prefers resolved_indicator_code over indicator."""
        state = ConversationState(
            indicator="GDP per capita",
            resolved_indicator_code="NY.GDP.PCAP.CD",
            provider="WORLDBANK",
            country="US",
        )
        intent = materialize_intent(state)
        assert intent.indicators == ["NY.GDP.PCAP.CD"]

    def test_materialize_falls_back_to_indicator_when_no_resolved_code(self):
        """Without resolved code, materialize uses indicator name."""
        state = ConversationState(
            indicator="GDP",
            provider="FRED",
            country="US",
        )
        intent = materialize_intent(state)
        assert intent.indicators == ["GDP"]

    def test_extract_state_captures_resolved_code(self):
        """extract_state_from_intent stores resolved code from params."""
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP per capita"],
            parameters={"country": "US", "indicator": "NY.GDP.PCAP.CD"},
            clarificationNeeded=False,
        )
        state = extract_state_from_intent(intent)
        assert state.resolved_indicator_code == "NY.GDP.PCAP.CD"
        assert state.indicator == "GDP per capita"

    def test_extract_state_resolved_code_always_set(self):
        """resolved_indicator_code is always set from params.indicator."""
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["CPIAUCSL"],
            parameters={"country": "US", "indicator": "CPIAUCSL"},
            clarificationNeeded=False,
        )
        state = extract_state_from_intent(intent)
        assert state.resolved_indicator_code == "CPIAUCSL"

    def test_resolved_code_preserved_across_time_change(self):
        """Changing time range should keep the resolved indicator code."""
        state = ConversationState(
            indicator="inflation rate",
            resolved_indicator_code="FP.CPI.TOTL.ZG",
            provider="WORLDBANK",
            country="US",
            start_date="2020-01-01",
        )
        delta = FollowUpDelta(
            changed_start_date="2010-01-01",
            delta_type="time_change",
        )
        merged = merge_state(state, delta)
        assert merged.resolved_indicator_code == "FP.CPI.TOTL.ZG"
        assert merged.start_date == "2010-01-01"

    def test_context_merge_does_not_carry_resolved_code_across_indicator_change(self):
        previous = ConversationState(
            indicator="GDP per capita",
            resolved_indicator_code="NY.GDP.PCAP.CD",
            provider="WORLDBANK",
            country="US",
        )
        new_state = ConversationState(
            indicator="GDP growth rate",
            provider="WORLDBANK",
            country="US",
        )
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.resolved_indicator_code is None

    def test_context_merge_does_not_carry_crypto_coin_ids_across_indicator_change(self):
        previous = ConversationState(
            indicator="bitcoin price",
            coin_ids=["bitcoin"],
            provider="COINGECKO",
        )
        new_state = ConversationState(
            indicator="ethereum price",
            provider="COINGECKO",
        )
        merged = merge_new_state_with_previous(new_state, previous)
        assert merged.coin_ids is None

    def test_resolved_code_survives_redis_round_trip(self, monkeypatch):
        """resolved_indicator_code survives Redis serialization."""
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
        mgr = ConversationManager()
        cid = mgr.get_or_create(None)

        state = ConversationState(
            indicator="GDP per capita",
            resolved_indicator_code="NY.GDP.PCAP.CD",
            provider="WORLDBANK",
            country="US",
            turn_number=1,
        )
        mgr.set_conversation_state(cid, state)

        # Simulate restart
        mgr2 = ConversationManager()
        mgr2.get_or_create(cid)
        retrieved = mgr2.get_conversation_state(cid)
        assert retrieved is not None
        assert retrieved.resolved_indicator_code == "NY.GDP.PCAP.CD"
        assert retrieved.indicator == "GDP per capita"

    def test_build_answer_set_members_from_data_projects_provider_country_and_code(self):
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP growth rate"],
            parameters={"__semantic_indicator_label": "GDP growth rate", "country": "JP"},
            clarificationNeeded=False,
            originalQuery="Japan GDP growth rate",
        )
        data = [
            NormalizedData(
                metadata=Metadata(
                    source="World Bank",
                    indicator="NY.GDP.MKTP.KD.ZG",
                    country="JP",
                    frequency="annual",
                    unit="%",
                    seriesId="NY.GDP.MKTP.KD.ZG",
                ),
                data=[],
            )
        ]

        members = build_answer_set_members_from_data(data, intent=intent, turn_number=3)

        assert members == [
            AnswerSetMember(
                provider="WORLDBANK",
                indicator_label="GDP growth rate",
                provider_code="NY.GDP.MKTP.KD.ZG",
                series_id="NY.GDP.MKTP.KD.ZG",
                country="JP",
                countries=["JP"],
                source_turn=3,
            )
        ]

    def test_update_answer_members_from_data_accumulates_recent_history_across_turns(self):
        initial_state = ConversationState(
            indicator="GDP",
            provider="FRED",
            country="US",
            turn_number=1,
        )
        fred_intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            parameters={"country": "US"},
            clarificationNeeded=False,
            originalQuery="US GDP from FRED",
        )
        fred_data = [
            NormalizedData(
                metadata=Metadata(
                    source="FRED",
                    indicator="GDP",
                    country="US",
                    frequency="quarterly",
                    unit="USD",
                    seriesId="GDP",
                ),
                data=[],
            )
        ]

        update_answer_members_from_data(initial_state, fred_data, intent=fred_intent)

        next_state = merge_new_state_with_previous(
            ConversationState(
                indicator="GDP",
                provider="WORLDBANK",
                country="JP",
                turn_number=2,
            ),
            initial_state,
        )
        wb_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["GDP"],
            parameters={"country": "JP"},
            clarificationNeeded=False,
            originalQuery="Japan GDP from World Bank",
        )
        wb_data = [
            NormalizedData(
                metadata=Metadata(
                    source="World Bank",
                    indicator="NY.GDP.MKTP.CD",
                    country="JP",
                    frequency="annual",
                    unit="USD",
                    seriesId="NY.GDP.MKTP.CD",
                ),
                data=[],
            )
        ]

        update_answer_members_from_data(next_state, wb_data, intent=wb_intent)

        assert next_state.active_answer_members == [
            AnswerSetMember(
                provider="WORLDBANK",
                indicator_label="NY.GDP.MKTP.CD",
                provider_code="NY.GDP.MKTP.CD",
                series_id="NY.GDP.MKTP.CD",
                country="JP",
                countries=["JP"],
                source_turn=2,
            )
        ]
        assert next_state.recent_answer_members == [
            AnswerSetMember(
                provider="FRED",
                indicator_label="GDP",
                provider_code="GDP",
                series_id="GDP",
                country="US",
                countries=["US"],
                source_turn=1,
            ),
            AnswerSetMember(
                provider="WORLDBANK",
                indicator_label="NY.GDP.MKTP.CD",
                provider_code="NY.GDP.MKTP.CD",
                series_id="NY.GDP.MKTP.CD",
                country="JP",
                countries=["JP"],
                source_turn=2,
            ),
        ]

    def test_update_answer_members_from_data_refreshes_statscan_product_id_from_series(self):
        state = ConversationState(
            indicator="employment rate",
            provider="STATSCAN",
            country="Canada",
            statscan_product_id="14100362",
            statscan_cube_metadata={"dimension": [{"dimensionNameEn": "Old"}]},
            turn_number=4,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100287"],
            parameters={"country": "Canada", "__semantic_indicator_label": "employment rate"},
            clarificationNeeded=False,
            originalQuery="Show by province",
        )
        data = [
            NormalizedData(
                metadata=Metadata(
                    source="Statistics Canada",
                    indicator="Ontario employment rate",
                    country="Canada",
                    frequency="monthly",
                    unit="%",
                    seriesId="14100287:7.9.1.1.1.1.0.0.0.0",
                ),
                data=[],
            )
        ]

        update_answer_members_from_data(state, data, intent=intent)

        assert state.statscan_product_id == "14100287"
        assert state.statscan_cube_metadata is None

    def test_update_answer_members_from_data_prefers_explicit_statscan_product_over_filtered_series_product(self):
        state = ConversationState(
            indicator="employment rate",
            provider="STATSCAN",
            country="Canada",
            statscan_product_id="14100330",
            turn_number=4,
        )
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["14100330"],
            parameters={
                "country": "Canada",
                "indicator": "14100330",
                "__statscan_product_id": "14100287",
                "__semantic_indicator_label": "employment rate",
            },
            clarificationNeeded=False,
            originalQuery="Show all provinces",
        )
        data = [
            NormalizedData(
                metadata=Metadata(
                    source="Statistics Canada",
                    indicator="Ontario employment rate",
                    country="Canada",
                    frequency="monthly",
                    unit="%",
                    seriesId="20100056:10.1.1.2.0.0.0.0.0.0",
                ),
                data=[],
            )
        ]

        update_answer_members_from_data(state, data, intent=intent)

        assert state.statscan_product_id == "14100287"

    def test_update_answer_members_from_data_refreshes_coingecko_coin_ids_from_series(self):
        state = ConversationState(
            indicator="bitcoin price",
            provider="COINGECKO",
            coin_ids=["bitcoin"],
            turn_number=4,
        )
        intent = ParsedIntent(
            apiProvider="COINGECKO",
            indicators=["dynamic", "ethereum price"],
            parameters={"coinIds": ["ethereum"]},
            clarificationNeeded=False,
            originalQuery="Add Ethereum for comparison",
        )
        data = [
            NormalizedData(
                metadata=Metadata(
                    source="CoinGecko",
                    indicator="Bitcoin Price",
                    country=None,
                    frequency="daily",
                    unit="USD",
                    seriesId="bitcoin",
                ),
                data=[],
            ),
            NormalizedData(
                metadata=Metadata(
                    source="CoinGecko",
                    indicator="Ethereum Price",
                    country=None,
                    frequency="daily",
                    unit="USD",
                    seriesId="ethereum",
                ),
                data=[],
            ),
        ]

        update_answer_members_from_data(state, data, intent=intent)

        assert state.coin_ids == ["bitcoin", "ethereum"]

    def test_extract_state_from_intent_preserves_comtrade_reporter_scope(self):
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["exports"],
            parameters={
                "countries": ["United States", "China"],
                "reporter": "United States",
                "partner": "China",
                "flow": "EXPORT",
            },
            clarificationNeeded=False,
            originalQuery="US exports to China",
        )

        state = extract_state_from_intent(intent)

        assert state.provider == "COMTRADE"
        assert state.country == "United States"
        assert state.countries is None
        assert state.trade_reporter == "United States"
        assert state.trade_partner == "China"

    def test_extract_state_from_intent_comtrade_ignores_partner_in_generic_countries_scope(self):
        intent = ParsedIntent(
            apiProvider="COMTRADE",
            indicators=["exports"],
            parameters={
                "countries": ["US", "CN"],
                "reporter": "United States",
                "partner": "China",
                "flow": "EXPORT",
            },
            clarificationNeeded=False,
            originalQuery="US exports to China",
        )

        state = extract_state_from_intent(intent)

        assert state.country == "United States"
        assert state.countries is None
        assert state.trade_reporter == "United States"
        assert state.trade_partner == "China"

    def test_materialize_intent_uses_comtrade_reporters_for_multi_reporter_scope(self):
        state = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            countries=["Germany", "France"],
            trade_reporter="Germany",
            trade_partner="China",
            trade_flow="EXPORT",
            turn_number=4,
        )

        intent = materialize_intent(state)

        assert intent.parameters["reporters"] == ["Germany", "France"]
        assert intent.parameters["reporter"] == "Germany"
        assert intent.parameters["partner"] == "China"
        assert intent.parameters["flow"] == "EXPORT"

    def test_materialize_intent_prefers_current_comtrade_country_scope_over_stale_reporter(self):
        state = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            country="United States",
            trade_reporter="Germany",
            trade_partner="Canada",
            trade_flow="EXPORT",
            turn_number=4,
        )

        intent = materialize_intent(state)

        assert intent.parameters["country"] == "United States"
        assert intent.parameters["reporter"] == "United States"
        assert intent.parameters["partner"] == "Canada"

    def test_indicator_change_with_trade_reporter_resets_country_scope_to_reporter(self):
        current = ConversationState(
            indicator="trade balance",
            provider="COMTRADE",
            countries=["Germany", "France"],
            trade_reporter="Germany",
            trade_partner="China",
            trade_flow="EXPORT",
        )
        delta = FollowUpDelta(
            changed_indicator="total trade",
            raw_query="Show total trade US and Canada",
        )

        merged = merge_state(current, delta)

        assert merged.country == "Germany"
        assert merged.countries is None
        assert merged.trade_reporter == "Germany"

    def test_indicator_change_to_total_trade_clears_trade_flow(self):
        current = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            country="United States",
            trade_reporter="United States",
            trade_partner="Canada",
            trade_flow="EXPORT",
        )
        delta = FollowUpDelta(
            changed_indicator="total trade",
            raw_query="Show total trade US and Canada",
        )

        merged = merge_state(current, delta)

        assert merged.trade_flow is None

    def test_comtrade_country_change_updates_trade_reporter(self):
        current = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            country="Germany",
            trade_reporter="Germany",
            trade_partner="Canada",
            trade_flow="EXPORT",
        )
        delta = FollowUpDelta(
            changed_country="US",
            raw_query="Switch back to US exports",
        )

        merged = merge_state(current, delta)

        assert merged.country == "US"
        assert merged.trade_reporter == "US"

    def test_answer_member_history_survives_redis_round_trip(self, monkeypatch):
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
        mgr = ConversationManager()
        cid = mgr.get_or_create(None)

        state = ConversationState(
            indicator="GDP",
            provider="WORLDBANK",
            country="US",
            turn_number=2,
            active_answer_members=[
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP",
                    provider_code="NY.GDP.MKTP.CD",
                    series_id="NY.GDP.MKTP.CD",
                    country="US",
                    countries=["US"],
                    source_turn=2,
                )
            ],
            recent_answer_members=[
                AnswerSetMember(
                    provider="FRED",
                    indicator_label="GDP",
                    provider_code="GDP",
                    series_id="GDP",
                    country="US",
                    countries=["US"],
                    source_turn=1,
                ),
                AnswerSetMember(
                    provider="WORLDBANK",
                    indicator_label="GDP",
                    provider_code="NY.GDP.MKTP.CD",
                    series_id="NY.GDP.MKTP.CD",
                    country="US",
                    countries=["US"],
                    source_turn=2,
                ),
            ],
        )
        mgr.set_conversation_state(cid, state)

        mgr2 = ConversationManager()
        mgr2.get_or_create(cid)
        retrieved = mgr2.get_conversation_state(cid)
        assert retrieved is not None
        assert retrieved.active_answer_members is not None
        assert retrieved.active_answer_members[0].provider == "WORLDBANK"
        assert retrieved.recent_answer_members is not None
        assert [member.provider for member in retrieved.recent_answer_members] == ["FRED", "WORLDBANK"]


# ─── Phase 2.6 schema bump: telemetry fields ──────────────────────────

class TestPhase26TelemetryFields:
    """The schema bump adds delta_confidence + needs_full_rewrite to FollowUpDelta.

    Behavior is TELEMETRY ONLY in this PR — merge_state must not consume them
    yet. These tests freeze that contract so a future PR that wires them up
    has to deliberately update the tests.
    """

    def test_defaults_are_inert(self):
        delta = FollowUpDelta()
        assert delta.delta_confidence is None
        assert delta.needs_full_rewrite is False

    def test_fields_round_trip_through_model_dump(self):
        delta = FollowUpDelta(delta_confidence=0.42, needs_full_rewrite=True)
        dumped = delta.model_dump()
        assert dumped["delta_confidence"] == 0.42
        assert dumped["needs_full_rewrite"] is True
        # Existing fields untouched
        assert dumped["changed_indicator"] is None

    def test_merge_state_ignores_new_fields(self):
        """Telemetry-only contract: merge_state must NOT consume these fields.

        If this test starts failing because someone wired the fields into
        merge_state, that wiring belongs in a separate PR with shadow-mode
        telemetry per docs/DEEP_REVIEW_2026-05-30.md §6 invariant #8.
        """
        current = ConversationState(indicator="GDP", country="USA", provider="FRED")
        delta = FollowUpDelta(delta_confidence=0.2, needs_full_rewrite=True)
        merged = merge_state(current, delta)
        # State preserved — no field-level changes
        assert merged.indicator == "GDP"
        assert merged.country == "USA"
        assert merged.provider == "FRED"
