from __future__ import annotations

from scripts.validation.common import audit_direct_query_shape, default_query_for_row


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
