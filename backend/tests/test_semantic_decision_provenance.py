from __future__ import annotations

from backend.services.semantic_decision_provenance import SemanticDecisionProvenance


def test_candidate_generation_cannot_commit_semantic_state() -> None:
    provenance = SemanticDecisionProvenance(
        decision_source="candidate_generation",
        semantic_authority="none",
        candidate_count=3,
    )

    assert not provenance.has_final_semantic_authority()
    assert not provenance.can_commit_semantic_state()


def test_llm_pick_with_passing_verification_can_commit_semantic_state() -> None:
    provenance = SemanticDecisionProvenance(
        decision_source="llm_pick",
        semantic_authority="llm_adjudication",
        candidate_count=5,
        post_fetch_judgment="pass",
        state_commit="allowed",
    )

    assert provenance.has_final_semantic_authority()
    assert provenance.can_commit_semantic_state()


def test_post_fetch_failure_blocks_state_commit_even_after_llm_pick() -> None:
    provenance = SemanticDecisionProvenance(
        decision_source="llm_pick",
        semantic_authority="llm_adjudication",
        candidate_count=5,
        post_fetch_judgment="fail",
        state_commit="blocked",
    )

    assert provenance.has_final_semantic_authority()
    assert not provenance.can_commit_semantic_state()


def test_provenance_serializes_reject_retry_evidence() -> None:
    provenance = SemanticDecisionProvenance(
        decision_source="llm_reject_retry",
        semantic_authority="llm_adjudication",
        candidate_count=4,
        rejected_reasons=("children series is not households",),
        retry_terms="private households total count",
    )

    payload = provenance.to_dict()

    assert payload["decision_source"] == "llm_reject_retry"
    assert payload["candidate_count"] == 4
    assert payload["rejected_reasons"] == ("children series is not households",)
    assert payload["retry_terms"] == "private households total count"

