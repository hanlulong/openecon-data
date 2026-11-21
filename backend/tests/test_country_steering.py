"""_apply_country_constraint reports whether a country-specific series exists.

The selector surfaces the target country to the LLM picker ONLY when the
candidate set actually contains a series specifically about that country, so a
country-only follow-up ("US GDP per capita" -> "for Canada") is steered to the
"for Canada" series instead of re-picking the generic US series. For
country-agnostic concepts the country must NOT be surfaced (no bias).
"""
from __future__ import annotations

from backend.services.indicator_selector import IndicatorSelector
from backend.services import indicator_selector as selector
from backend.utils.jurisdictions import country_evidence_text


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


def test_ambiguous_subdivision_title_does_not_claim_the_other_country():
    candidates = [("GANGSP", "Gross Domestic Product: All Industry Total in Georgia"),
                  ("GDP", "Gross Domestic Product")]
    kept, _, all_conflict, has_match = _constrain("US", candidates)
    assert kept == candidates
    assert not all_conflict and not has_match
    assert selector._derive_candidate_country("GAUR", "Unemployment Rate in Georgia") is None
    assert selector._derive_candidate_country("anything", "GDP in Niger") is None
    assert selector._derive_candidate_country("anything", "GDP in Luxembourg") is None


def test_same_parent_collisions_still_establish_country_coverage():
    assert selector._derive_candidate_country("anything", "GDP in Mexico") == "MX"
    assert country_evidence_text("GDP in Belize") == "GDP in Belize"


def test_explicit_country_evidence_and_native_suffix_remain_authoritative(monkeypatch):
    assert selector._derive_candidate_country("anything", "GDP in the country of Georgia") == "GE"
    assert selector._derive_candidate_country("anything", "GDP in Georgia (country)") == "GE"
    assert selector._derive_candidate_country("anything", "GDP in Georgia, United States") == "US"
    assert selector._derive_candidate_country("anything", "GDP in US") == "US"
    assert selector._derive_candidate_country("anything", "GDP in CAN") == "CA"
    assert selector._derive_candidate_country("anything", "GDP in CN") == "CN"
    assert selector._derive_candidate_country("anything", "GDP in US, Georgia") == "US"
    monkeypatch.setattr(selector, "_iso3_suffix_family_exists", lambda code: True)
    assert selector._derive_candidate_country("FAMILYUSA", "GDP in Georgia") == "US"
    monkeypatch.setattr(selector, "_iso3_suffix_family_exists", lambda code: False)
    assert selector._derive_candidate_country("FAMILYUSA", "GDP in Georgia") is None


def test_bilateral_titles_and_known_foreign_country_filters_are_preserved(monkeypatch):
    monkeypatch.setattr(selector, "_iso3_suffix_family_exists", lambda code: True)
    assert selector._derive_candidate_country("FAMILYUSA", "Imports from Georgia") is None
    assert selector._derive_candidate_country("anything", "Exports from US to China") is None
    kept, _, all_conflict, has_match = _constrain("Peru", [
        ("PE", "GDP for Peru"), ("BR", "GDP for Brazil"),
    ])
    assert kept == [("PE", "GDP for Peru")]
    assert not all_conflict and has_match
