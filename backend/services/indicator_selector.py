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
from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings

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

Selection rules:
- Prefer modeled ILO estimates (SL.*) over Jobs Indicators (JI.*)
- Prefer % of GDP over absolute values for cross-country comparison
- Prefer gross enrollment over net unless user specifies "net"
- Prefer "total" gender/age when user doesn't specify male/female/youth
- Prefer the most general/standard version of an indicator
- When in doubt, PICK — the user can always ask for a different variant"""


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
        # Step 1: Find candidates via embedding similarity
        candidates = self._get_candidates(query, provider)

        if not candidates:
            return SelectionResult(code=None, source="no_candidates")

        if len(candidates) == 1:
            code = self._normalize_code(candidates[0][0], provider)
            return SelectionResult(code=code, name=candidates[0][1], source="single_match")

        # Step 2: LLM picks from candidates
        result = await self._llm_pick(query, candidates, provider)

        # Step 3: If LLM couldn't decide, try with fewer/different candidates
        if not result or (not result.code and not result.needs_user_choice):
            # Retry with top 5 only (simpler for LLM)
            result = await self._llm_pick(query, candidates[:5], provider)

        return result or SelectionResult(code=candidates[0][0], name=candidates[0][1], source="top_candidate")

    def _get_candidates(
        self, query: str, provider: str, top_k: int = 20,
    ) -> List[tuple[str, str]]:
        """Step 1: Find nearest indicators using OpenAI embeddings."""
        try:
            from .embedding_retrieval import get_embedding_retrieval
            er = get_embedding_retrieval()
            results = er.search(query, provider=provider, top_k=top_k)
            if results:
                return [(r["code"], r["name"]) for r in results]
        except Exception as e:
            logger.warning("Embedding retrieval failed: %s", e)

        # Fallback: FTS5 OR search if embeddings unavailable
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

    async def _llm_pick(
        self,
        query: str,
        candidates: List[tuple[str, str]],
        provider: str,
    ) -> Optional[SelectionResult]:
        """Step 2: LLM picks the best indicator from candidates."""
        options = "\n".join(
            f"{i + 1}. [{code}] {name}"
            for i, (code, name) in enumerate(candidates)
        )

        prompt = LLM_SELECTION_PROMPT.format(
            query=query, provider=provider, options=options,
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
