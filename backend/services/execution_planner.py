"""Minimal typed execution-plan builder for the Phase 2 runtime contract.

The planner deliberately records structural execution expectations derived from
the parsed intent and resolved scope. It does *not* encode new semantic
heuristics about meaning; transform/variant correctness is delegated to the
model-backed verification stage.
"""

from __future__ import annotations

from typing import Any

from ..models import ExecutionPlan, ParsedIntent
from ..utils.providers import normalize_provider_name


def _candidate_code(intent: ParsedIntent) -> str:
    params = intent.parameters or {}
    candidates = [
        params.get("indicator"),
        params.get("seriesId"),
        params.get("series_id"),
        params.get("code"),
        (intent.indicators or [None])[0],
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return "UNKNOWN"


def _candidate_id(provider: str, code: str) -> str:
    provider_norm = normalize_provider_name(provider or "") or "UNKNOWN"
    code_norm = str(code or "").strip().upper() or "UNKNOWN"
    return f"{provider_norm}:{code_norm}"


def _execution_query_text(query: str, intent: ParsedIntent) -> str:
    return str(intent.resolvedQuery or query or intent.originalQuery or "").strip()


def _requested_countries(intent: ParsedIntent) -> list[str]:
    params = intent.parameters or {}
    countries = params.get("countries")
    if isinstance(countries, (list, tuple, set)):
        return [
            text
            for item in countries
            if (text := str(item or "").strip())
        ]

    country = str(params.get("country") or "").strip()
    return [country] if country else []


def _requested_indicator_text(intent: ParsedIntent) -> str:
    params = intent.parameters or {}
    candidates = [
        (intent.indicators or [None])[0],
        params.get("indicator"),
        params.get("seriesId"),
        params.get("series_id"),
        params.get("code"),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def build_minimal_execution_plan(query: str, intent: ParsedIntent) -> ExecutionPlan:
    """Build the minimal typed execution contract used by Phase 2.

    This intentionally stays small: it captures the candidate/provider identity,
    the broad expected shape of the result, and the verification checks that the
    post-fetch verification stage must satisfy.
    """

    provider = normalize_provider_name(intent.apiProvider or "") or "UNKNOWN"
    params = dict(intent.parameters or {})
    code = _candidate_code(intent)
    query_text = _execution_query_text(query, intent)
    requested_countries = _requested_countries(intent)
    requested_indicator = _requested_indicator_text(intent)
    query_type = str(intent.queryType or "data_fetch")

    verification_checks = ["indicator_identity", "provider_executable"]
    min_series_count = max(1, len(requested_countries))

    if requested_countries:
        verification_checks.append("country_scope")
    if query_type == "comparison" and min_series_count < 2:
        min_series_count = 2
    if intent.needsDecomposition:
        verification_checks.append("decomposition_cardinality")
        if not intent.decompositionEntities or len(intent.decompositionEntities or []) != 1:
            min_series_count = max(min_series_count, 2)
    if min_series_count > 1:
        verification_checks.append("comparison_cardinality")

    expected_shape: dict[str, Any] = {
        "min_series_count": min_series_count,
        "query_text": query_text,
        "requested_indicator": requested_indicator,
        "requested_countries": requested_countries,
        "query_type": query_type,
        "is_follow_up": bool(intent.isFollowUp),
    }
    if intent.needsDecomposition:
        expected_shape["decomposition_type"] = str(intent.decompositionType or "")
        expected_shape["decomposition_entities"] = list(intent.decompositionEntities or [])

    return ExecutionPlan(
        provider=provider,
        candidate_id=_candidate_id(provider, code),
        fetch_strategy="single_indicator",
        params=params,
        expected_shape=expected_shape,
        verification_checks=verification_checks,
    )
