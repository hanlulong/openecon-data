"""
Indicator Selector — retrieval + LLM adjudication for ALL 330K indicators.

Architecture (decided 2026-04-01):
  Step 1: FTS5 + embedding retrieval → find candidate indicators
  Step 2: LLM picks, asks, or rejects the candidate set

No catalog injection or provider-code shortcut maps. Retrieval supplies the
candidate evidence; the LLM adjudicates the user's requested measure.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings
from ..utils.providers import normalize_provider_name

# Indicators database path
_INDICATORS_DB = Path(__file__).parent.parent / "data" / "indicators.db"

logger = logging.getLogger(__name__)

_settings: Optional[Settings] = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


_FREQUENCY_ALIASES = {
    "daily": {"daily", "day", "d"},
    "weekly": {"weekly", "week", "w"},
    "monthly": {"monthly", "month", "m"},
    "quarterly": {"quarterly", "quarter", "q"},
    "annual": {"annual", "annually", "yearly", "year", "a"},
}

_UNIT_CUE_RE = re.compile(
    r"\b(?:dollars?|u\.?s\.?|usd|percent(?:age)?|index|capita|ppp|"
    r"currency|millions?|billions?|thousands?|trillions?|units?)\b|"
    r"\b\d{4}\s*=\s*100\b",
    flags=re.IGNORECASE,
)

_MEASUREMENT_QUALIFIER_ALIASES = {
    "real": {"real", "inflation adjusted", "inflation-adjusted", "deflated", "cpi", "constant"},
    "nominal": {"nominal", "current"},
}


def _normalize_metadata_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _extract_requested_frequencies(text: str) -> set[str]:
    normalized = _normalize_metadata_text(text)
    tokens = set(normalized.split())
    found: set[str] = set()
    for canonical, aliases in _FREQUENCY_ALIASES.items():
        alias_tokens = {_normalize_metadata_text(alias) for alias in aliases}
        long_aliases = {alias for alias in alias_tokens if len(alias) > 1}
        one_letter_aliases = {alias for alias in alias_tokens if len(alias) == 1}
        if canonical == "annual":
            long_aliases = {
                alias
                for alias in long_aliases
                if not re.search(rf"\b\d+\s*{re.escape(alias)}\b", normalized)
            }
        if tokens & long_aliases:
            found.add(canonical)
            continue
        if any(re.search(rf"\(\s*{re.escape(alias)}\s*\)", normalized) for alias in one_letter_aliases):
            found.add(canonical)
    return found


def _frequency_matches(requested: set[str], candidate_frequency: str) -> bool:
    if not requested:
        return True
    normalized = _normalize_metadata_text(candidate_frequency)
    tokens = set(normalized.split())
    if not normalized:
        return False
    for canonical in requested:
        aliases = {
            _normalize_metadata_text(alias)
            for alias in _FREQUENCY_ALIASES.get(canonical, {canonical})
        }
        if canonical in tokens or tokens & aliases or normalized.startswith(canonical):
            return True
    return False


def _extract_requested_unit_tokens(text: str) -> set[str]:
    query = re.sub(r"\b(?:from|via|use)\s+[a-z][a-z0-9 ._-]*$", "", str(text or ""), flags=re.IGNORECASE)
    query = re.sub(
        r"\s+\((?:daily|weekly(?:,\s*ending\s+[a-z]+)?|monthly|quarterly|annual|yearly)\)\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    ).strip()
    match = re.search(r"\s+in\s+(?P<unit>[^,;:]+)$", query, flags=re.IGNORECASE)
    if not match:
        return set()
    unit_text = match.group("unit")
    if not _UNIT_CUE_RE.search(unit_text):
        return set()
    return {
        token
        for token in _normalize_metadata_text(unit_text).split()
        if len(token) > 1 and token not in {"and", "the", "per", "of", "at", "in"}
    }


def _unit_matches(requested_unit_tokens: set[str], candidate_unit: str) -> bool:
    if not requested_unit_tokens:
        return True
    candidate_tokens = {
        token
        for token in _normalize_metadata_text(candidate_unit).split()
        if len(token) > 1 and token not in {"and", "the", "per", "of", "at", "in"}
    }
    return bool(candidate_tokens and requested_unit_tokens <= candidate_tokens)


def _extract_requested_measurement_qualifiers(text: str) -> set[str]:
    tokens = _normalize_metadata_text(text).split()
    found: set[str] = set()
    for idx, token in enumerate(tokens):
        next_token = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if token == "real" and next_token != "estate":
            found.add("real")
        elif token == "nominal":
            found.add("nominal")
    normalized = _normalize_metadata_text(text)
    if "inflation adjusted" in normalized:
        found.add("real")
    return found


def _measurement_matches(requested: set[str], candidate_text: str) -> bool:
    if not requested:
        return True
    normalized = _normalize_metadata_text(candidate_text)
    return all(
        any(_normalize_metadata_text(alias) in normalized for alias in _MEASUREMENT_QUALIFIER_ALIASES.get(term, {term}))
        for term in requested
    )


LLM_SELECTION_PROMPT = """You are selecting the best economic indicator for a user's data query.

User query: "{query}"
Data provider: {provider}

Available indicators (numbered):
{options}

INSTRUCTIONS:

A query like "unemployment" clearly means the GENERAL/TOTAL unemployment rate.
Variants like "youth unemployment", "female unemployment" are SUBSETS — they are
NOT what the user meant unless they explicitly asked for them.

Only consider the query AMBIGUOUS when there are genuinely DIFFERENT MEASURES of
the same concept that the user might want. For example:
- "health spending" is ambiguous: % of GDP vs per capita vs absolute dollars
- "GDP" is ambiguous: current US$ vs constant dollars vs PPP
- "unemployment" is NOT ambiguous: it means the total rate

DECISION:
- Try to PICK one indicator when the candidate list contains a semantically valid answer.
- Use REJECT when NONE of the provided candidates answer the requested measure.
- Only use ASK when the user's query EXPLICITLY asks about something that has
  fundamentally different measurement approaches (e.g., "health spending" could
  be % of GDP OR per capita — genuinely different numbers).
- Single-word or broad queries like "GDP", "trade", "energy" should PICK the
  most standard version, NOT ask the user.
- Do NOT REJECT just because the query is broad. REJECT only when all candidates
  are clearly about different concepts, populations, geographies, frequencies,
  or units than the user's requested measure.

Reply with one of:
- "PICK: <number>" when a candidate answers the query.
- "ASK: <number>,<number>,..." when the user must choose between genuinely
  different measurement approaches.
- "REJECT: <short reason>" and "SEARCH: <alternative search terms>" when no
  candidate answers the requested measure.

Defaults for broad queries:
- "GDP" → GDP current US$ (most standard)
- "trade" → trade (% of GDP)
- "energy" → energy use per capita or total
- "emissions" → CO2 emissions total
- "education" → school enrollment primary
- "agriculture" → agriculture value added (% of GDP)
- "manufacturing" → manufacturing value added (% of GDP)

CRITICAL RULE — Match specificity of answer to specificity of question:
- "unemployment rate" → NATIONAL/AGGREGATE (never county or MSA level)
- "unemployment rate Florida" → STATE level Florida
- "unemployment rate Sarasota County" → COUNTY level (user was specific)
- If the user did NOT mention a state/county/city, NEVER pick a geographic sub-unit.
- If the user did NOT mention "female", "youth", "male", ALWAYS pick "total".
- If the user did NOT mention "seasonally adjusted" or "NSA", prefer seasonally adjusted.
- The answer should NEVER be more specific than what the user asked for.

CRITICAL RULE — Frequency matching:
- If the user query contains "monthly" / "month" → MUST pick a series with
  frequency=monthly (e.g., une_rt_m, not une_rt_a).
- If the user query contains "quarterly" / "quarter" → MUST pick frequency=quarterly.
- If the user query contains "annual" / "annually" / "yearly" → prefer frequency=annual.
- If the user query contains "daily" → prefer frequency=daily.
- If the user query contains "weekly" → prefer frequency=weekly.
- A monthly variant is BETTER than annual when monthly is requested, even if the
  annual variant has a slightly more popular code.  Frequency match is a HARD
  constraint, not a preference.
- If no frequency-matching candidate exists, fall back to the closest available
  (e.g., monthly if no daily exists), and note "frequency unavailable" in reasoning.

Selection rules:
- NEVER pick a DISCONTINUED series when active alternatives exist
- Prefer ACTIVE (recent data) over DISCONTINUED/OBSOLETE series
- Prefer NATIONAL/AGGREGATE over state/county/MSA/regional variants
- Prefer TOTAL over demographic subsets (female, male, youth, elderly)
- For direct count/number/total requests, prefer an indicator that measures the
  requested entity count directly. Do not pick distribution, ratio, subset,
  account-allocation, or breakdown tables unless the user explicitly asks for
  that narrower measure.
- Prefer SEASONALLY ADJUSTED over not adjusted (NSA)
- Prefer modeled ILO estimates (SL.*) over Jobs Indicators (JI.*)
- Prefer % of GDP over absolute values for cross-country comparison
- Prefer gross enrollment over net unless user specifies "net"
- Prefer SHORTER/SIMPLER indicator codes when concepts are identical
- Prefer the most GENERAL version — never pick a more specific variant than asked

FRED-specific rules for near-identical series:
- Flow of Funds codes (BOGZ1F...): prefer L (Level) over R (Revaluation),
  A (Transactions), U (Volume changes). Users want LEVELS unless specified.
- When two codes differ only by trailing letters (Q vs A, SA vs NSA, MM vs YY):
  prefer the STANDARD version (SA, Annual, or the shorter code)
- "Total Private" is more general than "Construction" or other industry subsets
- Prefer US Dollar denomination over other currencies unless user specifies

When in doubt between valid variants, PICK the most general — the user can
always ask for a variant. When none are valid, REJECT and provide better SEARCH
terms."""


class IndicatorSelector:
    """Hybrid retrieval plus LLM indicator selection."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or _get_settings()

    async def select(
        self,
        query: str,
        provider: str,
        country: Optional[str] = None,
        metadata_query: Optional[str] = None,
    ) -> SelectionResult:
        """
        Select the best indicator for a query.

        Step 1: Find 20 nearest indicators using OpenAI embeddings
        Step 2: LLM picks (or asks user if ambiguous)

        Args:
            query: Natural language query (e.g., "female youth unemployment")
            provider: Data provider (e.g., "WorldBank", "FRED")
            country: Optional country context
            metadata_query: Optional fuller query text used only for explicit
                frequency/unit/measurement metadata constraints.

        Returns:
            SelectionResult with selected code or options for user choice
        """
        telemetry_enabled = bool(getattr(self._settings, "indicator_telemetry_enabled", False))
        fusion_mode = str(getattr(self._settings, "indicator_fusion", "legacy") or "legacy").lower()

        # Step 1: Find candidates via embedding similarity (with scores)
        candidates, scores = self._get_candidates_with_scores(query, provider)

        if not candidates:
            return SelectionResult(code=None, source="no_candidates")

        # Step 1.5: If top candidates have very similar scores, retrieval can't
        # confidently distinguish them. Tell the LLM to ASK the user instead of
        # guessing. This reduces overconfident wrong picks.
        # Threshold: if top 3+ candidates are within 0.03 cosine similarity,
        # they're too similar for automated selection.
        candidates_are_ambiguous = self._scores_are_ambiguous(scores)
        if candidates_are_ambiguous:
            logger.info(
                "🔍 Top candidates very similar (spread=%.4f < 0.03) — will prefer ASK",
                scores[0] - scores[2],
            )

        # Step 2: LLM picks from top 20 candidates (embedding retrieves 50 for better recall)
        llm_candidates = candidates[:20]
        metadata_constraint_query = metadata_query or query
        result = await self._llm_pick(query, llm_candidates, provider, prefer_ask=candidates_are_ambiguous)
        result = await self._retry_if_metadata_conflict(metadata_constraint_query, result, llm_candidates, provider)

        # Step 2.5: If the LLM says the whole candidate set is off-target,
        # honor its alternative search terms with one bounded research retry.
        if self._is_llm_rejection(result):
            retry_query = str(getattr(result, "retry_query", "") or "").strip()
            if retry_query and retry_query.lower() != query.lower():
                logger.info(
                    "🔎 LLM rejected candidate set for '%s'; retrying selector search with '%s'",
                    query[:80],
                    retry_query[:80],
                )
                retry_candidates, retry_scores = self._get_candidates_with_scores(retry_query, provider)
                if retry_candidates:
                    retry_ambiguous = self._scores_are_ambiguous(retry_scores)
                    retry_result = await self._llm_pick(
                        retry_query,
                        retry_candidates[:20],
                        provider,
                        prefer_ask=retry_ambiguous,
                    )
                    if retry_result and (retry_result.code or retry_result.needs_user_choice):
                        return await self._retry_if_metadata_conflict(
                            metadata_constraint_query,
                            retry_result,
                            retry_candidates[:20],
                            provider,
                        )
                    if self._is_llm_rejection(retry_result):
                        return retry_result
            return result

        # Step 3: If LLM couldn't decide, try with fewer/different candidates
        if not result or (not result.code and not result.needs_user_choice):
            # Retry with top 5 only (simpler for LLM)
            result = await self._llm_pick(query, candidates[:5], provider)
            result = await self._retry_if_metadata_conflict(metadata_constraint_query, result, candidates[:5], provider)

        if self._is_llm_rejection(result):
            return result

        if result and (result.code or result.needs_user_choice):
            self._emit_telemetry(
                telemetry_enabled,
                fusion_mode,
                query,
                provider,
                candidates,
                result,
            )
            return result

        logger.info(
            "🔵 IndicatorSelector made no final selection for '%s'; refusing top-candidate fallback",
            query[:80],
        )
        final = SelectionResult(code=None, source="no_decision")
        self._emit_telemetry(telemetry_enabled, fusion_mode, query, provider, candidates, final)
        return final

    @staticmethod
    def _emit_telemetry(
        enabled: bool,
        fusion_mode: str,
        query: str,
        provider: str,
        fused_candidates: List[tuple[str, str]],
        result: SelectionResult,
    ) -> None:
        """Phase 2.1 baseline telemetry — structured per-query record.

        Logs the fused candidate codes + the LLM's final pick + the
        selection source. Used as a baseline to compare RRF vs legacy
        fusion during the Phase 2.2 shadow window before any default flip.
        Gated by INDICATOR_TELEMETRY_ENABLED so dev/test traffic doesn't
        flood logs.
        """
        if not enabled:
            return
        import json as _json
        try:
            top_fused = [code for code, _name in (fused_candidates or [])[:10]]
            payload = {
                "fusion": fusion_mode,
                "provider": str(provider or ""),
                "query": str(query or "")[:200],
                "fused_top10": top_fused,
                "final_code": getattr(result, "code", None),
                "final_source": getattr(result, "source", None),
                "needs_user_choice": bool(getattr(result, "needs_user_choice", False)),
            }
            logger.info("indicator_selector_telemetry %s", _json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # never raise from telemetry path
            logger.debug("telemetry emit failed: %s", exc)

    @staticmethod
    def _scores_are_ambiguous(scores: List[float]) -> bool:
        if len(scores) >= 3 and all(score > 0 for score in scores[:3]):
            first, second, third = scores[:3]
            # FTS5 candidates and embedding candidates are merged by evidence
            # source, so score order is not guaranteed. Only use the score-gap
            # ambiguity signal when the first three scores are actually ordered.
            if first >= second >= third:
                return (first - third) < 0.03
        return False

    @staticmethod
    def _is_llm_rejection(result: Optional["SelectionResult"]) -> bool:
        return bool(result and getattr(result, "source", "") == "llm_reject")

    def _get_candidates_with_scores(
        self, query: str, provider: str, top_k: int = 50,
    ) -> tuple[List[tuple[str, str]], List[float]]:
        """Step 1: Find nearest indicators using hybrid FTS5 + embedding retrieval.

        Cycle 29 fix: Audit found that embedding-only retrieval has only ~60%
        accuracy on FRED variants because:
        - Embeddings use only `name` field (no aliases or descriptions)
        - Verbose BLS/Census titles ("Sticky Price CPI less Food and Energy")
          swamp canonical series ("Core CPI" / CPILFESL)
        - FTS5 nails lexical/acronym matches that embeddings miss

        Solution: merge BOTH retrieval methods. FTS5 gets the canonical
        codes that match query vocabulary; embeddings get semantic
        paraphrases.  The union (deduped) is passed to the LLM for selection.

        Semantic matching comes from retrieval plus LLM adjudication, not forced
        provider-code shortcuts.
        """

        # 1. Run BOTH retrievals in parallel-ish (sequential but cheap)
        # Normalize runtime aliases to the provider names stored in the 330K
        # catalog/embedding metadata.  Without this, canonical runtime names such
        # as STATSCAN miss the StatsCan embedding partition and silently degrade
        # to lexical-only retrieval.
        retrieval_provider = self._catalog_provider_name(provider)
        embedding_candidates: List[tuple[str, str]] = []
        embedding_scores: List[float] = []
        try:
            from .embedding_retrieval import get_embedding_retrieval
            er = get_embedding_retrieval()
            results = er.search(query, provider=retrieval_provider, top_k=top_k)
            if results:
                embedding_candidates = [(r["code"], r["name"]) for r in results]
                embedding_scores = [r.get("score", 0.0) for r in results]
        except Exception as e:
            logger.warning("Embedding retrieval failed: %s", e)

        # FTS5 retrieval — uses bm25 lexical matching, complements embeddings
        fts5_candidates: List[tuple[str, str]] = []
        try:
            fts5_candidates = self._get_candidates_fts5(query, retrieval_provider, top_k=20)
        except Exception as e:
            logger.debug("FTS5 retrieval failed: %s", e)

        # 2. Merge with score-aware hybrid ordering.  FTS5 is excellent recall
        # evidence for lexical/provider-title surfaces, but it must not occupy the
        # whole front of the LLM prompt ahead of much stronger embedding matches.
        # Keep both evidence sources, dedupe by provider code, and let the final
        # LLM selector adjudicate semantics.
        if embedding_candidates or fts5_candidates:
            merged_by_code: Dict[str, Dict[str, Any]] = {}

            for rank, (code, name) in enumerate(fts5_candidates[:20]):
                code_text = str(code or "").strip()
                if not code_text:
                    continue
                entry = merged_by_code.setdefault(
                    code_text,
                    {
                        "candidate": (code, name),
                        "embedding_score": None,
                        "embedding_rank": None,
                        "fts_rank": None,
                    },
                )
                if entry["fts_rank"] is None:
                    entry["fts_rank"] = rank

            for rank, (code, name) in enumerate(embedding_candidates):
                code_text = str(code or "").strip()
                if not code_text:
                    continue
                score = embedding_scores[rank] if rank < len(embedding_scores) else 0.0
                entry = merged_by_code.setdefault(
                    code_text,
                    {
                        "candidate": (code, name),
                        "embedding_score": None,
                        "embedding_rank": None,
                        "fts_rank": None,
                    },
                )
                # Prefer the embedding-sourced display name when this source has
                # stronger numeric evidence; FTS-only candidates still remain as
                # recall candidates below embedding-backed matches.
                if entry["embedding_score"] is None or score > float(entry["embedding_score"]):
                    entry["candidate"] = (code, name)
                    entry["embedding_score"] = score
                    entry["embedding_rank"] = rank

            # Two fusion strategies behind INDICATOR_FUSION:
            # - "legacy" (default): the score-aware merge with magic constants
            #   (0.02, 0.55, 0.10, 0.005). Kept as the rollback path during the
            #   shadow-mode validation period for Phase 2.2.
            # - "rrf": canonical parameterless Reciprocal Rank Fusion,
            #   score(c) = Σ 1/(k + rank_i(c)). One constant (k), one citation
            #   (Cormack et al., SIGIR 2009), encoder-independent. Replaces
            #   the magic constants entirely. Default off until shadow shows
            #   parity per docs/DEEP_REVIEW_2026-05-30.md §6 invariant #8.
            rrf_k = max(1, int(getattr(self._settings, "indicator_rrf_k", 60) or 60))
            fusion_mode = str(getattr(self._settings, "indicator_fusion", "legacy") or "legacy").lower()

            def _effective_rank(item: tuple[str, Dict[str, Any]]) -> tuple[float, int, int, str]:
                code, entry = item
                embedding_score = entry["embedding_score"]
                fts_rank = entry["fts_rank"]
                embedding_rank = entry["embedding_rank"]
                if embedding_score is not None:
                    lexical_boost = 0.02 / (int(fts_rank) + 1) if fts_rank is not None else 0.0
                    effective_score = float(embedding_score) + lexical_boost
                else:
                    # FTS-only rows are useful recall candidates, but their
                    # synthetic score must stay below real embedding evidence.
                    effective_score = 0.55 - min(0.10, 0.005 * int(fts_rank or 0))
                return (
                    -effective_score,
                    int(embedding_rank) if embedding_rank is not None else top_k + int(fts_rank or 0),
                    int(fts_rank) if fts_rank is not None else top_k,
                    code,
                )

            def _rrf_rank(item: tuple[str, Dict[str, Any]]) -> tuple[float, str]:
                """Reciprocal Rank Fusion: score(c) = Σ 1/(k + rank_i(c))."""
                _code, entry = item
                fts_rank = entry["fts_rank"]
                embedding_rank = entry["embedding_rank"]
                score = 0.0
                if fts_rank is not None:
                    score += 1.0 / (rrf_k + int(fts_rank) + 1)
                if embedding_rank is not None:
                    score += 1.0 / (rrf_k + int(embedding_rank) + 1)
                return (-score, _code)

            if fusion_mode == "rrf":
                ranked_entries = sorted(merged_by_code.items(), key=_rrf_rank)[:top_k]
                merged_candidates = [entry["candidate"] for _code, entry in ranked_entries]
                merged_scores = [-_rrf_rank((code, entry))[0] for code, entry in ranked_entries]
            else:
                ranked_entries = sorted(merged_by_code.items(), key=_effective_rank)[:top_k]
                merged_candidates = [entry["candidate"] for _code, entry in ranked_entries]
                merged_scores = [
                    -_effective_rank((code, entry))[0]
                    for code, entry in ranked_entries
                ]

            prioritized_candidates, prioritized_scores = self._prioritize_candidates_by_provider_surface(
                merged_candidates,
                merged_scores,
                retrieval_provider,
            )
            return self._prioritize_candidates_by_query_metadata(
                query,
                prioritized_candidates,
                prioritized_scores,
                retrieval_provider,
            )

        return [], []

    @staticmethod
    def _catalog_provider_name(provider: str) -> str:
        """Return the provider spelling used by indicators.db/embeddings."""
        canonical = normalize_provider_name(provider or "")
        return {
            "STATSCAN": "StatsCan",
        }.get(canonical, provider)

    def _prioritize_candidates_by_query_metadata(
        self,
        query: str,
        candidates: List[tuple[str, str]],
        scores: List[float],
        provider: str,
    ) -> tuple[List[tuple[str, str]], List[float]]:
        """Order candidates by explicit metadata constraints in the query.

        This is not a semantic shortcut: it never creates candidates or maps a
        concept to a code.  It only moves already-retrieved provider catalog
        candidates whose metadata satisfies explicit user constraints
        (frequency, unit, or price-basis qualifier) ahead of near-neighbors
        that contradict those constraints.
        """

        if not candidates:
            return candidates, scores

        requested_frequencies = _extract_requested_frequencies(query)
        requested_unit_tokens = _extract_requested_unit_tokens(query)
        requested_measurements = _extract_requested_measurement_qualifiers(query)
        if not (requested_frequencies or requested_unit_tokens or requested_measurements):
            return candidates, scores

        enriched = self._enrich_candidates(candidates, provider)
        has_frequency_match = requested_frequencies and any(
            _frequency_matches(requested_frequencies, str(item.get("frequency") or ""))
            for item in enriched
        )
        has_unit_match = requested_unit_tokens and any(
            _unit_matches(requested_unit_tokens, str(item.get("unit") or ""))
            for item in enriched
        )
        has_measurement_match = requested_measurements and any(
            _measurement_matches(requested_measurements, self._candidate_metadata_text(item))
            for item in enriched
        )
        if not (has_frequency_match or has_unit_match or has_measurement_match):
            return candidates, scores

        paired = list(zip(range(len(candidates)), candidates, scores, enriched))

        def _rank(item: tuple[int, tuple[str, str], float, Dict[str, Any]]) -> tuple[int, int, int, int]:
            index, _candidate, _score, meta = item
            frequency_penalty = (
                0
                if not has_frequency_match
                or _frequency_matches(requested_frequencies, str(meta.get("frequency") or ""))
                else 1
            )
            unit_penalty = (
                0
                if not has_unit_match
                or _unit_matches(requested_unit_tokens, str(meta.get("unit") or ""))
                else 1
            )
            measurement_penalty = (
                0
                if not has_measurement_match
                or _measurement_matches(requested_measurements, self._candidate_metadata_text(meta))
                else 1
            )
            return frequency_penalty, unit_penalty, measurement_penalty, index

        paired.sort(key=_rank)
        return [candidate for _idx, candidate, _score, _meta in paired], [
            score for _idx, _candidate, score, _meta in paired
        ]

    async def _retry_if_metadata_conflict(
        self,
        query: str,
        result: Optional["SelectionResult"],
        candidates: List[tuple[str, str]],
        provider: str,
    ) -> Optional["SelectionResult"]:
        """Retry LLM selection if a PICK contradicts explicit metadata constraints."""

        if not result or not result.code or not candidates:
            return result

        compatible = self._metadata_compatible_subset(query, candidates, provider)
        if not compatible:
            return result

        normalized_selected = self._normalize_code(str(result.code or ""), provider)
        compatible_codes = {
            self._normalize_code(str(code or ""), provider)
            for code, _name in compatible
        }
        if normalized_selected in compatible_codes:
            return result

        retry_result = await self._llm_pick(query, compatible[:20], provider, prefer_ask=False)
        if retry_result and (retry_result.code or retry_result.needs_user_choice):
            return retry_result

        return SelectionResult(
            code=None,
            source="metadata_conflict",
            rejection_reason="LLM pick contradicted explicit frequency/unit/measurement metadata.",
        )

    def _metadata_compatible_subset(
        self,
        query: str,
        candidates: List[tuple[str, str]],
        provider: str,
    ) -> List[tuple[str, str]]:
        requested_frequencies = _extract_requested_frequencies(query)
        requested_unit_tokens = _extract_requested_unit_tokens(query)
        requested_measurements = _extract_requested_measurement_qualifiers(query)
        if not (requested_frequencies or requested_unit_tokens or requested_measurements):
            return []

        enriched = self._enrich_candidates(candidates, provider)
        paired = list(zip(candidates, enriched))
        filtered = paired
        if requested_frequencies and any(
            _frequency_matches(requested_frequencies, str(meta.get("frequency") or ""))
            for _candidate, meta in paired
        ):
            filtered = [
                (candidate, meta)
                for candidate, meta in filtered
                if _frequency_matches(requested_frequencies, str(meta.get("frequency") or ""))
            ]
        if requested_unit_tokens and any(
            _unit_matches(requested_unit_tokens, str(meta.get("unit") or ""))
            for _candidate, meta in paired
        ):
            filtered = [
                (candidate, meta)
                for candidate, meta in filtered
                if _unit_matches(requested_unit_tokens, str(meta.get("unit") or ""))
            ]
        if requested_measurements and any(
            _measurement_matches(requested_measurements, self._candidate_metadata_text(meta))
            for _candidate, meta in paired
        ):
            filtered = [
                (candidate, meta)
                for candidate, meta in filtered
                if _measurement_matches(requested_measurements, self._candidate_metadata_text(meta))
            ]

        if len(filtered) >= len(paired):
            return []
        return [candidate for candidate, _meta in filtered]

    @staticmethod
    def _candidate_metadata_text(item: Dict[str, Any]) -> str:
        return " ".join(
            str(item.get(key) or "")
            for key in ("name", "description", "keywords", "category", "unit")
        )

    def _prioritize_candidates_by_provider_surface(
        self,
        candidates: List[tuple[str, str]],
        scores: List[float],
        provider: str,
    ) -> tuple[List[tuple[str, str]], List[float]]:
        """Prefer candidates from public executable provider surfaces.

        This is candidate ordering only; the LLM still adjudicates the final
        indicator.  For IMF, the local catalog includes legacy/auxiliary rows
        whose short codes can look like good natural-language answers but are
        not the public DataMapper WEO/regional surfaces used by the runtime.
        Exact user-supplied codes still pass through the provider separately.
        """
        if normalize_provider_name(provider or "") != "IMF" or not candidates:
            return candidates, scores

        try:
            from .indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
        except Exception as exc:
            logger.debug("IMF candidate prioritization skipped: %s", exc)
            return candidates, scores

        def _rank(item: tuple[int, tuple[str, str], float]) -> tuple[int, int]:
            index, (code, _name), _score = item
            try:
                metadata = lookup.get("IMF", code)
            except Exception:
                metadata = None
            category = str((metadata or {}).get("category") or "").strip().upper()
            code_text = str(code or "").strip().upper()
            has_namespace = (
                "_" in code_text
                or "." in code_text
                or any(ch.isdigit() for ch in code_text)
            )
            if category == "WEO":
                surface_rank = 0
            elif category.endswith("REO"):
                surface_rank = 1
            elif has_namespace:
                surface_rank = 2
            else:
                surface_rank = 3
            return surface_rank, index

        paired = list(zip(range(len(candidates)), candidates, scores))
        paired.sort(key=_rank)
        return [candidate for _idx, candidate, _score in paired], [
            score for _idx, _candidate, score in paired
        ]

    def _get_candidates_fts5(
        self, query: str, provider: str, top_k: int = 20,
    ) -> List[tuple[str, str]]:
        """FTS5 fallback when embeddings unavailable."""
        try:
            from .indicator_database import IndicatorDatabase
            db = IndicatorDatabase()
            conn = db._get_connection()
            cur = conn.cursor()

            safe_query = query
            for char in ['"', "'", '(', ')', '*', '-', ':', '^', ',']:
                safe_query = safe_query.replace(char, ' ')
            words = [w.strip() for w in safe_query.split() if w.strip() and len(w.strip()) > 2]
            if not words:
                return []

            fts_query = " OR ".join([f'"{w}"*' for w in words])
            cur.execute(
                """SELECT i.code, i.name FROM indicators_fts f
                JOIN indicators i ON f.rowid = i.id
                WHERE indicators_fts MATCH ? AND i.provider = ?
                ORDER BY bm25(indicators_fts, 0, 3.0, 10.0, 1.0, 3.0, 2.0, 2.0)
                LIMIT ?""",
                (fts_query, provider, top_k),
            )
            return cur.fetchall()
        except Exception as e:
            logger.warning("FTS5 fallback failed: %s", e)

        return []

    def _enrich_candidates(
        self,
        candidates: List[tuple[str, str]],
        provider: str,
    ) -> List[Dict[str, Any]]:
        """Enrich candidates with metadata from indicators.db.

        Returns dicts with frequency/unit/activity plus compact evidence fields.
        This gives the LLM visibility into whether a series is active or obsolete.
        """
        enriched = []
        try:
            conn = sqlite3.connect(str(_INDICATORS_DB))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            codes = [c[0] for c in candidates]
            # Build a lookup map: (provider, code) -> metadata row
            placeholders = ",".join(["?"] * len(codes))
            cur.execute(
                f"SELECT code, frequency, unit, end_date, category, description, keywords FROM indicators "
                f"WHERE provider = ? AND code IN ({placeholders})",
                [self._catalog_provider_name(provider)] + codes,
            )
            meta_map: Dict[str, Dict[str, Any]] = {}
            for row in cur.fetchall():
                meta_map[row["code"]] = {
                    "frequency": row["frequency"] or "",
                    "unit": row["unit"] or "",
                    "end_date": row["end_date"] or "",
                    "category": row["category"] or "",
                    "description": row["description"] or "",
                    "keywords": row["keywords"] or "",
                }
            conn.close()
        except Exception as e:
            logger.warning("Failed to enrich candidates from DB: %s", e)
            meta_map = {}

        for code, name in candidates:
            meta = meta_map.get(code, {})
            end_date = meta.get("end_date", "")
            # Mark as discontinued if last observation is older than 5 years.
            # Use a sliding 5-year window relative to today rather than a
            # hardcoded year, so 2020-2025 series aren't incorrectly flagged
            # as dead in 2026+.
            discontinued = False
            if end_date:
                try:
                    from datetime import datetime as _dt
                    year = int(end_date[:4])
                    if year < (_dt.now().year - 5):
                        discontinued = True
                except (ValueError, IndexError):
                    pass

            enriched.append({
                "code": code,
                "name": name,
                "frequency": meta.get("frequency", ""),
                "unit": meta.get("unit", ""),
                "end_date": end_date,
                "category": meta.get("category", ""),
                "description": meta.get("description", ""),
                "keywords": meta.get("keywords", ""),
                "discontinued": discontinued,
            })

        return enriched

    async def _llm_pick(
        self,
        query: str,
        candidates: List[tuple[str, str]],
        provider: str,
        prefer_ask: bool = False,
    ) -> Optional[SelectionResult]:
        """Step 2: LLM picks the best indicator from candidates."""
        # Enrich candidates with metadata so the LLM can see frequency,
        # unit, and whether a series is discontinued/obsolete.
        enriched = self._enrich_candidates(candidates, provider)

        option_lines = []
        for i, item in enumerate(enriched):
            parts = [f"{i + 1}. [{item['code']}] {item['name']}"]
            meta_parts = []
            if item["frequency"]:
                meta_parts.append(item["frequency"])
            if item["unit"]:
                meta_parts.append(item["unit"])
            if item.get("category"):
                meta_parts.append(f"category: {item['category']}")
            evidence_text = " ".join(
                str(item.get(key) or "").strip()
                for key in ("keywords", "description")
            ).strip()
            if evidence_text:
                evidence_text = re.sub(r"\s+", " ", evidence_text)[:180]
                meta_parts.append(f"evidence: {evidence_text}")
            if item["end_date"]:
                meta_parts.append(f"last data: {item['end_date'][:10]}")
            if item["discontinued"]:
                meta_parts.append("DISCONTINUED")
            if meta_parts:
                parts.append(f"  ({', '.join(meta_parts)})")
            option_lines.append("".join(parts))

        options = "\n".join(option_lines)

        prompt = LLM_SELECTION_PROMPT.format(
            query=query, provider=provider, options=options,
        )

        # When candidates are very similar (embedding scores within 0.03),
        # tell the LLM to prefer ASK over PICK to avoid overconfident wrong picks.
        if prefer_ask:
            prompt += (
                "\n\nIMPORTANT: The available indicators are VERY similar to each other. "
                "Unless you are HIGHLY confident one is clearly the best match, "
                "use ASK to let the user choose from the top 3-5 most relevant options. "
                "Do not ASK merely because retrieval scores are close when one option is the "
                "general/direct count or total and the alternatives are breakdowns, distributions, "
                "sub-populations, or account tables that the user did not request."
            )

        settings = self._settings
        if settings.llm_provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            }
        else:
            url = (settings.llm_base_url or "http://localhost:8000").rstrip("/") + "/v1/chat/completions"
            headers = {"Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url, headers=headers,
                    json={
                        "model": settings.llm_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 500,
                        "temperature": 0,
                    },
                )
                data = resp.json()
                content = (data["choices"][0]["message"].get("content") or "").strip()

                if not content:
                    return None

                return self._parse_llm_response(content, candidates, provider, query)

        except Exception as e:
            logger.warning("LLM selection failed: %s", e)

        return None

    def _parse_llm_response(
        self,
        content: str,
        candidates: List[tuple[str, str]],
        provider: str,
        query: str = "",
    ) -> Optional["SelectionResult"]:
        """Parse the selector LLM's control response.

        The parser is intentionally mechanical: it extracts option numbers only
        from explicit PICK/ASK/REJECT control lines so years, codes, or other
        explanatory numbers cannot silently change the selected candidate.
        """
        lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
        if not lines:
            return None

        # Parse REJECT before PICK/ASK so rejection reasons containing "pick" do
        # not get misinterpreted as a selection.
        reject_line = next((line for line in lines if re.match(r"^REJECT\b", line, re.IGNORECASE)), "")
        if reject_line:
            search_line = next((line for line in lines if re.match(r"^SEARCH\b", line, re.IGNORECASE)), "")
            rejection_reason = re.sub(r"^REJECT\s*[:\-]?\s*", "", reject_line, flags=re.IGNORECASE).strip()
            retry_query = re.sub(r"^SEARCH\s*[:\-]?\s*", "", search_line, flags=re.IGNORECASE).strip()
            logger.info(
                "🚫 LLM rejected all candidates for '%s': %s",
                query[:40],
                rejection_reason[:120],
            )
            return SelectionResult(
                code=None,
                source="llm_reject",
                rejection_reason=rejection_reason,
                retry_query=retry_query,
            )

        pick_line = next((line for line in lines if re.search(r"\bPICK\b", line, re.IGNORECASE)), "")
        if pick_line:
            match = re.search(r"\bPICK\b\s*[:#\-]?\s*(\d{1,3})\b", pick_line, re.IGNORECASE)
            if match:
                num = int(match.group(1)) - 1
                if 0 <= num < len(candidates):
                    code, name = candidates[num]
                    code = self._normalize_code(code, provider)
                    logger.info("🎯 LLM picked: '%s' → %s (%s)", query[:40], code, name[:40])
                    return SelectionResult(code=code, name=name, source="llm_pick")

        ask_line = next((line for line in lines if re.search(r"\bASK\b", line, re.IGNORECASE)), "")
        if ask_line:
            match = re.search(r"\bASK\b\s*[:#\-]?\s*([0-9,\s]+)", ask_line, re.IGNORECASE)
            number_text = match.group(1) if match else ""
            options_list = []
            for digits in re.findall(r"\d{1,3}", number_text):
                idx = int(digits) - 1
                if 0 <= idx < len(candidates):
                    code, name = candidates[idx]
                    code = self._normalize_code(code, provider)
                    if not any(o["code"] == code for o in options_list):
                        options_list.append({"code": code, "name": name})
            if options_list:
                logger.info("🔵 LLM asks user: '%s' → %d options", query[:40], len(options_list))
                return SelectionResult(code=None, source="user_choice", options=options_list[:10])

        # Conservative fallback for non-compliant but explicit responses such as
        # "choose option 2". Do not extract arbitrary digits from explanations.
        fallback = re.search(
            r"(?:\b(?:choose|choice|option|indicator|number)\b|#)\s*[:#\-]?\s*(\d{1,3})\b",
            content,
            re.IGNORECASE,
        )
        if fallback:
            num = int(fallback.group(1)) - 1
            if 0 <= num < len(candidates):
                code, name = candidates[num]
                code = self._normalize_code(code, provider)
                return SelectionResult(code=code, name=name, source="llm_pick")

        return None

    @staticmethod
    def _normalize_code(code: str, provider: str) -> str:
        """Normalize provider-specific code prefixes."""
        if provider.upper() == "BIS" and code.startswith("BIS_"):
            return code[4:]
        return code


class SelectionResult:
    """Result of indicator selection."""

    def __init__(
        self,
        code: Optional[str] = None,
        name: Optional[str] = None,
        source: str = "unknown",
        options: Optional[List[Dict[str, str]]] = None,
        rejection_reason: str = "",
        retry_query: str = "",
    ):
        self.code = code
        self.name = name
        self.source = source
        self.options = options
        self.rejection_reason = rejection_reason
        self.retry_query = retry_query

    @property
    def needs_user_choice(self) -> bool:
        return self.source == "user_choice" and bool(self.options)

    @property
    def rejected_candidates(self) -> bool:
        return self.source == "llm_reject"

    def __repr__(self) -> str:
        if self.needs_user_choice:
            return f"SelectionResult(user_choice, {len(self.options)} options)"
        if self.rejected_candidates:
            return f"SelectionResult(llm_reject, retry_query={self.retry_query!r})"
        return f"SelectionResult(code={self.code}, source={self.source})"
