from __future__ import annotations

from scripts.validation.build_strata_table import (
    AMBIGUITY_ALLOCATION,
    RISK_WEIGHTED_MULTIROUND_ALLOCATION,
    direct_provider_allocation,
    scale_named_allocation,
)


def test_direct_provider_allocation_respects_floor_and_total() -> None:
    rows = [("FRED", 100), ("IMF", 60), ("WorldBank", 40)]
    allocation = direct_provider_allocation(rows, total=180, floor=30)

    assert sum(allocation.values()) == 180
    assert allocation["FRED"] >= 30
    assert allocation["IMF"] >= 30
    assert allocation["WorldBank"] >= 30
    assert allocation["FRED"] > allocation["WorldBank"]


def test_direct_provider_allocation_caps_small_provider_capacity() -> None:
    rows = [("FRED", 1000), ("BIS", 61), ("ExchangeRate", 49)]
    allocation = direct_provider_allocation(rows, total=500, floor=300)

    assert sum(allocation.values()) == 500
    assert allocation["BIS"] <= 61
    assert allocation["ExchangeRate"] <= 49


def test_scale_named_allocation_preserves_keys_and_total() -> None:
    allocation = scale_named_allocation(
        {"provider_switch_chain": 600, "transform_switch_chain": 500, "fx_pair_chain": 200},
        total=7000,
    )

    assert set(allocation) == {"provider_switch_chain", "transform_switch_chain", "fx_pair_chain"}
    assert sum(allocation.values()) == 7000
    assert allocation["provider_switch_chain"] > allocation["fx_pair_chain"]


def test_default_family_weight_maps_scale_to_expected_totals() -> None:
    multiround = scale_named_allocation(RISK_WEIGHTED_MULTIROUND_ALLOCATION, total=7000)
    ambiguity = scale_named_allocation(AMBIGUITY_ALLOCATION, total=5000)

    assert sum(multiround.values()) == 7000
    assert sum(ambiguity.values()) == 5000
    assert "provider_switch_chain" in multiround
    assert "fx_pair_chain" in multiround
    assert "transform_ambiguity" in ambiguity
    assert "dominant_interpretation_cases" in ambiguity
