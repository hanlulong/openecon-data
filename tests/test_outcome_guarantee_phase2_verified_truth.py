from __future__ import annotations

from pathlib import Path

from backend.models import ParsedIntent
from backend.services.execution_planner import build_minimal_execution_plan


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


def test_phase2_execution_plan_uses_typed_query_scope_for_comparison_cardinality() -> None:
    intent = ParsedIntent(
        apiProvider="WORLDBANK",
        indicators=["unemployment rate"],
        parameters={},
        clarificationNeeded=False,
        queryType="comparison",
        originalQuery="which country has the highest unemployment rate",
    )

    plan = build_minimal_execution_plan(
        "which country has the highest unemployment rate",
        intent,
    )

    assert "comparison_cardinality" in plan.verification_checks
    assert plan.expected_shape["min_series_count"] == 2


def test_phase2_execution_plan_does_not_infer_semantic_transform_from_query_text() -> None:
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["GDP"],
        parameters={"country": "US", "seriesId": "GDP"},
        clarificationNeeded=False,
        queryType="data_fetch",
        originalQuery="show me US GDP",
    )

    plan = build_minimal_execution_plan(
        "show me US GDP growth and change",
        intent,
    )

    assert plan.expected_shape["requested_indicator"] == "GDP"
    assert plan.verification_checks == ["indicator_identity", "provider_executable", "country_scope"]


def test_phase2_gate_script_exists() -> None:
    assert (REPO_ROOT / "scripts" / "phase2_verified_truth_gate.py").exists()
