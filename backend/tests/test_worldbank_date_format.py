"""WorldBank quarterly/monthly observation dates must be valid ISO dates.

The batch fetch path emitted f"{raw}-01-01", so a quarterly token "2016Q1"
became the invalid string "2016Q1-01-01" (and frequency was hardcoded "annual")
for every non-annual WB series. _format_wb_date maps WB tokens to the period-start
ISO date.
"""
from __future__ import annotations

import pytest

from backend.providers.worldbank import WorldBankProvider


@pytest.mark.parametrize("raw,iso", [
    ("2016", "2016-01-01"),
    ("2016Q1", "2016-01-01"),
    ("2016Q2", "2016-04-01"),
    ("2016Q3", "2016-07-01"),
    ("2016Q4", "2016-10-01"),
    ("2004Q4", "2004-10-01"),
    ("2016M03", "2016-03-01"),
    ("2016M3", "2016-03-01"),
    ("2016M12", "2016-12-01"),
    ("2016-05-01", "2016-05-01"),  # already ISO -> unchanged
])
def test_format_wb_date(raw, iso):
    assert WorldBankProvider._format_wb_date(raw) == iso


def test_format_wb_date_never_emits_invalid_qm_suffix():
    for raw in ("2016Q1", "2016M03", "2020Q4", "1999M11"):
        out = WorldBankProvider._format_wb_date(raw)
        assert "Q" not in out and "M" not in out.upper().replace("-", ""), out
        # valid ISO yyyy-mm-dd
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", out), out
