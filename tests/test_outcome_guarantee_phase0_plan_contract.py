from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

PLAN_FILENAME = "plan-session-restart-2026-04-11-outcome-guarantee-consensus.md"
TEST_SPEC_FILENAME = "test-spec-session-restart-2026-04-11-outcome-guarantee.md"


def _normalize(text: str) -> str:
    return text.lower().replace("–", "-").replace("—", "-")


def _candidate_plan_dirs() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [repo_root / ".omx" / "plans"]

    team_state_root = os.getenv("OMX_TEAM_STATE_ROOT")
    if team_state_root:
        leader_root = Path(team_state_root).resolve().parent.parent
        candidates.append(leader_root / ".omx" / "plans")

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_candidates.append(candidate)
    return unique_candidates


def _read_plan_artifact(filename: str) -> str:
    for plan_dir in _candidate_plan_dirs():
        artifact = plan_dir / filename
        if artifact.exists():
            return artifact.read_text(encoding="utf-8")
    pytest.skip(f"Could not find {filename} in any plan directory candidate")


def _phase_section(content: str, phase_number: int) -> str:
    pattern = rf"^#### Phase {phase_number} .*?(?=^#### Phase \d+ |\Z)"
    match = re.search(pattern, content, flags=re.MULTILINE | re.DOTALL)
    assert match, f"Missing Phase {phase_number} section"
    return match.group(0)


PHASE_TITLES = [
    "#### Phase 0 — Plan hardening and evaluation contract",
    "#### Phase 1 — Candidate evidence + semantic decision backbone",
    "#### Phase 2 — Minimal typed `ExecutionPlan` skeleton + post-fetch verification + staged state commit",
    "#### Phase 3 — Full execution planner boundary and provider-boundary generalization",
    "#### Phase 4 — Decomposition and state-model hardening (V3)",
    "#### Phase 5 — Provider migration under the new contracts",
    "#### Phase 6 — Broad evaluation, regression hardening, rollout",
]


@pytest.mark.unit
def test_consensus_plan_has_ordered_gated_phases() -> None:
    content = _read_plan_artifact(PLAN_FILENAME)

    positions = [content.index(title) for title in PHASE_TITLES]
    assert positions == sorted(positions), "Phase headings should appear in execution order"

    phase_zero = _phase_section(content, 0)
    assert "**Hard pass/fail gate:**" in phase_zero
    assert "**Must define**" in phase_zero
    assert "**Non-goals**" in phase_zero
    assert "**Exit evidence required**" in phase_zero

    for phase_number in range(1, 7):
        phase = _phase_section(content, phase_number)
        assert "**Hard pass/fail gate:**" in phase
        assert "**Must deliver**" in phase
        assert "**Non-goals**" in phase
        assert "**Exit evidence required**" in phase


@pytest.mark.unit
def test_phase_zero_contract_names_required_rules() -> None:
    phase_zero = _normalize(_phase_section(_read_plan_artifact(PLAN_FILENAME), 0))

    required_rules = [
        "feature flags",
        "provider matrix",
        "exact-output checks",
        "review-agent requirement",
        "active-improvement requirement",
        "clarification-width rule",
        "post-answer alternative exploration requirement",
    ]

    for rule in required_rules:
        assert rule in phase_zero, f"Phase 0 contract should name {rule!r}"


@pytest.mark.unit
def test_consensus_plan_captures_provider_matrix_and_process_gates() -> None:
    normalized_content = _normalize(_read_plan_artifact(PLAN_FILENAME))

    required_providers = [
        "fred",
        "world bank",
        "imf",
        "bis",
        "eurostat",
        "statscan",
        "comtrade",
        "coingecko",
        "exchangerate",
    ]
    for provider in required_providers:
        assert provider in normalized_content, f"Missing provider coverage for {provider}"

    required_process_rules = [
        "2-3+ parallel review agents",
        "one concrete improvement per cycle",
        "up to **10**",
        "post-answer alternative exploration",
        "10 manual multi-round chains",
    ]
    for rule in required_process_rules:
        assert rule in normalized_content, f"Missing process rule {rule!r}"


@pytest.mark.unit
def test_test_spec_covers_exact_output_checks_and_benchmark_families() -> None:
    normalized_content = _normalize(_read_plan_artifact(TEST_SPEC_FILENAME))

    exact_output_checks = [
        "exact indicator correctness",
        "group/country completeness",
        "data sufficiency",
        "value-range sanity checks",
        "ambiguity behavior",
        "2-3+ parallel review agents every cycle",
        "10 manual multi-round chains before commit",
    ]
    for check in exact_output_checks:
        assert check in normalized_content, f"Missing exact-output or process check {check!r}"

    benchmark_families = [
        "direct retrieval",
        "transform/derived metrics",
        "ranking",
        "breakdown/decomposition",
        "comparison",
        "multi-round state transitions",
        "ambiguous clarification cases",
    ]
    for family in benchmark_families:
        assert family in normalized_content, f"Missing benchmark family {family!r}"
