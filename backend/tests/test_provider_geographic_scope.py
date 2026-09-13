"""Provider fallbacks must preserve geographic identity through HTTP dispatch."""

from unittest.mock import AsyncMock

import pytest

from backend.providers.eurostat import EurostatProvider
from backend.providers.worldbank import WorldBankProvider
from backend.routing.country_resolver import CountryResolver


UNRESOLVED_EUROPE = (
    "Europe", "European", "European countries", "European_countries",
    "European economies", "countries in Europe",
)


@pytest.fixture
def worldbank():
    provider = WorldBankProvider.__new__(WorldBankProvider)
    provider.base_url = "https://api.worldbank.org/v2"
    provider.metadata_search = None
    return provider


@pytest.fixture
def eurostat():
    return EurostatProvider.__new__(EurostatProvider)


@pytest.mark.parametrize("country", UNRESOLVED_EUROPE)
def test_worldbank_does_not_resolve_continent_as_union_or_selected_members(worldbank, country):
    assert worldbank._map_regional_term(country) is None
    assert worldbank._expand_country_group(country) is None
    assert worldbank._country_code(country) == country.upper()


@pytest.mark.parametrize("country", UNRESOLVED_EUROPE)
def test_eurostat_does_not_resolve_continent_as_union(eurostat, country):
    assert eurostat._country_code(country) == country.upper()


@pytest.mark.parametrize("alias", ["EU", "European Union", "European_Union", " European   Union "])
def test_worldbank_explicit_union_aggregate_uses_official_identity(worldbank, alias):
    assert worldbank._map_regional_term(alias) == "EUU"
    assert worldbank._country_code(alias) == "EUU"


@pytest.mark.parametrize("native", ["EUU", "ECS", "EMU", "WLD", "SAS", "SSF"])
def test_worldbank_preserves_native_aggregate_identifiers(worldbank, native):
    assert worldbank._country_code(native) == native
    assert worldbank._expand_country_group(native) is None


@pytest.mark.parametrize("country,expected", [
    ("Germany", "DE"), ("DEU", "DE"), ("South Africa", "ZA"),
    ("South Korea", "KR"), ("United States", "US"), ("UK", "GB"),
])
def test_worldbank_preserves_named_and_coded_countries(worldbank, country, expected):
    assert worldbank._map_regional_term(country) is None
    assert worldbank._expand_country_group(country) is None
    assert worldbank._country_code(country) == expected


@pytest.mark.parametrize("phrase,expected", [
    ("South Asia", "SAS"), ("South_Asia", "SAS"),
    ("countries in South Asia", "SAS"), ("countries_in_South_Asia", "SAS"),
    ("countries in EU", "EUU"),
])
def test_worldbank_keeps_complete_regional_phrases(worldbank, phrase, expected):
    assert worldbank._map_regional_term(phrase) == expected


@pytest.mark.parametrize("phrase", ["EUROPE", "EUROZONE", "Euro area", "EU27_2020", "South Asianish"])
def test_worldbank_regional_matching_cannot_consume_part_of_a_token(worldbank, phrase):
    assert worldbank._map_regional_term(phrase) is None


@pytest.mark.parametrize("phrase", ["Nord", "G7ish", "G700 countries", "ASEANish"])
def test_worldbank_group_matching_cannot_use_prefix_or_reciprocal_substring(worldbank, phrase):
    assert worldbank._expand_country_group(phrase) is None


@pytest.mark.parametrize("phrase,canonical", [
    ("EU", "EU"), ("European Union", "EU"), ("European_Union", "EU"),
    ("euro area", "EUROZONE"), ("euro_area", "EUROZONE"),
    ("G7", "G7"), ("G7 countries", "G7"), ("countries in G7", "G7"),
    ("Nordic countries", "NORDIC"), ("countries in Nordic countries", "NORDIC"),
    ("South Asia", "SOUTH_ASIA"), ("BRICS economies", "BRICS"),
])
def test_worldbank_preserves_explicit_canonical_group_members(worldbank, phrase, canonical):
    expected = CountryResolver.get_region_expansion(canonical, format="iso3")
    assert expected
    assert set(worldbank._expand_country_group(phrase) or []) == set(expected)


@pytest.mark.parametrize("alias,expected", [
    ("EU", "EU27_2020"), ("European Union", "EU27_2020"),
    ("European_Union", "EU27_2020"), (" European   Union ", "EU27_2020"),
    ("Euro area", "EA20"), ("Euro_area", "EA20"), ("Eurozone", "EA20"),
    ("EU_27", "EU27_2020"), ("EU27_2020", "EU27_2020"), ("EA20", "EA20"),
    ("Germany", "DE"), ("Greece", "EL"), ("Czech Republic", "CZ"),
])
def test_eurostat_preserves_explicit_aliases_codes_and_countries(eurostat, alias, expected):
    assert eurostat._country_code(alias) == expected


class DispatchCaptured(BaseException):
    """Stop at the mocked HTTP boundary without entering provider fallbacks."""


@pytest.mark.asyncio
@pytest.mark.parametrize("country,expected_codes", [
    *((country, [country.upper()]) for country in UNRESOLVED_EUROPE),
    ("EUU", ["EUU"]), ("ECS", ["ECS"]), ("EMU", ["EMU"]),
    ("Germany", ["DE"]), ("countries in South Asia", ["SAS"]),
    ("EU", list(CountryResolver.EU_MEMBERS)),
    ("euro area", list(CountryResolver.EUROZONE_MEMBERS)),
    ("G7", list(CountryResolver.G7_MEMBERS)),
])
async def test_worldbank_fetch_dispatch_preserves_requested_geography(
    worldbank, monkeypatch, country, expected_codes,
):
    # No catalog/database or HTTP client work; geography methods remain real.
    monkeypatch.setattr(worldbank, "_indicator_source_id", lambda indicator: None)
    monkeypatch.setattr("backend.providers.worldbank.get_http1_client", lambda: object())
    captured = {}

    async def capture(client, url, **kwargs):
        captured.update(url=url, **kwargs)
        raise DispatchCaptured

    monkeypatch.setattr(worldbank, "_get_with_retry", capture)
    with pytest.raises(DispatchCaptured):
        await worldbank.fetch_indicator(
            indicator="NY.GDP.MKTP.CD", country=country,
            start_date="2023-01-01", end_date="2023-12-31",
        )
    actual_codes = captured["url"].split("/country/", 1)[1].split("/indicator/", 1)[0].split(";")
    # World Bank accepts both ISO2 and ISO3 identifiers for individual countries.
    assert {CountryResolver.to_iso3(code) or code for code in actual_codes} == {
        CountryResolver.to_iso3(code) or code for code in expected_codes
    }
    assert len(actual_codes) == len(expected_codes)
    assert captured["url"].endswith("/indicator/NY.GDP.MKTP.CD")
    assert captured["params"]["date"] == "2023:2023"


@pytest.mark.asyncio
@pytest.mark.parametrize("country,expected", [
    *((country, country.upper()) for country in UNRESOLVED_EUROPE),
    ("European Union", "EU27_2020"), ("European_Union", "EU27_2020"),
    ("Euro area", "EA20"), ("EU27_2020", "EU27_2020"),
    ("EA20", "EA20"), ("Germany", "DE"),
])
async def test_eurostat_fetch_dispatch_preserves_requested_geography(
    eurostat, monkeypatch, country, expected,
):
    monkeypatch.setattr(eurostat, "_resolve_dataset", AsyncMock(return_value=("nama_10_gdp", "GDP")))
    monkeypatch.setattr("backend.providers.eurostat.get_http_client", lambda: object())
    captured = {}

    async def capture(client, url, **kwargs):
        captured.update(url=url, **kwargs)
        raise DispatchCaptured

    monkeypatch.setattr(eurostat, "_get_with_retry", capture)
    with pytest.raises(DispatchCaptured):
        await eurostat.fetch_indicator(
            indicator="nama_10_gdp", country=country, start_year=2023, end_year=2023,
        )
    assert captured["params"]["geo"] == expected
    assert captured["url"].endswith("/nama_10_gdp")
    assert captured["params"]["sinceTimePeriod"] == "2023"
