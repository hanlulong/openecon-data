"""IMF series report their real unit, not an empty string.

The IMF provider previously hardcoded unit="percent"-or-EMPTY per code, leaving
every non-percent series (NGDPD, BCA, ...) with no unit. The fix prefers the
authoritative unit from the IMF catalog metadata (which the DataMapper
publishes: "Billions of U.S. dollars", "Percent of GDP", ...). This guards the
catalog data that fix depends on.
"""
from __future__ import annotations

import pytest

from backend.services.indicator_database import get_indicator_lookup


@pytest.mark.parametrize(
    "code,expected_unit",
    [
        ("NGDPD", "Billions of U.S. dollars"),
        ("BCA", "Billions of U.S. dollars"),
        ("GGXWDG_NGDP", "Percent of GDP"),
        ("PCPIPCH", "Annual percent change"),
        ("LUR", "Percent"),
    ],
)
def test_imf_catalog_carries_real_units(code, expected_unit):
    meta = get_indicator_lookup().get("IMF", code)
    assert meta is not None, f"IMF catalog missing {code}"
    assert str(meta.get("unit") or "").strip() == expected_unit


def test_imf_unit_lookup_is_non_empty_for_level_series():
    # The whole point of the fix: level series (not in the percent heuristic)
    # must now get a real, non-empty unit from the catalog.
    for code in ("NGDPD", "BCA", "NGDPDPC", "PPPGDP"):
        meta = get_indicator_lookup().get("IMF", code)
        if meta is None:
            continue
        assert str(meta.get("unit") or "").strip(), f"{code} has empty catalog unit"
