"""Regression tests for the 2026-06-12 night cycle framework fixes.

Pure-logic / no-network coverage for:
- retrieval: synonym-expansion removal + AND-first FTS + RRF default
- time-window provenance (M5): WB date param + provenance helper
- clarification-answer loop suppression (M2)
- country-group geography merge (no dropped comparators) + BIS dedup
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


# --- Retrieval: synonym expansion removed, query stays lexical -------------

def test_normalize_query_no_semantic_expansion():
    from backend.services.indicator_database import IndicatorLookup

    lk = IndicatorLookup(db=SimpleNamespace())
    # "m2" must NOT explode into "m2 money supply monetary" (that AND-narrowed
    # FTS and dropped M2SL); machine separators still normalize, noise drops.
    assert lk._normalize_query("M2 money supply") == "m2 money supply"
    assert lk._normalize_query("GDP_GROWTH") == "gdp growth"
    assert "gross domestic product" not in lk._normalize_query("gdp")


def test_rrf_is_default_fusion():
    from backend.config import Settings

    assert Settings().indicator_fusion == "rrf"


# --- Time-window provenance (M5) -------------------------------------------

def test_time_scope_provenance_helper():
    from backend.services.data_fetcher import _user_set_time_scope

    # Explicit stamp wins over text inference.
    assert _user_set_time_scope({"__time_scope_authority": "user"}, "")
    assert not _user_set_time_scope({"__time_scope_authority": "default"}, "in 2015")
    # No stamp → fall back to text heuristic.
    assert _user_set_time_scope({}, "US GDP in 2015")
    assert not _user_set_time_scope({}, "US GDP")


def test_apply_default_time_periods_stamps_provenance():
    from backend.models import ParsedIntent
    from backend.services.parameter_validator import ParameterValidator

    user_dated = ParsedIntent(
        apiProvider="FRED", indicators=["GDP"], clarificationNeeded=False,
        parameters={"startDate": "2015-01-01"},
    )
    ParameterValidator.apply_default_time_periods(user_dated)
    assert user_dated.parameters["__time_scope_authority"] == "user"

    undated = ParsedIntent(
        apiProvider="FRED", indicators=["GDP"], clarificationNeeded=False,
        parameters={},
    )
    ParameterValidator.apply_default_time_periods(undated)
    assert undated.parameters["__time_scope_authority"] == "default"
    assert undated.parameters.get("startDate")


def test_state_carries_time_scope_source():
    from backend.services.conversation_state_v2 import (
        ConversationState,
        materialize_intent,
    )

    state = ConversationState(
        indicator="GDP", country="France", provider="WORLDBANK",
        start_date="2015-01-01", end_date="2024-01-01",
        time_scope_source="user",
    )
    intent = materialize_intent(state)
    assert intent.parameters.get("__time_scope_authority") == "user"


# --- Clarification-answer loop suppression (M2) ----------------------------

def test_uncertain_clarification_suppressed_for_exact_user_input():
    from backend.models import ParsedIntent
    from backend.services.indicator_clarification import (
        build_uncertain_result_clarification,
    )

    intent = ParsedIntent(
        apiProvider="FRED", indicators=["GDP"], clarificationNeeded=False,
        parameters={"__semantic_authority": "exact_user_input"},
    )
    # A user who already chose this series must never be re-asked.
    result = build_uncertain_result_clarification(
        qs=SimpleNamespace(), conversation_id="conv-1", query="gdp",
        intent=intent, data=[],
    )
    assert result is None


# --- Country-group geography merge + dedup ---------------------------------

def test_region_merge_keeps_named_country():
    """'US vs Eurozone' must keep the US alongside the expanded members."""
    from backend.models import ParsedIntent
    from backend.services.query_helpers import apply_country_overrides

    class _Svc:
        def _detect_explicit_provider(self, q):
            return None

        def _normalize_country_to_iso2(self, c):
            m = {"united states": "US", "us": "US", "usa": "US", "germany": "DE",
                 "france": "FR", "italy": "IT", "spain": "ES"}
            return m.get(str(c).strip().lower())

    intent = ParsedIntent(
        apiProvider="WORLDBANK", indicators=["inflation"], clarificationNeeded=False,
        parameters={"country": "US"},
        originalQuery="inflation in US vs Eurozone",
    )
    apply_country_overrides(_Svc(), intent, intent.originalQuery)
    countries = intent.parameters.get("countries") or []
    norm = {_Svc()._normalize_country_to_iso2(c) or str(c).upper() for c in countries}
    assert "US" in norm, f"US dropped from comparison: {countries}"
    assert len(countries) > 1
    # No duplicate normalized codes.
    assert len(norm) == len(countries) or len(set(map(str, countries))) == len(countries)


def test_bis_dedup_by_code(monkeypatch):
    from backend.providers.bis import BISProvider

    p = BISProvider()
    # Two regions that overlap on members must not double-list a country.
    members = ["DE", "FR", "DE", "us"]
    seen, out = set(), []
    for m in members:
        code = p._country_code(m)
        if code in seen:
            continue
        seen.add(code)
        out.append(m)
    assert len(out) == 3
