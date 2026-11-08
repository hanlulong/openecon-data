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


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def structured_indicator_label(
    intent: Any, params: Optional[Mapping[str, Any]] = None,
) -> str:
    """Best semantic indicator label available on the intent/params.

    1. ``__semantic_indicator_label`` is semantic BY CONSTRUCTION (merged
       conversation state / LLM indicator text): an acronym concept such as
       "GDP"/"CPI"/"PMI" is kept as-is. It is treated as a series id only
       when there is structural evidence that it IS one: the indicator DB
       knows that exact token as a code for this provider, or the label is
       the very token carried in ``params['indicator']`` (state persisted the
       typed code as both label and code). A series-id label is replaced by
       a non-code concept phrase among the parsed indicators when there is
       one, else by the catalog NAME (a carried "UNRATE"/"BAMLH0A0HYM2"
       becomes its title); on a follow-up a series id with no catalog name
       yields "" (never "Japan UNRATE").
    2. Otherwise the indicator token, whose code shape is checked: a bare
       provider code becomes its catalog name. On a follow-up an unknown bare
       code yields "" (never "Japan UNRATE"), and the display label — which
       is the raw query text by construction — is skipped, as is any
       candidate equal to the raw delta utterance.
    """
    params = params or (getattr(intent, "parameters", None) or {})
    indicators = list(getattr(intent, "indicators", None) or []) if intent is not None else []
    provider = str(getattr(intent, "apiProvider", "") or "") if intent is not None else ""
    follow_up = is_follow_up_intent(intent, params)
    raw_texts = set()
    if follow_up:
        for raw in (getattr(intent, "originalQuery", "") if intent is not None else "", params.get("__original_query")):
            cleaned = _clean(raw).lower()
            if cleaned:
                raw_texts.add(cleaned)

    semantic = _clean(params.get("__semantic_indicator_label"))
    if semantic and semantic.lower() not in raw_texts:
        if not _looks_like_provider_code(semantic):
            return semantic
        semantic_title = _catalog_name_for_code(provider, semantic)
        # Token identity, not shape: a label that IS the carried code token
        # is a series id even when the catalog cannot name it (an uncatalogued
        # typed code, or the DB briefly unavailable); a label that differs
        # from the carried code ("CPI" over CPIAUCSL) is the user's concept.
        label_is_carried_code = semantic.lower() == _clean(params.get("indicator")).lower()
        if not semantic_title and not label_is_carried_code:
            return semantic
        # The label is a real provider code (state persisted from a turn where
        # the user typed the id). A concept phrase the parser gave for this
        # turn describes the user's concept better than that series' title
        # (which can be geography-specific); the title is the fallback.
        for candidate in [*indicators, params.get("indicator")]:
            text = _clean(candidate)
            if text and text.lower() not in raw_texts and not _looks_like_provider_code(text):
                return text
        if semantic_title:
            return semantic_title
        return "" if follow_up else semantic

    candidates = [] if follow_up else [params.get("__display_indicator_label")]
    candidates += [indicators[0] if indicators else "", params.get("indicator")]
    code_fallback = ""
    for candidate in candidates:
        text = _clean(candidate)
        if not text or text.lower() in raw_texts:
            continue
        if not _looks_like_provider_code(text):
            return text
        if not code_fallback:
            code_fallback = text
    if code_fallback:
        return _catalog_name_for_code(provider, code_fallback) or ("" if follow_up else code_fallback)
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
