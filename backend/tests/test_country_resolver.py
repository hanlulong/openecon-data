from __future__ import annotations

from backend.routing.country_resolver import CountryResolver
from backend.services.query_helpers import extract_countries_from_query


def test_country_resolver_ignores_us_dollar_symbol_units() -> None:
    assert CountryResolver.detect_all_countries_in_query(
        "Germany renewable natural capital fisheries (current US$)"
    ) == ["DE"]


def test_country_resolver_ignores_us_dollar_text_units() -> None:
    assert CountryResolver.detect_all_countries_in_query(
        "Current account balance U.S. dollars"
    ) == []


def test_country_resolver_keeps_explicit_us_geography() -> None:
    assert CountryResolver.detect_all_countries_in_query("US inflation") == ["US"]
    assert CountryResolver.detect_all_countries_in_query("U.S. GDP") == ["US"]
    assert CountryResolver.detect_all_countries_in_query(
        "U.S. State Tax Collections: T23 Hunting and Fishing License for Maine"
    ) == ["US"]


def test_query_country_extraction_does_not_add_us_for_us_dollar_worldbank_units() -> None:
    assert extract_countries_from_query(
        "Germany Renewable natural capital fisheries (current US$) from World Bank"
    ) == ["DE"]
