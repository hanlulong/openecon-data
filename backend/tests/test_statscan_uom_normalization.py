"""StatsCan billions-normalization must be gated on the cube's UOM, not the name.

The old rule normalized any series whose title contained "GDP"/"DEBT", which
corrupted "government debt as % of GDP": 180.5(%) / 1e9 = 1.8e-7 labeled
"billions". Normalization now fires only for a dollar LEVEL unit of measure.
"""

import pytest

from backend.providers.statscan import StatsCanProvider


@pytest.mark.parametrize(
    "uom",
    [
        "Dollars",
        "Current dollars",
        "Thousands of dollars",
        "Chained (2017) dollars",
        "Canadian dollars",
        "2012 constant dollars",
        "United States dollars",
    ],
)
def test_dollar_levels_normalize(uom):
    assert StatsCanProvider._uom_is_currency_level(uom) is True


@pytest.mark.parametrize(
    "uom",
    [
        "Percent",
        "Percentage of GDP",
        "Index",
        "2002=100",          # CPI-style index
        "Dollars, 1972=100", # dollar-denominated index, not a level
        "Number",
        "Dollars per litre", # a rate, not a level
        "Dollars per hour",
        "Per dollar of output",
        "",
        None,
    ],
)
def test_non_levels_do_not_normalize(uom):
    assert StatsCanProvider._uom_is_currency_level(uom) is False
