"""Arbitration must enforce the same hard predicates as the response stage.

Root-caused live 2026-07-16 ("Ontario unemployment rate"): dispatch fetched
correct StatsCan Ontario data, then uncertain-match RECOVERY fetched
cross-provider candidates (including OECD, which must never be auto-routed)
and served OECD's national series because the comparator scored only lexical
label relevance. The response-stage subnational fail-closed check then
discarded it — the user got nothing although correct data had been fetched.

Two capability-level guards close the gap:
- provider_strategy.MANUAL_ONLY_PROVIDERS / provider_is_auto_routable: OECD
  joins candidate fan-out only on explicit user request.
- maybe_recover_from_uncertain_match applies the response stage's own
  _served_data_references_region predicate to every candidate, so a national
  series can never WIN recovery for a sub-regional query.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.indicator_clarification import (
    collect_indicator_choice_options,
    maybe_recover_from_uncertain_match,
)
from backend.services.provider_strategy import (
    MANUAL_ONLY_PROVIDERS,
    provider_is_auto_routable,
)


def _series(indicator: str, country: str, source: str) -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country=country,
            frequency="monthly", unit="percent",
        ),
        data=[{"date": "2026-05-01", "value": 5.5}],
    )


def _intent(region: str | None) -> ParsedIntent:
    return ParsedIntent(
        apiProvider="StatsCan",
        indicators=["unemployment rate"],
        parameters={"country": "CA"},
        clarificationNeeded=False,
        subnationalRegion=region,
        originalQuery="Ontario unemployment rate" if region else "Canada unemployment rate",
    )


# ---------------------------------------------------------------------------
# provider_is_auto_routable
# ---------------------------------------------------------------------------

def test_oecd_is_manual_only_and_gate_honors_explicit_request() -> None:
    assert "OECD" in MANUAL_ONLY_PROVIDERS
    assert provider_is_auto_routable("OECD") is False
    assert provider_is_auto_routable("OECD", explicit_provider="OECD") is True
    for provider in ("FRED", "WORLDBANK", "IMF", "BIS", "EUROSTAT", "STATSCAN"):
        assert provider_is_auto_routable(provider) is True


# ---------------------------------------------------------------------------
# Candidate fan-out excludes manual-only providers unless explicitly named
# ---------------------------------------------------------------------------

def _fanout_qs(providers_probed: list, explicit: str | None):
    class _Selector:
        def _get_candidates_with_scores(self, query, provider, top_k=8):
            providers_probed.append(provider)
            return [], []

    return SimpleNamespace(
        _select_indicator_query_for_resolution=lambda intent: "unemployment rate",
        _get_fallback_providers=lambda *a, **k: [],
        _detect_explicit_provider=lambda q: explicit,
        _collect_target_countries=lambda params: [],
        _extract_countries_from_query=lambda q: [],
    ), _Selector


def test_fanout_skips_oecd_without_explicit_request(monkeypatch) -> None:
    probed: list = []
    qs, selector_cls = _fanout_qs(probed, explicit=None)
    import backend.services.indicator_selector as sel_mod
    monkeypatch.setattr(sel_mod, "IndicatorSelector", selector_cls)

    collect_indicator_choice_options(qs, "Ontario unemployment rate", _intent("Ontario"))
    assert "OECD" not in probed
    assert "WORLDBANK" in probed  # the rest of the fan-out is unaffected


def test_fanout_includes_oecd_when_user_names_it(monkeypatch) -> None:
    probed: list = []
    qs, selector_cls = _fanout_qs(probed, explicit="OECD")
    import backend.services.indicator_selector as sel_mod
    monkeypatch.setattr(sel_mod, "IndicatorSelector", selector_cls)

    intent = _intent(None)
    intent.originalQuery = "Canada unemployment rate from OECD"
    collect_indicator_choice_options(qs, intent.originalQuery, intent)
    assert "OECD" in probed


# ---------------------------------------------------------------------------
# Recovery comparator: national candidate must not win a sub-regional query
# ---------------------------------------------------------------------------

def _recovery_qs(candidate: list, region_checks: list):
    async def _fetch_data(intent):
        return candidate

    def _references_region(data, region):
        region_checks.append(region)
        return any(
            region.lower() in str(getattr(s.metadata, "country", "") or "").lower()
            for s in data
        )

    return SimpleNamespace(
        # Initial result is "uncertain"; fetched candidates are confident so
        # they WOULD win on score but for the region predicate.
        _needs_indicator_clarification=lambda q, d, i: d is not None
        and str(getattr(d[0].metadata, "source", "")) == "StatsCan",
        _extract_series_provider_and_code=lambda s: ("STATSCAN", "14100375"),
        _collect_target_countries=lambda params: [],
        _select_indicator_query_for_resolution=lambda intent: "unemployment rate",
        _detect_explicit_provider=lambda q: None,
        _collect_indicator_choice_options=lambda q, i, max_options=4: [
            "[WorldBank] Unemployment rate (SL.UEM.TOTL.ZS)"
        ],
        _fetch_data=_fetch_data,
        _rerank_data_by_query_relevance=lambda q, d: d,
        _is_ranking_query=lambda q: False,
        _has_implausible_top_series=lambda q, d: False,
        _served_data_references_region=_references_region,
    )


def test_recovery_rejects_national_candidate_for_regional_query() -> None:
    # Poor-titled but region-correct current data vs a lexically-perfect
    # national candidate: with a region set, the candidate must be skipped.
    current = [_series("Table 14-10-0375", "Ontario", "StatsCan")]
    national = [_series("Unemployment rate", "Canada", "WorldBank")]
    qs = _recovery_qs(national, region_checks := [])

    out = asyncio.run(
        maybe_recover_from_uncertain_match(qs, "Ontario unemployment rate", _intent("Ontario"), current)
    )
    assert out is None, "national candidate must not replace region-correct data"
    assert region_checks == ["Ontario"], "the response-stage predicate must be consulted"


def test_recovery_still_works_without_region() -> None:
    # Control: same setup, no region — proves the fixture actually reaches the
    # win condition and the gate (not a broken fixture) is what blocks above.
    current = [_series("Table 14-10-0375", "Canada", "StatsCan")]
    national = [_series("Unemployment rate", "Canada", "WorldBank")]
    qs = _recovery_qs(national, [])

    out = asyncio.run(
        maybe_recover_from_uncertain_match(qs, "Canada unemployment rate", _intent(None), current)
    )
    assert out == national
