from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.providers import eurostat as eurostat_module
from backend.providers.eurostat import EurostatProvider
from backend.utils.retry import DataNotAvailableError


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeEurostatClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def get(self, _url: str, *, params: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append(dict(params))
        if params.get("geo") == "DE":
            return _FakeResponse(_json_stat_payload(geo_labels={}, values={}))
        if "geo" not in params:
            return _FakeResponse(
                _json_stat_payload(
                    geo_labels={"EU_V": "European Union (aggregate changing according to the context)"},
                    values={"0": 100},
                )
            )
        raise AssertionError(f"unexpected Eurostat params: {params}")


def _json_stat_payload(*, geo_labels: dict[str, str], values: dict[str, float]) -> dict:
    geo_index = {code: index for index, code in enumerate(geo_labels)}
    return {
        "id": ["freq", "unit", "geo", "time"],
        "size": [1, 1, len(geo_labels), 1],
        "dimension": {
            "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
            "unit": {"category": {"index": {"PC": 0}, "label": {"PC": "Percentage"}}},
            "geo": {"category": {"index": geo_index, "label": geo_labels}},
            "time": {"category": {"index": {"1999": 0}, "label": {"1999": "1999"}}},
        },
        "value": values,
        "label": "Relative prevalence rate of work-related health problems",
        "updated": "2026-05-15T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_eurostat_country_miss_is_supportability_blocked_when_only_aggregate_geo_exists(monkeypatch):
    client = _FakeEurostatClient()
    monkeypatch.setattr(eurostat_module, "get_http_client", lambda: client)

    provider = EurostatProvider()
    default_start = str(datetime.now(timezone.utc).year - 5)

    with pytest.raises(DataNotAvailableError) as exc_info:
        await provider.fetch_indicator("HSW_HP_SVCLN", country="Germany")

    message = str(exc_info.value)
    assert "reason=eurostat_requested_geo_unavailable" in message
    assert "dataset=hsw_hp_svcln" in message
    assert "country=DE" in message
    assert "available_geo=EU_V" in message
    assert client.calls == [
        {"freq": "A", "geo": "DE", "sinceTimePeriod": default_start},
        {"freq": "A", "geo": "DE", "lastTimePeriod": "1"},
        {"freq": "A", "lastTimePeriod": "1"},
    ]


@pytest.mark.asyncio
async def test_eurostat_all_available_still_returns_aggregate_without_country_substitution(monkeypatch):
    client = _FakeEurostatClient()
    monkeypatch.setattr(eurostat_module, "get_http_client", lambda: client)

    provider = EurostatProvider()
    default_start = str(datetime.now(timezone.utc).year - 5)
    result = await provider.fetch_indicator("HSW_HP_SVCLN", country="__ALL__")

    assert result.metadata.country == "ALL_AVAILABLE"
    assert result.metadata.seriesId == "hsw_hp_svcln"
    assert len(result.data) == 1
    assert result.data[0].date == "1999-01-01"
    assert result.data[0].value == 100
    assert client.calls == [{"freq": "A", "sinceTimePeriod": default_start}]
