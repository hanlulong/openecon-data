from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validation.materialize_next_review_batch import (
    direct_oversample_count,
    materialize_direct,
    materialize_ambiguity,
    materialize_multiround,
    normalize_batch_targets,
    select_quality_screened_direct_records,
    supportability_prefilter_samples,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "materialize_next_review_batch.py"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]




def test_direct_oversample_count_deepens_imf_candidate_pool() -> None:
    assert direct_oversample_count("IMF", 9, 115_381) == 5_000
    assert direct_oversample_count("IMF", 30, 115_381) == 15_000
    assert direct_oversample_count("IMF", 1, 115_381) == 201
    assert direct_oversample_count("FRED", 9, 138_774) == 450
    assert direct_oversample_count("IMF", 9, 100) == 100
    assert direct_oversample_count("IMF", 544, 115_381) == 115_381
    assert direct_oversample_count("FRED", 597, 138_774) == 1_194
    assert direct_oversample_count("WorldBank", 268, 29_269) == 2_680


def _imf_sample_row(row_id: int, code: str, name: str, *, category: str = "INDICATOR") -> dict:
    return {
        "id": row_id,
        "provider": "IMF",
        "code": code,
        "name": name,
        "description": "",
        "category": category,
        "subcategory": None,
        "unit": None,
        "frequency": None,
        "coverage": None,
        "keywords": None,
        "synonyms": None,
        "raw_metadata": None,
        "popularity": 1,
        "last_updated": None,
    }


def test_materialize_direct_replaces_supportability_probe_rows_from_same_provider(monkeypatch):
    import scripts.validation.materialize_next_review_batch as module

    rows = [
        _imf_sample_row(1, "TXG_H5_60_EUR", "Detailed unsupported trade surface"),
        _imf_sample_row(2, "NGDPD", "Xqzv Blorf Snarg Pleet Alpha", category="WEO"),
        _imf_sample_row(3, "NGDP", "Xqzv Blorf Snarg Pleet Beta", category="WEO"),
        _imf_sample_row(4, "PCPIPCH", "Xqzv Blorf Snarg Pleet Gamma", category="WEO"),
    ]

    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(module, "_merge_provider_anchor_rows", lambda provider, sampled_rows, *, db_path: sampled_rows)
    monkeypatch.setattr(
        module,
        "selection_supportability_reason_for_row",
        lambda record: (
            "imf_non_weo_public_surface_unsupported"
            if (record.get("origin") or {}).get("source_indicator_code") == "TXG_H5_60_EUR"
            else None
        ),
    )

    inventory: list[dict] = []
    selected = materialize_direct(
        [{"name": "IMF", "planned_batch_sessions": 3}],
        snapshot_meta={
            "snapshot_date": "2026-04-14",
            "git_sha": "abc12345",
            "indicator_count": 330050,
            "provider_counts": {"IMF": 4},
        },
        seed=20260516,
        holdout_split="replacement-test",
        dataset_tier="cert_holdout",
        db_path=Path("unused.db"),
        supportability_inventory=inventory,
    )

    assert len(selected) == 3
    assert all(
        "selection_supportability_reason" not in row["provenance"]
        for row in selected
    )
    assert inventory
    assert inventory[0]["session_id"] == "batch-direct-imf-000001"
    assert inventory[0]["provider"] == "IMF"
    assert inventory[0]["supportability_reason"] == "imf_non_weo_public_surface_unsupported"
    assert inventory[0]["disposition"] == "excluded_with_replacement"
    assert inventory[0]["replacement_session_ids"]
    assert inventory[0]["replacement_session_ids"][0] in {row["id"] for row in selected}


def test_materialize_direct_blocks_underfill_when_only_supportability_candidates_remain(monkeypatch):
    import scripts.validation.materialize_next_review_batch as module

    rows = [
        _imf_sample_row(1, "TXG_H5_60_EUR", "Detailed unsupported trade surface"),
        _imf_sample_row(2, "NGDPD", "Gross domestic product, current prices", category="WEO"),
    ]

    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(module, "_merge_provider_anchor_rows", lambda provider, sampled_rows, *, db_path: sampled_rows)
    monkeypatch.setattr(
        module,
        "selection_supportability_reason_for_row",
        lambda record: (
            "imf_non_weo_public_surface_unsupported"
            if (record.get("origin") or {}).get("source_indicator_code") == "TXG_H5_60_EUR"
            else None
        ),
    )

    with pytest.raises(RuntimeError, match="could not fill IMF planned count 2"):
        materialize_direct(
            [{"name": "IMF", "planned_batch_sessions": 2}],
            snapshot_meta={
                "snapshot_date": "2026-04-14",
                "git_sha": "abc12345",
                "indicator_count": 330050,
                "provider_counts": {"IMF": 2},
            },
            seed=20260516,
            holdout_split="underfill-test",
            dataset_tier="cert_holdout",
            db_path=Path("unused.db"),
            supportability_inventory=[],
        )


def test_supportability_prefilter_samples_fails_fast_for_large_imf_underfill(monkeypatch):
    import scripts.validation.materialize_next_review_batch as module

    rows = [
        _imf_sample_row(1, "TXG_H5_60_EUR", "Detailed unsupported trade surface"),
        _imf_sample_row(2, "NGDPD", "Gross domestic product, current prices", category="WEO"),
    ]
    monkeypatch.setattr(
        module,
        "selection_supportability_reason_for_row",
        lambda record: (
            "imf_non_weo_public_surface_unsupported"
            if ((record.get("origin") or {}).get("source_indicator_code") == "TXG_H5_60_EUR")
            else None
        ),
    )

    with pytest.raises(RuntimeError, match="could not fill IMF planned count 2"):
        supportability_prefilter_samples(
            "IMF",
            rows,
            2,
            certification_target="user_answerability",
            include_supportability_probes=False,
            min_candidate_rows=0,
        )


def test_supportability_prefilter_samples_keeps_selectable_large_imf_rows(monkeypatch):
    import scripts.validation.materialize_next_review_batch as module

    rows = [
        _imf_sample_row(1, "TXG_H5_60_EUR", "Detailed unsupported trade surface"),
        _imf_sample_row(2, "NGDPD", "Gross domestic product, current prices", category="WEO"),
        _imf_sample_row(3, "NGDP", "Gross domestic product, current prices", category="WEO"),
    ]
    monkeypatch.setattr(
        module,
        "selection_supportability_reason_for_row",
        lambda record: (
            "imf_non_weo_public_surface_unsupported"
            if ((record.get("origin") or {}).get("source_indicator_code") == "TXG_H5_60_EUR")
            else None
        ),
    )

    filtered, excluded_count, did_filter = supportability_prefilter_samples(
        "IMF",
        rows,
        2,
        certification_target="user_answerability",
        include_supportability_probes=False,
        min_candidate_rows=0,
    )

    assert did_filter is True
    assert excluded_count == 1
    assert [row["code"] for row in filtered] == ["NGDPD", "NGDP"]


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
    all_rows = read_jsonl(output_dir / "next_batch_all.jsonl")

    assert len(direct_rows) == 3
    assert len(multiround_rows) == 2
    assert len(ambiguity_rows) == 3
    assert len(all_rows) == 8
    assert all_rows == direct_rows + multiround_rows + ambiguity_rows
    assert direct_rows[0]["provenance"]["holdout_split"] == "batch_review"
    assert direct_rows[0]["provenance"]["batch_plan"] == "next_review_batch"
    assert "description" in direct_rows[0]["origin"]
    assert "raw_metadata" in direct_rows[0]["origin"]
    assert multiround_rows[0]["family"] == "transform_switch_chain"
    assert ambiguity_rows[0]["provenance"]["family"] == "dominant_interpretation_cases"


def test_normalize_batch_targets_accepts_expansion_plan_mapping() -> None:
    targets = normalize_batch_targets(
        {
            "FRED": {
                "class": "critical",
                "floor": 0.98,
                "current_n": 88,
                "additional_target_sessions": 784,
                "recommended_total_n": 872,
            },
            "IMF": {
                "class": "critical",
                "floor": 0.98,
                "current_n": 143,
                "additional_target_sessions": 577,
                "recommended_total_n": 720,
            },
        }
    )

    assert targets == [
        {
            "name": "FRED",
            "class": "critical",
            "floor": 0.98,
            "current_n": 88,
            "additional_target_sessions": 784,
            "planned_batch_sessions": 784,
            "recommended_total_n": 872,
            "target_n": 872,
        },
        {
            "name": "IMF",
            "class": "critical",
            "floor": 0.98,
            "current_n": 143,
            "additional_target_sessions": 577,
            "planned_batch_sessions": 577,
            "recommended_total_n": 720,
            "target_n": 720,
        },
    ]


def test_materialize_next_review_batch_accepts_expansion_plan_target_mapping(tmp_path: Path):
    batch_plan = tmp_path / "batch.json"
    snapshot = tmp_path / "snapshot.json"
    output_dir = tmp_path / "out"

    write_json(
        batch_plan,
        {
            "allocation": {
                "direct": {
                    "targets": {
                        "FRED": {
                            "additional_target_sessions": 1,
                            "recommended_total_n": 89,
                            "current_n": 88,
                        }
                    }
                },
                "multiround": {
                    "targets": {
                        "provider_switch_chain": {
                            "additional_target_sessions": 1,
                            "recommended_total_n": 2,
                            "current_n": 1,
                        }
                    }
                },
                "ambiguity": {
                    "targets": {
                        "provider_ambiguity": {
                            "additional_target_sessions": 1,
                            "recommended_total_n": 2,
                            "current_n": 1,
                        }
                    }
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
            "--session-id-prefix",
            "ralph175-design-expansion",
        ],
        check=True,
    )

    direct_rows = read_jsonl(output_dir / "next_batch_direct.jsonl")
    multiround_rows = read_jsonl(output_dir / "next_batch_multiround.jsonl")
    ambiguity_rows = read_jsonl(output_dir / "next_batch_ambiguity.jsonl")
    assert len(direct_rows) == 1
    assert len(multiround_rows) == 1
    assert len(ambiguity_rows) == 1
    assert len(read_jsonl(output_dir / "next_batch_all.jsonl")) == 3
    assert direct_rows[0]["id"].startswith("ralph175-design-expansion-batch-direct-fred-")
    assert multiround_rows[0]["id"] == "ralph175-design-expansion-batch-provider_switch_chain-000002"
    assert ambiguity_rows[0]["id"] == "ralph175-design-expansion-amb-provider_ambiguity-000002"


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
    assert read_jsonl(output_dir / "next_batch_all.jsonl") == []


def test_materialize_multiround_starts_after_current_n():
    rows = materialize_multiround(
        [
            {
                "name": "provider_switch_chain",
                "planned_batch_sessions": 2,
                "target_n": 19,
                "current_n": 2,
            }
        ],
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc12345", "indicator_count": 330050},
        seed=20260415,
        holdout_split="batch_review",
        dataset_tier="dev",
    )

    assert [row["id"] for row in rows] == [
        "batch-provider_switch_chain-000003",
        "batch-provider_switch_chain-000004",
    ]
    assert rows[0]["rounds"][0]["query"] == "United States GDP from FRED"
    assert rows[0]["rounds"][1]["query"] == "India GDP from World Bank"
    assert rows[0]["rounds"][2]["query"] == "Brazil GDP from IMF"
    assert rows[1]["rounds"][1]["query"] == "China GDP from World Bank"


def test_materialize_ambiguity_starts_after_current_n():
    rows = materialize_ambiguity(
        [
            {
                "name": "dominant_interpretation_cases",
                "planned_batch_sessions": 2,
                "target_n": 30,
                "current_n": 2,
            }
        ],
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc12345", "indicator_count": 330050},
        seed=20260415,
        holdout_split="batch_review",
        dataset_tier="dev",
    )

    assert [row["id"] for row in rows] == [
        "amb-dominant_interpretation_cases-000003",
        "amb-dominant_interpretation_cases-000004",
    ]
    assert [row["query"] for row in rows] == [
        "US unemployment rate",
        "Bitcoin price",
    ]


def test_materialize_ambiguity_keeps_canada_employment_as_direct_answer():
    rows = materialize_ambiguity(
        [
            {
                "name": "transform_ambiguity",
                "planned_batch_sessions": 1,
                "target_n": 30,
                "current_n": 1,
            }
        ],
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc12345", "indicator_count": 330050},
        seed=20260415,
        holdout_split="batch_review",
        dataset_tier="dev",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "amb-transform_ambiguity-000002"
    assert row["query"] == "Canada employment"
    assert row["expected_behavior"] == "direct_answer"
    assert row["gold"]["acceptable_outcomes"] == ["direct_answer_correct"]
    assert row["gold"]["unnecessary_clarification"] is True


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


def test_select_quality_screened_direct_records_prefers_low_risk_statscan_over_modestly_more_specific_medium_slice():
    records = [
        {
            "id": "statscan-medium-health-slice",
            "query": "Canada youth reported Health indicator statistics for youth aged 12 to 17 years from Statistics Canada",
            "provider_stratum": "StatsCan",
            "origin": {
                "name": "Youth-reported Health indicator statistics for youth aged 12 to 17 years",
                "description": "",
                "source_provider": "StatsCan",
            },
            "provenance": {"query_quality_risk": "medium", "query_quality_reasons": ["long_query"]},
        },
        {
            "id": "statscan-low-import-industry",
            "query": "Canada Import of goods or services by industry and enterprise size from Statistics Canada",
            "provider_stratum": "StatsCan",
            "origin": {
                "name": "Import of goods or services by industry and enterprise size",
                "description": "",
                "source_provider": "StatsCan",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["statscan-low-import-industry"]


def test_select_quality_screened_direct_records_rejects_code_prefixed_worldbank_row_when_clean_alternative_exists():
    records = [
        {
            "id": "worldbank-code-prefix",
            "query": "Brazil 1101130:Fish and seafood from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "1101130:Fish and seafood",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["indicator_code_prefix"]},
        },
        {
            "id": "worldbank-clean",
            "query": "China Lower secondary completion rate total (% of relevant age group) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Lower secondary completion rate, total (% of relevant age group)",
                "description": "",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["worldbank-clean"]


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


def test_select_quality_screened_direct_records_caps_worldbank_education_category():
    records = [
        {
            "id": f"edu-{idx}",
            "query": f"Education row {idx} from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": f"Education row {idx}",
                "description": "",
                "category": "Education Statistics",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        }
        for idx in range(7)
    ] + [
        {
            "id": "ddh-1",
            "query": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "category": "Disability Data Hub (DDH)",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 7)

    categories = [row["origin"]["category"] for row in selected]
    assert categories.count("Education Statistics") == 6
    assert "Disability Data Hub (DDH)" in categories


def test_select_quality_screened_direct_records_expands_cap_for_proven_worldbank_family():
    records = [
        {
            "id": "worldbank-literacy-a",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "category": "Disability Data Hub (DDH)",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
        {
            "id": "worldbank-literacy-b",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "category": "Disability Data Hub (DDH)",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
        {
            "id": "worldbank-literacy-c",
            "query": "Brazil Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty) from World Bank",
            "provider_stratum": "WorldBank",
            "origin": {
                "name": "Literacy rate (% of persons aged 15 to 29 years with any degree of functional difficulty)",
                "description": "",
                "category": "Disability Data Hub (DDH)",
                "source_provider": "WorldBank",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 3)

    assert len(selected) == 3


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


def test_select_quality_screened_direct_records_prefers_supported_imf_candidate_over_h5_trade_family():
    records = [
        {
            "id": "imf-h5-code-only",
            "query": "Germany aggregate trade indicator from IMF",
            "provider_stratum": "IMF",
            "origin": {
                "name": "Aggregate trade indicator",
                "source_indicator_code": "TMG_H5_80_EUR",
            },
            "provenance": {"query_quality_risk": "high", "query_quality_reasons": ["imf_low_viability_family"]},
        },
        {
            "id": "imf-aggregate-trade",
            "query": "Germany exports of goods from IMF",
            "provider_stratum": "IMF",
            "origin": {
                "name": "Exports of goods",
                "source_indicator_code": "XG_FOB_USD",
            },
            "provenance": {"query_quality_risk": "low", "query_quality_reasons": []},
        },
    ]

    selected = select_quality_screened_direct_records(records, 1)

    assert [row["id"] for row in selected] == ["imf-aggregate-trade"]


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
