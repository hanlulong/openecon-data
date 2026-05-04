"""Model-backed semantic matching for runtime safety checks.

This module is intentionally narrow: it does not try to parse user intent or
resolve indicators. It only judges whether already-fetched candidate metadata
appears to represent the same economic concept as the user's request.
"""

from __future__ import annotations

import logging
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from ..models import NormalizedData
from .candidate_evidence_builder import CandidateEvidence
from .json_parser import JSONParseError, parse_json_response

logger = logging.getLogger(__name__)


class SemanticMatchJudgment(BaseModel):
    """Structured semantic-match decision from the LLM."""

    decision: Literal["match", "mismatch", "uncertain"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class OutcomeDecision(BaseModel):
    """Explicit prefetch/postfetch control decision."""

    outcome: Literal["direct_answer", "clarify", "unsupported"]
    reason: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    selected_candidate_id: Optional[str] = None
    clarification_option_limit: int = Field(default=4, ge=1, le=10)
    verification_expectations: List[str] = Field(default_factory=list)


class ExecutionResultJudgment(BaseModel):
    """Structured post-fetch verification decision."""

    decision: Literal["pass", "fail", "uncertain"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    failed_checks: List[str] = Field(default_factory=list)


def _series_summary(series: NormalizedData) -> dict[str, str]:
    """Build a compact metadata summary for one candidate series."""
    meta = getattr(series, "metadata", None)
    return {
        "provider": str(getattr(meta, "source", "") or ""),
        "series_id": str(getattr(meta, "seriesId", "") or ""),
        "indicator": str(getattr(meta, "indicator", "") or ""),
        "description": str(getattr(meta, "description", "") or ""),
        "unit": str(getattr(meta, "unit", "") or ""),
        "frequency": str(getattr(meta, "frequency", "") or ""),
    }


def _judge_system_prompt() -> str:
    return (
        "You are a semantic matching judge for an economic data system.\n\n"
        "Your task is ONLY to decide whether fetched candidate series metadata "
        "matches the same economic concept the user requested.\n\n"
        "Important rules:\n"
        "- Ignore provider differences and code format differences.\n"
        "- Country coverage has already been validated elsewhere.\n"
        "- Focus on semantic meaning: concept, transform/variant, direction "
        "(imports vs exports), subject (households vs government vs corporate), "
        "and whether the candidate is clearly the same metric.\n"
        "- Return 'match' only when the candidate clearly matches.\n"
        "- Return 'mismatch' when the candidate clearly represents a different concept.\n"
        "- Return 'uncertain' when the metadata is insufficient to prove a match.\n"
        "- Be conservative. If in doubt, choose 'uncertain'."
    )


def _execution_judge_system_prompt() -> str:
    return (
        "You verify whether a fetched economic-data result satisfies an execution plan.\n\n"
        "Important rules:\n"
        "- Treat the execution plan as the source of truth for requested metric, country scope, "
        "and result shape.\n"
        "- Focus on whether the fetched result matches the user's requested metric and the "
        "execution plan's verification checks.\n"
        "- Fail when the result is clearly a different concept, transform, or shape.\n"
        "- Fail when a ranking or comparison requires multiple comparable series but the "
        "result does not provide them.\n"
        "- Fail when the requested metric says growth, spread, M1, imports, exports, or another "
        "specific transform/variant and the fetched result reflects a different metric.\n"
        "- Fail when the execution plan requests explicit countries but the fetched summaries do "
        "not support that country scope.\n"
        "- Fail when the execution plan expects a decomposition/breakdown but the fetched result "
        "collapses to a single member, or when it expects a single-member filter but returns a full breakdown.\n"
        "- Be conservative. If the metadata is insufficient, choose 'uncertain'."
    )


def _judge_user_prompt(
    *,
    original_indicators: List[str],
    fallback_result: List[NormalizedData],
    original_query: Optional[str],
    original_concept: Optional[str],
) -> str:
    candidate_summaries = [
        _series_summary(series)
        for series in fallback_result[:3]
    ]
    return (
        f"Original query: {str(original_query or '').strip() or '(not provided)'}\n"
        f"Original indicator text: {', '.join(str(item) for item in original_indicators if str(item).strip()) or '(none)'}\n"
        f"Catalog concept: {str(original_concept or '').strip() or '(none)'}\n"
        f"Candidate series: {candidate_summaries}"
    )


def _execution_judge_user_prompt(
    *,
    original_query: str,
    execution_plan: dict[str, Any],
    result_summaries: list[dict[str, Any]],
) -> str:
    return (
        f"Original query: {original_query}\n"
        f"Execution plan: {execution_plan}\n"
        f"Fetched result summary: {result_summaries}"
    )


async def judge_fallback_result(
    qs: Any,
    *,
    original_indicators: List[str],
    fallback_result: List[NormalizedData],
    original_query: Optional[str],
    original_concept: Optional[str],
) -> Optional[SemanticMatchJudgment]:
    """Ask the configured LLM whether fallback metadata matches the request."""
    if not fallback_result or not original_indicators:
        return None

    openrouter = getattr(qs, "openrouter", None)
    if openrouter is None:
        return None

    system_prompt = _judge_system_prompt()
    user_prompt = _judge_user_prompt(
        original_indicators=original_indicators,
        fallback_result=fallback_result,
        original_query=original_query,
        original_concept=original_concept,
    )

    instructor_client = getattr(openrouter, "instructor_client", None)
    instructor_model = getattr(openrouter, "instructor_model", None)
    if instructor_client is not None and instructor_model is not None:
        try:
            judgment: SemanticMatchJudgment = await instructor_client.chat.completions.create(
                model=instructor_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_model=SemanticMatchJudgment,
                max_retries=2,
                temperature=0.0,
                max_tokens=220,
            )
            return judgment
        except Exception as exc:
            logger.warning("Fallback semantic judge (Instructor) failed: %s", exc)

    llm_provider = getattr(openrouter, "llm_provider", None)
    if llm_provider is None:
        return None

    try:
        result = await llm_provider.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=220,
            response_format={"type": "json_object"},
        )
        content = result["choices"][0]["message"]["content"]
        parsed = parse_json_response(content, fix_truncated=True)
        return SemanticMatchJudgment.model_validate(parsed)
    except (KeyError, JSONParseError, ValueError) as exc:
        logger.warning("Fallback semantic judge (manual JSON) failed: %s", exc)
    except Exception as exc:
        logger.warning("Fallback semantic judge failed: %s", exc)

    return None


async def judge_resolved_indicator(
    qs: Any,
    *,
    provider: str,
    indicator_query: str,
    resolved_code: str,
    resolved_name: str = "",
    resolved_metadata: Optional[dict[str, Any]] = None,
) -> Optional[SemanticMatchJudgment]:
    """Judge whether one resolved provider indicator matches the intended metric."""
    openrouter = getattr(qs, "openrouter", None)
    if openrouter is None:
        return None

    system_prompt = (
        "You are a semantic matching judge for an economic data system.\n\n"
        "Decide whether a resolved provider-specific indicator appears to match "
        "the user's intended metric.\n\n"
        "Focus on semantic meaning only:\n"
        "- concept identity\n"
        "- variant / transform (level, growth, ratio, per-capita, real vs nominal)\n"
        "- direction (imports vs exports)\n"
        "- subject/population scope (total vs youth vs household vs government)\n"
        "- whether the candidate is clearly narrower or different than requested\n\n"
        "Return:\n"
        "- 'match' when it clearly matches\n"
        "- 'mismatch' when it clearly does not\n"
        "- 'uncertain' when metadata is not enough to prove a match\n"
        "Be conservative. If in doubt, choose 'uncertain'."
    )
    candidate_payload = {
        "provider": str(provider or ""),
        "resolved_code": str(resolved_code or ""),
        "resolved_name": str(resolved_name or ""),
        "metadata": dict(resolved_metadata or {}),
    }
    user_prompt = (
        f"User metric request: {str(indicator_query or '').strip()}\n"
        f"Resolved indicator candidate: {candidate_payload}"
    )

    instructor_client = getattr(openrouter, "instructor_client", None)
    instructor_model = getattr(openrouter, "instructor_model", None)
    if instructor_client is not None and instructor_model is not None:
        try:
            judgment: SemanticMatchJudgment = await instructor_client.chat.completions.create(
                model=instructor_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_model=SemanticMatchJudgment,
                max_retries=2,
                temperature=0.0,
                max_tokens=220,
            )
            return judgment
        except Exception as exc:
            logger.warning("Resolved-indicator judge (Instructor) failed: %s", exc)

    llm_provider = getattr(openrouter, "llm_provider", None)
    if llm_provider is None:
        return None

    try:
        result = await llm_provider.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=220,
            response_format={"type": "json_object"},
        )
        content = result["choices"][0]["message"]["content"]
        parsed = parse_json_response(content, fix_truncated=True)
        return SemanticMatchJudgment.model_validate(parsed)
    except (KeyError, JSONParseError, ValueError) as exc:
        logger.warning("Resolved-indicator judge (manual JSON) failed: %s", exc)
    except Exception as exc:
        logger.warning("Resolved-indicator judge failed: %s", exc)

    return None


async def judge_execution_result(
    qs: Any,
    *,
    original_query: str,
    execution_plan: dict[str, Any],
    fetched_result: List[NormalizedData],
) -> Optional[ExecutionResultJudgment]:
    """Judge whether the fetched result satisfies the execution plan."""
    if not fetched_result:
        return None

    openrouter = getattr(qs, "openrouter", None)
    if openrouter is None:
        return None

    summaries: list[dict[str, Any]] = []
    for series in fetched_result[:3]:
        meta_summary = _series_summary(series)
        point_count = len(getattr(series, "data", []) or [])
        non_null_count = sum(
            1 for point in (getattr(series, "data", []) or [])
            if getattr(point, "value", None) is not None
        )
        summaries.append(
            {
                **meta_summary,
                "point_count": point_count,
                "non_null_count": non_null_count,
            }
        )

    system_prompt = _execution_judge_system_prompt()
    user_prompt = _execution_judge_user_prompt(
        original_query=str(original_query or "").strip(),
        execution_plan=execution_plan,
        result_summaries=summaries,
    )

    instructor_client = getattr(openrouter, "instructor_client", None)
    instructor_model = getattr(openrouter, "instructor_model", None)
    if instructor_client is not None and instructor_model is not None:
        try:
            verdict: ExecutionResultJudgment = await instructor_client.chat.completions.create(
                model=instructor_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_model=ExecutionResultJudgment,
                max_retries=2,
                temperature=0.0,
                max_tokens=260,
            )
            return verdict
        except Exception as exc:
            logger.warning("Execution-result judge (Instructor) failed: %s", exc)

    llm_provider = getattr(openrouter, "llm_provider", None)
    if llm_provider is None:
        return None

    try:
        result = await llm_provider.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=260,
            response_format={"type": "json_object"},
        )
        content = result["choices"][0]["message"]["content"]
        parsed = parse_json_response(content, fix_truncated=True)
        return ExecutionResultJudgment.model_validate(parsed)
    except (KeyError, JSONParseError, ValueError) as exc:
        logger.warning("Execution-result judge (manual JSON) failed: %s", exc)
    except Exception as exc:
        logger.warning("Execution-result judge failed: %s", exc)

    return None


def decide_prefetch_outcome(
    *,
    candidates: List[CandidateEvidence],
    primary_accepted: bool,
    primary_candidate_id: Optional[str] = None,
) -> OutcomeDecision:
    """Make an explicit prefetch direct/clarify/unsupported decision.

    This function intentionally consumes already-built candidate evidence rather than
    raw resolver internals so the control contract is explicit and testable.
    """
    option_candidates = [candidate for candidate in candidates if candidate.source == "option"]
    executable_options = [candidate for candidate in option_candidates if candidate.executable]
    primary_candidate = next(
        (candidate for candidate in candidates if candidate.candidate_id == primary_candidate_id),
        None,
    )
    clarification_limit = 4
    if len(option_candidates) > clarification_limit:
        clarification_limit = min(10, len(option_candidates))

    if len(executable_options) >= 2:
        return OutcomeDecision(
            outcome="clarify",
            reason="multiple_executable_candidates",
            confidence=0.35,
            selected_candidate_id=primary_candidate_id,
            clarification_option_limit=clarification_limit,
            verification_expectations=primary_candidate.verification_expectations if primary_candidate else [],
        )

    if len(executable_options) == 1 and not primary_accepted:
        selected = executable_options[0]
        return OutcomeDecision(
            outcome="unsupported",
            reason="single_candidate_without_final_authority",
            confidence=0.9,
            selected_candidate_id=selected.candidate_id,
            clarification_option_limit=clarification_limit,
            verification_expectations=selected.verification_expectations,
        )

    if primary_accepted and primary_candidate is not None:
        return OutcomeDecision(
            outcome="direct_answer",
            reason="primary_candidate_accepted",
            confidence=0.8,
            selected_candidate_id=primary_candidate.candidate_id,
            clarification_option_limit=clarification_limit,
            verification_expectations=primary_candidate.verification_expectations,
        )

    if len(option_candidates) >= 2:
        return OutcomeDecision(
            outcome="clarify",
            reason="multiple_candidate_options",
            confidence=0.3,
            selected_candidate_id=primary_candidate_id,
            clarification_option_limit=clarification_limit,
            verification_expectations=primary_candidate.verification_expectations if primary_candidate else [],
        )

    return OutcomeDecision(
        outcome="unsupported",
        reason="no_reliable_executable_candidate",
        confidence=0.9,
        selected_candidate_id=primary_candidate_id,
        clarification_option_limit=clarification_limit,
        verification_expectations=primary_candidate.verification_expectations if primary_candidate else [],
    )
