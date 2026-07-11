"""Guards: the selector's confident-pick cache.

The FTS + embedding + LLM adjudication pipeline dominates per-query latency
and the same concept recurs across users. Only confident llm_pick results are
cached; ambiguous/user-choice/rejection outcomes and alternate-retry calls
(exclude_codes) always run the full pipeline.
"""

import asyncio

import pytest

import backend.services.indicator_selector as sel_mod
from backend.services.indicator_selector import IndicatorSelector, SelectionResult


@pytest.fixture(autouse=True)
def _clean_cache():
    sel_mod._SELECTION_CACHE.clear()
    yield
    sel_mod._SELECTION_CACHE.clear()


def test_confident_pick_cached_and_reused(monkeypatch):
    selector = IndicatorSelector()
    calls = []

    async def fake_uncached(query, provider, **kw):
        calls.append(query)
        return SelectionResult(code="UNRATE", name="Unemployment Rate", source="llm_pick")

    monkeypatch.setattr(selector, "_select_uncached", fake_uncached)
    r1 = asyncio.run(selector.select("unemployment rate", "FRED", country="US"))
    r2 = asyncio.run(selector.select("unemployment rate", "FRED", country="US"))
    assert r1.code == r2.code == "UNRATE"
    assert len(calls) == 1  # second call served from cache


def test_ambiguous_results_not_cached(monkeypatch):
    selector = IndicatorSelector()
    calls = []

    async def fake_uncached(query, provider, **kw):
        calls.append(query)
        return SelectionResult(code=None, source="user_choice", options=[{"code": "A", "name": "a"}])

    monkeypatch.setattr(selector, "_select_uncached", fake_uncached)
    asyncio.run(selector.select("ambiguous thing", "FRED"))
    asyncio.run(selector.select("ambiguous thing", "FRED"))
    assert len(calls) == 2


def test_exclude_codes_bypasses_cache(monkeypatch):
    selector = IndicatorSelector()
    calls = []

    async def fake_uncached(query, provider, **kw):
        calls.append(kw.get("exclude_codes"))
        return SelectionResult(code="GDPC1", name="Real GDP", source="llm_pick")

    monkeypatch.setattr(selector, "_select_uncached", fake_uncached)
    asyncio.run(selector.select("gdp", "FRED"))
    asyncio.run(selector.select("gdp", "FRED", exclude_codes={"GDP"}))
    asyncio.run(selector.select("gdp", "FRED", exclude_codes={"GDP"}))
    assert len(calls) == 3  # first populates; exclusion calls never use cache


def test_cache_keys_differ_by_country_and_language_arm(monkeypatch):
    selector = IndicatorSelector()
    calls = []

    async def fake_uncached(query, provider, **kw):
        calls.append((query, kw.get("country"), kw.get("english_terms")))
        return SelectionResult(code="X", name="x", source="llm_pick")

    monkeypatch.setattr(selector, "_select_uncached", fake_uncached)
    asyncio.run(selector.select("unemployment rate", "STATSCAN", country="CA"))
    asyncio.run(selector.select("unemployment rate", "STATSCAN", country=None))
    asyncio.run(selector.select("失业率", "STATSCAN", country="CA", english_terms="unemployment rate Canada"))
    assert len(calls) == 3
