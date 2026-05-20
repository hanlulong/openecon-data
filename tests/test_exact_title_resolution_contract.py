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
    _strip_trailing_exact_title_frequency_wrapper,
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


def test_worldbank_exact_name_match_accepts_unit_text_inside_title() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=20):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider in {"WorldBank", "WORLDBANK"}
            return [
                {
                    "provider": "WorldBank",
                    "code": "DT.ODA.DACD.RFGE.CD",
                    "name": "Gross ODA aid disbursement for refugees in donor countries,  DAC donors total (current US$)",
                    "unit": "",
                }
            ]

    query = "Gross ODA aid disbursement for refugees in donor countries DAC donors total (current US$) from World Bank"
    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(query, "WorldBank")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="WorldBank",
            countries=[],
        )

    assert match is not None
    assert match["code"] == "DT.ODA.DACD.RFGE.CD"
    assert intent is not None
    assert intent.parameters["indicator"] == "DT.ODA.DACD.RFGE.CD"
    assert intent.parameters["__exact_indicator_title_match"] is True


def test_exact_title_intent_drops_countries_that_only_appear_inside_title() -> None:
    title = (
        "Country-by-country reporting (CbCR) - Aggregate totals by the effective "
        "tax rate of the MNE group and by tax jurisdiction  - Corporate tax statistics"
    )

    class _Lookup:
        def search(self, text, provider=None, limit=20):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "OECD"
            return [
                {
                    "provider": "OECD",
                    "code": "DSD_CBCR@DF_CBCRIII",
                    "name": title,
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        intent = build_exact_indicator_title_intent(
            f"{title} from OECD",
            explicit_provider="OECD",
            countries=["ME"],
        )

    assert intent is not None
    assert intent.parameters["indicator"] == "DSD_CBCR@DF_CBCRIII"
    assert "country" not in intent.parameters
    assert "countries" not in intent.parameters


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


def test_exact_title_frequency_wrapper_strips_compound_frequency_only() -> None:
    title = "OECD based Recession Indicators for Canada from the Peak through the Trough (DISCONTINUED)"

    assert (
        _strip_trailing_exact_title_frequency_wrapper(f"{title} (Daily, 7-Day)")
        == title
    )
    assert _strip_trailing_exact_title_frequency_wrapper(
        "Bachelor's Degree or Higher (5-year estimate) in Liberty County, FL"
    ) is None
    assert title in exact_title_search_inputs(f"{title} (Daily, 7-Day) from FRED", "FRED")


def test_fred_exact_title_with_compound_frequency_resolves_exact_series() -> None:
    title = "OECD based Recession Indicators for Canada from the Peak through the Trough (DISCONTINUED)"

    class _Lookup:
        def search(self, text, provider=None, limit=20):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if title not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "CANRECDM",
                    "name": title,
                    "frequency": "Daily, 7-Day",
                    "unit": "+1 or 0",
                    "popularity": 18,
                },
                {
                    "provider": "FRED",
                    "code": "CANRECM",
                    "name": title,
                    "frequency": "Monthly",
                    "unit": "+1 or 0",
                    "popularity": 20,
                },
            ]

    query = f"{title} (Daily, 7-Day) from FRED"
    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(query, "FRED")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="FRED",
            countries=[],
            all_providers=["FRED"],
        )

    assert match is not None
    assert match["code"] == "CANRECDM"
    assert intent is not None
    assert intent.apiProvider == "FRED"
    assert intent.parameters["indicator"] == "CANRECDM"
    assert intent.parameters["__semantic_provider_locked"] is True
    assert intent.parameters["__exact_indicator_title_match"] is True
    assert is_provider_locked(intent.parameters)
    assert is_exact_match_locked(intent.parameters)


def test_fred_exact_title_without_frequency_does_not_guess_duplicate_series() -> None:
    title = "OECD based Recession Indicators for Canada from the Peak through the Trough (DISCONTINUED)"

    class _Lookup:
        def search(self, text, provider=None, limit=20):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if title not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "CANRECDM",
                    "name": title,
                    "frequency": "Daily, 7-Day",
                    "unit": "+1 or 0",
                },
                {
                    "provider": "FRED",
                    "code": "CANRECM",
                    "name": title,
                    "frequency": "Monthly",
                    "unit": "+1 or 0",
                },
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(f"{title} from FRED", "FRED")
        intent = build_exact_indicator_title_intent(
            f"{title} from FRED",
            explicit_provider="FRED",
            countries=[],
            all_providers=["FRED"],
        )

    assert match is None
    assert intent is None


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


def test_exact_title_match_accepts_short_imf_title_with_unit_and_source_context() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "IMF"
            return [
                {
                    "provider": "IMF",
                    "code": "DEBT1",
                    "name": "DEBT",
                    "unit": "% of GDP",
                    "category": "DEBT",
                    "raw_metadata": (
                        '{"label":"DEBT","source":"Fiscal Affairs Departmental Data",'
                        '"unit":"% of GDP","dataset":"DEBT"}'
                    ),
                }
            ]

    query = "India DEBT in percent of GDP Fiscal Affairs Departmental Data from IMF"
    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(query, "IMF")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="IMF",
            countries=["IN"],
            all_providers=["IMF"],
        )

    assert match is not None
    assert match["code"] == "DEBT1"
    assert intent is not None
    assert intent.parameters["indicator"] == "DEBT1"
    assert intent.parameters["country"] == "IN"
    assert is_exact_match_locked(intent.parameters)


def test_exact_title_match_accepts_short_imf_revenue_title_with_source_context() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "IMF"
            return [
                {
                    "provider": "IMF",
                    "code": "GGR_G01_GDP_PT",
                    "name": "Revenue",
                    "unit": "% of GDP",
                    "category": "FM",
                    "raw_metadata": (
                        '{"label":"Revenue","source":"Fiscal Monitor (October 2025)",'
                        '"unit":"% of GDP","dataset":"FM"}'
                    ),
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            "Japan Revenue in percent of GDP Fiscal Monitor (October 2025) from IMF",
            "IMF",
        )

    assert match is not None
    assert match["code"] == "GGR_G01_GDP_PT"


def test_exact_title_match_rejects_short_imf_title_without_source_context() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "IMF"
            return [
                {
                    "provider": "IMF",
                    "code": "GGR_G01_GDP_PT",
                    "name": "Revenue",
                    "unit": "% of GDP",
                    "category": "FM",
                    "raw_metadata": (
                        '{"label":"Revenue","source":"Fiscal Monitor (October 2025)",'
                        '"unit":"% of GDP","dataset":"FM"}'
                    ),
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match("Japan Revenue in percent of GDP from IMF", "IMF")

    assert match is None


def test_exact_title_match_rejects_short_non_executable_imf_title() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "IMF"
            return [
                {
                    "provider": "IMF",
                    "code": "FM4_XDC",
                    "name": "M4",
                    "unit": "National Currency",
                    "category": "INDICATOR",
                    "raw_metadata": (
                        '{"label":"M4","source":"International Financial Statistics",'
                        '"unit":"National Currency"}'
                    ),
                }
            ]

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(
            "Germany M4 in National Currency International Financial Statistics from IMF",
            "IMF",
        )

    assert match is None


def test_exact_title_match_accepts_full_imf_cpi_aggregate_titles(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    cases = [
        (
            "PCPI_CP_02_BY2008_IX",
            (
                "Prices, Consumer Prices, All Items, By Classification of Individual "
                "Consumption According to Purpose (COICOP) 1999, Expenditure of "
                "Households, Alcoholic Beverages, Tobacco, and Narcotics, BY2008, Index"
            ),
        ),
        (
            "PCPI_CP_04_BY2005_IX",
            (
                "Prices, Consumer Price Index, Housing, water, electricity, gas and other "
                "fuels, COICOP, Base Year = 2005, Index"
            ),
        ),
        (
            "PCPI_CP_06_BY2010_IX",
            "Prices, Consumer Price Index, Health, COICOP, Base Year = 2010, Index",
        ),
    ]
    for code, name in cases:
        assert db.insert_indicator(Indicator(provider="IMF", code=code, name=name, category="INDICATOR"))
    lookup = IndicatorLookup(db)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        for code, name in cases:
            query = f"Brazil {name} from IMF"
            match = find_exact_provider_title_match(query, "IMF")
            looks_exact = looks_like_exact_provider_title_match(query, "IMF")

            assert match is not None
            assert match["code"] == code
            assert looks_exact is True


def test_exact_title_match_does_not_accept_shortened_imf_cpi_phrase(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="IMF",
            code="PCPI_CP_06_BY2010_IX",
            name="Prices, Consumer Price Index, Health, COICOP, Base Year = 2010, Index",
            category="INDICATOR",
        )
    )
    lookup = IndicatorLookup(db)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(
            "Brazil Health Consumer Price Index Base Year = 2010 from IMF",
            "IMF",
        )
        looks_exact = looks_like_exact_provider_title_match(
            "Brazil Health Consumer Price Index Base Year = 2010 from IMF",
            "IMF",
        )

    assert match is None
    assert looks_exact is False


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


def test_exact_title_match_uses_token_bag_for_worldbank_title_permutation(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    assert db.insert_indicator(
        Indicator(
            provider="WorldBank",
            code="DXGSRMRCHSAXD",
            name="Exports Merchandise, Customs, Price, US$, seas. adj.",
            description="The price index of Merchandise (goods) exports, free on board (f.o.b.), in US$ seasonally adjusted.",
            category="Global Economic Monitor",
            popularity=10,
            raw_metadata='{"source": {"id": "15", "value": "Global Economic Monitor"}}',
        )
    )
    lookup = IndicatorLookup(db)
    query = "Customs Price US$ seas. adj. Exports Merchandise from World Bank"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "WorldBank")
        broad_match = find_exact_provider_title_match(
            "Merchandise exports from World Bank",
            "WorldBank",
        )

    assert match is not None
    assert match["code"] == "DXGSRMRCHSAXD"
    assert broad_match is None


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


def test_exact_title_match_collapses_bis_prefixed_dataflow_aliases(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    for code, category in [
        ("BIS_WS_CPMI_INSTITUT", "BIS Dataflow"),
        ("WS_CPMI_INSTITUT", "BIS Statistics"),
    ]:
        assert db.insert_indicator(
            Indicator(
                provider="BIS",
                code=code,
                name="CPMI institutions",
                category=category,
                frequency="Annual",
                popularity=10,
            )
        )
    lookup = IndicatorLookup(db)
    query = "United States CPMI institutions from BIS"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "BIS")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="BIS",
            countries=["US"],
            all_providers=["BIS"],
        )

    assert match is not None
    assert match["code"] == "BIS_WS_CPMI_INSTITUT"
    assert intent is not None
    assert intent.parameters["indicator"] == "BIS_WS_CPMI_INSTITUT"
    assert intent.parameters["country"] == "US"


def test_exact_title_match_rejects_bis_same_title_different_dataflows() -> None:
    lookup = Mock(
        exact_name_matches=Mock(
            return_value=[
                {
                    "provider": "BIS",
                    "code": "BIS_WS_TEST_A",
                    "name": "CPMI duplicate title",
                    "frequency": "Annual",
                },
                {
                    "provider": "BIS",
                    "code": "WS_TEST_B",
                    "name": "CPMI duplicate title",
                    "frequency": "Annual",
                },
            ]
        ),
        search=Mock(return_value=[]),
    )

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match("CPMI duplicate title from BIS", "BIS")

    assert match is None


def test_exact_title_match_rejects_bis_aliases_with_different_frequency(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    for code, frequency in [
        ("BIS_WS_TEST_ALIAS", "Annual"),
        ("WS_TEST_ALIAS", "Monthly"),
    ]:
        assert db.insert_indicator(
            Indicator(
                provider="BIS",
                code=code,
                name="CPMI alias title",
                category="BIS Statistics",
                frequency=frequency,
                popularity=10,
            )
        )
    lookup = IndicatorLookup(db)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match("CPMI alias title from BIS", "BIS")

    assert match is None


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


def test_exact_title_match_keeps_fred_title_internal_semiannual_as_title_text(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    title = "ICE BofA CCC & Lower US High Yield Index Semi-Annual Yield to Worst"
    assert db.insert_indicator(
        Indicator(
            provider="FRED",
            code="BAMLH0A3HYCSYTW",
            name=title,
            unit="Percent",
            frequency="Daily, Close",
            popularity=20,
        )
    )
    lookup = IndicatorLookup(db)
    query = f"{title} from FRED"

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        match = find_exact_provider_title_match(query, "FRED")
        looks_exact = looks_like_exact_provider_title_match(query, "FRED")
        intent = build_exact_indicator_title_intent(
            query,
            explicit_provider="FRED",
            countries=[],
            all_providers=["FRED"],
        )

    assert match is not None
    assert match["code"] == "BAMLH0A3HYCSYTW"
    assert looks_exact is True
    assert intent is not None
    assert intent.parameters["indicator"] == "BAMLH0A3HYCSYTW"
    assert intent.parameters["__exact_indicator_title_match"] is True


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


def test_exact_title_match_preserves_real_measurement_qualifier() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if "Real Residential Property Prices for China" not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "QCNN628BIS",
                    "name": "Residential Property Prices for China",
                    "unit": "Index 2010=100",
                    "frequency": "Quarterly",
                    "description": "Residential property price index.",
                    "keywords": "residential property prices china housing",
                    "popularity": 50,
                },
                {
                    "provider": "FRED",
                    "code": "QCNR628BIS",
                    "name": "Real Residential Property Prices for China",
                    "unit": "Index 2010=100",
                    "frequency": "Quarterly",
                    "description": "The series is deflated using CPI.",
                    "keywords": "real residential property prices china cpi",
                    "popularity": 40,
                },
            ]

    query = "Real Residential Property Prices for China in Index 2010=100 from FRED"
    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(query, "FRED")

    assert match is not None
    assert match["code"] == "QCNR628BIS"


def test_exact_title_match_applies_frequency_to_strict_name_matches() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            return []

        def exact_name_matches(self, search_inputs, provider=None, limit=20):
            assert provider == "FRED"
            if "Bank Credit All Commercial Banks" not in search_inputs:
                return []
            return [
                {
                    "provider": "FRED",
                    "code": "TOTBKCR",
                    "name": "Bank Credit, All Commercial Banks",
                    "unit": "Billions of U.S. Dollars",
                    "frequency": "Weekly, Ending Wednesday",
                    "popularity": 80,
                },
                {
                    "provider": "FRED",
                    "code": "LOANINV",
                    "name": "Bank Credit, All Commercial Banks",
                    "unit": "Billions of U.S. Dollars",
                    "frequency": "Monthly",
                    "popularity": 60,
                },
            ]

    query = "US Bank Credit All Commercial Banks in Billions of U.S. Dollars (Monthly) from FRED"
    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        match = find_exact_provider_title_match(query, "FRED")

    assert match is not None
    assert match["code"] == "LOANINV"


def test_build_exact_statscan_title_intent_keeps_country_scope_outside_title() -> None:
    lookup_results = [
        {
            "provider": "StatsCan",
            "code": "13100287",
            "name": "Health behaviour in school-aged children 2002, student response to question: How often do you go to school or to bed hungry because there is not enough food at home?",
        }
    ]

    with patch(
        "backend.services.indicator_database.get_indicator_lookup",
        return_value=Mock(search=Mock(return_value=lookup_results)),
    ):
        intent = build_exact_indicator_title_intent(
            "Canada Health behaviour in school-aged children 2002, student response to question: How often do you go to school or to bed hungry because there is not enough food at home? from Statistics Canada",
            explicit_provider="StatsCan",
            countries=["CA"],
            all_providers=["StatsCan"],
        )

    assert intent is not None
    assert intent.parameters["indicator"] == "13100287"
    assert intent.parameters["country"] == "CA"
    assert intent.parameters["geography"] == "Canada"


def test_exact_name_matches_prefers_unicode_raw_provider_title(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    for code, name, popularity in [
        ("ae-coin", "Æ Coin", 1),
        ("coin-2", "COIN", 100),
        ("coin-3", "Coin", 90),
    ]:
        assert db.insert_indicator(
            Indicator(
                provider="CoinGecko",
                code=code,
                name=name,
                category="Cryptocurrency",
                unit="USD",
                popularity=popularity,
            )
        )
    lookup = IndicatorLookup(db)

    exact_rows = lookup.exact_name_matches(["Æ Coin"], provider="CoinGecko")
    lowercase_rows = lookup.exact_name_matches(["æ coin"], provider="CoinGecko")
    generic_rows = lookup.exact_name_matches(["Coin"], provider="CoinGecko")

    assert [row["code"] for row in exact_rows] == ["ae-coin"]
    assert [row["code"] for row in lowercase_rows] == ["ae-coin"]
    assert "ae-coin" not in [row["code"] for row in generic_rows]


def test_build_exact_indicator_title_intent_handles_unicode_coingecko_title(tmp_path) -> None:
    db = IndicatorDatabase(tmp_path / "indicators.db")
    for code, name, popularity in [
        ("ae-coin", "Æ Coin", 1),
        ("coin-2", "COIN", 100),
        ("coin-3", "Coin", 90),
    ]:
        assert db.insert_indicator(
            Indicator(
                provider="CoinGecko",
                code=code,
                name=name,
                category="Cryptocurrency",
                unit="USD",
                popularity=popularity,
            )
        )
    lookup = IndicatorLookup(db)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        intent = build_exact_indicator_title_intent(
            "Æ Coin cryptocurrency price from CoinGecko",
            explicit_provider="CoinGecko",
            all_providers=["CoinGecko"],
        )
        generic_intent = build_exact_indicator_title_intent(
            "Coin cryptocurrency price from CoinGecko",
            explicit_provider="CoinGecko",
            all_providers=["CoinGecko"],
        )

    assert intent is not None
    assert intent.parameters["indicator"] == "ae-coin"
    assert intent.parameters["coinIds"] == ["ae-coin"]
    assert intent.parameters["__exact_indicator_title_match"] is True
    assert generic_intent is not None
    assert generic_intent.parameters["indicator"] != "ae-coin"
    assert "ae-coin" not in generic_intent.parameters.get("coinIds", [])


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
