"""Helpers for building explicit candidate evidence for prefetch decisions.

The goal of this module is to make the prefetch control contract explicit without
introducing new semantic heuristics. It normalizes already-produced candidate
metadata into typed evidence that later decision stages can inspect.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional

from pydantic import BaseModel, Field


class CandidateEvidence(BaseModel):
    """Comparable summary of one candidate considered by the prefetch path."""

    candidate_id: str
    provider: str
    code: str
    title: str
    description: str = ""
    unit: Optional[str] = None
    frequency: Optional[str] = None
    geography_scope: Optional[str] = None
    source: str = "option"
    executable: bool = False
    is_primary_guess: bool = False
    verification_expectations: List[str] = Field(default_factory=list)


def build_candidate_id(provider: str, code: str) -> str:
    """Return a stable candidate identifier for cross-stage bookkeeping."""
    provider_norm = str(provider or "").strip().upper()
    code_norm = str(code or "").strip().upper()
    return f"{provider_norm}:{code_norm}"


class CandidateEvidenceBuilder:
    """Build typed candidate evidence from resolved candidates and options."""

    DEFAULT_CLARIFICATION_LIMIT = 4
    MAX_CLARIFICATION_LIMIT = 10

    def build_prefetch_evidence(
        self,
        *,
        provider: str,
        resolved: Optional[Any],
        options: Iterable[dict[str, Any]],
        target_countries: Optional[list[str]] = None,
        primary_accepted: bool = False,
    ) -> list[CandidateEvidence]:
        """Build evidence for prefetch direct/clarify/unsupported decisions."""
        evidence: list[CandidateEvidence] = []

        resolved_code = str(getattr(resolved, "code", "") or "").strip()
        resolved_provider = str(getattr(resolved, "provider", "") or provider or "").strip()
        resolved_metadata = dict(getattr(resolved, "metadata", None) or {})
        if resolved_code and resolved_provider:
            evidence.append(
                CandidateEvidence(
                    candidate_id=build_candidate_id(resolved_provider, resolved_code),
                    provider=resolved_provider,
                    code=resolved_code,
                    title=str(getattr(resolved, "name", "") or resolved_code),
                    description=str(resolved_metadata.get("description", "") or ""),
                    unit=str(resolved_metadata.get("unit", "") or "") or None,
                    frequency=str(resolved_metadata.get("frequency", "") or "") or None,
                    geography_scope=self._infer_geography_scope(target_countries),
                    source="resolved",
                    executable=bool(primary_accepted),
                    is_primary_guess=True,
                    verification_expectations=self._verification_expectations(
                        provider=resolved_provider,
                        has_country_scope=bool(target_countries),
                    ),
                )
            )

        for option in options:
            option_provider = str(option.get("provider", "") or "").strip()
            option_code = str(option.get("code", "") or "").strip()
            if not option_provider or not option_code:
                continue
            evidence.append(
                CandidateEvidence(
                    candidate_id=build_candidate_id(option_provider, option_code),
                    provider=option_provider,
                    code=option_code,
                    title=str(option.get("title") or option.get("label") or option_code),
                    description=str(option.get("description", "") or ""),
                    geography_scope=self._infer_geography_scope(target_countries),
                    source=str(option.get("source", "option") or "option"),
                    executable=bool(option.get("executable", True)),
                    verification_expectations=self._verification_expectations(
                        provider=option_provider,
                        has_country_scope=bool(target_countries),
                    ),
                )
            )

        return evidence

    def clarification_option_limit(self, candidates: Iterable[CandidateEvidence]) -> int:
        """Return the shared clarification-width budget for a decision."""
        candidate_list = list(candidates)
        option_count = sum(1 for candidate in candidate_list if candidate.source == "option")
        if option_count <= 0:
            return self.DEFAULT_CLARIFICATION_LIMIT
        if option_count > self.DEFAULT_CLARIFICATION_LIMIT:
            return min(self.MAX_CLARIFICATION_LIMIT, option_count)
        return min(self.DEFAULT_CLARIFICATION_LIMIT, option_count)

    @staticmethod
    def _infer_geography_scope(target_countries: Optional[list[str]]) -> Optional[str]:
        if not target_countries:
            return None
        if len(target_countries) == 1:
            return "single_country"
        return "multi_country"

    @staticmethod
    def _verification_expectations(*, provider: str, has_country_scope: bool) -> list[str]:
        expectations = ["indicator_identity", "provider_executable"]
        if has_country_scope:
            expectations.append("country_scope")
        if str(provider or "").strip():
            expectations.append("provider_contract")
        return expectations
