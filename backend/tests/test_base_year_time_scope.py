"""A constant-price base year must not read as an explicit time filter.

"US GDP in 2015 dollars" denominates values in 2015 dollars; it is NOT a
request to restrict the window to 2015. The text heuristic previously saw the
bare year, marked the window user-set, and suppressed the default-window strip
+ Eurostat sparse retry, producing a false "no data". Base years are now
stripped before the year tests; genuine year filters/ranges still count.
"""

import pytest

from backend.services.data_fetcher import _query_has_explicit_time_scope as scope


@pytest.mark.parametrize(
    "query",
    [
        "US GDP in 2015 dollars",
        "US real GDP in 2017 US dollars",
        "GDP at 2010 prices",
        "GDP in constant 2015 dollars",
        "chained 2012 dollars GDP",
    ],
)
def test_base_year_is_not_a_time_scope(query):
    assert scope(query) is False


@pytest.mark.parametrize(
    "query",
    [
        "US GDP in 2015",                # real single-year filter
        "US GDP from 2010 to 2020",      # real range
        "US GDP since 2019",
        "GDP in constant 2015 dollars from 2010 to 2020",  # base year + real range
        "latest US GDP",
    ],
)
def test_real_time_scope_still_detected(query):
    assert scope(query) is True
