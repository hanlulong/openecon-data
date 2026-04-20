from __future__ import annotations

from scripts.validation.common import (
    audit_direct_query_shape,
    category_success_adjustment,
    detect_single_country_from_text,
    default_query_for_row,
    family_success_adjustment,
    heuristic_subfamily_adjustment,
    preferred_default_country_for_record,
    preferred_default_country,
    provider_family_key,
    provider_subfamily_key,
    subfamily_success_adjustment,
)


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

    assert "exports of iron and steel" in query.lower()
    assert "from comtrade" in query.lower()


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

    assert query == "Bank Deposits to GDP for Japan from FRED"


def test_default_query_for_row_prefers_country_in_origin_name_over_provider_default() -> None:
    row = {
        "provider": "FRED",
        "code": "CPTEST",
        "name": "Consumer Price Indices (CPIs, HICPs), COICOP 1999: Consumer Price Index: Total for Luxembourg",
        "description": "",
    }

    query = default_query_for_row(row)

    assert query.lower().startswith("luxembourg ")
    assert "united states" not in query.lower()


def test_detect_single_country_from_text_extracts_named_country() -> None:
    assert detect_single_country_from_text("Consumer Price Index for Luxembourg") == "Luxembourg"


def test_detect_single_country_from_text_ignores_ambiguous_short_aliases() -> None:
    assert detect_single_country_from_text("household composition and degree of urbanisation") is None


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

    assert "physicians by status" in query.lower()
    assert query.lower().endswith("from oecd")


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


def test_audit_direct_query_shape_flags_oecd_publication_table_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Japan Africa's Development Dynamics (AfDD) Table 36 - Employment by business activity and skill level from OECD",
            "origin": {"name": "Africa's Development Dynamics (AfDD) Table 36 - Employment by business activity and skill level"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "oecd_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_eurostat_vine_breakdown_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Area under wine-grape vine varieties broken down by vine variety and by age of the vines - Cyprus from Eurostat",
            "origin": {"name": "Area under wine-grape vine varieties broken down by vine variety and by age of the vines - Cyprus"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "eurostat_agri_breakdown_query" in audit["reasons"]


def test_audit_direct_query_shape_flags_imf_debt_schedule_queries():
    audit = audit_direct_query_shape(
        {
            "query": "Japan Other Sectors Principal External Debt Debt-service Payment schedule More than 9 and up to 12 months from IMF",
            "origin": {"name": "External Debt, Other Sectors, Debt-service Payment schedule, More than 9 and up to 12 months, Principal"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "imf_low_viability_family" in audit["reasons"]


def test_audit_direct_query_shape_flags_fred_naics_revenue_queries():
    audit = audit_direct_query_shape(
        {
            "query": "US Total Revenue for 6211: Offices of Physicians - Taxable Establishments Subject to Federal Income Tax from FRED",
            "origin": {"name": "Total Revenue for 6211: Offices of Physicians - Taxable, Establishments Subject to Federal Income Tax"},
        }
    )

    assert audit["risk_level"] == "high"
    assert "fred_low_viability_family" in audit["reasons"]


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
