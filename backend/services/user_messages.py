"""Tiny localized message catalog for user-facing response strings.

Design goals
------------
- ONE place for the handful of user-facing strings that must render in the
  language the user actually wrote in (detected semantically by the parse
  LLM and carried on ``ParsedIntent.language`` — never by regex/charset
  rules here).
- English is the source of truth and the ALWAYS-available fallback. A missing
  language, a missing message id, or a template that references a placeholder
  the caller did not supply must NEVER raise — a user-facing path can never be
  allowed to explode over a translation lookup.
- Named-placeholder templates only (``{country}``, ``{region}`` …) so callers
  pass keyword arguments and the ordering is self-documenting.

Scope is deliberately narrow: only the response strings that Proposals B and C
localize live here. Do not grow this into a general i18n layer.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# message_id -> language (ISO 639-1) -> template string.
#
# Every template keeps the existing "⚠️ **…**" markdown convention so the
# frontend renders these identically to the inline strings they replaced.
# Chinese copy is native Simplified Chinese, not a machine gloss.
_MESSAGES: Dict[str, Dict[str, str]] = {
    # (1) Empty-data finalization (query._finalize_empty_data_response).
    # {scope} is a pre-assembled, mostly-proper-noun fragment (indicator /
    # country / provider); the surrounding sentence is what gets localized.
    "no_data_finalization": {
        "en": (
            "⚠️ **No Data Available**\n\n"
            "No data found for {scope}. The provider may not publish this "
            "series at this scope — try broadening the time range or "
            "rephrasing the indicator."
        ),
        "zh": (
            "⚠️ **无可用数据**\n\n"
            "未找到 {scope} 的相关数据。该数据源可能未在此范围内发布该序列——"
            "请尝试扩大时间范围，或换一种方式描述该指标。"
        ),
    },
    # (2) Proposal B — subnational fail-closed: national data was served but the
    # user asked about a sub-country region the provider does not decompose to.
    "subnational_national_only": {
        "en": (
            "⚠️ **{region}-level data not available**\n\n"
            "Only national-level data is available for **{country}** from "
            "**{provider}**; **{region}**-level data is not published there."
        ),
        "zh": (
            "⚠️ **无 {region} 级别的数据**\n\n"
            "{provider} 仅提供 **{country}** 的全国级数据，"
            "未发布 **{region}** 级别的数据。"
        ),
    },
    # (3) Geography coverage warning (geography_validation).
    "country_coverage_partial_with_available": {
        "en": (
            "Data is only available for a subset of requested countries. "
            "Missing: {missing}. Available: {available}."
        ),
        "zh": (
            "仅能获取部分所请求国家/地区的数据。缺失：{missing}。可用：{available}。"
        ),
    },
    "country_coverage_partial_missing_only": {
        "en": (
            "Data is only available for a subset of requested countries. "
            "Missing: {missing}."
        ),
        "zh": (
            "仅能获取部分所请求国家/地区的数据。缺失：{missing}。"
        ),
    },
    # (3b) Time-window honesty: the series EXISTS but every observation falls
    # before the user's requested window (not-yet-published periods, frozen
    # series). Distinct from generic no-data: we can tell the user exactly how
    # far the data goes. {scope} = indicator/country fragment, {latest} = date.
    "not_yet_released": {
        "en": (
            "⚠️ **Requested period not yet available**\n\n"
            "The latest available observation for {scope} is **{latest}**. "
            "Data for the requested period has not been published yet — try "
            "again after the next release, or ask for the latest available data."
        ),
        "zh": (
            "⚠️ **所请求时段的数据尚未发布**\n\n"
            "{scope} 目前最新的数据点为 **{latest}**。"
            "所请求时段的数据尚未发布——请在下次发布后再试，或查询最新可用数据。"
        ),
    },
    # (4) Multi-indicator partial-failure note (data_fetcher.fetch_multi_indicator_data).
    "multi_indicator_partial": {
        "en": (
            "Could not fetch data for: {missing}. The results shown cover only "
            "the indicators that resolved successfully; the missing series may "
            "be unavailable from the selected provider(s) at this scope."
        ),
        "zh": (
            "无法获取以下指标的数据：{missing}。所显示的结果仅涵盖成功解析的指标；"
            "缺失的序列在所选数据源的此范围内可能不可用。"
        ),
    },
}

_DEFAULT_LANG = "en"


def _normalize_language(language: Optional[str]) -> str:
    """Reduce an ISO-639-1(ish) tag to a bare catalog key (e.g. ``zh-CN`` -> ``zh``).

    Defaults to English for None / empty / unrecognizable input. Purely
    structural string handling — this is not language *detection* (that is the
    LLM's job on the parse path); it only canonicalizes an already-detected tag.
    """
    if not language:
        return _DEFAULT_LANG
    lang = str(language).strip().lower()
    if not lang:
        return _DEFAULT_LANG
    # "zh-cn" / "zh_hans" / "en-us" -> primary subtag
    lang = lang.replace("_", "-").split("-", 1)[0]
    return lang or _DEFAULT_LANG


def get_message(message_id: str, language: Optional[str] = None, **kwargs) -> str:
    """Render a localized user-facing message.

    Falls back to English whenever the requested language is missing, and to a
    best-effort raw template whenever placeholder substitution fails. NEVER
    raises: callers use these strings on live response paths where a lookup
    error must not surface to the user.

    Args:
        message_id: catalog key (see ``_MESSAGES``).
        language: ISO 639-1 tag from ``ParsedIntent.language`` (or None -> en).
        **kwargs: named placeholder values referenced by the template.

    Returns:
        The rendered string, or "" if ``message_id`` is unknown (so a caller can
        detect the miss and keep its own inline default).
    """
    entry = _MESSAGES.get(message_id)
    if not entry:
        logger.debug("user_messages: unknown message_id %r", message_id)
        return ""
    lang = _normalize_language(language)
    template = entry.get(lang) or entry.get(_DEFAULT_LANG)
    if template is None:
        # Message exists but has neither the requested language nor English;
        # take whatever translation is present rather than raising.
        template = next(iter(entry.values()), "")
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError) as exc:
        logger.debug(
            "user_messages: placeholder mismatch for %r/%s (%s); falling back to English",
            message_id, lang, exc,
        )
        english = entry.get(_DEFAULT_LANG, template)
        try:
            return english.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            # Give back the unsubstituted English template rather than crash.
            return english
