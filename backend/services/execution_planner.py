"""Minimal typed execution-plan builder for the Phase 2 runtime contract."""

from __future__ import annotations

import re
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


def build_minimal_execution_plan(query: str, intent: ParsedIntent) -> ExecutionPlan:
    """Build the minimal typed execution contract used by Phase 2.

    This intentionally stays small: it captures the candidate/provider identity,
    the broad expected shape of the result, and the verification checks that the
    post-fetch verification stage must satisfy.
    """

    provider = normalize_provider_name(intent.apiProvider or "") or "UNKNOWN"
    params = dict(intent.parameters or {})
    code = _candidate_code(intent)
    query_text = str(query or intent.originalQuery or "").strip()
    lowered = query_text.lower()

    verification_checks = ["indicator_identity", "provider_executable"]
    min_series_count = 1

    if params.get("country") or params.get("countries"):
        verification_checks.append("country_scope")

    if any(token in lowered for token in ("highest", "lowest", "top ", "ranking", "rank ")):
        verification_checks.append("requires_multiple_series")
        min_series_count = 2

    if "spread" in lowered:
        verification_checks.append("requires_spread_metric")

    if re.search(r"\bm1\b", lowered):
        verification_checks.append("requires_m1_metric")

    if "growth" in lowered or "rate of change" in lowered:
        verification_checks.append("requires_growth_metric")

    expected_shape: dict[str, Any] = {
        "min_series_count": min_series_count,
        "query_text": query_text,
        "needs_multiple_series": min_series_count > 1,
    }
    if params.get("countries"):
        expected_shape["requested_countries"] = list(params.get("countries") or [])
    elif params.get("country"):
        expected_shape["requested_countries"] = [params.get("country")]

    return ExecutionPlan(
        provider=provider,
        candidate_id=_candidate_id(provider, code),
        fetch_strategy="single_indicator",
        params=params,
        expected_shape=expected_shape,
        verification_checks=verification_checks,
    )
