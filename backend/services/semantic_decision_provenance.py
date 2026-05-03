"""Decision provenance schema for semantic routing and indicator selection.

This module is intentionally small and side-effect free.  It does not make
runtime decisions; it gives the new semantic path a typed vocabulary for tests
and later integration so candidate-generation code cannot masquerade as final
semantic authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


DecisionSource = Literal[
    "exact_code",
    "exact_title",
    "mechanical_normalization",
    "candidate_generation",
    "llm_pick",
    "llm_reject_retry",
    "llm_ask",
    "post_fetch_verified",
    "unsupported",
]

SemanticAuthority = Literal[
    "exact_user_input",
    "llm_adjudication",
    "post_fetch_semantic_judge",
    "none",
]

PostFetchJudgment = Literal["not_run", "pass", "fail", "uncertain"]
StateCommitDecision = Literal["not_applicable", "allowed", "blocked"]


NON_AUTHORITATIVE_SOURCES = frozenset(
    {
        "mechanical_normalization",
        "candidate_generation",
        "llm_ask",
        "unsupported",
    }
)


@dataclass(frozen=True)
class SemanticDecisionProvenance:
    """Machine-checkable semantic-decision provenance for the flagged path."""

    decision_source: DecisionSource
    semantic_authority: SemanticAuthority = "none"
    candidate_count: int = 0
    rejected_reasons: tuple[str, ...] = field(default_factory=tuple)
    retry_terms: str = ""
    post_fetch_judgment: PostFetchJudgment = "not_run"
    state_commit: StateCommitDecision = "not_applicable"

    def has_final_semantic_authority(self) -> bool:
        """Return whether this record is allowed to choose semantic truth."""
        if self.decision_source in NON_AUTHORITATIVE_SOURCES:
            return False
        return self.semantic_authority != "none"

    def can_commit_semantic_state(self) -> bool:
        """Return whether this decision can be promoted into conversation state."""
        if not self.has_final_semantic_authority():
            return False
        if self.post_fetch_judgment in {"fail", "uncertain"}:
            return False
        if self.state_commit == "blocked":
            return False
        return True

    def to_dict(self) -> dict[str, object]:
        """Serialize to a stable dict for logs, reports, and tests."""
        return asdict(self)

