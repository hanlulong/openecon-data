"""A "since YYYY" follow-up must clear a prior end_date (no inverted range).

"US GDP from 2000 to 2015" then "since 2020" previously kept end=2015 while
setting start=2020, producing an inverted/empty window. The time delta for
"since YYYY" now emits an explicit end date (today), which the merge applies
over the stale one.
"""

from backend.services.delta_extractor import DeltaExtractor
from backend.services.conversation_state_v2 import ConversationState, merge_state


def _extractor():
    ext = DeltaExtractor.__new__(DeltaExtractor)
    # Bypass the pure-time-change gate to exercise the "since" branch directly.
    ext._looks_like_pure_time_change_query = lambda q, s: True
    return ext


def test_since_year_emits_end_date():
    d = _extractor()._try_time_change("since 2020", ConversationState())
    assert d is not None
    assert d.changed_start_date == "2020-01-01"
    assert d.changed_end_date is not None
    assert d.changed_end_date >= "2026-01-01"


def test_since_year_merge_not_inverted():
    st = ConversationState()
    st.start_date = "2000-01-01"
    st.end_date = "2015-12-31"
    delta = _extractor()._try_time_change("since 2020", st)
    merged = merge_state(st, delta)
    assert merged.start_date == "2020-01-01"
    assert merged.start_date <= merged.end_date  # no inverted window


def test_last_n_years_still_has_end_date():
    d = _extractor()._try_time_change("last 10 years", ConversationState())
    assert d is not None and d.changed_end_date is not None
