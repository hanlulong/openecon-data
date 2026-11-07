"""Specialization penalties must match TERMS, not substrings.

Root cause (verified 2026-11-07 against indicators.db): the subgroup penalty
lists in ``specialization_mismatch_penalty`` were matched with ``term in text``,
so the 3-letter gender terms fired inside unrelated words -- "men" in
increMENt / governMENt / employMENt / investMENt, "man" in MANufacturing /
GerMANy / OMAN / huMAN. ~37% of all catalog names (120,949 / 330,073) carried a
false 1.6 gender penalty, turning correct series (ChinaMacro social-financing
increment, IMF general-government debt % of GDP, China manufacturing PMI) into
junk-menu material. On the QUERY side the same bug made "unemployment" /
"Germany" / "Oman" queries look like they asked for a gender subgroup, so the
legitimate penalty on "Unemployment Rate - Women" was suppressed.

The fix is one structural tokenizer (word-start on both sides; whole word with
optional plural for terms of <= 3 characters). These tests pin the MECHANISM
over many word shapes, not one triggering query.
"""
from __future__ import annotations

import pytest

from backend.services.relevance_scorer import (
    labor_rate_specificity_penalty,
    specialization_mismatch_penalty,
    text_mentions_term,
)

_GENDER_PENALTY = 1.6


# Generic query vs candidate whose name only CONTAINS a short gender term as a
# substring of another word: no subgroup penalty may apply.
@pytest.mark.parametrize(
    "query, candidate",
    [
        ("china social financing", "china social financing monthly increment"),
        ("us debt to gdp ratio", "general government gross debt percent of gdp"),
        ("us jobs", "civilian employment level"),
        ("us gross investment", "gross private domestic investment"),
        ("germany gdp", "gross domestic product for germany"),
        ("china pmi", "china manufacturing pmi"),
        ("oman inflation", "inflation, consumer prices oman"),
        ("human development", "human development index"),
        ("romania gdp", "gdp for romania"),
        ("us industrial production", "industrial production: manufacturing (sic)"),
    ],
)
def test_substring_of_short_term_does_not_trigger_gender_penalty(query: str, candidate: str) -> None:
    assert specialization_mismatch_penalty(query, candidate) == 0.0


# Generic query vs a genuine gender-subgroup series: the penalty must apply,
# including when the QUERY contains a short term only as a substring.
@pytest.mark.parametrize(
    "query, candidate",
    [
        ("us unemployment rate", "unemployment rate - women"),
        ("government spending", "total government expenditure for men"),
        ("germany employment", "employment, female"),
        ("manufacturing output", "manufacturing employees, men's"),
        ("us population", "population by sex"),
        ("us population", "population, both sexes"),
        ("us population", "population, male"),
    ],
)
def test_real_gender_subgroup_is_penalized_for_generic_query(query: str, candidate: str) -> None:
    assert specialization_mismatch_penalty(query, candidate) >= _GENDER_PENALTY


@pytest.mark.parametrize(
    "query, candidate",
    [
        ("female labor force participation", "labor force participation rate, female"),
        ("unemployment rate for women", "unemployment rate - women"),
        ("population by sex", "population, male"),
    ],
)
def test_query_that_names_the_subgroup_suppresses_the_penalty(query: str, candidate: str) -> None:
    assert specialization_mismatch_penalty(query, candidate) < _GENDER_PENALTY


def test_longer_terms_keep_word_start_prefix_matching() -> None:
    # Inflected forms of longer terms still count on both sides.
    assert specialization_mismatch_penalty("us gdp", "government educational expenditure") >= 2.4
    assert specialization_mismatch_penalty("regional gdp canada", "gdp by province") == 0.0
    # "urbanization" names the urban subgroup -> no urban penalty for urban series.
    assert specialization_mismatch_penalty(
        "china urbanization rate", "urban population (% of total population)"
    ) == 0.0


def test_term_inside_another_word_no_longer_matches_documented_shift() -> None:
    # Deliberate, documented behavior shift of word-start matching: a term
    # glued to a prefix without a separator no longer matches (substring
    # matching penalized these), while the separated spelling still does.
    assert not text_mentions_term("nonmetropolitan counties", "metropolitan")
    assert text_mentions_term("non-metropolitan counties", "metropolitan")
    assert not text_mentions_term("suburban population", "urban")
    # Known edge case: a place name that IS the short word still matches.
    assert text_mentions_term("isle of man gdp", "man")


def test_labor_rate_sibling_check_does_not_match_inside_unemployment() -> None:
    # WB SL.UEM.TOTL.ZS is the canonical unemployment-rate series; it must not
    # be treated as an EMPLOYMENT-rate sibling because "employment" appears
    # inside "unemployment".
    candidate = "unemployment, total (% of total labor force) sl.uem.totl.zs worldbank"
    assert labor_rate_specificity_penalty("unemployment rate united states", candidate) == 0.0
    # A real employment-rate sibling is still penalized for an unemployment query.
    assert labor_rate_specificity_penalty(
        "unemployment rate united states", "employment rate, total"
    ) > 2.0


def test_text_mentions_term_lowercases_its_input() -> None:
    # The compiled patterns are lower-case; the helper must not silently
    # return False for a mixed-case caller ("Women", "MEN").
    assert text_mentions_term("Unemployment Rate - Women", "women")
    assert text_mentions_term("EMPLOYED MEN", "men")
    assert not text_mentions_term("GOVERNMENT DEBT", "men")
