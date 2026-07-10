"""FIX 4: FRED country must come from a positive SUBJECT-geography signal in the
provider-native title, never from a mere mention and never from a blanket "US"
default.

FRED hosts both U.S. domestic series and international/global series and exposes
no normalized country field. The only signals available at the call site are the
series title/id. We assign a country only when the title names one as the
series' subject geography — a leading "<Country> ..." token or the FRED
"... for <Country>" convention. A bilateral counterparty mention
("Imports from China") must NOT flip the geography; absent a positive signal the
country is left UNSET (None) rather than defaulting to "US".
"""
from __future__ import annotations

import pytest

from backend.providers.fred import _infer_country_from_fred_info


def _infer(title):
    return _infer_country_from_fred_info({"title": title})


# Bilateral counterparty mentions must not become the geography.
@pytest.mark.parametrize("title", [
    "U.S. Imports of Goods from China",
    "U.S. Imports from China",
    "U.S. Exports of Goods to Germany",
    "Imports of Goods and Services from Mexico",  # no subject geography at all
])
def test_counterparty_mention_is_not_the_geography(title):
    assert _infer(title) != "China"
    assert _infer(title) != "Germany"
    assert _infer(title) != "Mexico"


# Leading "U.S." is the reporting geography even with a foreign counterparty.
@pytest.mark.parametrize("title", [
    "U.S. Imports of Goods from China",
    "U.S. National Income",
])
def test_leading_us_is_us(title):
    assert _infer(title) == "US"


# "for <Country>" (optionally "for the <Country>") is the subject geography.
@pytest.mark.parametrize("title,country", [
    ("Real Gross Domestic Product for Japan", "Japan"),
    ("Gross Domestic Product for Canada", "Canada"),
    ("Consumer Price Index for Germany", "Germany"),
    ("Federal Debt: Total Public Debt for the United States of America", "US"),
    ("Gross Domestic Product for the United States", "US"),
])
def test_for_country_is_subject_geography(title, country):
    assert _infer(title) == country


# Global / aggregate series must not be pinned to a country (and not to "US").
@pytest.mark.parametrize("title", [
    "Global price of Brent Crude",
    "Global price of WTI Crude Oil",
    "World Uncertainty Index",
])
def test_global_series_are_unset(title):
    assert _infer(title) is None


# No positive signal -> UNSET, never a blanket "US" default.
@pytest.mark.parametrize("title", [
    "Real Gross Domestic Product",
    "Unemployment Rate",
    "Federal Funds Effective Rate",
    "",
])
def test_no_signal_is_unset(title):
    assert _infer(title) is None


def test_return_type_is_optional_str():
    # Downstream (Metadata.country) is Optional[str]; None must be a valid result.
    assert _infer("Unemployment Rate") is None
    assert isinstance(_infer("Real GDP for Japan"), str)
