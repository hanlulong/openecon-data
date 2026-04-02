"""
Indicator Selector — Simple two-step resolution for ALL 330K indicators.

Architecture (decided 2026-04-01):
  Step 1: OpenAI embedding → find 20 nearest indicators
  Step 2: LLM picks best match (multi-round if needed)

No catalog, no FTS5, no name matching. Just embedding + LLM.
The embedding understands semantic meaning; the LLM understands context.
"""

from __future__ import annotations

import logging
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
- ALWAYS try to PICK one indicator. Most queries have an obvious default.
- Only use ASK when the user's query EXPLICITLY asks about something that has
  fundamentally different measurement approaches (e.g., "health spending" could
  be % of GDP OR per capita — genuinely different numbers).
- Single-word or broad queries like "GDP", "trade", "energy" should PICK the
  most standard version, NOT ask the user.

Reply with "PICK: <number>" (strongly preferred) or "ASK: <number>,<number>,..."

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

Selection rules:
- NEVER pick a DISCONTINUED series when active alternatives exist
- Prefer ACTIVE (recent data) over DISCONTINUED/OBSOLETE series
- Prefer NATIONAL/AGGREGATE over state/county/MSA/regional variants
- Prefer TOTAL over demographic subsets (female, male, youth, elderly)
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

When in doubt, PICK the most general — the user can always ask for a variant"""


class IndicatorSelector:
    """Two-step indicator resolution: embed → LLM pick."""

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

        # Step 1.5: If top candidates have very similar scores, the embedding
        # can't confidently distinguish them. Tell the LLM to ASK the user
        # instead of guessing. This reduces overconfident wrong picks.
        # Threshold: if top 3+ candidates are within 0.03 cosine similarity,
        # they're too similar for automated selection.
        candidates_are_ambiguous = False
        if len(scores) >= 3 and scores[0] > 0:
            score_spread = scores[0] - scores[2]  # gap between #1 and #3
            if score_spread < 0.03:
                candidates_are_ambiguous = True
                logger.info(
                    "🔍 Top candidates very similar (spread=%.4f < 0.03) — will prefer ASK",
                    score_spread,
                )

        # Step 2: LLM picks from top 20 candidates (embedding retrieves 50 for better recall)
        llm_candidates = candidates[:20]
        result = await self._llm_pick(query, llm_candidates, provider, prefer_ask=candidates_are_ambiguous)

        # Step 3: If LLM couldn't decide, try with fewer/different candidates
        if not result or (not result.code and not result.needs_user_choice):
            # Retry with top 5 only (simpler for LLM)
            result = await self._llm_pick(query, candidates[:5], provider)

        return result or SelectionResult(code=candidates[0][0], name=candidates[0][1], source="top_candidate")

    def _get_candidates_with_scores(
        self, query: str, provider: str, top_k: int = 50,
    ) -> tuple[List[tuple[str, str]], List[float]]:
        """Step 1: Find nearest indicators with similarity scores."""
        try:
            from .embedding_retrieval import get_embedding_retrieval
            er = get_embedding_retrieval()
            results = er.search(query, provider=provider, top_k=top_k)
            if results:
                candidates = [(r["code"], r["name"]) for r in results]
                scores = [r.get("score", 0.0) for r in results]
                return candidates, scores
        except Exception as e:
            logger.warning("Embedding retrieval failed: %s", e)

        # Fallback
        candidates = self._get_candidates_fts5(query, provider, top_k)
        return candidates, [0.0] * len(candidates)

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
            # Mark as discontinued if last observation is before 2020
            discontinued = False
            if end_date:
                try:
                    # end_date may be "2025-11-01" or "2021-01-01T05:00:00Z"
                    year = int(end_date[:4])
                    if year < 2020:
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
                "use ASK to let the user choose from the top 3-5 most relevant options."
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

                content_upper = content.upper()

                # Parse PICK response
                if "PICK" in content_upper:
                    digits = "".join(c for c in content_upper.split("PICK")[-1] if c.isdigit())
                    if digits:
                        num = int(digits[:3]) - 1
                        if 0 <= num < len(candidates):
                            code, name = candidates[num]
                            code = self._normalize_code(code, provider)
                            logger.info("🎯 LLM picked: '%s' → %s (%s)", query[:40], code, name[:40])
                            return SelectionResult(code=code, name=name, source="llm_pick")

                # Parse ASK response
                if "ASK" in content_upper:
                    nums_str = content_upper.split("ASK")[-1]
                    options_list = []
                    for part in nums_str.replace(",", " ").split():
                        digits = "".join(c for c in part if c.isdigit())
                        if digits:
                            idx = int(digits[:3]) - 1
                            if 0 <= idx < len(candidates):
                                code, name = candidates[idx]
                                code = self._normalize_code(code, provider)
                                if not any(o["code"] == code for o in options_list):
                                    options_list.append({"code": code, "name": name})
                    if options_list:
                        logger.info("🔵 LLM asks user: '%s' → %d options", query[:40], len(options_list))
                        return SelectionResult(code=None, source="user_choice", options=options_list[:10])

                # Fallback: extract any number
                digits = "".join(c for c in content if c.isdigit())
                if digits:
                    num = int(digits[:3]) - 1
                    if 0 <= num < len(candidates):
                        code, name = candidates[num]
                        code = self._normalize_code(code, provider)
                        return SelectionResult(code=code, name=name, source="llm_pick")

        except Exception as e:
            logger.warning("LLM selection failed: %s", e)

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
    ):
        self.code = code
        self.name = name
        self.source = source
        self.options = options

    @property
    def needs_user_choice(self) -> bool:
        return self.source == "user_choice" and bool(self.options)

    def __repr__(self) -> str:
        if self.needs_user_choice:
            return f"SelectionResult(user_choice, {len(self.options)} options)"
        return f"SelectionResult(code={self.code}, source={self.source})"
