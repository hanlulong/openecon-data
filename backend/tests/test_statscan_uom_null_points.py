"""StatsCan unit label is a SERIES property, never a per-point property.

_normalize_units used to return the SOURCE-scale label ("millions") for a None
value while converted points got "billions"; both per-point loops
(_apply_uom_normalization and the fetch_series vector path) kept the LAST
point's label, so a trailing null relabelled a series that had already been
divided by 1000 — a silent 1000x unit error.
"""

import asyncio

import pytest

import backend.providers.statscan as statscan_mod
from backend.providers.statscan import StatsCanProvider

_NULL_SHAPES = [
    [100_000.0, 200_000.0, None],
    [100_000.0, None, None],
    [None, 100_000.0],
    [None, None],
]


def _vec(values, scalar_code=6):
    return [
        {"refPer": f"2024-0{i + 1}-01", "value": v, "frequencyCode": 6, "scalarFactorCode": scalar_code}
        for i, v in enumerate(values)
    ]


def _expected_label(p, scalar_code, indicator_name):
    # The label the same series gets when no point is null.
    _, label = p._normalize_units(1.0, scalar_code, to_unit="billions", indicator_name=indicator_name)
    return label


@pytest.mark.parametrize("scalar_code", [6, 9, 42])  # convert / same-unit / unknown code
@pytest.mark.parametrize("indicator_name", ["gross domestic product", "population estimate", None])
def test_normalize_units_none_value_reports_series_label(scalar_code, indicator_name):
    p = StatsCanProvider()
    value, none_label = p._normalize_units(
        None, scalar_code, to_unit="billions", indicator_name=indicator_name
    )
    assert value is None
    assert none_label == _expected_label(p, scalar_code, indicator_name)


def test_conversion_math_unchanged():
    p = StatsCanProvider()
    assert p._normalize_units(2_287_214.0, 6) == (pytest.approx(2287.214), "billions")
    assert p._normalize_units(5.0, 9, indicator_name="gdp") == (5.0, "billions")
    assert p._normalize_units(5.0, 42, indicator_name="gdp") == (5.0, "units")


@pytest.mark.parametrize("values", _NULL_SHAPES)
@pytest.mark.parametrize("scalar_code", [6, 9])
def test_apply_uom_normalization_label_independent_of_null_points(values, scalar_code):
    p = StatsCanProvider()
    points, unit = p._apply_uom_normalization(
        _vec(values, scalar_code), scalar_code, "gross domestic product", "Dollars"
    )
    assert unit == _expected_label(p, scalar_code, "gross domestic product")
    for raw, pt in zip(values, points):
        if raw is None:
            assert pt["value"] is None
        elif scalar_code == 6:
            assert unit == "billions"
            assert pt["value"] == pytest.approx(raw / 1000.0)
        else:
            assert pt["value"] == raw


@pytest.mark.parametrize("values", _NULL_SHAPES)
@pytest.mark.parametrize("scalar_code", [6, 9])
def test_fetch_series_vector_path_label_independent_of_null_points(values, scalar_code, monkeypatch):
    p = StatsCanProvider()
    vector_data = _vec(values, scalar_code)

    class _Resp:
        def json(self):
            return [{"status": "SUCCESS", "object": {"vectorDataPoint": vector_data}}]

    async def _vector_id(indicator, vector_id):
        return 12345

    async def _post(*a, **k):
        return _Resp()

    async def _uom(vector_id):
        return "Dollars"

    monkeypatch.setattr(p, "_vector_id", _vector_id)
    monkeypatch.setattr(p, "_post_with_retry", _post)
    monkeypatch.setattr(p, "_get_vector_uom_description", _uom)
    monkeypatch.setattr(statscan_mod, "get_statscan_http_client", lambda: object())

    result = asyncio.run(p.fetch_series({"indicator": "gross domestic product", "vectorId": 12345}))
    assert result.metadata.unit == _expected_label(p, scalar_code, "gross domestic product")
    for raw, pt in zip(values, result.data):
        if raw is None:
            assert pt.value is None
        elif scalar_code == 6:
            assert pt.value == pytest.approx(raw / 1000.0)
