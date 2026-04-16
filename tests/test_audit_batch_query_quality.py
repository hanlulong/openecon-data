from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "audit_batch_query_quality.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_audit_batch_query_quality_flags_catalog_like_direct_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany Learning Deprivation Gap;PISA 2018 for grade 15Y using MPL Level 2 for reading, Fourth Quintile",
                "origin": {
                    "name": "Learning Deprivation Gap;PISA 2018 for grade 15Y using MPL Level 2 for reading, Fourth Quintile"
                },
            },
            {
                "id": "direct-2",
                "query": "US GDP",
                "origin": {
                    "name": "Gross Domestic Product"
                },
            },
            {
                "id": "amb-1",
                "query": "interest rate",
                "expected_behavior": "clarify",
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["row_count"] == 3
    assert report["summary"]["high_risk_rows"] == 1
    assert report["summary"]["reason_counts"]["catalog_jargon"] >= 1
    flagged = report["flagged_rows"][0]
    assert flagged["id"] == "direct-1"
    assert flagged["risk_level"] == "high"
    assert "provider_title_like" in flagged["reasons"] or "catalog_jargon" in flagged["reasons"]


def test_audit_batch_query_quality_handles_multiple_datasets(tmp_path: Path):
    dataset_a = tmp_path / "a.jsonl"
    dataset_b = tmp_path / "b.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(dataset_a, [{"id": "direct-1", "query": "US GDP", "origin": {"name": "Gross Domestic Product"}}])
    write_jsonl(dataset_b, [{"id": "direct-2", "query": "Canada MICS: Something, Urban", "origin": {"name": "MICS: Something, Urban"}}])

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset_a),
            "--dataset",
            str(dataset_b),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["row_count"] == 2
    assert report["summary"]["by_type"]["direct"] == 2
    assert report["summary"]["high_risk_rows"] == 1


def test_audit_batch_query_quality_flags_scenario_and_methodology_dense_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Canada Climate projections by scenario 2030–2060 – Cities and FUAs from OECD",
                "origin": {
                    "name": "Climate projections by scenario, 2030–2060 – Cities and FUAs"
                },
            },
            {
                "id": "direct-2",
                "query": "US $ current prices current PPPs Annual net national income per capita from OECD",
                "origin": {
                    "name": "Annual net national income per capita, US $, current prices, current PPPs"
                },
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 2
    reasons = {row["id"]: set(row["reasons"]) for row in report["flagged_rows"]}
    assert "scenario_projection_query" in reasons["direct-1"]
    assert "methodology_dense" in reasons["direct-2"]


def test_audit_batch_query_quality_flags_accounting_subtotal_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany Exchange Difference Sub Total Federation Income and Distribution from IMF",
                "origin": {
                    "name": "Federation Income and Distribution, Exchange Difference, Sub Total"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "accounting_artifact_query" in flagged["reasons"]


def test_audit_batch_query_quality_flags_indicator_code_prefixed_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Brazil 1101130:Fish and seafood from World Bank",
                "origin": {
                    "name": "1101130:Fish and seafood"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "indicator_code_prefix" in flagged["reasons"]


def test_audit_batch_query_quality_flags_ambiguous_subnational_abbreviations(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "US AL Market Hotness: Page View Count per Property in Jefferson County from FRED",
                "origin": {
                    "name": "AL Market Hotness: Page View Count per Property in Jefferson County"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "subnational_abbrev_ambiguous" in flagged["reasons"]


def test_audit_batch_query_quality_avoids_false_country_conflicts_on_common_words(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "A woman can apply for a passport in the same way as a man from World Bank",
                "origin": {
                    "name": "A woman can apply for a passport in the same way as a man (1=yes; 0=no)"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 0


def test_audit_batch_query_quality_flags_socioeconomic_slice_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Japan Borrowed to start or operate a business poorest 40% (% age 15+) from World Bank",
                "origin": {
                    "name": "Borrowed to start or operate a business, poorest 40% (% age 15+)"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "socioeconomic_slice" in flagged["reasons"]


def test_audit_batch_query_quality_flags_survey_micro_slice_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Japan Borrowed to start or operate a business poorest 40% (% age 15+) from World Bank",
                "origin": {
                    "name": "Borrowed to start or operate a business, poorest 40% (% age 15+)"
                },
            },
            {
                "id": "direct-2",
                "query": "Germany Palm Oil Land Area by type of ownership: Private (in Hectares) from World Bank",
                "origin": {
                    "name": "Palm Oil Land Area by type of ownership: Private (in Hectares)"
                },
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 2
    reasons = {row["id"]: set(row["reasons"]) for row in report["flagged_rows"]}
    assert "survey_micro_slice" in reasons["direct-1"]
    assert "ownership_breakdown_query" in reasons["direct-2"]


def test_audit_batch_query_quality_flags_mobile_phone_age_slice_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany No mobile phone due to personal safety concerns (% age 15+) from World Bank",
                "origin": {
                    "name": "No mobile phone due to personal safety concerns (% age 15+)"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "survey_micro_slice" in flagged["reasons"]


def test_audit_batch_query_quality_flags_worldbank_niche_breakdown_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany aged 15-64 total Public to private wage gap from World Bank",
                "origin": {"name": "Public to private wage gap, aged 15-64, total"},
            },
            {
                "id": "direct-2",
                "query": "Net official flows from UN agencies UNPBF (current US$) from World Bank",
                "origin": {"name": "Net official flows from UN agencies, UNPBF (current US$)"},
            },
            {
                "id": "direct-3",
                "query": "Japan Small firms with a bank loan or line of credit (%) from World Bank",
                "origin": {"name": "Small firms with a bank loan or line of credit (%)"},
            },
            {
                "id": "direct-4",
                "query": "China total short term 15_Debt securities held by nonresidents from World Bank",
                "origin": {"name": "15_Debt securities held by nonresidents, total, short term"},
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 4
    reasons = {row["id"]: set(row["reasons"]) for row in report["flagged_rows"]}
    assert "gap_subgroup_query" in reasons["direct-1"]
    assert "official_flow_subseries" in reasons["direct-2"]
    assert "financial_inclusion_slice" in reasons["direct-3"]
    assert "holder_term_breakdown_query" in reasons["direct-4"]


def test_audit_batch_query_quality_flags_worldbank_niche_catalog_families(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany No mobile phone due to personal safety concerns (% age 15+) from World Bank",
                "origin": {
                    "name": "No mobile phone due to personal safety concerns (% age 15+)",
                    "category": "Global Findex database",
                },
            },
            {
                "id": "direct-2",
                "query": "India GOAL 11 Sustainable Cities and Communities (5 year moving average) from World Bank",
                "origin": {
                    "name": "GOAL 11: Sustainable Cities and Communities (5 year moving average)",
                    "category": "Statistical Performance Indicators (SPI)",
                },
            },
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 2
    reasons = {row["id"]: set(row["reasons"]) for row in report["flagged_rows"]}
    assert "worldbank_niche_catalog_family" in reasons["direct-1"]
    assert "worldbank_niche_catalog_family" in reasons["direct-2"]


def test_audit_batch_query_quality_flags_classification_labor_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "China Employment Manufacturing Labor Markets Number of persons from IMF",
                "origin": {
                    "name": "Labor Markets, Employment, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 3.1, Manufacturing, Number of persons"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "classification_labor_query" in flagged["reasons"]


def test_audit_batch_query_quality_flags_definition_financial_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany Bhutan definition Non-Hydro Power Debt from IMF",
                "origin": {
                    "name": "Bhutan definition, non-hydro power debt"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "definition_financial_query" in flagged["reasons"]


def test_audit_batch_query_quality_flags_government_finance_definition_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Revenue Taxes Dominican Republic Definition Government and Public Sector Finance Budgetary Central Government from IMF",
                "origin": {
                    "name": "Dominican Republic Definition, Government and Public Sector Finance, Revenue, Budgetary Central Government, Taxes"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "definition_financial_query" in flagged["reasons"]


def test_audit_batch_query_quality_flags_imf_complex_finance_family_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "China Inward Debt Positions (Net): Resident Financial Intermediaries from IMF",
                "origin": {
                    "name": "Inward Debt Positions (Net): Resident Financial Intermediaries, Euros"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "imf_complex_finance_family" in flagged["reasons"]


def test_audit_batch_query_quality_flags_imf_external_debt_position_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany Gross External Debt Position Private Sector Debt Not Publicly Guranteed from IMF",
                "origin": {
                    "name": "Gross External Debt Position, Private Sector Debt Not Publicly Guranteed"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "imf_complex_finance_family" in flagged["reasons"]


def test_audit_batch_query_quality_flags_imf_producer_price_weight_queries(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Germany Weight Producer Price Index Manufacture of furniture from IMF",
                "origin": {
                    "name": "Prices, Producer Price Index, Manufacture of furniture, Weight"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "imf_low_viability_family" in flagged["reasons"]


def test_audit_batch_query_quality_flags_oecd_unemployment_benefits_tax_row(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Canada Effective tax rate on taking up work when claiming unemployment benefits from OECD",
                "origin": {
                    "name": "Effective tax rate on taking up work when claiming unemployment benefits"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "oecd_low_viability_family" in flagged["reasons"]


def test_audit_batch_query_quality_flags_imf_memorandum_gva_query(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "Brazil Memorandum Items Real Activity Gross Value Added Information and communication Previous Year Prices from IMF",
                "origin": {
                    "name": "Memorandum Items, Real Activity, Gross Value Added, Information and communication, Previous Year Prices"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "imf_price_or_memorandum_family" in flagged["reasons"]


def test_audit_batch_query_quality_flags_oecd_temporary_employment_permanency_row(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "audit.json"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "United States Share of women in temporary employment by permanency of the job from OECD",
                "origin": {
                    "name": "Share of women in temporary employment by permanency of the job"
                },
            }
        ],
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dataset",
            str(dataset),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["high_risk_rows"] == 1
    flagged = report["flagged_rows"][0]
    assert "oecd_low_viability_family" in flagged["reasons"]
