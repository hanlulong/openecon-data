"""A collection preserves requested meanings independently of execution codes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import IndicatorSpec, Metadata, NormalizedData, ParsedIntent, indicator_specs_from_intent
from backend.services import data_fetcher as df
from backend.services.conversation_state_v2 import (
    ConversationState, FollowUpDelta, build_answer_set_members_from_data,
    extract_state_from_intent, materialize_intent, merge_state,
)
from backend.utils.retry import DataNotAvailableError, ProviderUnavailable, ResultVerificationUnavailable


def series(label, country="RU", source="World Bank"):
    return NormalizedData(
        metadata=Metadata(source=source, indicator=label, seriesId=label.upper(),
                          country=country, frequency="annual", unit="Percent"),
        data=[{"date": "2024-01-01", "value": 4.0}],
    )


def parsed(labels=None, **params):
    return ParsedIntent(apiProvider="WORLDBANK", indicators=labels or ["GDP", "GDP growth", "GDP per capita"],
                        parameters={"country": "RU", "startDate": "2020-01-01", "endDate": "2024-12-31", **params},
                        clarificationNeeded=False, language="zh", originalQuery="three economic measures")


def service(explicit=None):
    return SimpleNamespace(
        _detect_explicit_provider=lambda _q: explicit,
        _normalize_provider_alias=lambda p: p,
        _select_routed_provider=AsyncMock(return_value="WORLDBANK"),
        _try_with_fallback=AsyncMock(side_effect=DataNotAvailableError("no fallback")),
        _verify_execution_result=AsyncMock(return_value=None),
        _build_minimal_execution_plan=lambda *_a: None,
    )


def test_added_indicator_and_global_frequency_do_not_erase_requested_set():
    state = ConversationState(indicator="unemployment rate", provider="FRED", resolved_indicator_code="UNRATE",
                              last_indicators_resolved=["UNRATE"], country="US")
    merged = merge_state(state, FollowUpDelta(added_indicators=["GDPC1"], changed_frequency="quarterly"))
    intent = materialize_intent(merged)
    assert intent.indicators == ["unemployment rate", "GDPC1"]
    assert [s.frequency for s in merged.requested_indicators] == ["quarterly", "quarterly"]
    assert "indicator" not in intent.parameters
    assert "__semantic_indicator_label" not in intent.parameters


def test_added_frequency_is_local_and_spec_projects_legacy_delta_labels():
    state = ConversationState(indicator="unemployment rate", frequency="monthly", provider="FRED")
    delta = FollowUpDelta(added_indicator_specs=[IndicatorSpec(label="GDPC1", frequency="quarterly", series_id="GDPC1")],
                         raw_query="and GDPC1 quarterly")
    assert delta.added_indicators == ["GDPC1"]
    merged = merge_state(state, delta)
    assert [(s.label, s.frequency) for s in merged.requested_indicators] == [
        ("unemployment rate", "monthly"), ("GDPC1", "quarterly"),
    ]
    restored = ConversationState.model_validate_json(merged.model_dump_json())
    assert indicator_specs_from_intent(materialize_intent(restored)) == merged.requested_indicators


@pytest.mark.parametrize("delta", [FollowUpDelta(changed_provider="IMF"),
                                   FollowUpDelta(changed_country="CA"),
                                   FollowUpDelta(changed_frequency="monthly")])
def test_execution_invalidations_preserve_all_requested_concepts(delta):
    state = extract_state_from_intent(parsed())
    state.resolved_indicator_code = "OLD_CODE"
    merged = merge_state(state, delta)
    assert materialize_intent(merged).indicators == ["GDP", "GDP growth", "GDP per capita"]


def test_provider_lock_is_not_promoted_into_explicit_spec_constraint():
    auto = parsed(__semantic_provider_locked=True)
    assert all(s.provider is None for s in extract_state_from_intent(auto).requested_indicators)
    explicit = parsed(__semantic_provider_locked=True, __explicit_provider="WORLDBANK")
    state = extract_state_from_intent(explicit)
    assert all(s.provider == "WORLDBANK" for s in state.requested_indicators)
    added = merge_state(state, FollowUpDelta(added_indicators=["inflation"]))
    assert added.requested_indicators[-1].provider == "WORLDBANK"


def test_children_have_independent_concepts_modifiers_and_authority(monkeypatch):
    parent = parsed(__semantic_indicator_label="GDP", __semantic_authority="llm_adjudication",
                    __exact_provider_code_match=True, __semantic_provider_locked=True,
                    __statscan_product_id="old", __measure_continuity={"series": [{"concept": "GDP", "unit": "USD"}]})
    captured = []

    async def fetch(_svc, child):
        captured.append(child.model_copy(deep=True))
        return [series(child.indicators[0])]

    monkeypatch.setattr(df, "fetch_data", fetch)
    result = asyncio.run(df.fetch_multi_indicator_data(service(), parent))
    assert [x.requested_indicator.label for x in result] == parent.indicators
    for child, label in zip(captured, parent.indicators):
        assert child.parameters["__semantic_indicator_label"] == label
        assert not child.parameters.get("__semantic_provider_locked")
        assert not child.parameters.get("__semantic_authority")
        assert not child.parameters.get("__exact_provider_code_match")
        assert not child.parameters.get("__statscan_product_id")
        assert bool(child.parameters.get("__measure_continuity")) == (label == "GDP")
        assert child.language == "zh"
    members = build_answer_set_members_from_data(result, intent=parent)
    assert [m.indicator_label for m in members] == parent.indicators


def test_each_failed_primary_gets_own_fallback_and_report(monkeypatch):
    svc = service()
    async def unavailable(_svc, child):
        raise ProviderUnavailable("upstream timeout")
    async def fallback(child, _error):
        return [series(child.indicators[0], source="IMF")]
    monkeypatch.setattr(df, "fetch_data", unavailable)
    svc._try_with_fallback = AsyncMock(side_effect=fallback)
    report = []
    token = df.MULTI_INDICATOR_FETCH_REPORTS.set(report)
    try:
        result = asyncio.run(df.fetch_multi_indicator_data(svc, parsed()))
    finally:
        df.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    assert len(result) == 3
    assert svc._try_with_fallback.await_count == 3
    assert [leg["spec"]["label"] for leg in report[0]["legs"]] == ["GDP", "GDP growth", "GDP per capita"]
    assert not report[0]["unserved"]


def test_explicit_provider_failure_never_substitutes(monkeypatch):
    svc = service("WORLDBANK")
    monkeypatch.setattr(df, "fetch_data", AsyncMock(side_effect=ProviderUnavailable("down")))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(df.fetch_multi_indicator_data(svc, parsed()))
    svc._try_with_fallback.assert_not_awaited()


def test_partial_result_has_typed_gap_on_every_series_and_no_cache_mutation(monkeypatch):
    svc = service()
    original = series("GDP")
    async def fetch(_svc, child):
        if child.indicators[0] == "GDP growth":
            raise ProviderUnavailable("down")
        return [original]
    monkeypatch.setattr(df, "fetch_data", fetch)
    result = asyncio.run(df.fetch_multi_indicator_data(svc, parsed(["GDP", "GDP growth", "GDP per capita"])))
    assert len(result) == 2
    assert all(s.fetch_incomplete for s in result)
    assert all("GDP growth" in " ".join(s.metadata.notes or []) for s in result)
    assert original.metadata.notes is None
    assert not original.fetch_incomplete


def test_outer_cancellation_awaits_active_and_queued_legs(monkeypatch):
    started, finished = [], []
    async def run():
        ready = asyncio.Event()
        async def fetch(_svc, child):
            started.append(child.indicators[0])
            if len(started) == 4:
                ready.set()
            try:
                await asyncio.sleep(60)
            finally:
                finished.append(child.indicators[0])
        monkeypatch.setattr(df, "fetch_data", fetch)
        task = asyncio.create_task(df.fetch_multi_indicator_data(service(), parsed([f"measure {i}" for i in range(7)])))
        await asyncio.wait_for(ready.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert len(started) == 4
    assert set(finished) == set(started)


def test_all_outage_report_is_available_before_aggregate_error(monkeypatch):
    monkeypatch.setattr(df, "fetch_data", AsyncMock(side_effect=ProviderUnavailable("down")))
    svc = service()
    svc._try_with_fallback = AsyncMock(side_effect=ProviderUnavailable("also down"))
    reports = []
    token = df.MULTI_INDICATOR_FETCH_REPORTS.set(reports)
    try:
        with pytest.raises(ProviderUnavailable):
            asyncio.run(df.fetch_multi_indicator_data(svc, parsed()))
    finally:
        df.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    assert not reports[0]["served"]
    assert [r["indicator"] for r in reports[0]["unserved"]] == ["GDP", "GDP growth", "GDP per capita"]
    assert {r["reason"] for r in reports[0]["unserved"]} == {"unavailable"}


def test_shared_fetcher_supplies_its_executed_intent_to_verifier():
    svc = service()
    captured = []
    async def shared_fetch(child):
        child.parameters["indicator"] = "NATIVE_" + child.indicators[0]
        return [series(child.indicators[0])]
    async def verify(_query, child, _plan, _data):
        captured.append(child.parameters["indicator"])
        return None
    svc._verify_execution_result = verify
    result = asyncio.run(df.fetch_multi_indicator_data(svc, parsed(), fetcher=shared_fetch))
    assert len(result) == 3
    assert captured == ["NATIVE_GDP", "NATIVE_GDP growth", "NATIVE_GDP per capita"]


def test_timeout_preserves_completed_results_and_awaits_cancelled_children(monkeypatch):
    finished = []
    async def fetch(_svc, child):
        if child.indicators[0] == "GDP":
            return [series("GDP")]
        try:
            await asyncio.sleep(60)
        finally:
            finished.append(child.indicators[0])
    monkeypatch.setattr(df, "fetch_data", fetch)
    monkeypatch.setattr(df, "io_remaining", lambda: 0.03)
    reports = []
    token = df.MULTI_INDICATOR_FETCH_REPORTS.set(reports)
    try:
        result = asyncio.run(df.fetch_multi_indicator_data(service(), parsed()))
    finally:
        df.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    assert [s.requested_indicator.label for s in result] == ["GDP"]
    assert set(finished) == {"GDP growth", "GDP per capita"}
    assert {r["reason"] for r in reports[0]["unserved"]} == {"timeout"}


def test_nonliteral_parser_series_id_cannot_become_a_constraint():
    intent = parsed(["GDP"], seriesId="GDP", indicatorSpecs=[{"label": "GDP", "series_id": "GDP"}])
    intent.originalQuery = "someGDPlabel"
    assert indicator_specs_from_intent(intent)[0].series_id is None


def test_new_delta_constraints_require_literal_user_evidence():
    state = ConversationState(indicator="GDP", provider="WORLDBANK")
    delta = FollowUpDelta(added_indicator_specs=[IndicatorSpec(
        label="inflation", provider="FRED", series_id="UNRATE",
    )], raw_query="also inflation")
    added = merge_state(state, delta).requested_indicators[-1]
    assert added.provider is None and added.series_id is None
    valid = FollowUpDelta(added_indicator_specs=[IndicatorSpec(
        label="unemployment", provider="FRED", series_id="UNRATE",
    )], raw_query="also UNRATE from FRED")
    assert merge_state(state, valid).requested_indicators[-1].series_id == "UNRATE"


@pytest.mark.parametrize("native_provider,catalog_provider,code", [
    ("FRED", "FRED", "MEASURE123"),
    ("WORLDBANK", "WorldBank", "MEASURE.TEST.001"),
])
def test_literal_opaque_native_code_reuses_existing_existence_gate(monkeypatch, native_provider, catalog_provider, code):
    from backend.services import indicator_resolution as resolution
    svc = service()
    svc._looks_like_provider_indicator_code = lambda provider, candidate: provider == native_provider and candidate == code
    monkeypatch.setattr(resolution, "_catalog_series_entry", lambda provider, candidate:
                        {"code": code} if provider == catalog_provider and candidate == code else None)
    monkeypatch.setattr(df, "_catalog_series_entry", resolution._catalog_series_entry)
    seen = []
    async def fetch(_svc, child):
        seen.append(child.model_copy(deep=True))
        return [series(child.indicators[0], source=child.apiProvider)]
    monkeypatch.setattr(df, "fetch_data", fetch)
    parent = parsed(["inflation", code])
    parent.originalQuery = f"inflation and {code} quarterly"
    result = asyncio.run(df.fetch_multi_indicator_data(svc, parent))
    native = next(child for child in seen if child.indicators == [code])
    assert native.apiProvider == native_provider
    assert native.parameters["indicator"] == code
    assert native.parameters["__semantic_authority"] == "exact_user_input"
    assert native.parameters["__requested_series_id"] == code
    assert result[-1].requested_indicator.series_id == code
    assert parent.parameters["__indicator_specs"][-1]["series_id"] == code


@pytest.mark.parametrize("initial_provider", ["WORLDBANK", "FRED"])
def test_plain_concept_acronym_does_not_gain_literal_authority(monkeypatch, initial_provider):
    from backend.services import indicator_resolution as resolution
    svc = service()
    svc._select_routed_provider.return_value = initial_provider
    svc._looks_like_provider_indicator_code = lambda provider, code: provider == "FRED"
    monkeypatch.setattr(resolution, "_catalog_series_entry", lambda provider, code:
                        {"code": code} if provider == "FRED" and code == "GDP" else None)
    monkeypatch.setattr(df, "_catalog_series_entry", resolution._catalog_series_entry)
    seen = []
    async def fetch(_svc, child):
        seen.append(child)
        return [series(child.indicators[0])]
    monkeypatch.setattr(df, "fetch_data", fetch)
    parent = parsed(["GDP", "inflation"])
    parent.apiProvider = initial_provider
    parent.originalQuery = "GDP and inflation"
    asyncio.run(df.fetch_multi_indicator_data(svc, parent))
    assert all(child.apiProvider == initial_provider for child in seen)
    assert svc._select_routed_provider.await_count == len(parent.indicators)
    assert not any(child.parameters.get("__exact_provider_code_match") for child in seen)


def test_verification_unavailable_is_not_retried_as_provider_outage(monkeypatch):
    svc = service()
    monkeypatch.setattr(df, "fetch_data", AsyncMock(side_effect=ResultVerificationUnavailable("judge offline")))
    reports = []
    token = df.MULTI_INDICATOR_FETCH_REPORTS.set(reports)
    try:
        with pytest.raises(DataNotAvailableError):
            asyncio.run(df.fetch_multi_indicator_data(svc, parsed()))
    finally:
        df.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    svc._try_with_fallback.assert_not_awaited()
    assert {r["reason"] for r in reports[0]["unserved"]} == {"verification_unavailable"}


def test_fallback_verification_unavailable_supersedes_primary_no_data(monkeypatch):
    svc = service()
    monkeypatch.setattr(df, "fetch_data", AsyncMock(side_effect=DataNotAvailableError("primary has no data")))
    svc._try_with_fallback = AsyncMock(side_effect=ResultVerificationUnavailable("judge offline"))
    reports = []
    token = df.MULTI_INDICATOR_FETCH_REPORTS.set(reports)
    try:
        with pytest.raises(DataNotAvailableError):
            asyncio.run(df.fetch_multi_indicator_data(svc, parsed()))
    finally:
        df.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    assert {r["reason"] for r in reports[0]["unserved"]} == {"verification_unavailable"}
