"""Shared SDMX time-period parsing for SDMX-shaped providers.

Phase 3.2 of the deep review: the period->ISO-date logic was forked six
times across IMF / Eurostat / BIS (x3) / OECD with near-identical bodies.
Most copies agreed on a *start-of-period* convention, but OECD diverged
in a way that produced WRONG dates (Q1 -> "YYYY-03-01", two months late).
This module hosts the single canonical implementation; every provider's
period parser now delegates here.

Pure functions, no provider dependencies, no module-level state — safe to
import from anywhere.

THE CONVENTION (this is the bug fix):
    All SDMX periods map to the FIRST day of the period.
      - Annual   "YYYY"      -> "YYYY-01-01"
      - Quarter  "YYYY-Qn"   -> start-of-quarter, month = (n-1)*3 + 1
                                  Q1->01, Q2->04, Q3->07, Q4->10, day 01
      - Month    "YYYY-Mnn"  -> "YYYY-MM-01"
      - Month    "YYYY-MM"   -> "YYYY-MM-01"
    The IMF implementation (backend/providers/imf.py) is the correctness
    reference; this module reproduces it byte-for-byte and additionally
    tolerates the separator variants the other providers emit (BIS sees
    both "-Q" and bare "Q"; some inputs arrive without the dash before the
    quarter token, e.g. "YYYYQn").
"""
from __future__ import annotations

import re

# Quarter ordinal -> two-digit start-of-quarter month. (q-1)*3 + 1.
_QUARTER_START_MONTH = {"1": "01", "2": "04", "3": "07", "4": "10"}

_FREQUENCY_LABELS = {
    "A": "annual",
    "Q": "quarterly",
    "M": "monthly",
    "D": "daily",
    "W": "weekly",
}

# Pre-compiled period shape matchers.
_RE_ANNUAL = re.compile(r"\d{4}")
_RE_QUARTER = re.compile(r"(\d{4})-?Q([1-4])")
_RE_MONTH_M = re.compile(r"(\d{4})-M(\d{1,2})")
_RE_MONTH_DASH = re.compile(r"\d{4}-\d{2}")


def period_to_iso_date(period: str) -> str:
    """Normalize an SDMX time period to a start-of-period ISO date string.

    Handles the period shapes that SDMX-shaped providers emit and maps each
    to the FIRST day of its period (see module docstring for the rationale —
    this start-of-period convention is the cross-provider bug fix).

    Recognized inputs (whitespace-trimmed):
        "YYYY"        annual    -> "YYYY-01-01"
        "YYYY-Qn"     quarter   -> "YYYY-{01,04,07,10}-01"
        "YYYYQn"      quarter   -> same (tolerates missing dash)
        "YYYY-Mnn"    month     -> "YYYY-{nn:02d}-01"
        "YYYY-MM"     month     -> "YYYY-MM-01"

    Anything else is returned unchanged (the sensible fallback the original
    IMF implementation used), so malformed or already-ISO values pass
    through untouched rather than raising.

    Args:
        period: raw SDMX period token (e.g. "2020-Q2", "2020-M03", "2020").

    Returns:
        An ISO date string anchored to the start of the period, or the
        trimmed input unchanged when no shape matches.
    """
    value = str(period or "").strip()

    # Annual: bare four-digit year.
    if _RE_ANNUAL.fullmatch(value):
        return f"{value}-01-01"

    # Quarter: "YYYY-Qn" or "YYYYQn" -> start-of-quarter.
    match = _RE_QUARTER.fullmatch(value)
    if match:
        month = _QUARTER_START_MONTH[match.group(2)]
        return f"{match.group(1)}-{month}-01"

    # Month, "M" form: "YYYY-Mnn".
    match = _RE_MONTH_M.fullmatch(value)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-01"

    # Month, dash form: "YYYY-MM".
    if _RE_MONTH_DASH.fullmatch(value):
        return f"{value}-01"

    # Sensible fallback: leave unrecognized tokens untouched.
    return value


def frequency_label(code: str) -> str:
    """Map an SDMX frequency code to a human-readable label.

    A->annual, Q->quarterly, M->monthly, D->daily, W->weekly. Unknown
    codes fall back to the trimmed input itself, or "annual" when empty —
    preserving the IMF reference behavior.

    Args:
        code: SDMX frequency code (case-insensitive), e.g. "Q".

    Returns:
        A lowercase frequency label, the trimmed input for unknown codes,
        or "annual" when the input is empty.
    """
    key = str(code or "").strip().upper()
    return _FREQUENCY_LABELS.get(key, str(code or "").strip() or "annual")


def frequency_from_period(period: str) -> str:
    """Infer an SDMX frequency code (A/Q/M) from a period token's shape.

    Returns "A" for "YYYY", "Q" for quarter shapes ("YYYY-Qn"/"YYYYQn"),
    and "M" for month shapes ("YYYY-Mnn"/"YYYY-MM"). Falls back to "A" when
    the shape is unrecognized.

    Args:
        period: raw SDMX period token.

    Returns:
        One of "A", "Q", or "M".
    """
    value = str(period or "").strip()
    if _RE_QUARTER.fullmatch(value):
        return "Q"
    if _RE_MONTH_M.fullmatch(value) or _RE_MONTH_DASH.fullmatch(value):
        return "M"
    return "A"
