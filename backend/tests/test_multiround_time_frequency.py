"""Mechanism tests for the multiround-time-frequency cluster (2026-11-07).

(1) Every delta-built ParsedIntent carries time-scope provenance + language,
    so follow-up windows ("只看最近两年", "last 2 years", add/compare country)
    reach the provider instead of being stripped by the exact-match gate.
(2) A frequency-only metadata conflict keeps the right-concept pick (with a
    disclosure) instead of forcing a top-5 re-ask; unit conflicts unchanged.
(3) Time-window membership uses calendar-period overlap at the series
    frequency (Q4 / single-month windows), shared by the honesty filter and
    the window probe.
(4) FRED non-US follow-up discovery searches the structured concept, never
    the raw delta utterance.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services import data_fetcher
from backend.services.conversation_state_v2 import (
    AnswerSetMember,
    ConversationState,
    FollowUpDelta,
    materialize_intent,
    materialize_time_scope,
    merge_state,
    merged_language,
    merged_time_scope,
)
from backend.services.indicator_selector import IndicatorSelector, SelectionResult
from backend.services.query import QueryService
from backend.services.time_scope_provenance import stamped_user_time_scope
from backend.utils.retry import DataNotAvailableError


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _state(provider="FRED", code="GDPC1", label="real GDP", source="default", language="zh"):
    return ConversationState(
        indicator=label,
        country="US",
        provider=provider,
        start_date="2021-01-01",
        end_date="2025-12-31",
        time_scope_source=source,
        language=language,
        turn_number=1,
        original_query="美国实际GDP",
        active_answer_members=[
            AnswerSetMember(provider=provider, indicator_label=label, provider_code=code, country="US"),
        ],
    )


def _member_intent(state, delta, query):
    svc = QueryService.__new__(QueryService)
    member = state.active_answer_members[0]
    return svc._build_collective_member_intent(
        query=query,
        member_query=f"US {state.indicator}",
        state=state,
        member=member,
        delta=delta,
        collective_indicator_label=state.indicator,
    )


def _series(points, frequency="quarterly", source="FRED", indicator="Real GDP", country="US"):
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country=country,
            frequency=frequency, unit="Index", seriesId="X1",
        ),
        data=[{"date": d, "value": v} for d, v in points],
    )


def _svc():
    return SimpleNamespace(
        _MAX_ALTERNATE_CODE_RETRIES=2,
        _preflight_geographic_split=AsyncMock(return_value=None),
        _maybe_expand_ranking_country_scope=lambda query, provider, params: params,
        _resolve_indicator_for_fetch=AsyncMock(side_effect=lambda provider, request, params: params),
        _get_from_cache=AsyncMock(return_value=None),
        _save_to_cache=AsyncMock(),
        _normalize_bis_metadata_labels=lambda result: None,
        _looks_like_provider_indicator_code=lambda provider, value: True,
    )


def _capture_dispatch(monkeypatch, responses):
    replies = iter(responses)
    calls = []

    async def fake_dispatch(svc, intent, execution_plan):  # noqa: ARG001
        calls.append(copy.deepcopy(dict(getattr(execution_plan, "params", None) or intent.parameters or {})))
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return [reply] if isinstance(reply, NormalizedData) else reply

    monkeypatch.setattr(data_fetcher, "fetch_from_provider_dispatch", fake_dispatch)
    return calls


# --------------------------------------------------------------------------
# (1) delta time-scope provenance
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "delta, expected_authority",
    [
        (FollowUpDelta(changed_start_date="2024-01-01", delta_type="time_change"), "user"),
        (FollowUpDelta(added_countries=["JP"], delta_type="add_country"), "default"),
        (FollowUpDelta(changed_country="GB", delta_type="compare_with"), "default"),
    ],
)
def test_collective_member_intent_always_stamps_authority_and_language(delta, expected_authority):
    intent = _member_intent(_state(), delta, "只看最近两年")
    params = intent.parameters
    assert params.get("startDate") or params.get("endDate")
    assert params["__time_scope_authority"] == expected_authority
    assert intent.language == "zh"
    assert intent.isFollowUp is True


def test_contract_every_delta_time_producer_stamps_authority_with_dates():
    """Contract: any delta-path producer that emits dates also emits provenance."""
    state = _state(source="user")
    deltas = [
        FollowUpDelta(changed_start_date="2024-01-01"),
        FollowUpDelta(added_countries=["JP"]),
        FollowUpDelta(changed_language="en", changed_end_date="2025-06-30"),
    ]
    for delta in deltas:
        produced = [
            materialize_time_scope(state, delta),
            materialize_intent(merge_state(state, delta)).parameters,
            _member_intent(state, delta, "compare with Japan").parameters,
        ]
        for params in produced:
            if params.get("startDate") or params.get("endDate"):
                assert params.get("__time_scope_authority"), params


def test_shared_time_scope_matches_merge_state_semantics():
    state = _state(source="default")
    for delta in (
        FollowUpDelta(changed_start_date="2024-01-01"),
        FollowUpDelta(added_countries=["JP"]),
        FollowUpDelta(changed_end_date="2025-06-30", changed_language="en"),
    ):
        merged = merge_state(state, delta)
        assert merged_time_scope(state, delta) == (merged.start_date, merged.end_date, merged.time_scope_source)
        assert merged_language(state, delta) == merged.language
        assert materialize_time_scope(merged) == {
            k: v for k, v in materialize_intent(merged).parameters.items()
            if k in {"startDate", "endDate", "__time_scope_authority"}
        }


def test_turn_three_inherits_user_window_provenance():
    """Turn 2 changes the window (zh), turn 3 adds a country: still 'user'."""
    state = _state(source="default")
    turn2 = FollowUpDelta(changed_start_date="2024-01-01", delta_type="time_change")
    after_turn2 = state.model_copy(deep=True)
    (after_turn2.start_date, after_turn2.end_date, after_turn2.time_scope_source) = merged_time_scope(state, turn2)
    turn3 = FollowUpDelta(added_countries=["JP"], delta_type="add_country")
    intent3 = _member_intent(after_turn2, turn3, "再加上日本")
    assert intent3.parameters["startDate"] == "2024-01-01"
    assert intent3.parameters["__time_scope_authority"] == "user"


@pytest.mark.parametrize("provider", ["FRED", "WORLDBANK", "IMF"])
@pytest.mark.parametrize("query", ["只看最近两年", "only the last two years"])
def test_follow_up_time_change_window_reaches_provider(monkeypatch, provider, query):
    calls = _capture_dispatch(monkeypatch, [_series([("2024-01-01", 1.0), ("2024-04-01", 2.0)])])
    delta = FollowUpDelta(changed_start_date="2024-01-01", delta_type="time_change")
    intent = _member_intent(_state(provider=provider, code="CODE1"), delta, query)
    asyncio.run(data_fetcher.fetch_data(_svc(), intent))
    assert calls and calls[0].get("startDate") == "2024-01-01"
    assert calls[0].get("endDate") == "2025-12-31"


@pytest.mark.parametrize("provider", ["FRED", "WORLDBANK", "IMF"])
@pytest.mark.parametrize(
    "delta, query",
    [
        (FollowUpDelta(added_countries=["JP"], delta_type="add_country"), "再加上日本"),
        (FollowUpDelta(changed_country="GB", delta_type="compare_with"), "compare with the UK"),
    ],
)
def test_carried_default_window_is_kept_for_exact_locked_follow_up(monkeypatch, provider, delta, query):
    calls = _capture_dispatch(monkeypatch, [_series([("2023-01-01", 1.0)])])
    intent = _member_intent(_state(provider=provider, code="CODE1"), delta, query)
    asyncio.run(data_fetcher.fetch_data(_svc(), intent))
    assert calls[0].get("startDate") == "2021-01-01"
    assert calls[0].get("__time_scope_authority") == "default"


def test_carried_default_window_still_broadens_when_member_has_no_data(monkeypatch):
    """Provenance stays 'default', so the honest broadened retry still fires."""
    calls = _capture_dispatch(
        monkeypatch,
        [DataNotAvailableError("empty"), _series([("2015-01-01", 1.0)], frequency="annual", source="World Bank")],
    )
    delta = FollowUpDelta(added_countries=["JP"], delta_type="add_country")
    intent = _member_intent(_state(provider="WORLDBANK", code="NY.GDP.MKTP.CD"), delta, "再加上日本")
    result = asyncio.run(data_fetcher.fetch_data(_svc(), intent))
    assert len(calls) == 2
    assert calls[0].get("startDate") == "2021-01-01"
    assert "startDate" not in calls[1]
    assert result[0].data[0].date == "2015-01-01"


def test_stamp_beats_text_heuristic_in_both_directions():
    assert stamped_user_time_scope({"__time_scope_authority": "user"}) is True
    assert stamped_user_time_scope({"__time_scope_authority": "default"}) is False
    assert stamped_user_time_scope({"startDate": "2020-01-01"}) is None
    # Unstamped legacy params keep the existing text fallback (mixed script).
    assert data_fetcher._user_set_time_scope({}, "中国GDP 2020-2024") is True
    assert data_fetcher._user_set_time_scope({"__time_scope_authority": "user"}, "只看最近两年") is True


def test_fred_provider_reads_the_stamp_not_its_text_regex():
    from backend.providers import fred

    assert fred._user_set_time_scope({"__time_scope_authority": "user", "__original_query": "只看最近两年"}) is True
    assert fred._user_set_time_scope({"__time_scope_authority": "default", "__original_query": "last 5 years"}) is False
    assert fred._user_set_time_scope({"__original_query": "last 5 years"}) is True


# --------------------------------------------------------------------------
# (2) frequency-only metadata conflict keeps the right-concept pick
# --------------------------------------------------------------------------

def _fake_enrich(metadata):
    def enrich(candidates, provider):  # noqa: ARG001
        return [
            {
                "code": code, "name": name, "end_date": "", "category": "",
                "description": "", "keywords": "", "discontinued": False,
                **metadata[code],
            }
            for code, name in candidates
        ]
    return enrich


@pytest.mark.asyncio
async def test_frequency_only_conflict_keeps_original_pick_and_skips_top5_reask(monkeypatch):
    selector = IndicatorSelector(settings=SimpleNamespace())
    candidates = [
        ("DGS10", "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity"),
        ("MORTGAGE30US", "30-Year Fixed Rate Mortgage Average"),
        ("TB3MS", "3-Month Treasury Bill Secondary Market Rate"),
    ]
    monkeypatch.setattr(selector, "_get_candidates_with_scores", lambda q, p, constraint_query=None: (candidates, [0.9, 0.6, 0.5]))
    monkeypatch.setattr(selector, "_enrich_candidates", _fake_enrich({
        "DGS10": {"frequency": "Daily", "unit": "Percent"},
        "MORTGAGE30US": {"frequency": "Weekly", "unit": "Percent"},
        "TB3MS": {"frequency": "Monthly", "unit": "Percent"},
    }))
    seen = []

    async def fake_llm_pick(query, cands, provider, prefer_ask=False, country=None, **_kw):  # noqa: ARG001
        seen.append([code for code, _ in cands])
        if len(seen) == 1:
            return SelectionResult(code="DGS10", name=cands[0][1], source="llm_pick")
        # The only monthly candidate is a different concept: the LLM rejects it.
        return SelectionResult(code=None, source="llm_reject", retry_query="")

    monkeypatch.setattr(selector, "_llm_pick", fake_llm_pick)
    result = await selector.select("monthly 10-year treasury yield", "FRED")

    assert result.code == "DGS10"
    assert result.metadata_mismatch == {"requested_frequency": "monthly", "native_frequency": "Daily"}
    assert result.authoritative is False
    # original pick + compatible-subset retry only; no forced top-5 re-ask.
    assert seen == [["DGS10", "MORTGAGE30US", "TB3MS"], ["TB3MS"]]


@pytest.mark.asyncio
async def test_unit_conflict_still_fails_closed():
    selector = IndicatorSelector(settings=SimpleNamespace())
    candidates = [("GDP", "Gross Domestic Product"), ("GDPC1", "Real Gross Domestic Product")]
    selector._enrich_candidates = _fake_enrich({
        "GDP": {"frequency": "Quarterly", "unit": "Billions of Dollars"},
        "GDPC1": {"frequency": "Quarterly", "unit": "Billions of Chained 2017 Dollars"},
    })

    async def reject(*_a, **_kw):
        return SelectionResult(code=None, source="llm_reject")

    selector._llm_pick = reject
    original = SelectionResult(code="GDP", source="llm_pick")
    out = await selector._retry_if_metadata_conflict(
        "gdp in billions of chained 2017 dollars", original, candidates, "FRED"
    )
    assert out.code is None and out.source == "metadata_conflict"


def test_frequency_disclosure_on_fresh_fetch_and_cache_hit(monkeypatch):
    calls = _capture_dispatch(monkeypatch, [_series([("2024-01-02", 4.1), ("2024-01-03", 4.2)], frequency="Daily")])
    svc = _svc()
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["DGS10"],
        parameters={"indicator": "DGS10", "frequency": "monthly", "country": "US", "__semantic_authority": "exact_user_input"},
        originalQuery="美国10年期国债收益率 月度", clarificationNeeded=False, language="zh",
    )
    fresh = asyncio.run(data_fetcher.fetch_data(svc, intent))
    assert any("月度" in note and "日度" in note for note in fresh[0].metadata.notes or [])
    cached_obj = svc._save_to_cache.await_args.args[2]
    assert not (cached_obj.metadata.notes or []), "disclosure must not be cached"
    assert len(calls) == 1

    svc2 = _svc()
    svc2._get_from_cache = AsyncMock(return_value=cached_obj)
    intent_en = ParsedIntent(
        apiProvider="FRED", indicators=["DGS10"],
        parameters={"indicator": "DGS10", "frequency": "monthly", "country": "US", "__semantic_authority": "exact_user_input"},
        originalQuery="monthly US 10 year yield", clarificationNeeded=False, language="en",
    )
    hit = asyncio.run(data_fetcher.fetch_data(svc2, intent_en))
    assert any("daily" in note and "monthly" in note for note in hit[0].metadata.notes or [])
    assert not (cached_obj.metadata.notes or [])


@pytest.mark.parametrize("requested, native", [("monthly", "Monthly"), ("quarterly", "Quarterly, End of Period"), (None, "Daily"), ("monthly", "irregular")])
def test_no_frequency_disclosure_when_matching_or_unknown(requested, native):
    params = {"frequency": requested} if requested else {}
    out = data_fetcher._disclose_frequency_mismatch([_series([("2024-01-01", 1.0)], frequency=native)], params, "en")
    assert not (out[0].metadata.notes or [])


# --------------------------------------------------------------------------
# (3) sub-annual windows use calendar-period overlap
# --------------------------------------------------------------------------

def _user_window(start, end):
    return {"startDate": start, "endDate": end, "__time_scope_authority": "user"}


def test_q4_request_does_not_count_other_quarters_of_the_year():
    probe = [_series([("2026-01-01", 1.0), ("2026-04-01", 2.0), ("2026-07-01", 3.0)], frequency="Quarterly")]
    recovered, latest = data_fetcher._classify_time_window_probe(_user_window("2026-10-01", "2026-12-31"), probe)
    assert recovered == [] and latest == "2026-07-01"


def test_q3_positive_control_recovers_the_quarter():
    probe = [_series([("2026-04-01", 2.0), ("2026-07-01", 3.0)], frequency="Quarterly")]
    recovered, _latest = data_fetcher._classify_time_window_probe(_user_window("2026-07-01", "2026-09-30"), probe)
    assert [p.date for p in recovered[0].data] == ["2026-07-01"]


def test_fred_q4_gdp_probe_path_raises_no_data_in_requested_window(monkeypatch):
    _capture_dispatch(monkeypatch, [
        DataNotAvailableError("empty"),
        _series([("2026-04-01", 2.0), ("2026-07-01", 3.0)], frequency="Quarterly"),
    ])
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["GDPC1"],
        parameters={"indicator": "GDPC1", "country": "US", "__semantic_authority": "exact_user_input", **_user_window("2026-10-01", "2026-12-31")},
        originalQuery="Q4 2026 GDP", clarificationNeeded=False,
    )
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(data_fetcher.fetch_data(_svc(), intent))
    assert "no_data_in_requested_window" in str(failure.value)
    assert "latest_available=2026-07-01" in str(failure.value)


@pytest.mark.parametrize(
    "provider, query, window, points",
    [
        ("CHINAMACRO", "2026年11月CPI", ("2026-11-01", "2026-11-30"),
         [("2026-09-01", 0.1), ("2026-10-01", 0.2)]),
        ("FRED", "Dec 2026 unemployment", ("2026-12-01", "2026-12-31"),
         [("2026-10-01", 4.1), ("2026-11-01", 4.2)]),
    ],
)
def test_single_month_window_honesty_filter_path(monkeypatch, provider, query, window, points):
    _capture_dispatch(monkeypatch, [_series(points, frequency="monthly", source=provider)])
    intent = ParsedIntent(
        apiProvider=provider, indicators=["X1"],
        parameters={"indicator": "X1", "country": "CN" if provider == "CHINAMACRO" else "US", "__semantic_authority": "exact_user_input", **_user_window(*window)},
        originalQuery=query, clarificationNeeded=False,
    )
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(data_fetcher.fetch_data(_svc(), intent))
    assert "no_data_in_requested_window" in str(failure.value)


def test_period_end_dated_monthly_observation_stays_in_its_own_month():
    series = _series([("2026-10-31", 1.0), ("2026-11-30", 2.0)], frequency="Monthly")
    out = data_fetcher._enforce_user_time_window(_user_window("2026-11-01", "2026-11-30"), "", [series])
    assert [p.date for p in out[0].data] == ["2026-11-30"]


def test_recency_window_on_monthly_data_keeps_overlapping_months():
    series = _series([("2026-06-01", 1.0), ("2026-08-01", 2.0), ("2026-09-01", 3.0), ("2026-10-01", 4.0)], frequency="Monthly")
    out = data_fetcher._enforce_user_time_window(_user_window("2026-08-07", "2026-11-07"), "", [series])
    assert [p.date for p in out[0].data] == ["2026-08-01", "2026-09-01", "2026-10-01"]


def test_annual_series_full_year_window_unchanged_and_boundary_year_kept():
    series = _series([("2020-01-01", 1.0), ("2021-01-01", 2.0), ("2024-01-01", 3.0), ("2025-01-01", 4.0)], frequency="annual")
    out = data_fetcher._enforce_user_time_window(_user_window("2021-07-18", "2024-12-31"), "", [series])
    assert [p.date for p in out[0].data] == ["2021-01-01", "2024-01-01"]


def test_mislabeled_frequency_falls_back_to_coarser_spacing():
    # Labeled monthly, but observations are annual (Jan-1 stamps): the
    # coarser observed spacing wins, so the boundary year is NOT dropped.
    series = _series([("2020-01-01", 1.0), ("2021-01-01", 2.0), ("2022-01-01", 3.0)], frequency="monthly")
    out = data_fetcher._enforce_user_time_window(_user_window("2021-07-01", "2022-12-31"), "", [series])
    assert [p.date for p in out[0].data] == ["2021-01-01", "2022-01-01"]


def test_unknown_frequency_keeps_year_level_behavior():
    series = _series([("2026-01-01", 1.0), ("2026-04-01", 2.0)], frequency="irregular")
    out = data_fetcher._enforce_user_time_window(_user_window("2026-10-01", "2026-12-31"), "", [series])
    assert [p.date for p in out[0].data] == ["2026-01-01", "2026-04-01"]


# --------------------------------------------------------------------------
# (4) FRED follow-up discovery text = structured concept, not the utterance
# --------------------------------------------------------------------------

@pytest.mark.parametrize("utterance", ["compare with Japan", "和日本比较", "再加上日本", "what about Japan?"])
@pytest.mark.parametrize("label, code", [("real GDP", "GDPC1"), ("consumer price index", "CPIAUCSL"), ("unemployment rate", "UNRATE")])
def test_fred_follow_up_country_search_uses_concept_not_utterance(utterance, label, code):
    intent = ParsedIntent(
        apiProvider="FRED", indicators=[code],
        parameters={"indicator": code, "country": "JP", "__semantic_indicator_label": label, "__original_query": utterance},
        originalQuery=utterance, clarificationNeeded=False, isFollowUp=True,
    )
    text = data_fetcher._fred_country_context_query(intent.parameters, intent, "JP", "JP")
    assert label in text
    assert "Japan" in text
    assert utterance not in text
    assert code not in text


def test_fred_follow_up_with_only_code_label_uses_catalog_name(monkeypatch):
    from backend.services import follow_up_retrieval

    monkeypatch.setattr(follow_up_retrieval, "_catalog_name_for_code", lambda provider, code: "Unemployment Rate")
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["UNRATE"],
        parameters={"indicator": "UNRATE", "country": "GB"},
        originalQuery="再加上英国", clarificationNeeded=False, isFollowUp=True,
    )
    text = data_fetcher._fred_country_context_query(intent.parameters, intent, "GB", "GB")
    assert "Unemployment Rate" in text and "再加上英国" not in text and "UNRATE" not in text


def test_fred_first_turn_country_search_unchanged():
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["GDP"],
        parameters={"indicator": "GDP", "country": "JP", "__original_query": "Japan GDP from FRED"},
        originalQuery="Japan GDP from FRED", clarificationNeeded=False,
    )
    assert data_fetcher._fred_country_context_query(intent.parameters, intent, "JP", "JP") == "Japan GDP"


def test_no_data_clarification_retrieves_with_concept_on_follow_up():
    from backend.services import indicator_clarification

    seen = []
    qs = SimpleNamespace(
        _collect_indicator_choice_options=lambda query, intent, max_options=4: seen.append(query) or [],
        _normalize_country_to_iso2=lambda c: "DE",
    )
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["consumer price index"],
        parameters={"country": "DE", "__semantic_indicator_label": "consumer price index"},
        originalQuery="add Germany", clarificationNeeded=False, isFollowUp=True,
    )
    indicator_clarification.build_no_data_indicator_clarification(qs, "c1", "add Germany", intent)
    assert seen and all("add Germany" not in q for q in seen)
    assert "consumer price index" in seen[0] and "Germany" in seen[0]
