"""Request-level selection authority: a confident, predicate-validated LLM pick
suppresses a REDUNDANT near-tie ask-gate — and ONLY that.

Root cause (verified live, "California GDP" flip-flop): temperature is already 0
everywhere, but vLLM continuous-batching non-determinism makes a RE-selection
inside the clarification machinery sometimes ASK where the main selection just
confidently PICKED. The main pick served correct data (CANGSP) and passed every
hard predicate, yet the later ask-gate still flipped the response to a
clarification MENU because comparator scores were close.

The contract: a confident ``llm_pick`` whose SERVED top series IS that pick and
passes the same hard predicates the response stage enforces carries AUTHORITY to
suppress the single near-tie ask-branch within the same request. It NEVER
suppresses refusal of wrong data — wrong-member / wrong-region / concept
mismatches fire on earlier HARD branches, before the near-tie branch this signal
is allowed to relax.

Two seams under test:
  * the MINT — SelectionResult.authoritative (the confidence signal): a real
    code, source ``llm_pick``, made WITHOUT the prefer_ask score-ambiguity bias;
  * the CONSUMPTION — _selection_authority_holds + needs_indicator_clarification:
    (a) an authoritative pick was minted this request, (b) the served top series
    IS that pick, (c) the region hard predicate passes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.services.indicator_clarification as ic
from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.indicator_clarification import (
    _selection_authority_holds,
    needs_indicator_clarification,
)
from backend.services.indicator_selector import SelectionResult


def _series(indicator: str, source: str = "FRED", series_id: str = "CANGSP",
            country: str = "US") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country=country,
            frequency="quarterly", unit="USD", seriesId=series_id,
        ),
        data=[{"date": "2025-01-01", "value": 1.0}],
    )


def _intent(*, region: str | None, pick_code: str | None, pick_provider: str | None) -> ParsedIntent:
    params: dict = {"country": "US", "indicator": pick_code or ""}
    if pick_code and pick_provider:
        params["__authoritative_pick_code"] = pick_code
        params["__authoritative_pick_provider"] = pick_provider
    return ParsedIntent(
        apiProvider="FRED",
        indicators=[pick_code or "GDP"],
        parameters=params,
        clarificationNeeded=False,
        subnationalRegion=region,
        originalQuery=("California GDP" if region else "US GDP"),
    )


def _qs() -> SimpleNamespace:
    """Fake QueryService exposing only what the authority seam reads, faithful
    to the real staticmethods (normalized provider + provider-native code; a
    substring region check over served metadata)."""
    return SimpleNamespace(
        _extract_series_provider_and_code=lambda s: (
            str(getattr(s.metadata, "source", "") or "").upper(),
            str(getattr(s.metadata, "seriesId", "") or "").upper(),
        ),
        _served_data_references_region=lambda data, region: any(
            region.lower() in str(getattr(sr.metadata, "country", "") or "").lower()
            or region.lower() in str(getattr(sr.metadata, "indicator", "") or "").lower()
            for sr in data
        ),
    )


# ---------------------------------------------------------------------------
# _selection_authority_holds — the structural (a)+(b)+(c) contract
# ---------------------------------------------------------------------------

def test_authority_holds_when_confident_pick_is_the_served_top_series() -> None:
    # (i): (a) authoritative pick minted, (b) served top series IS it, (c) no
    # region so the region predicate is a no-op → authority granted.
    intent = _intent(region=None, pick_code="CANGSP", pick_provider="FRED")
    top = _series("Real GDP: All Industries in California", series_id="CANGSP")
    assert _selection_authority_holds(_qs(), intent, top) == "CANGSP"


def test_authority_denied_when_served_code_differs_from_pick() -> None:
    # (ii): a national series was substituted after the pick — served code
    # (GDP) != picked code (CANGSP). Authority must not apply.
    intent = _intent(region=None, pick_code="CANGSP", pick_provider="FRED")
    served_national = _series("Gross Domestic Product", series_id="GDP")
    assert _selection_authority_holds(_qs(), intent, served_national) is None


def test_authority_denied_when_served_provider_differs_from_pick() -> None:
    intent = _intent(region=None, pick_code="CANGSP", pick_provider="FRED")
    other_provider = _series("Real GDP in California", source="WorldBank", series_id="CANGSP")
    assert _selection_authority_holds(_qs(), intent, other_provider) is None


def test_authority_denied_when_region_predicate_fails() -> None:
    # (iii): region set, but the served top series names only the nation — the
    # SAME hard predicate _enforce_subnational_fail_closed uses would discard it.
    intent = _intent(region="California", pick_code="GDP", pick_provider="FRED")
    national = _series("Gross Domestic Product", series_id="GDP", country="United States")
    assert _selection_authority_holds(_qs(), intent, national) is None


def test_authority_holds_when_region_predicate_passes() -> None:
    intent = _intent(region="California", pick_code="CANGSP", pick_provider="FRED")
    regional = _series("Real GDP: All Industries in California", series_id="CANGSP")
    assert _selection_authority_holds(_qs(), intent, regional) == "CANGSP"


def test_authority_denied_when_no_pick_minted() -> None:
    # (iv): an ASK / uncertain selection stamps NO __authoritative_pick_code, so
    # the served data — whatever it is — carries no authority.
    intent = _intent(region=None, pick_code=None, pick_provider=None)
    top = _series("Gross Domestic Product", series_id="GDP")
    assert _selection_authority_holds(_qs(), intent, top) is None


def test_authority_denied_when_intent_or_series_missing() -> None:
    intent = _intent(region=None, pick_code="CANGSP", pick_provider="FRED")
    assert _selection_authority_holds(_qs(), None, _series("x")) is None
    assert _selection_authority_holds(_qs(), intent, None) is None


# ---------------------------------------------------------------------------
# SelectionResult.authoritative — the MINT confidence signal (part of (a)/(iv))
# ---------------------------------------------------------------------------

def test_confident_pick_is_authoritative() -> None:
    assert SelectionResult(code="CANGSP", source="llm_pick").authoritative is True


def test_pick_under_prefer_ask_ambiguity_is_not_authoritative() -> None:
    # (iv): a pick produced under the prefer_ask score-ambiguity bias (retrieval
    # could not separate the candidates) must NOT carry authority.
    r = SelectionResult(code="CANGSP", source="llm_pick", made_under_prefer_ask=True)
    assert r.authoritative is False


def test_ask_result_is_not_authoritative() -> None:
    r = SelectionResult(
        code=None, source="user_choice",
        options=[{"code": "A", "name": "a"}, {"code": "B", "name": "b"}],
    )
    assert r.needs_user_choice is True
    assert r.authoritative is False


def test_reject_nodecision_metadata_conflict_not_authoritative() -> None:
    assert SelectionResult(code=None, source="llm_reject").authoritative is False
    assert SelectionResult(code=None, source="no_decision").authoritative is False
    # A code that resolved via metadata-conflict retry is NOT an llm_pick source.
    assert SelectionResult(code="X", source="metadata_conflict").authoritative is False


# ---------------------------------------------------------------------------
# (v) The opt-in pattern keeps old-signature fakes/callers working.
# ---------------------------------------------------------------------------

def test_old_signature_selectionresult_still_works() -> None:
    # Constructed WITHOUT the new made_under_prefer_ask kwarg — exactly how every
    # pre-existing caller and test fake builds it.
    r = SelectionResult(code="CANGSP", name="Real GDP California", source="llm_pick")
    assert r.made_under_prefer_ask is False
    assert r.authoritative is True


def test_mint_reads_authority_via_getattr_default() -> None:
    # The resolution seam mints only on getattr(selection, "authoritative", False):
    # a fake selection object lacking the attribute must default to no-authority.
    fake_selection = SimpleNamespace(code="X", source="llm_pick")
    assert getattr(fake_selection, "authoritative", False) is False


# ---------------------------------------------------------------------------
# End-to-end through the gate: authority relaxes the near-tie branch ONLY.
# ---------------------------------------------------------------------------

def _gate_qs() -> SimpleNamespace:
    qs = _qs()
    qs._detect_explicit_provider = lambda q: None
    qs._is_temporal_split_query = lambda q: False
    return qs


def _force_near_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the gate into its single soft near-tie branch deterministically.

    Scores: top 0.5, second 0.4 → gap 0.1 (< 0.2) and top < 0.7. Cues: the query
    carries none (no high-signal cues → strict/concept/temporal branches skip),
    the top and second series carry DIFFERENT cues (the near-tie tie-breaker).
    The catalog concept lookup is neutralized so it cannot fire an earlier HARD
    branch.
    """
    monkeypatch.setattr(
        ic, "_score_series_relevance",
        lambda q, s: {"CANGSP": 0.5, "OTHER": 0.4}.get(
            str(getattr(s.metadata, "seriesId", "") or "").upper(), 0.0
        ),
    )

    def _cues(text: str) -> set:
        if "SECONDCUE" in text:
            return {"bar"}
        if "TOPCUE" in text:
            return {"foo"}
        return set()

    monkeypatch.setattr(ic, "_extract_indicator_cues", _cues)
    import backend.services.catalog_service as cs
    monkeypatch.setattr(cs, "find_concept_by_term", lambda *a, **k: None)


def _near_tie_data() -> list:
    return [
        _series("TOPCUE California GDP", series_id="CANGSP"),
        _series("SECONDCUE something else", series_id="OTHER"),
    ]


def test_gate_suppresses_near_tie_ask_for_authoritative_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    # (i) end-to-end: near-tie WOULD flip to a menu, but the authoritative pick
    # (top served series == pick, predicates pass) suppresses it.
    _force_near_tie(monkeypatch)
    intent = _intent(region=None, pick_code="CANGSP", pick_provider="FRED")
    assert needs_indicator_clarification(
        _gate_qs(), "California GDP", _near_tie_data(), intent, caller="uncertain_result_builder",
    ) is False


def test_gate_still_asks_when_pick_not_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    # (iv) end-to-end: no authoritative pick → the near-tie flips as before.
    _force_near_tie(monkeypatch)
    intent = _intent(region=None, pick_code=None, pick_provider=None)
    assert needs_indicator_clarification(
        _gate_qs(), "California GDP", _near_tie_data(), intent, caller="uncertain_result_builder",
    ) is True


def test_gate_still_asks_when_served_top_differs_from_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    # (ii) end-to-end: the pick was CANGSP but the served top is a substituted
    # national series (GDP) — authority denied, the near-tie flips as before.
    _force_near_tie(monkeypatch)
    intent = _intent(region=None, pick_code="GDP", pick_provider="FRED")
    assert needs_indicator_clarification(
        _gate_qs(), "California GDP", _near_tie_data(), intent, caller="uncertain_result_builder",
    ) is True


def test_gate_still_asks_when_region_predicate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # (iii) end-to-end: region set but the served top series names only the
    # nation — authority denied, the near-tie flips as before.
    _force_near_tie(monkeypatch)
    intent = _intent(region="California", pick_code="CANGSP", pick_provider="FRED")
    data = [
        _series("TOPCUE Gross Domestic Product", series_id="CANGSP", country="United States"),
        _series("SECONDCUE something else", series_id="OTHER", country="United States"),
    ]
    assert needs_indicator_clarification(
        _gate_qs(), "California GDP", data, intent, caller="uncertain_result_builder",
    ) is True
