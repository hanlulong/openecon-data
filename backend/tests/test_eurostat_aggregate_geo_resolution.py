"""Fetch-time resolution of Eurostat aggregate geo codes against dataset codelists.

Eurostat publishes each aggregate (euro area, EU) under several composition
codes, and which ones a dataset carries drifts with membership: after
Bulgaria joined on 2026-01-01, une_rt_m stopped publishing EA20 (geo=EA20
returns an empty geo dimension) and carries only EA21, while teicp000 keeps
EA/EA20/EA21. The mechanism under test resolves the requested aggregate
against the DATASET'S OWN codelist; nothing here pins a code-to-code table.
The logical euro-area token (bare "EA") resolves to the newest FIXED
composition the dataset publishes: the changing-composition series steps at
every accession (live namq_10_gdp: EA 2025-Q1 = the EA20 level, 2026-Q1 = the
EA21 level), so growth computed on it is wrong for the accession year.
"""
from __future__ import annotations

import pytest

from backend.providers import eurostat as eurostat_module
from backend.providers.eurostat import EurostatProvider, resolve_eurostat_aggregate_geo
from backend.utils.geographies import aggregate_geo_family
from backend.utils.retry import DataNotAvailableError, ProviderCoolingDown


# ---------------------------------------------------------------- unit: resolver

@pytest.mark.parametrize("available,expected", [
    ({"EU27_2020", "EA21", "DE", "FR"}, "EA21"),
    # The changing-composition code is published too, but the newest fixed
    # composition is one consistent country set.
    ({"EA", "EA20", "EA21", "DE"}, "EA21"),
    ({"EA", "EA19", "EA20"}, "EA20"),
    ({"EA20", "EU27_2020"}, "EA20"),
    ({"EA19", "EA20", "EA21"}, "EA21"),
    # Only the changing composition published: serve it.
    ({"EA", "EU27_2020", "DE"}, "EA"),
    # A future composition needs no code change.
    ({"EA21", "EA22"}, "EA22"),
])
def test_logical_euro_area_resolves_to_what_the_dataset_publishes(available, expected):
    assert resolve_eurostat_aggregate_geo("EA", available) == expected


def test_logical_token_prefers_a_composition_observed_at_the_latest_period():
    assert resolve_eurostat_aggregate_geo("EA", {"EA20", "EA21"}, {"EA20"}) == "EA20"
    assert resolve_eurostat_aggregate_geo("EA", {"EA20", "EA21"}, {"EA20", "EA21"}) == "EA21"


@pytest.mark.parametrize("requested", ["EA19", "EA20"])
def test_explicit_fixed_composition_is_honoured_when_published(requested):
    assert resolve_eurostat_aggregate_geo(requested, {"EA", "EA19", "EA20", "EA21"}) == requested


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
    assert aggregate_geo_family(code) is None
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
    assert "euro area served as EA21" in notes and "Euro area - EA21" in notes
    # The logical token reads the codelist first, then fetches the resolved code.
    geos = [call.get("geo") for call in client.calls]
    assert geos == [None, "EA21"]
    probe = client.calls[0]
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
async def test_logical_euro_area_prefers_newest_fixed_composition_over_changing_one(monkeypatch):
    provider, client = _provider(monkeypatch, {"EA": 2.1, "EA20": 2.0, "EA21": 2.2})

    result = await provider.fetch_indicator("hicp_test_m", country="euro area", start_year=2026)

    assert result.metadata.country == "EA21"
    assert [p.value for p in result.data] == [2.2, 2.2]
    assert [call.get("geo") for call in client.calls] == [None, "EA21"]
    client.calls.clear()
    # Cached codelist: the next euro-area request costs no probe.
    again = await provider.fetch_indicator("hicp_test_m", country="Eurozone", start_year=2026)
    assert again.metadata.country == "EA21"
    assert [call.get("geo") for call in client.calls] == ["EA21"]


@pytest.mark.asyncio
async def test_published_specific_composition_costs_no_extra_request(monkeypatch):
    provider, client = _provider(monkeypatch, {"EA": 2.1, "EA20": 2.0, "EA21": 2.2})

    result = await provider.fetch_indicator("hicp_test_m", country="EA20", start_year=2026)

    assert result.metadata.country == "EA20"
    assert not result.metadata.notes
    assert [call.get("geo") for call in client.calls] == ["EA20"]


@pytest.mark.asyncio
async def test_changing_composition_only_dataset_is_served_with_disclosure(monkeypatch):
    provider, client = _provider(monkeypatch, {"EA": 2.1, "DE": 2.4})

    result = await provider.fetch_indicator("hicp_test_m", country="Euro area", start_year=2026)

    assert result.metadata.country == "EA"
    notes = " ".join(result.metadata.notes or [])
    assert "changing-composition" in notes and "member countries change" in notes
    assert [call.get("geo") for call in client.calls] == [None, "EA"]


@pytest.mark.asyncio
@pytest.mark.parametrize("country", ["Euro area", "EA20"])
async def test_codelist_probe_cooldown_is_not_swallowed(monkeypatch, country):
    # Only the no-geo codelist probe hits the cooldown. Swallowing it would end
    # as "No data found" (a plain DataNotAvailableError) instead of surfacing
    # the cooldown that stops the caller's fan-out.
    provider, client = _provider(monkeypatch, {"EA21": 6.5})

    async def get_with_retry(_client, url, *, params, raise_on_status, timeout):
        if "geo" not in params:
            raise ProviderCoolingDown("Eurostat cooling down")
        return await client.get(url, params=params, timeout=timeout)

    provider._get_with_retry = get_with_retry
    with pytest.raises(ProviderCoolingDown):
        await provider.fetch_indicator("une_test_m", country=country, start_year=2026)


@pytest.mark.asyncio
async def test_failed_codelist_probe_falls_back_to_the_family_code_and_is_not_repeated(monkeypatch):
    # A probe the dataset refuses (e.g. too large even for lastTimePeriod=1)
    # leaves the logical token as the first attempt; the empty-result path must
    # not fire the same failing probe a second time in the same fetch.
    import httpx

    provider, client = _provider(monkeypatch, {"EA": 2.1, "EA21": 2.2})
    probes = []

    async def get_with_retry(_client, url, *, params, raise_on_status, timeout):
        if "geo" not in params:
            probes.append(dict(params))
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "413", request=request, response=httpx.Response(413, request=request)
            )
        return await client.get(url, params=params, timeout=timeout)

    provider._get_with_retry = get_with_retry
    result = await provider.fetch_indicator("hicp_test_m", country="Euro area", start_year=2026)

    assert result.metadata.country == "EA"
    assert len(probes) == 1


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
    assert first.metadata.country == "EA21"
    client.calls.clear()

    second = await provider.fetch_indicator("hicp_test_manr", country="Euro area", start_year=2026)
    assert second.metadata.country == "EA21"
    # Inferred-freq empty -> native-frequency retry only; no second codelist probe.
    assert [("geo" in call, call.get("freq")) for call in client.calls] == [(True, "A"), (True, None)]
