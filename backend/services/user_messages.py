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

Scope is deliberately narrow: the response strings that Proposals B and C
localize, plus the CHROME of indicator-clarification menus (headers, "please
choose" prompts, reply hints, retry notices, "current match" labels — the
``clarify_*`` ids, rendered by ``indicator_clarification._chrome``). Option
labels that are provider titles, executed option values, rewritten queries and
series ids are NEVER localized here. Do not grow this into a general i18n layer.

System-written option labels (group scope, decomposition) ARE localized, so
they are presentation only: reply resolution keys on the pending payload's
structural ``option_roles`` and the executed option value, never on a label's
wording (query_helpers.build_intent_from_semantic_clarification).

Reply-hint convention: every hint shown NEXT TO numbered option buttons starts
with "Reply with the option number" (en) or "请回复选项编号" (zh); the web UI
strips exactly those prefixes when it renders the options as buttons
(ChatPage.tsx ``sanitizeClarificationContent``). Keep new languages' hints on
one fixed prefix and add it there.
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
    # (3b) Observed series coverage does not establish a publication schedule.
    # {scope} = indicator/country fragment, {latest} = observed date.
    "no_observations_in_window": {
        "en": (
            "⚠️ **No observations in the requested period**\n\n"
            "The latest available observation for {scope} is **{latest}**. "
            "No observations were found for this series in the requested period. "
            "You can ask for a different period or the latest available data."
        ),
        "zh": (
            "⚠️ **所请求时段内没有观测数据**\n\n"
            "{scope} 目前最新的数据点为 **{latest}**。"
            "未找到该序列在所请求时段内的观测数据。"
            "您可以查询其他时段或最新可用数据。"
        ),
    },
    # (3c) Same, scoped to the ONE served series ({series} = "CODE (title)"),
    # never to the user's concept: other series for the concept may well cover
    # the period. Dates are what was RETRIEVED, not a publication schedule.
    # {direction} = optional rendered window_period_* sentence (may be "").
    "no_observations_in_window_series_range": {
        "en": (
            "⚠️ **No observations in the requested period**\n\n"
            "The retrieved observations for series {series} run from "
            "**{earliest}** to **{latest}**.{direction} "
            "No observations were found for this series in the requested period. "
            "Other series for this indicator may cover it — you can ask for a "
            "different period, the latest available data, or another series."
        ),
        "zh": (
            "⚠️ **所请求时段内没有观测数据**\n\n"
            "检索到的序列 {series} 的观测数据范围为 **{earliest}** 至 **{latest}**。"
            "{direction}未找到该序列在所请求时段内的观测数据。"
            "该指标的其他序列可能覆盖此时段——您可以查询其他时段、最新可用数据或其他序列。"
        ),
    },
    "no_observations_in_window_series_latest": {
        "en": (
            "⚠️ **No observations in the requested period**\n\n"
            "The latest retrieved observation for series {series} is "
            "**{latest}**.{direction} "
            "No observations were found for this series in the requested period. "
            "Other series for this indicator may cover it — you can ask for a "
            "different period, the latest available data, or another series."
        ),
        "zh": (
            "⚠️ **所请求时段内没有观测数据**\n\n"
            "检索到的序列 {series} 最新的数据点为 **{latest}**。"
            "{direction}未找到该序列在所请求时段内的观测数据。"
            "该指标的其他序列可能覆盖此时段——您可以查询其他时段、最新可用数据或其他序列。"
        ),
    },
    "window_period_before_earliest": {
        "en": " The requested period is earlier than {earliest}.",
        "zh": "所请求时段早于 {earliest}。",
    },
    "window_period_after_latest": {
        "en": " The requested period is later than {latest}.",
        "zh": "所请求时段晚于 {latest}。",
    },
    # (2b) Subnational fail-closed on NO-DATA paths (window miss / menu where
    # every fetched candidate failed the region check). Weaker than
    # subnational_national_only on purpose: nothing was served, and no
    # re-adjudication ran, so it never claims the region is "not published".
    "subnational_series_not_found": {
        "en": (
            "⚠️ **No {region}-level series found**\n\n"
            "No **{region}**-level series for this indicator was found at "
            "**{provider}**. The closest match, {national}, does not refer to "
            "{region}."
        ),
        "zh": (
            "⚠️ **未找到 {region} 级别的序列**\n\n"
            "在 **{provider}** 未找到该指标的 **{region}** 级别序列。"
            "最接近的匹配 {national} 并非 {region} 的数据。"
        ),
    },
    "subnational_options_not_found": {
        "en": (
            "⚠️ **No {region}-level series found**\n\n"
            "None of the candidate series for this indicator at **{provider}** "
            "refer to **{region}**, so no {region}-level data can be shown."
        ),
        "zh": (
            "⚠️ **未找到 {region} 级别的序列**\n\n"
            "{provider} 上该指标的候选序列均不涉及 **{region}**，"
            "因此无法提供 {region} 级别的数据。"
        ),
    },
    # (3c) Provider-default geography disclosure (query._disclose_default_geography).
    # Worded for "not resolved", not "not specified": the user may have named
    # a place the framework could not resolve. {scope} = served geography code.
    "geo_scope_default": {
        "en": (
            "ℹ️ **Geography not resolved from the request** — showing the "
            "source's default scope: **{scope}**. Name a country or region to "
            "change it."
        ),
        "zh": (
            "ℹ️ **未能从请求中识别地区** —— 当前显示数据源的默认范围：**{scope}**。"
            "如需其他范围，请指明国家或地区。"
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
    # ------------------------------------------------------------------
    # (5) Indicator-clarification menu chrome (indicator_clarification.py).
    # English templates are byte-identical to the literals they replaced.
    # Inserted fragments ({region}, {families}, {label}, {reason}, {provider},
    # {name}) are proper-noun / provider-title / LLM-reason text and are
    # deliberately NOT translated.
    # ------------------------------------------------------------------
    # Reply-turn notices: rendered in the language of the turn that BUILT the
    # menu (a bare "2" reply carries no language signal of its own).
    "clarify_invalid_option_number": {
        "en": "Please choose one of the listed option numbers.",
        "zh": "请从列出的选项编号中选择一个。",
    },
    "clarify_option_failed": {
        "en": "That option did not return usable data. Please choose a different option.",
        "zh": "该选项未返回可用数据，请选择其他选项。",
    },
    # No reliable match.
    "clarify_no_reliable_match": {
        "en": (
            "I could not find a reliable indicator and provider combination for "
            "this request without guessing."
        ),
        "zh": "在不进行猜测的前提下，我无法为此请求找到可靠的指标与数据源组合。",
    },
    "clarify_no_reliable_large_comparison": {
        "en": (
            "Large cross-country comparisons are the hardest case here, and the "
            "current metric is not reliably available across the full scope."
        ),
        "zh": "大范围的跨国比较是最难处理的情况，当前指标无法在整个范围内可靠获取。",
    },
    "clarify_no_reliable_large_comparison_hint": {
        "en": (
            "Try a smaller country set, ask for one overall group value, or "
            "choose a different metric wording."
        ),
        "zh": "请尝试缩小国家范围、改为查询区域整体数值，或换一种指标表述。",
    },
    "clarify_no_reliable_rephrase": {
        "en": (
            "Please rephrase with a more specific metric, narrower geography, or "
            "different provider scope."
        ),
        "zh": "请换一种表述：使用更具体的指标、更小的地理范围，或指定其他数据源。",
    },
    "clarify_no_reliable_message": {
        "en": "I stopped before fetching because the available matches were not reliable enough.",
        "zh": "现有匹配结果不够可靠，因此我在获取数据前停止了。",
    },
    # Mixed indicator families.
    "clarify_multi_family_header": {
        "en": (
            "Your query mixes multiple indicator families, which can lead to "
            "partial or incorrect results in one fetch."
        ),
        "zh": "您的查询混合了多个指标类别，一次获取可能导致结果不完整或不正确。",
    },
    "clarify_multi_family_detected": {
        "en": "I detected: {families}.",
        "zh": "检测到的类别：{families}。",
    },
    "clarify_multi_family_choose": {
        "en": "Please choose the first indicator focus to fetch accurately:",
        "zh": "请选择要优先准确获取的指标：",
    },
    "clarify_multi_family_reply_hint": {
        "en": "Reply with the option number (for example, 1) or the exact indicator you want first.",
        "zh": "请回复选项编号（例如 1），或输入您想优先获取的具体指标。",
    },
    "clarify_multi_family_no_options_hint": {
        "en": (
            "Reply with the exact indicator focus first (for example: unemployment "
            "rate, CPI inflation, or government debt-to-GDP)."
        ),
        "zh": "请先回复您要查询的具体指标（例如：失业率、CPI 通胀或政府债务占 GDP 比重）。",
    },
    # Group (region) scope. Labels here are system text, not provider titles;
    # the executed option VALUES are rewritten queries and are never localized.
    "clarify_group_scope_ambiguous": {
        "en": "Your query mentions {region}, but the scope is still ambiguous.",
        "zh": "您的查询提到了 {region}，但范围仍不明确。",
    },
    "clarify_group_scope_question": {
        "en": (
            "Do you want to compare {region} member countries, or ask for one "
            "overall value for the group?"
        ),
        "zh": "您是想比较 {region} 的各成员国，还是查询该区域整体的单一数值？",
    },
    "clarify_group_scope_option_compare": {
        "en": "compare member countries",
        "zh": "比较各成员国",
    },
    "clarify_group_scope_option_group_value": {
        "en": "one overall group value (aggregate/average if available)",
        "zh": "区域整体的单一数值（如有，则为汇总值/平均值）",
    },
    "clarify_group_scope_reply_hint": {
        "en": "Reply with the option number, or type a different scope.",
        "zh": "请回复选项编号，或输入其他范围。",
    },
    # Decomposition without named members. {category_label}/{category_plural}
    # are themselves rendered from the clarify_decomp_noun_* ids.
    "clarify_decomp_noun_category": {"en": "category", "zh": "类别"},
    "clarify_decomp_noun_categories": {"en": "categories", "zh": "类别"},
    "clarify_decomp_noun_member": {"en": "breakdown member", "zh": "细分项"},
    "clarify_decomp_noun_members": {"en": "breakdown members", "zh": "细分项"},
    "clarify_decomp_missing": {
        "en": (
            "Your query asks for a {category_label} breakdown, but no specific "
            "{category_plural} were named."
        ),
        "zh": "您的查询要求按{category_label}细分，但未指明具体的{category_plural}。",
    },
    "clarify_decomp_question": {
        "en": "Which {category_plural} should I use?",
        "zh": "应使用哪些{category_plural}？",
    },
    "clarify_decomp_option_specify": {
        "en": "specify {category_plural}",
        "zh": "指定{category_plural}",
    },
    "clarify_decomp_option_overall": {
        "en": "use the main overall series",
        "zh": "使用总体主序列",
    },
    "clarify_decomp_reply_hint": {
        "en": "Reply with the option number, or type the specific {category_plural} you want.",
        "zh": "请回复选项编号，或输入您想要的具体{category_plural}。",
    },
    # Geographic breakdown without a named geography.
    "clarify_geo_breakdown_missing": {
        "en": (
            "Your query asks for a geographic breakdown, but it does not name the "
            "geography to break down."
        ),
        "zh": "您的查询要求按地理区域细分，但未指明要细分的国家或地区。",
    },
    "clarify_geo_breakdown_subnational": {
        "en": (
            "If you want a subnational breakdown, name the country first "
            "(for example: 'Canada employment by province')."
        ),
        "zh": "如需国家以下级别的细分，请先指明国家（例如：“加拿大各省就业”）。",
    },
    "clarify_geo_breakdown_group": {
        "en": (
            "If you want a multi-country group, name the region explicitly "
            "(for example: 'employment in South Asia')."
        ),
        "zh": "如需多国区域数据，请明确指明区域（例如：“南亚就业”）。",
    },
    # "Current match" labels. clarify_current_unresolved is shown when no
    # concrete series has been resolved yet; the zh copy says so plainly
    # instead of surfacing internal routing jargon.
    "clarify_current_unresolved": {
        "en": "{provider} routing guess",
        "zh": "尚未确定（已初步路由至 {provider}）",
    },
    "clarify_current_from": {
        "en": "{name} from {provider}",
        "zh": "{name}（来源：{provider}）",
    },
    "clarify_unknown_provider": {"en": "Unknown provider", "zh": "未知数据源"},
    "clarify_unknown_source": {"en": "unknown source", "zh": "未知来源"},
    "clarify_unknown_indicator": {"en": "Unknown indicator", "zh": "未知指标"},
    "clarify_current_routed_match": {
        "en": "Current routed match: {label}",
        "zh": "当前路由匹配：{label}",
    },
    "clarify_current_match": {
        "en": "Current match: {label}",
        "zh": "当前匹配：{label}",
    },
    # Indicator-choice menus (prefetch / post-fetch uncertainty / no-data).
    "clarify_prefetch_header": {
        "en": "I found multiple plausible indicator matches before fetching data.",
        "zh": "获取数据前，我找到了多个可能匹配的指标。",
    },
    "clarify_uncertain_header": {
        "en": "I found multiple plausible indicators and the current match is uncertain.",
        "zh": "我找到了多个可能的指标，当前匹配并不确定。",
    },
    "clarify_no_data_header": {
        "en": "I couldn't find data with the current indicator selection.",
        "zh": "按当前选择的指标未找到数据。",
    },
    "clarify_severe_mismatch_header": {
        "en": "The current indicator match may be wrong for your request.",
        "zh": "当前匹配的指标可能不符合您的请求。",
    },
    "clarify_severe_mismatch_reply": {
        "en": (
            "Please reply with the exact indicator name or code you want "
            "(for example, NE.IMP.GNFS.ZS)."
        ),
        "zh": "请回复您想要的具体指标名称或代码（例如 NE.IMP.GNFS.ZS）。",
    },
    "clarify_choose_intended": {
        "en": "Please choose the indicator you intended:",
        "zh": "请选择您想要的指标：",
    },
    "clarify_choose_one": {
        "en": "Please choose one option:",
        "zh": "请选择一个选项：",
    },
    "clarify_reply_hint": {
        "en": "Reply with the option number (for example, 1) or the exact indicator text you want.",
        "zh": "请回复选项编号（例如 1），或输入您想要的具体指标名称。",
    },
    # Low-confidence parse. {reason} is the parse LLM's own explanation.
    "clarify_low_confidence_reason_default": {
        "en": "I could not confidently determine the requested data.",
        "zh": "我无法确定您所请求的数据。",
    },
    "clarify_low_confidence_uncertain": {
        "en": "I'm not certain about this query: {reason}",
        "zh": "我不确定如何理解此查询：{reason}",
    },
    "clarify_low_confidence_rephrase": {
        "en": "Could you rephrase with more specific details?",
        "zh": "能否换一种更具体的表述？",
    },
    "clarify_low_confidence_promode": {
        "en": "Or would you like to use Pro Mode for a custom analysis?",
        "zh": "或者，您想使用 Pro Mode 进行自定义分析吗？",
    },
    "clarify_low_confidence_message": {
        "en": (
            "**Uncertain Query**\n{reason}\n\n"
            "Please provide more details or use Pro Mode for better results."
        ),
        "zh": "**查询不明确**\n{reason}\n\n请提供更多细节，或使用 Pro Mode 以获得更好的结果。",
    },
    # (6) Explicit series-code honesty (data_fetcher._attach_requested_series_
    # disclosure): the user typed a provider code that is not what is served.
    "requested_series_not_found": {
        "en": (
            "The series code you named, {requested}, was not found at the provider; "
            "showing the closest available series, {served}, instead."
        ),
        "zh": "未找到您指定的序列代码 {requested}；以下显示最接近的可用序列 {served}。",
    },
    "requested_series_substituted": {
        "en": (
            "You asked for series {requested}, but it could not be served for this "
            "request; showing {served} instead."
        ),
        "zh": "您指定的序列 {requested} 无法用于本次请求；以下显示的是 {served}。",
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
