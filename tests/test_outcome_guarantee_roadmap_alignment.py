from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP_PATH = REPO_ROOT / "docs" / "design" / "QUERY_OUTCOME_IMPLEMENTATION_ROADMAP.md"


@pytest.mark.unit
def test_versioned_roadmap_has_gated_phase_contract_headings() -> None:
    roadmap_text = ROADMAP_PATH.read_text(encoding="utf-8")

    required_headings = [
        "#### Phase 0 — Plan hardening and evaluation contract",
        "#### Phase 1 — Candidate evidence + semantic decision backbone",
        "#### Phase 2 — Minimal typed `ExecutionPlan` skeleton + post-fetch verification + staged state commit",
        "#### Phase 3 — Full execution planner boundary and provider-boundary generalization",
    ]
    for heading in required_headings:
        assert heading in roadmap_text


@pytest.mark.unit
def test_versioned_roadmap_names_hard_gates_non_goals_and_exit_evidence() -> None:
    roadmap_text = ROADMAP_PATH.read_text(encoding="utf-8")

    required_contract_terms = [
        "**Hard pass/fail gate:**",
        "**Non-goals**",
        "**Exit evidence required**",
        "feature flags",
        "provider matrix",
        "post-answer alternative exploration",
    ]
    for term in required_contract_terms:
        assert term in roadmap_text
