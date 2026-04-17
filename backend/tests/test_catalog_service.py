from __future__ import annotations

from backend.services.catalog_service import (
    find_concept_by_term,
    get_best_provider,
    get_indicator_code,
    get_variant_for_query,
    reload_catalog,
)


def test_debt_service_ratio_maps_to_debt_service_concept():
    reload_catalog()
    concept = find_concept_by_term("debt service ratio in china")
    assert concept == "debt_service_ratio"


def test_debt_to_gdp_maps_to_government_debt():
    reload_catalog()
    concept = find_concept_by_term("government debt to gdp ratio")
    assert concept == "government_debt"


def test_import_share_of_gdp_maps_to_imports():
    reload_catalog()
    concept = find_concept_by_term("import share of gdp in china and us")
    assert concept == "imports"


def test_export_to_gdp_typo_maps_to_exports():
    reload_catalog()
    concept = find_concept_by_term("export to gdp ration in china and uk")
    assert concept == "exports"


def test_unemployment_rate_not_blocked_by_employment_exclusion():
    reload_catalog()
    concept = find_concept_by_term("US unemployment rate monthly during 2020 to 2024")
    assert concept == "unemployment"


def test_employment_rate_maps_to_employment_rate_concept():
    reload_catalog()
    concept = find_concept_by_term("Canada employment rate")
    assert concept == "employment_rate"


def test_employment_to_population_ratio_maps_to_specific_ratio_concept():
    reload_catalog()
    concept = find_concept_by_term("employment to population ratio in Canada")
    assert concept == "employment_to_population_ratio"


def test_federal_funds_target_rate_maps_to_interest_rate():
    reload_catalog()
    concept = find_concept_by_term("Federal funds target rate history since 2005")
    assert concept == "interest_rate"


def test_housing_starts_with_building_permits_maps_to_housing_starts():
    reload_catalog()
    concept = find_concept_by_term("US housing starts and building permits trend since 2016")
    assert concept == "housing_starts"


def test_altcoin_price_query_maps_to_cryptocurrency():
    reload_catalog()
    concept = find_concept_by_term("XRP price performance over the last 6 months")
    assert concept == "cryptocurrency"


def test_wages_query_maps_to_wages_concept():
    reload_catalog()
    concept = find_concept_by_term("Average wages and earnings trend in the US since 2016")
    assert concept == "wages"


def test_gdp_deflator_query_maps_to_gdp_deflator_concept():
    reload_catalog()
    concept = find_concept_by_term("GDP deflator inflation in Germany between 2012 and 2024")
    assert concept == "gdp_deflator"


def test_hicp_query_maps_to_hicp_inflation_concept():
    reload_catalog()
    concept = find_concept_by_term("HICP inflation in euro area countries 2019 to 2024")
    assert concept == "hicp_inflation"


def test_current_account_query_maps_to_current_account_concept():
    reload_catalog()
    concept = find_concept_by_term("Compare current account balances for energy importers versus exporters")
    assert concept == "current_account"


def test_get_best_provider_handles_oecd_coverage_labels_with_comments():
    reload_catalog()
    provider, code, _ = get_best_provider("gdp_growth", countries=["CA"], preferred_provider="OECD")
    assert provider == "OECD"
    assert code == "DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD"


def test_get_best_provider_handles_44_country_coverage_for_bis():
    reload_catalog()
    provider, code, _ = get_best_provider("household_debt", countries=["DE"])
    assert provider == "BIS"
    assert code == "WS_TC"


def test_get_best_provider_prefers_eurostat_for_hicp_inflation():
    reload_catalog()
    provider, code, _ = get_best_provider("hicp_inflation", countries=["DE"])
    assert provider == "Eurostat"
    assert code == "PRC_HICP_AIND"


def test_get_best_provider_prefers_bis_for_real_effective_exchange_rate():
    reload_catalog()
    provider, code, _ = get_best_provider("real_effective_exchange_rate", countries=["JP"])
    assert provider == "BIS"
    assert code == "WS_EER"


def test_get_best_provider_prefers_global_coverage_without_country_context():
    reload_catalog()
    provider, code, _ = get_best_provider("gdp_per_capita")
    assert provider == "WorldBank"
    assert code == "NY.GDP.PCAP.CD"


def test_real_effective_exchange_rate_worldbank_fallback_code_exists():
    reload_catalog()
    code = get_indicator_code("real_effective_exchange_rate", "WorldBank")
    assert code == "PX.REX.REER"


def test_oil_price_maps_to_oil_price_concept():
    reload_catalog()
    concept = find_concept_by_term("Brent oil price")
    assert concept == "oil_price"


def test_oil_price_variant_lookup_prefers_brent_series():
    reload_catalog()
    code, confidence = get_variant_for_query("oil_price", "FRED", "Brent oil price")
    assert code == "DCOILBRENTEU"
    assert confidence >= 0.9


def test_oil_price_variant_lookup_prefers_wti_series():
    reload_catalog()
    code, confidence = get_variant_for_query("oil_price", "FRED", "WTI oil price")
    # WTI already matches the catalog primary, so no variant override is needed.
    assert code is None
    assert confidence == 0.0
