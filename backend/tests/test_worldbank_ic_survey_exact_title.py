"""WorldBank Enterprise-Survey / Doing-Business (IC.*) codes must not exact-lock.

Their titles normalize to bare macro phrases ("Capacity utilization (%)" ->
"capacity utilization"), so a generic query single-locked a sparse cross-country
survey indicator and bypassed routing, yielding No Data ("US capacity
utilization" -> IC.FRM.INNOV.T3). Excluding IC.* from the exact-title shortcut
lets such queries route normally (-> FRED TCU). Distinctive non-IC WorldBank
title pastes still lock.
"""
from __future__ import annotations

import pytest

from backend.services.indicator_resolution import find_exact_provider_title_match as match


@pytest.mark.parametrize("query", [
    "US capacity utilization",
    "capacity utilization",
    "annual employment growth",
    "closing a business",
    "documents to export",
])
def test_ic_survey_titles_do_not_exact_lock(query):
    result = match(query, "WORLDBANK")
    assert result is None or not str(result.get("code", "")).upper().startswith("IC."), (
        f"{query!r} -> {result.get('code') if result else None}"
    )


@pytest.mark.parametrize("query,code", [
    ("GDP (current US$)", "NY.GDP.MKTP.CD"),
    ("Population, total", "SP.POP.TOTL"),
    ("Inflation, consumer prices (annual %)", "FP.CPI.TOTL.ZG"),
    ("Unemployment, total (% of total labor force) (modeled ILO estimate)", "SL.UEM.TOTL.ZS"),
])
def test_legitimate_worldbank_title_pastes_still_lock(query, code):
    result = match(query, "WORLDBANK")
    assert result is not None and result.get("code") == code, (
        f"{query!r} -> {result.get('code') if result else None}"
    )
