from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validation.materialize_next_review_batch import select_quality_screened_direct_records


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "materialize_next_review_batch.py"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_materialize_next_review_batch_writes_expected_counts(tmp_path: Path):
    batch_plan = tmp_path / "batch.json"
    snapshot = tmp_path / "snapshot.json"
    output_dir = tmp_path / "out"

    write_json(
        batch_plan,
        {
            "allocation": {
                "direct": {
                    "targets": [
                        {"name": "FRED", "planned_batch_sessions": 2, "target_n": 10},
                        {"name": "IMF", "planned_batch_sessions": 1, "target_n": 8},
                    ]
                },
                "multiround": {
                    "targets": [
                        {"name": "transform_switch_chain", "planned_batch_sessions": 2, "target_n": 10},
                    ]
                },
                "ambiguity": {
                    "targets": [
                        {"name": "dominant_interpretation_cases", "planned_batch_sessions": 3, "target_n": 12},
                    ]
                },
            }
        },
    )
    write_json(
        snapshot,
        {
            "snapshot_date": "2026-04-14",
            "git_sha": "abc12345",
            "indicator_count": 330050,
            "provider_counts": {
                "FRED": 138774,
                "IMF": 115381,
            },
        },
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--batch-plan",
            str(batch_plan),
            "--snapshot",
            str(snapshot),
            "--output-dir",
            str(output_dir),
            "--dataset-tier",
            "dev",
            "--holdout-split",
            "batch_review",
        ],
        check=True,
    )

    direct_rows = read_jsonl(output_dir / "next_batch_direct.jsonl")
    multiround_rows = read_jsonl(output_dir / "next_batch_multiround.jsonl")
    ambiguity_rows = read_jsonl(output_dir / "next_batch_ambiguity.jsonl")

    assert len(direct_rows) == 3
    assert len(multiround_rows) == 2
    assert len(ambiguity_rows) == 3
    assert direct_rows[0]["provenance"]["holdout_split"] == "batch_review"
    assert direct_rows[0]["provenance"]["batch_plan"] == "next_review_batch"
    assert "description" in direct_rows[0]["origin"]
    assert "raw_metadata" in direct_rows[0]["origin"]
    assert multiround_rows[0]["family"] == "transform_switch_chain"
    assert ambiguity_rows[0]["provenance"]["family"] == "dominant_interpretation_cases"


def test_materialize_next_review_batch_handles_empty_targets(tmp_path: Path):
    batch_plan = tmp_path / "batch.json"
    snapshot = tmp_path / "snapshot.json"
    output_dir = tmp_path / "out"

    write_json(batch_plan, {"allocation": {"direct": {"targets": []}, "multiround": {"targets": []}, "ambiguity": {"targets": []}}})
    write_json(snapshot, {"snapshot_date": "2026-04-14", "git_sha": "abc12345", "indicator_count": 330050, "provider_counts": {}})

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--batch-plan",
            str(batch_plan),
            "--snapshot",
            str(snapshot),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
    )

    assert read_jsonl(output_dir / "next_batch_direct.jsonl") == []
    assert read_jsonl(output_dir / "next_batch_multiround.jsonl") == []
    assert read_jsonl(output_dir / "next_batch_ambiguity.jsonl") == []


def test_select_quality_screened_direct_records_prefers_low_risk():
    records = [
        {"id": "high-1", "query": "very long", "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["catalog_jargon"]}},
        {"id": "medium-1", "query": "medium", "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]}},
        {"id": "low-1", "query": "short", "provenance": {"query_quality_risk": "low", "query_quality_reasons": []}},
        {"id": "low-2", "query": "shorter", "provenance": {"query_quality_risk": "low", "query_quality_reasons": []}},
    ]

    selected = select_quality_screened_direct_records(records, 2)

    assert [row["id"] for row in selected] == ["low-1", "low-2"]


def test_select_quality_screened_direct_records_prefers_more_specific_low_risk_rows():
    records = [
        {
            "id": "generic-1",
            "query": "France Immigration from Eurostat",
            "origin": {"name": "Immigration", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
        {
            "id": "specific-1",
            "query": "Italy Practising dentists from Eurostat",
            "origin": {"name": "Practising dentists", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["specific-1"]


def test_select_quality_screened_direct_records_avoids_high_risk_oecd_methodology_titles():
    records = [
        {
            "id": "oecd-dense",
            "query": "US $ current prices current PPPs Annual net national income per capita from OECD",
            "origin": {
                "name": "Annual net national income per capita, US $, current prices, current PPPs",
                "description": "",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["methodology_dense"]},
        },
        {
            "id": "oecd-clear",
            "query": "Germany Water use from OECD",
            "origin": {"name": "Water use", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["oecd-clear"]


def test_select_quality_screened_direct_records_can_prefer_medium_risk_when_specificity_is_much_stronger():
    records = [
        {
            "id": "oecd-generic-low",
            "query": "Germany Water use from OECD",
            "origin": {"name": "Water use", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
        {
            "id": "oecd-specific-medium",
            "query": "Canada All countries National CPI Growth rate over one year All items less food and energy from OECD",
            "origin": {
                "name": "National CPI, Growth rate over one year, All items less food and energy",
                "description": "",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["acronym_dense"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["oecd-specific-medium"]


def test_select_quality_screened_direct_records_prefers_simple_coingecko_assets_over_complex_slugs():
    records = [
        {
            "id": "coingecko-complex",
            "query": "treasury-bond-eth-tokenized-stock-defichain cryptocurrency price from CoinGecko",
            "provider_stratum": "CoinGecko",
            "origin": {
                "name": "iShares 20+ Year Treasury Bond ETF Defichain",
                "description": "",
                "source_provider": "CoinGecko",
                "source_indicator_code": "treasury-bond-eth-tokenized-stock-defichain",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
        {
            "id": "coingecko-simple",
            "query": "Sushi cryptocurrency price from CoinGecko",
            "provider_stratum": "CoinGecko",
            "origin": {
                "name": "Sushi",
                "description": "",
                "source_provider": "CoinGecko",
                "source_indicator_code": "sushi",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["coingecko-simple"]


def test_select_quality_screened_direct_records_avoids_fred_regional_price_slices_when_generic_series_exists():
    records = [
        {
            "id": "fred-regional-price",
            "query": "US Average Price: Pork Sirloin Roast Bone-In (Cost per Pound/453.6 Grams) in the South Census Region - Urban from FRED",
            "provider_stratum": "FRED",
            "origin": {"name": "Average Price: Pork Sirloin Roast Bone-In", "description": ""},
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["multi_modifier_title"]},
        },
        {
            "id": "fred-generic",
            "query": "US Consumer Price Indices (CPIs HICPs) from FRED",
            "provider_stratum": "FRED",
            "origin": {"name": "Consumer Price Indices (CPIs HICPs)", "description": ""},
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["fred-generic"]


def test_select_quality_screened_direct_records_prefers_worldbank_literacy_over_binary_policy_query():
    records = [
        {
            "id": "worldbank-binary-policy",
            "query": "Brazil Sons and daughters have equal rights to inherit assets from their parents (1=yes; 0=no) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {"name": "Sons and daughters have equal rights to inherit assets from their parents (1=yes; 0=no)", "description": ""},
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["worldbank_binary_policy_query"]},
        },
        {
            "id": "worldbank-functional-difficulty",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {"name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)", "description": ""},
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["worldbank-functional-difficulty"]


def test_select_quality_screened_direct_records_prefers_oecd_share_of_students_over_cpi_energy_bundle():
    records = [
        {
            "id": "oecd-cpi-bundle",
            "query": "Canada All countries National CPI Growth rate over one year All items less food and energy from OECD",
            "provider_stratum": "OECD",
            "origin": {
                "name": "National CPI, Growth rate over one year, All items less food and energy",
                "description": "",
                "source_provider": "OECD",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query", "acronym_dense", "multi_modifier_title"]},
        },
        {
            "id": "oecd-student-share",
            "query": "Japan Share of students enrolled in school and work-based programmes from OECD",
            "provider_stratum": "OECD",
            "origin": {
                "name": "Share of students enrolled in school and work-based programmes",
                "description": "",
                "source_provider": "OECD",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["oecd-student-share"]


def test_select_quality_screened_direct_records_prefers_worldbank_literacy_over_attendance_variant():
    records = [
        {
            "id": "worldbank-attendance",
            "query": "Japan rural Adjusted net attendance rate one year before the official primary entry age male (%) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Adjusted net attendance rate one year before the official primary entry age, male",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query", "multi_modifier_title"]},
        },
        {
            "id": "worldbank-literacy",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["worldbank-literacy"]


def test_select_quality_screened_direct_records_prefers_worldbank_completion_over_education_expenditure_family():
    records = [
        {
            "id": "worldbank-education-exp",
            "query": "Germany World Bank: Share of household consumption for private expenditures on primary education (%) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "World Bank: Share of household consumption for private expenditures on primary education (%)",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["worldbank_education_expenditure_family"]},
        },
        {
            "id": "worldbank-completion",
            "query": "India male Completion rate lower secondary education adjusted location parity index (LPIA) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Completion rate, lower secondary education, adjusted location parity index (LPIA), male",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["worldbank-completion"]


def test_select_quality_screened_direct_records_prefers_worldbank_literacy_over_assessment_family():
    records = [
        {
            "id": "worldbank-assessment",
            "query": "China Rural Above Proficiency;SEA-PLM 2019 for grade 5 using MPL Level 6 for reading from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Rural Above Proficiency;SEA-PLM 2019 for grade 5 using MPL Level 6 for reading",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["worldbank_assessment_family"]},
        },
        {
            "id": "worldbank-literacy-2",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["worldbank-literacy-2"]


def test_select_quality_screened_direct_records_prefers_completion_over_specialized_worldbank_source():
    records = [
        {
            "id": "worldbank-qeds",
            "query": "China All instruments USD Ext. Debt Service Pmt DI: Intercom Lending More than 18 to 24 Prin. and Int. from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "All instruments, USD, Ext. Debt Service Pmt, DI: Intercom Lending, More than 18 to 24, Prin. and Int.",
                "description": "",
                "category": "Quarterly External Debt Statistics SDDS",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["worldbank_specialized_source_family"]},
        },
        {
            "id": "worldbank-completion-2",
            "query": "India male Completion rate lower secondary education adjusted location parity index (LPIA) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Completion rate, lower secondary education, adjusted location parity index (LPIA), male",
                "description": "",
                "category": "Education Statistics",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["worldbank-completion-2"]


def test_select_quality_screened_direct_records_prefers_literacy_over_ddh_prevalence_family():
    records = [
        {
            "id": "worldbank-ddh-prevalence",
            "query": "India Adjusted prevalence of male persons with some degree of mobility difficulty (% of male persons) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Adjusted prevalence of male persons with some degree of mobility difficulty (% of male persons)",
                "description": "",
                "category": "Disability Data Hub (DDH)",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["worldbank_ddh_prevalence_family"]},
        },
        {
            "id": "worldbank-literacy-3",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "category": "Disability Data Hub (DDH)",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["worldbank-literacy-3"]


def test_select_quality_screened_direct_records_caps_worldbank_family_duplicates():
    records = [
        {
            "id": "worldbank-completion-a",
            "query": "India male Completion rate lower secondary education adjusted location parity index (LPIA) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Completion rate, lower secondary education, adjusted location parity index (LPIA), male",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
        {
            "id": "worldbank-completion-b",
            "query": "China female Completion rate lower secondary education adjusted location parity index (LPIA) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Completion rate, lower secondary education, female, adjusted location parity index (LPIA)",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
        {
            "id": "worldbank-literacy-a",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 2)

    ids = [row["id"] for row in selected]
    assert "worldbank-literacy-a" in ids
    assert len([row_id for row_id in ids if row_id.startswith("worldbank-completion-")]) == 1


def test_select_quality_screened_direct_records_prefers_oecd_student_share_over_publication_table_query():
    records = [
        {
            "id": "oecd-afdd",
            "query": "Japan Africa's Development Dynamics (AfDD) Table 36 - Employment by business activity and skill level from OECD",
            "provider_stratum": "OECD",
            "origin": {
                "name": "Africa's Development Dynamics (AfDD) Table 36 - Employment by business activity and skill level",
                "description": "",
                "source_provider": "OECD",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["oecd_low_viability_family"]},
        },
        {
            "id": "oecd-student-share-2",
            "query": "Japan Share of students enrolled in school and work-based programmes from OECD",
            "provider_stratum": "OECD",
            "origin": {
                "name": "Share of students enrolled in school and work-based programmes",
                "description": "",
                "source_provider": "OECD",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["oecd-student-share-2"]


def test_select_quality_screened_direct_records_prefers_eurostat_household_composition_over_vine_breakdown():
    records = [
        {
            "id": "eurostat-vines",
            "query": "Area under wine-grape vine varieties broken down by vine variety and by age of the vines - Germany from Eurostat",
            "provider_stratum": "Eurostat",
            "origin": {
                "name": "Area under wine-grape vine varieties broken down by vine variety and by age of the vines - Germany",
                "description": "",
                "source_provider": "Eurostat",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["eurostat_agri_breakdown_query"]},
        },
        {
            "id": "eurostat-household",
            "query": "Germany household composition degree of urbanisation and frequency from Eurostat",
            "provider_stratum": "Eurostat",
            "origin": {
                "name": "Persons communicating via social media by income quintile, household composition, degree of urbanisation and frequency",
                "description": "",
                "source_provider": "Eurostat",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["eurostat-household"]


def test_select_quality_screened_direct_records_prefers_imf_consumer_prices_over_debt_schedule():
    records = [
        {
            "id": "imf-debt-schedule",
            "query": "Japan Other Sectors Principal External Debt Debt-service Payment schedule More than 9 and up to 12 months from IMF",
            "provider_stratum": "IMF",
            "origin": {
                "name": "External Debt, Other Sectors, Debt-service Payment schedule, More than 9 and up to 12 months, Principal",
                "description": "",
                "source_provider": "IMF",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["imf_low_viability_family"]},
        },
        {
            "id": "imf-cpi",
            "query": "United States Consumer Price Index Food and non-alcoholic beverages Base Year = 2005 from IMF",
            "provider_stratum": "IMF",
            "origin": {
                "name": "Prices, Consumer Price Index, Food and non-alcoholic beverages, COICOP, Base Year = 2005, Index",
                "description": "",
                "source_provider": "IMF",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query", "multi_modifier_title"]},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["imf-cpi"]


def test_select_quality_screened_direct_records_prefers_fred_cpi_over_naics_revenue():
    records = [
        {
            "id": "fred-revenue",
            "query": "US Total Revenue for 6211: Offices of Physicians - Taxable Establishments Subject to Federal Income Tax from FRED",
            "provider_stratum": "FRED",
            "origin": {
                "name": "Total Revenue for 6211: Offices of Physicians - Taxable, Establishments Subject to Federal Income Tax",
                "description": "",
                "source_provider": "FRED",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["fred_low_viability_family"]},
        },
        {
            "id": "fred-cpi",
            "query": "US Consumer Price Indices (CPIs HICPs) from FRED",
            "provider_stratum": "FRED",
            "origin": {
                "name": "Consumer Price Indices (CPIs HICPs)",
                "description": "",
                "source_provider": "FRED",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["fred-cpi"]
