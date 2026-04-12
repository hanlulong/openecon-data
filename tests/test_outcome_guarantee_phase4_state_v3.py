from __future__ import annotations

from pathlib import Path

from backend.models import ParsedIntent
from backend.services.conversation_state_v2 import (
    ConversationState,
    FollowUpDelta,
    materialize_intent,
    merge_new_state_with_previous,
    merge_state,
)
from backend.services.execution_planner import build_minimal_execution_plan


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_phase4_delta_extractor_prompt_mentions_changed_decomposition() -> None:
    prompt_text = (REPO_ROOT / "backend" / "services" / "delta_extractor.py").read_text(encoding="utf-8")
    assert "changed_decomposition" in prompt_text
    assert "by province" in prompt_text


def test_phase4_by_province_and_ontario_only_are_distinct_state_transitions() -> None:
    base_state = ConversationState(
        indicator="unemployment rate",
        country="CA",
        provider="STATSCAN",
    )

    by_province = merge_state(
        base_state,
        FollowUpDelta(
            added_dimensions={"Geography": "province"},
            is_dimension_modifier_change=True,
            raw_query="break it down by province",
        ),
    )
    assert by_province.decomposition == {
        "type": "provinces",
        "entities": None,
        "axis": "Geography",
    }

    ontario_only = merge_state(
        by_province,
        FollowUpDelta(
            added_dimensions={"Geography": "Ontario"},
            is_dimension_modifier_change=True,
            raw_query="just show Ontario",
        ),
    )
    assert ontario_only.decomposition is None
    assert ontario_only.dimensions == {"Geography": "Ontario"}


def test_phase4_time_change_preserves_decomposition_in_materialized_intent() -> None:
    previous = ConversationState(
        indicator="unemployment rate",
        country="CA",
        provider="STATSCAN",
        decomposition={"type": "provinces", "entities": None, "axis": "Geography"},
        turn_number=1,
    )
    new_state = ConversationState(
        indicator="unemployment rate",
        provider="STATSCAN",
        start_date="2010-01-01",
        end_date="2024-12-31",
    )

    merged = merge_new_state_with_previous(new_state, previous)
    intent = materialize_intent(merged)
    assert intent.needsDecomposition is True
    assert intent.decompositionType == "provinces"
    assert intent.parameters["startDate"] == "2010-01-01"


def test_phase4_execution_plan_records_decomposition_expectation() -> None:
    intent = ParsedIntent(
        apiProvider="STATSCAN",
        indicators=["unemployment rate"],
        parameters={"country": "CA"},
        clarificationNeeded=False,
        needsDecomposition=True,
        decompositionType="provinces",
        decompositionEntities=None,
        originalQuery="show unemployment rate by province",
    )

    plan = build_minimal_execution_plan("show unemployment rate by province", intent)

    assert "decomposition_cardinality" in plan.verification_checks
    assert plan.expected_shape["decomposition_type"] == "provinces"
