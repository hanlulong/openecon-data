"""Exact-title matcher must not hard-lock a partial query to an over-specific title.

`_is_close_exact_title_match` used an ASYMMETRIC overlap ratio (shared / shorter
side), so any query fully contained in a longer title scored 1.0 -- even when the
title's extra tokens carried meaning. That let "real GDP growth" hard-lock to
WorldBank "Real AGRICULTURAL GDP growth rates" (which then errors), and any short
query lock to a more-specific sibling ("all employees manufacturing" -> "... in
Georgia"). The symmetric-overlap floor (shared / longer side >= 0.7) rejects those
while preserving genuine equality, country-wrapper, and near-equal reorder matches.

Rejecting a false shortcut is safe: the query simply falls through to the robust
FTS + embedding + LLM resolution path instead of being force-locked.
"""
from __future__ import annotations

import re

from backend.services.indicator_resolution import _is_close_exact_title_match


def _n(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _match(query: str, name: str) -> bool:
    return _is_close_exact_title_match(_n(query), _n(name))


# --- False positives the symmetric floor must now REJECT ---

def test_rejects_meaning_bearing_qualifier_leak():
    # The production bug: a national real-GDP-growth query locking to the
    # agricultural sub-series.
    assert not _match("real GDP growth", "Real agricultural GDP growth rates")
    # Geography qualifier leak.
    assert not _match("all employees manufacturing", "All Employees Manufacturing in Georgia")
    # Sub-category qualifier leak.
    assert not _match("advance retail sales", "Advance Retail Sales Grocery Stores")
    # Denomination qualifier leak (a National-currency sibling also exists).
    assert not _match("outward direct investment positions", "Outward Direct Investment Positions US Dollars")


# --- Legitimate matches the change must PRESERVE ---

def test_preserves_exact_and_near_equal_matches():
    # Identical title -> always matches (equality path).
    assert _match("gross domestic product", "gross domestic product")
    # A full pasted title still matches itself even with trailing unit punctuation
    # collapsed by normalization (equality path).
    assert _match("Completion rate upper secondary education female", "Completion rate, upper secondary education, female")
    # Near-equal light-wrapper where the symmetric overlap stays high.
    # 5-of-6 contained -> 0.83 >= 0.7 still matches.
    assert _match("a b c d e", "a b c d e f")
    # 5-of-7 contained -> 0.714 >= 0.7 still matches (boundary stays inclusive).
    assert _match("a b c d e", "a b c d e f g")


def test_symmetric_floor_boundary_rejects_two_extra_meaning_tokens():
    # 3-of-5 contained -> 0.6 < 0.7 -> rejected (the bug shape).
    assert not _match("a b c", "a b c d e")
    # 4-of-6 contained -> 0.67 < 0.7 -> rejected.
    assert not _match("a b c d", "a b c d e f")
