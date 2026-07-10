"""FIX 1: BIS unit/dataType/priceType must follow the SELECTED series' own
decoded dimensions, not an ``indicator_code`` ladder that can diverge from the
sub-series ``_select_best_series`` actually returned.

The wrong-data bug: WS_TC's unit was hard-set to "percent of GDP" by an
``if indicator_code == "WS_TC"`` ladder regardless of which sub-series was
served. When the preferred "% of GDP" sub-series is missing, selection falls
through to e.g. a domestic-currency series, yet the ladder still stamped
"percent of GDP" — a unit that contradicts the served numbers.
"""
from __future__ import annotations

from backend.providers.bis import BISProvider


def _derive(dims, indicator_code, indicator_name="Total credit"):
    provider = BISProvider()
    return provider._descriptive_metadata_from_dims(  # pylint: disable=protected-access
        selected_dims=dims,
        indicator_code=indicator_code,
        indicator_name=indicator_name,
    )


def test_unit_follows_served_dim_over_contradicting_ladder():
    # WS_TC ladder says "percent of GDP"; the SELECTED series is domestic
    # currency -> the served dim must win.
    dims = {
        "FREQ": {"id": "Q", "name": "Quarterly"},
        "UNIT_TYPE": {"id": "798", "name": "Domestic currency"},
    }
    unit, _data_type, _price_type = _derive(dims, "WS_TC")
    assert unit == "Domestic currency"


def test_unit_matches_ladder_when_served_dim_agrees():
    dims = {"UNIT_TYPE": {"id": "770", "name": "% of GDP"}}
    unit, _data_type, _price_type = _derive(dims, "WS_TC")
    assert unit == "% of GDP"


def test_unit_from_codelist_when_served_name_absent():
    # Value carries only the SDMX code, no name -> label via the codelist table.
    dims = {"UNIT_TYPE": {"id": "770", "name": ""}}
    unit, _data_type, _price_type = _derive(dims, "WS_TC")
    assert unit == "% of GDP"


def test_present_but_unlabelable_unit_does_not_assert_ladder():
    # A unit dimension IS present but neither name nor a known code -> stay empty
    # rather than assert a ladder unit that could contradict the served series.
    dims = {"UNIT_TYPE": {"id": "999", "name": ""}}
    unit, _data_type, _price_type = _derive(dims, "WS_TC")
    assert unit == ""


def test_no_unit_dimension_falls_back_to_ladder():
    # Series carries no unit dimension at all -> the indicator ladder is safe.
    dims = {
        "FREQ": {"id": "Q", "name": "Quarterly"},
        "TC_BORROWERS": {"id": "P", "name": "Private non-financial sector"},
    }
    unit, data_type, _price_type = _derive(dims, "WS_TC")
    assert unit == "percent of GDP"
    assert data_type == "Level"


def test_credit_gap_without_unit_dim_keeps_ladder_unit_and_type():
    # Regression guard for the existing WS_CREDIT_GAP expectation.
    dims = {"CG_DTYPE": {"id": "C", "name": "Credit-to-GDP gaps (actual-trend)"}}
    unit, data_type, price_type = _derive(dims, "WS_CREDIT_GAP", "Credit-to-GDP gaps")
    assert unit == "percentage points"
    assert data_type == "Gap"
    assert price_type is None


def test_price_type_follows_valuation_dim():
    real_dims = {
        "UNIT_MEASURE": {"id": "628", "name": "Index"},
        "PP_VALUATION": {"id": "R", "name": "Real"},
    }
    unit, data_type, price_type = _derive(real_dims, "WS_SPP", "Residential property prices")
    assert unit == "Index"
    assert data_type == "Index"
    assert price_type == "Real (inflation-adjusted)"

    nominal_dims = {
        "UNIT_MEASURE": {"id": "628", "name": "Index"},
        "PP_VALUATION": {"id": "N", "name": "Nominal"},
    }
    _unit, _data_type, price_type = _derive(nominal_dims, "WS_SPP", "Residential property prices")
    assert price_type == "Nominal (current prices)"


def test_empty_dims_are_pure_ladder():
    unit, data_type, price_type = _derive({}, "WS_CBPOL", "Policy rate")
    assert unit == "percent"
    assert data_type == "Rate"
    assert price_type is None


def test_missing_dims_do_not_crash():
    # Robustness: None-ish / partial dims must not raise.
    assert _derive({}, "WS_UNKNOWN", "") == ("", None, None)
