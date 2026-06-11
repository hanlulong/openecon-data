"""Multi-round conversation-state correctness regression battery.

Covers 6 verified bugs (F1–F6) where follow-up turns silently return WRONG
data. Each test is annotated FIX-PROVES (currently RED, must turn GREEN after
its fix) or DIDN'T-BREAK (currently GREEN, must STAY GREEN — guards the sacred
"preserve multi-round conversations" invariant).

Tested levels:
- F1, F2, F5: DeltaExtractor handler / entry-guard level (deterministic).
- F4, F3: merge_state / topic-change reset helper level (pure functions).
- F6: collective share-key tuple level (the key builder used at read+write).

See the task plan for the canonical 30-sequence mapping (tests 1..30).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.services.conversation_state_v2 import (
    AnswerSetMember,
    ConversationState,
    FollowUpDelta,
    merge_state,
    merge_new_state_with_previous,
    reset_state_for_topic_change,
)
from backend.services.delta_extractor import DeltaExtractor


@pytest.fixture
def extractor():
    mock_qs = MagicMock()
    # Ensure statscan_provider is falsy-safe for non-StatsCan paths
    mock_qs.statscan_provider = None
    return DeltaExtractor(mock_qs)


# ─────────────────────────────────────────────────────────────────────
# F1: currency-pair regex must NOT hijack FRED indicator follow-ups
# ─────────────────────────────────────────────────────────────────────

class TestF1CurrencyPairHijack:
    def test_1_cpi_vs_pce_no_currency_pair_dim(self, extractor):
        """FIX-PROVES: 'CPI vs PCE' after US CPI (FRED) must NOT become Currency Pair."""
        state = ConversationState(indicator="CPI", country="US", provider="FRED")
        delta = extractor._try_exchange_rate_pair_change("CPI vs PCE", state)
        assert delta is None or delta.added_dimensions is None or (
            "Currency Pair" not in (delta.added_dimensions or {})
        )

    def test_2_gdp_to_gnp_no_currency_pair_dim(self, extractor):
        """FIX-PROVES: 'GDP to GNP' after US GDP (FRED) must NOT become Currency Pair."""
        state = ConversationState(indicator="GDP", country="US", provider="FRED")
        delta = extractor._try_exchange_rate_pair_change("GDP to GNP", state)
        assert delta is None or "Currency Pair" not in (delta.added_dimensions or {})

    def test_3_exchangerate_pair_switch_still_works(self, extractor):
        """DIDN'T-BREAK: EXCHANGERATE 'GBP to USD' pair switch must STILL fire."""
        state = ConversationState(indicator="exchange rate", provider="EXCHANGERATE")
        delta = extractor._try_exchange_rate_pair_change("GBP to USD", state)
        assert delta is not None
        assert delta.added_dimensions == {"Currency Pair": "GBP to USD"}
        assert delta.is_dimension_modifier_change is True

    def test_4_exchangerate_pair_switch_vs_notation_preserved(self, extractor):
        """DIDN'T-BREAK: EXCHANGERATE 'JPY vs USD' pair switch preserved."""
        state = ConversationState(indicator="USD EUR rate", provider="EXCHANGERATE")
        delta = extractor._try_exchange_rate_pair_change("JPY vs USD", state)
        assert delta is not None
        assert delta.added_dimensions == {"Currency Pair": "JPY to USD"}


# ─────────────────────────────────────────────────────────────────────
# F2: pure-time-change entry guard must not starve indicator switches
# ─────────────────────────────────────────────────────────────────────

class TestF2TimeChangeStarvesIndicator:
    def test_5_inflation_since_2010_routes_to_indicator(self, extractor):
        """FIX-PROVES: 'inflation since 2010' must NOT be a pure time change."""
        state = ConversationState(indicator="GDP", country="US")
        # The time handler must decline (return None) because 'inflation' is a
        # residual content token that does NOT match state.indicator (GDP).
        delta = extractor._try_time_change("inflation since 2010", state)
        assert delta is None

    def test_6_unemployment_last_5_years_routes_to_indicator(self, extractor):
        """FIX-PROVES: 'unemployment last 5 years' must NOT be a pure time change."""
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor._try_time_change("unemployment last 5 years", state)
        assert delta is None

    def test_7_exports_range_routes_to_indicator(self, extractor):
        """FIX-PROVES: 'exports 2010-2020' must NOT be a pure time change vs GDP."""
        state = ConversationState(indicator="GDP", country="CA")
        delta = extractor._try_time_change("exports 2010-2020", state)
        assert delta is None

    def test_8_since_2010_preserves_gdp(self, extractor):
        """DIDN'T-BREAK: pure 'since 2010' (no content token) must still fire."""
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor._try_time_change("since 2010", state)
        assert delta is not None
        assert delta.changed_start_date == "2010-01-01"

    def test_9_last_20_years_preserves_gdp(self, extractor):
        """DIDN'T-BREAK: pure 'last 20 years' must still fire."""
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor._try_time_change("last 20 years", state)
        assert delta is not None
        assert delta.delta_type == "time_change"
        assert delta.changed_start_date is not None

    def test_10_from_2000_to_2015_preserves_gdp(self, extractor):
        """DIDN'T-BREAK: pure 'from 2000 to 2015' must still fire."""
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor._try_time_change("from 2000 to 2015", state)
        assert delta is not None
        assert delta.changed_start_date == "2000-01-01"
        assert delta.changed_end_date == "2015-12-31"

    def test_2b_indicator_token_alias_does_not_block_pure_time(self, extractor):
        """DIDN'T-BREAK: time change that restates current indicator alias still fires.

        'GDP last 10 years' when state.indicator is 'GDP' — the residual token
        DOES match state.indicator, so it remains a pure time change.
        """
        state = ConversationState(indicator="GDP", country="US")
        delta = extractor._try_time_change("GDP last 10 years", state)
        assert delta is not None
        assert delta.delta_type == "time_change"


# ─────────────────────────────────────────────────────────────────────
# F3: topic-change / new_query must reset stale provider-scoped context
#      gated on a corroborating topic-change signal (provider-family +
#      concept change), NEVER on the bare new_query label.
# ─────────────────────────────────────────────────────────────────────

class TestF3TopicChangeReset:
    def test_11_trade_to_crypto_resets_trade_and_dates(self):
        """FIX-PROVES: COMTRADE trade → Bitcoin price (COINGECKO): trade None,
        dates reset, provider not locked."""
        previous = ConversationState(
            indicator="exports",
            country="US",
            provider="COMTRADE",
            provider_locked=True,
            trade_flow="EXPORT",
            trade_reporter="US",
            trade_partner="CN",
            start_date="2015-01-01",
            end_date="2020-12-31",
            turn_number=2,
        )
        new_state = ConversationState(
            indicator="bitcoin price",
            provider="COINGECKO",
            coin_ids=["bitcoin"],
        )
        merged = merge_new_state_with_previous(new_state, previous)
        merged = reset_state_for_topic_change(merged, previous, llm_new_query=True)
        assert merged.trade_flow is None
        assert merged.trade_reporter is None
        assert merged.trade_partner is None
        assert merged.start_date is None
        assert merged.end_date is None
        assert merged.provider_locked is False

    def test_12_statscan_decomp_to_stock_not_leaked(self):
        """FIX-PROVES: STATSCAN provincial decomposition → Tesla stock: no
        STATSCAN decomposition / product leaks forward."""
        previous = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            provider_locked=True,
            statscan_product_id="14100287",
            statscan_cube_metadata={"dimension": []},
            decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
            turn_number=2,
        )
        new_state = ConversationState(
            indicator="Tesla stock price",
            provider="ALPHAVANTAGE",
        )
        merged = merge_new_state_with_previous(new_state, previous)
        merged = reset_state_for_topic_change(merged, previous, llm_new_query=True)
        assert merged.decomposition is None
        assert merged.statscan_product_id is None
        assert merged.statscan_cube_metadata is None
        assert merged.provider_locked is False

    def test_13_fred_to_other_geography_reroutes(self):
        """FIX-PROVES: 'US GDP from FRED' → 'Germany population' must not stay
        locked to FRED (provider family unchanged here is acceptable, but the
        provider lock from FRED must release on a true topic change)."""
        previous = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            provider_locked=True,
            turn_number=1,
        )
        # Germany population routes to WorldBank — different provider family.
        new_state = ConversationState(
            indicator="population",
            country="DE",
            provider="WORLDBANK",
        )
        merged = merge_new_state_with_previous(new_state, previous)
        merged = reset_state_for_topic_change(merged, previous, llm_new_query=True)
        assert merged.provider_locked is False
        # New provider/country/indicator must win (explicit values in new_state)
        assert merged.country == "DE"
        assert merged.provider == "WORLDBANK"

    def test_14_related_indicator_switch_keeps_dates_and_country(self):
        """DIDN'T-BREAK (SACRED): 'US GDP 2010-2020' → 'show unemployment'
        (LLM may mislabel new_query). Dates 2010-2020 + country US MUST persist."""
        previous = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            start_date="2010-01-01",
            end_date="2020-12-31",
            turn_number=1,
        )
        # Same provider family (US macro), only the indicator concept changed.
        new_state = ConversationState(
            indicator="unemployment",
            provider="FRED",
        )
        merged = merge_new_state_with_previous(new_state, previous)
        merged = reset_state_for_topic_change(merged, previous, llm_new_query=True)
        assert merged.start_date == "2010-01-01"
        assert merged.end_date == "2020-12-31"
        assert merged.country == "US"
        assert merged.indicator == "unemployment"

    def test_15_for_japan_keeps_gdp(self):
        """DIDN'T-BREAK: 'US GDP' → 'for Japan' (country-only). GDP preserved,
        no destructive reset (this is not even labeled new_query)."""
        previous = ConversationState(
            indicator="GDP",
            country="US",
            provider="FRED",
            turn_number=1,
        )
        new_state = ConversationState(
            indicator="GDP",
            country="JP",
            provider="FRED",
        )
        merged = merge_new_state_with_previous(new_state, previous)
        merged = reset_state_for_topic_change(merged, previous, llm_new_query=False)
        assert merged.country == "JP"
        assert merged.indicator == "GDP"


# ─────────────────────────────────────────────────────────────────────
# F4: stale statscan_product_id must die on indicator AND provider change
# ─────────────────────────────────────────────────────────────────────

class TestF4StatscanProductIdSurvives:
    def test_16_indicator_change_clears_product_id(self):
        """FIX-PROVES: Canada CPI (STATSCAN, product) → 'what about GDP':
        statscan_product_id must be cleared so it re-resolves to GDP."""
        state = ConversationState(
            indicator="CPI",
            country="CA",
            provider="STATSCAN",
            statscan_product_id="18100004",
            statscan_cube_metadata={"dimension": []},
        )
        delta = FollowUpDelta(
            changed_indicator="GDP",
            delta_type="indicator_switch",
        )
        merged = merge_state(state, delta)
        assert merged.statscan_product_id is None
        assert merged.statscan_cube_metadata is None

    def test_17_indicator_switch_reresolves_product(self):
        """FIX-PROVES: 'unemployment rate' → 'switch to housing starts'."""
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            statscan_product_id="14100287",
        )
        delta = FollowUpDelta(
            changed_indicator="housing starts",
            delta_type="indicator_switch",
        )
        merged = merge_state(state, delta)
        assert merged.statscan_product_id is None

    def test_18_provider_switch_drops_product_id(self):
        """FIX-PROVES: Canada CPI (STATSCAN) → 'switch to FRED': product_id
        dropped on the provider-change path too."""
        state = ConversationState(
            indicator="CPI",
            country="CA",
            provider="STATSCAN",
            statscan_product_id="18100004",
            statscan_cube_metadata={"dimension": []},
        )
        delta = FollowUpDelta(
            changed_provider="FRED",
            delta_type="provider_change",
        )
        merged = merge_state(state, delta)
        assert merged.statscan_product_id is None

    def test_19_dimension_modifier_preserves_product_and_decomposition(self):
        """DIDN'T-BREAK: 'unemployment by province' → 'for 2018-2022':
        product_id + decomposition preserved (time change is non-destructive)."""
        state = ConversationState(
            indicator="unemployment rate",
            country="CA",
            provider="STATSCAN",
            statscan_product_id="14100287",
            decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
        )
        delta = FollowUpDelta(
            changed_start_date="2018-01-01",
            changed_end_date="2022-12-31",
            delta_type="time_change",
        )
        merged = merge_state(state, delta)
        assert merged.statscan_product_id == "14100287"
        assert merged.decomposition == {
            "type": "provinces",
            "entities": None,
            "axis": "Geography",
        }

    def test_20_dimension_modifier_indicator_preserves_product(self):
        """DIDN'T-BREAK: Canada CPI → 'show energy' (dimension modifier):
        product_id preserved (is_dimension_modifier_change=True)."""
        state = ConversationState(
            indicator="CPI",
            country="CA",
            provider="STATSCAN",
            statscan_product_id="18100004",
            statscan_cube_metadata={"dimension": []},
        )
        delta = FollowUpDelta(
            added_dimensions={"products": "Energy"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.statscan_product_id == "18100004"
        assert merged.dimensions == {"products": "Energy"}


# ─────────────────────────────────────────────────────────────────────
# F5: country name vs ISO2 mismatch must not null valid country changes
# ─────────────────────────────────────────────────────────────────────

class TestF5CountryNameIso2Mismatch:
    def test_21_germany_from_eurostat_keeps_country(self, extractor):
        """FIX-PROVES: 'Germany unemployment from Eurostat' — provider change
        sanitizer must keep changed_country=GERMANY (normalizes to DE)."""
        delta = FollowUpDelta(
            changed_provider="EUROSTAT",
            changed_country="GERMANY",
            delta_type="provider_change",
        )
        updated = extractor._sanitize_provider_change_geography_artifacts(
            delta, "Germany unemployment from Eurostat"
        )
        assert updated.changed_country is not None
        assert str(updated.changed_country).upper() in {"GERMANY", "DE"}

    def test_22_japan_from_world_bank_keeps_country(self, extractor):
        """FIX-PROVES: 'Japan GDP from World Bank' (LLM emits 'JAPAN')."""
        delta = FollowUpDelta(
            changed_provider="WORLDBANK",
            changed_country="JAPAN",
            delta_type="provider_change",
        )
        updated = extractor._sanitize_provider_change_geography_artifacts(
            delta, "Japan GDP from World Bank"
        )
        assert updated.changed_country is not None
        assert str(updated.changed_country).upper() in {"JAPAN", "JP"}

    def test_22b_changed_countries_name_form_kept(self, extractor):
        """FIX-PROVES: changed_countries list with names survives sanitize."""
        delta = FollowUpDelta(
            changed_provider="WORLDBANK",
            changed_countries=["GERMANY", "FRANCE"],
            delta_type="provider_change",
        )
        updated = extractor._sanitize_provider_change_geography_artifacts(
            delta, "Germany and France GDP from World Bank"
        )
        assert updated.changed_countries
        upper = {str(c).upper() for c in updated.changed_countries}
        assert upper <= {"GERMANY", "FRANCE", "DE", "FR"}
        assert len(updated.changed_countries) == 2

    def test_23_world_bank_switch_no_geo_preserves_us(self, extractor):
        """DIDN'T-BREAK: 'US GDP from FRED' → 'use World Bank' (no geography in
        the follow-up): sanitizer must NOT inject/keep a bogus geography."""
        delta = FollowUpDelta(
            changed_provider="WORLDBANK",
            delta_type="provider_change",
        )
        updated = extractor._sanitize_provider_change_geography_artifacts(
            delta, "use World Bank"
        )
        # No country in the follow-up → changed_country stays None (carried
        # forward US comes from previous state, not from this delta).
        assert updated.changed_country is None

    def test_24_from_imf_no_geo_preserves_de(self, extractor):
        """DIDN'T-BREAK: 'Germany GDP' → 'from IMF': sanitizer keeps no spurious
        geography; the artifact filter must not crash on a code-form country."""
        delta = FollowUpDelta(
            changed_provider="IMF",
            changed_country="1W",  # a non-mentioned aggregate artifact
            delta_type="provider_change",
        )
        updated = extractor._sanitize_provider_change_geography_artifacts(
            delta, "from IMF"
        )
        assert updated.changed_country is None

    def test_24b_artifact_country_not_in_query_is_dropped(self, extractor):
        """DIDN'T-BREAK: an LLM-hallucinated country not present in the query
        text is still dropped (the original purpose of the sanitizer)."""
        delta = FollowUpDelta(
            changed_provider="EUROSTAT",
            changed_country="FRANCE",
            delta_type="provider_change",
        )
        updated = extractor._sanitize_provider_change_geography_artifacts(
            delta, "Germany unemployment from Eurostat"
        )
        # FRANCE is not mentioned → must be dropped.
        assert updated.changed_country is None


# ─────────────────────────────────────────────────────────────────────
# F6: collective delta share-key must include country
# ─────────────────────────────────────────────────────────────────────

def _make_member(provider, label, country, code=None):
    return AnswerSetMember(
        provider=provider,
        indicator_label=label,
        provider_code=code,
        series_id=code,
        country=country,
        countries=[country] if country else None,
    )


class TestF6CollectiveShareKey:
    """The collective share-key restricts code reuse. For geography-encoded
    providers (FRED/StatsCan), a code is country-specific, so the key MUST
    include the member's canonical country. For country-agnostic providers
    (WorldBank/IMF), including the country is harmless (keyed per country) and
    must still let the WB/IMF code resolve correctly per country.
    """

    @staticmethod
    def _share_key(qs, member, label_key):
        from backend.utils.providers import normalize_provider_name
        provider_key = normalize_provider_name(str(getattr(member, "provider", "") or ""))
        country_key = qs._collective_share_country_key(member)
        return (provider_key, country_key, label_key)

    @pytest.fixture
    def qs(self):
        from backend.services.query import QueryService
        return QueryService.__new__(QueryService)

    def test_25_fred_members_do_not_share_code_across_countries(self, qs):
        """FIX-PROVES: US + Germany unemployment (FRED collective). The two
        members must NOT collapse to one share-key (which would force Germany
        to reuse US's UNRATE)."""
        us = _make_member("FRED", "unemployment rate", "US", code="UNRATE")
        de = _make_member("FRED", "unemployment rate", "DE", code="LRHUTTTTDEM156S")
        label_key = "unemployment rate"
        k_us = self._share_key(qs, us, label_key)
        k_de = self._share_key(qs, de, label_key)
        assert k_us != k_de

    def test_26_per_capita_keys_per_country(self, qs):
        """FIX-PROVES: US, Canada, Mexico GDP → per-capita. Each member keyed
        for its own country (no cross-country code collapse)."""
        members = [
            _make_member("FRED", "GDP per capita", "US"),
            _make_member("FRED", "GDP per capita", "CA"),
            _make_member("FRED", "GDP per capita", "MX"),
        ]
        keys = {self._share_key(qs, m, "gdp per capita") for m in members}
        assert len(keys) == 3

    def test_27_world_bank_code_reuse_across_countries(self, qs):
        """DIDN'T-BREAK: US + India GDP from World Bank → GDP growth. The WB
        code is country-agnostic (country is a separate param), so the SAME
        code must still be shared across countries — the share-key country
        component is empty for country-agnostic providers."""
        us = _make_member("WORLDBANK", "GDP growth", "US", code="NY.GDP.MKTP.KD.ZG")
        ind = _make_member("WORLDBANK", "GDP growth", "IN", code="NY.GDP.MKTP.KD.ZG")
        label_key = "gdp growth"
        k_us = self._share_key(qs, us, label_key)
        k_in = self._share_key(qs, ind, label_key)
        # IDENTICAL keys → WB code shared across countries (no per-country split,
        # so one resolution serves every country, exactly as before F6).
        assert k_us == k_in
        assert k_us[0] == "WORLDBANK"
        assert k_us[1] == ""  # country-agnostic → empty country component
        assert k_us[2] == label_key


# ─────────────────────────────────────────────────────────────────────
# Cross-cutting guards (28, 29, 30)
# ─────────────────────────────────────────────────────────────────────

class TestCrossCutting:
    def test_28_frequency_change_preserves_else(self):
        state = ConversationState(indicator="GDP", country="US", frequency="quarterly")
        delta = FollowUpDelta(changed_frequency="monthly")
        merged = merge_state(state, delta)
        assert merged.frequency == "monthly"
        assert merged.indicator == "GDP"
        assert merged.country == "US"

    def test_29_trade_flow_flip_preserves_reporter_partner(self):
        state = ConversationState(
            indicator="exports",
            provider="COMTRADE",
            trade_flow="EXPORT",
            trade_reporter="US",
            trade_partner="CN",
        )
        delta = FollowUpDelta(changed_trade_flow="IMPORT")
        merged = merge_state(state, delta)
        assert merged.trade_flow == "IMPORT"
        assert merged.trade_reporter == "US"
        assert merged.trade_partner == "CN"

    def test_30_dimension_filter_preserves_product_id(self):
        state = ConversationState(
            indicator="CPI",
            country="CA",
            provider="STATSCAN",
            statscan_product_id="18100004",
            dimensions={"products": "All-items"},
        )
        delta = FollowUpDelta(
            added_dimensions={"products": "Food"},
            is_dimension_modifier_change=True,
            delta_type="dimension_change",
        )
        merged = merge_state(state, delta)
        assert merged.statscan_product_id == "18100004"
        assert merged.dimensions == {"products": "Food"}
