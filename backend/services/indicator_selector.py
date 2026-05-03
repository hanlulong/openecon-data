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

# Indicators database path
_INDICATORS_DB = Path(__file__).parent.parent / "data" / "indicators.db"

logger = logging.getLogger(__name__)

_settings: Optional[Settings] = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


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
    ) -> SelectionResult:
        """
        Select the best indicator for a query.

        Step 1: Find 20 nearest indicators using OpenAI embeddings
        Step 2: LLM picks (or asks user if ambiguous)

        Args:
            query: Natural language query (e.g., "female youth unemployment")
            provider: Data provider (e.g., "WorldBank", "FRED")
            country: Optional country context

        Returns:
            SelectionResult with selected code or options for user choice
        """
        # Step 1: Find candidates via embedding similarity (with scores)
        candidates, scores = self._get_candidates_with_scores(query, provider)

        if not candidates:
            return SelectionResult(code=None, source="no_candidates")

        if len(candidates) == 1:
            code = self._normalize_code(candidates[0][0], provider)
            return SelectionResult(code=code, name=candidates[0][1], source="single_match")

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
        result = await self._llm_pick(query, llm_candidates, provider, prefer_ask=candidates_are_ambiguous)

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
                        return retry_result
                    if self._is_llm_rejection(retry_result):
                        return retry_result
            return result

        # Step 3: If LLM couldn't decide, try with fewer/different candidates
        if not result or (not result.code and not result.needs_user_choice):
            # Retry with top 5 only (simpler for LLM)
            result = await self._llm_pick(query, candidates[:5], provider)

        if self._is_llm_rejection(result):
            return result

        fallback_code = self._normalize_code(candidates[0][0], provider)
        return result or SelectionResult(code=fallback_code, name=candidates[0][1], source="top_candidate")

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
        embedding_candidates: List[tuple[str, str]] = []
        embedding_scores: List[float] = []
        try:
            from .embedding_retrieval import get_embedding_retrieval
            er = get_embedding_retrieval()
            results = er.search(query, provider=provider, top_k=top_k)
            if results:
                embedding_candidates = [(r["code"], r["name"]) for r in results]
                embedding_scores = [r.get("score", 0.0) for r in results]
        except Exception as e:
            logger.warning("Embedding retrieval failed: %s", e)

        # FTS5 retrieval — uses bm25 lexical matching, complements embeddings
        fts5_candidates: List[tuple[str, str]] = []
        try:
            fts5_candidates = self._get_candidates_fts5(query, provider, top_k=20)
        except Exception as e:
            logger.debug("FTS5 retrieval failed: %s", e)

        # 2. Merge: FTS5, then embeddings. FTS5 results often contain
        # canonical lexical matches that embeddings miss. Embeddings provide
        # semantic paraphrases for novel queries.
        seen_codes: set = set()
        merged_candidates: List[tuple[str, str]] = []
        merged_scores: List[float] = []

        if embedding_candidates or fts5_candidates:
            # Take top FTS5 (canonical lexical matches)
            for code, name in fts5_candidates[:10]:
                if code not in seen_codes:
                    seen_codes.add(code)
                    merged_candidates.append((code, name))
                    merged_scores.append(0.55)

            # Then top embedding results (semantic paraphrases)
            for i, (code, name) in enumerate(embedding_candidates):
                if code not in seen_codes:
                    seen_codes.add(code)
                    merged_candidates.append((code, name))
                    merged_scores.append(
                        embedding_scores[i] if i < len(embedding_scores) else 0.0
                    )
                if len(merged_candidates) >= top_k:
                    break

            return merged_candidates, merged_scores

        return [], []

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
        """Enrich candidates with metadata from indicators.db (frequency, unit, end_date).

        Returns a list of dicts with keys: code, name, frequency, unit, end_date, discontinued.
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
                f"SELECT code, frequency, unit, end_date FROM indicators "
                f"WHERE provider = ? AND code IN ({placeholders})",
                [provider] + codes,
            )
            meta_map: Dict[str, Dict[str, Any]] = {}
            for row in cur.fetchall():
                meta_map[row["code"]] = {
                    "frequency": row["frequency"] or "",
                    "unit": row["unit"] or "",
                    "end_date": row["end_date"] or "",
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
