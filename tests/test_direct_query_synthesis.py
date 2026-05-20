from __future__ import annotations

import json

import scripts.validation.common as common
from scripts.validation.common import (
    audit_direct_query_shape,
    CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    category_success_adjustment,
    detect_single_country_from_text,
    default_query_for_row,
    family_success_adjustment,
    heuristic_subfamily_adjustment,
    preferred_default_country_for_record,
    preferred_default_country,
    imf_public_sdmx_runtime_family,
    provider_family_key,
    provider_subfamily_key,
    subfamily_success_adjustment,
)
from scripts.validation.sample_direct_cert_set import build_record


def test_default_query_for_row_naturalizes_imf_indicator_names():
    row = {
        "provider": "IMF",
        "code": "PPPI_ISIC31_IX",
        "name": "Prices, Producer Price Index, ISIC Rev. 3.1, Index",
        "description": "",
    }

    query = default_query_for_row(row)

    assert "producer price index" in query.lower()
    assert "from imf" in query.lower()
    assert "isic rev" not in query.lower()


def test_user_answerability_query_enriches_generic_imf_debt_title_without_code():
    row = {
        "provider": "IMF",
        "provider_stratum": "IMF",
        "code": "DEBT1",
        "name": "DEBT",
        "category": "DEBT",
        "unit": "% of GDP",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "DEBT1",
            "name": "DEBT",
            "category": "DEBT",
            "unit": "% of GDP",
            "raw_metadata": json.dumps(
                {
                    "label": "DEBT",
                    "description": "DEBT",
                    "source": "Fiscal Affairs Departmental Data",
                    "unit": "% of GDP",
                    "dataset": "DEBT",
                }
            ),
        },
        "provenance": {"certification_target": CERTIFICATION_TARGET_USER_ANSWERABILITY},
    }

    query = default_query_for_row(row, certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY)

    assert query == "India DEBT in percent of GDP Fiscal Affairs Departmental Data from IMF"
    assert "DEBT1" not in query


def test_user_answerability_query_enriches_generic_imf_revenue_title_without_code():
    row = {
        "provider": "IMF",
        "provider_stratum": "IMF",
        "code": "GGR_G01_GDP_PT",
        "name": "Revenue",
        "category": "FM",
        "unit": "% of GDP",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "GGR_G01_GDP_PT",
            "name": "Revenue",
            "category": "FM",
            "unit": "% of GDP",
            "raw_metadata": json.dumps(
                {
                    "label": "Revenue",
                    "description": "Revenue",
                    "source": "Fiscal Monitor (October 2025)",
                    "unit": "% of GDP",
                    "dataset": "FM",
                }
            ),
        },
        "provenance": {"certification_target": CERTIFICATION_TARGET_USER_ANSWERABILITY},
    }

    query = default_query_for_row(row, certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY)

    assert query == "Japan Revenue in percent of GDP Fiscal Monitor (October 2025) from IMF"
    assert "GGR_G01_GDP_PT" not in query


def test_user_answerability_query_does_not_enrich_non_executable_imf_indicator_title():
    row = {
        "provider": "IMF",
        "provider_stratum": "IMF",
        "code": "FM4_XDC",
        "name": "M4 Monetary",
        "category": "INDICATOR",
        "unit": "National Currency",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "FM4_XDC",
            "name": "M4 Monetary",
            "category": "INDICATOR",
            "unit": "National Currency",
            "raw_metadata": json.dumps(
                {
                    "label": "M4 Monetary",
                    "source": "International Financial Statistics",
                    "unit": "National Currency",
                }
            ),
        },
        "provenance": {"certification_target": CERTIFICATION_TARGET_USER_ANSWERABILITY},
    }

    query = default_query_for_row(row, certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY)

    assert query == "Brazil M4 Monetary from IMF"
    assert "International Financial Statistics" not in query


def test_user_answerability_query_adds_unit_for_duplicate_provider_title(monkeypatch):
    monkeypatch.setattr(
        common,
        "_provider_title_units",
        lambda provider: {
            "gdp per capita current prices": [
                {
                    "code": "NGDPDPC",
                    "unit": "U.S. dollars per capita",
                    "name": "GDP per capita, current prices",
                },
                {
                    "code": "PPPPC",
                    "unit": "Purchasing power parity; international dollars per capita",
                    "name": "GDP per capita, current prices",
                },
            ]
        },
    )
    row = {
        "provider": "IMF",
        "code": "NGDPDPC",
        "name": "GDP per capita, current prices",
        "unit": "U.S. dollars per capita",
        "category": "WEO",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert "U.S. dollars per capita" in query
    assert query.endswith("from IMF")


def test_user_answerability_query_adds_frequency_for_duplicate_provider_title(monkeypatch):
    monkeypatch.setattr(
        common,
        "_provider_title_units",
        lambda provider: {
            "deposits all commercial banks": [
                {
                    "code": "DPSACBW027SBOG",
                    "unit": "Billions of U.S. Dollars",
                    "frequency": "Weekly, Ending Wednesday",
                    "name": "Deposits, All Commercial Banks",
                },
                {
                    "code": "DPSACBM027NBOG",
                    "unit": "Billions of U.S. Dollars",
                    "frequency": "Monthly",
                    "name": "Deposits, All Commercial Banks",
                },
            ]
        },
    )
    row = {
        "provider": "FRED",
        "code": "DPSACBW027SBOG",
        "name": "Deposits, All Commercial Banks",
        "unit": "Billions of U.S. Dollars",
        "frequency": "Weekly, Ending Wednesday",
        "category": "Search: bank",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert "Weekly, Ending Wednesday" in query
    assert query.endswith("from FRED")


def test_user_answerability_fred_exact_title_with_frequency_is_not_high_risk(monkeypatch):
    title = "Deposits, All Commercial Banks"
    monkeypatch.setattr(
        common,
        "_provider_title_units",
        lambda provider: {
            "deposits all commercial banks": [
                {
                    "code": "DPSACBW027SBOG",
                    "unit": "Billions of U.S. Dollars",
                    "frequency": "Weekly, Ending Wednesday",
                    "name": title,
                },
                {
                    "code": "DPSACBM027NBOG",
                    "unit": "Billions of U.S. Dollars",
                    "frequency": "Monthly",
                    "name": title,
                },
            ]
        },
    )
    row = {
        "provider": "FRED",
        "code": "DPSACBW027SBOG",
        "name": title,
        "unit": "Billions of U.S. Dollars",
        "frequency": "Weekly, Ending Wednesday",
        "category": "Search: bank",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "FRED",
            "query": query,
            "origin": {
                "name": title,
                "source_indicator_code": "DPSACBW027SBOG",
                "source_provider": "FRED",
                "category": "Search: bank",
            },
        }
    )

    assert "Weekly, Ending Wednesday" in query
    assert audit["risk_level"] != "high"


def test_user_answerability_fred_preserves_short_acronym_suffix_title():
    row = {
        "provider": "FRED",
        "code": "BA06RC1A027NBEA",
        "name": "Total wages and salaries, BLS",
        "unit": "Billions of Dollars",
        "frequency": "Annual",
        "category": "Search: GDP",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "US Total wages and salaries, BLS from FRED"


def test_user_answerability_fred_preserves_average_price_region_title_without_code_shortcut():
    title = (
        "Average Price: Steak, Rib Eye, USDA Choice, Boneless "
        "(Cost per Pound/453.6 Grams) in the Northeast Census Region - Urban"
    )
    row = {
        "provider": "FRED",
        "code": "APU0100703425",
        "name": title,
        "unit": "U.S. Dollars",
        "frequency": "Monthly",
        "category": "Commodities",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "FRED",
            "query": query,
            "origin": {
                "name": title,
                "source_indicator_code": "APU0100703425",
                "source_provider": "FRED",
                "category": "Commodities",
            },
        }
    )

    assert query == f"{title} from FRED"
    assert "APU0100703425" not in query
    assert not query.startswith(("US ", "United States "))
    assert audit["risk_level"] != "high"


def test_user_answerability_fred_preserves_average_price_us_city_average_title():
    title = (
        "Average Price: Steak, Rib Eye, USDA Choice, Boneless "
        "(Cost per Pound/453.6 Grams) in U.S. City Average"
    )
    row = {
        "provider": "FRED",
        "code": "APU0000703425",
        "name": title,
        "unit": "U.S. Dollars",
        "frequency": "Monthly",
        "category": "Commodities",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == f"{title} from FRED"
    assert "APU0000703425" not in query
    assert not query.startswith(("US ", "United States "))


def test_user_answerability_fred_preserves_average_price_cbsa_title():
    title = (
        "Average Price: Gasoline, Unleaded Regular "
        "(Cost per Gallon/3.785 Liters) in Philadelphia-Camden-Wilmington, "
        "PA-NJ-DE-MD (CBSA)"
    )
    row = {
        "provider": "FRED",
        "code": "APUS12B74714",
        "name": title,
        "unit": "U.S. Dollars",
        "frequency": "Monthly",
        "category": "Commodities",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == f"{title} from FRED"
    assert "APUS12B74714" not in query
    assert "average price gasoline unleaded regular cost" not in query.lower()
    assert not query.startswith(("US ", "United States "))


def test_user_answerability_fred_ignores_description_only_country_mentions():
    row = {
        "provider": "FRED",
        "code": "GASDESECW",
        "name": "PADD I (East Coast District) Diesel Sales Price",
        "description": (
            "PADD I represents the East Coast District and includes Connecticut, "
            "Florida, Georgia, North Carolina, South Carolina, and Virginia."
        ),
        "unit": "Dollars per Gallon",
        "frequency": "Weekly, Ending Monday",
        "category": "Commodities",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "US PADD I (East Coast District) Diesel Sales Price (Weekly, Ending Monday) from FRED"
    assert not query.startswith("Georgia ")


def test_user_answerability_fred_keeps_name_or_coverage_country_inference():
    row = {
        "provider": "FRED",
        "code": "CANADATEST",
        "name": "Policy Rate",
        "description": "A description without provider-native country scope.",
        "coverage": "Canada",
        "unit": "Percent",
        "frequency": "Monthly",
        "category": "Search: synthetic",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "Canada Policy Rate from FRED"


def test_user_answerability_fred_does_not_preserve_unrelated_long_title_by_default():
    title = (
        "Long FRED Catalog Title With Many Dense Qualifiers And Instrument "
        "Descriptions That Should Continue To Use Core Concept Tokens Rather "
        "Than The Full Provider Title When It Is Not An Average Price Scope"
    )
    row = {
        "provider": "FRED",
        "code": "FREDLONGTEST",
        "name": title,
        "description": "Long synthetic FRED title for a non-average-price surface.",
        "unit": "Index",
        "frequency": "Monthly",
        "category": "Search: synthetic",
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query != f"{title} from FRED"


def test_user_answerability_direct_record_asks_user_need_not_legacy_worldbank_code():
    row = {
        "id": 1,
        "provider": "WorldBank",
        "code": "NY.GDP.MKTP.CD",
        "name": "GDP (current US$)",
        "description": "GDP at purchaser's prices is the sum of gross value added.",
        "category": "World Development Indicators",
    }

    record = build_record(
        row,
        1,
        provider_count=100,
        provider_sample_count=10,
        snapshot_id="snap",
        seed=7,
        holdout_split="unit",
        dataset_tier="unit",
    )

    assert record["evaluation_target"] == CERTIFICATION_TARGET_USER_ANSWERABILITY
    assert record["query"].endswith("from World Bank")
    assert "NY.GDP.MKTP.CD" not in record["query"]
    assert record["gold"]["legacy_source_indicator_code_required"] is False
    assert record["provenance"]["legacy_catalog_replay_required"] is False


def test_user_answerability_worldbank_query_does_not_inject_arbitrary_country():
    row = {
        "id": 1,
        "provider": "WorldBank",
        "code": "CoHD_v_ss",
        "name": "Cost of vegetables relative to the starchy staples in a least-cost healthy diet",
        "description": "",
        "category": "Food Prices for Nutrition",
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "Cost of vegetables relative to the starchy staples in a least-cost healthy diet from World Bank"
    assert not query.startswith(("United States ", "Germany ", "Brazil ", "India ", "China "))


def test_user_answerability_worldbank_wdi_keeps_exact_title_without_high_risk():
    title = (
        "Learning poverty: Share of Female Children at the End-of-Primary age below "
        "minimum reading proficiency adjusted by Out-of-School Children (%)"
    )
    row = {
        "id": 1,
        "provider": "WorldBank",
        "code": "SE.LPV.PRIM.FE",
        "name": title,
        "description": "",
        "category": "World Development Indicators",
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "WorldBank",
            "query": query,
            "origin": {
                "name": title,
                "source_indicator_code": "SE.LPV.PRIM.FE",
                "source_provider": "WorldBank",
                "category": "World Development Indicators",
            },
        }
    )

    assert title in query
    assert query.endswith("from World Bank")
    assert audit["risk_level"] != "high"


def test_user_answerability_worldbank_non_wdi_keeps_exact_title_without_country_guess():
    title = (
        "Number of people pushed or further pushed below the $2.15 ($ 2017 PPP) "
        "poverty line by out-of-pocket health care expenditure"
    )
    row = {
        "id": 1,
        "provider": "WorldBank",
        "code": "SH.UHC.TOT1.TO",
        "name": title,
        "description": (
            "This indicator shows the number of people pushed further into poverty "
            "by out-of-pocket health spending."
        ),
        "category": "Health Nutrition and Population Statistics",
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "WorldBank",
            "query": query,
            "origin": {
                "name": title,
                "source_indicator_code": "SH.UHC.TOT1.TO",
                "source_provider": "WorldBank",
                "category": "Health Nutrition and Population Statistics",
            },
        }
    )

    assert query == f"{title} from World Bank"
    assert not query.startswith(("United States ", "Germany ", "Brazil ", "India ", "China "))
    assert "worldbank_countryless_single_country_query" not in audit["reasons"]
    assert audit["risk_level"] != "high"


def test_user_answerability_eurostat_keeps_exact_title_without_high_risk():
    title = (
        "Employed persons by level of difficulty to take one or two hours off at short notice, "
        "educational attainment level and professional status (2019)"
    )
    row = {
        "id": 1,
        "provider": "Eurostat",
        "code": "LFSO_19FXWT04",
        "name": title,
        "description": "",
        "category": "Eurostat Dataset",
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "Eurostat",
            "query": query,
            "origin": {
                "name": title,
                "source_indicator_code": "LFSO_19FXWT04",
                "source_provider": "Eurostat",
                "category": "Eurostat Dataset",
            },
        }
    )

    assert title in query
    assert "one or two hours off at short notice" in query
    assert "LFSO_19FXWT04" not in query
    assert query.endswith("from Eurostat")
    assert audit["risk_level"] != "high"


def test_user_answerability_eurostat_eu_coverage_does_not_inject_arbitrary_country():
    title = (
        "Relative prevalence rate of work-related health problems by severity, "
        "diagnosis group, permanency of the job, length of service in the enterprise "
        "and NACE Rev. 1.1 activity"
    )
    row = {
        "id": 1,
        "provider": "Eurostat",
        "code": "HSW_HP_SVCLN",
        "name": title,
        "description": title,
        "category": "Eurostat Dataset",
        "coverage": "EU",
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == f"{title} from Eurostat"


def test_user_answerability_statscan_keeps_exact_table_title_without_high_risk():
    title = (
        "Body mass index (BMI), by age group and sex, household population aged 18 and over "
        "excluding pregnant women, Canada, provinces, territories, health regions (January 2000 "
        "boundaries) and peer groups"
    )
    row = {
        "id": 1,
        "provider": "StatsCan",
        "code": "13100554",
        "name": title,
        "description": "",
        "category": "Statistics Canada Table",
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "StatsCan",
            "query": query,
            "origin": {
                "name": title,
                "source_indicator_code": "13100554",
                "source_provider": "StatsCan",
                "category": "Statistics Canada Table",
            },
        }
    )

    assert title in query
    assert "13100554" not in query
    assert query.endswith("from Statistics Canada")
    assert audit["risk_level"] != "high"


def test_user_answerability_oecd_keeps_exact_dataflow_title_without_high_risk():
    title = (
        "Share of young adults who are not employment nor in formal education or training (NEET), "
        "by country of birth and age at migration"
    )
    row = {
        "id": 1,
        "provider": "OECD",
        "code": "OECD_DSD_EAG_LSO_EA@DF_LSO_TRANS_MIGR",
        "name": title,
        "description": "",
        "category": "OECD Dataflow",
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "OECD",
            "query": query,
            "origin": {
                "name": title,
                "source_indicator_code": "OECD_DSD_EAG_LSO_EA@DF_LSO_TRANS_MIGR",
                "source_provider": "OECD",
                "category": "OECD Dataflow",
            },
        }
    )

    assert title in query
    assert "OECD_DSD_EAG_LSO_EA" not in query
    assert query.endswith("from OECD")
    assert audit["risk_level"] != "high"


def test_user_answerability_direct_record_preserves_unit_in_origin_and_gold_tags():
    row = {
        "id": 1,
        "provider": "IMF",
        "code": "NGDPDPC",
        "name": "GDP per capita, current prices",
        "description": "Gross domestic product per person.",
        "unit": "U.S. dollars per capita",
        "category": "WEO",
    }

    record = build_record(
        row,
        1,
        provider_count=100,
        provider_sample_count=10,
        snapshot_id="snap",
        seed=7,
        holdout_split="unit",
        dataset_tier="unit",
    )

    assert record["origin"]["unit"] == "U.S. dollars per capita"
    assert "dollars" in record["gold"]["required_concept_tags"]


def test_legacy_catalog_replay_direct_record_can_still_carry_exact_worldbank_code():
    row = {
        "id": 1,
        "provider": "WorldBank",
        "code": "NY.GDP.MKTP.CD",
        "name": "GDP (current US$)",
        "description": "GDP at purchaser's prices is the sum of gross value added.",
        "category": "World Development Indicators",
    }

    record = build_record(
        row,
        1,
        provider_count=100,
        provider_sample_count=10,
        snapshot_id="snap",
        seed=7,
        holdout_split="unit",
        dataset_tier="unit",
        certification_target=CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
    )

    assert record["evaluation_target"] == CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY
    assert record["query"] == "NY.GDP.MKTP.CD from World Bank"
    assert record["gold"]["legacy_source_indicator_code_required"] is True


def test_default_query_for_row_uses_imf_exact_code_for_public_sdmx_aggregate_trade():
    row = {
        "provider": "IMF",
        "code": "TXG_FOB_USD",
        "name": "External Trade, Exports, Goods, Value, Free on Board, US Dollars",
        "description": "",
        "category": "INDICATOR",
    }

    assert default_query_for_row(row) == "TXG_FOB_USD from IMF"
    assert imf_public_sdmx_runtime_family(row["code"], row["name"], row["category"]) == "itg_aggregate"


def test_default_query_for_row_keeps_detailed_imf_trade_family_outside_public_sdmx_surface():
    row = {
        "provider": "IMF",
        "code": "TMG_H5_84T86_CIF_USD",
        "name": (
            "External Trade, Goods, Value of Imports, By Harmonized Commodity Description "
            "and Coding Systems (HS 2017) Rev. 5, machinery, US Dollars"
        ),
        "description": "",
        "category": "INDICATOR",
    }

    query = default_query_for_row(row)

    assert query != "TMG_H5_84T86_CIF_USD from IMF"
    assert imf_public_sdmx_runtime_family(row["code"], row["name"], row["category"]) is None


def test_default_query_for_row_keeps_detailed_imf_cpi_family_outside_public_sdmx_surface():
    row = {
        "provider": "IMF",
        "code": "PCPI_ECP_01123_IX",
        "name": "Consumer Price Index Lamb and goat",
        "description": "",
        "category": "INDICATOR",
    }

    query = default_query_for_row(row)

    assert query != "PCPI_ECP_01123_IX from IMF"
    assert "lamb and goat" in query.lower()
    assert imf_public_sdmx_runtime_family(row["code"], row["name"], row["category"]) is None


def test_user_answerability_preserves_supported_imf_cpi_aggregate_titles():
    rows = [
        {
            "provider": "IMF",
            "code": "PCPI_CP_02_BY2008_IX",
            "name": (
                "Prices, Consumer Prices, All Items, By Classification of Individual "
                "Consumption According to Purpose (COICOP) 1999, Expenditure of "
                "Households, Alcoholic Beverages, Tobacco, and Narcotics, BY2008, Index"
            ),
            "description": "",
            "category": "INDICATOR",
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
        },
        {
            "provider": "IMF",
            "code": "PCPI_CP_04_BY2005_IX",
            "name": (
                "Prices, Consumer Price Index, Housing, water, electricity, gas and other "
                "fuels, COICOP, Base Year = 2005, Index"
            ),
            "description": "",
            "category": "INDICATOR",
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
        },
        {
            "provider": "IMF",
            "code": "PCPI_CP_06_BY2010_IX",
            "name": "Prices, Consumer Price Index, Health, COICOP, Base Year = 2010, Index",
            "description": "",
            "category": "INDICATOR",
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
        },
    ]

    for row in rows:
        query = common.default_query_for_row(
            row,
            certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
        )
        audit = common.audit_direct_query_shape({**row, "query": query})

        assert imf_public_sdmx_runtime_family(row["code"], row["name"], row["category"]) == "cpi_aggregate"
        assert query == f"Brazil {row['name']} from IMF"
        assert row["code"] not in query
        assert audit["risk_level"] != "high"
        assert "very_long_query" not in audit["reasons"]
        assert "methodology_dense" not in audit["reasons"]
        assert "multi_modifier_title" not in audit["reasons"]


def test_user_answerability_does_not_preserve_unsupported_imf_cpi_detail_title():
    row = {
        "provider": "IMF",
        "code": "PCPI_ECP_01123_IX",
        "name": "Prices, Consumer Price Index, Lamb and goat, COICOP, Base Year = 2010, Index",
        "description": "",
        "category": "INDICATOR",
        "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
    }

    query = common.default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert imf_public_sdmx_runtime_family(row["code"], row["name"], row["category"]) is None
    assert query != f"Brazil {row['name']} from IMF"
    assert row["code"] not in query


def test_user_answerability_selection_supportability_rows_use_exact_imf_probe() -> None:
    row = build_record(
        {
            "id": 1,
            "provider": "IMF",
            "code": "NXG_H5_XII_FOB_USD",
            "name": "National Accounts, External Sector, Exports of Goods, HS 2017 Section XII",
            "description": "",
            "category": "INDICATOR",
        },
        1,
        provider_count=100,
        provider_sample_count=10,
        snapshot_id="snap",
        seed=7,
        holdout_split="unit",
        dataset_tier="unit",
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    row["provenance"]["selection_supportability_reason"] = "imf_non_weo_public_surface_unsupported"

    common.apply_selection_supportability_probe_query(row)
    audit = audit_direct_query_shape(row)

    assert row["query"] == "NXG_H5_XII_FOB_USD from IMF"
    assert row["provenance"]["supportability_probe_query"] == "imf_exact_provider_code"
    assert "original_user_answerability_query" in row["provenance"]
    assert row["provenance"]["original_user_answerability_query"] != row["query"]
    assert audit["risk_level"] != "high"
    assert "multi_modifier_title" not in audit["reasons"]


def test_default_query_for_row_does_not_treat_are_verb_as_country() -> None:
    row = {
        "provider": "WorldBank",
        "code": "UIS.MS.56.F",
        "name": "Total inbound internationally mobile students, female (number)",
        "description": (
            "Total number of female students who have crossed a national or territorial border "
            "for the purpose of education and are now enrolled outside their country of origin."
        ),
    }

    assert detect_single_country_from_text("Are Total inbound internationally mobile students") is None
    assert not default_query_for_row(row).startswith("Are ")


def test_default_query_for_row_uses_slug_for_short_coingecko_symbols():
    row = {
        "provider": "CoinGecko",
        "code": "apollox-2",
        "name": "APX",
        "description": "",
    }

    query = default_query_for_row(row)

    assert "price" in query.lower()
    assert "apollox" in query.lower()


def test_default_query_for_row_prefers_slug_for_complex_coingecko_assets():
    row = {
        "provider": "CoinGecko",
        "code": "matic-aave-usdc",
        "name": "Matic Aave Interest Bearing USDC",
        "description": "",
    }

    query = default_query_for_row(row)

    assert "matic-aave-usdc" in query
    assert query.lower().endswith("from coingecko")


def test_user_answerability_query_uses_provider_native_symbol_for_symbol_only_coingecko_asset():
    row = {
        "provider": "CoinGecko",
        "code": "_",
        "name": "༼ つ ◕_◕ ༽つ",
        "description": "",
        "synonyms": "gib",
        "raw_metadata": json.dumps({"id": "_", "symbol": "gib", "name": "༼ つ ◕_◕ ༽つ"}),
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "gib cryptocurrency price from CoinGecko"
    assert "༼" not in query


def test_user_answerability_query_preserves_numeric_coingecko_slug_when_humanized_generic():
    row = {
        "provider": "CoinGecko",
        "code": "01-token",
        "name": "01",
        "description": "",
        "raw_metadata": json.dumps({"id": "01-token", "symbol": "01", "name": "01"}),
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "01-token cryptocurrency price from CoinGecko"
    assert "Token cryptocurrency price" not in query


def test_user_answerability_query_preserves_short_coingecko_acronym_slug():
    row = {
        "provider": "CoinGecko",
        "code": "3a-lending-protocol",
        "name": "3A",
        "description": "",
        "synonyms": "a3a",
        "raw_metadata": json.dumps({"id": "3a-lending-protocol", "symbol": "a3a", "name": "3A"}),
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "3a-lending-protocol cryptocurrency price from CoinGecko"
    assert "3A Lending Protocol" not in query


def test_user_answerability_query_preserves_uppercase_coingecko_acronym_slug():
    row = {
        "provider": "CoinGecko",
        "code": "aag-ventures",
        "name": "AAG",
        "description": "",
        "synonyms": "aag",
        "raw_metadata": json.dumps({"id": "aag-ventures", "symbol": "aag", "name": "AAG"}),
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "aag-ventures cryptocurrency price from CoinGecko"
    assert "Aag Ventures" not in query


def test_user_answerability_query_preserves_short_coingecko_slug_with_suffix_qualifier():
    row = {
        "provider": "CoinGecko",
        "code": "aevo-exchange",
        "name": "Aevo",
        "description": "",
        "synonyms": "aevo",
        "raw_metadata": json.dumps({"id": "aevo-exchange", "symbol": "aevo", "name": "Aevo"}),
    }

    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert query == "aevo-exchange cryptocurrency price from CoinGecko"
    assert "Aevo Exchange" not in query

    token_suffix_row = {
        "provider": "CoinGecko",
        "code": "aga-token",
        "name": "AGA",
        "description": "",
        "synonyms": "aga",
        "raw_metadata": json.dumps({"id": "aga-token", "symbol": "aga", "name": "AGA"}),
    }

    token_suffix_query = default_query_for_row(
        token_suffix_row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )

    assert token_suffix_query == "aga-token cryptocurrency price from CoinGecko"
    assert "Aga Token" not in token_suffix_query


def test_default_query_for_row_makes_exchange_rate_provider_explicit():
    row = {
        "provider": "ExchangeRate",
        "code": "USDGBP",
        "name": "USD to GBP",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query.lower().endswith("exchange rate from exchangerate")
    assert " to " in query


def test_default_query_for_row_naturalizes_comtrade_codes_into_exports_query():
    row = {
        "provider": "Comtrade",
        "code": "72",
        "name": "72 - Iron and steel",
        "description": "",
    }

    query = default_query_for_row(row)

    assert "exports of HS72 from Comtrade" in query


def test_user_answerability_comtrade_query_uses_high_coverage_reporter_pool():
    row = {
        "provider": "Comtrade",
        "provider_stratum": "Comtrade",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "Comtrade",
            "source_indicator_code": "262020",
            "name": (
                "262020 - Ash and residues; (not from the manufacture of iron "
                "or steel), containing mainly lead"
            ),
            "description": (
                "HS Code 262020: 262020 - Ash and residues; (not from the "
                "manufacture of iron or steel), containing mainly lead"
            ),
            "category": "HS Subheading",
        },
    }

    query = default_query_for_row(row, certification_target="user_answerability")

    assert query.startswith("India exports of HS 262020 ")
    assert query.endswith(" from Comtrade")


def test_user_answerability_comtrade_query_preserves_hs_code_for_embedded_heading_numbers():
    row = {
        "provider": "Comtrade",
        "provider_stratum": "Comtrade",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "Comtrade",
            "source_indicator_code": "820600",
            "name": (
                "820600 - Tools, hand; two or more of heading no. 8202 to "
                "8205, put up in sets for retail sale"
            ),
            "description": (
                "HS Code 820600: 820600 - Tools, hand; two or more of heading "
                "no. 8202 to 8205, put up in sets for retail sale"
            ),
            "category": "HS Subheading",
        },
    }

    query = default_query_for_row(row, certification_target="user_answerability")

    assert "exports of HS 820600 Tools, hand" in query
    assert "HS 8202" not in query
    assert query.endswith(" from Comtrade")


def test_audit_direct_query_shape_keeps_exact_comtrade_hs_code_query_low_risk():
    query = (
        "Japan exports of HS 820600 Tools, hand; two or more of heading no. "
        "8202 to 8205, put up in sets for retail sale from Comtrade"
    )

    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "Comtrade",
            "query": query,
            "origin": {
                "source_provider": "Comtrade",
                "source_indicator_code": "820600",
                "name": (
                    "820600 - Tools, hand; two or more of heading no. 8202 to "
                    "8205, put up in sets for retail sale"
                ),
            },
        }
    )

    assert audit["risk_level"] == "low"
    assert "very_long_query" not in audit["reasons"]


def test_default_query_for_row_carries_statscan_product_id_with_title_evidence():
    row = {
        "provider": "StatsCan",
        "code": "24100026",
        "name": "Travel price index, quarterly",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "24100026 Travel price index quarterly from StatsCan"


def test_user_answerability_oecd_query_uses_provider_native_default_scope():
    row = {
        "provider": "OECD",
        "provider_stratum": "OECD",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "OECD",
            "source_indicator_code": "OECD_DSD_EAG_LSO_EA@DF_LSO_EARN_REL_BEL",
            "name": (
                "Earnings of workers relative to the earnings of workers with "
                "below upper secondary educational attainment, by age group, "
                "gender and educational attainment level"
            ),
            "description": "This dataset contains data on relative earnings of workers.",
        },
    }

    query = default_query_for_row(row, certification_target="user_answerability")

    assert query.endswith("from OECD")
    assert query.startswith("Earnings of workers relative to")
    assert not query.startswith(("United States ", "Germany ", "Canada ", "Japan "))


def test_user_answerability_oecd_distribution_rows_do_not_inject_arbitrary_country():
    row = {
        "provider": "OECD",
        "provider_stratum": "OECD",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "OECD",
            "source_indicator_code": "OECD_DSD_EGDNA_SOCDEM@DF_SOCIODEMOGRAPHIC_AGE",
            "name": "Population in the National Accounts: distribution of people in income quintiles by age",
            "description": "This dataset presents the number of individuals belonging to households in each quintile broken down by age group.",
        },
    }

    query = default_query_for_row(row, certification_target="user_answerability")

    assert query == (
        "Population in the National Accounts: distribution of people "
        "in income quintiles by age from OECD"
    )


def test_default_query_for_row_keeps_statscan_dimension_titles_natural():
    row = {
        "provider": "StatsCan",
        "code": "17100147",
        "name": "First names at birth by sex at birth, selected indicators",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "Canada selected indicators First names at birth by sex at birth from Statistics Canada"
    assert not query.startswith("17100147")


def test_default_query_for_row_carries_statscan_historical_product_id_with_date_range():
    row = {
        "provider": "StatsCan",
        "code": "14100297",
        "name": "Labour force characteristics by occupation, annual, 1987 to 2018, inactive",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "14100297 Labour force characteristics by occupation annual 1987 to 2018 inactive from StatsCan"


def test_audit_direct_query_shape_does_not_block_exact_statscan_product_title():
    audit = audit_direct_query_shape(
        {
            "provider": "StatsCan",
            "query": (
                "13100949 Health indicator statistics for children and youth aged 1 to 17 years "
                "parent reported by household income quintile and highest level of parental education from StatsCan"
            ),
            "origin": {
                "name": (
                    "Health indicator statistics for children and youth aged 1 to 17 years, "
                    "parent reported by household income quintile and highest level of parental education"
                ),
                "source_indicator_code": "13100949",
                "source_provider": "StatsCan",
            },
        }
    )

    assert audit["risk_level"] != "high"
    assert "education_subgroup_slice" not in audit["reasons"]
    assert "socioeconomic_slice" not in audit["reasons"]


def test_audit_direct_query_shape_flags_opaque_acronym_queries():
    row = {
        "query": "Germany NAAG",
        "origin": {"name": "NAAG"},
    }

    audit = audit_direct_query_shape(row)

    assert audit["risk_level"] == "high"
    assert "opaque_acronym_query" in audit["reasons"]


def test_default_query_for_row_avoids_prefixing_country_when_title_already_has_scope():
    row = {
        "provider": "FRED",
        "code": "DDOI02JPA156NWDB",
        "name": "Bank Deposits to GDP for Japan",
        "description": "Bank deposits as a share of GDP for Japan.",
    }

    query = default_query_for_row(row)

    assert query == "DDOI02JPA156NWDB from FRED"


def test_default_query_for_row_uses_exact_fred_code_for_long_punctuated_titles():
    row = {
        "provider": "FRED",
        "code": "BOGZ1FL363030005A",
        "name": "General Government; Total Time and Savings Deposits; Asset, Level",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "BOGZ1FL363030005A from FRED"


def test_default_query_for_row_prefers_country_in_origin_name_over_provider_default() -> None:
    row = {
        "provider": "BIS",
        "code": "CPTEST",
        "name": "Consumer Price Indices (CPIs, HICPs), COICOP 1999: Consumer Price Index: Total for Luxembourg",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query.lower().startswith("luxembourg ")
    assert "united states" not in query.lower()


def test_default_query_for_row_uses_exact_oecd_dataflow_with_country_scope() -> None:
    row = {
        "provider": "OECD",
        "code": "OECD_DSD_NASEC10@DF_TABLE14_REV",
        "name": "Annual non-financial accounts by institutional sector (Revenue)",
        "coverage": "Canada",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "Canada OECD_DSD_NASEC10@DF_TABLE14_REV from OECD"


def test_default_query_for_row_strips_eurostat_extraction_suffix_for_exact_dataset() -> None:
    row = {
        "provider": "Eurostat",
        "code": "SBS_SC_1B_SE_R2$DV_664",
        "name": "Services by employment size class (NACE Rev. 2, H-N, S95) (2005-2020)",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "SBS_SC_1B_SE_R2 from Eurostat"


def test_detect_single_country_from_text_extracts_named_country() -> None:
    assert detect_single_country_from_text("Consumer Price Index for Luxembourg") == "Luxembourg"


def test_detect_single_country_from_text_ignores_ambiguous_short_aliases() -> None:
    assert detect_single_country_from_text("household composition and degree of urbanisation") is None


def test_detect_single_country_from_text_ignores_nor_conjunction() -> None:
    assert detect_single_country_from_text("Spices; coriander seeds, neither crushed nor ground") is None


def test_default_query_for_row_does_not_treat_nor_conjunction_as_country() -> None:
    row = {
        "provider": "Comtrade",
        "code": "090921",
        "name": "Spices; coriander seeds, neither crushed nor ground",
        "description": "",
    }

    assert not default_query_for_row(row).startswith("Nor ")


def test_default_query_for_row_does_not_treat_comtrade_commodity_country_word_as_reporter() -> None:
    row = {
        "provider": "Comtrade",
        "code": "0105",
        "name": "0105 - Poultry; live, fowls of the species Gallus domesticus, ducks, geese, turkeys and guinea fowls",
        "description": "",
    }

    query = default_query_for_row(row, certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY)

    assert not query.startswith("Guinea exports")
    assert "exports of HS 0105 Poultry" in query
    assert query.endswith(" from Comtrade")


def test_detect_single_country_from_text_ignores_ambiguous_america_region_alias() -> None:
    assert detect_single_country_from_text("Latin America and the Caribbean informal employment") is None


def test_detect_single_country_from_text_ignores_currency_and_preposition_aliases() -> None:
    assert detect_single_country_from_text("Current account balance U.S. dollars") is None
    assert detect_single_country_from_text("Industry value added current US$") is None
    assert detect_single_country_from_text("Cost per training hour") is None


def test_default_query_for_row_does_not_treat_current_us_dollar_unit_as_country_scope() -> None:
    row = {
        "provider": "WorldBank",
        "code": "NV.IND.TOTL.CD",
        "name": "Industry (including construction), value added (current US$)",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "NV.IND.TOTL.CD from World Bank"
    assert query.lower().endswith("from world bank")
    assert not query.startswith(("United States ", "Germany ", "Brazil ", "India ", "Nigeria ", "China "))


def test_default_query_for_row_preserves_mixed_case_worldbank_code() -> None:
    row = {
        "provider": "WorldBank",
        "code": "CoHD_v_ss",
        "name": "Cost of vegetables relative to the starchy staples in a least-cost healthy diet",
        "description": "",
    }

    assert default_query_for_row(row) == "CoHD_v_ss from World Bank"


def test_default_query_for_row_does_not_treat_metric_ton_as_tonga_scope() -> None:
    row = {
        "provider": "WorldBank",
        "code": "NY.ADJ.DCO2.GN.ZS",
        "name": "Adjusted savings: carbon dioxide damage (% of GNI)",
        "description": (
            "Cost of damage due to carbon dioxide emissions from fossil fuel use "
            "estimated at a dollar value per ton of CO2 emitted."
        ),
    }

    query = default_query_for_row(row)

    assert not query.startswith("Ton ")
    assert query == "NY.ADJ.DCO2.GN.ZS from World Bank"


def test_default_query_for_row_does_not_treat_worldwide_source_family_as_country_scope() -> None:
    row = {
        "provider": "WorldBank",
        "code": "CC.EST",
        "name": "Control of Corruption: Estimate",
        "description": (
            "The Worldwide Governance Indicators (WGI) are a research dataset summarizing "
            "views on the quality of governance."
        ),
    }

    query = default_query_for_row(row)

    assert query != "Worldwide Control of Corruption: Estimate from World Bank"
    assert query == "CC.EST from World Bank"


def test_default_query_for_row_keeps_intrinsic_worldbank_country_scope() -> None:
    row = {
        "provider": "WorldBank",
        "code": "SP.POP.TOTL",
        "name": "Population, total",
        "coverage": "Japan",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query == "Japan SP.POP.TOTL from World Bank"


def test_default_query_for_row_enriches_generic_oecd_title_from_description():
    row = {
        "provider": "OECD",
        "code": "DSD_HEALTH_EMP_REAC@DF_PHYS",
        "name": "Physicians",
        "description": (
            "<p>This dataset provides data on the number of physicians by "
            "<strong>status</strong> (ie. practising physicians, professionally "
            "active physicians).</p>"
        ),
    }

    query = default_query_for_row(row)

    assert query == "Germany DSD_HEALTH_EMP_REAC@DF_PHYS from OECD"


def test_default_query_for_row_does_not_prepend_country_when_imf_title_already_names_one():
    row = {
        "provider": "IMF",
        "code": "NER_CBS_PSD_XDC",
        "name": "Nigeria Definition, Central Bank Survey: Private Sector Deposits, National Currency",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query.lower().startswith("nigeria definition")
    assert "germany " not in query.lower()


def test_default_query_for_row_uses_imf_code_for_high_modifier_titles():
    row = {
        "provider": "IMF",
        "code": "NER_CBS_PSD_XDC",
        "name": "Nigeria Definition, Central Bank Survey: Private Sector Deposits, National Currency",
        "description": "",
    }

    query = default_query_for_row(row)

    assert "nigeria definition" in query.lower()
    assert query.lower().endswith("from imf")


def test_default_query_for_row_adds_explicit_provider_for_eurostat_queries():
    row = {
        "provider": "Eurostat",
        "code": "migr_immi1ctz",
        "name": "Immigration",
        "description": "Long-term immigration flows",
    }

    query = default_query_for_row(row)

    assert query.lower().endswith("from eurostat")


def test_audit_direct_query_shape_flags_country_scope_conflict():
    audit = audit_direct_query_shape(
        {
            "query": "US Bank Deposits to GDP for Japan",
            "origin": {"name": "Bank Deposits to GDP for Japan"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "country_scope_conflict" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_non_api_source_families() -> None:
    gdld = audit_direct_query_shape(
        {
            "provider_stratum": "WorldBank",
            "query": "China Annual wage for skilled female workers in US$ from World Bank",
            "origin": {
                "source_provider": "WorldBank",
                "source_indicator_code": "w_F_skl",
                "name": "Annual wage for skilled female workers in US$",
                "category": "Gender Disaggregated Labor Database (GDLD)",
            },
        }
    )
    g20 = audit_direct_query_shape(
        {
            "provider_stratum": "WorldBank",
            "query": "Germany SME loan accounts from World Bank",
            "origin": {
                "source_provider": "WorldBank",
                "source_indicator_code": "i_loan_acc_A1_sme_perNFC",
                "name": "SME loan accounts",
                "category": "G20 Financial Inclusion Indicators",
            },
        }
    )

    assert gdld["risk_level"] == "high"
    assert g20["risk_level"] == "high"
    assert "worldbank_specialized_source_family" in gdld["reasons"]
    assert "worldbank_specialized_source_family" in g20["reasons"]


def test_audit_direct_query_shape_keeps_exact_worldbank_code_probe_low_risk() -> None:
    audit = audit_direct_query_shape(
        {
            "provider_stratum": "WorldBank",
            "query": "CoHD_v_ss from World Bank",
            "origin": {
                "source_provider": "WorldBank",
                "source_indicator_code": "CoHD_v_ss",
                "name": "Cost of vegetables relative to the starchy staples in a least-cost healthy diet",
                "category": "Food Prices for Nutrition",
                "raw_metadata": '{"source": {"id": "88", "value": "Food Prices for Nutrition"}}',
            },
        }
    )

    assert audit["risk_level"] == "low"
    assert not any(reason.startswith("worldbank_") for reason in audit["reasons"])


def test_audit_direct_query_shape_flags_countryless_non_exact_worldbank_prompt() -> None:
    audit = audit_direct_query_shape(
        {
            "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
            "provider_stratum": "WorldBank",
            "scope_family": "single_country",
            "query": "number people pushed further 2017 ppp from World Bank",
            "origin": {
                "source_provider": "WorldBank",
                "source_indicator_code": "SH.UHC.TOT1.TO",
                "name": (
                    "Number of people pushed or further pushed below the $2.15 "
                    "($ 2017 PPP) poverty line by out-of-pocket health care expenditure"
                ),
                "category": "Health Nutrition and Population Statistics",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_countryless_single_country_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_country_role_availability() -> None:
    cpia = audit_direct_query_shape(
        {
            "provider_stratum": "WorldBank",
            "query": "Japan CPIA structural policies cluster average from World Bank",
            "origin": {
                "source_provider": "WorldBank",
                "source_indicator_code": "IQ.CPA.STRC.XQ",
                "name": "CPIA structural policies cluster average (1=low to 6=high)",
                "category": "World Development Indicators",
            },
        }
    )
    oda = audit_direct_query_shape(
        {
            "provider_stratum": "WorldBank",
            "query": "India Net ODA provided to the least developed countries from World Bank",
            "origin": {
                "source_provider": "WorldBank",
                "source_indicator_code": "DC.ODA.TLDC.CD",
                "name": "Net ODA provided, to the least developed countries (current US$)",
                "category": "World Development Indicators",
            },
        }
    )

    assert cpia["risk_level"] == "high"
    assert oda["risk_level"] == "high"
    assert "worldbank_country_availability_surface" in cpia["reasons"]
    assert "worldbank_country_availability_surface" in oda["reasons"]


def test_audit_direct_query_shape_flags_micro_demographic_slices():
    audit = audit_direct_query_shape(
        {
            "query": "Brazil female population age 7 from World Bank",
            "origin": {"name": "Population, age 7, female"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "micro_demographic_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_men_age_micro_demographic_slices():
    audit = audit_direct_query_shape(
        {
            "query": "China Received private sector wages: in cash only men (% age 15+) from World Bank",
            "origin": {"name": "Received private sector wages: in cash only, men (% age 15+)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "micro_demographic_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_education_subgroup_slices():
    audit = audit_direct_query_shape(
        {
            "query": "Germany age 30-34 total Barro-Lee: Average years of secondary schooling from World Bank",
            "origin": {"name": "Barro-Lee: Average years of secondary schooling, age 30-34, total"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "education_subgroup_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_definition_survey_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Nigeria Definition Central Bank Survey Private Sector Deposits from IMF",
            "origin": {"name": "Nigeria Definition, Central Bank Survey: Private Sector Deposits, National Currency"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "definition_survey_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_binary_policy_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Brazil Sons and daughters have equal rights to inherit assets from their parents (1=yes; 0=no) from World Bank",
            "origin": {"name": "Sons and daughters have equal rights to inherit assets from their parents (1=yes; 0=no)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_binary_policy_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_commercial_bank_policy_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "033_Are commercial banks permitted to distribute insurance?_#VGDA_07 from World Bank",
            "origin": {
                "name": "033_Are commercial banks permitted to distribute insurance?_#VGDA_07",
                "category": "Global Financial Inclusion and Consumer Protection Survey",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_binary_policy_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_country_partnership_strategy_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India LFPR per 1000 for ages 15+ Rural & Urban: Level Secondary from World Bank",
            "origin": {
                "name": "LFPR per 1000 for ages 15+ Rural & Urban: Level Secondary",
                "category": "Country Partnership Strategy for India (FY2013 - 17)",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_niche_catalog_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_dhs_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India DHS Primary completion rate Urban from World Bank",
            "origin": {
                "name": "DHS: Primary completion rate. Urban",
                "source_indicator_code": "HH.DHS.PCR.U",
                "category": "Education Statistics",
                "sourceOrganization": "Demographic and Health Surveys",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_education_statistics_inventory_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India proportion students the end lower secondary from World Bank",
            "origin": {
                "name": "Proportion of students at the end of lower secondary education achieving minimum proficiency",
                "source_indicator_code": "UIS.MATH.LOWERSEC.LPIA",
                "category": "Education Statistics",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_niche_catalog_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_dac_donor_role_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "Net bilateral aid flows from DAC donors Estonia (current US$) from World Bank",
            "origin": {
                "name": "Net bilateral aid flows from DAC donors, Estonia (current US$)",
                "source_indicator_code": "DC.DAC.ESTL.CD",
                "category": "World Development Indicators",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_specialized_source_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_archived_wgi_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India Control of Corruption: Estimate from World Bank",
            "origin": {
                "name": "Control of Corruption: Estimate",
                "source_indicator_code": "CC.EST",
                "category": "World Development Indicators",
                "sourceOrganization": "Worldwide Governance Indicators, World Bank (WB)",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_specialized_source_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_national_learning_goals_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India National Learning Goals Average score for incentives from World Bank",
            "origin": {
                "name": "(National Learning Goals) Average score for incentives",
                "source_indicator_code": "SE.PRM.BNLG.4",
                "category": "Education Statistics",
                "sourceOrganization": "Global Education Policy Dashboard",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_assessment_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_piaac_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India PIAAC: Mean Young Adult Literacy Proficiency. Male from World Bank",
            "origin": {
                "name": "PIAAC: Mean Young Adult Literacy Proficiency. Male",
                "source_indicator_code": "LO.PIAAC.LIT.YOU.MA",
                "category": "Education Statistics",
                "sourceOrganization": "OECD Programme for the International Assessment of Adult Competencies (PIAAC)",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_assessment_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_salaries_share_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India World Bank Salaries' share of tertiary recurrent expenditures percent from World Bank",
            "origin": {
                "name": "World Bank: Salaries' share of tertiary recurrent expenditures (%)",
                "source_indicator_code": "PER.TER.SAL.SHARE.RCT",
                "category": "Education Statistics",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_finance_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_adjusted_wealth_parity_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India primary education rural male Completion rate adjusted wealth parity index WPIA from World Bank",
            "origin": {
                "name": "Completion rate, primary education, rural, male, adjusted wealth parity index (WPIA)",
                "source_indicator_code": "UIS.CR.1.RUR.M.WPIA",
                "category": "Education Statistics",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_demographic_literacy_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_publication_table_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Japan Africa's Development Dynamics (AfDD) Table 36 - Employment by business activity and skill level from OECD",
            "origin": {"name": "Africa's Development Dynamics (AfDD) Table 36 - Employment by business activity and skill level"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_informality_nonproduction_family():
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "America Hourly earnings ratio of formal to informal employees from OECD",
            "origin": {
                "name": "Hourly earnings ratio of formal to informal employees",
                "description": (
                    "The OECD Key Indicators of Informality based on Individuals and their Households "
                    "database provides comparable indicators and harmonised data on informal employment."
                ),
                "raw_metadata": (
                    '{"annotations":[{"type":"NonProductionDataflow","text":"true"}]}'
                ),
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]
    assert "oecd_non_production_dataflow" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_sdg_goal_dataflows() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Global Sustainable Development Goal 03 - Good health and well-being from OECD",
            "origin": {
                "name": "Sustainable Development Goal 03 - Good health and well-being",
                "source_indicator_code": "OECD_DSD_SDG@DF_SDG_G_3",
                "category": "OECD Dataflow",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_trade_enterprise_characteristics_dataflows() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Canada Trade in goods by enterprise characteristics by activity sectors from OECD",
            "origin": {
                "name": "Trade in goods by enterprise characteristics by activity sectors",
                "source_indicator_code": "OECD_DSD_TEC_ISIC4@DF_TEC09",
                "category": "OECD Dataflow",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_instruction_time_and_childcare_dataflows() -> None:
    for query, code in [
        ("Germany Instruction time in compulsory general education by age from OECD", "OECD_DSD_EAG_IT@DF_EAG_IT_AGE"),
        ("United States Instruction time per subject by level of education from OECD", "OECD_DSD_EAG_IT@DF_EAG_IT_SUBJ_ISCED"),
        ("Japan Net childcare cost for parents using centre-based childcare from OECD", "OECD_DSD_TAXBEN_NCC@DF_NCC"),
        ("Germany Number of students and repeaters by grade and level of education from OECD", "OECD_DSD_EAG_UOE_NON_FIN_STUD@DF_UOE_NF_RAW_RPTR"),
        ("Japan Number of mobile students enrolled and graduated by country of origin from OECD", "OECD_DSD_EAG_UOE_NON_FIN_STUD@DF_UOE_NF_RAW_MOB"),
        ("Korea Revenue Statistics in Asia and Pacific - Reference series from OECD", "OECD_DSD_REF_ASAP@DF_REFSERIES_ASAP"),
        ("Canada Teachers' actual salaries relative to workers' earnings from OECD", "OECD_DSD_EAG_EARNINGS@DF_EAG_EARNINGS"),
        ("Japan domestic concept Quarterly employment by institutional sector from OECD", "OECD.SDD.NAD,DSD_NASEC10@DF_QNA_EXPENDITURE_INST"),
        ("Japan National and regional house price indices from OECD", "OECD.SDD.TPS,DSD_RHPI@DF_RHPI"),
        ("Canada Number of national tertiary students enrolled abroad from OECD", "OECD_DSD_EAG_UOE_MOB@DF_MOB"),
        ("Australia non-consolidated Annual Financial Accounts (flows) from OECD", "OECD.SDD.NAD,DSD_NAFIN@DF_FINACCOUNT_FLOWS"),
    ]:
        audit = audit_direct_query_shape(
            {
                "provider": "OECD",
                "query": query,
                "origin": {
                    "name": query.removesuffix(" from OECD"),
                    "source_indicator_code": code,
                    "category": "OECD Dataflow",
                },
            }
        )

        assert audit["risk_level"] == "high"
        assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_keeps_exact_oecd_dataflow_probe_low_risk() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Canada OECD_DSD_EAG_UOE_NON_FIN_STUD@DF_UOE_NF_MEAN_AGE from OECD",
            "origin": {
                "name": "Mean age of enrolled students new entrants and graduates",
                "source_indicator_code": "OECD_DSD_EAG_UOE_NON_FIN_STUD@DF_UOE_NF_MEAN_AGE",
                "category": "OECD Dataflow",
            },
        }
    )

    assert audit["risk_level"] == "low"
    assert "country_scope_conflict" not in audit["reasons"]
    assert "oecd_low_viability_family" not in audit["reasons"]


def test_audit_direct_query_shape_keeps_oecd_nonproduction_alone_below_high_risk():
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Korea tax revenues in Revenue Statistics in Asia and the Pacific from OECD",
            "origin": {
                "name": "Korea - tax revenues in Revenue Statistics in Asia and the Pacific",
                "raw_metadata": (
                    '{"annotations":[{"type":"NonProductionDataflow","text":"true"}]}'
                ),
            },
        }
    )

    assert "oecd_non_production_dataflow" in audit["reasons"]
    assert "oecd_low_viability_family" not in audit["reasons"]
    assert audit["risk_level"] == "medium"


def test_audit_direct_query_shape_flags_eurostat_vine_breakdown_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Area under wine-grape vine varieties broken down by vine variety and by age of the vines - Cyprus from Eurostat",
            "origin": {"name": "Area under wine-grape vine varieties broken down by vine variety and by age of the vines - Cyprus"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_agri_breakdown_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_rejected_next200_dataflows() -> None:
    cases = [
        (
            "Spain Time spent in the main activity by sex and household composition from Eurostat",
            "TUS_20HHSTATUS",
            "eurostat_cross_tab_query",
        ),
        (
            "France by direction EU level - gross weight of goods handled in main ports from Eurostat",
            "MAR_QG_QM_EWHD",
            "eurostat_transport_port_query",
        ),
        (
            "Italy Physical supply and use of wood in the rough over bark from Eurostat",
            "FOR_EPSUW",
            "eurostat_forestry_material_flow_query",
        ),
        (
            "Germany Former daily tobacco smokers by sex from Eurostat",
            "HLTH_EHIS_SK2I",
            "eurostat_cross_tab_query",
        ),
        (
            "Spain Infant deaths occurring in EU by cause and age from Eurostat",
            "HLTH_CD_INFOEU",
            "eurostat_cross_tab_query",
        ),
        (
            "Germany Deaths by week, sex, 5-year age group and NUTS2 region from Eurostat",
            "DEMO_R_MWK2_05",
            "eurostat_cross_tab_query",
        ),
        (
            "Italy Infant deaths occurring in the EU by cause and age from Eurostat",
            "HLTH_CD_INFOEU",
            "eurostat_cross_tab_query",
        ),
        (
            "Gross weight of goods transported to/from main ports - Portugal from Eurostat",
            "MAR_GO_AM_PT",
            "eurostat_transport_port_query",
        ),
        (
            "Italy Mean hourly earnings by sex age and economic activity (2022) from Eurostat",
            "EARN_SES22_13",
            "eurostat_cross_tab_query",
        ),
        (
            "Italy Early leavers from education and training by sex and NUTS 1 region from Eurostat",
            "EDAT_LFSE_16",
            "eurostat_cross_tab_query",
        ),
        (
            "Germany sex occupation Mean annual earnings by size of the enterprise from Eurostat",
            "EARN_SES_SIZE",
            "eurostat_cross_tab_query",
        ),
    ]

    for query, code, reason in cases:
        audit = audit_direct_query_shape(
            {
                "provider": "Eurostat",
                "query": query,
                "origin": {
                    "name": query.removesuffix(" from Eurostat"),
                    "source_indicator_code": code,
                    "category": "Eurostat Dataset",
                },
            }
        )

        assert audit["risk_level"] == "high"
        assert reason in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_debt_schedule_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Japan Other Sectors Principal External Debt Debt-service Payment schedule More than 9 and up to 12 months from IMF",
            "origin": {"name": "External Debt, Other Sectors, Debt-service Payment schedule, More than 9 and up to 12 months, Principal"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_does_not_use_query_text_as_imf_supportability_authority() -> None:
    from scripts.validation.run_certification import unsupported_direct_surface_reason

    row = {
        "provider": "IMF",
        "query": (
            "Brazil Food Consumer Prices Food and Non-alcoholic Beverages "
            "Food at Home Fruits and Vegetables from IMF"
        ),
        "type": "direct",
    }
    audit = audit_direct_query_shape(row)

    assert "imf_query_only_public_surface_family" not in audit["reasons"]
    assert unsupported_direct_surface_reason(row, audit) is None


def test_unsupported_direct_surface_reason_uses_imf_provider_native_code_metadata() -> None:
    from scripts.validation.run_certification import unsupported_direct_surface_reason

    rows = [
        (
            "PMP_ISIC3_C_IX",
            "Import Price Index, Mining and quarrying, Index",
        ),
        (
            "PCPIFFHF_IX",
            "Food Consumer Prices, Food at Home, Fruits and Vegetables, Index",
        ),
        (
            "GGDD_FY_USD",
            "General Government, Total debt, Fiscal Year, US Dollar",
        ),
    ]

    for code, name in rows:
        row = {
            "provider": "IMF",
            "query": f"Brazil {code} from IMF",
            "type": "direct",
            "origin": {
                "source_provider": "IMF",
                "source_indicator_code": code,
                "name": name,
                "category": "INDICATOR",
            },
        }
        audit = audit_direct_query_shape(row)

        assert "imf_query_only_public_surface_family" not in audit["reasons"]
        assert unsupported_direct_surface_reason(row, audit) == "imf_non_weo_public_surface_unsupported"


def test_user_answerability_does_not_preblock_on_legacy_imf_code_metadata() -> None:
    from scripts.validation.run_certification import unsupported_direct_surface_reason

    row = {
        "provider": "IMF",
        "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
        "query": "Brazil producer price index from IMF",
        "type": "direct",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "PMP_ISIC3_C_IX",
            "name": "Import Price Index, Mining and quarrying, Index",
            "category": "INDICATOR",
        },
    }
    audit = audit_direct_query_shape(row)

    assert unsupported_direct_surface_reason(row, audit) is None


def test_user_answerability_audit_does_not_use_legacy_imf_code_as_inventory_risk() -> None:
    row = {
        "provider": "IMF",
        "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
        "query": "Brazil labor force participation from IMF",
        "type": "direct",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "LE_PLP_RATE",
            "name": "Labor Force Participation Rate",
            "category": "INDICATOR",
        },
    }

    audit = audit_direct_query_shape(row)

    assert "imf_low_viability_family" not in audit["reasons"]


def test_audit_direct_query_shape_keeps_broad_imf_ppi_query_executable() -> None:
    row = {"provider": "IMF", "query": "Brazil Producer Price Index from IMF", "type": "direct"}

    audit = audit_direct_query_shape(row)

    assert "imf_query_only_public_surface_family" not in audit["reasons"]


def test_audit_direct_query_shape_keeps_broad_imf_trade_balance_query_executable() -> None:
    row = {"provider": "IMF", "query": "Japan Trade Balance (% of GDP) from IMF", "type": "direct"}

    audit = audit_direct_query_shape(row)

    assert "imf_query_only_public_surface_family" not in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_code_only_unsupported_trade_prefixes() -> None:
    for code in ["TMG_H5_80_EUR", "TX_H5_60_USD", "TM_H5_18_USD", "TXG_SI3_USD", "TMG_SI5_USD"]:
        audit = audit_direct_query_shape(
            {
                "provider": "IMF",
                "query": "Germany aggregate trade indicator from IMF",
                "origin": {
                    "name": "Aggregate trade indicator",
                    "source_indicator_code": code,
                },
            }
        )

        assert audit["risk_level"] == "high"
        assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_labour_market_family_by_code() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Rate Labour Market Employment to Population Ratio from IMF",
            "origin": {
                "name": "Labour Market, Employment to Population Ratio, Rate",
                "source_indicator_code": "LE_PLP_RATE",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_labor_markets_plural_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": (
                "Germany Employment Wages Labor Markets Wholesale and retail trade; "
                "repair of motor vehicles and motorcycles from IMF"
            ),
            "origin": {
                "name": (
                    "Labor Markets, Employment, Wages, By International Standard Industrial "
                    "Classification of All Economic Activities (ISIC) Rev. 4, Wholesale and "
                    "retail trade; repair of motor vehicles and motorcycles, US Dollars"
                ),
                "source_indicator_code": "LEW_ISIC4_G_USD",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_non_datamapper_indicator_category() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": (
                "Germany Current Account Services Transportation Air Transport Other "
                "Net Balance of Payments Goods and Services from IMF"
            ),
            "origin": {
                "name": (
                    "Balance of Payments, Current Account, Goods and Services, Services, "
                    "Transport, Air Transport, Other, Net, US Dollars"
                ),
                "source_indicator_code": "BSTRAO_USD",
                "category": "INDICATOR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_central_government_revenue_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Revenue Cash Fiscal Central Government and Social Security Funds from IMF",
            "origin": {
                "name": "Fiscal, Central Government and Social Security Funds, Revenue, 2001 Manual, Cash, Euros",
                "source_indicator_code": "GCGR_G01_CA_EUR",
                "category": "ALT_FISCAL",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_gfs_tax_detail_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": (
                "Germany Revenue Central Government Taxes Government and Public Sector "
                "Finance Taxes on goods and services from IMF"
            ),
            "origin": {
                "name": (
                    "Government and Public Sector Finance, Revenue, Central Government, "
                    "Taxes, Taxes on goods and services, Excises [2014 Manual], National Currency"
                ),
                "source_indicator_code": "GRTGSE_G14_CG_XDC",
                "category": "INDICATOR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_activity_gdp_detail_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Production Approach Real Manufacturing Activity Gross Domestic Product from IMF",
            "origin": {
                "name": (
                    "National Accounts, Activity, Gross Domestic Product, Production Approach, "
                    "Real, By International Standard Industrial Classification of All Economic "
                    "Activities (ISIC) Rev. 4, Manufacturing, National Currency"
                ),
                "source_indicator_code": "NGDP_PA_R_ISIC4_C_XDC",
                "category": "INDICATOR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_share_price_definition_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": (
                "NACE2 Bosnia & Herzegovina Definition Share Price Index "
                "Bosnian Investment Fund Index (AVG JUN-28-02=1000) from IMF"
            ),
            "origin": {
                "name": (
                    "Bosnia & Herzegovina Definition, Share Price Index, Bosnian Investment "
                    "Fund Index, (AVG, JUN-28-02=1000), NACE2, Index"
                ),
                "source_indicator_code": "BIH_BIFIBPA_IX",
                "category": "INDICATOR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_keeps_datamapper_imf_debt_category_executable() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "United States General Government Debt from IMF",
            "origin": {
                "name": "General Government Debt",
                "source_indicator_code": "GG_DEBT_GDP",
                "category": "GDD",
                "raw_metadata": json.dumps(
                    {
                        "source": "Global Debt Database (Sep 2025)",
                        "dataset": "GDD",
                    }
                ),
            },
        }
    )

    assert "imf_low_viability_family" not in audit["reasons"]


def test_audit_direct_query_shape_keeps_high_level_imf_fiscal_aggregate_executable() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Brazil General Government net lending Fiscal from IMF",
            "origin": {
                "name": "Fiscal, General Government, net lending",
                "source_indicator_code": "HN_GGO_F_081",
                "category": "ALT_FISCAL",
            },
        }
    )

    assert "imf_low_viability_family" not in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_local_government_fiscal_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Local Government Revenue Tax Trade Exchange Taxes Accrual Fiscal from IMF",
            "origin": {
                "name": "Fiscal, Local Government, Revenue, Tax, Trade, Exchange Taxes, 2001 Manual, Accrual, National Currency",
                "source_indicator_code": "GLRTTXT_G01_AC_XDC",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_does_not_apply_imf_trade_screen_to_other_providers() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Comtrade",
            "query": "Japan Merchandise Trade Value of Exports Chapter 60 knitted goods from Comtrade",
            "origin": {
                "name": "Merchandise Trade Value of Exports Chapter 60 knitted goods",
                "source_indicator_code": "600110",
            },
        }
    )

    assert "imf_low_viability_family" not in audit["reasons"]


def test_audit_direct_query_shape_flags_fred_naics_revenue_queries():
    audit = audit_direct_query_shape(
        {
            "query": "US Total Revenue for 6211: Offices of Physicians - Taxable Establishments Subject to Federal Income Tax from FRED",
            "origin": {"name": "Total Revenue for 6211: Offices of Physicians - Taxable, Establishments Subject to Federal Income Tax"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "fred_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_fred_regional_acs_and_fiscal_situation_families() -> None:
    cases = [
        "US LA High School Graduate or Higher (5-year estimate) in St. Bernard Parish from FRED",
        "Fiscal Situation of General Government: Net Lending/borrowing for Indonesia from FRED",
    ]
    for query in cases:
        audit = audit_direct_query_shape(
            {
                "provider": "FRED",
                "query": query,
                "origin": {"name": query.removesuffix(" from FRED"), "source_provider": "FRED"},
            }
        )

        assert audit["risk_level"] == "high"
        assert "fred_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_fred_hicp_by_origin_code():
    audit = audit_direct_query_shape(
        {
            "provider_stratum": "FRED",
            "query": "US harmonized inflation from FRED",
            "origin": {
                "source_provider": "FRED",
                "name": "Harmonized Index of Consumer Prices: All Items",
                "source_indicator_code": "HICPUSAINDEX",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "fred_hicp_catalog_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_id_challenge_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Japan Challenge: Applying for a job total (% of population ages 15+ without an ID) from World Bank",
            "origin": {"name": "Challenge: Applying for a job, total (% of population ages 15+ without an ID)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_id_financial_inclusion_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_education_expenditure_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Germany World Bank: Share of household consumption for private expenditures on primary education (%) from World Bank",
            "origin": {"name": "World Bank: Share of household consumption for private expenditures on primary education (%)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_urban_education_expenditure_code_family():
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Bank: Share of tertiary education spending on urban areas (%) from World Bank",
            "origin": {
                "name": "World Bank: Share of tertiary education spending on urban areas (%)",
                "source_indicator_code": "PER.OC.GEO.URB.TER",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_assessment_queries():
    audit = audit_direct_query_shape(
        {
            "query": "China Rural Above Proficiency;SEA-PLM 2019 for grade 5 using MPL Level 6 for reading from World Bank",
            "origin": {"name": "Rural Above Proficiency;SEA-PLM 2019 for grade 5 using MPL Level 6 for reading"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_assessment_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_macro_exposure_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Germany Price level ratio of PPP conversion factor (GDP) to market exchange rate from World Bank",
            "origin": {"name": "Price level ratio of PPP conversion factor (GDP) to market exchange rate"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_macro_exposure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_specialized_source_category():
    audit = audit_direct_query_shape(
        {
            "query": "China All instruments USD Ext. Debt Service Pmt DI: Intercom Lending More than 18 to 24 Prin. and Int. from World Bank",
            "origin": {
                "name": "All instruments, USD, Ext. Debt Service Pmt, DI: Intercom Lending, More than 18 to 24, Prin. and Int.",
                "category": "Quarterly External Debt Statistics SDDS",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_specialized_source_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_ddh_prevalence_queries():
    audit = audit_direct_query_shape(
        {
            "query": "India Adjusted prevalence of male persons with some degree of mobility difficulty (% of male persons) from World Bank",
            "origin": {
                "name": "Adjusted prevalence of male persons with some degree of mobility difficulty (% of male persons)",
                "category": "Disability Data Hub (DDH)",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_ddh_prevalence_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_ddh_mobile_phone_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "Brazil Persons owing a mobility phone (% of persons with any degree of functional difficulty) from World Bank",
            "origin": {
                "name": "Persons owing a mobility phone (% of persons with any degree of functional difficulty)",
                "category": "Disability Data Hub (DDH)",
                "source_indicator_code": "ocel_any_dfcl_all",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_ddh_prevalence_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_tertiary_expenditure_ppp_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India Government expenditure on tertiary education PPP$ (millions) from World Bank",
            "origin": {
                "name": "Government expenditure on tertiary education, PPP$ (millions)",
                "category": "Education Statistics",
                "source_indicator_code": "UIS.X.PPP.5T8.FSGOV",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_double_shift_school_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India World Bank: Share of schools with double shifts (%) from World Bank",
            "origin": {
                "name": "World Bank: Share of schools with double shifts (%)",
                "category": "Education Statistics",
                "source_indicator_code": "PER.ALL.SFT.D",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_subnational_salary_share_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India World Bank: Subnational government share of salaries (%) from World Bank",
            "origin": {
                "name": "World Bank: Subnational government share of salaries (%)",
                "category": "Education Statistics",
                "source_indicator_code": "PER.ALL.SAL.SUBNAT",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_age_band_population_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India ages 11-16 female Population from World Bank",
            "origin": {
                "name": "Population, ages 11-16, female",
                "category": "Education Statistics",
                "source_indicator_code": "SP.POP.1116.FE.UN",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_demographic_literacy_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_total_age_band_population_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India ages 7-10 total Population from World Bank",
            "origin": {
                "name": "Population, ages 7-10, total",
                "category": "Education Statistics",
                "source_indicator_code": "SP.POP.0710.TO.UN",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_demographic_literacy_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_ease_of_doing_business_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "Global: Ease of doing business score (DB15 methodology) from World Bank",
            "origin": {"name": "Ease of doing business score (DB15 methodology)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_specialized_source_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_reference_year_population_slices() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Population Age primary for Reference Year 2019; Male from World Bank",
            "origin": {"name": "Population Age primary for Reference Year 2019; Male"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_demographic_literacy_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_teacher_salary_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Bank: Average monthly teacher salary (in USD or local currency) from World Bank",
            "origin": {"name": "Average monthly teacher salary (in USD or local currency)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_public_capital_education_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Bank: Public capital expenditure on education as % of GDP from World Bank",
            "origin": {"name": "Public capital expenditure on education as % of GDP"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_school_fee_revenue_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Bank: Share of total education revenue from school fees (%) from World Bank",
            "origin": {"name": "Share of total education revenue from school fees (%)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_tertiary_rural_spending_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Bank: Share of tertiary education spending on rural areas (%) from World Bank",
            "origin": {"name": "Share of tertiary education spending on rural areas (%)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_general_rural_education_share_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Bank: Share of education spending on rural areas (%) from World Bank",
            "origin": {"name": "Share of education spending on rural areas (%)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_school_feeding_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Bank: School Feeding Programs (% of total recurrent expenditures) from World Bank",
            "origin": {"name": "School Feeding Programs (% of total recurrent expenditures)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_education_expenditure_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_literacy_disability_slice() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "Brazil Literacy rate (% of persons with any degree of seeing difficulty) from World Bank",
            "origin": {
                "name": "Literacy rate (% of persons with any degree of seeing difficulty)",
                "category": "Disability Data Hub (DDH)",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_demographic_literacy_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_disability_age_literacy_prompts() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "Brazil Literacy rate (% of persons aged 30 to 44 years with disability i.e. at least a lot of functional difficulty) from World Bank",
            "origin": {"name": "Literacy rate (% of persons aged 30 to 44 years with disability i.e. at least a lot of functional difficulty)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_demographic_literacy_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_financial_resilience_prompts() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "borrowing selling something Less than two weeks can be covered using savings seeking help from friends and family or other ways in case household loses its main source of income secondary education or more (% age 15+) from World Bank",
            "origin": {"name": "Less than two weeks can be covered using savings seeking help from friends and family or other ways"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_id_financial_inclusion_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_reference_year_population_prompts_without_gender() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "World Population Age 0516 for Reference Year 2019; from World Bank",
            "origin": {"name": "Population Age 0516 for Reference Year 2019"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_demographic_literacy_slice" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_price_or_memorandum_family():
    audit = audit_direct_query_shape(
        {
            "query": "Germany Expenditure General Government Memorandum items Cash Government and Public Sector Finance from IMF",
            "origin": {
                "name": "Government and Public Sector Finance, Expenditure, General Government, Memorandum items, Current Expenditures [2001 Manual], Cash, National Currency",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_price_or_memorandum_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_consumer_price_weight_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Weight Nepal Definition Price Index Consumer Prices:Non-food and Services from IMF",
            "origin": {
                "name": "Nepal Definition, Price Index Consumer Prices:Non-food and Services, Weight",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_price_or_memorandum_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_household_expenditure_consumer_price_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany All Items Glassware BY2015 Consumer Prices Expenditure of Households tableware and household utensils from IMF",
            "origin": {"name": "Germany All Items Glassware BY2015 Consumer Prices Expenditure of Households tableware and household utensils"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_price_or_memorandum_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_producer_price_activity_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Other Manufacturing Producer Price Index Commodities by Activity from IMF",
            "origin": {
                "name": "Prices, Producer Price Index, Commodities by Activity, Other Manufacturing, ISIC Rev 4, Index",
                "source_indicator_code": "PPPI_ISIC4_C32_IX",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_price_or_memorandum_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_sectoral_financial_asset_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Pension Funds Assets Sectoral Financial derivatives and employee stock options from IMF",
            "origin": {
                "name": "Sectoral Financial, Pension Funds, Assets, Financial derivatives and employee stock options",
                "source_indicator_code": "NS_PF_AF_XDC",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_complex_finance_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_next200_imf_probe_reject_families() -> None:
    cases = [
        (
            "Germany Real Primary Sector Agriculture livestock Gross Value Added forestry and agriculture services from IMF",
            "NGDPVA_R_ISIC31_A01T02_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Central Government Revenue Tax Trade Other Fiscal from IMF",
            "CG01_GRTTO_G01_USD",
            "imf_low_viability_family",
        ),
        (
            "Germany Real Gross Fixed Capital Formation of which Construction Previous Year Prices from IMF",
            "NFIC_R_CH_PYP_XDC",
            "imf_low_viability_family",
        ),
        (
            "Fisheries All Currencies Bahamas Definition Banking System Indicators Sectoral Distribution of Credit from IMF",
            "BHS_SDC_AFI_XDC",
            "imf_complex_finance_family",
        ),
        (
            "Germany Production Industrial Production Construction Base Year Economic Activity By Economic Activity from IMF",
            "AIPCO_BY1990_IX",
            "imf_low_viability_family",
        ),
        (
            "Germany Revenue Interest Cash Fiscal Social Security Central Government from IMF",
            "GXRI_G01_CA_USD",
            "imf_low_viability_family",
        ),
        (
            "Germany Pulp paper External Trade Value of Exports paperboard and art. thereof from IMF",
            "TXG_HS47T49_USD",
            "imf_low_viability_family",
        ),
        (
            "Germany Monetary Other Depository Corporations Survey Other Items (Net) Share and Alternate from IMF",
            "ODC_SA_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Expense Subsidies Accrual Fiscal Social Security Central Government To Public Corporations from IMF",
            "GXESUB_G01_AC_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Regional Government Revenue Tax Trade Exports Accrual Fiscal from IMF",
            "GRRTTX_G01_AC_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Real Expenditure Gross Domestic Product Gross Capital Formation from IMF",
            "NGDP_E_R_GCF_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Branch Trade Tajikistan Definition Nominal Gdp By Branches of Origin from IMF",
            "TJK_NAG_NGDPSW_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Expense Other Miscellaneous Cash Fiscal General Government Consolidation Non-interest Property Expense from IMF",
            "GGXOM_G01_CA_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Crude Materials Inedible except Fuels SITC External Trade Value of Exports from IMF",
            "TXG_SITC2_USD",
            "imf_low_viability_family",
        ),
        (
            "Germany Portfolio Investment Debt Securities Long-term IIP Assets denominated in Japanese Yen from IMF",
            "IAD_PI_DS_L_JPY",
            "imf_complex_finance_family",
        ),
        (
            "SNNP Gamo_Gofa Kilogram Ethiopia Definition Crop Production By Region from IMF",
            "ETH_CROP_REGION_KG",
            "imf_low_viability_family",
        ),
        (
            "Germany Regional Government Cash Fiscal Cash Infow from Financing Activities from IMF",
            "GR_CASH_INFLOW_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany General Government Fiscal Overall primary balance from IMF",
            "GG_GXOPB_G01_USD",
            "imf_low_viability_family",
        ),
        (
            "Japan Consolidated Income and Distribution Transfer of funds from special account Monetization from IMF",
            "NI_CID_F_008",
            "imf_low_viability_family",
        ),
        (
            "Vanuatu Definition Vanilla Merchandise Trade Value of Exports FOB from IMF",
            "TXGBDVA_FOB_XDC",
            "imf_low_viability_family",
        ),
        (
            "Uzbekistan Services Other Weight Definition Consumer Price Weight from IMF",
            "UZB_PCPIS_O_WT",
            "imf_price_or_memorandum_family",
        ),
        (
            "Germany Goods Producer Price Index from IMF",
            "PPPIG_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Jamaica Fiscal Revenue details Environmental Levy from IMF",
            "JM_CGO_DR_013",
            "imf_low_viability_family",
        ),
        (
            "Germany Central Government Cash surplus/deficit Cash Fiscal from IMF",
            "GCXCCB_G01_CA_EUR",
            "imf_low_viability_family",
        ),
        (
            "Germany Fiscal Gross Operating Balance from IMF",
            "GXCBG_G14_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Revenue Social contributions Government and Public Sector Finance Budgetary Central Government from IMF",
            "GCBRSC_G14_XDC",
            "imf_low_viability_family",
        ),
        (
            "Japan Final Summary Excise & Fees import duty & other Customs Rev from IMF",
            "JPN_CUSTOMS_REV",
            "imf_low_viability_family",
        ),
        (
            "Germany Producer Price Index Mining of coal and lignite; extraction of peat from IMF",
            "PPPI_MINING_COAL_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Stock Market Zimbabwe Definition Victoria Falls-All Share Index from IMF",
            "ZWE_STOCK_VF_IX",
            "imf_low_viability_family",
        ),
        (
            "Germany Nominal Seasonally Adjusted GDP-GNP Relation Net Primary Income from Abroad from IMF",
            "NGDP_GNP_NPI_SA_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Oil Production Economic Activity Barrels per Day from IMF",
            "OIL_PROD_BPD",
            "imf_low_viability_family",
        ),
        (
            "Economic Activity Brunei Darussalam Definition Foreign Direct Investment Financial and Insurance Activities from IMF",
            "BRN_FDI_FIN_INS_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Real Seasonally Adjusted Taxes on Products from IMF",
            "TAX_PRODUCTS_SA_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Assets Nominal Sectoral Accounts Rest of the World Currency and deposits from IMF",
            "NSA_ROW_CURRDEP_XDC",
            "imf_complex_finance_family",
        ),
        (
            "Germany Real Oil Expenditure Gross Domestic Product Gross Capital Formation Gross Fixed Capital Formation from IMF",
            "NFI_OIL_R_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Banks Financial Certificates Of Deposits 180 To 360 Days Percent per Annum from IMF",
            "FCD_180_360_PA",
            "imf_low_viability_family",
        ),
        (
            "Germany Pound Sterling Rate Exchange Rate Other Foreign Currency per National Currency End of Period from IMF",
            "ENDE_XDC_GBP_RATE",
            "imf_low_viability_family",
        ),
        (
            "Japan Deductions Federation Income and Distribution from IMF",
            "NI_D_FED_008",
            "imf_low_viability_family",
        ),
        (
            "Germany Production Approach Real Statistical Discrepancy in GDP from IMF",
            "NGDP_R_STATDISC_XDC",
            "imf_low_viability_family",
        ),
        (
            "Mortality National Percent Bangladesh Definition Socio Demographic Indicators Crude Death Rate from IMF",
            "BGD_CRUDE_DEATH_RATE",
            "imf_low_viability_family",
        ),
        (
            "Germany Real Government Consumption Expenditure Donor (wages) from IMF",
            "NGOV_CONS_DONOR_WAGES",
            "imf_low_viability_family",
        ),
        (
            "MAC Ghana Definition Share Price Index from IMF",
            "GHA_MAC_SHARE_PRICE",
            "imf_low_viability_family",
        ),
        (
            "Tourism Arrivals New Zealand Persons Number of Economic Activity Number of Visitors from IMF",
            "NZL_TOUR_ARRIVALS",
            "imf_low_viability_family",
        ),
        (
            "Singapore Persons Number of Indicators of Economic Activity Number of Tourist Arrivals by Origin from IMF",
            "SGP_TOURIST_ARRIVALS",
            "imf_low_viability_family",
        ),
        (
            "Germany General Government Cash Fiscal Memo Item: Expenditure from IMF",
            "GG_MEMO_EXP_CA_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Central Government International reserves Official Reserve Assets Other Reserve Assets (Specify) from IMF",
            "RES_ORA_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany non durable Consumer Price Index Excluding Fish and seafood from IMF",
            "PCPI_EX_FISH_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Germany Transport Harmonized Consumer Prices from IMF",
            "HICP_TRANSPORT_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Fiscal Revenue details Jamaica GCT (Imports) from IMF",
            "JM_CGO_DR_GCT_IMPORTS",
            "imf_low_viability_family",
        ),
        (
            "Kosovo Definition Central Bank Balance Sheet: Monetary gold as SDRs from IMF",
            "XKX_CBB_MGAS_EUR",
            "imf_low_viability_family",
        ),
        (
            "Germany Rate US Dollars per ounce of gold End of period from IMF",
            "PZPIGOLD_USD_EOP_RATE",
            "imf_low_viability_family",
        ),
        (
            "Germany Production Education Economic Activity from IMF",
            "A_ISIC4_P_IX",
            "imf_low_viability_family",
        ),
        (
            "Germany NACE2 Producer Price Index Production of motor vehicles (transport) trailers and semi-trailers from IMF",
            "PPPI_NACE2_C30_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Germany Liquid assets to total assets o/w large banks 1/ from IMF",
            "SUR_FSLT_LB_PT",
            "imf_complex_finance_family",
        ),
        (
            "Germany Central Government Principle payments (incl. Tbills) from IMF",
            "SUR_GCDSP_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Mid-Market Rate Exchange Rates Real Effective Exchange Rate based on Consumer Price Index from IMF",
            "EREER_IX",
            "imf_low_viability_family",
        ),
        (
            "Germany Alcoholic Beverages Tobacco and Narcotics Tobacco Base Year Previous Period Consumer Prices from IMF",
            "PCPI_TOB_PP_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Germany Real Reference Chained Expenditure Gross Domestic Product External Balance of Goods and Services from IMF",
            "NGDP_E_R_EBGS_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Savings and Investment Memorandum Items Nominal Oil Income Gross National Disposable Income Per Capita from IMF",
            "NGNDI_OIL_PC_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany All Items Special Indexes Capital City Consumer Prices Clothing & Footwear from IMF",
            "PCPI_CC_CLOTH_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Germany Expenditure Economic affairs Fiscal By Functions of Government Fuel and energy from IMF",
            "COFOG_FUEL_ENERGY_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Manufacturing NACE2 Percentage Change Previous Period Percent Producer Price Index from IMF",
            "PPPI_NACE2_MAN_PCH",
            "imf_price_or_memorandum_family",
        ),
        (
            "Germany Monetary Microfinance Deposit Taking Institutions' Balance Sheet Other Items (Net) Share and Alternate from IMF",
            "MFI_DTI_OIN_SA_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Activity Real Manufacturing San Marino Definition mining and quarrying and other industrial activities from IMF",
            "SMR_MANUFACTURING_ACTIVITY",
            "imf_low_viability_family",
        ),
        (
            "Germany Central Government LT Loans by Commercial Banks from IMF",
            "CG_LT_LOANS_CB_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Miscellaneous Products: Other manufacturing n.e.c. from IMF",
            "MISC_OTHER_MANUFACTURING_NEC",
            "imf_low_viability_family",
        ),
        (
            "Germany Oil Nominal Taxes less Subsidies on Products from IMF",
            "NGDP_TAX_SUBSIDIES_PRODUCTS_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany NACE2 Producer Price Index Extraction of coal and lignite from IMF",
            "PPPI_NACE2_COAL_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Germany Water Collection NACE2 Producer Price Index treatment and supply from IMF",
            "PPPI_NACE2_WATER_IX",
            "imf_price_or_memorandum_family",
        ),
        (
            "Industrial Production Angola Definition Business Confidence in Retail Trade from IMF",
            "AGO_BUSINESS_CONF_RETAIL",
            "imf_low_viability_family",
        ),
        (
            "Equities Financial Market Prices Primary Market Instruments UK FTSE 100 from IMF",
            "FMP_EQUITIES_FTSE100",
            "imf_low_viability_family",
        ),
        (
            "Germany Assets Nominal Sectoral Accounts money market funds Monetary gold and SDRs from IMF",
            "SA_MMF_MONETARY_GOLD_SDRS_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany Other Community Real Reference Chained Services Social and Personal Service Activities from IMF",
            "NGDP_R_SERVICES_SOCIAL_PERSONAL",
            "imf_low_viability_family",
        ),
        (
            "Germany Fiscal Expenditure Defense Fiscal Year-March Central Government By Functions of Government from IMF",
            "COFOG_DEFENSE_MARCH_CG",
            "imf_low_viability_family",
        ),
        (
            "Economic Activity Other Indicators Average Days Fiji Definition Tourism: Length of Stay Cruise Ship Passengers from IMF",
            "FJI_TOURISM_CRUISE_STAY",
            "imf_low_viability_family",
        ),
        (
            "Germany Nominal roots GDP by Activity Cultivation of tubers vegetables and legumes from IMF",
            "NGDP_ACTIVITY_TUBERS",
            "imf_low_viability_family",
        ),
        (
            "Germany defence education GVA Public administration human health and social work activities from IMF",
            "GVA_PUBLIC_ADMIN_HEALTH_SOCIAL_WORK",
            "imf_low_viability_family",
        ),
        (
            "Germany Water Electricity Harmonized Overlap Housing Gas and Other Fuels from IMF",
            "HICP_OVERLAP_HOUSING_GAS",
            "imf_price_or_memorandum_family",
        ),
        (
            "Kosovo Definition Other Depository Corporations Balance Sheet: Gross loans and lease financing from IMF",
            "ODC_BALANCE_SHEET_GROSS_LOANS",
            "imf_low_viability_family",
        ),
        (
            "Germany National Accounts, NFC, FC, HH, NPISH, Tertiary Sector from IMF",
            "NATIONAL_ACCOUNTS_SECTORS_TERTIARY",
            "imf_low_viability_family",
        ),
        (
            "Germany Central bank Assets Insurance Sectoral pension and standardized guarantee schemes from IMF",
            "CB_ASSETS_INSURANCE_PENSION_SCHEMES",
            "imf_low_viability_family",
        ),
        (
            "Germany Revenue Social contributions Employee contributions Fiscal Other social contributions from IMF",
            "FISCAL_SOCIAL_CONTRIBUTIONS_EMPLOYEE",
            "imf_low_viability_family",
        ),
        (
            "Germany Government Fiscal Year Nominal Gross Domestic Product Regional Government Enterprises from IMF",
            "FSM_NAG_NGDPNU_FY_USD",
            "imf_low_viability_family",
        ),
        (
            "Germany Vehicles aircraft External Trade Value of Re-Exports vessels and associated  equipment from IMF",
            "TXGBRE_HS86T89_USD",
            "imf_low_viability_family",
        ),
        (
            "Percent Panama Definition Poverty And Income Distribution Indicators Multidimensional poverty index from IMF",
            "PAN_PIDI_SPP_MPI_PT",
            "imf_low_viability_family",
        ),
        (
            "Natural Gas Cubic Meters Oman Definition Other Real Sector Statistics imports + local production (Mil.Cubic.M) from IMF",
            "OMN_OIL_NGILP_MCB",
            "imf_low_viability_family",
        ),
        (
            "Monetary Aggregates Serbia Republic of Definition Foreign exchange reserves of NBS (EUR m) from IMF",
            "SRB_FMERNBS_XDC",
            "imf_low_viability_family",
        ),
        (
            "Germany General Government Fiscal Total Changes in Net Worth from IMF",
            "GFS_CHANGES_NET_WORTH",
            "imf_low_viability_family",
        ),
        (
            "Germany Real Fiscal Year Total Non-Monetary Gross Domestic Product from IMF",
            "NAGDP_NON_MONETARY_FY",
            "imf_low_viability_family",
        ),
        (
            "Germany Total Economy Assets Sectoral Equity and investment fund shares from IMF",
            "SA_TOTAL_ECONOMY_EQUITY_FUNDS",
            "imf_low_viability_family",
        ),
        (
            "Germany Genenral Government Expense Other Fiscal Non-interest Property Expense from IMF",
            "GFS_NON_INTEREST_PROPERTY_EXPENSE",
            "imf_low_viability_family",
        ),
        (
            "Germany Monetary Commercial Banks' Balance Sheet Deposits Included in Broad Money from IMF",
            "CB_BALANCE_SHEET_BROAD_MONEY_DEPOSITS",
            "imf_low_viability_family",
        ),
        (
            "Germany Memorandum Items Structures Real Expenditure Gross Fixed Capital Formation from IMF",
            "NINV_R_STRUCTURES_GFCF",
            "imf_low_viability_family",
        ),
        (
            "Germany Real Construction NACE2 Cumulative Percent Gross value added Previous year prices from IMF",
            "GVA_CONSTRUCTION_NACE2_PYP",
            "imf_low_viability_family",
        ),
        (
            "Cambodia Definition Foreign Direct Investment Approval - from other countries in US dollars from IMF",
            "KHM_FDI_APPROVAL_OTHER_COUNTRIES",
            "imf_low_viability_family",
        ),
        (
            "Exchange Rates period average Rate Cambodia Definition Official buying rate riels /US dollar from IMF",
            "KHM_OFFICIAL_BUYING_RATE_PA",
            "imf_low_viability_family",
        ),
        (
            "SPAIN.STATE FINANCIAL TRANSACTIONS.ESA 2010. Net acquisition of financial assets. EUR millions from IMF",
            "ESP_FINANCIAL_TRANSACTIONS_NET_ACQ_ASSETS",
            "imf_low_viability_family",
        ),
        (
            "Brazil Central Government Fiscal: Total Expenditures -  Number of public workers from IMF",
            "BRA_CG_PUBLIC_WORKERS",
            "imf_low_viability_family",
        ),
        (
            "Brazil Population By Sex Male Persons Number of Socio-Demographic Indicators Other changes in volume from IMF",
            "BRA_SOCIO_DEM_OTHER_CHANGES",
            "imf_low_viability_family",
        ),
        (
            "Uganda Definition Credit Institutions' Balance Sheet: NDA Other Items(Net) Other Items(Net) from IMF",
            "UGA_CI_BS_NDA_OTHER_ITEMS",
            "imf_low_viability_family",
        ),
        (
            "Brazil Genenral Government Fiscal Net financial wealth position from IMF",
            "BRA_GG_NET_FINANCIAL_WEALTH",
            "imf_low_viability_family",
        ),
        (
            "Brazil Real Agriculture NACE2 Gross value added forestry and fishing from IMF",
            "BRA_GVA_AGRI_NACE2_FORESTRY",
            "imf_low_viability_family",
        ),
        (
            "Brazil Other Services except Government Nominal Services Hotels and Restaurants from IMF",
            "BRA_SERVICES_HOTELS_RESTAURANTS",
            "imf_low_viability_family",
        ),
        (
            "Brazil Financial intermediation Tajikistan Definition Loans of Banks by Sectors from IMF",
            "TJK_LOANS_BANKS_BY_SECTOR",
            "imf_low_viability_family",
        ),
        (
            "Monetary Aggregates MONEY SUPPLY Togo Definition in Francs CFA from IMF",
            "TGO_MONEY_SUPPLY_CFA",
            "imf_low_viability_family",
        ),
        (
            "Consumption By Product Oil Refinery Products Gasoline Motor Gasoline Ton Economic Activity from IMF",
            "OIL_REFINERY_GASOLINE_CONSUMPTION",
            "imf_low_viability_family",
        ),
        (
            "Brazil Fiscal Rule Indicator from IMF",
            "BRA_FISCAL_RULE_INDICATOR",
            "imf_low_viability_family",
        ),
        (
            "Construction Dominican Republic Definition Type of good from IMF",
            "DOM_CONSTRUCTION_TYPE_GOOD",
            "imf_low_viability_family",
        ),
        (
            "Brazil Genenral Government Expense Social Benefits Fiscal from IMF",
            "BRA_GG_SOCIAL_BENEFITS",
            "imf_low_viability_family",
        ),
        (
            "Brazil Analytical Measures General Government Government and Public Sector Finance Wealth and Debt from IMF",
            "BRA_GG_WEALTH_DEBT",
            "imf_low_viability_family",
        ),
        (
            "Brazil Catering Tajikistan Definition Loans of Banks by Sectors from IMF",
            "TJK_LOANS_BANKS_CATERING",
            "imf_low_viability_family",
        ),
        (
            "Brazil Transport Tajikistan Definition Loans of Banks by Sectors from IMF",
            "TJK_LOANS_BANKS_TRANSPORT",
            "imf_low_viability_family",
        ),
        (
            "Nominal Education Non-market Education Dominican Republic definition from IMF",
            "DOM_NON_MARKET_EDUCATION",
            "imf_low_viability_family",
        ),
        (
            "Brazil Construction Real Reference chained Seasonally adjusted Industry from IMF",
            "BRA_CONSTRUCTION_SA_INDUSTRY",
            "imf_low_viability_family",
        ),
        (
            "Brazil Central Government Fiscal: Total Expenditures -  Wages and Salaries(mn SUR) from IMF",
            "BRA_CG_EXP_WAGES_SUR",
            "imf_low_viability_family",
        ),
        (
            "The Definition Tourist Arrivals Persons Number of Gambia Traditional Countries: Norwegian from IMF",
            "GMB_TOURIST_ARRIVALS_NORWEGIAN",
            "imf_low_viability_family",
        ),
        (
            "Brazil Financial Derivatives Monetary Central Bank Survey Financial Derivatives Other Financial Corporations from IMF",
            "BRA_CB_FIN_DERIVATIVES_OFC",
            "imf_low_viability_family",
        ),
        (
            "Brazil Fiscal Year Real Gross Value Added from IMF",
            "BRA_FY_REAL_GVA",
            "imf_low_viability_family",
        ),
        (
            "Brazil Real Services Wholesale and Retail Trade from IMF",
            "BRA_REAL_SERVICES_WHOLESALE_RETAIL",
            "imf_low_viability_family",
        ),
        (
            "Brazil Real Seasonally Adjusted Gross Value Added from IMF",
            "BRA_REAL_SA_GVA",
            "imf_low_viability_family",
        ),
        (
            "Brazil Assets Loans Sectoral Households and NPISHs from IMF",
            "BRA_SECTORAL_LOANS_HH_NPISH",
            "imf_low_viability_family",
        ),
        (
            "Brazil Genenral Government Fiscal burden Cash Fiscal from IMF",
            "BRA_GG_FISCAL_BURDEN_CASH",
            "imf_low_viability_family",
        ),
        (
            "Total Brunei Darussalam Definition Foreign Direct Investment from IMF",
            "BRN_FDI_TOTAL",
            "imf_low_viability_family",
        ),
        (
            "Monetary Monetary Aggregates Fiji Definition Broad Money (M3) from IMF",
            "FJI_BROAD_MONEY_M3",
            "imf_low_viability_family",
        ),
        (
            "Average Rate Exchange Rates National Currency Per Norway Kroner from IMF",
            "NOK_EXCHANGE_RATE_AVERAGE",
            "imf_low_viability_family",
        ),
        (
            "Nominal Education Market Education Dominican Republic definition from IMF",
            "DOM_MARKET_EDUCATION",
            "imf_low_viability_family",
        ),
        (
            "Brazil Central Government Undisbursed balance in mil. SRD on guarantees from IMF",
            "BRA_CG_UNDISBURSED_GUARANTEES",
            "imf_low_viability_family",
        ),
        (
            "Financial Corporations Financial Institutions Nominal Bahrain Definition from IMF",
            "BHR_FINANCIAL_CORPORATIONS_INSTITUTIONS",
            "imf_low_viability_family",
        ),
        (
            "Fed.Sts. Definition National Income Disposable Income Gross Deflator Micronesia from IMF",
            "FSM_GNDI_GROSS_DEFLATOR",
            "imf_low_viability_family",
        ),
        (
            "Brazil Imports Burundi Definition PRODUCTION GOODS:17. Leather from IMF",
            "BDI_IMPORTS_PRODUCTION_GOODS_LEATHER",
            "imf_low_viability_family",
        ),
        (
            "Remittances US Dollar Guatemala Definition Currency Income for Family Remittances from IMF",
            "GTM_FAMILY_REMITTANCES",
            "imf_low_viability_family",
        ),
        (
            "Brazil Percent Change Previous Period percent Exchange Rates Nominal Effective Exchange Rate from IMF",
            "BRA_NEER_PCH",
            "imf_low_viability_family",
        ),
        (
            "Fertility Rural Percent Bangladesh Definition Socio Demographic Indicators Total Fertility Rate from IMF",
            "BGD_RURAL_TOTAL_FERTILITY",
            "imf_low_viability_family",
        ),
        (
            "Brazil Local Government Accrual Fiscal Net operating balance Adjustment to Net operating balance from IMF",
            "BRA_LG_NET_OPERATING_BALANCE",
            "imf_low_viability_family",
        ),
        (
            "Brazil Population By Age Youth Persons Number of Socio-Demographic Indicators from IMF",
            "BRA_POP_AGE_YOUTH",
            "imf_low_viability_family",
        ),
        (
            "Koror Socio-Demographic Indicator Population Persons Number of Palau Definition from IMF",
            "PLW_KOROR_POPULATION",
            "imf_low_viability_family",
        ),
        (
            "Real Services Dominican Republic definition from IMF",
            "DOM_REAL_SERVICES",
            "imf_low_viability_family",
        ),
        (
            "Brazil Total Exports Merchandise Trade Textiles & Fabrics from IMF",
            "BRA_MERCH_TRADE_TEXTILES_FABRICS",
            "imf_low_viability_family",
        ),
        (
            "Brazil Exports Burundi Definition MANUFACTURED PRODUCTS: others (1) from IMF",
            "BDI_MET_TXG_OTH_XDC",
            "imf_low_viability_family",
        ),
        (
            "Brazil Transport from IMF",
            "NGDPVA_ISIC3_I61T64_XDC",
            "imf_low_viability_family",
        ),
        (
            "China Financial Derivatives from IMF",
            "CHN_FINANCIAL_DERIVATIVES",
            "imf_low_viability_family",
        ),
        (
            "Brazil Real Gross Capital Formation Change in Inventories from IMF",
            "BRA_GCF_INVENTORIES",
            "imf_low_viability_family",
        ),
        (
            "Brazil General Government Accrual Fiscal Net operating balance from IMF",
            "BRA_GG_ACCRUAL_NET_OPERATING_BALANCE",
            "imf_low_viability_family",
        ),
        (
            "Brazil Real Seasonally adjusted National Income Consumption of Fixed Capital from IMF",
            "BRA_NI_CONSUMPTION_FIXED_CAPITAL",
            "imf_low_viability_family",
        ),
        (
            "Mortality National Percent Bangladesh Definition Socio Demographic Indicators Crude Death Rate from IMF",
            "BGD_CRUDE_DEATH_RATE",
            "imf_low_viability_family",
        ),
        (
            "Germany Real Government Consumption Expenditure Donor (wages) from IMF",
            "NGOV_CONS_DONOR_WAGES",
            "imf_low_viability_family",
        ),
        (
            "MAC Ghana Definition Share Price Index from IMF",
            "GHA_MAC_SHARE_PRICE",
            "imf_low_viability_family",
        ),
        (
            "Tourism Arrivals New Zealand Persons Number of Economic Activity Number of Visitors from IMF",
            "NZL_TOUR_ARRIVALS",
            "imf_low_viability_family",
        ),
        (
            "Singapore Persons Number of Indicators of Economic Activity Number of Tourist Arrivals by Origin from IMF",
            "SGP_TOURIST_ARRIVALS",
            "imf_low_viability_family",
        ),
    ]

    for query, code, reason in cases:
        audit = audit_direct_query_shape(
            {
                "provider": "IMF",
                "query": query,
                "origin": {"name": query.removesuffix(" from IMF"), "source_indicator_code": code},
            }
        )

        assert audit["risk_level"] == "high", query
        assert reason in audit["reasons"], query


def test_audit_direct_query_shape_flags_imf_other_revenue_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "US Revenue Other Revenue Accrual Fiscal Social Security Central Government Sales of Goods and Services from IMF",
            "origin": {"name": "US Revenue Other Revenue Accrual Fiscal Social Security Central Government Sales of Goods and Services"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "definition_financial_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_merchandise_trade_chapter_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Merchandise Trade Value of Imports. Chapter 80-. Tin and tin products from IMF",
            "origin": {"name": "Germany Merchandise Trade Value of Imports. Chapter 80-. Tin and tin products"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_merchandise_trade_export_chapter_codes() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Merchandise Trade Value of Exports. Chapter 60- Knitted goods from IMF",
            "origin": {
                "name": "Merchandise Trade, Value of Exports. Chapter 60- Knitted goods, Euros",
                "source_indicator_code": "TXG_H5_60_EUR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_hs_external_trade_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Exports Value Railway tramway locomotives External Trade By Harmonized Commodity Description and Coding Systems (HS 2017) Rev. 5 from IMF",
            "origin": {"name": "Germany Exports Value Railway tramway locomotives External Trade By Harmonized Commodity Description and Coding Systems (HS 2017) Rev. 5"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_cpc_external_trade_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Total Exports Merchandise Trade By Central Product Classification (CPC) Version 2.1 Wood and Cork from IMF",
            "origin": {
                "name": "Merchandise Trade, Total Exports, By Central Product Classification (CPC) Version 2.1, Wood and Cork",
                "source_indicator_code": "TXG_CPC21_31_XDC",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_does_not_flag_imf_aggregate_exports_query() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany exports of goods from IMF",
            "origin": {
                "name": "Exports of goods",
                "source_indicator_code": "XG_FOB_USD",
            },
        }
    )

    assert "imf_low_viability_family" not in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_sitc_trade_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Goods Crude materials inedible except fuels External Trade Value of Imports Free on Board (FOB) By Standard International Trade Classification (SITC) Rev. 4 from IMF",
            "origin": {"name": "Germany Goods Crude materials inedible except fuels External Trade Value of Imports Free on Board (FOB) By Standard International Trade Classification (SITC) Rev. 4"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_dataset_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "China Dataset: NAMAIN_T0101_Q from IMF",
            "origin": {"name": "Dataset: NAMAIN_T0101_Q"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_current_account_reserve_assets_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "US Current Account Primary Income Investment income Fiscal Year Balance of Payments from IMF",
            "origin": {"name": "US Current Account Primary Income Investment income Fiscal Year Balance of Payments"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_complex_finance_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_central_government_redundant_labor_titles() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Montenegro Definition Central Government Operations: Funds for redundant labor from IMF",
            "origin": {"name": "Montenegro Definition Central Government Operations: Funds for redundant labor"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "definition_financial_query" in audit["reasons"]


def test_category_success_adjustment_uses_probe_history():
    assert category_success_adjustment("WorldBank", "Quarterly Public Sector Debt") < 0
    assert category_success_adjustment("WorldBank", "Disability Data Hub (DDH)") >= 0


def test_provider_family_key_normalizes_worldbank_and_imf_names():
    assert provider_family_key("WorldBank", "World Bank: Per student expenditure on Teaching/learning materials (in USD or local currency)") == "per student expenditure on"
    assert provider_family_key("IMF", "Balance of Payments, Current Account, Goods and Services, Goods, Net exports of goods under merchanting (credit) [BPM6], Fiscal Year, US Dollars") == "balance of payments current"


def test_provider_subfamily_key_keeps_useful_worldbank_qualifiers():
    assert provider_subfamily_key(
        "WorldBank",
        "Completion rate, lower secondary education, fourth quintile, male, adjusted location parity index (LPIA)",
    ) == "completion rate lower secondary education fourth"
    assert provider_subfamily_key(
        "WorldBank",
        "Completion rate, lower secondary education, female, adjusted location parity index (LPIA)",
    ) == "completion rate lower secondary education female"


def test_family_success_adjustment_uses_probe_history():
    assert family_success_adjustment("WorldBank", "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)") > 0
    assert family_success_adjustment("WorldBank", "World Bank: Per student expenditure on Teaching/learning materials (in USD or local currency)") < 0
    assert family_success_adjustment("IMF", "Balance of Payments, Current Account, Goods and Services, Goods, Net exports of goods under merchanting (credit) [BPM6], Fiscal Year, US Dollars") > 0
    assert family_success_adjustment("IMF", "Prices, Consumer Prices, By Classification of Individual Consumption According to Purpose (COICOP), Expenditure of Households, Harmonized, Meat, Index") < 0


def test_subfamily_success_adjustment_uses_probe_history():
    assert subfamily_success_adjustment(
        "WorldBank",
        "Completion rate, lower secondary education, fourth quintile, male, adjusted location parity index (LPIA)",
    ) > 0
    assert subfamily_success_adjustment(
        "WorldBank",
        "Completion rate, lower secondary education, female, adjusted location parity index (LPIA)",
    ) < 0


def test_heuristic_subfamily_adjustment_prefers_and_demotes_expected_families():
    assert heuristic_subfamily_adjustment(
        "WorldBank",
        "Education Statistics",
        "Learning Deprivation Gap;TIMSS 2015 for grade 4 using MPL Low (400 points) for math, Fifth Quintile",
    ) < 0
    assert heuristic_subfamily_adjustment(
        "IMF",
        "INDICATOR",
        "Balance of Payments, Current Account, Secondary Income, General government, Current taxes on income, wealth, et(credit) [BPM6], National Currency",
    ) > 0


def test_preferred_default_country_uses_probe_history_for_worldbank_subfamilies():
    defaults = ["United States", "China", "India", "Brazil", "Japan", "Germany"]
    assert preferred_default_country(
        "WORLDBANK",
        "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
        defaults,
        "United States",
    ) == "Brazil"
    assert preferred_default_country(
        "WORLDBANK",
        "Completion rate, lower secondary education, fourth quintile, male, adjusted location parity index (LPIA)",
        defaults,
        "United States",
    ) == "India"


def test_preferred_default_country_for_record_uses_category_country_priors():
    defaults = ["United States", "China", "India", "Brazil", "Japan", "Germany"]
    assert preferred_default_country_for_record(
        "WORLDBANK",
        "Education Statistics",
        "Percentage of qualified teachers in primary education, both sexes (%)",
        defaults,
        "United States",
    ) == "India"


def test_preferred_default_country_for_record_uses_imf_reo_region_country():
    defaults = ["United States", "China", "India", "Brazil", "Japan", "Germany"]

    assert preferred_default_country_for_record(
        "IMF",
        "AFRREO",
        "Real Non-Oil GDP Growth",
        defaults,
        "India",
    ) == "Nigeria"


def test_default_query_for_row_uses_imf_reo_region_country():
    row = {
        "provider": "IMF",
        "code": "NGDPXO_RPCH",
        "name": "Real Non-Oil GDP Growth",
        "description": "",
        "category": "AFRREO",
    }

    assert default_query_for_row(row) == "Nigeria Real Non-Oil GDP Growth from IMF"


def test_default_query_for_row_keeps_imf_reo_natural_title_without_query_text_guard():
    row = {
        "provider": "IMF",
        "code": "TTT",
        "name": "Terms of Trade (Index 2010 = 100)",
        "description": "",
        "category": "AFRREO",
    }

    assert default_query_for_row(row) == "Nigeria Terms of Trade (Index 2010 = 100) from IMF"


def test_audit_direct_query_shape_treats_imf_bop_exact_code_as_fail_closed_supportability():
    from scripts.validation.run_certification import unsupported_direct_surface_reason

    row = {
        "provider_stratum": "IMF",
        "query": "BFORAONF_S_BP6_FY_USD from IMF",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": "BFORAONF_S_BP6_FY_USD",
            "category": "INDICATOR",
            "name": (
                "Balance of Payments, Financial Account, Other investment, "
                "Other accounts receivable/payable, Net acquisition of financial assets, "
                "Other sectors, Short-term [BPM6], Fiscal Year, US Dollars"
            ),
        },
    }

    audit = audit_direct_query_shape(row)

    assert audit["risk_level"] == "high"
    assert unsupported_direct_surface_reason(row, audit) == "imf_non_weo_public_surface_unsupported"


def test_audit_direct_query_shape_flags_oecd_conflict_analysis_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Canada Trends of political violence in West Africa: Analysis by armed group from OECD",
            "origin": {"name": "Trends of political violence in West Africa: Analysis by armed group"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_national_accounts_distribution_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Population in the National Accounts: distributions by household type from OECD",
            "origin": {"name": "Population in the National Accounts: distributions by household type"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_new_entrants_gender_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Can Share of new entrants and graduates in each field of education by gender from OECD",
            "origin": {"name": "Share of new entrants and graduates in each field of education by gender"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_neet_migration_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Share of young adults who are not employment nor in formal education or training (NEET) by country of birth and age at migration from OECD",
            "origin": {"name": "Share of young adults who are not employment nor in formal education or training (NEET) by country of birth and age at migration"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_earners_distribution_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Canada Earners distribution based on their level of earnings relative to the overall median by age group gender and educational attainment level from OECD",
            "origin": {"name": "Earners distribution based on their level of earnings relative to the overall median by age group gender and educational attainment level"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_mobile_students_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Can Share of mobile students enrolled at tertiary level by country of origin from OECD",
            "origin": {"name": "Share of mobile students enrolled at tertiary level by country of origin"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_teachers_salary_relative_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Are Teachers' actual salary relative to earnings of tertiary-educated workers from OECD",
            "origin": {"name": "Teachers' actual salary relative to earnings of tertiary-educated workers"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_oecd_programme_share_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "OECD",
            "query": "Japan Share of students enrolled in school and work-based programmes from OECD",
            "origin": {"name": "Share of students enrolled in school and work-based programmes"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_education_programme_share_query" in audit["reasons"]


def test_default_query_for_row_uses_eurostat_exact_code_for_dimension_heavy_titles() -> None:
    origin = {
        "name": "Persons reporting basic activity difficulty by sex, age, level of difficulty and income quintile",
        "source_indicator_code": "ILC_HCH17",
        "source_provider": "Eurostat",
    }
    row = {
        "provider": "Eurostat",
        "code": origin["source_indicator_code"],
        "name": origin["name"],
        "description": "",
        "coverage": "EU",
    }

    query = default_query_for_row(row)
    materialized_query = default_query_for_row({"provider_stratum": "Eurostat", "origin": origin})

    assert query == "ILC_HCH17 from Eurostat"
    assert materialized_query == "ILC_HCH17 from Eurostat"
    assert "income quintile" not in query.lower()
    audit = audit_direct_query_shape(
        {
            **row,
            "query": query,
            "provider_stratum": "Eurostat",
            "origin": origin,
        }
    )
    assert audit["risk_level"] == "low"
    assert audit["reasons"] == []


def test_audit_direct_query_shape_flags_eurostat_dimension_fragments() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Eurostat",
            "query": "France household composition and degree of urbanisation from Eurostat",
            "origin": {
                "name": "Persons participating in formal/informal voluntary activities or active citizenship by income quintile, household composition and degree of urbanisation",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_dimension_fragment_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_dimension_frequency_fragments() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Eurostat",
            "query": "France household composition degree of urbanisation and frequency from Eurostat",
            "origin": {"name": "Persons using professional homecare services by household type, income group, degree of urbanisation and frequency (2016)"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_dimension_fragment_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_ppp_indices_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Eurostat",
            "query": "Germany Purchasing power parities price level indices from Eurostat",
            "origin": {"name": "Purchasing power parities price level indices"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_cross_tab_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_weekly_hours_cross_tabs() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Eurostat",
            "query": "Spain Average actual weekly hours worked in the main job of persons who worked in this job during the reference week by professional status full-time/part-time employment and economic activity (NACE Rev. 2) (2008-2026) - quarterly data from Eurostat",
            "origin": {"name": "Average actual weekly hours worked in the main job by professional status and economic activity"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_cross_tab_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_not_employed_cross_tabs() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Eurostat",
            "query": "Germany Number of not employed persons who would have stayed longer at work (or not) if more flexible working time arrangements had been available - by sex and occupation (previous job) (1 000) from Eurostat",
            "origin": {"name": "Number of not employed persons who would have stayed longer at work by sex and occupation"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_cross_tab_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_colonoscopy_cross_tabs() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Eurostat",
            "query": "Spain Self-reported last colonoscopy by sex age and degree of urbanisation from Eurostat",
            "origin": {"name": "Self-reported last colonoscopy by sex age and degree of urbanisation"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_cross_tab_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_health_enhancing_activity_cross_tabs() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "Eurostat",
            "query": "Italy Performing health-enhancing physical activity by sex from Eurostat",
            "origin": {"name": "Performing health-enhancing physical activity by sex"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_cross_tab_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_fred_hicp_catalog_family_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "FRED",
            "query": "US Consumer Price Indices (CPIs HICPs) from FRED",
            "origin": {
                "name": "Consumer Price Indices (CPIs, HICPs), COICOP 1999: Consumer Price Index: Water Supply and Miscellaneous Services Relating to the Dwelling for Luxembourg",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "fred_hicp_catalog_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_coingecko_slug_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "CoinGecko",
            "query": "grif_gg cryptocurrency price from CoinGecko",
            "origin": {"name": "grif_gg"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "coin_slug_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_coingecko_old_asset_queries() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "CoinGecko",
            "query": "Drife [OLD] cryptocurrency price from CoinGecko",
            "origin": {"name": "Drife [OLD]"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "coin_slug_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_low_viability_obscure_coin_names() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "CoinGecko",
            "query": "Dagknight Dog cryptocurrency price from CoinGecko",
            "origin": {
                "name": "Dagknight Dog",
                "source_indicator_code": "dogk",
                "category": "Cryptocurrency",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "coin_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_public_sector_revenue_indicator_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Government and Public Sector Finance from IMF",
            "origin": {
                "name": "Government and Public Sector Finance, Revenue General Government [2014 Manual], National Currency",
                "source_indicator_code": "GR_G14_GG_XDC",
                "category": "INDICATOR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_monetary_aggregate_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany M4 Monetary from IMF",
            "origin": {
                "name": "Monetary, M4, National Currency",
                "source_indicator_code": "FM4_XDC",
                "category": "INDICATOR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_worldbank_archive_and_pefa_sources() -> None:
    archive = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India Cost of a nutrient adequate diet in PPP dollars from World Bank",
            "origin": {
                "name": "Cost of a nutrient adequate diet in PPP dollars",
                "source_indicator_code": "CoNA_PPP",
                "category": "FPN Datahub Archive",
            },
        }
    )
    pefa = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "India Existence and operation of a procurement complaints mechanism from World Bank",
            "origin": {
                "name": "(vii) Existence and operation of a procurement complaints mechanism",
                "source_indicator_code": "PI-19.7",
                "category": "PEFA 2011",
            },
        }
    )

    assert archive["risk_level"] == "high"
    assert "worldbank_niche_catalog_family" in archive["reasons"]
    assert pefa["risk_level"] == "high"
    assert "worldbank_specialized_source_family" in pefa["reasons"]


def test_audit_direct_query_shape_flags_imf_public_enterprises_operation_balance() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany General Government Fiscal operation balance (public enterprises) from IMF",
            "origin": {
                "name": "Fiscal, General Government, operation balance (public enterprises)",
                "source_indicator_code": "HN_GGO_B_094",
                "category": "ALT_FISCAL",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_bop_reserve_and_external_debt_families() -> None:
    reserve = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Financial Account Reserve assets Securities Balance of Payments Other reserve assets from IMF",
            "origin": {
                "name": "Balance of Payments, Financial Account, Reserve assets, Other reserve assets, Securities, Equity and investment fund shares [BPM6], National Currency",
                "source_indicator_code": "BFRAOSE_BP6_XDC",
                "category": "INDICATOR",
            },
        }
    )
    debt = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Central Bank Long-term External Debt Exchange rate changes Special drawing rights allocations from IMF",
            "origin": {
                "name": "External Debt, Central Bank, Exchange rate changes, Long-term, Special drawing rights (allocations), BPM6, US Dollars",
                "source_indicator_code": "DCB_FXCLS_BP6_USD",
                "category": "INDICATOR",
            },
        }
    )

    assert reserve["risk_level"] == "high"
    assert "imf_low_viability_family" in reserve["reasons"]
    assert debt["risk_level"] == "high"
    assert "imf_low_viability_family" in debt["reasons"]


def test_audit_direct_query_shape_flags_imf_public_finance_expenditure_family() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Expenditure General Government Expense Subsidies Government and Public Sector Finance from IMF",
            "origin": {
                "name": "Government and Public Sector Finance, Expenditure, General Government, Expense, Subsidies, To Private Enterprises [2001 Manual], National Currency",
                "source_indicator_code": "GGESTP_G01_XDC",
                "category": "INDICATOR",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]



def test_audit_direct_query_shape_flags_imf_bop_external_debt_and_iip_families() -> None:
    bop = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Services Rail Transport Freight Net Balance of Payments Extended Classification of Other Transport from IMF",
            "origin": {
                "name": "Balance of Payments, Services, Extended Classification of Other Transport, Rail Transport, Freight, Net, National Currency",
                "source_indicator_code": "BSOOTRF_XDC",
                "category": "INDICATOR",
            },
        }
    )
    debt = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Other sectors Short-term External Debt External assets in debt instruments Currency and deposits from IMF",
            "origin": {
                "name": "External Debt, Other sectors, External assets in debt instruments, Short-term, Currency and deposits, BPM6, US Dollars",
                "source_indicator_code": "DODS_SDIC_BP6_USD",
                "category": "INDICATOR",
            },
        }
    )
    iip = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Assets Other investment Loans General government International Investment Position from IMF",
            "origin": {
                "name": "International Investment Position, Assets, Other investment, Loans, General government, Credit and loans with the IMF (other than reserves) [BPM6], Euros",
                "source_indicator_code": "IAOLNGIMF_BP6_EUR",
                "category": "INDICATOR",
            },
        }
    )

    for audit in (bop, debt, iip):
        assert audit["risk_level"] == "high"
        assert "imf_complex_finance_family" in audit["reasons"]
        assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_definition_and_central_government_fiscal_families() -> None:
    interest_rate_definition = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Interest Rates Congo Democratic Rep. Definition BCC CREDIT RATE -at 84 days Percent Per Annum from IMF",
            "origin": {
                "name": "Congo Democratic Rep. Definition, Interest Rates, BCC CREDIT RATE -at 84 days, Percent Per Annum",
                "source_indicator_code": "COD_INR_BC_84J_PA",
                "category": "INDICATOR",
            },
        }
    )
    fiscal_definition = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Fiscal Expense Cash Rwanda Definition Use of goods and services from IMF",
            "origin": {
                "name": "Rwanda Definition, Fiscal, Expense, Use of goods and services, 2014 Manual, Cash, National Currency",
                "source_indicator_code": "RWA_GEGS_G14_CA_XDC",
                "category": "INDICATOR",
            },
        }
    )
    central_government_tax = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Central Government Revenue Tax Individuals Cash Fiscal Income and Profits from IMF",
            "origin": {
                "name": "Fiscal, Central Government, Revenue, Tax, Income and Profits, Individuals, 2001 Manual, Cash, National Currency",
                "source_indicator_code": "GCRTII_G01_CA_XDC",
                "category": "INDICATOR",
            },
        }
    )

    for audit in (interest_rate_definition, fiscal_definition, central_government_tax):
        assert audit["risk_level"] == "high"
        assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_monetary_fiscal_interest_and_gva_families() -> None:
    monetary = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Monetary and Financial Accounts Central Bank Survey Other Items Net Share and Alternate from IMF",
            "origin": {
                "name": "Monetary and Financial Accounts, Central Bank Survey, Other Items (Net), Share and Alternate, National Currency",
                "source_indicator_code": "FASEOALT_XDC",
                "category": "INDICATOR",
            },
        }
    )
    fiscal = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Total Outlays Economic Affairs Electricity Cash Fiscal Budgetary Central Government Fuel and Energy from IMF",
            "origin": {
                "name": "Fiscal, Budgetary Central Government, Total Outlays, Economic Affairs, Fuel and Energy, Electricity, 2001 Manual, Cash, US Dollars",
                "source_indicator_code": "GBEAFE_G01_CA_USD",
                "category": "INDICATOR",
            },
        }
    )
    interest = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Interest Rates 364 Days Financial Treasury Bills Monthly Average Discount Factor Percent per Annum from IMF",
            "origin": {
                "name": "Financial, Interest Rates, Treasury Bills (Monthly Average Discount Factor), 364 Days, Percent per Annum",
                "source_indicator_code": "FITBDF_12M_PA",
                "category": "INDICATOR",
            },
        }
    )
    gva = audit_direct_query_shape(
        {
            "provider": "IMF",
            "query": "Germany Real Seasonally Adjusted Gross Value Added Real estate activities from IMF",
            "origin": {
                "name": "National Accounts, Gross Value Added, Real, Seasonally Adjusted, Real estate activities, ISIC Rev. 4, US Dollars",
                "source_indicator_code": "NGDPVA_R_ISIC4_L_SA_USD",
                "category": "INDICATOR",
            },
        }
    )

    for audit in (monetary, fiscal, interest, gva):
        assert audit["risk_level"] == "high"
        assert "imf_low_viability_family" in audit["reasons"]

def test_audit_direct_query_shape_flags_worldbank_doing_business_methodology() -> None:
    audit = audit_direct_query_shape(
        {
            "provider": "WorldBank",
            "query": "Brazil Time to export days DB06-15 methodology from World Bank",
            "origin": {
                "name": "Time to export (days) (DB06-15 methodology)",
                "source_indicator_code": "TRD.ACRS.BRDR.EXPT.DURS.DY.DB0615",
                "category": "Doing Business",
            },
        }
    )

    assert audit["risk_level"] == "high"
    assert "worldbank_specialized_source_family" in audit["reasons"]
