"""
Indicator Selector Service — Two-stage resolution for 330K indicators.

Stage 1: Catalog → concept family (which indicator group?)
Stage 2: LLM assesses clarity:
  - Clear request → auto-select the best indicator
  - Ambiguous request → LLM narrows to ≤10 options → ask user to choose
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

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
Variants like "youth unemployment", "female unemployment", "unemployment by education"
are SUBSETS — they are NOT what the user meant unless they explicitly asked for them.

Only consider the query AMBIGUOUS when there are genuinely DIFFERENT MEASURES of the
same concept that the user might want. For example:
- "health spending" is ambiguous: % of GDP vs per capita vs absolute dollars are
  different measures, and the user hasn't specified which one.
- "GDP" is ambiguous: current US$ vs constant dollars vs PPP are different measures.
- "unemployment" is NOT ambiguous: it means the total rate. Male/female/youth are subsets.

DECISION:
- If the query clearly maps to one indicator (or one indicator is the obvious default):
  Reply with "PICK: <number>"
- If there are 2-10 genuinely different MEASURES that the user might want:
  Reply with "ASK: <number>,<number>,..." listing only those different measures.
  Do NOT include demographic subsets (male/female/youth) unless the user asked for them.

Selection rules when picking:
- Prefer modeled ILO estimates (SL.*) over Jobs Indicators (JI.*)
- Prefer % of GDP over absolute values for cross-country comparison
- Prefer gross enrollment over net unless user specifies "net"
- Prefer "total" gender/age when user doesn't specify male/female/youth
- Prefer the most general/standard version of an indicator"""


class IndicatorSelector:
    """Two-stage indicator resolution: catalog family → LLM variant selection."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or _get_settings()

    async def select(
        self,
        query: str,
        concept: str,
        provider: str,
        country: Optional[str] = None,
    ) -> SelectionResult:
        """
        Select the best indicator for a query within a concept family.

        Returns SelectionResult with either:
        - A single selected code (clear request)
        - A list of ≤10 options for user to choose (ambiguous request)
        """
        from .catalog_service import find_concept_by_term, get_indicator_code
        from .indicator_database import IndicatorDatabase

        # Stage 1: Get base indicator from catalog
        concept_name = find_concept_by_term(concept) or find_concept_by_term(query)
        if not concept_name:
            return SelectionResult(code=None, source="no_concept")

        base_code = get_indicator_code(concept_name, provider)
        if not base_code or base_code in ("DYNAMIC", "N/A", "NONE"):
            return SelectionResult(code=None, source="no_provider_code")

        # Get family variants
        db = IndicatorDatabase()
        variants = self._get_family_variants(db, base_code, query, provider)

        if not variants:
            return SelectionResult(code=base_code, source="catalog_base")

        if len(variants) == 1:
            return SelectionResult(
                code=variants[0][0], name=variants[0][1], source="single_variant",
            )

        # Narrow large families before sending to LLM
        if len(variants) > 30:
            variants = self._narrow_variants(variants, query)

        # Stage 2: LLM decides — clear (auto-pick) or ambiguous (ask user)
        result = await self._llm_assess(query, variants, provider, base_code)
        return result

    def _get_family_variants(
        self, db: Any, base_code: str, query: str, provider: str,
    ) -> List[Tuple[str, str]]:
        """Get indicator variants in the same concept family."""
        conn = db._get_connection()
        cur = conn.cursor()

        cur.execute(
            "SELECT name FROM indicators WHERE provider=? AND code=?",
            (provider, base_code),
        )
        row = cur.fetchone()
        if not row:
            return []

        core = row[0].split(",")[0].split("(")[0].strip().lower()
        if len(core) < 4:
            core = row[0].split(",")[0].strip().lower()

        cur.execute(
            "SELECT code, name FROM indicators "
            "WHERE provider=? AND LOWER(name) LIKE ? "
            "ORDER BY LENGTH(name) LIMIT 60",
            (provider, f"%{core}%"),
        )
        return cur.fetchall()

    def _narrow_variants(
        self, variants: List[Tuple[str, str]], query: str,
    ) -> List[Tuple[str, str]]:
        """Narrow a large variant list using query keyword relevance."""
        query_words = set(query.lower().split())
        stopwords = {"the", "a", "an", "in", "of", "for", "and", "or", "to", "as", "by", "at"}
        query_words -= stopwords

        scored = []
        for code, name in variants:
            name_lower = name.lower()
            score = sum(1 for w in query_words if w in name_lower)
            score += 0.5 / max(len(name), 1) * 100
            scored.append((score, code, name))

        scored.sort(reverse=True)
        return [(code, name) for _, code, name in scored[:30]]

    async def _llm_assess(
        self,
        query: str,
        variants: List[Tuple[str, str]],
        provider: str,
        base_code: str,
    ) -> SelectionResult:
        """LLM decides: auto-pick (clear) or return options (ambiguous)."""
        options = "\n".join(
            f"{i + 1}. [{code}] {name}"
            for i, (code, name) in enumerate(variants)
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
                    # Reasoning model returned empty — fall back to catalog
                    return SelectionResult(code=base_code, source="catalog_fallback")

                content_upper = content.upper()

                # Parse PICK response
                if "PICK" in content_upper:
                    digits = "".join(
                        c for c in content_upper.split("PICK")[-1] if c.isdigit()
                    )
                    if digits:
                        num = int(digits[:3]) - 1
                        if 0 <= num < len(variants):
                            code, name = variants[num]
                            logger.info(
                                "🎯 LLM auto-selected: '%s' → %s (%s)",
                                query, code, name[:50],
                            )
                            return SelectionResult(
                                code=code, name=name, source="llm_selection",
                            )

                # Parse ASK response
                if "ASK" in content_upper:
                    nums_str = content_upper.split("ASK")[-1]
                    nums = []
                    for part in nums_str.replace(",", " ").split():
                        digits = "".join(c for c in part if c.isdigit())
                        if digits:
                            idx = int(digits[:3]) - 1
                            if 0 <= idx < len(variants) and idx not in [n for n, _, _ in nums]:
                                code, name = variants[idx]
                                nums.append((idx, code, name))
                    if nums:
                        options_list = [
                            {"code": code, "name": name}
                            for _, code, name in nums[:10]
                        ]
                        logger.info(
                            "🔵 LLM requests user choice: '%s' → %d options",
                            query, len(options_list),
                        )
                        return SelectionResult(
                            code=None, source="user_choice",
                            options=options_list,
                        )

                # Fallback: try to extract any number
                digits = "".join(c for c in content if c.isdigit())
                if digits:
                    num = int(digits[:3]) - 1
                    if 0 <= num < len(variants):
                        code, name = variants[num]
                        return SelectionResult(
                            code=code, name=name, source="llm_selection",
                        )

        except Exception as e:
            logger.warning("LLM indicator selection failed: %s", e)

        return SelectionResult(code=base_code, source="catalog_fallback")


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
