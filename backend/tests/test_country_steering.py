"""_apply_country_constraint reports whether a country-specific series exists.

The selector surfaces the target country to the LLM picker ONLY when the
candidate set actually contains a series specifically about that country, so a
country-only follow-up ("US GDP per capita" -> "for Canada") is steered to the
"for Canada" series instead of re-picking the generic US series. For
country-agnostic concepts the country must NOT be surfaced (no bias).
"""
from __future__ import annotations

from backend.services.indicator_selector import IndicatorSelector


def _constrain(country, candidates):
    scores = [1.0 - 0.1 * i for i in range(len(candidates))]
    return IndicatorSelector._apply_country_constraint(country, candidates, scores)


def test_has_match_true_when_country_specific_candidate_present():
    cands = [
        ("NYGDPPCAPKDUSA", "Constant GDP per capita for the United States"),
        ("NYGDPPCAPKDCAN", "Constant GDP per capita for Canada"),
    ]
    kept, scores, all_conflict, has_match = _constrain("Canada", cands)
    assert has_match is True
    assert all_conflict is False
    # the Canada series is kept; the US series is dropped as a country conflict
    assert any(c[0] == "NYGDPPCAPKDCAN" for c in kept)
    assert all(c[0] != "NYGDPPCAPKDUSA" for c in kept)


def test_has_match_false_for_country_agnostic_candidates():
    # Neutral series (no country evidence in code or title) -> no country match,
    # nothing dropped, country must not be surfaced to the picker.
    cands = [("CPIAUCSL", "Consumer Price Index for All Urban Consumers"),
             ("GDP", "Gross Domestic Product")]
    kept, scores, all_conflict, has_match = _constrain("US", cands)
    assert has_match is False
    assert kept == cands  # untouched


def test_no_op_when_country_unresolvable():
    cands = [("BTC", "Bitcoin price")]
    kept, scores, all_conflict, has_match = _constrain("", cands)
    assert has_match is False
    assert all_conflict is False
    assert kept == cands
