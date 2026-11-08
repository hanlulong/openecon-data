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


@pytest.mark.parametrize("language, slot_words, hint_forbidden", [
    ("en", ("asked for", "**monthly**"), ("asked for", "monthly")),
    ("zh", ("您请求的是", "**月度**"), ("您请求", "月度")),
])
def test_frequency_note_states_the_request_only_for_a_parse_slot(language, slot_words, hint_forbidden):
    daily = [_series([("2024-01-02", 1.0)], frequency="Daily")]
    # Parse/state SLOT: a structured statement of the request -> said so.
    slot_note = data_fetcher._disclose_frequency_mismatch(daily, {"frequency": "monthly"}, language)[0].metadata.notes
    assert all(word in slot_note[0] for word in slot_words), slot_note
    # Annotation only (raw-text hint): the served frequency, nothing claimed.
    hint = {"__frequency_mismatch": {"requested_frequency": "monthly", "native_frequency": "Daily"}}
    hint_note = data_fetcher._disclose_frequency_mismatch(daily, hint, language)[0].metadata.notes
    assert len(hint_note) == 1
    assert not any(word in hint_note[0] for word in hint_forbidden), hint_note
    assert ("**daily**" if language == "en" else "**日度**") in hint_note[0]


@pytest.mark.parametrize("query, native", [
    # Real-user phrasings the text hint misreads as a frequency request.
    ("US unemployment rate for the year 2025", "Monthly"),
    ("China CPI year-on-year", "Monthly"),
    ("CPI year over year", "Monthly"),
    ("GDP growth annual rate", "Quarterly"),
    ("3-month treasury bill rate", "Daily"),
    ("inflation from a year ago", "Monthly"),
])
def test_misread_frequency_hints_never_become_a_claim_about_the_request(query, native):
    from backend.services.indicator_selector import FREQUENCY_MISMATCH_PARAM, _extract_requested_frequencies

    # The annotation's request side is exactly this hint (selector kept-pick).
    requested = _extract_requested_frequencies(query)
    assert requested, f"hint no longer misreads {query!r}: drop the case"
    params = {FREQUENCY_MISMATCH_PARAM: {"requested_frequency": ",".join(sorted(requested)), "native_frequency": native}}
    for language in ("en", "zh"):
        out = data_fetcher._disclose_frequency_mismatch([_series([("2025-01-01", 1.0)], frequency=native)], params, language)
        for note in out[0].metadata.notes or []:
            assert "asked for" not in note and "您请求" not in note, (query, note)


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


# --------------------------------------------------------------------------
# Review fix-ups: kept-pick provenance survives the selection cache and
# reaches the first-turn disclosure; follow-up labels never leak raw text;
# a malformed window bound never drops its side of the window; the collective
# delta path persists provenance end to end.
# --------------------------------------------------------------------------

def _treasury_selector(monkeypatch, seen):
    selector = IndicatorSelector(settings=SimpleNamespace())
    candidates = [
        ("DGS10", "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity"),
        ("TB3MS", "3-Month Treasury Bill Secondary Market Rate"),
    ]
    monkeypatch.setattr(selector, "_get_candidates_with_scores", lambda q, p, constraint_query=None: (candidates, [0.9, 0.5]))
    monkeypatch.setattr(selector, "_enrich_candidates", _fake_enrich({
        "DGS10": {"frequency": "Daily", "unit": "Percent"},
        "TB3MS": {"frequency": "Monthly", "unit": "Percent"},
    }))

    async def fake_llm_pick(query, cands, provider, prefer_ask=False, country=None, **_kw):  # noqa: ARG001
        seen.append([code for code, _ in cands])
        if len(cands) > 1:
            return SelectionResult(code="DGS10", name=cands[0][1], source="llm_pick")
        return SelectionResult(code=None, source="llm_reject", retry_query="")

    monkeypatch.setattr(selector, "_llm_pick", fake_llm_pick)
    return selector


@pytest.mark.asyncio
async def test_kept_pick_stays_non_authoritative_across_selection_cache(monkeypatch):
    from backend.services import indicator_selector as selector_module

    selector_module._SELECTION_CACHE.clear()
    seen: list = []
    selector = _treasury_selector(monkeypatch, seen)
    first = await selector.select("monthly 10-year treasury yield", "FRED")
    llm_calls = len(seen)
    repeat = await selector.select("monthly 10-year treasury yield", "FRED")

    assert len(seen) == llm_calls, "the repeat must be served by the selection cache"
    assert first.code == repeat.code == "DGS10"
    assert first.authoritative is False
    assert repeat.authoritative is False
    assert repeat.metadata_mismatch == first.metadata_mismatch == {
        "requested_frequency": "monthly", "native_frequency": "Daily",
    }


def _fred_resolution_svc():
    return SimpleNamespace(
        fred_provider=SimpleNamespace(),
        statscan_provider=SimpleNamespace(),
        _looks_like_provider_indicator_code=lambda _p, _i: False,
        _verify_semantic_discriminators=lambda *_a, **_k: True,
    )


def _resolve_with_selection(selection, params, query="monthly 10-year treasury yield", indicator="10-year treasury yield"):
    from unittest.mock import patch

    from backend.services.indicator_resolution import resolve_indicator_for_fetch

    intent = ParsedIntent(
        apiProvider="FRED", indicators=[indicator], parameters=dict(params),
        clarificationNeeded=False, originalQuery=query, language="en",
    )
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=AsyncMock(return_value=selection)):
        resolved = asyncio.run(resolve_indicator_for_fetch(_fred_resolution_svc(), "FRED", intent, dict(params)))
    return intent, resolved


def test_kept_pick_annotation_reaches_first_turn_disclosure_without_frequency_slot(monkeypatch):
    """First-turn parses carry no 'frequency' slot: the selector annotation is
    the requested-frequency signal, threaded selector -> resolution -> fetch."""
    mismatch = {"requested_frequency": "monthly", "native_frequency": "Daily"}
    selection = SelectionResult(code="DGS10", name="10-Year Treasury", source="llm_pick", metadata_mismatch=mismatch)
    intent, params = _resolve_with_selection(selection, {"country": "US"})

    assert params["indicator"] == "DGS10"
    assert params["__frequency_mismatch"] == mismatch
    assert "__authoritative_pick_code" not in params
    assert "frequency" not in params

    _capture_dispatch(monkeypatch, [_series([("2024-01-02", 4.1), ("2024-01-03", 4.2)], frequency="Daily")])
    svc = _svc()
    intent.parameters = params
    served = asyncio.run(data_fetcher.fetch_data(svc, intent))
    notes = served[0].metadata.notes or []
    # The annotation's request is a raw-text HINT: the note states the served
    # frequency and never claims what the user asked.
    assert any("**daily**" in note for note in notes), notes
    assert not any("asked for" in note or "monthly" in note for note in notes), notes
    assert not (svc._save_to_cache.await_args.args[2].metadata.notes or []), "disclosure must not be cached"


def test_stale_frequency_annotation_is_dropped_when_a_new_pick_carries_none():
    selection = SelectionResult(code="GS10", name="10-Year Treasury, monthly", source="llm_pick")
    _intent, params = _resolve_with_selection(
        selection,
        {"country": "US", "__frequency_mismatch": {"requested_frequency": "monthly", "native_frequency": "Daily"}},
    )
    assert params["indicator"] == "GS10"
    assert "__frequency_mismatch" not in params


def test_frequency_annotation_is_provenance_not_cache_identity():
    svc = QueryService.__new__(QueryService)
    base = {"indicator": "DGS10", "country": "US", "startDate": "2024-01-01"}
    annotated = {**base, "__frequency_mismatch": {"requested_frequency": "monthly", "native_frequency": "Daily"}}
    assert svc._build_cache_params("FRED", dict(base)) == svc._build_cache_params("FRED", annotated)


def test_multiple_requested_frequencies_in_annotation_do_not_disclose():
    params = {"__frequency_mismatch": {"requested_frequency": "annual,monthly", "native_frequency": "Daily"}}
    out = data_fetcher._disclose_frequency_mismatch([_series([("2024-01-01", 1.0)], frequency="Daily")], params, "en")
    assert not (out[0].metadata.notes or [])


@pytest.mark.parametrize("utterance", ["和日本比较", "compare with Japan"])
def test_follow_up_label_never_uses_display_label_or_raw_utterance(monkeypatch, utterance):
    from backend.services import follow_up_retrieval

    monkeypatch.setattr(follow_up_retrieval, "_catalog_name_for_code", lambda provider, code: "")
    params = {
        "indicator": "GDPC1", "country": "JP",
        "__semantic_indicator_label": "GDP",          # acronym concept, not a catalog id
        "__display_indicator_label": utterance,       # raw text by construction
        "__original_query": utterance,
    }
    intent = ParsedIntent(apiProvider="FRED", indicators=["GDPC1"], parameters=params,
                          originalQuery=utterance, clarificationNeeded=False, isFollowUp=True)
    text = data_fetcher._fred_country_context_query(params, intent, "JP", "JP")
    assert text == "Japan GDP"

    # Semantic label equal to the utterance (a bad upstream label) is skipped too.
    params2 = {**params, "__semantic_indicator_label": utterance}
    intent2 = intent.model_copy(update={"parameters": params2})
    assert utterance not in data_fetcher._fred_country_context_query(params2, intent2, "JP", "JP")


def test_follow_up_code_label_known_to_catalog_becomes_its_title(monkeypatch):
    from backend.services import follow_up_retrieval

    names = {"BAMLH0A0HYM2": "ICE BofA US High Yield Index Option-Adjusted Spread"}
    monkeypatch.setattr(follow_up_retrieval, "_catalog_name_for_code", lambda provider, code: names.get(code, ""))
    params = {"indicator": "BAMLH0A0HYM2", "country": "GB", "__semantic_indicator_label": "BAMLH0A0HYM2"}
    intent = ParsedIntent(apiProvider="FRED", indicators=["BAMLH0A0HYM2"], parameters=params,
                          originalQuery="再加上英国", clarificationNeeded=False, isFollowUp=True)
    text = data_fetcher._fred_country_context_query(params, intent, "GB", "GB")
    assert "High Yield" in text and "BAMLH0A0HYM2" not in text and "再加上英国" not in text


def test_follow_up_unknown_bare_code_yields_no_search_text(monkeypatch):
    from backend.services import follow_up_retrieval

    monkeypatch.setattr(follow_up_retrieval, "_catalog_name_for_code", lambda provider, code: "")
    params = {"indicator": "UNRATE", "country": "JP", "__display_indicator_label": "compare with Japan"}
    intent = ParsedIntent(apiProvider="FRED", indicators=["UNRATE"], parameters=params,
                          originalQuery="compare with Japan", clarificationNeeded=False, isFollowUp=True)
    assert data_fetcher._fred_country_context_query(params, intent, "JP", "JP") == ""


@pytest.mark.parametrize(
    "start, end, points, expected",
    [
        # Out-of-range day: clamped, the start side is still enforced.
        ("2026-02-30", "2026-12-31", [("2024-05-01", 1.0), ("2026-03-01", 2.0)], ["2026-03-01"]),
        # Non-ISO start: year-level bound, never dropped.
        ("2020-Q1", "2021-12-31", [("2018-06-01", 1.0), ("2020-02-01", 2.0)], ["2020-02-01"]),
        # Out-of-range day on the END bound.
        ("2026-01-01", "2026-02-29", [("2026-02-01", 1.0), ("2026-03-01", 2.0)], ["2026-02-01"]),
        # Compact YYYYMMDD bounds: the same dates as their ISO forms, never
        # dropped (the year-level _year() gate already read them as 2020/2021).
        ("20200101", "2021-12-31", [("2018-06-01", 1.0), ("2020-02-01", 2.0)], ["2020-02-01"]),
        ("2020-01-01", "20210630", [("2021-06-01", 1.0), ("2021-09-01", 2.0)], ["2021-06-01"]),
        # Year 0 is no bound on either gate (never a ValueError): the END
        # bound is still enforced.
        ("0000-01-01", "2021-12-31", [("2018-06-01", 1.0), ("2022-02-01", 2.0)], ["2018-06-01"]),
    ],
)
def test_malformed_window_bound_never_drops_its_side(start, end, points, expected):
    series = _series(points, frequency="Monthly")
    out = data_fetcher._enforce_user_time_window(_user_window(start, end), "", [series])
    assert [p.date for p in out[0].data] == expected


@pytest.mark.parametrize(
    "value, is_end, expected",
    [
        ("0000", False, None),
        ("0000-01-01", True, None),
        ("20200101", False, "2020-01-01"),
        ("20201231", True, "2020-12-31"),
        ("20200230", True, "2020-02-29"),   # clamped like the ISO form
        ("20201399", False, "2020-01-01"),  # month out of range -> year level
        ("202001", True, "2020-12-31"),     # other digit runs -> year level, as _year()
        ("2020Q1", False, "2020-01-01"),
        (2021, True, "2021-12-31"),
        ("Q1-2020", False, None),
        ("", False, None),
    ],
)
def test_window_bound_parses_every_shape_without_raising(value, is_end, expected):
    bound = data_fetcher._window_bound(value, is_end=is_end)
    assert (bound.isoformat() if bound else None) == expected
    # _requested_window (shared by the honesty filter and the window probe)
    # never raises on any of these shapes either.
    data_fetcher._requested_window({"startDate": value, "endDate": value})


def _collective_service(monkeypatch, fetch_reply):
    from backend.services import query as query_module

    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    captured_intents: list = []
    saved_states: list = []

    async def fake_fetch(intent):
        captured_intents.append(copy.deepcopy(intent))
        return fetch_reply(intent)

    monkeypatch.setattr(svc, "_fetch_data", fake_fetch)
    monkeypatch.setattr(svc, "_verify_execution_result", AsyncMock(return_value=None))
    monkeypatch.setattr(query_module.conversation_manager, "add_message_safe", lambda conv_id, *_a, **_k: conv_id or "conv-mrtf")
    monkeypatch.setattr(
        query_module.conversation_manager, "set_conversation_state",
        lambda _conv_id, state: saved_states.append(state.model_copy(deep=True)),
    )
    return svc, captured_intents, saved_states


def test_collective_delta_end_to_end_persists_window_provenance_for_turn_three(monkeypatch):
    def reply(intent):
        country = (intent.parameters or {}).get("country") or "US"
        return [_series([("2024-01-01", 1.0), ("2024-04-01", 2.0)], frequency="Quarterly", country=country)]

    svc, captured, saved = _collective_service(monkeypatch, reply)
    state = _state(provider="FRED", code="GDPC1", source="default", language="zh")

    # Turn 2: zh time change on an exact-locked collective member.
    turn2 = FollowUpDelta(changed_start_date="2024-01-01", delta_type="time_change", raw_query="只看最近两年")
    response = asyncio.run(svc._execute_collective_answer_member_delta(
        query="只看最近两年", conversation_id="conv-mrtf", tracker=None, state=state, delta=turn2,
    ))
    assert response is not None and not response.error
    member_params = captured[-1].parameters
    assert member_params["startDate"] == "2024-01-01"
    assert member_params["__time_scope_authority"] == "user"
    assert captured[-1].language == "zh"
    response_params = response.intent.parameters
    assert response_params["startDate"] == "2024-01-01"
    assert response_params["__time_scope_authority"] == "user"
    assert response.intent.language == "zh"
    persisted = saved[-1]
    assert (persisted.start_date, persisted.time_scope_source, persisted.language) == ("2024-01-01", "user", "zh")

    # Turn 3 from the PERSISTED state: add a country; the user window holds.
    turn3 = FollowUpDelta(added_countries=["JP"], delta_type="add_country", raw_query="再加上日本")
    response3 = asyncio.run(svc._execute_collective_answer_member_delta(
        query="再加上日本", conversation_id="conv-mrtf", tracker=None, state=persisted, delta=turn3,
    ))
    assert response3 is not None and not response3.error
    turn3_members = captured[-2:]
    for intent in turn3_members:
        params = intent.parameters or {}
        assert params.get("startDate") == "2024-01-01"
        assert params.get("__time_scope_authority") == "user"
        assert intent.language == "zh"
    assert saved[-1].time_scope_source == "user"


def test_collective_shared_resolution_rewrite_keeps_provenance(monkeypatch):
    def reply(intent):
        country = (intent.parameters or {}).get("country") or "US"
        series = _series([("2023-01-01", 1.0)], frequency="annual", source="World Bank", country=country)
        series.metadata.seriesId = "NY.GDP.PCAP.CD"
        return [series]

    svc, captured, _saved = _collective_service(monkeypatch, reply)
    state = ConversationState(
        indicator="GDP", provider="WORLDBANK", countries=["US", "JP"],
        start_date="2019-01-01", end_date="2024-12-31", time_scope_source="user",
        language="zh", turn_number=2, original_query="美国和日本GDP",
        active_answer_members=[
            AnswerSetMember(provider="WORLDBANK", indicator_label="GDP", provider_code="NY.GDP.MKTP.CD", country="US"),
            AnswerSetMember(provider="WORLDBANK", indicator_label="GDP", provider_code="NY.GDP.MKTP.CD", country="JP"),
        ],
    )
    delta = FollowUpDelta(changed_indicator="GDP per capita", delta_type="indicator_change", raw_query="改成人均GDP")
    response = asyncio.run(svc._execute_collective_answer_member_delta(
        query="改成人均GDP", conversation_id="conv-mrtf", tracker=None, state=state, delta=delta,
    ))
    assert response is not None and not response.error
    assert len(captured) == 2
    rewritten = captured[1].parameters
    assert rewritten.get("__decision_source") == "collective_shared_resolution"
    for intent in captured:
        params = intent.parameters or {}
        assert params.get("startDate") == "2019-01-01"
        assert params.get("__time_scope_authority") == "user"
        assert intent.language == "zh"
