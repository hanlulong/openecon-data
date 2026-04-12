from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_phase1_candidate_evidence_builder_module_exists() -> None:
    assert (REPO_ROOT / "backend" / "services" / "candidate_evidence_builder.py").exists()


@pytest.mark.unit
def test_phase1_outcome_decision_contract_exists() -> None:
    service_sources = (REPO_ROOT / "backend" / "services").glob("*.py")
    assert any("class OutcomeDecision" in path.read_text(encoding="utf-8") for path in service_sources)


@pytest.mark.unit
def test_phase1_feature_flags_are_declared() -> None:
    config_text = (REPO_ROOT / "backend" / "config.py").read_text(encoding="utf-8")
    assert "USE_OUTCOME_DECISION_STAGE" in config_text
