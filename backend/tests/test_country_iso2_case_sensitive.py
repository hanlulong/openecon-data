"""2-letter ISO2 country codes must match case-sensitively (uppercase only).

Many ISO2 codes are also ordinary English words ("in"->IN, "is"->IS, "no"->NO,
"to"->TO, "me"->ME, "it"->IT, "do"->DO, "at"->AT, "de"->DE). Matching them
case-insensitively turned every preposition in a query/title into a phantom
country signal (~half of all catalog titles, polluting the exact-title country
rank). The fix requires <=2-char aliases to appear as an explicit UPPERCASE token
while keeping >=3-char full names case-insensitive.
"""
from __future__ import annotations

import pytest

from backend.services.indicator_resolution import (
    _extract_country_codes_from_text as extract,
    _country_reordered_exact_title_variants as reordered,
)


# English-word ISO2 collisions must NOT be extracted as countries.
@pytest.mark.parametrize("text,absent", [
    ("US inflation in 2020", "IN"),
    ("inflation in Germany", "IN"),
    ("show me unemployment", "ME"),
    ("is there housing data", "IS"),
    ("no recession data", "NO"),
    ("do we have trade data", "DO"),
    ("employment in the united states", "IN"),
])
def test_lowercase_word_iso2_codes_are_not_countries(text, absent):
    assert absent not in extract(text), f"{text!r} -> {sorted(extract(text))}"


# Real country references must still resolve.
@pytest.mark.parametrize("text,expected", [
    ("US CPI", {"US"}),
    ("US inflation in 2020", {"US"}),
    ("UK unemployment", {"GB"}),
    ("Canada GDP", {"CA"}),
    ("India GDP", {"IN"}),          # via full name "india"
    ("Iceland inflation", {"IS"}),  # via full name "iceland"
    ("Norway oil price", {"NO"}),   # via full name "norway"
    ("inflation in Germany", {"DE"}),
    ("compare US and China GDP", {"US", "CN"}),
    ("employment in the united states", {"US"}),
])
def test_real_countries_still_resolve(text, expected):
    assert extract(text) == expected, f"{text!r} -> {sorted(extract(text))}"


def test_uppercase_country_wrapper_still_strips_but_not_preposition():
    assert reordered("GDP for US") == ["US GDP"]
    assert reordered("GDP for in") == []  # "in" is not a country wrapper
