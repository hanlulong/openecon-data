from __future__ import annotations

from unittest.mock import Mock, patch

from backend.services.indicator_database import Indicator, IndicatorDatabase, IndicatorLookup
from backend.services.indicator_resolution import (
    build_exact_indicator_title_intent,
    find_exact_provider_title_match,
    is_exact_match_locked,
    is_provider_locked,
    looks_like_exact_provider_title_match,
)


def test_build_exact_indicator_title_intent_builds_provider_locked_intent() -> None:
    lookup_results = [
        {
            "provider": "FRED",
            "code": "MELIPRVSUSCOUNTY24005",
            "name": "Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
        }
    ]

    with patch(
        "backend.services.indicator_database.get_indicator_lookup",
        return_value=Mock(search=Mock(return_value=lookup_results)),
    ):
        intent = build_exact_indicator_title_intent(
            "US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
            explicit_provider="FRED",
            countries=["US"],
            all_providers=["FRED"],
        )

    assert intent is not None
    assert intent.apiProvider == "FRED"
    assert intent.parameters["indicator"] == "MELIPRVSUSCOUNTY24005"
    assert intent.parameters["country"] == "US"
    assert intent.parameters["__semantic_provider_locked"] is True
    assert intent.parameters["__exact_indicator_title_match"] is True


def test_build_exact_indicator_title_intent_rejects_generic_suffix_only_match() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            if provider == "FRED":
                return [
                    {
                        "provider": "FRED",
                        "code": "PCETRIM12M159SFRBDAL",
                        "name": "Trimmed Mean PCE Inflation Rate",
                    }
                ]
            return []

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        intent = build_exact_indicator_title_intent(
            "Germany inflation rate",
            all_providers=["FRED"],
        )

    assert intent is None


def test_build_exact_indicator_title_intent_rejects_broad_catalog_concept_suffix_match() -> None:
    lookup_results = [
        {
            "provider": "FRED",
            "code": "REAINTRATREARAT10Y",
            "name": "10-Year Real Interest Rate",
        }
    ]

    with patch(
        "backend.services.indicator_database.get_indicator_lookup",
        return_value=Mock(search=Mock(return_value=lookup_results)),
    ):
        intent = build_exact_indicator_title_intent(
            "interest rate",
            broad_concept="interest rate",
            all_providers=["FRED"],
        )

    assert intent is None


def test_exact_title_match_prefers_count_variant_over_percentage_variant() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            if provider != "WorldBank":
                return []
            if text == "Are Teachers in post-secondary non-tertiary education female (number)":
                return [
                    {
                        "provider": "WorldBank",
                        "code": "UIS.FTP.4",
                        "name": "Percentage of teachers in post-secondary non-tertiary education who are female (%)",
                    },
                    {
                        "provider": "WorldBank",
                        "code": "UIS.T.4.F",
                        "name": "Teachers in post-secondary non-tertiary education, female (number)",
                    },
                ]
            return []

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            "Are Teachers in post-secondary non-tertiary education female (number) from World Bank",
            "WorldBank",
        )

    assert match is not None
    assert match["code"] == "UIS.T.4.F"


def test_exact_title_match_uses_exact_name_lookup_when_fts_misses_short_titles() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            assert "M1 for Republic of Korea" in search_inputs
            return [
                {
                    "provider": "FRED",
                    "code": "MYAGM1KRM189S",
                    "name": "M1 for Republic of Korea",
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            "M1 for Republic of Korea from FRED",
            "FRED",
        )
        looks_exact = looks_like_exact_provider_title_match(
            "M1 for Republic of Korea from FRED",
            "FRED",
        )

    assert match is not None
    assert match["code"] == "MYAGM1KRM189S"
    assert looks_exact is True


def test_exact_title_match_accepts_short_imf_weo_titles() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "IMF"
            assert "Real GDP growth" in search_inputs
            return [
                {
                    "provider": "IMF",
                    "code": "NGDP_RPCH",
                    "name": "Real GDP growth",
                    "category": "WEO",
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match("Japan Real GDP growth from IMF", "IMF")
        looks_exact = looks_like_exact_provider_title_match("Japan Real GDP growth from IMF", "IMF")

    assert match is not None
    assert match["code"] == "NGDP_RPCH"
    assert looks_exact is True


def test_exact_title_match_rejects_ambiguous_duplicate_imf_title_without_unit() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "IMF"
            return [
                {
                    "provider": "IMF",
                    "code": "NGDPDPC",
                    "name": "GDP per capita, current prices",
                    "unit": "U.S. dollars per capita",
                    "category": "WEO",
                },
                {
                    "provider": "IMF",
                    "code": "PPPPC",
                    "name": "GDP per capita, current prices",
                    "unit": "Purchasing power parity; international dollars per capita",
                    "category": "WEO",
                },
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match("Germany GDP per capita current prices from IMF", "IMF")
        looks_exact = looks_like_exact_provider_title_match(
            "Germany GDP per capita current prices from IMF",
            "IMF",
        )

    assert match is None
    assert looks_exact is True


def test_exact_title_match_uses_unit_to_disambiguate_duplicate_imf_title() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "IMF"
            assert "GDP per capita current prices" in search_inputs
            return [
                {
                    "provider": "IMF",
                    "code": "NGDPDPC",
                    "name": "GDP per capita, current prices",
                    "unit": "U.S. dollars per capita",
                    "category": "WEO",
                },
                {
                    "provider": "IMF",
                    "code": "PPPPC",
                    "name": "GDP per capita, current prices",
                    "unit": "Purchasing power parity; international dollars per capita",
                    "category": "WEO",
                },
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            "Germany GDP per capita current prices in U.S. dollars per capita from IMF",
            "IMF",
        )

    assert match is not None
    assert match["code"] == "NGDPDPC"


def test_exact_title_match_prefers_base_worldbank_series_over_unrequested_quintile_variant() -> None:
    class _Lookup:
        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            return []

        def search(self, text, provider=None, limit=5):
            if provider != "WorldBank":
                return []
            assert limit >= 20
            return [
                {
                    "provider": "WorldBank",
                    "code": "SH.DYN.MORT.Q2",
                    "name": "Under-5 mortality rate (per 1,000 live births): Q2",
                    "category": "Health Nutrition and Population Statistics by Wealth Quintile",
                },
                {
                    "provider": "WorldBank",
                    "code": "SH.DYN.MORT",
                    "name": "Mortality rate, under-5 (per 1,000 live births)",
                    "category": "World Development Indicators",
                },
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            "India Mortality rate under-5 (per 1 000 live births) from World Bank",
            "WorldBank",
        )

    assert match is not None
    assert match["code"] == "SH.DYN.MORT"


def test_exact_title_match_uses_normalized_title_lookup_for_comma_variants(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="WorldBank",
            code="SL.UEM.TOTL.MA.NE.ZS",
            name="Unemployment, male (% of male labor force) (national estimate)",
            popularity=10,
        )
    )
    lookup = IndicatorLookup(db)
    query = "Japan Unemployment male (% of male labor force) (national estimate) from World Bank"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "WorldBank")
        looks_exact = looks_like_exact_provider_title_match(query, "WorldBank")

    assert match is not None
    assert match["code"] == "SL.UEM.TOTL.MA.NE.ZS"
    assert looks_exact is True


def test_exact_title_match_accepts_short_worldbank_public_source_title(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="WorldBank",
            code="TOT",
            name="Terms of Trade",
            category="Global Economic Monitor",
            popularity=10,
            raw_metadata='{"source": {"id": "15", "value": "Global Economic Monitor"}}',
        )
    )
    lookup = IndicatorLookup(db)
    query = "Terms of Trade from World Bank"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "WorldBank")
        looks_exact = looks_like_exact_provider_title_match(query, "WorldBank")

    assert match is not None
    assert match["code"] == "TOT"
    assert looks_exact is True


def test_exact_title_match_rejects_short_worldbank_partial_title(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="WorldBank",
            code="TOT",
            name="Terms of Trade",
            category="Global Economic Monitor",
            popularity=10,
        )
    )
    lookup = IndicatorLookup(db)
    query = "Trade from World Bank"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "WorldBank")
        looks_exact = looks_like_exact_provider_title_match(query, "WorldBank")

    assert match is None
    assert looks_exact is False


def test_exact_title_match_ignores_appended_frequency_disambiguator(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="FRED",
            code="DTWEXM",
            name="Nominal Major Currencies U.S. Dollar Index (Goods Only) (DISCONTINUED)",
            popularity=10,
        )
    )
    lookup = IndicatorLookup(db)
    query = (
        "US Nominal Major Currencies U.S. Dollar Index (Goods Only) "
        "(DISCONTINUED) (Daily) from FRED"
    )

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "FRED")
        looks_exact = looks_like_exact_provider_title_match(query, "FRED")

    assert match is not None
    assert match["code"] == "DTWEXM"
    assert looks_exact is True


def test_exact_and_provider_lock_helpers_read_shared_flags() -> None:
    params = {
        "__semantic_provider_locked": True,
        "__exact_indicator_title_match": True,
    }

    assert is_provider_locked(params) is True
    assert is_exact_match_locked(params) is True
    assert is_provider_locked({}) is False
    assert is_exact_match_locked({}) is False
