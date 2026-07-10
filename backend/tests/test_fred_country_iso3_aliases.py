"""FRED country inference must not treat ISO3 codes as title aliases, and must
not invent a country from a mere word collision.

`_infer_country_from_fred_info` reads the provider-native title. It uses aliases
of length >= 4 (full country names); the 3-letter ISO3 codes ("per"->PE,
"are"->AE, "can"->CA, "nor"->NO, "pan"->PA, ...) collide with ordinary English
words and are dropped. FIX 4 further requires a positive SUBJECT-geography signal
(leading "<Country>" or "for <Country>"): a collision word buried mid-title no
longer infers any country, and an unlabeled US series is left UNSET rather than
defaulting to "US". See test_fred_country_structured_signal.py for the full
contract.
"""
from __future__ import annotations

import pytest

from backend.providers.fred import _infer_country_from_fred_info, _country_aliases_for_iso2


def _infer(title: str) -> str:
    return _infer_country_from_fred_info({"title": title})


# ISO3 / word collisions buried mid-title must infer NO foreign country. With
# no leading-country or "for <Country>" signal these now resolve to UNSET.
NOT_FOREIGN = [
    "Market Hotness: Listing Views per Property in Ohio County, WV",   # 'per' -> Peru
    "Real Gross Domestic Product per Capita",                          # 'per' -> Peru
    "13) To the Extent That the Price or Nonprice Terms Are Applied",  # 'are' -> UAE
    "Consumer Price Index for All Urban Consumers",                    # 'for All' is not a country
]

# Genuine subject-geography mentions ("for <Country>") must still be detected.
GENUINE = [
    ("Gross Domestic Product for Peru", "Peru"),
    ("Real GDP for Canada", "Canada"),
    ("Bank Deposits to GDP for Jordan", "Jordan"),
    ("Constant GDP per capita for Cuba", "Cuba"),
]


@pytest.mark.parametrize("title", NOT_FOREIGN)
def test_iso3_word_collisions_do_not_infer_foreign_country(title):
    # Must not be any foreign country; absent a positive signal it is UNSET.
    assert _infer(title) is None, f"{title!r} -> {_infer(title)!r}"


@pytest.mark.parametrize("title,country", GENUINE)
def test_genuine_country_names_still_detected(title, country):
    assert _infer(title) == country, f"{title!r} -> {_infer(title)!r}"


@pytest.mark.parametrize("title,expected", [
    # Positive US signal -> one consistent "US" label.
    ("Gross Domestic Product for the United States", "US"),
    ("U.S. National Income", "US"),
    ("Federal Debt: Total Public Debt for the United States of America", "US"),
    # No positive signal -> UNSET, not a blanket "US" default.
    ("Real Gross Domestic Product", None),
])
def test_us_series_label_only_with_positive_signal(title, expected):
    assert _infer(title) == expected, f"{title!r} -> {_infer(title)!r}"


def test_no_three_letter_aliases_returned():
    # The scan-alias list must contain only full names (>= 4 chars) now.
    for iso2 in ("PE", "AE", "CA", "NO", "PA"):
        aliases = _country_aliases_for_iso2(iso2)
        assert aliases, f"{iso2} lost all aliases"
        assert all(len(a) >= 4 for a in aliases), f"{iso2} still has a short alias: {aliases}"
