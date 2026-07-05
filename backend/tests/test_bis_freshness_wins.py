"""BIS must return the freshest candidate series, not the first with data.

When multiple REF_AREA candidates exist for a query (e.g. a euro member's
national code and the Euro-area XM aggregate), the old code returned the first
one with any data — so a discontinued national series (a euro member's policy
rate frozen at 1998-12) shadowed the live ECB/Euro-area series. Selection is now
freshness-based (most-recent last observation), provider/indicator-agnostic.
"""

from backend.providers.bis import _bis_last_obs_sort_key


class _Meta:
    def __init__(self, end_date):
        self.endDate = end_date


class _Series:
    def __init__(self, end_date):
        self.metadata = _Meta(end_date)


def _freshest(*end_dates):
    cands = [_Series(d) for d in end_dates]
    return max(cands, key=_bis_last_obs_sort_key).metadata.endDate


def test_live_series_beats_discontinued():
    # The Germany-vs-XM case: 1998-12 (stale) must lose to a current date.
    assert _freshest("1998-12", "2026-06") == "2026-06"


def test_mixed_granularity_years_and_months():
    assert _freshest("2025", "2026-01", "1998-12") == "2026-01"


def test_day_precision():
    assert _freshest("2026-06-01", "2026-06-15") == "2026-06-15"


def test_single_candidate_returned():
    assert _freshest("2020-01") == "2020-01"


def test_absent_date_sorts_oldest():
    assert _bis_last_obs_sort_key(_Series(None)) == (0, 0, 0)
    # A real date beats an absent one.
    assert _freshest(None, "2000-01") == "2000-01"
