"""FRED country inference must not treat ISO3 codes as title aliases.

`_infer_country_from_fred_info` scans the provider-native title for country
names. It used aliases of length > 2, which pulled in the 3-letter ISO3 codes
("per"->PE, "are"->AE, "can"->CA, "nor"->NO, "pan"->PA, ...). Those collide with
ordinary English words, so "GDP per capita ... in Ohio" was tagged country=Peru
and survey text containing "are" was tagged United Arab Emirates -- ~9% of the
FRED catalog. Requiring aliases of length >= 4 (full country names) removes the
collisions while keeping every genuine country match.
"""
from __future__ import annotations

import pytest

from backend.providers.fred import _infer_country_from_fred_info, _country_aliases_for_iso2


def _infer(title: str) -> str:
    return _infer_country_from_fred_info({"title": title})


# ISO3-code false positives -> must resolve to the US default (no foreign country).
NOT_FOREIGN = [
    "Market Hotness: Listing Views per Property in Ohio County, WV",   # 'per' -> Peru
    "Real Gross Domestic Product per Capita",                          # 'per' -> Peru
    "13) To the Extent That the Price or Nonprice Terms Are Applied",  # 'are' -> UAE
    "Consumer Price Index for All Urban Consumers",
]

# Genuine country mentions (full names, >= 4 chars) must still be detected.
GENUINE = [
    ("Gross Domestic Product for Peru", "Peru"),
    ("Real GDP for Canada", "Canada"),
    ("Bank Deposits to GDP for Jordan", "Jordan"),
    ("Constant GDP per capita for Cuba", "Cuba"),
]


@pytest.mark.parametrize("title", NOT_FOREIGN)
def test_iso3_word_collisions_do_not_infer_foreign_country(title):
    assert _infer(title) in ("US", "United States Of America"), f"{title!r} -> {_infer(title)!r}"


@pytest.mark.parametrize("title,country", GENUINE)
def test_genuine_country_names_still_detected(title, country):
    assert _infer(title) == country, f"{title!r} -> {_infer(title)!r}"


def test_no_three_letter_aliases_returned():
    # The scan-alias list must contain only full names (>= 4 chars) now.
    for iso2 in ("PE", "AE", "CA", "NO", "PA"):
        aliases = _country_aliases_for_iso2(iso2)
        assert aliases, f"{iso2} lost all aliases"
        assert all(len(a) >= 4 for a in aliases), f"{iso2} still has a short alias: {aliases}"
