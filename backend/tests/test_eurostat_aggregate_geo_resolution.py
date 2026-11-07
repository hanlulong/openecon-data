"""Fetch-time resolution of Eurostat aggregate geo codes against dataset codelists.

Eurostat publishes each aggregate (euro area, EU) under several composition
codes, and which ones a dataset carries drifts with membership: after
Bulgaria joined on 2026-01-01, une_rt_m stopped publishing EA20 (geo=EA20
returns an empty geo dimension) and carries only EA21, while teicp000 keeps
EA/EA20/EA21. The mechanism under test resolves the requested aggregate
against the DATASET'S OWN codelist; nothing here pins a code-to-code table.
"""
from __future__ import annotations

import pytest

from backend.providers import eurostat as eurostat_module
from backend.providers.eurostat import (
    EurostatProvider,
    eurostat_aggregate_geo_family,
    resolve_eurostat_aggregate_geo,
)
from backend.utils.retry import DataNotAvailableError


# ---------------------------------------------------------------- unit: resolver

@pytest.mark.parametrize("available,expected", [
    ({"EU27_2020", "EA21", "DE", "FR"}, "EA21"),
    ({"EA", "EA20", "EA21", "DE"}, "EA"),
    ({"EA20", "EU27_2020"}, "EA20"),
    ({"EA19", "EA20", "EA21"}, "EA21"),
    # A future composition needs no code change.
    ({"EA21", "EA22"}, "EA22"),
])
def test_logical_euro_area_resolves_to_what_the_dataset_publishes(available, expected):
    assert resolve_eurostat_aggregate_geo("EA", available) == expected


def test_persisted_fixed_composition_code_is_kept_when_published():
    assert resolve_eurostat_aggregate_geo("EA20", {"EA", "EA20", "EA21"}) == "EA20"


def test_persisted_fixed_composition_code_moves_to_published_code():
    assert resolve_eurostat_aggregate_geo("EA20", {"EU27_2020", "EA21"}) == "EA21"


@pytest.mark.parametrize("available,observed,expected", [
    ({"EU", "EU27_2020"}, (), "EU27_2020"),
    ({"EU", "EA21"}, (), "EU"),
    # Freshness (an observation at the latest period) outranks member count:
    # EU28 stopped in 2020, so a dataset still listing it must not win.
    ({"EU28", "EU27_2007"}, {"EU27_2007"}, "EU27_2007"),
    ({"EU28", "EU27_2007"}, (), "EU28"),
])
def test_eu_family_resolution_uses_codelist_and_freshness(available, observed, expected):
    assert resolve_eurostat_aggregate_geo("EU27_2020", available, observed) == expected


def test_resolution_never_crosses_aggregate_families():
    # A euro-area request must never be answered with an EU aggregate.
    assert resolve_eurostat_aggregate_geo("EA", {"EU27_2020", "DE", "FR"}) is None
    assert resolve_eurostat_aggregate_geo("EU27_2020", {"EA21", "DE"}) is None


@pytest.mark.parametrize("code", ["DE", "EE", "ES", "EL", "FR", "DE30", "FR_CAP", ""])
def test_non_aggregate_geos_are_untouched(code):
    assert eurostat_aggregate_geo_family(code) is None
    assert resolve_eurostat_aggregate_geo(code, {"EA21", "EU27_2020", "DE"}) is None


@pytest.mark.parametrize("code", ["EA", "EA19", "EA20", "EA21", "EA22", "EU", "EU15", "EU27_2020", "EU28", "EU_V"])
def test_aggregate_geo_category_is_recognized_structurally(code):
    assert EurostatProvider._is_aggregate_geo_category(code, "")


@pytest.mark.parametrize("code", ["DE", "EE", "ES", "EL", "BG"])
def test_member_states_are_not_aggregate_geo_categories(code):
    assert not EurostatProvider._is_aggregate_geo_category(code, "")


def test_observed_geo_codes_decodes_json_stat_positions():
    payload = _json_stat(["EU27_2020", "EA20", "EA21"], {"EU27_2020": 5.9, "EA21": 6.5})
    assert EurostatProvider._observed_geo_codes(payload) == {"EU27_2020", "EA21"}


# ------------------------------------------------------- integration: fetch path

def _json_stat(geos: list[str], values_by_geo: dict[str, float], periods=("2026-09", "2026-10")) -> dict:
    """Monthly JSON-stat cube [freq, unit, geo, time] with values per geo."""
    values = {}
    for g, geo in enumerate(geos):
        if geo in values_by_geo:
            for t in range(len(periods)):
                values[str(g * len(periods) + t)] = values_by_geo[geo]
    return {
        "id": ["freq", "unit", "geo", "time"],
        "size": [1, 1, len(geos), len(periods)],
        "dimension": {
            "freq": {"category": {"index": {"M": 0}, "label": {"M": "Monthly"}}},
            "unit": {"category": {"index": {"PC_ACT": 0}, "label": {"PC_ACT": "Percentage of population in the labour force"}}},
            "geo": {
                "category": {
                    "index": {geo: i for i, geo in enumerate(geos)},
                    "label": {geo: (f"Euro area - {geo}" if geo.startswith("EA") else geo) for geo in geos},
                }
            },
            "time": {"category": {"index": {p: i for i, p in enumerate(periods)}, "label": {p: p for p in periods}}},
        },
        "value": values,
        "label": "Unemployment by sex and age - monthly data",
        "updated": "2026-11-01T11:00:00+0100",
    }


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _CodelistClient:
    """Eurostat stand-in: a geo filter outside the codelist yields an empty geo dimension."""

    def __init__(self, published: dict[str, float]) -> None:
        self.published = published
        self.calls: list[dict[str, str]] = []

    async def get(self, _url: str, *, params: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append(dict(params))
        geo = params.get("geo")
        if geo is None:
            return _FakeResponse(_json_stat(list(self.published), self.published, periods=("2026-10",)))
        if geo in self.published:
            return _FakeResponse(_json_stat([geo], {geo: self.published[geo]}))
        return _FakeResponse(_json_stat([], {}))


def _provider(monkeypatch, published: dict[str, float]) -> tuple[EurostatProvider, _CodelistClient]:
    client = _CodelistClient(published)
    monkeypatch.setattr(eurostat_module, "get_http_client", lambda: client)
    return EurostatProvider(), client


@pytest.mark.asyncio
async def test_euro_area_request_resolves_to_dataset_published_code_with_disclosure(monkeypatch):
    provider, client = _provider(monkeypatch, {"EU27_2020": 5.9, "EA21": 6.5, "DE": 3.8})

    result = await provider.fetch_indicator("une_test_m", country="Euro area", start_year=2026)

    assert result.metadata.country == "EA21"
    assert [p.value for p in result.data] == [6.5, 6.5]
    notes = " ".join(result.metadata.notes or [])
    assert "EA21" in notes and "does not publish EA" in notes
    geos = [call.get("geo") for call in client.calls]
    assert geos == ["EA", None, "EA21"]
    probe = client.calls[1]
    assert probe["lastTimePeriod"] == "1" and "freq" not in probe and "sinceTimePeriod" not in probe


@pytest.mark.asyncio
async def test_cached_codelist_resolves_before_first_fetch(monkeypatch):
    provider, client = _provider(monkeypatch, {"EU27_2020": 5.9, "EA21": 6.5})
    await provider.fetch_indicator("une_test_m", country="Euro area", start_year=2026)
    client.calls.clear()

    result = await provider.fetch_indicator("une_test_m", country="Eurozone", start_year=2026)

    assert result.metadata.country == "EA21"
    assert [call.get("geo") for call in client.calls] == ["EA21"]
    assert any("EA21" in note for note in result.metadata.notes or [])


@pytest.mark.asyncio
async def test_persisted_ea20_state_resolves_against_ea21_only_dataset(monkeypatch):
    provider, client = _provider(monkeypatch, {"EA21": 6.5})

    result = await provider.fetch_indicator("une_test_m", country="EA20", start_year=2026)

    assert result.metadata.country == "EA21"
    assert any("does not publish EA20" in note for note in result.metadata.notes or [])
    assert [call.get("geo") for call in client.calls] == ["EA20", None, "EA21"]


@pytest.mark.asyncio
async def test_published_headline_code_costs_no_extra_request(monkeypatch):
    provider, client = _provider(monkeypatch, {"EA": 2.1, "EA20": 2.0, "EA21": 2.1})

    result = await provider.fetch_indicator("hicp_test_m", country="euro area", start_year=2026)

    assert result.metadata.country == "EA"
    assert not result.metadata.notes
    assert [call.get("geo") for call in client.calls] == ["EA"]


@pytest.mark.asyncio
async def test_euro_area_is_never_served_from_an_eu_only_dataset(monkeypatch):
    provider, client = _provider(monkeypatch, {"EU27_2020": 5.9, "DE": 3.8})

    with pytest.raises(DataNotAvailableError):
        await provider.fetch_indicator("une_test_m", country="Euro area", start_year=2026)
    assert "EU27_2020" not in [call.get("geo") for call in client.calls]


@pytest.mark.asyncio
async def test_individual_country_miss_does_not_trigger_aggregate_resolution(monkeypatch):
    provider, client = _provider(monkeypatch, {"EA21": 6.5, "FR": 7.6})

    with pytest.raises(DataNotAvailableError):
        await provider.fetch_indicator("une_test_m", country="Germany", start_year=2026)
    # Every geo-filtered call stays on DE: no aggregate substitution for a country.
    assert {call.get("geo") for call in client.calls if "geo" in call} == {"DE"}


@pytest.mark.parametrize("code", ["EA", "EA20", "EA21", "EU27_2020", "DE"])
def test_eurostat_coverage_predicate_accepts_resolved_aggregate_codes(code):
    # A served EA21 flows back through conversation state; the coverage
    # predicate must keep routing that follow-up to Eurostat.
    from backend.services.provider_fallback import provider_covers_country_list

    assert provider_covers_country_list("Eurostat", [code])


class _MislabeledFreqClient(_CodelistClient):
    """Monthly dataset whose code suffix makes the provider infer freq=A."""

    async def get(self, _url: str, *, params: dict[str, str], timeout: float) -> _FakeResponse:
        if params.get("freq") == "A":
            self.calls.append(dict(params))
            return _FakeResponse(_json_stat([], {}))
        return await super().get(_url, params=params, timeout=timeout)


@pytest.mark.asyncio
async def test_cached_codelist_with_published_code_is_not_reprobed_on_other_empties(monkeypatch):
    client = _MislabeledFreqClient({"EA": 2.1, "EA21": 2.1, "EU27_2020": 2.4})
    monkeypatch.setattr(eurostat_module, "get_http_client", lambda: client)
    provider = EurostatProvider()

    first = await provider.fetch_indicator("hicp_test_manr", country="Euro area", start_year=2026)
    assert first.metadata.country == "EA" and not first.metadata.notes
    client.calls.clear()

    second = await provider.fetch_indicator("hicp_test_manr", country="Euro area", start_year=2026)
    assert second.metadata.country == "EA"
    # Inferred-freq empty -> native-frequency retry only; no second codelist probe.
    assert [("geo" in call, call.get("freq")) for call in client.calls] == [(True, "A"), (True, None)]
