"""_rank_results must match query tokens / country names as WHOLE WORDS.

The +2 name boost and the -15 wrong-country penalty used bare substring
containment, so the token "us" scored ~54k FRED titles (AustraliA, bUSiness,
censUS) and the wrong-country code "uk" penalized "Milwaukee"/"Kilowatt". The
_word_in helper requires whole-word matches. (Both ranker sides operate on
already-lowercased text.)
"""
from __future__ import annotations

import pytest

from backend.services.indicator_database import _word_in


@pytest.mark.parametrize("word,text,expected", [
    ("us", "australia", False),
    ("us", "business", False),
    ("us", "census bureau", False),
    ("us", "real gdp in the us", True),
    ("uk", "milwaukee employment", False),
    ("uk", "kilowatt hours", False),
    ("uk", "unemployment in the uk", True),
    ("m2", "cm2 education", False),
    ("m2", "m2 money stock", True),
    ("rate", "aggregate reserves", False),
    ("rate", "unemployment rate", True),
    ("euro area", "euro area gdp", True),
    ("canada", "canada gdp", True),
])
def test_word_in_whole_word_only(word, text, expected):
    assert _word_in(word, text) is expected


def test_word_in_empty_word_is_false():
    assert _word_in("", "anything") is False
