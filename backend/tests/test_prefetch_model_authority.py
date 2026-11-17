"""Model decisions, not title spelling, authorize prefetch direct answers."""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

import backend.services.indicator_clarification as clarification
from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.cache import cache_service
from backend.services.indicator_resolution import is_resolved_indicator_plausible
from backend.services.indicator_selector import FREQUENCY_MISMATCH_PARAM, SelectionResult
from backend.services.query import QueryService
from backend.tests.utils import run


EMPLOYMENT_CODE = "SL.EMP.TOTL.SP.ZS"
EMPLOYMENT_TITLE = "Employment to population ratio, 15+, total (%) (modeled ILO estimate)"
QUERY = "employment rate for Peru"


@pytest.fixture
def service(monkeypatch):
    cache_service.clear()
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    monkeypatch.setattr(svc.settings, "use_outcome_decision_stage", False)
    # Isolate the retrieval wording and optional option-discovery dependencies;
    # the real plausibility predicate and adjudication boundary remain intact.
    monkeypatch.setattr(svc, "_select_indicator_query_for_resolution", Mock(return_value="employment rate"))
    monkeypatch.setattr(svc, "_collect_indicator_choice_options", Mock(return_value=[]))
    monkeypatch.setattr(svc, "_get_fallback_providers", Mock(return_value=[]))
    return svc


def intent(**params):
    return ParsedIntent(
        apiProvider="WORLDBANK", indicators=["employment rate"],
        parameters={"country": "PE", **params}, clarificationNeeded=False,
        originalQuery=QUERY,
    )


def prefetch(svc, parsed):
    return run(svc._build_prefetch_indicator_choice_clarification(
        conversation_id="prefetch-authority-test", query=parsed.originalQuery,
        intent=parsed, explicit_provider=None, is_multi_indicator=False,
    ))


def model_pick(**kwargs):
    return SelectionResult(code=EMPLOYMENT_CODE, name=EMPLOYMENT_TITLE, source="llm_pick", **kwargs)


def assert_no_new_pick_authority(parsed):
    assert not parsed.parameters.get("__authoritative_pick_code")
    assert not parsed.parameters.get("__authoritative_pick_provider")


def assert_pick_authority(parsed):
    assert parsed.parameters["__authoritative_pick_code"] == EMPLOYMENT_CODE
    assert parsed.parameters["__authoritative_pick_provider"] == "WORLDBANK"


def employment_series(*, country="Peru", source="World Bank", code=EMPLOYMENT_CODE,
                      title=EMPLOYMENT_TITLE):
    return NormalizedData(
        metadata=Metadata(source=source, country=country, seriesId=code,
                          indicator=title, frequency="annual", unit="Percent"),
        data=[{"date": "2024-01-01", "value": 65.0}],
    )


def picked_intent(**params):
    parsed = intent(indicator=EMPLOYMENT_CODE, __semantic_authority="llm_adjudication",
                    __authoritative_pick_code=EMPLOYMENT_CODE,
                    __authoritative_pick_provider="WORLDBANK", **params)
    parsed.confidence = 0.85
    return parsed


def gate(service, parsed, data):
    outcome = service._needs_indicator_clarification(parsed.originalQuery, data, parsed)
    return outcome, clarification._LAST_GATE_BRANCH.get()


def fetch_employment(service, parsed):
    with patch.object(service, "_get_from_cache", new=AsyncMock(return_value=None)), \
         patch.object(service, "_save_to_cache", new=AsyncMock()), \
         patch.object(service.world_bank_provider, "fetch_indicator", new=AsyncMock(return_value=[employment_series()])) as fetch:
        fetched = run(service._fetch_data(parsed))
    fetch.assert_awaited_once()
    assert fetched and fetched[0].metadata.seriesId == EMPLOYMENT_CODE
    return fetched


def test_model_pick_is_not_vetoed_by_employment_ratio_title(service):
    parsed = intent()
    # Establish the original failure mechanism rather than bypassing it.
    assert not is_resolved_indicator_plausible(
        service, "WORLDBANK", "employment rate", EMPLOYMENT_CODE, EMPLOYMENT_TITLE,
    )
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=model_pick())) as select:
        response = prefetch(service, parsed)
    assert response is None
    select.assert_awaited_once()
    assert parsed.parameters["indicator"] == EMPLOYMENT_CODE
    assert parsed.parameters["__semantic_authority"] == "llm_adjudication"
    assert parsed.parameters["country"] == "PE"
    assert_pick_authority(parsed)


@pytest.mark.parametrize("selection_kwargs", [
    {"made_under_prefer_ask": True},
    {"metadata_mismatch": {"requested_frequency": "monthly", "native_frequency": "annual"}},
])
def test_non_authoritative_pick_clears_stale_stronger_authority(service, selection_kwargs):
    parsed = intent(__authoritative_pick_code="OLD.CODE", __authoritative_pick_provider="IMF")
    selection = model_pick(**selection_kwargs)
    assert not selection.authoritative
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=selection)):
        assert prefetch(service, parsed) is None
    assert parsed.parameters["indicator"] == EMPLOYMENT_CODE
    assert parsed.parameters["__semantic_authority"] == "llm_adjudication"
    assert_no_new_pick_authority(parsed)
    assert gate(service, parsed, [employment_series()])[0] is True


def test_new_strong_pick_replaces_stale_authority_pair(service):
    parsed = intent(__authoritative_pick_code="OLD.CODE", __authoritative_pick_provider="IMF")
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=model_pick())):
        assert prefetch(service, parsed) is None
    assert_pick_authority(parsed)


@pytest.mark.parametrize("source", ["unknown", "llm_reject", "user_choice", "metadata_conflict"])
def test_non_pick_sources_cannot_authorize_an_incidental_code(service, source):
    parsed = intent(__authoritative_pick_code="OLD.CODE", __authoritative_pick_provider="IMF")
    selection = SelectionResult(code=EMPLOYMENT_CODE, name=EMPLOYMENT_TITLE, source=source)
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=selection)):
        response = prefetch(service, parsed)
    assert response is not None
    assert not response.data
    assert not parsed.parameters.get("indicator")
    assert not parsed.parameters.get("__semantic_authority")
    assert_no_new_pick_authority(parsed)


@pytest.mark.parametrize("code", [None, "", "  ", "UNKNOWN", "N/A", "none", "--"])
def test_empty_and_placeholder_model_picks_cannot_gain_authority(service, code):
    parsed = intent(__authoritative_pick_code="OLD.CODE", __authoritative_pick_provider="IMF")
    selection = SelectionResult(code=code, name=EMPLOYMENT_TITLE, source="llm_pick")
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=selection)):
        response = prefetch(service, parsed)
    assert response is not None
    assert not response.data
    assert not parsed.parameters.get("indicator")
    assert not parsed.parameters.get("__semantic_authority")
    assert_no_new_pick_authority(parsed)


def test_failed_selector_cannot_mint_authority(service):
    parsed = intent(__authoritative_pick_code="OLD.CODE", __authoritative_pick_provider="IMF")
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(side_effect=RuntimeError("selector unavailable"))):
        response = prefetch(service, parsed)
    assert response is not None
    assert not response.data
    assert not parsed.parameters.get("indicator")
    assert not parsed.parameters.get("__semantic_authority")
    assert_no_new_pick_authority(parsed)


def test_ask_clears_stale_pick_authority_without_serving_a_menu_member(service):
    parsed = intent(__authoritative_pick_code="OLD.CODE", __authoritative_pick_provider="IMF")
    selection = SelectionResult(source="user_choice", options=[
        {"code": EMPLOYMENT_CODE, "name": EMPLOYMENT_TITLE},
        {"code": "SL.EMP.TOTL.SP.NE.ZS", "name": "Employment ratio (national estimate)"},
    ])
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=selection)), \
         patch.object(service, "_filter_viable_indicator_choice_options", new=AsyncMock(side_effect=lambda **kwargs: kwargs["options"])) as viability:
        response = prefetch(service, parsed)
    assert response is not None
    assert not response.data
    assert not parsed.parameters.get("indicator")
    assert_no_new_pick_authority(parsed)
    viability.assert_awaited_once()


@pytest.mark.parametrize("authority", ["llm_adjudication", "post_fetch_semantic_judge", "exact_user_input"])
def test_trusted_current_code_does_not_return_to_lexical_selection(service, authority):
    parsed = intent(indicator=EMPLOYMENT_CODE, __semantic_indicator_label=EMPLOYMENT_TITLE,
                    __semantic_authority=authority)
    before = parsed.parameters.copy()
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(side_effect=AssertionError("trusted code must not be reselected"))) as select:
        response = prefetch(service, parsed)
    assert response is None
    select.assert_not_awaited()
    service._collect_indicator_choice_options.assert_not_called()
    assert parsed.parameters == before
    assert_no_new_pick_authority(parsed)


@pytest.mark.parametrize(
    "delta_resolved,indicator_changed,trusted",
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_verified_state_authority_requires_unchanged_delta(service, delta_resolved, indicator_changed, trusted):
    parsed = intent(indicator=EMPLOYMENT_CODE, __semantic_indicator_label=EMPLOYMENT_TITLE,
                    __semantic_authority="verified_conversation_state",
                    __delta_resolved=delta_resolved, __delta_indicator_changed=indicator_changed)
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(side_effect=AssertionError("existing code is not a fresh pick"))) as select:
        response = prefetch(service, parsed)
    assert (response is None) is trusted
    if not trusted:
        assert not response.data
    select.assert_not_awaited()
    assert_no_new_pick_authority(parsed)


@pytest.mark.parametrize("purpose", ["_prefetch_option_validation", "_uncertain_recovery_attempted"])
def test_candidate_validation_cannot_borrow_prior_semantic_authority(service, purpose):
    parsed = intent(indicator=EMPLOYMENT_CODE, __semantic_indicator_label=EMPLOYMENT_TITLE,
                    __semantic_authority="llm_adjudication", **{purpose: True})
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(side_effect=AssertionError("candidate validation is not a new selection"))):
        response = prefetch(service, parsed)
    assert response is not None
    assert not response.data
    assert_no_new_pick_authority(parsed)


def test_literal_native_code_keeps_existing_preflight_semantics(service):
    parsed = ParsedIntent(apiProvider="FRED", indicators=["UNRATE"],
                          parameters={"country": "US", "seriesId": "UNRATE"},
                          clarificationNeeded=False, originalQuery="show me UNRATE from FRED")
    before = parsed.parameters.copy()
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(side_effect=AssertionError("literal native code must not be reselected"))) as select:
        response = prefetch(service, parsed)
    assert response is None
    select.assert_not_awaited()
    assert parsed.parameters == before


@pytest.mark.parametrize("mismatch", [None, {"requested_frequency": "monthly", "native_frequency": "annual"}])
def test_selector_frequency_provenance_limits_stronger_authority(service, mismatch):
    parsed = intent(**{FREQUENCY_MISMATCH_PARAM: {"requested_frequency": "daily", "native_frequency": "monthly"}})
    selection = model_pick(metadata_mismatch=mismatch)
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=selection)):
        response = prefetch(service, parsed)
    assert response is None
    if mismatch:
        assert parsed.parameters[FREQUENCY_MISMATCH_PARAM] == mismatch
        assert parsed.parameters[FREQUENCY_MISMATCH_PARAM] is not selection.metadata_mismatch
        assert_no_new_pick_authority(parsed)
    else:
        assert FREQUENCY_MISMATCH_PARAM not in parsed.parameters
        assert_pick_authority(parsed)


def test_low_parse_confidence_keeps_correct_model_pick_through_fetch_and_uncertainty(service):
    parsed = intent(startDate="2020-01-01", endDate="2024-12-31", __time_scope_authority="user")
    parsed.confidence = 0.85
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=model_pick())):
        assert prefetch(service, parsed) is None
    assert_pick_authority(parsed)
    fetched = fetch_employment(service, parsed)
    assert_pick_authority(parsed)
    outcome, branch = gate(service, parsed, fetched)
    assert outcome is False, branch


@pytest.mark.parametrize("selection_kwargs", [
    {"made_under_prefer_ask": True},
    {"metadata_mismatch": {"requested_frequency": "monthly", "native_frequency": "annual"}},
])
def test_fetch_preserves_provenance_without_promoting_a_weaker_pick(service, selection_kwargs):
    parsed = intent(startDate="2020-01-01", endDate="2024-12-31", __time_scope_authority="user")
    parsed.confidence = 0.85
    selection = model_pick(**selection_kwargs)
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=selection)):
        assert prefetch(service, parsed) is None
    assert_no_new_pick_authority(parsed)
    fetched = fetch_employment(service, parsed)
    assert_no_new_pick_authority(parsed)
    if selection.metadata_mismatch:
        annotation = parsed.parameters[FREQUENCY_MISMATCH_PARAM]
        assert annotation == selection.metadata_mismatch
        assert annotation is not selection.metadata_mismatch
    assert gate(service, parsed, fetched)[0] is True


@pytest.mark.parametrize("stale", [
    {"__authoritative_pick_code": "OLD.CODE"},
    {"__authoritative_pick_provider": "IMF"},
    {"__semantic_authority": "post_fetch_semantic_judge"},
    {"__semantic_authority": "exact_user_input"},
    {"__semantic_authority": "verified_conversation_state", "__delta_resolved": True,
     "__delta_indicator_changed": False},
    {"_uncertain_recovery_attempted": True},
    {FREQUENCY_MISMATCH_PARAM: {"requested_frequency": "monthly", "native_frequency": "annual"}},
])
def test_trusted_resolver_drops_ineligible_stronger_authority(service, stale):
    parsed = picked_intent()
    parsed.parameters.update(stale)
    resolved = run(service._resolve_indicator_for_fetch("WORLDBANK", parsed, parsed.parameters))
    assert resolved["indicator"] == EMPLOYMENT_CODE
    assert_no_new_pick_authority(parsed)
    if FREQUENCY_MISMATCH_PARAM in stale:
        assert resolved[FREQUENCY_MISMATCH_PARAM] == stale[FREQUENCY_MISMATCH_PARAM]
        assert resolved[FREQUENCY_MISMATCH_PARAM] is not stale[FREQUENCY_MISMATCH_PARAM]


@pytest.mark.parametrize("query,untrusted_branch", [
    (QUERY, "strict_cue_low_score"),
    ("labor utilization for Peru", "low_top_score"),
])
def test_own_served_strong_pick_survives_each_soft_score_gate(service, query, untrusted_branch):
    parsed = intent(indicator=EMPLOYMENT_CODE)
    parsed.originalQuery = query
    parsed.indicators = [query]
    parsed.confidence = 0.85
    data = [employment_series()]
    assert gate(service, parsed, data) == (True, untrusted_branch)
    parsed.parameters.update(__authoritative_pick_code=EMPLOYMENT_CODE,
                             __authoritative_pick_provider="WORLDBANK")
    outcome, branch = gate(service, parsed, data)
    assert outcome is False, branch
    assert branch.startswith("selection_authority_")


@pytest.mark.parametrize("source,code,include_pick", [
    ("FRED", EMPLOYMENT_CODE, False),
    ("World Bank", "SL.EMP.TOTL.SP.MA.ZS", False),
    ("FRED", EMPLOYMENT_CODE, True),
    ("World Bank", "SL.EMP.TOTL.SP.MA.ZS", True),
])
def test_soft_gate_requires_every_served_provider_and_code_to_match_pick(service, source, code, include_pick):
    parsed = picked_intent()
    substituted = employment_series(source=source, code=code)
    data = [employment_series(), substituted] if include_pick else [substituted]
    outcome, branch = gate(service, parsed, data)
    assert outcome is True, branch
    assert not branch.startswith("selection_authority_")


@pytest.mark.parametrize("requested,served", [
    ({"country": "PE"}, ["Colombia"]),
    ({"country": "PE"}, [None]),
    ({"country": "PE"}, [""]),
    ({"country": "PE"}, ["Atlantis"]),
    ({"country": "PE"}, ["Peru", "Colombia"]),
    ({"country": None}, ["Peru"]),
    ({"country": "Atlantis"}, ["Peru"]),
    ({"country": None, "countries": ["PE", "CO"]}, ["Peru"]),
    ({"country": None, "countries": ["PE", "CO"]}, ["Peru", None]),
    ({"country": None, "countries": ["PE", "Atlantis"]}, ["Peru"]),
])
def test_soft_gate_does_not_bypass_unknown_or_inexact_country_coverage(service, requested, served):
    parsed = picked_intent(**requested)
    if not requested.get("country") and not requested.get("countries"):
        parsed.originalQuery = "employment rate"
    outcome, branch = gate(service, parsed, [employment_series(country=country) for country in served])
    assert outcome is True, branch
    assert not branch.startswith("selection_authority_")


def test_soft_gate_accepts_complete_multi_country_union(service):
    parsed = picked_intent(country=None, countries=["PE", "CO"])
    parsed.originalQuery = "employment rate for Peru and Colombia"
    data = [employment_series(country="Colombia"), employment_series(country="PE")]
    outcome, branch = gate(service, parsed, data)
    assert outcome is False, branch
    assert branch.startswith("selection_authority_")


def test_soft_gate_can_derive_known_requested_country_from_query(service):
    parsed = picked_intent(country=None)
    outcome, branch = gate(service, parsed, [employment_series()])
    assert outcome is False, branch
    assert branch.startswith("selection_authority_")


@pytest.mark.parametrize("purpose", ["_prefetch_option_validation", "_uncertain_recovery_attempted"])
@pytest.mark.parametrize("confidence", [0.85, 0.95])
def test_soft_gate_still_judges_candidates_with_inherited_pick_flags(service, purpose, confidence):
    parsed = picked_intent(**{purpose: True})
    parsed.confidence = confidence
    outcome, branch = gate(service, parsed, [employment_series()])
    assert outcome is True, branch
    assert not branch.startswith("selection_authority_")


def test_soft_gate_retains_region_refusal(service):
    parsed = picked_intent()
    parsed.subnationalRegion = "Lima"
    assert gate(service, parsed, [employment_series()])[0] is True


@pytest.mark.parametrize("code,title,expected_branch", [
    ("FP.CPI.TOTL.ZG", EMPLOYMENT_TITLE, "concept_mismatch_mapped"),
    ("UNMAPPED.TEST", "Gross domestic product", "concept_mismatch_inferred"),
])
def test_strong_pick_does_not_bypass_hard_catalog_concept_conflict(service, code, title, expected_branch):
    parsed = picked_intent()
    parsed.parameters.update(indicator=code, __authoritative_pick_code=code)
    data = [employment_series(code=code, title=title)]
    assert gate(service, parsed, data) == (True, expected_branch)


def test_strong_pick_does_not_bypass_hard_debt_ratio_guard(service):
    parsed = ParsedIntent(
        apiProvider="IMF", indicators=["GGXWDG_NGDP"],
        parameters={"country": "US", "indicator": "GGXWDG_NGDP",
                    "__authoritative_pick_code": "GGXWDG_NGDP",
                    "__authoritative_pick_provider": "IMF"},
        clarificationNeeded=False, confidence=0.85, originalQuery="US debt to GDP ratio",
    )
    data = [employment_series(country="United States", source="IMF", code="GGXWDG_NGDP",
                              title="General government gross debt")]
    assert gate(service, parsed, data) == (True, "debt_ratio_cue")


@pytest.mark.parametrize("purpose", ["_prefetch_option_validation", "_uncertain_recovery_attempted"])
def test_near_tie_cannot_borrow_authority_for_candidate_validation(service, monkeypatch, purpose):
    # Fix only the comparative scores/cues to exercise the existing near-tie
    # seam; the authority and purpose checks remain the real implementation.
    monkeypatch.setattr(clarification, "_score_series_relevance", lambda query, series:
                        0.5 if series.metadata.seriesId == EMPLOYMENT_CODE else 0.4)
    monkeypatch.setattr(clarification, "_extract_indicator_cues", lambda text:
                        {"top"} if "TOP" in text else {"second"} if "SECOND" in text else set())
    parsed = picked_intent(**{purpose: True})
    parsed.originalQuery = "labor utilization for Peru"
    parsed.indicators = ["labor utilization"]
    data = [employment_series(title="TOP"), employment_series(code="OTHER", title="SECOND")]
    outcome, branch = gate(service, parsed, data)
    assert outcome is True, branch
    assert branch == "near_tie_no_authority"
