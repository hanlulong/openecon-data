from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scripts.phase6_evaluation_bundle import validate_exact_output_case


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_phase6_gate_script_exists_with_required_bundle_artifacts() -> None:
    script = (REPO_ROOT / "scripts" / "phase6_evaluation_bundle.py").read_text(encoding="utf-8")
    assert "phase6-evaluation-bundle.json" in script
    assert "phase6-evaluation-bundle.md" in script
    assert "phase2_verified_truth" in script
    assert "phase5_provider_matrix" in script


def test_phase6_gate_requires_manual_and_review_attachments() -> None:
    script = (REPO_ROOT / "scripts" / "phase6_evaluation_bundle.py").read_text(encoding="utf-8")
    assert "phase6-manual-multiround.md" in script
    assert "phase6-manual-multiround.json" in script
    assert "phase6-review-1.md" in script
    assert "review_evidence_attached" in script


def test_phase6_canonical_multiround_wrapper_exists() -> None:
    script = (REPO_ROOT / "scripts" / "test_multiround.py").read_text(encoding="utf-8")
    assert "test_multiround_10x10.py" in script
    assert "--report" in script
    assert "--suite" in script
    assert "OPENECON_MULTIROUND_BASE_URL" in script


def test_phase6_multiround_harness_supports_named_suites() -> None:
    script = (REPO_ROOT / "scripts" / "test_multiround_10x10.py").read_text(encoding="utf-8")
    assert "list_suite_descriptions" in script
    assert "load_suite(args.suite)" in script
    assert "suite=args.suite" in script
    assert '"suite": suite' in script


def test_phase6_exact_output_validator_accepts_catalog_concept_for_decomposition() -> None:
    response = {
        "intent": {
            "apiProvider": "STATSCAN",
            "indicators": ["14100287"],
            "needsDecomposition": True,
            "decompositionType": "provinces",
            "parameters": {
                "__catalog_concept": "unemployment",
                "indicator": "14100287",
            },
        },
        "data": [
            {
                "metadata": {
                    "source": "Statistics Canada",
                    "indicator": "Nunavut 14100287",
                    "country": "Canada",
                    "seriesId": "14100287:1.1.1.1",
                },
                "data": [{"date": "2026-01-01", "value": 1.0}],
            },
            {
                "metadata": {
                    "source": "Statistics Canada",
                    "indicator": "Alberta 14100287",
                    "country": "Canada",
                    "seriesId": "14100287:2.1.1.1",
                },
                "data": [{"date": "2026-01-01", "value": 2.0}],
            },
        ],
    }

    with patch("scripts.phase6_evaluation_bundle.run_query", return_value=(response, 1.23)):
        result = validate_exact_output_case(
            {
                "query": "Canada unemployment rate by province",
                "expected_source": "STATSCAN",
                "indicator_cues": ["unemployment"],
                "min_series_count": 2,
                "needs_decomposition": True,
                "decomposition_type": "provinces",
            },
            base_url="http://localhost:3001",
        )

    assert result["ok"] is True
