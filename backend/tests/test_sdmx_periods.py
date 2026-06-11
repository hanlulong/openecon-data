"""Tests for the shared SDMX period parser (Phase 3.2).

Two jobs:

1. Pin the canonical period_to_iso_date / frequency_label behavior on a
   battery of inputs including edge/garbage cases.

2. Prove the refactor is behavior-preserving for the providers that were
   ALREADY correct (IMF / Eurostat / BIS) by pasting each provider's OLD
   inline logic in here as an oracle and asserting byte-parity against the
   shared function across a representative input set. The OECD oracle, by
   contrast, asserts the NEW (corrected) behavior and documents the
   intended change away from OECD's prior buggy output.
"""
from __future__ import annotations

import pytest

from backend.providers._sdmx.periods import (
    frequency_from_period,
    frequency_label,
    period_to_iso_date,
)


# --------------------------------------------------------------------------
# 1. Canonical behavior battery.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "period, expected",
    [
        # Annual.
        ("2020", "2020-01-01"),
        ("1999", "1999-01-01"),
        # Quarterly, start-of-quarter convention: (q-1)*3 + 1.
        ("2020-Q1", "2020-01-01"),
        ("2020-Q2", "2020-04-01"),
        ("2020-Q3", "2020-07-01"),
        ("2020-Q4", "2020-10-01"),
        # Quarterly without the dash separator (BIS / variant inputs).
        ("2020Q1", "2020-01-01"),
        ("2020Q4", "2020-10-01"),
        # Monthly, "M" form.
        ("2020-M03", "2020-03-01"),
        ("2020-M1", "2020-01-01"),
        ("2020-M12", "2020-12-01"),
        # Monthly, dash form.
        ("2020-03", "2020-03-01"),
        ("2020-12", "2020-12-01"),
        # Whitespace is trimmed.
        ("  2020-Q2  ", "2020-04-01"),
    ],
)
def test_period_to_iso_date_battery(period: str, expected: str) -> None:
    assert period_to_iso_date(period) == expected


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "not-a-date",
        "2020-Q5",        # invalid quarter -> no match -> passthrough
        "20-Q1",          # not 4-digit year
        "2020-13",        # not a valid YYYY-MM shape? still passes shape regex
        None,
    ],
)
def test_period_to_iso_date_garbage_passes_through_or_trims(garbage) -> None:
    # Garbage / unrecognized shapes return the trimmed input unchanged
    # (the original IMF fallback), never raise.
    out = period_to_iso_date(garbage)
    assert isinstance(out, str)
    # "2020-13" matches the YYYY-MM shape regex and is treated as a month,
    # mirroring the original IMF impl (it does not range-check months).
    if garbage == "2020-13":
        assert out == "2020-13-01"
    elif garbage in ("", None):
        assert out == ""
    else:
        assert out == str(garbage).strip()


def test_frequency_label_cases() -> None:
    assert frequency_label("A") == "annual"
    assert frequency_label("Q") == "quarterly"
    assert frequency_label("M") == "monthly"
    assert frequency_label("D") == "daily"
    assert frequency_label("W") == "weekly"
    # Case-insensitive.
    assert frequency_label("q") == "quarterly"
    # Unknown code -> trimmed input.
    assert frequency_label("X") == "X"
    # Empty -> "annual".
    assert frequency_label("") == "annual"
    assert frequency_label(None) == "annual"


def test_frequency_from_period() -> None:
    assert frequency_from_period("2020") == "A"
    assert frequency_from_period("2020-Q3") == "Q"
    assert frequency_from_period("2020Q3") == "Q"
    assert frequency_from_period("2020-M06") == "M"
    assert frequency_from_period("2020-06") == "M"
    assert frequency_from_period("garbage") == "A"


# --------------------------------------------------------------------------
# 2. Provider oracles — byte-parity proofs.
#
# Each oracle is the provider's ORIGINAL inline period->date logic, copied
# verbatim so the test fails if the shared function ever drifts from the
# behavior the provider had before Phase 3.2.
# --------------------------------------------------------------------------

# Representative inputs the providers actually see. Monthly cases are split
# out because the old Eurostat/BIS-fork-3 oracles mishandled them (see the
# dedicated change-intent tests below).
_QUARTERLY_AND_ANNUAL_INPUTS = [
    "2020",
    "2019",
    "2020-Q1",
    "2020-Q2",
    "2020-Q3",
    "2020-Q4",
]

_MONTHLY_INPUTS = ["2020-03", "2020-12", "2020-M03", "2020-M12"]


def _imf_oracle(period: str) -> str:
    """Verbatim copy of imf.py:_period_to_date before Phase 3.2."""
    import re

    value = str(period or "").strip()
    if re.fullmatch(r"\d{4}", value):
        return f"{value}-01-01"
    match = re.fullmatch(r"(\d{4})-Q([1-4])", value)
    if match:
        month = {"1": "01", "2": "04", "3": "07", "4": "10"}[match.group(2)]
        return f"{match.group(1)}-{month}-01"
    match = re.fullmatch(r"(\d{4})-M(\d{1,2})", value)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-01"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return f"{value}-01"
    return value


def _eurostat_oracle(label: str) -> str:
    """Verbatim copy of eurostat.py:_normalize_time_label before Phase 3.2."""
    if label and "-" in label:
        if "Q" in label:
            year, quarter = label.split("-Q")
            month = (int(quarter) - 1) * 3 + 1
            return f"{year}-{month:02d}-01"
        if "M" in label:
            year, month = label.split("-")
            return f"{year}-{month}-01"
    return f"{label}-01-01"


def _bis_fork1_oracle(time_period: str) -> str:
    """Verbatim copy of bis.py fork 1 (~419) date branch before Phase 3.2."""
    if "-" in time_period:
        if "Q" in time_period:
            year, quarter = time_period.split("-Q", 1)
            month = (int(quarter) - 1) * 3 + 1
            return f"{year}-{month:02d}-01"
        else:
            return f"{time_period}-01"
    else:
        return f"{time_period}-01-01"


def _oecd_old_buggy_oracle(time_period: str) -> str:
    """Verbatim copy of oecd.py inline block BEFORE Phase 3.2 (the bug)."""
    time_period = str(time_period)
    if "-Q" in time_period:
        # Quarterly: BUG -> int(quarter) * 3 anchors two months late.
        year, quarter = time_period.split("-Q")
        month = int(quarter) * 3
        return f"{year}-{month:02d}-01"
    elif "-" in time_period and len(time_period.split("-")) == 2:
        return f"{time_period}-01"
    else:
        return f"{time_period}-01-01"


@pytest.mark.parametrize("period", _QUARTERLY_AND_ANNUAL_INPUTS + _MONTHLY_INPUTS)
def test_parity_imf(period: str) -> None:
    # IMF was fully correct for every shape; shared func must match byte-for-byte.
    assert period_to_iso_date(period) == _imf_oracle(period)


@pytest.mark.parametrize("period", _QUARTERLY_AND_ANNUAL_INPUTS)
def test_parity_eurostat_annual_quarterly(period: str) -> None:
    # Eurostat was correct for annual + quarterly shapes.
    assert period_to_iso_date(period) == _eurostat_oracle(period)


@pytest.mark.parametrize("period", _QUARTERLY_AND_ANNUAL_INPUTS)
def test_parity_bis_fork1_annual_quarterly(period: str) -> None:
    # BIS fork 1 was correct for annual + quarterly + YYYY-MM monthly shapes.
    assert period_to_iso_date(period) == _bis_fork1_oracle(period)


@pytest.mark.parametrize("period", ["2020-03", "2020-12"])
def test_parity_bis_fork1_monthly(period: str) -> None:
    # BIS fork 1's non-Q branch used f"{time_period}-01" for YYYY-MM, which
    # the shared function reproduces exactly.
    assert period_to_iso_date(period) == _bis_fork1_oracle(period)


# --------------------------------------------------------------------------
# 3. Change-intent tests — these document deliberate corrections.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "period, old_buggy, new_correct",
    [
        ("2020-Q1", "2020-03-01", "2020-01-01"),
        ("2020-Q2", "2020-06-01", "2020-04-01"),
        ("2020-Q3", "2020-09-01", "2020-07-01"),
        ("2020-Q4", "2020-12-01", "2020-10-01"),
    ],
)
def test_oecd_quarter_bug_is_fixed(period, old_buggy, new_correct) -> None:
    """OECD CHANGE-INTENT: quarters now align with the other providers.

    Before Phase 3.2 OECD computed month = int(quarter) * 3, anchoring each
    quarter two months late. This asserts the OLD output was buggy, the NEW
    output is the start-of-quarter convention, and that they DIFFER -- so the
    test fails loudly if anyone reintroduces the old formula.
    """
    assert _oecd_old_buggy_oracle(period) == old_buggy  # the bug, documented
    assert period_to_iso_date(period) == new_correct      # the fix
    assert old_buggy != new_correct                       # behavior changed


@pytest.mark.parametrize("period", ["2020", "2020-03", "2020-01"])
def test_oecd_non_quarter_unchanged(period: str) -> None:
    # OECD annual + YYYY-MM monthly were already correct; the fix leaves them.
    assert period_to_iso_date(period) == _oecd_old_buggy_oracle(period)


@pytest.mark.parametrize("period", ["2020-03", "2020-12"])
def test_eurostat_monthly_bug_is_fixed(period: str) -> None:
    """Eurostat CHANGE-INTENT: YYYY-MM monthly labels are now correct.

    The old _normalize_time_label fell through both inner branches for a
    "YYYY-MM" label (no literal "M", "Q" absent) and produced the malformed
    "YYYY-MM-01-01". The shared parser yields the correct "YYYY-MM-01".
    """
    old_malformed = _eurostat_oracle(period)
    assert old_malformed == f"{period}-01-01"     # the latent bug, documented
    assert period_to_iso_date(period) == f"{period}-01"  # the fix
    assert old_malformed != period_to_iso_date(period)
