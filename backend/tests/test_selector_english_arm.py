"""Guard tests for the English canonical retrieval arm (Proposal A.2).

A non-English query keeps its own retrieval arms AND gains an additional FTS5 +
embedding arm on the English canonical metric name, fused into the same RRF. The
extra arm is a strict no-op when the English terms are empty or identical to the
primary query, so English queries keep their exact ranking.
"""
from __future__ import annotations

import asyncio

import pytest

import backend.services.embedding_retrieval as er_mod
from backend.services.indicator_selector import IndicatorSelector


class _StubEmbedder:
    """Embedding arm returns nothing; FTS5 drives the fusion deterministically."""

    def search(self, text, provider=None, top_k=50):
        return []


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch):
    monkeypatch.setattr(er_mod, "get_embedding_retrieval", lambda: _StubEmbedder())


def _selector(fts_map, calls):
    sel = IndicatorSelector()

    def fake_fts5(text, provider, top_k=20):
        calls.append(text)
        return fts_map(text)

    # Instance override: _get_candidates_with_scores calls self._get_candidates_fts5.
    sel._get_candidates_fts5 = fake_fts5
    return sel


def test_english_arm_fuses_english_hits():
    calls = []

    def fts_map(text):
        if text == "unemployment rate":
            return [("ENG_CODE", "unemployment rate")]
        return [("RAW_CODE", "raw")]

    sel = _selector(fts_map, calls)
    cands, _scores = sel._get_candidates_with_scores(
        "失业率", "WorldBank", english_query="unemployment rate",
    )
    codes = [c for c, _ in cands]
    assert "ENG_CODE" in codes   # english-arm recall folded in
    assert "RAW_CODE" in codes   # primary arm preserved
    assert calls == ["失业率", "unemployment rate"]  # BOTH arms retrieved


def test_identical_english_skips_second_retrieval():
    calls = []
    sel = _selector(lambda t: [("ONLY", t)], calls)
    sel._get_candidates_with_scores(
        "unemployment rate", "WorldBank", english_query="unemployment rate",
    )
    assert calls == ["unemployment rate"]  # no duplicate arm


def test_case_insensitive_identity_skips_arm():
    calls = []
    sel = _selector(lambda t: [("ONLY", t)], calls)
    sel._get_candidates_with_scores("GDP", "WorldBank", english_query="gdp")
    assert calls == ["GDP"]


def test_empty_english_skips_arm():
    calls = []
    sel = _selector(lambda t: [("ONLY", t)], calls)
    sel._get_candidates_with_scores("gdp", "WorldBank", english_query=None)
    assert calls == ["gdp"]


def test_english_input_ranking_unchanged():
    # english_query identical to query must yield the same candidate ordering
    # as omitting it entirely (no ranking perturbation for English queries).
    fmap = lambda t: [("A", "a"), ("B", "b")]
    base = _selector(fmap, [])._get_candidates_with_scores("gdp", "WorldBank")
    with_id = _selector(fmap, [])._get_candidates_with_scores(
        "gdp", "WorldBank", english_query="gdp",
    )
    assert [c for c, _ in base[0]] == [c for c, _ in with_id[0]]


def test_select_threads_english_terms_to_retrieval():
    sel = IndicatorSelector()
    captured = {}

    def fake_gcws(query, provider, top_k=50, english_query=None):
        captured["query"] = query
        captured["english_query"] = english_query
        return [], []

    sel._get_candidates_with_scores = fake_gcws
    result = asyncio.run(sel.select("失业率", "WorldBank", english_terms="unemployment rate"))
    assert captured["english_query"] == "unemployment rate"
    assert result.code is None  # empty candidate set short-circuits cleanly
