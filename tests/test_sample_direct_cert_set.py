from __future__ import annotations

from scripts.validation.sample_direct_cert_set import provider_oversample_target


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
