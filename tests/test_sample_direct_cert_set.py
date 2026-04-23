from __future__ import annotations

from scripts.validation.sample_direct_cert_set import (
    _select_quality_screened_records,
    provider_oversample_target,
)


def test_provider_oversample_target_caps_large_provider_batches() -> None:
    target = provider_oversample_target(
        provider_population=138_774,
        provider_sample_count=6_607,
        oversample_factor=8,
        oversample_cap=20_000,
        oversample_buffer=500,
    )

    assert target == 20_000


def test_provider_oversample_target_respects_population_and_buffer() -> None:
    target = provider_oversample_target(
        provider_population=1_000,
        provider_sample_count=150,
        oversample_factor=8,
        oversample_cap=20_000,
        oversample_buffer=500,
    )

    assert target == 1_000


def test_select_quality_screened_records_prefers_supported_imf_candidate_over_h5_trade_family() -> None:
    unsupported = {
        "id": "imf-h5",
        "provider_stratum": "IMF",
        "query": "Germany Merchandise Trade Value of Exports. Chapter 60- Knitted goods from IMF",
        "origin": {
            "name": "Merchandise Trade, Value of Exports. Chapter 60- Knitted goods, Euros",
            "source_indicator_code": "TXG_H5_60_EUR",
        },
        "provenance": {
            "query_quality_risk": "high",
            "query_quality_reasons": ["imf_low_viability_family"],
        },
    }
    acceptable = {
        "id": "imf-aggregate",
        "provider_stratum": "IMF",
        "query": "Germany exports of goods from IMF",
        "origin": {
            "name": "Exports of goods",
            "source_indicator_code": "XG_FOB_USD",
        },
        "provenance": {
            "query_quality_risk": "low",
            "query_quality_reasons": [],
        },
    }

    selected = _select_quality_screened_records([unsupported, acceptable], 1)

    assert [row["id"] for row in selected] == ["imf-aggregate"]
