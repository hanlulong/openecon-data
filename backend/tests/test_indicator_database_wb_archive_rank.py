"""Guard tests for FIX 4 — World Bank archive-source ranking penalty.

Every World Bank row has a NULL end_date, so the staleness scorer in
_rank_results never fires for them. The one structured freshness signal present
on 100% of WB rows is raw_metadata.source. Series published under an archival or
discontinued database ("WDI Database Archives", "Doing Business", ...) must be
strongly de-ranked so their live equivalents win. The rule keys on the SOURCE
field's lifecycle semantics, applied uniformly — not on a list of indicator ids.
"""
from __future__ import annotations

import json

from backend.services.indicator_database import IndicatorLookup


def _lookup() -> IndicatorLookup:
    # _rank_results is pure (no DB access); construct without running __init__.
    return IndicatorLookup.__new__(IndicatorLookup)


def _wb_row(code: str, source_value: str, relevance: float = -5.0) -> dict:
    return {
        "code": code,
        "name": "Access to electricity (% of population)",
        "provider": "WorldBank",
        "relevance": relevance,
        "popularity": 0,
        "raw_metadata": json.dumps({"source": {"id": "2", "value": source_value}}),
    }


def test_archive_source_ranks_below_live_equivalent():
    lk = _lookup()
    rows = [
        _wb_row("EG.ELC.ACCS.ZS.ARCH", "WDI Database Archives"),
        _wb_row("EG.ELC.ACCS.ZS", "World Development Indicators"),
    ]
    ranked = lk._rank_results(rows, "access to electricity")
    assert ranked[0]["code"] == "EG.ELC.ACCS.ZS"
    # The archive row must have been actively penalized, not merely tied.
    scores = {r["code"]: r["_score"] for r in ranked}
    assert scores["EG.ELC.ACCS.ZS"] - scores["EG.ELC.ACCS.ZS.ARCH"] >= 20


def test_doing_business_discontinued_source_penalized():
    lk = _lookup()
    rows = [
        _wb_row("IC.DB.X", "Doing Business"),
        _wb_row("IC.LIVE.X", "World Development Indicators"),
    ]
    ranked = lk._rank_results(rows, "business")
    assert ranked[0]["code"] == "IC.LIVE.X"


def test_archive_marker_is_case_insensitive():
    lk = _lookup()
    rows = [
        _wb_row("A.ARCH", "FPN Datahub ARCHIVE"),
        _wb_row("A.LIVE", "World Development Indicators"),
    ]
    ranked = lk._rank_results(rows, "access")
    assert ranked[0]["code"] == "A.LIVE"


def test_missing_or_malformed_raw_metadata_does_not_crash():
    lk = _lookup()
    rows = [
        {"code": "X", "name": "x", "provider": "WorldBank", "relevance": -5.0,
         "popularity": 0, "raw_metadata": None},
        {"code": "Y", "name": "y", "provider": "WorldBank", "relevance": -5.0,
         "popularity": 0, "raw_metadata": "{not-json"},
        {"code": "Z", "name": "z", "provider": "WorldBank", "relevance": -5.0,
         "popularity": 0},  # no raw_metadata key at all
    ]
    ranked = lk._rank_results(rows, "x")  # must not raise
    assert {r["code"] for r in ranked} == {"X", "Y", "Z"}


def test_non_worldbank_rows_ignore_archive_rule():
    """The penalty is WB-scoped; a FRED row with 'archive' in metadata is
    untouched by this block (its provider branch does not run)."""
    lk = _lookup()
    rows = [
        {"code": "SERIES1", "name": "some fred series", "provider": "FRED",
         "relevance": -5.0, "popularity": 0,
         "raw_metadata": json.dumps({"source": {"value": "Archive"}})},
    ]
    ranked = lk._rank_results(rows, "series")
    # No archive penalty applied for a non-WB provider.
    assert ranked[0]["code"] == "SERIES1"
