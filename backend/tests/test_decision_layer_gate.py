"""Decision layer (ask / answer / recover) contracts.

Root cause (live 2026-11-07, "US debt to GDP ratio" / "What is the
debt-to-GDP ratio of the United States?"): the LLM picked IMF GGXWDG_NGDP
(general government gross debt, % of GDP), the uncertainty gate flagged it,
and uncertain-match RECOVERY served FRED HOUSEHOLD debt to GDP -- wrong data.
Three mechanism defects made that possible:

1. recovery candidates rode the gate's high-confidence pre-resolved skip
   (only option-validation fetches were exempt), so they were never judged;
2. a gate-UNCERTAIN candidate could still win on a lexical score margin;
3. a lexical comparator could REPLACE the request's authoritative LLM pick.

These tests run the REAL gate (``needs_indicator_clarification`` bound on the
fake service) and the real relevance scorer / catalog: a stubbed gate would
pass while production stays wrong. They assert INTENT (household debt is never
the answer to a government-debt question), never a pinned winner id.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import textwrap
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

import backend.services.indicator_clarification as ic
from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.indicator_clarification import (
    _is_judged_candidate_fetch,
    build_uncertain_result_clarification,
    maybe_recover_from_uncertain_match,
    needs_indicator_clarification,
    parse_indicator_option,
)


def _series(indicator: str, series_id: str, source: str, *, country: str = "United States",
            unit: str = "Percent of GDP") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country=country,
            frequency="annual", unit=unit, seriesId=series_id,
        ),
        data=[{"date": "2024-01-01", "value": 100.0}],
    )


IMF_GOV_DEBT = ("General government gross debt", "GGXWDG_NGDP", "IMF")
FRED_HOUSEHOLD_US = ("Household Debt to GDP for United States", "HDTGPDUSQ163N", "FRED")
FRED_HOUSEHOLD_AU = ("Household Debt to GDP for Australia", "HDTGPDAUQ163N", "FRED")

DEBT_QUERIES = [
    "US debt to GDP ratio",
    "What is the debt-to-GDP ratio of the United States?",
]


def _norm(provider: str) -> str:
    return ic.normalize_provider_name(provider)


def _debt_intent(query: str, *, authoritative: bool) -> ParsedIntent:
    params: dict = {"country": "US", "indicator": "GGXWDG_NGDP"}
    if authoritative:
        params["__authoritative_pick_code"] = "GGXWDG_NGDP"
        params["__authoritative_pick_provider"] = "IMF"
    return ParsedIntent(
        apiProvider="IMF",
        indicators=["GGXWDG_NGDP"],
        parameters=params,
        clarificationNeeded=False,
        confidence=0.95,
        originalQuery=query,
    )


def _real_gate_qs(candidates: List[tuple], fetch_calls: list) -> SimpleNamespace:
    """Fake service whose gate is the REAL needs_indicator_clarification."""
    by_code = {code: (name, code, src) for name, code, src in candidates}

    async def _fetch_data(intent: ParsedIntent):
        code = str((intent.parameters or {}).get("indicator") or "")
        fetch_calls.append(code)
        name, code, src = by_code[code]
        country = "Australia" if code.startswith("HDTGPDAU") else "United States"
        return [_series(name, code, src, country=country)]

    qs = SimpleNamespace(
        _extract_series_provider_and_code=lambda s: (
            _norm(str(s.metadata.source)), str(s.metadata.seriesId or "")
        ),
        _detect_explicit_provider=lambda q: None,
        _is_temporal_split_query=lambda q: False,
        _served_data_references_region=lambda d, r: True,
        _collect_indicator_choice_options=lambda q, i, max_options=4: [
            f"[{src}] {name} ({code})" for name, code, src in candidates
        ],
        _fetch_data=_fetch_data,
        _rerank_data_by_query_relevance=lambda q, d: d,
        _is_ranking_query=lambda q: False,
        _has_implausible_top_series=lambda q, d: False,
    )
    qs._needs_indicator_clarification = (
        lambda q, d, i=None, caller="": needs_indicator_clarification(qs, q, d, i, caller=caller)
    )
    return qs


def _gate_records(caplog: pytest.LogCaptureFixture) -> List[dict]:
    out = []
    for record in caplog.records:
        msg = record.getMessage()
        if msg.startswith("uncertainty_gate "):
            out.append(json.loads(msg[len("uncertainty_gate "):]))
    return out


def _recovery_records(caplog: pytest.LogCaptureFixture) -> List[dict]:
    out = []
    for record in caplog.records:
        msg = record.getMessage()
        if msg.startswith("recovery_decision "):
            out.append(json.loads(msg[len("recovery_decision "):]))
    return out


# ---------------------------------------------------------------------------
# (1a) judged-candidate seam
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "params, expected",
    [
        ({"_prefetch_option_validation": True}, True),
        ({"_uncertain_recovery_attempted": True}, True),
        ({}, False),
        (None, False),
        ({"indicator": "UNRATE"}, False),
    ],
)
def test_is_judged_candidate_fetch(params: Optional[dict], expected: bool) -> None:
    assert _is_judged_candidate_fetch(params) is expected


def test_recovery_candidate_is_not_exempted_by_high_confidence_skip(caplog) -> None:
    # A FRED-shaped code + confidence 0.95 used to take the pre-resolved skip.
    qs = _real_gate_qs([FRED_HOUSEHOLD_US], [])
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["HDTGPDUSQ163N"],
        parameters={"indicator": "HDTGPDUSQ163N", "_uncertain_recovery_attempted": True},
        clarificationNeeded=False,
        confidence=0.95,
        originalQuery="US debt to GDP ratio",
    )
    with caplog.at_level(logging.INFO, logger=ic.logger.name):
        needs_indicator_clarification(
            qs, "US debt to GDP ratio", [_series(*FRED_HOUSEHOLD_US)], intent,
            caller="recovery_candidate",
        )
    branches = [r["branch"] for r in _gate_records(caplog)]
    assert len(branches) == 1
    assert branches != ["high_confidence_pre_resolved"]

    # Control: the SAME fetch on the main path still takes the skip.
    caplog.clear()
    intent.parameters = {"indicator": "HDTGPDUSQ163N"}
    with caplog.at_level(logging.INFO, logger=ic.logger.name):
        assert needs_indicator_clarification(
            qs, "US debt to GDP ratio", [_series(*FRED_HOUSEHOLD_US)], intent, caller="main",
        ) is False
    assert [r["branch"] for r in _gate_records(caplog)] == ["high_confidence_pre_resolved"]


# ---------------------------------------------------------------------------
# Headline regression: household debt never replaces government debt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", DEBT_QUERIES)
def test_household_debt_never_wins_recovery_without_authority(query: str, caplog) -> None:
    # No authority stamp: the catalog concept check on the JUDGED candidate
    # must reject household debt even though it out-scores lexically.
    fetch_calls: list = []
    qs = _real_gate_qs([FRED_HOUSEHOLD_US, FRED_HOUSEHOLD_AU], fetch_calls)
    current = [_series(*IMF_GOV_DEBT)]
    with caplog.at_level(logging.INFO, logger=ic.logger.name):
        out = asyncio.run(
            maybe_recover_from_uncertain_match(qs, query, _debt_intent(query, authoritative=False), current)
        )
    served = out or current
    served_codes = {str(s.metadata.seriesId) for s in served}
    assert not served_codes & {"HDTGPDUSQ163N", "HDTGPDAUQ163N"}, (
        "household debt must never answer a government debt-to-GDP question"
    )
    for record in _gate_records(caplog):
        if record["caller"] == "recovery_candidate":
            assert record["branch"] != "high_confidence_pre_resolved"


@pytest.mark.parametrize("query", DEBT_QUERIES)
def test_authoritative_pick_is_never_replaced_by_lexical_recovery(query: str, caplog) -> None:
    fetch_calls: list = []
    qs = _real_gate_qs([FRED_HOUSEHOLD_US], fetch_calls)
    current = [_series(*IMF_GOV_DEBT)]
    with caplog.at_level(logging.INFO, logger=ic.logger.name):
        out = asyncio.run(
            maybe_recover_from_uncertain_match(qs, query, _debt_intent(query, authoritative=True), current)
        )
    assert out is None
    assert fetch_calls == [], "recovery must not even fetch replacements for an authoritative pick"
    recovery_gate = [r for r in _gate_records(caplog) if r["caller"] == "recovery_gate"]
    if recovery_gate and recovery_gate[0]["outcome"]:
        decisions = _recovery_records(caplog)
        assert len(decisions) == 1 and decisions[0]["blocked"] == "authoritative_pick"


def test_judged_candidate_with_matching_concept_can_still_win(caplog) -> None:
    # Control: recovery is not disabled -- a judged candidate that the gate
    # finds confident and that beats the current score by the margin wins.
    # (Lambda gate: this exercises the comparator, not the gate.)
    current = [_series("Table 14-10-0375", "14100375", "StatsCan", country="Canada", unit="percent")]
    better = [_series("Unemployment rate", "SL.UEM.TOTL.ZS", "WorldBank", country="Canada", unit="percent")]

    async def _fetch(intent):
        return better

    qs = SimpleNamespace(
        _needs_indicator_clarification=lambda q, d, i, caller="": str(d[0].metadata.source) == "StatsCan",
        _extract_series_provider_and_code=lambda s: (_norm(str(s.metadata.source)), str(s.metadata.seriesId)),
        _detect_explicit_provider=lambda q: None,
        _collect_indicator_choice_options=lambda q, i, max_options=4: [
            "[WorldBank] Unemployment rate (SL.UEM.TOTL.ZS)"
        ],
        _fetch_data=_fetch,
        _rerank_data_by_query_relevance=lambda q, d: d,
        _is_ranking_query=lambda q: False,
        _has_implausible_top_series=lambda q, d: False,
        _served_data_references_region=lambda d, r: True,
    )
    intent = ParsedIntent(
        apiProvider="StatsCan", indicators=["unemployment rate"], parameters={"country": "CA"},
        clarificationNeeded=False, originalQuery="Canada unemployment rate",
    )
    with caplog.at_level(logging.INFO, logger=ic.logger.name):
        out = asyncio.run(maybe_recover_from_uncertain_match(qs, "Canada unemployment rate", intent, current))
    assert out == better
    decisions = _recovery_records(caplog)
    assert len(decisions) == 1
    assert decisions[0]["winner"] == "WORLDBANK:SL.UEM.TOTL.ZS"
    assert decisions[0]["candidates"][0]["verdict"] == "accepted"


def test_gate_uncertain_candidate_cannot_win_on_lexical_margin(caplog) -> None:
    # Before: `or candidate_score >= best + 0.35` let a candidate the gate had
    # just judged UNCERTAIN win purely on word overlap.
    current = [_series("Table 14-10-0375", "14100375", "StatsCan", country="Canada", unit="percent")]
    lexically_better = [_series("Unemployment rate", "SL.UEM.TOTL.ZS", "WorldBank", country="Canada", unit="percent")]

    async def _fetch(intent):
        return lexically_better

    qs = SimpleNamespace(
        _needs_indicator_clarification=lambda q, d, i, caller="": True,  # everything uncertain
        _extract_series_provider_and_code=lambda s: (_norm(str(s.metadata.source)), str(s.metadata.seriesId)),
        _detect_explicit_provider=lambda q: None,
        _collect_indicator_choice_options=lambda q, i, max_options=4: [
            "[WorldBank] Unemployment rate (SL.UEM.TOTL.ZS)"
        ],
        _fetch_data=_fetch,
        _rerank_data_by_query_relevance=lambda q, d: d,
        _is_ranking_query=lambda q: False,
        _has_implausible_top_series=lambda q, d: False,
        _served_data_references_region=lambda d, r: True,
    )
    intent = ParsedIntent(
        apiProvider="StatsCan", indicators=["unemployment rate"], parameters={"country": "CA"},
        clarificationNeeded=False, originalQuery="Canada unemployment rate",
    )
    with caplog.at_level(logging.INFO, logger=ic.logger.name):
        out = asyncio.run(maybe_recover_from_uncertain_match(qs, "Canada unemployment rate", intent, current))
    assert out is None
    decisions = _recovery_records(caplog)
    assert len(decisions) == 1
    assert decisions[0]["winner"] is None
    assert decisions[0]["candidates"][0]["verdict"] == "gate_uncertain"


# ---------------------------------------------------------------------------
# (3) every gate decision emits exactly one structured record
# ---------------------------------------------------------------------------

def _gate_return_nodes() -> List[ast.Return]:
    source = textwrap.dedent(inspect.getsource(needs_indicator_clarification))
    func = ast.parse(source).body[0]
    returns: List[ast.Return] = []

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # the nested _gate_log itself
            if isinstance(child, ast.Return):
                returns.append(child)
            _walk(child)

    _walk(func)
    return returns


def test_every_gate_return_goes_through_gate_log() -> None:
    returns = _gate_return_nodes()
    assert len(returns) >= 15
    branch_names = []
    for node in returns:
        call = node.value
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_gate_log", (
            f"gate return at line {node.lineno} bypasses _gate_log"
        )
        assert isinstance(call.args[1], ast.Constant)
        branch_names.append(call.args[1].value)
    assert len(branch_names) == len(set(branch_names)), "branch names must be unique and stable"


def _simple_qs() -> SimpleNamespace:
    return SimpleNamespace(
        _extract_series_provider_and_code=lambda s: (_norm(str(s.metadata.source)), str(s.metadata.seriesId or "")),
        _detect_explicit_provider=lambda q: None,
        _is_temporal_split_query=lambda q: False,
        _served_data_references_region=lambda d, r: True,
    )


@pytest.mark.parametrize(
    "query, data, intent, expected_branch",
    [
        ("us gdp", [], None, "no_data"),
        (
            "US debt to GDP ratio",
            [_series(*IMF_GOV_DEBT)],
            ParsedIntent(apiProvider="IMF", indicators=["GGXWDG_NGDP"],
                         parameters={"indicator": "GGXWDG_NGDP"}, clarificationNeeded=False,
                         originalQuery="US debt to GDP ratio"),
            "debt_ratio_cue",
        ),
        (
            "US unemployment rate",
            [_series("Unemployment Rate", "UNRATE", "FRED", unit="percent")],
            ParsedIntent(apiProvider="FRED", indicators=["UNRATE"], parameters={"indicator": "UNRATE"},
                         clarificationNeeded=False, confidence=0.95, originalQuery="US unemployment rate"),
            "high_confidence_pre_resolved",
        ),
        (
            "zzqx",
            [_series("Qqq", "QQQ1", "FRED", unit="index")],
            None,
            "low_top_score",
        ),
    ],
)
def test_gate_branch_emits_exactly_one_record(query, data, intent, expected_branch, caplog) -> None:
    with caplog.at_level(logging.INFO, logger=ic.logger.name):
        needs_indicator_clarification(_simple_qs(), query, data, intent, caller="t")
    records = _gate_records(caplog)
    assert len(records) == 1
    assert records[0]["branch"] == expected_branch
    assert {"top_score", "second_score", "top"} <= set(records[0])
    if expected_branch not in {"no_data", "high_confidence_pre_resolved"}:
        assert records[0]["top"] and records[0]["top_score"] is not None


# ---------------------------------------------------------------------------
# (4) post-fetch menu keeps the served series selectable
# ---------------------------------------------------------------------------

def _builder_qs(collected: List[str], served_uncertain: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _needs_indicator_clarification=lambda q, d, i, caller="": served_uncertain,
        _collect_indicator_choice_options=lambda q, i, max_options=3: list(collected),
        _extract_series_provider_and_code=lambda s: (_norm(str(s.metadata.source)), str(s.metadata.seriesId or "")),
        bis_provider=SimpleNamespace(_lookup_dataflow_info=lambda code: ("", "")),
    )


def _capture_pending(monkeypatch) -> list:
    stored: list = []
    monkeypatch.setattr(
        ic.conversation_manager, "set_pending_indicator_options",
        lambda conv_id, payload: stored.append(payload),
    )
    return stored


def _option_codes(options: List[str]) -> List[tuple]:
    return [parse_indicator_option(o) for o in options]


def test_served_series_is_selectable_when_menu_has_two_or_more_options(monkeypatch) -> None:
    stored = _capture_pending(monkeypatch)
    collected = [
        "[FRED] Household Debt to GDP for United States (HDTGPDUSQ163N)",
        "[FRED] Federal Debt: Total Public Debt as Percent of Gross Domestic Product (GFDEGDQ188S)",
        "[WorldBank] Central government debt, total (% of GDP) (GC.DOD.TOTL.GD.ZS)",
    ]
    query = "US debt to GDP ratio"
    response = build_uncertain_result_clarification(
        _builder_qs(collected), "conv-x", query, _debt_intent(query, authoritative=False),
        [_series(*IMF_GOV_DEBT)],
    )
    assert response is not None and response.clarificationNeeded
    labels = [opt.label for opt in (response.clarificationOptions or [])]
    stored_options = stored[-1]["options"]
    parsed = _option_codes(stored_options)
    assert parsed.count(("IMF", "GGXWDG_NGDP")) == 1, "served series must appear exactly once"
    for option in collected:
        assert option in stored_options, "no collected option may be dropped"
    assert len(stored_options) == len(collected) + 1
    assert len(labels) == len(stored_options)
    # Displayed numbered lines and stored options share one order.
    numbered = [line for line in response.clarificationQuestions if line[:2].rstrip(".").isdigit()]
    assert [line.split(". ", 1)[1] for line in numbered] == stored_options


def test_served_series_is_not_duplicated_when_already_collected(monkeypatch) -> None:
    stored = _capture_pending(monkeypatch)
    collected = [
        "[FRED] Household Debt to GDP for United States (HDTGPDUSQ163N)",
        "[IMF] General government gross debt (GGXWDG_NGDP)",
        "[FRED] Federal Debt: Total Public Debt as Percent of Gross Domestic Product (GFDEGDQ188S)",
    ]
    query = "US debt to GDP ratio"
    build_uncertain_result_clarification(
        _builder_qs(collected), "conv-y", query, _debt_intent(query, authoritative=False),
        [_series(*IMF_GOV_DEBT)],
    )
    parsed = _option_codes(stored[-1]["options"])
    assert parsed.count(("IMF", "GGXWDG_NGDP")) == 1


def test_served_series_below_gate_low_score_threshold_is_not_offered(monkeypatch) -> None:
    # Debt-service ratios for a debt-to-GDP question scores far below the
    # gate's low_top_score threshold: known-wrong data is never an option.
    stored = _capture_pending(monkeypatch)
    collected = [
        "[WorldBank] Central government debt, total (% of GDP) (GC.DOD.TOTL.GD.ZS)",
        "[IMF] Government Debt (% of GDP) (GGXWDG_GDP)",
    ]
    intent = ParsedIntent(
        apiProvider="BIS", indicators=["debt to GDP ratio"], parameters={"country": "CN"},
        clarificationNeeded=False, originalQuery="debt to GDP ratio in China",
    )
    served = [_series("Debt service ratios", "WS_DSR", "BIS", country="CN", unit="percent")]
    build_uncertain_result_clarification(
        _builder_qs(collected), "conv-w", intent.originalQuery, intent, served,
    )
    assert stored[-1]["options"] == collected


def test_served_series_with_directional_conflict_is_not_offered(monkeypatch) -> None:
    stored = _capture_pending(monkeypatch)
    collected = [
        "[WorldBank] Imports of goods and services (% of GDP) (NE.IMP.GNFS.ZS)",
        "[IMF] Imports of goods and services (TMG_GDP)",
    ]
    served_exports = [_series("Exports of goods and services (% of GDP)", "NE.EXP.GNFS.ZS", "WorldBank",
                              unit="% of GDP")]
    intent = ParsedIntent(
        apiProvider="WorldBank", indicators=["NE.EXP.GNFS.ZS"], parameters={"indicator": "NE.EXP.GNFS.ZS"},
        clarificationNeeded=False, originalQuery="US imports share of GDP",
    )
    response = build_uncertain_result_clarification(
        _builder_qs(collected), "conv-z", "US imports share of GDP", intent, served_exports,
    )
    offered = _option_codes(stored[-1]["options"]) if stored else []
    if response is not None and response.clarificationOptions:
        offered += [(o.provider and _norm(o.provider), o.code) for o in response.clarificationOptions]
    assert ("WORLDBANK", "NE.EXP.GNFS.ZS") not in offered, "known-wrong-direction data is never an option"
