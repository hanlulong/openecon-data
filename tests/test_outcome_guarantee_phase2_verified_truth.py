from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_phase2_feature_flags_are_declared() -> None:
    config_text = (REPO_ROOT / "backend" / "config.py").read_text(encoding="utf-8")
    for flag in (
        "USE_MINIMAL_EXECUTION_PLAN",
        "USE_POST_FETCH_SEMANTIC_JUDGE",
        "USE_STAGED_STATE_COMMIT",
    ):
        assert flag in config_text


def test_phase2_execution_plan_contract_exists() -> None:
    models_text = (REPO_ROOT / "backend" / "models.py").read_text(encoding="utf-8")
    planner_text = (REPO_ROOT / "backend" / "services" / "execution_planner.py").read_text(encoding="utf-8")
    assert "class ExecutionPlan" in models_text
    assert "build_minimal_execution_plan" in planner_text


def test_phase2_gate_script_exists() -> None:
    assert (REPO_ROOT / "scripts" / "phase2_verified_truth_gate.py").exists()
