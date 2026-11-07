"""Retrieval text for follow-up (delta) intents — built from structured state.

On a follow-up, ``intent.originalQuery`` / ``params['__original_query']`` is
the RAW delta utterance ("和日本比较", "compare with Japan", "再加上英国"): it
names the change, not the concept. Searching a provider catalog or building a
clarification menu from it yields nonsense candidates. The concept is already
held structurally — the merged semantic indicator label (conversation state)
and the requested country — so retrieval on follow-ups is rebuilt from those.

Structural only: no text is interpreted. The one shape check (is a label a
bare provider code?) decides whether to look the code's catalog NAME up in the
indicator DB; it never maps concepts to codes. The raw query is left untouched
everywhere else (history, display, language, cache identity).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# A single token of upper-case letters/digits/punctuation (FRED "UNRATE",
# WB "NY.GDP.MKTP.CD", StatsCan "14100287", vectors "v41690973").
_CODE_SHAPE_RE = re.compile(r"v?[A-Z0-9][A-Z0-9_.\-]{1,63}")


def is_follow_up_intent(intent: Any, params: Optional[Mapping[str, Any]] = None) -> bool:
    if intent is not None and bool(getattr(intent, "isFollowUp", False)):
        return True
    return bool((params or {}).get("__delta_resolved"))


def _looks_like_provider_code(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value) and bool(_CODE_SHAPE_RE.fullmatch(value))


def _catalog_name_for_code(provider: str, code: str) -> str:
    try:
        from .indicator_database import get_indicator_lookup

        metadata = get_indicator_lookup().get(str(provider or "").upper(), code)
    except Exception:
        metadata = None
    if not metadata:
        return ""
    return str(metadata.get("name") or metadata.get("title") or "").strip()


def structured_indicator_label(
    intent: Any, params: Optional[Mapping[str, Any]] = None,
) -> str:
    """Best semantic indicator label available on the intent/params.

    Prefers the conversation-state semantic label, then the display label,
    then the indicator token; a code-shaped token is replaced by its catalog
    name when the indicator DB knows it.
    """
    params = params or (getattr(intent, "parameters", None) or {})
    indicators = list(getattr(intent, "indicators", None) or []) if intent is not None else []
    candidates = [
        params.get("__semantic_indicator_label"),
        params.get("__display_indicator_label"),
        indicators[0] if indicators else "",
        params.get("indicator"),
    ]
    provider = str(getattr(intent, "apiProvider", "") or "") if intent is not None else ""
    code_fallback = ""
    for candidate in candidates:
        text = re.sub(r"\s+", " ", str(candidate or "")).strip()
        if not text:
            continue
        if not _looks_like_provider_code(text):
            return text
        if not code_fallback:
            code_fallback = text
    if code_fallback:
        return _catalog_name_for_code(provider, code_fallback) or code_fallback
    return ""


def follow_up_retrieval_text(
    intent: Any,
    params: Optional[Mapping[str, Any]] = None,
    country_label: str = "",
) -> str:
    """Concept(+country) search text for a follow-up intent; "" when not a follow-up.

    Never returns the raw delta utterance. Callers keep their existing text
    for non-follow-up intents.
    """
    if not is_follow_up_intent(intent, params):
        return ""
    label = structured_indicator_label(intent, params)
    if not label:
        return ""
    country = str(country_label or "").strip()
    if country and country.lower() not in label.lower():
        return re.sub(r"\s+", " ", f"{country} {label}").strip()
    return label
