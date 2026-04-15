from __future__ import annotations

from backend.services.query_helpers import extract_countries_from_query


def test_extract_countries_from_query_ignores_worldbank_provider_suffix():
    countries = extract_countries_from_query(
        "Brazil Sons and daughters have equal rights to inherit assets from their parents (1=yes; 0=no) from World Bank"
    )

    assert countries == ["BR"]


def test_extract_countries_from_query_preserves_actual_world_aggregate_request():
    countries = extract_countries_from_query("World GDP per capita from World Bank")

    assert countries == ["1W"]


def test_extract_countries_from_query_ignores_oecd_provider_at_query_start():
    countries = extract_countries_from_query("OECD unemployment rate in Japan")

    assert countries == ["JP"]
