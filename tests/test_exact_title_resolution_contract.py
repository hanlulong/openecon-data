from __future__ import annotations

from unittest.mock import Mock, patch

from backend.services.indicator_database import Indicator, IndicatorDatabase, IndicatorLookup
from backend.services.indicator_resolution import (
    build_exact_indicator_title_intent,
    exact_title_search_inputs,
    find_exact_provider_title_match,
    is_exact_match_locked,
    is_provider_locked,
    looks_like_exact_provider_title_match,
    _strip_trailing_exact_title_unit_suffix,
    _trailing_exact_title_unit_suffix,
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


def test_exact_title_unit_suffix_does_not_strip_national_accounts_title_text() -> None:
    title = "Population in the National Accounts distribution of people in income quintiles by age from OECD"

    assert _trailing_exact_title_unit_suffix(title) is None
    assert _strip_trailing_exact_title_unit_suffix(title) is None
    assert "Population" not in exact_title_search_inputs(title, "OECD")


def test_exact_title_unit_suffix_still_detects_measurement_phrases() -> None:
    assert _trailing_exact_title_unit_suffix(
        "GDP per capita current prices in U.S. dollars per capita from IMF"
    )
    assert _trailing_exact_title_unit_suffix(
        "Some provider title in Index 2017=100 from FRED"
    )
    assert _trailing_exact_title_unit_suffix(
        "National Accounts, gross value added in National Currency from IMF"
    )


def test_oecd_national_accounts_exact_titles_resolve_without_unit_suffix_false_positive(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    cases = [
        (
            "United States Population in the National Accounts: distribution of people in income quintiles by age from OECD",
            "DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE",
            "Population in the National Accounts: distribution of people in income quintiles by age",
        ),
        (
            "United States Household income and saving in the National Accounts: distributions by main source of income from OECD",
            "DSD_EGDNA_INC_MSI@DF_INC_MSI",
            "Household income and saving in the National Accounts: distributions by main source of income",
        ),
    ]
    for _query, code, name in cases:
        assert db.insert_indicator(Indicator(provider="OECD", code=code, name=name, popularity=10))
    lookup = IndicatorLookup(db)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        for query, code, name in cases:
            match = find_exact_provider_title_match(query, "OECD")
            looks_exact = looks_like_exact_provider_title_match(query, "OECD")
            intent = build_exact_indicator_title_intent(
                query,
                explicit_provider="OECD",
                countries=["US"],
                all_providers=["OECD"],
            )

            assert match is not None
            assert match["code"] == code
            assert looks_exact is True
            assert intent is not None
            assert intent.parameters["indicator"] == code
            assert intent.indicators == [name]


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


def test_exact_title_match_accepts_short_bis_hyphenated_dataflow_title(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="BIS",
            code="BIS_WS_CREDIT_GAP",
            name="Credit-to-GDP gaps",
            category="BIS Statistics",
            popularity=10,
        )
    )
    lookup = IndicatorLookup(db)
    query = "China Credit-to-GDP gaps from BIS"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "BIS")
        looks_exact = looks_like_exact_provider_title_match(query, "BIS")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="BIS",
            countries=["CN"],
            all_providers=["BIS"],
        )

    assert match is not None
    assert match["code"] == "BIS_WS_CREDIT_GAP"
    assert looks_exact is True
    assert intent is not None
    assert intent.parameters["indicator"] == "BIS_WS_CREDIT_GAP"
    assert intent.parameters["country"] == "CN"


def test_exact_title_match_rejects_short_bis_partial_title(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="BIS",
            code="BIS_WS_CREDIT_GAP",
            name="Credit-to-GDP gaps",
            category="BIS Statistics",
            popularity=10,
        )
    )
    lookup = IndicatorLookup(db)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        credit_match = find_exact_provider_title_match("credit from BIS", "BIS")
        gaps_match = find_exact_provider_title_match("GDP gaps from BIS", "BIS")
        looks_credit = looks_like_exact_provider_title_match("credit from BIS", "BIS")
        looks_gaps = looks_like_exact_provider_title_match("GDP gaps from BIS", "BIS")

    assert credit_match is None
    assert gaps_match is None
    assert looks_credit is False
    assert looks_gaps is False


def test_exact_title_search_inputs_include_dash_normalized_variant() -> None:
    variants = exact_title_search_inputs("China Credit-to-GDP gaps from BIS", "BIS")

    assert "Credit to GDP gaps" in variants


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


def test_exact_title_search_inputs_include_leading_acronym_comma_tail_variant() -> None:
    variants = exact_title_search_inputs("US BLS Total wages and salaries from FRED", "FRED")

    assert "Total wages and salaries, BLS" in variants


def test_exact_title_search_inputs_reject_short_acronym_tail_variant() -> None:
    variants = exact_title_search_inputs("US BLS wages from FRED", "FRED")

    assert "wages, BLS" not in variants


def test_exact_title_match_accepts_leading_acronym_tail_title(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="FRED",
            code="BA06RC1A027NBEA",
            name="Total wages and salaries, BLS",
            unit="Billions of Dollars",
            frequency="Annual",
            popularity=49,
        )
    )
    lookup = IndicatorLookup(db)
    query = "US BLS Total wages and salaries from FRED"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "FRED")
        looks_exact = looks_like_exact_provider_title_match(query, "FRED")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="FRED",
            countries=["US"],
            all_providers=["FRED"],
        )

    assert match is not None
    assert match["code"] == "BA06RC1A027NBEA"
    assert looks_exact is True
    assert intent is not None
    assert intent.parameters["indicator"] == "BA06RC1A027NBEA"
    assert intent.parameters["country"] == "US"
    assert intent.parameters["__semantic_provider_locked"] is True
    assert intent.parameters["__exact_indicator_title_match"] is True
    assert intent.clarificationNeeded is False


def test_exact_title_match_rejects_generic_leading_acronym_fragment(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="FRED",
            code="BA06RC1A027NBEA",
            name="Total wages and salaries, BLS",
            popularity=49,
        )
    )
    lookup = IndicatorLookup(db)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        assert find_exact_provider_title_match("US BLS wages from FRED", "FRED") is None


def test_exact_title_match_strips_fred_unit_suffix_and_uses_unit_to_disambiguate() -> None:
    title = "Nonfarm Business Sector: Labor Productivity (Output per Hour) for All Workers"

    class _Lookup:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            self.calls.append(list(search_inputs))
            if title not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "PRS85006092",
                    "name": title,
                    "unit": "Percent Change at Annual Rate",
                    "popularity": 56,
                },
                {
                    "provider": "FRED",
                    "code": "OPHNFB",
                    "name": title,
                    "unit": "Index 2017=100",
                    "popularity": 69,
                },
                {
                    "provider": "FRED",
                    "code": "PRS85006091",
                    "name": title,
                    "unit": "Percent Change from Quarter One Year Ago",
                    "popularity": 40,
                },
            ]

    lookup = _Lookup()
    query = f"US {title} in Index 2017=100 from FRED"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "FRED")
        looks_exact = looks_like_exact_provider_title_match(query, "FRED")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="FRED",
            countries=["US"],
            all_providers=["FRED"],
        )

    assert match is not None
    assert match["code"] == "OPHNFB"
    assert looks_exact is True
    assert intent is not None
    assert intent.parameters["indicator"] == "OPHNFB"
    assert intent.parameters["__semantic_provider_locked"] is True
    assert intent.parameters["__exact_indicator_title_match"] is True
    assert intent.clarificationNeeded is False
    assert any(title in call for call in lookup.calls)


def test_exact_title_match_strips_fred_unit_suffix_for_percent_variant() -> None:
    title = "Nonfarm Business Sector: Labor Productivity (Output per Hour) for All Workers"

    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if title not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "OPHNFB",
                    "name": title,
                    "unit": "Index 2017=100",
                    "popularity": 69,
                },
                {
                    "provider": "FRED",
                    "code": "PRS85006092",
                    "name": title,
                    "unit": "Percent Change at Annual Rate",
                    "popularity": 56,
                },
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            f"US {title} in Percent Change at Annual Rate from FRED",
            "FRED",
        )

    assert match is not None
    assert match["code"] == "PRS85006092"


def test_exact_title_match_rejects_fred_mismatched_explicit_unit() -> None:
    title = "Nonfarm Business Sector: Labor Productivity (Output per Hour) for All Workers"

    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if title not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "OPHNFB",
                    "name": title,
                    "unit": "Index 2017=100",
                    "popularity": 69,
                },
                {
                    "provider": "FRED",
                    "code": "PRS85006092",
                    "name": title,
                    "unit": "Percent Change at Annual Rate",
                    "popularity": 56,
                },
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            f"US {title} in Imaginary Units from FRED",
            "FRED",
        )

    assert match is None


def test_exact_title_match_does_not_expand_generic_fred_unit_phrase_to_title() -> None:
    title = "Nonfarm Business Sector: Labor Productivity (Output per Hour) for All Workers"

    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            return [
                {
                    "provider": "FRED",
                    "code": "OPHNFB",
                    "name": title,
                    "unit": "Index 2017=100",
                    "popularity": 69,
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            "US labor productivity in Index 2017=100 from FRED",
            "FRED",
        )
        looks_exact = looks_like_exact_provider_title_match(
            "US labor productivity in Index 2017=100 from FRED",
            "FRED",
        )

    assert match is None
    assert looks_exact is False


def test_exact_title_match_accepts_short_fred_title_with_frequency_disambiguator() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if "Demand Deposits" not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "WDDNS",
                    "name": "Demand Deposits",
                    "unit": "Billions of Dollars",
                    "frequency": "Weekly, Ending Monday",
                    "popularity": 37,
                },
                {
                    "provider": "FRED",
                    "code": "DEMDEPSL",
                    "name": "Demand Deposits",
                    "unit": "Billions of Dollars",
                    "frequency": "Monthly",
                    "popularity": 43,
                },
                {
                    "provider": "FRED",
                    "code": "DEMDEPNS",
                    "name": "Demand Deposits",
                    "unit": "Billions of Dollars",
                    "frequency": "Monthly",
                    "popularity": 15,
                },
            ]

    query = "US Demand Deposits (Monthly) from FRED"
    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(query, "FRED")
        looks_exact = looks_like_exact_provider_title_match(query, "FRED")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="FRED",
            countries=["US"],
            all_providers=["FRED"],
        )

    assert match is not None
    assert match["code"] == "DEMDEPSL"
    assert looks_exact is True
    assert intent is not None
    assert intent.parameters["indicator"] == "DEMDEPSL"
    assert intent.parameters["country"] == "US"
    assert intent.parameters["__exact_indicator_title_match"] is True


def test_exact_title_match_rejects_ambiguous_short_fred_title_without_frequency() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if "M1" not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "M1SL",
                    "name": "M1",
                    "unit": "Billions of Dollars",
                    "frequency": "Monthly",
                    "popularity": 82,
                },
                {
                    "provider": "FRED",
                    "code": "WM1NS",
                    "name": "M1",
                    "unit": "Billions of Dollars",
                    "frequency": "Weekly, Ending Monday",
                    "popularity": 66,
                },
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match("M1 from FRED", "FRED")
        looks_exact = looks_like_exact_provider_title_match("M1 from FRED", "FRED")

    assert match is None
    assert looks_exact is True


def test_exact_title_match_does_not_promote_non_exact_short_fred_phrase() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            return [
                {
                    "provider": "FRED",
                    "code": "DEMDEPSL",
                    "name": "Demand Deposits",
                    "unit": "Billions of Dollars",
                    "frequency": "Monthly",
                    "popularity": 43,
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match("Deposits from FRED", "FRED")
        looks_exact = looks_like_exact_provider_title_match("Deposits from FRED", "FRED")

    assert match is None
    assert looks_exact is False


def test_exact_and_provider_lock_helpers_read_shared_flags() -> None:
    params = {
        "__semantic_provider_locked": True,
        "__exact_indicator_title_match": True,
    }

    assert is_provider_locked(params) is True
    assert is_exact_match_locked(params) is True
    assert is_provider_locked({}) is False
    assert is_exact_match_locked({}) is False
