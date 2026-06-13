"""Provider values must pass through exactly as published.

Regression for the removed value-sniffing percent normalizer: any percent/
rate series whose values all sat below |1.5| was silently multiplied by 100
(T10Y2Y 0.39 -> 39.0, and would have hit near-zero policy rates and FX
rates). Provider unit metadata is authoritative; value distribution is not.
"""
from __future__ import annotations

import pytest


def test_value_sniffing_normalizer_is_gone():
    import backend.providers._sdmx as sdmx

    assert not hasattr(sdmx, "normalize_percentage_values"), (
        "the value-distribution percent rescaler must not come back; "
        "convert units from provider unit METADATA at the provider if a "
        "genuinely decimal-published series is ever found"
    )
    for provider_mod in ("fred", "imf", "eurostat"):
        mod = __import__(f"backend.providers.{provider_mod}", fromlist=["*"])
        for cls_name in dir(mod):
            cls = getattr(mod, cls_name)
            if isinstance(cls, type):
                assert not hasattr(cls, "_normalize_percentage_values"), (
                    f"{provider_mod}.{cls_name} still has the rescaler hook"
                )


@pytest.mark.asyncio
async def test_fred_small_percent_series_passes_through_unscaled(monkeypatch):
    """A spread-like series (all |values| < 1.5, unit Percent) must keep its
    published scale end-to-end through FREDProvider.fetch_series."""
    from backend.providers.fred import FREDProvider

    provider = FREDProvider("test-key")

    async def fake_resolve(indicator, series_id):
        return "T10Y2Y", None

    series_info = {
        "id": "T10Y2Y",
        "title": "10-Year Treasury Constant Maturity Minus 2-Year Treasury Constant Maturity",
        "units": "Percent",
        "frequency": "Daily",
        "observation_start": "1976-06-01",
        "observation_end": "2026-06-12",
        "last_updated": "2026-06-12",
        "seasonal_adjustment": "Not Seasonally Adjusted",
        "notes": "",
    }
    observations = {
        "observations": [
            {"date": "2026-06-10", "value": "0.42"},
            {"date": "2026-06-11", "value": "0.40"},
            {"date": "2026-06-12", "value": "0.39"},
        ]
    }

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    async def fake_get_with_retry(client, url, *args, **kwargs):
        if "series/observations" in url:
            return FakeResponse(observations)
        return FakeResponse({"seriess": [series_info]})

    monkeypatch.setattr(provider, "_resolve_series_id_async", fake_resolve)
    monkeypatch.setattr(provider, "_get_with_retry", fake_get_with_retry)

    result = await provider.fetch_series({"seriesId": "T10Y2Y"})
    values = [point.value for point in result.data]
    assert values == [0.42, 0.40, 0.39], (
        f"published scale must be preserved exactly, got {values}"
    )
