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


# --- Round 4: shared normalization across ALL WDS paths ----------------------

def test_apply_uom_normalization_currency_level_converts_to_billions():
    from backend.providers.statscan import StatsCanProvider

    p = StatsCanProvider()
    vector_data = [
        {"refPer": "2024-01-01", "value": 2_501_000.0},  # millions
        {"refPer": "2024-04-01", "value": 2_512_000.0},
    ]
    # scalar code 6 = millions in StatsCan's scalar factor table
    points, unit = p._apply_uom_normalization(
        vector_data, 6, "gross domestic product", "Dollars"
    )
    assert unit and "billion" in unit.lower()
    assert points[0]["value"] == pytest.approx(2501.0)


def test_apply_uom_normalization_percent_untouched():
    from backend.providers.statscan import StatsCanProvider

    p = StatsCanProvider()
    vector_data = [{"refPer": "2024-01-01", "value": 180.5}]
    points, unit = p._apply_uom_normalization(
        vector_data, 0, "debt as percent of gdp", "Percent"
    )
    assert unit is None  # caller keeps its own labeling
    assert points[0]["value"] == 180.5


def test_apply_uom_normalization_unreadable_uom_untouched():
    from backend.providers.statscan import StatsCanProvider

    p = StatsCanProvider()
    vector_data = [{"refPer": "2024-01-01", "value": 42.0}]
    points, unit = p._apply_uom_normalization(vector_data, 6, "gdp", None)
    assert unit is None
    assert points[0]["value"] == 42.0


def test_all_wds_paths_use_shared_normalization():
    import inspect
    from backend.providers.statscan import StatsCanProvider

    for fn in (
        StatsCanProvider.fetch_categorical_data,
        StatsCanProvider.fetch_from_product_with_discovery,
        StatsCanProvider.fetch_with_dimensions,
        StatsCanProvider.fetch_multi_province_data,
    ):
        assert "_apply_uom_normalization" in inspect.getsource(fn), fn.__name__
