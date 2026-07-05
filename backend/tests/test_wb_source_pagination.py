"""WorldBank source endpoint must not silently truncate past page 10.

The old logic paged only when `1 < total_pages <= 10` and had NO else branch,
so any response with more than 10 pages returned page 1 as if it were the whole
dataset. It now pages the full result up to a cap and flags truncation when the
cap is hit (never a silent drop).
"""

from backend.providers.worldbank import WorldBankProvider


def test_more_than_ten_pages_fetches_all():
    # The exact regression: 25 pages must be fetched, not collapsed to page 1.
    last_page, truncated = WorldBankProvider._source_paging_plan(25)
    assert last_page == 25
    assert truncated is False


def test_ten_pages_still_full():
    assert WorldBankProvider._source_paging_plan(10) == (10, False)


def test_beyond_cap_is_flagged_not_silent():
    last_page, truncated = WorldBankProvider._source_paging_plan(60)
    assert last_page == WorldBankProvider._MAX_SOURCE_PAGES
    assert truncated is True


def test_single_page():
    assert WorldBankProvider._source_paging_plan(1) == (1, False)


def test_bad_input_safe():
    assert WorldBankProvider._source_paging_plan(0) == (1, False)
    assert WorldBankProvider._source_paging_plan("x") == (1, False)
