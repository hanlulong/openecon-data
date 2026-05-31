"""Indicator resolution, exact passthrough, and distilled query building.

Extracted from query.py to reduce file size and isolate indicator resolution
logic into a testable, reusable module.

This module provides:
- Candidate-only code hints for provider-native indicator codes
- Fail-closed guardrails for obviously unsupported/implausible results
- Resolution threshold computation (dynamic acceptance levels)
- Disabled provider override shims kept only for compatibility
- Full indicator resolution pipeline (exact provider-native title/code -> IndicatorSelector)
- Distilled indicator query building for cross-provider resolution
- Query type detection (ranking, comparison, temporal split)
- BIS metadata label normalization
"""

from __future__ import annotations

import logging
import json
import re
from collections import Counter
from typing import Any, Dict, List, NamedTuple, Optional

from ..models import NormalizedData, ParsedIntent
from ..routing.country_resolver import CountryResolver
from ..services.relevance_scorer import (
    extract_indicator_cues,
    score_series_relevance,
    specialization_mismatch_penalty,
    tokenize_indicator_terms,
)

logger = logging.getLogger(__name__)


# Pre-compiled regex patterns used by this module
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_TOP_N_RE = re.compile(r"\btop\s+(\d{1,3})\b")


class _CountryAliasRegex(NamedTuple):
    alias: str
    code: str
    boundary: re.Pattern[str]
    leading_space: re.Pattern[str]
    for_suffix: re.Pattern[str]
    punct_prefix: re.Pattern[str]


def _build_country_alias_regex_rows() -> tuple[_CountryAliasRegex, ...]:
    """Precompile existing country-alias patterns used in exact-title helpers.

    Keep this strictly mechanical: same alias source, same length-descending
    order, same escaping, and same boundary/prefix semantics as the previous
    per-call ``re`` loops.  This removes regex compilation from hot exact-title
    probes without adding provider/indicator shortcut authority.
    """

    rows: list[_CountryAliasRegex] = []
    aliases = sorted(
        {
            str(alias).strip()
            for alias in CountryResolver.COUNTRY_ALIASES.keys()
            if str(alias).strip()
        },
        key=len,
        reverse=True,
    )
    for alias_text in aliases:
        escaped = re.escape(alias_text)
        rows.append(
            _CountryAliasRegex(
                alias=alias_text,
                code=CountryResolver.normalize(alias_text) or "",
                boundary=re.compile(
                    rf"(?<![a-z0-9]){escaped}(?![a-z0-9])",
                    flags=re.IGNORECASE,
                ),
                leading_space=re.compile(
                    rf"^(?:{escaped})\s+",
                    flags=re.IGNORECASE,
                ),
                for_suffix=re.compile(
                    rf"^(?P<head>.+?)\s+for\s+(?P<country>{escaped})$",
                    flags=re.IGNORECASE,
                ),
                punct_prefix=re.compile(
                    rf"^(?P<country>{escaped})\s*[-,:]\s*(?P<head>.+)$",
                    flags=re.IGNORECASE,
                ),
            )
        )
    return tuple(rows)


_COUNTRY_ALIAS_REGEX_ROWS = _build_country_alias_regex_rows()

# GDP ratio patterns — used by 5 functions to detect "X as % of GDP" style queries.
# Hoisted to module level to eliminate duplication (was copy-pasted 5 times).
_GDP_RATIO_PATTERNS = (
    "% of gdp",
    "as % of gdp",
    "as percent of gdp",
    "as percentage of gdp",
    "share of gdp",
    "to gdp ratio",
    "ratio to gdp",
    "as share of gdp",
)

_EXACT_TITLE_UNIT_SUFFIX_CUE_RE = re.compile(
    r"\b(?:dollars?|currency|percent(?:age)?|index|capita|parity|ppp|"
    r"constant|current|millions?|billions?|thousands?|trillions?|"
    r"chained|base|units?)\b|\b\d{4}\s*=\s*100\b",
    flags=re.IGNORECASE,
)
# Standalone "national" is intentionally not a unit cue.  Legitimate
# measurement phrases such as "national currency" still match through the
# stronger "currency" token, while title text such as "in the National
# Accounts" must remain part of the exact provider-native title.
_EXACT_TITLE_UNIT_TOKEN_STOPWORDS = {
    "and",
    "the",
    "per",
    "of",
    "at",
    "from",
    "for",
    "in",
    "to",
}

_EXACT_TITLE_FREQUENCY_ALIASES = {
    "daily": {"daily", "d"},
    "weekly": {"weekly", "week", "w"},
    "biweekly": {"biweekly", "bi-weekly"},
    "monthly": {"monthly", "month", "m"},
    "quarterly": {"quarterly", "quarter", "q"},
    "semiannual": {"semiannual", "semi-annual", "semi annual"},
    "annual": {"annual", "annually", "yearly", "year", "a"},
}

_EXACT_TITLE_MEASUREMENT_QUALIFIER_ALIASES = {
    "real": {"real", "inflation adjusted", "inflation-adjusted", "deflated", "cpi", "constant"},
    "nominal": {"nominal", "current"},
}


def _normalize_exact_title_unit_text(text: str) -> str:
    """Normalize unit text for exact-title unit compatibility."""

    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _extract_exact_title_frequency_tokens(text: str) -> set[str]:
    """Extract explicit frequency words from exact-title query wrappers."""

    found: set[str] = set()
    # Only explicit wrappers such as "(Monthly)" or "(Weekly, Ending Monday)"
    # constrain exact-title disambiguation.  Provider-native titles can contain
    # words like "Semi-Annual" as literal title text; using those embedded title
    # words as frequency filters incorrectly rejects exact provider-title rows.
    normalized_wrappers = [
        _normalize_exact_title_unit_text(wrapper)
        for wrapper in re.findall(r"\(([^()]*)\)", str(text or ""))
        if _normalize_exact_title_unit_text(wrapper)
    ]
    for canonical, aliases in _EXACT_TITLE_FREQUENCY_ALIASES.items():
        normalized_aliases = {_normalize_exact_title_unit_text(alias) for alias in aliases}
        long_aliases = {alias for alias in normalized_aliases if len(alias) > 1}
        one_letter_aliases = {alias for alias in normalized_aliases if len(alias) == 1}
        for wrapper in normalized_wrappers:
            # Avoid treating descriptive duration phrases in provider-native
            # titles, such as FRED ACS "(5-year estimate)", as requested data
            # frequency. Standalone wrappers like "(Annual)" still count.
            if canonical == "annual" and re.search(r"\b\d+\s*(?:annual|year|yearly)\b", wrapper):
                continue
            wrapper_tokens = set(wrapper.split())
            if wrapper in long_aliases or any(wrapper.startswith(f"{alias} ") for alias in long_aliases):
                found.add(canonical)
                break
            # One-letter frequency abbreviations (M/Q/A/D/W) are too ambiguous
            # outside explicit parenthetical wrappers; within a wrapper, accept
            # only the standalone letter.
            if wrapper in one_letter_aliases or wrapper_tokens & one_letter_aliases:
                found.add(canonical)
                break
    return found


def _candidate_frequency_matches(requested: set[str], candidate_frequency: str) -> bool:
    """Return True when candidate frequency is compatible with request."""

    if not requested:
        return True
    normalized = _normalize_exact_title_unit_text(candidate_frequency)
    tokens = set(normalized.split())
    for canonical in requested:
        aliases = {
            _normalize_exact_title_unit_text(alias)
            for alias in _EXACT_TITLE_FREQUENCY_ALIASES.get(canonical, {canonical})
        }
        if canonical in tokens or tokens & aliases or normalized.startswith(canonical):
            return True
    return False


def _lost_explicit_frequency_tokens(original_query: str, indicator_query: str) -> set[str]:
    """Return explicit frequency tokens present in original but lost in indicator text."""

    original_frequencies = _extract_exact_title_frequency_tokens(original_query)
    indicator_frequencies = _extract_exact_title_frequency_tokens(indicator_query)
    return original_frequencies - indicator_frequencies


def _extract_exact_title_measurement_qualifiers(text: str) -> set[str]:
    """Extract explicit price-basis qualifiers from an exact-title query.

    This intentionally stays narrower than general semantic matching.  It only
    recognizes measurement-basis words that can distinguish provider-title
    near-neighbors (for example real vs nominal price indexes).  The common noun
    phrase ``real estate`` is excluded so housing asset titles are not treated
    as CPI-deflated requests merely because they contain the word "real".
    """

    tokens = _normalize_exact_title_unit_text(text).split()
    found: set[str] = set()
    for idx, token in enumerate(tokens):
        next_token = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if token == "real" and next_token != "estate":
            found.add("real")
        elif token == "nominal":
            found.add("nominal")
    if "inflation adjusted" in _normalize_exact_title_unit_text(text):
        found.add("real")
    return found


def _candidate_measurement_qualifiers_match(requested: set[str], candidate_text: str) -> bool:
    """Return True when candidate metadata supports requested price-basis words."""

    if not requested:
        return True
    normalized = _normalize_exact_title_unit_text(candidate_text)
    for qualifier in requested:
        aliases = _EXACT_TITLE_MEASUREMENT_QUALIFIER_ALIASES.get(qualifier, {qualifier})
        if not any(_normalize_exact_title_unit_text(alias) in normalized for alias in aliases):
            return False
    return True


def _has_ratio_cue(text: str) -> bool:
    """Check if text contains a GDP ratio pattern (e.g., '% of GDP')."""
    return any(pattern in text for pattern in _GDP_RATIO_PATTERNS)


def _trailing_exact_title_unit_suffix(text: str) -> Optional[str]:
    """Return a mechanical trailing unit phrase from an exact-title query.

    This is deliberately provider-neutral and narrow: it only recognizes
    trailing ``in <unit>`` phrases that contain measurement/unit cues.  It is
    not a semantic indicator shortcut; the stripped title still has to match a
    provider catalog title through the exact-title path.
    """

    unit_suffix = re.search(
        r"\s+in\s+(?P<unit>[^,;:]+)$",
        str(text or "").strip(),
        flags=re.IGNORECASE,
    )
    if not unit_suffix:
        return None
    unit_text = unit_suffix.group("unit").strip()
    unit_text = re.sub(
        r"\s+\b(?:from|via|use)\s+[a-z][a-z0-9 ._-]*$",
        "",
        unit_text,
        flags=re.IGNORECASE,
    ).strip()
    if not unit_text or not _EXACT_TITLE_UNIT_SUFFIX_CUE_RE.search(unit_text):
        return None
    return unit_text


def _strip_trailing_exact_title_unit_suffix(text: str) -> Optional[str]:
    """Strip a mechanical trailing exact-title unit suffix when present."""

    if not _trailing_exact_title_unit_suffix(text):
        return None
    unit_suffix = re.search(
        r"\s+in\s+(?P<unit>[^,;:]+)$",
        str(text or "").strip(),
        flags=re.IGNORECASE,
    )
    if not unit_suffix:
        return None
    stripped = str(text or "")[: unit_suffix.start()].strip(" ,;:-")
    return stripped or None


def _strip_trailing_exact_title_frequency_wrapper(text: str) -> Optional[str]:
    """Strip a mechanical trailing provider frequency wrapper when present.

    Exact-title direct-cert rows sometimes append provider-native frequency
    metadata after the title, for example ``(Daily, 7-Day)``.  That wrapper is
    not part of the catalog title, but it is still useful for disambiguating
    duplicate provider titles.  Keep the behavior mechanical: strip only the
    final parenthetical when it contains a recognized frequency token that
    :func:`_extract_exact_title_frequency_tokens` already understands.  The
    caller still carries the original query into candidate-frequency filtering.
    """

    query_text = str(text or "").strip()
    if not query_text:
        return None
    wrapper = re.search(r"\s+\((?P<frequency>[^()]*)\)\s*$", query_text)
    if not wrapper:
        return None
    if not _extract_exact_title_frequency_tokens(f"({wrapper.group('frequency')})"):
        return None
    stripped = query_text[: wrapper.start()].strip(" ,;:-")
    return stripped or None


def _exact_title_unit_tokens(text: str) -> set[str]:
    """Normalize explicit unit text for catalog-unit compatibility checks."""

    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if token not in _EXACT_TITLE_UNIT_TOKEN_STOPWORDS
    }

# Indicator cues that require strict precision matching (no fuzzy fallback).
_STRICT_PRECISION_CUES = {
    "import", "export", "trade_balance", "trade_openness",
    "debt_gdp_ratio", "public_debt", "gdp_deflator", "hicp",
    "producer_price", "real_effective_exchange_rate",
    "bond_yield", "money_supply", "policy_rate", "house_prices",
}

_IMF_GENERIC_DETAIL_MARKERS = {
    "BCA_NGDPD": {
        "primary income",
        "secondary income",
        "investment income",
        "reserve assets",
        "general government",
        "compensation of employees",
        "services",
        "transport",
        "repair services",
        "construction",
        "engineering",
    },
    "REV": {
        "other revenue",
        "tax",
        "taxes",
        "social contributions",
        "property income",
        "interest",
        "capital levies",
        "cash",
        "central government",
        "general government",
        "budgetary central government",
        "fiscal year",
    },
    "EXP": {
        "budgetary central government",
        "central government",
        "fiscal year",
        "expense",
        "education",
        "lower secondary education",
    },
    "PCPIPCH": {
        "capital city",
        "special indexes",
        "communication",
        "miscellaneous goods",
        "recreation and culture",
        "households",
        "expenditure of households",
    },
}

_WORLDBANK_GENERIC_DETAIL_MARKERS = {
    "SE.PRM.ENRR": {
        "tertiary education",
        "isced 5",
        "teachers",
        "teacher",
        "literacy",
        "functional difficulty",
        "post-secondary non-tertiary",
        "school life expectancy",
        "attrition",
        "secondary education",
        "15 to 29 years",
    },
    "NE.TRD.GNFS.ZS": {
        "terms of trade",
    },
    "NE.CON.GOVT.ZS": {
        "lower secondary education",
        "education expenditure",
        "ppp",
    },
}

_OECD_GENERIC_DETAIL_MARKERS = {
    "DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD": {
        "sustainable development goal",
        "good health and well-being",
        "decent work and economic growth",
    },
}

_EUROSTAT_GENERIC_DETAIL_MARKERS = {
    "PRC_HICP_AIND": {
        "purchasing power parities",
        "price level indices",
    },
}

def _looks_like_provider_indicator_code_local(provider: str, indicator: str) -> bool:
    """Small local code-shape guard that avoids importing heavier query helpers."""
    if not indicator:
        return False
    indicator_text = str(indicator).strip()
    if not indicator_text or " " in indicator_text:
        return False
    _lower = indicator_text.lower()
    if any(
        _lower.endswith(suffix)
        for suffix in (
            "tion",
            "ment",
            "ness",
            "ity",
            "ing",
            "ism",
            "ance",
            "ence",
            "ory",
            "ies",
            "ous",
            "ble",
            "ive",
            "age",
            "ure",
            "dom",
        )
    ):
        return False
    provider_upper = _normalize_provider_name(provider)
    code_upper = indicator_text.upper()
    if provider_upper in {"WORLDBANK", "WORLD BANK"}:
        return bool(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{1,127}", indicator_text)
            and ("." in indicator_text or "_" in indicator_text or any(ch.isdigit() for ch in indicator_text))
        )
    if provider_upper == "BIS":
        return bool(
            code_upper.startswith("WS_")
            or code_upper.startswith("BIS_WS_")
            or re.fullmatch(r"BIS\.[A-Z0-9_]{3,}", code_upper)
        )
    if provider_upper == "IMF":
        return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_\.]{2,}", code_upper))
    if provider_upper == "FRED":
        return bool(re.fullmatch(r"[A-Z0-9]{3,}", code_upper))
    if provider_upper in {"EUROSTAT", "OECD"}:
        return bool(re.fullmatch(r"[A-Z0-9_@\.]{3,}", code_upper))
    if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
        return bool(re.fullmatch(r"[A-Z0-9_]{3,}", code_upper))
    return False


def _imf_catalog_entry_supports_exact_code(candidate: str, metadata: Any) -> bool:
    """Return whether an IMF catalog hit is safe as exact DataMapper code.

    IMF's local catalog contains terse natural acronyms (for example ``GDP``)
    that are not the public WEO DataMapper series users expect when they ask
    natural-language questions like "Germany GDP from IMF".  Treat catalog hits
    as exact-code authority only when the code has provider-native namespace
    syntax or belongs to executable DataMapper categories already supported by
    the IMF provider.  Otherwise the normal selector/provider metadata path
    must decide.
    """
    if not metadata:
        return False
    code_upper = str(candidate or "").strip().upper()
    if not code_upper:
        return False
    has_namespace = (
        "_" in code_upper
        or "." in code_upper
        or any(ch.isdigit() for ch in code_upper)
    )
    category = str(metadata.get("category") or "").strip().upper()
    return bool(has_namespace or category == "WEO" or category.endswith("REO"))


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _short_imf_exact_title_tokens(text: str) -> set[str]:
    normalized = _normalize_exact_title_unit_text(str(text or "").replace("%", " percent "))
    return {
        token
        for token in normalized.split()
        if len(token) > 2
        and token not in {
            "and",
            "the",
            "from",
            "with",
            "data",
            "source",
            "database",
        }
    }


def _imf_short_exact_title_with_catalog_context(
    query_text: str,
    candidate: dict[str, Any],
) -> bool:
    """Allow short IMF titles only when explicit row-native context is present.

    IMF DataMapper has a few provider-native titles that are genuine exact
    names but too short for the broad exact-title floor (for example ``DEBT``
    and ``Revenue``).  A short title alone is ambiguous, so this branch requires
    the user query to also contain public catalog unit/source context from the
    same row.  It never infers a code from synonyms or prose.
    """
    category = str(candidate.get("category") or "").strip().upper()
    if not category or category in {"INDICATOR", "DATAFLOW"}:
        return False

    name = str(candidate.get("name") or "").strip()
    name_tokens = _short_imf_exact_title_tokens(name)
    name_is_short = len(name_tokens) <= 2 or bool(re.fullmatch(r"[A-Z0-9_ -]{2,12}", name))
    if not name_is_short:
        return False

    raw_metadata = _metadata_dict(candidate.get("raw_metadata") or candidate.get("metadata"))
    source = raw_metadata.get("source") or ""
    if isinstance(source, dict):
        source = source.get("value") or ""
    source_tokens = _short_imf_exact_title_tokens(source)
    if len(source_tokens) < 2:
        return False

    unit = str(candidate.get("unit") or raw_metadata.get("unit") or "").strip()
    unit_tokens = _short_imf_exact_title_tokens(unit)
    if not unit_tokens:
        return False

    query_tokens = _short_imf_exact_title_tokens(query_text)
    if not unit_tokens <= query_tokens:
        return False
    if len(source_tokens & query_tokens) < min(2, len(source_tokens)):
        return False
    return True


# ---------------------------------------------------------------------------
# Provider name normalization (shared utility — no circular imports)
# ---------------------------------------------------------------------------

def _normalize_provider_name(provider: str) -> str:
    from ..utils.providers import normalize_provider_name
    return normalize_provider_name(provider)


def _effective_original_query(intent: ParsedIntent) -> str:
    """Return the best 'original query' text for indicator resolution.

    For follow-up queries (e.g. "show from IMF instead", "exports"),
    ``intent.originalQuery`` is the *raw follow-up text* which often
    contains no indicator information.  In these cases, use the
    ``resolvedQuery`` that the follow-up parser already synthesised
    (e.g. "GDP per capita India").

    Falls back to ``originalQuery`` for non-follow-up queries.
    """
    if intent.isFollowUp and intent.resolvedQuery:
        return str(intent.resolvedQuery).strip()
    return str(intent.originalQuery or "").strip()


def is_provider_locked(params: Optional[dict]) -> bool:
    """Return True when semantic/provider clarification has locked the provider."""
    return bool((params or {}).get("__semantic_provider_locked"))


def is_exact_match_locked(params: Optional[dict]) -> bool:
    """Return True when the current params represent an exact provider-native match."""
    params = params or {}
    return bool(
        params.get("__exact_provider_code_match")
        or params.get("__exact_indicator_title_match")
    )


def build_exact_indicator_title_intent(
    query: str,
    *,
    explicit_provider: Optional[str] = None,
    broad_concept: Optional[str] = None,
    countries: Optional[List[str]] = None,
    all_providers: Optional[List[str]] = None,
) -> Optional[ParsedIntent]:
    """Build a provider-locked ParsedIntent for an exact provider-title match."""
    provider_candidates = (
        [_normalize_provider_name(explicit_provider)] if explicit_provider else list(all_providers or [])
    )
    provider_candidates = [provider for provider in provider_candidates if provider]

    matches: list[dict[str, Any]] = []
    seen = set()
    for provider in provider_candidates:
        candidate = find_exact_provider_title_match(query, provider)
        if not candidate:
            continue
        key = (_normalize_provider_name(candidate.get("provider") or provider), str(candidate.get("code") or ""))
        if key in seen:
            continue
        seen.add(key)
        matches.append(candidate)

    if len(matches) != 1:
        return None

    candidate = matches[0]
    provider = _normalize_provider_name(candidate.get("provider") or "")
    code = str(candidate.get("code") or "").strip()
    name = str(candidate.get("name") or query).strip()
    if not provider or not code:
        return None

    params: dict[str, Any] = {
        "indicator": code,
        "__semantic_indicator_label": name,
        "__semantic_provider_locked": True,
        "__exact_indicator_title_match": True,
    }
    if provider in {"STATSCAN", "STATISTICS CANADA"}:
        params.update({
            "__statscan_product_id": code,
            "__statscan_product_authority": "exact_user_input",
            "__semantic_authority": "exact_user_input",
            "__decision_source": "exact_title",
        })
    candidate_params = candidate.get("params")
    if isinstance(candidate_params, dict):
        params.update(candidate_params)
    if provider == "COINGECKO":
        raw_coin_ids = params.get("coinIds")
        if isinstance(raw_coin_ids, list):
            coin_ids = [str(coin_id).strip().lower() for coin_id in raw_coin_ids if str(coin_id or "").strip()]
        elif isinstance(raw_coin_ids, str):
            coin_ids = [coin_id.strip().lower() for coin_id in raw_coin_ids.split(",") if coin_id.strip()]
        else:
            coin_ids = []
        params["coinIds"] = list(dict.fromkeys(coin_ids)) or [code]

    countries = _countries_outside_exact_title(
        query,
        provider,
        name,
        list(countries or []),
    )
    if len(countries) == 1:
        params["country"] = countries[0]
        if provider in {"STATSCAN", "STATISTICS CANADA"}:
            country_code = str(countries[0] or "").strip().upper()
            if country_code in {"CA", "CAN", "CANADA"}:
                params["geography"] = "Canada"
    elif len(countries) > 1:
        params["countries"] = countries

    return ParsedIntent(
        apiProvider=provider,
        indicators=[name],
        parameters=params,
        clarificationNeeded=False,
        confidence=0.99,
        recommendedChartType="line",
        queryType="data_fetch",
        originalQuery=query,
        isFollowUp=False,
        followUpType=None,
        resolvedQuery=None,
        needsDecomposition=False,
        decompositionType=None,
        decompositionEntities=None,
        useProMode=False,
    )


def _exact_surface_tokens(text: str) -> list[str]:
    """Return lowercase alphanumeric tokens for exact-title wrapper checks."""

    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _remove_first_token_sequence(tokens: list[str], needle: list[str]) -> list[str]:
    """Remove the first contiguous ``needle`` sequence from ``tokens``."""

    if not tokens or not needle or len(needle) > len(tokens):
        return tokens[:]
    limit = len(tokens) - len(needle) + 1
    for idx in range(limit):
        if tokens[idx : idx + len(needle)] == needle:
            return tokens[:idx] + tokens[idx + len(needle) :]
    return tokens[:]


def _contains_token_sequence(tokens: list[str], needle: list[str]) -> bool:
    """Return True when ``needle`` appears as a contiguous token sequence."""

    if not tokens or not needle or len(needle) > len(tokens):
        return False
    limit = len(tokens) - len(needle) + 1
    return any(tokens[idx : idx + len(needle)] == needle for idx in range(limit))


def _countries_outside_exact_title(
    query: str,
    provider: str,
    exact_title: str,
    countries: list[str],
) -> list[str]:
    """Keep only geography mentions that are outside the matched exact title.

    Exact provider-title requests can contain country-looking tokens as part of
    the provider-native title itself (for example OECD's ``MNE`` acronym, which
    also happens to be Montenegro's ISO-3 code).  Those title-internal tokens
    are metadata, not a requested geography filter.  Conversely, a leading
    scope such as ``Canada GDP from World Bank`` must remain a country filter.
    """

    if not countries:
        return []

    provider_key = _normalize_provider_name(provider)
    residual_tokens = _exact_surface_tokens(query)
    if provider_key in {"STATSCAN", "STATISTICS CANADA"}:
        # StatsCan exact table titles are often full descriptive sentences whose
        # country words are table scope ("imports from Canada", "United States
        # tariffs") rather than requested geography filters.  Prefer literal
        # substring removal before the normalized variant matcher so those
        # title-internal country words cannot survive as residual tokens.
        residual_text = str(query or "")
        literal_removed = False
        literal_title_patterns = [exact_title]
        literal_title_patterns.extend(exact_title_search_inputs(exact_title, provider))
        provider_alias_tokens = [
            _exact_surface_tokens(alias)
            for alias in (
                "statistics canada",
                "statscan",
                "from statistics canada",
                "from statscan",
            )
        ]
        for literal_title in sorted(
            {text for text in literal_title_patterns if str(text or "").strip()},
            key=len,
            reverse=True,
        ):
            next_text = re.sub(re.escape(literal_title), " ", residual_text, count=1, flags=re.IGNORECASE)
            if next_text != residual_text:
                residual_tokens = _exact_surface_tokens(next_text)
                for provider_alias in provider_alias_tokens:
                    residual_tokens = _remove_first_token_sequence(residual_tokens, provider_alias)
                literal_removed = True
                break
        if literal_removed:
            filtered: list[str] = []
            seen_codes: set[str] = set()
            for country in countries:
                code = CountryResolver.normalize(str(country)) or str(country or "").strip().upper()
                if not code or code in seen_codes:
                    continue
                aliases = [
                    alias
                    for alias, alias_code in CountryResolver.COUNTRY_ALIASES.items()
                    if alias_code == code
                ]
                aliases.append(code)
                if any(
                    _contains_token_sequence(residual_tokens, _exact_surface_tokens(alias))
                    for alias in aliases
                    if _exact_surface_tokens(alias)
                ):
                    filtered.append(country)
                    seen_codes.add(code)
            return filtered
    title_variants = [exact_title]
    title_variants.extend(exact_title_search_inputs(exact_title, provider))
    title_variants = sorted(
        {
            " ".join(_exact_surface_tokens(variant))
            for variant in title_variants
            if _exact_surface_tokens(variant)
        },
        key=lambda value: len(value.split()),
        reverse=True,
    )
    for variant in title_variants:
        next_tokens = _remove_first_token_sequence(residual_tokens, variant.split())
        if next_tokens != residual_tokens:
            residual_tokens = next_tokens
            break

    filtered: list[str] = []
    seen_codes: set[str] = set()
    for country in countries:
        code = CountryResolver.normalize(str(country)) or str(country or "").strip().upper()
        if not code or code in seen_codes:
            continue
        aliases = [
            alias
            for alias, alias_code in CountryResolver.COUNTRY_ALIASES.items()
            if alias_code == code
        ]
        aliases.append(code)
        if any(
            _contains_token_sequence(residual_tokens, _exact_surface_tokens(alias))
            for alias in aliases
            if _exact_surface_tokens(alias)
        ):
            filtered.append(country)
            seen_codes.add(code)
    return filtered


def _with_provider_native_comtrade_hs_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    """Overlay current Comtrade HS reference text for exact-title matching.

    The local indicator catalog can contain stale or duplicated HS titles.  UN
    Comtrade's own HS reference metadata is the provider-native title surface;
    use it to compare literal pasted titles while preserving fail-closed
    behavior when the provider reference is unavailable or tied.
    """

    provider = _normalize_provider_name(str(candidate.get("provider") or ""))
    if provider != "COMTRADE":
        return dict(candidate)
    code = str(candidate.get("code") or "").strip()
    if not code:
        return dict(candidate)
    try:
        from ..providers.comtrade_metadata import get_hs_reference_entry

        entry = get_hs_reference_entry(code)
    except Exception:
        entry = None
    if not entry:
        return dict(candidate)
    reference_title = str(entry.get("text") or "").strip()
    if not reference_title:
        return dict(candidate)
    enriched = dict(candidate)
    enriched["name"] = reference_title
    unit = str(entry.get("standardUnitAbbr") or "").strip()
    if unit and unit.lower() != "n/a":
        enriched["unit"] = unit
    enriched["raw_metadata"] = {
        **(
            dict(enriched.get("raw_metadata") or {})
            if isinstance(enriched.get("raw_metadata"), dict)
            else {}
        ),
        "comtrade_hs_reference": dict(entry),
    }
    return enriched


def _normalized_bis_alias_dataflow(code: str) -> str:
    """Return canonical BIS dataflow for mechanical BIS_/WS_ aliases."""
    normalized = str(code or "").strip().upper().replace(".", "_")
    if normalized.startswith("BIS_WS_"):
        return normalized.removeprefix("BIS_")
    if normalized.startswith("BIS_") and normalized.removeprefix("BIS_").startswith("WS_"):
        return normalized.removeprefix("BIS_")
    return normalized


def _prefer_bis_catalog_alias_candidate(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Collapse duplicate BIS exact-title aliases for the same dataflow."""
    if not candidates:
        return None
    alias_keys = {
        _normalized_bis_alias_dataflow(str(candidate.get("code") or ""))
        for candidate in candidates
        if str(candidate.get("code") or "").strip()
    }
    if len(alias_keys) != 1:
        return None
    signatures = {
        (
            re.sub(r"[^a-z0-9]+", " ", str(candidate.get("name") or "").lower()).strip(),
            re.sub(r"[^a-z0-9]+", " ", str(candidate.get("unit") or "").lower()).strip(),
            re.sub(r"[^a-z0-9]+", " ", str(candidate.get("frequency") or "").lower()).strip(),
        )
        for candidate in candidates
    }
    if len(signatures) != 1:
        return None
    sorted_candidates = sorted(
        candidates,
        key=lambda candidate: (
            0 if str(candidate.get("code") or "").strip().upper().startswith("BIS_WS_") else 1,
            str(candidate.get("code") or ""),
        ),
    )
    return sorted_candidates[0]


def looks_like_exact_provider_title_match(text: str, provider_name: str) -> bool:
    """Return True when `text` closely matches a provider-native indicator title.

    This is intentionally strict and only meant to catch cases where the user
    has effectively pasted a concrete indicator name (sometimes with a geography
    prefix such as "US"). In those cases we should not distill the query down
    to a generic concept or let broad catalog shortcuts outrank the exact title.
    """
    try:
        from .indicator_database import get_indicator_lookup

        lookup = get_indicator_lookup()
    except Exception:
        return False

    query_text = str(text or "").strip()
    normalized_text = re.sub(r"[^a-z0-9]+", " ", query_text.lower()).strip()
    if not normalized_text:
        return False

    search_inputs = exact_title_search_inputs(text, provider_name)
    provider_key = _normalize_provider_name(provider_name)
    min_name_len = 4 if provider_key == "COINGECKO" else 24
    if provider_key == "IMF":
        min_name_len = 8
    if provider_key == "WORLDBANK":
        # World Bank public source catalogs include short exact titles such as
        # "Terms of Trade".  Treat a literal provider-scoped title as exact user
        # input; the strict matcher below still rejects suffix/generic partials.
        min_name_len = 8
    if provider_key == "BIS":
        # BIS exposes concise provider-native titles such as "Credit-to-GDP gaps".
        # This remains a literal provider-scoped title path; the matcher below
        # still rejects generic fragments like "credit" or "GDP gaps".
        min_name_len = 8
    if provider_key == "FRED":
        # FRED exposes short provider-native titles; accepting them here only
        # signals an exact-title surface, not a final code decision.
        min_name_len = 2
    if provider_key == "STATSCAN":
        # StatsCan table titles are provider-native product surfaces.  Keep the
        # detector exact-title-only, but do not require broad catalog-title
        # length once the provider is locked.
        min_name_len = 8

    candidates = []
    seen_codes = set()
    exact_candidates_found = False
    try:
        exact_name_matches = getattr(lookup, "exact_name_matches", None)
        if callable(exact_name_matches):
            for candidate in exact_name_matches(search_inputs, provider=provider_name, limit=20):
                code = str(candidate.get("code") or "")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    candidates.append(candidate)
                    exact_candidates_found = True
    except Exception:
        pass
    if not exact_candidates_found:
        for search_text in search_inputs:
            try:
                for candidate in lookup.search(search_text, provider=provider_name, limit=20):
                    code = str(candidate.get("code") or "")
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    candidates.append(candidate)
            except Exception:
                continue

    for raw_candidate in candidates:
        candidate = _with_provider_native_comtrade_hs_metadata(raw_candidate)
        candidate_name = str(candidate.get("name") or "").strip().lower()
        normalized_name = re.sub(r"[^a-z0-9]+", " ", candidate_name).strip()
        if not normalized_name or len(normalized_name) < min_name_len:
            continue
        if any(
            _is_close_exact_title_match(normalized_query, normalized_name)
            or _is_permutation_exact_title_match(normalized_query, normalized_name)
            for normalized_query in (
                re.sub(r"[^a-z0-9]+", " ", candidate_query.lower()).strip()
                for candidate_query in search_inputs
            )
            if normalized_query
        ):
            return True
    return False


def find_exact_provider_title_match(text: str, provider_name: str) -> Optional[Dict[str, Any]]:
    """Return the best provider-local exact-title candidate for a raw query."""
    try:
        from .indicator_database import get_indicator_lookup

        lookup = get_indicator_lookup()
    except Exception:
        return None

    query_text = str(text or "").strip()
    normalized_text = re.sub(r"[^a-z0-9]+", " ", query_text.lower()).strip()
    if not normalized_text:
        return None

    search_inputs = exact_title_search_inputs(text, provider_name)
    provider_key = _normalize_provider_name(provider_name)
    exact_query_norms = {
        re.sub(r"[^a-z0-9]+", " ", candidate_query.lower()).strip()
        for candidate_query in search_inputs
        if candidate_query
    }
    min_name_len = 3 if provider_key == "COINGECKO" else 24
    if provider_key == "IMF":
        # IMF DataMapper/WEO exposes short, provider-native titles such as
        # "Real GDP growth" and "Population".  These are exact title matches,
        # not semantic concept-to-code shortcuts, so allow them through the
        # existing provider-scoped exact-title path.
        min_name_len = 8
    if provider_key == "WORLDBANK":
        # World Bank non-WDI public sources expose concise provider-native
        # titles (for example "Terms of Trade") that should be resolved as
        # literal exact-title requests instead of falling through to a search
        # result list.  This stays provider-scoped and exact-title only.
        min_name_len = 8
    if provider_key == "BIS":
        # BIS public dataflow titles can be shorter than the broad exact-title
        # floor (for example "Credit-to-GDP gaps").  Allow those literal titles
        # through only inside the existing strict provider-scoped matcher.
        min_name_len = 8
    if provider_key == "FRED":
        # FRED has provider-native short titles such as "M1" and
        # "Demand Deposits".  Keep the path strict: a provider-scoped exact
        # title still must match catalog metadata, and duplicate titles are
        # disambiguated by explicit frequency/unit metadata or left unresolved.
        min_name_len = 2
    if provider_key == "STATSCAN":
        # StatsCan table titles map mechanically to product IDs.  This remains
        # strict provider-scoped title transport, not a semantic concept map.
        min_name_len = 8
    query_country_codes = _extract_country_codes_from_text(query_text)
    explicit_frequency_tokens = _extract_exact_title_frequency_tokens(query_text)
    explicit_measurement_qualifiers = _extract_exact_title_measurement_qualifiers(query_text)
    explicit_unit_tokens: set[str] = set()
    explicit_unit_normalized = ""
    if provider_key != "STATSCAN":
        for search_input in search_inputs:
            unit_suffix_text = _trailing_exact_title_unit_suffix(search_input)
            if unit_suffix_text:
                explicit_unit_tokens = _exact_title_unit_tokens(unit_suffix_text)
                explicit_unit_normalized = _normalize_exact_title_unit_text(unit_suffix_text)
                break

    best_candidate: Optional[Dict[str, Any]] = None
    best_rank = (-999, -999, -1, -999, -999, -1, -999)
    ranked_matches: list[tuple[tuple[int, int, int, int, int, int, int], dict[str, Any]]] = []
    seen_codes = set()

    def _unit_compatibility_rank(query: str, name: str, unit: str = "") -> int:
        """Prefer exact-title candidates with the same measurement family.

        WorldBank and other broad catalogs often contain near-duplicate title
        families such as "female (number)" and "who are female (%)".  Token
        closeness alone can rank the percentage variant above the count variant,
        even when the query pasted the count title.  Keep this generic: use only
        explicit unit/measurement words present in the query and candidate name.
        """

        query_tokens = set(query.split())
        name_tokens = set(name.split())
        count_cues = {"number", "count", "counts", "total"}
        ratio_cues = {
            "percent",
            "percentage",
            "rate",
            "ratio",
            "share",
            "proportion",
            "per",
        }

        query_wants_count = bool(query_tokens & count_cues)
        query_wants_ratio = bool(query_tokens & ratio_cues)
        name_is_count = bool(name_tokens & count_cues)
        name_is_ratio = bool(name_tokens & ratio_cues)

        base_rank = 0
        if query_wants_count and name_is_count and not name_is_ratio:
            base_rank = 2
        elif query_wants_ratio and name_is_ratio and not name_is_count:
            base_rank = 2
        elif query_wants_count and name_is_ratio and not name_is_count:
            base_rank = -2
        elif query_wants_ratio and name_is_count and not name_is_ratio:
            base_rank = -2

        unit_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", str(unit or "").lower())
            if len(token) > 2 and token not in {"and", "the", "per"}
        }
        if explicit_unit_tokens:
            exact_unit_tokens = _exact_title_unit_tokens(unit)
            candidate_unit_normalized = _normalize_exact_title_unit_text(unit)
            if not exact_unit_tokens:
                return -100
            if explicit_unit_normalized and candidate_unit_normalized == explicit_unit_normalized:
                return base_rank + 100
            unit_overlap = len(explicit_unit_tokens & exact_unit_tokens)
            if unit_overlap <= 0:
                return -100
            missing_requested_tokens = len(explicit_unit_tokens - exact_unit_tokens)
            extra_candidate_tokens = len(exact_unit_tokens - explicit_unit_tokens)
            return base_rank + unit_overlap * 5 - missing_requested_tokens * 4 - extra_candidate_tokens * 2
        if not unit_tokens:
            return base_rank
        unit_specific_tokens = unit_tokens - name_tokens
        if not unit_specific_tokens:
            return base_rank
        query_unit_overlap = len(query_tokens & unit_specific_tokens)
        if query_unit_overlap <= 0:
            return base_rank
        missing_specific_tokens = len(unit_specific_tokens - query_tokens)
        return base_rank + query_unit_overlap * 3 - missing_specific_tokens

    candidates_by_input: list[tuple[str, dict[str, Any]]] = []
    exact_candidates_found = False
    exact_name_candidates: list[dict[str, Any]] = []
    try:
        exact_name_matches = getattr(lookup, "exact_name_matches", None)
        if callable(exact_name_matches):
            for candidate in exact_name_matches(search_inputs, provider=provider_name, limit=20):
                candidates_by_input.append((str(candidate.get("name") or ""), candidate))
                exact_name_candidates.append(candidate)
                exact_candidates_found = True
    except Exception:
        pass

    if not exact_candidates_found:
        for search_text in search_inputs:
            try:
                results = lookup.search(search_text, provider=provider_name, limit=20)
            except Exception:
                results = []
            candidates_by_input.extend((search_text, candidate) for candidate in results)

        if provider_key == "FRED" and not candidates_by_input:
            # FTS5 can miss hyphenated duration titles because "6-Month" is
            # tokenized differently from "6 Month".  Drop only provider/country
            # wrappers and search the literal title substring before falling
            # back to LLM parsing.
            for search_text in search_inputs:
                cleaned_search = re.sub(
                    r"\b(?:from|via|use)\s+fred\b",
                    " ",
                    search_text,
                    flags=re.IGNORECASE,
                )
                for alias_row in _COUNTRY_ALIAS_REGEX_ROWS:
                    cleaned_search = alias_row.leading_space.sub(" ", cleaned_search, count=1)
                cleaned_search = re.sub(r"\s+", " ", cleaned_search).strip(" ,;:-")
                if not cleaned_search:
                    continue
                cleaned_variants = [cleaned_search]
                comma_variant = re.sub(
                    r"\bauction\s+high\s+discount\b",
                    "auction high, discount",
                    cleaned_search,
                    flags=re.IGNORECASE,
                )
                if comma_variant != cleaned_search:
                    cleaned_variants.append(comma_variant)
                try:
                    from .indicator_database import get_indicator_lookup

                    raw_lookup = get_indicator_lookup()
                    conn = raw_lookup.db._get_connection()  # pylint: disable=protected-access
                    cursor = conn.cursor()
                    for cleaned_variant in cleaned_variants:
                        cursor.execute(
                            """
                            SELECT *
                            FROM indicators
                            WHERE provider = ?
                              AND lower(name) LIKE ?
                            ORDER BY COALESCE(popularity, 0) DESC, code
                            LIMIT 20
                            """,
                            (raw_lookup._normalize_provider(provider_name), f"%{cleaned_variant.lower()}%"),
                        )
                        candidates_by_input.extend((search_text, dict(row)) for row in cursor.fetchall())
                except Exception:
                    continue

    if exact_name_candidates:
        if provider_key == "IMF":
            qualified_short_by_code: dict[str, dict[str, Any]] = {}
            for raw_candidate in exact_name_candidates:
                candidate = _with_provider_native_comtrade_hs_metadata(raw_candidate)
                code = str(candidate.get("code") or "").strip()
                candidate_name = str(candidate.get("name") or "").strip().lower()
                normalized_name = re.sub(r"[^a-z0-9]+", " ", candidate_name).strip()
                if not code or not normalized_name or len(normalized_name) >= min_name_len:
                    continue
                if normalized_name not in exact_query_norms:
                    continue
                if not _imf_short_exact_title_with_catalog_context(query_text, candidate):
                    continue
                qualified = dict(candidate)
                qualified["params"] = {
                    **dict(qualified.get("params") or {}),
                    "__semantic_authority": "exact_user_input",
                    "__decision_source": "exact_title_qualified_short",
                }
                qualified_short_by_code.setdefault(code.upper(), qualified)
            if len(qualified_short_by_code) == 1:
                return next(iter(qualified_short_by_code.values()))
            if len(qualified_short_by_code) > 1:
                return None

        strict_exact_by_code: dict[str, dict[str, Any]] = {}
        for raw_candidate in exact_name_candidates:
            candidate = _with_provider_native_comtrade_hs_metadata(raw_candidate)
            code = str(candidate.get("code") or "").strip()
            if not code:
                continue
            candidate_name = str(candidate.get("name") or "").strip().lower()
            normalized_name = re.sub(r"[^a-z0-9]+", " ", candidate_name).strip()
            if not normalized_name or len(normalized_name) < min_name_len:
                continue
            if not any(
                normalized_query == normalized_name
                or _is_close_exact_title_match(normalized_query, normalized_name)
                or _is_permutation_exact_title_match(normalized_query, normalized_name)
                for normalized_query in exact_query_norms
                if normalized_query
            ):
                continue
            if explicit_unit_tokens:
                candidate_unit_text = " ".join(
                    str(value or "")
                    for value in (candidate.get("name"), candidate.get("unit"))
                )
                candidate_unit_tokens = _exact_title_unit_tokens(candidate_unit_text)
                if not explicit_unit_tokens <= candidate_unit_tokens:
                    continue
            if explicit_frequency_tokens:
                candidate_frequency = str(candidate.get("frequency") or "")
                if candidate_frequency and not _candidate_frequency_matches(
                    explicit_frequency_tokens,
                    candidate_frequency,
                ):
                    continue
            if explicit_measurement_qualifiers:
                candidate_measurement_text = " ".join(
                    str(value or "")
                    for value in (
                        candidate.get("name"),
                        candidate.get("description"),
                        candidate.get("keywords"),
                        candidate.get("category"),
                    )
                )
                if not _candidate_measurement_qualifiers_match(
                    explicit_measurement_qualifiers,
                    candidate_measurement_text,
                ):
                    continue
            strict_exact_by_code.setdefault(code.upper(), dict(candidate))
        if len(strict_exact_by_code) == 1:
            return next(iter(strict_exact_by_code.values()))
        if provider_key == "BIS" and len(strict_exact_by_code) > 1:
            canonical_candidate = _prefer_bis_catalog_alias_candidate(
                list(strict_exact_by_code.values())
            )
            if canonical_candidate is not None:
                return canonical_candidate
            return None

    for search_text, raw_candidate in candidates_by_input:
        candidate = _with_provider_native_comtrade_hs_metadata(raw_candidate)
        code = str(candidate.get("code") or "")
        if code in seen_codes:
            continue
        seen_codes.add(code)
        candidate_name = str(candidate.get("name") or "").strip().lower()
        normalized_name = re.sub(r"[^a-z0-9]+", " ", candidate_name).strip()
        if not normalized_name or len(normalized_name) < min_name_len:
            continue
        if any(
            normalized_query == normalized_name
            or _is_close_exact_title_match(normalized_query, normalized_name)
            or _is_permutation_exact_title_match(normalized_query, normalized_name)
            for normalized_query in (
                re.sub(r"[^a-z0-9]+", " ", candidate_query.lower()).strip()
                for candidate_query in search_inputs
            )
            if normalized_query
        ):
            title_exact = normalized_name in exact_query_norms
            if provider_key == "FRED" and explicit_frequency_tokens:
                candidate_frequency = str(candidate.get("frequency") or "")
                if candidate_frequency and not _candidate_frequency_matches(
                    explicit_frequency_tokens,
                    candidate_frequency,
                ):
                    continue
            if explicit_measurement_qualifiers:
                candidate_measurement_text = " ".join(
                    str(value or "")
                    for value in (
                        candidate.get("name"),
                        candidate.get("description"),
                        candidate.get("keywords"),
                        candidate.get("category"),
                    )
                )
                if not _candidate_measurement_qualifiers_match(
                    explicit_measurement_qualifiers,
                    candidate_measurement_text,
                ):
                    continue
            candidate = dict(candidate)
            if provider_key == "COMTRADE":
                original_query_lower = query_text.lower()
                candidate_params: dict[str, Any] = {}
                if re.search(r"\b(?:exports?|exported|re-exports?)\b", original_query_lower):
                    candidate_params["flow"] = "X"
                elif re.search(r"\b(?:imports?|imported|re-imports?)\b", original_query_lower):
                    candidate_params["flow"] = "M"
                if code and code.isdigit():
                    candidate_params["commodity"] = code
                if candidate_params:
                    candidate["params"] = {**dict(candidate.get("params") or {}), **candidate_params}
            candidate_country_codes = _extract_country_codes_from_text(candidate_name)
            country_rank = len(query_country_codes & candidate_country_codes)
            if provider_key == "STATSCAN":
                # Country names inside StatsCan table titles describe table
                # scope, not necessarily requested geography filters.  A
                # unique literal table title should not be demoted into a
                # clarification because it contains additional country words.
                country_rank = 0
            query_token_lengths = [
                len(normalized_query.split())
                for normalized_query in (
                    re.sub(r"[^a-z0-9]+", " ", candidate_query.lower()).strip()
                    for candidate_query in search_inputs
                )
                if normalized_query
            ]
            query_token_len = min(query_token_lengths) if query_token_lengths else len(normalized_text.split())
            name_token_len = len(normalized_name.split())
            token_delta = abs(name_token_len - query_token_len)
            unit_rank = _unit_compatibility_rank(
                normalized_text,
                normalized_name,
                str(candidate.get("unit") or ""),
            )
            measurement_rank = 0
            if explicit_measurement_qualifiers:
                candidate_measurement_text = " ".join(
                    str(value or "")
                    for value in (
                        candidate.get("name"),
                        candidate.get("description"),
                        candidate.get("keywords"),
                        candidate.get("category"),
                    )
                )
                measurement_rank = (
                    20
                    if _candidate_measurement_qualifiers_match(
                        explicit_measurement_qualifiers,
                        candidate_measurement_text,
                    )
                    else -100
                )
            shared_tokens = len(set(normalized_name.split()) & set(normalized_text.split()))
            popularity = int(candidate.get("popularity") or 0)
            rank = (
                measurement_rank,
                unit_rank,
                country_rank,
                -token_delta,
                popularity,
                shared_tokens,
                -name_token_len,
            )
            ranked_matches.append((rank, candidate))
            if rank > best_rank:
                best_candidate = candidate
                best_rank = rank
    if ranked_matches:
        if not explicit_unit_tokens and not explicit_frequency_tokens:
            best_name_signature = re.sub(
                r"[^a-z0-9]+",
                " ",
                str((best_candidate or {}).get("name") or "").lower(),
            ).strip()
            same_title_candidates = [
                candidate
                for _, candidate in ranked_matches
                if re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    str(candidate.get("name") or "").lower(),
                ).strip()
                == best_name_signature
            ]
            same_title_codes = {
                str(candidate.get("code") or "").strip().upper()
                for candidate in same_title_candidates
                if str(candidate.get("code") or "").strip()
            }
            same_title_unit_frequency_signatures = {
                (
                    re.sub(r"[^a-z0-9]+", " ", str(candidate.get("unit") or "").lower()).strip(),
                    re.sub(r"[^a-z0-9]+", " ", str(candidate.get("frequency") or "").lower()).strip(),
                )
                for candidate in same_title_candidates
            }
            if len(same_title_codes) > 1 and len(same_title_unit_frequency_signatures) > 1:
                return None
        top_matches = [
            candidate
            for rank, candidate in ranked_matches
            if rank == best_rank
        ]
        top_codes = {
            str(candidate.get("code") or "").strip().upper()
            for candidate in top_matches
            if str(candidate.get("code") or "").strip()
        }
        top_title_unit_signatures = {
            (
                re.sub(r"[^a-z0-9]+", " ", str(candidate.get("name") or "").lower()).strip(),
                re.sub(r"[^a-z0-9]+", " ", str(candidate.get("unit") or "").lower()).strip(),
                re.sub(r"[^a-z0-9]+", " ", str(candidate.get("frequency") or "").lower()).strip(),
            )
            for candidate in top_matches
        }
        if explicit_unit_tokens and best_rank[1] <= -100:
            return None
        if len(top_codes) > 1:
            if provider_key == "BIS":
                canonical_candidate = _prefer_bis_catalog_alias_candidate(top_matches)
                if canonical_candidate is not None:
                    return canonical_candidate
            if explicit_frequency_tokens:
                return best_candidate
            best_name_signature = re.sub(
                r"[^a-z0-9]+",
                " ",
                str((best_candidate or {}).get("name") or "").lower(),
            ).strip()
            if (
                provider_key == "COINGECKO"
                and len(top_title_unit_signatures) <= 1
                and len(best_name_signature.split()) >= 2
                and best_name_signature in exact_query_norms
            ):
                # CoinGecko can expose multiple provider-native asset ids with
                # the same exact display name.  For a literal two-plus-token
                # asset title, carry all exact provider-native ids forward
                # instead of arbitrarily choosing one or falling through to a
                # low-confidence LLM parse / generic bitcoin fallback.
                exact_coin_ids: list[str] = []
                seen_coin_ids: set[str] = set()
                for candidate in top_matches:
                    coin_id = str(candidate.get("code") or "").strip().lower()
                    if (
                        coin_id
                        and re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,127}", coin_id)
                        and coin_id not in seen_coin_ids
                    ):
                        exact_coin_ids.append(coin_id)
                        seen_coin_ids.add(coin_id)
                if len(exact_coin_ids) > 10:
                    return None
                if len(exact_coin_ids) > 1:
                    combined = dict(best_candidate or top_matches[0])
                    combined["code"] = exact_coin_ids[0]
                    combined["params"] = {
                        **dict(combined.get("params") or {}),
                        "coinIds": exact_coin_ids,
                        "__semantic_authority": "exact_user_input",
                        "__decision_source": "exact_title_multi_asset",
                    }
                    return combined
                return best_candidate
            if len(top_title_unit_signatures) <= 1 and len(normalized_name.split()) >= 3:
                return best_candidate
            # A provider-native exact title can legitimately map to multiple
            # series that differ only by unit or measurement convention (for
            # example IMF WEO GDP per-capita current-price variants).  Do not
            # silently certify whichever catalog row happens to sort first; the
            # user must provide a unit/code or get a clarification/no-decision
            # path rather than a confident but arbitrary answer.
            return None
    return best_candidate


def exact_title_search_inputs(text: str, provider_name: str) -> list[str]:
    """Generate strict exact-title search variants for provider-local title matches."""
    query_text = str(text or "").strip()
    if not query_text:
        return []

    provider_key = _normalize_provider_name(provider_name)
    provider_aliases = {
        "WORLDBANK": ["world bank", "worldbank"],
        "STATSCAN": ["statistics canada", "statscan"],
        "COINGECKO": ["coin gecko", "coingecko"],
        "EXCHANGERATE": ["exchange rate", "exchange rate-api", "exchangerate", "exchangerate-api"],
        "COMTRADE": ["comtrade", "un comtrade"],
        "OECD": ["oecd"],
        "EUROSTAT": ["eurostat"],
        "IMF": ["imf"],
        "FRED": ["fred"],
        "BIS": ["bis"],
    }.get(provider_key, [str(provider_name or "").strip().lower()])

    search_inputs: list[str] = []
    queue = [query_text]
    seen: set[str] = set()

    while queue:
        candidate = queue.pop(0).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        search_inputs.append(candidate)

        normalized_punctuation = re.sub(r"[,:;()\[\]%/\-–—]+", " ", candidate)
        normalized_punctuation = re.sub(r"\s+", " ", normalized_punctuation).strip(" ,;:-")
        if (
            normalized_punctuation
            and normalized_punctuation != candidate
            and normalized_punctuation not in seen
        ):
            queue.append(normalized_punctuation)

        without_leading_transform = re.sub(
            r"^(?:real|nominal)\s+",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        if (
            without_leading_transform
            and without_leading_transform != candidate
            and without_leading_transform not in seen
        ):
            queue.append(without_leading_transform)

        without_trailing_frequency = _strip_trailing_exact_title_frequency_wrapper(candidate)
        if (
            without_trailing_frequency
            and without_trailing_frequency != candidate
            and without_trailing_frequency not in seen
        ):
            queue.append(without_trailing_frequency)

        if provider_key != "STATSCAN":
            without_unit_suffix = _strip_trailing_exact_title_unit_suffix(candidate)
            if (
                without_unit_suffix
                and without_unit_suffix != candidate
                and without_unit_suffix not in seen
            ):
                queue.append(without_unit_suffix)

        # Strip a leading country alias only when it appears as a plain prefix.
        for alias_row in _COUNTRY_ALIAS_REGEX_ROWS:
            stripped_country = alias_row.leading_space.sub("", candidate, count=1).strip()
            if stripped_country != candidate and stripped_country not in seen:
                queue.append(stripped_country)

        # Strip common provider suffixes/prefixes around pasted titles.
        for alias in provider_aliases:
            stripped_suffix = re.sub(
                rf"\b(?:from|via|use)\s+{re.escape(alias)}\b$",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip(" ,;:")
            if stripped_suffix and stripped_suffix not in seen:
                queue.append(stripped_suffix)

            stripped_prefix = re.sub(
                rf"^\[?{re.escape(alias)}\]?\s*",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip()
            if stripped_prefix and stripped_prefix not in seen:
                queue.append(stripped_prefix)

        for reordered in _country_reordered_exact_title_variants(candidate):
            if reordered not in seen:
                queue.append(reordered)

        for comma_tail in _leading_acronym_comma_tail_exact_title_variants(candidate, provider_aliases):
            if comma_tail not in seen:
                queue.append(comma_tail)

        if provider_key == "IMF":
            without_definition = re.sub(r"\bdefinition\b", " ", candidate, flags=re.IGNORECASE)
            without_definition = re.sub(r"\s+", " ", without_definition).strip(" ,;:-")
            if without_definition and without_definition != candidate and without_definition not in seen:
                queue.append(without_definition)
            unit_suffix = re.search(
                r"\s+in\s+(?P<unit>[^,;:]+)$",
                candidate,
                flags=re.IGNORECASE,
            )
            if unit_suffix:
                unit_text = unit_suffix.group("unit")
                if re.search(
                    r"\b(?:u\.?s\.?|us|dollars?|international|currency|percent|percentage|"
                    r"capita|parity|ppp|index|constant|current|millions?|billions?)\b",
                    unit_text,
                    flags=re.IGNORECASE,
                ):
                    without_unit_suffix = candidate[: unit_suffix.start()].strip(" ,;:-")
                    if (
                        without_unit_suffix
                        and without_unit_suffix != candidate
                        and without_unit_suffix not in seen
                    ):
                        queue.append(without_unit_suffix)

        if provider_key == "COINGECKO":
            stripped_crypto_suffix = re.sub(
                r"\b(?:cryptocurrency|crypto|token|coin)\s+price\b",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip(" ,;:")
            if stripped_crypto_suffix and stripped_crypto_suffix not in seen:
                queue.append(stripped_crypto_suffix)
            stripped_price_suffix = re.sub(
                r"\bprice\b$",
                "",
                stripped_crypto_suffix or candidate,
                flags=re.IGNORECASE,
            ).strip(" ,;:")
            if stripped_price_suffix and stripped_price_suffix not in seen:
                queue.append(stripped_price_suffix)

        if provider_key == "COMTRADE":
            without_trade_flow = re.sub(
                r"^(?:exports?|imports?|re-exports?|re-imports?)\s+of\s+",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip(" ,;:-")
            if (
                without_trade_flow
                and without_trade_flow != candidate
                and without_trade_flow not in seen
            ):
                queue.append(without_trade_flow)

        if ":" in candidate:
            suffix = candidate.split(":", 1)[1].strip()
            if suffix and suffix not in seen:
                queue.append(suffix)

    return search_inputs


def _leading_acronym_comma_tail_exact_title_variants(
    text: str,
    provider_aliases: list[str],
) -> list[str]:
    """Return mechanical exact-title variants for leading acronym qualifiers.

    Provider titles often put institutional/source qualifiers after a comma
    (``Total wages and salaries, BLS``), while user-facing prompts may move the
    short all-caps qualifier to the front (``BLS Total wages and salaries``).
    This helper only preserves and reorders literal title tokens so the existing
    provider-scoped exact-title lookup can decide; it does not map acronyms,
    concepts, or providers to codes.
    """

    candidate = str(text or "").strip(" ,;:-")
    if not candidate or "," in candidate:
        return []

    match = re.match(r"^(?P<acronym>[A-Z]{2,8})\s+(?P<title>.+)$", candidate)
    if not match:
        return []

    acronym = match.group("acronym").strip()
    title = re.sub(r"\s+", " ", match.group("title")).strip(" ,;:-")
    if not title:
        return []

    acronym_lower = acronym.lower()
    provider_alias_set = {str(alias or "").strip().lower() for alias in provider_aliases}
    if acronym_lower in provider_alias_set:
        return []
    if CountryResolver.normalize(acronym):
        return []

    meaningful_title_tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9]+", title)
        if len(token) > 1
        and token.lower() not in {"and", "the", "for", "from", "with", "of", "in", "to"}
    ]
    if len(meaningful_title_tokens) < 3:
        return []

    if re.search(rf"\b{re.escape(acronym)}\b", title):
        return []

    return [f"{title}, {acronym}"]


def _extract_country_codes_from_text(text: str) -> set[str]:
    """Extract ISO country codes from free text using alias matching."""
    query_text = str(text or "").strip().lower()
    if not query_text:
        return set()

    codes: set[str] = set()
    for alias_row in _COUNTRY_ALIAS_REGEX_ROWS:
        if alias_row.boundary.search(query_text) and alias_row.code:
            codes.add(alias_row.code)
    return codes


def _country_reordered_exact_title_variants(candidate: str) -> list[str]:
    """Generate country-aware search variants for provider-native titles."""
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return []

    variants: list[str] = []
    for alias_row in _COUNTRY_ALIAS_REGEX_ROWS:
        suffix_match = alias_row.for_suffix.match(candidate_text)
        if suffix_match:
            head = suffix_match.group("head").strip(" ,;:-")
            country = suffix_match.group("country").strip()
            if head and country:
                variants.append(f"{country} {head}".strip())

        prefixed_match = alias_row.punct_prefix.match(candidate_text)
        if prefixed_match:
            country = prefixed_match.group("country").strip()
            head = prefixed_match.group("head").strip(" ,;:-")
            if country and head:
                variants.append(f"{country} {head}".strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        if variant and variant != candidate_text and variant not in seen:
            seen.add(variant)
            deduped.append(variant)
    return deduped


def _is_close_exact_title_match(normalized_query: str, normalized_name: str) -> bool:
    """Return True only for genuinely close provider-title matches.

    The old implementation allowed any provider title that merely *ended* with
    a short generic phrase such as "inflation rate", which caused false exact-
    title shortcuts like:

    - "Germany inflation rate" -> FRED "Trimmed Mean PCE Inflation Rate"

    Exact-title matching should stay strict. We still allow country/provider
    wrappers around a pasted title, but we reject generic suffix-only matches
    unless the query is almost the whole title.
    """
    if not normalized_query or not normalized_name:
        return False
    if normalized_query == normalized_name:
        return True

    query_tokens = normalized_query.split()
    name_tokens = normalized_name.split()

    def _without_country_prefix(tokens: list[str]) -> list[str]:
        if len(tokens) <= 1:
            return tokens
        prefix_text = " ".join(tokens[: min(4, len(tokens))])
        prefix_codes = _extract_country_codes_from_text(prefix_text)
        if not prefix_codes:
            return tokens
        for length in range(min(4, len(tokens) - 1), 0, -1):
            candidate_prefix = " ".join(tokens[:length])
            if _extract_country_codes_from_text(candidate_prefix):
                return tokens[length:]
        return tokens

    query_tokens = _without_country_prefix(query_tokens)
    if len(query_tokens) > 1 and query_tokens[0] in {
        "export",
        "exports",
        "import",
        "imports",
        "reexport",
        "reexports",
        "reimport",
        "reimports",
    }:
        query_tokens = query_tokens[1:]
    if len(query_tokens) > 1 and query_tokens[0] == "of":
        query_tokens = query_tokens[1:]
    token_delta = abs(len(query_tokens) - len(name_tokens))
    shared_tokens = len(set(query_tokens) & set(name_tokens))
    overlap_ratio = shared_tokens / max(1, min(len(query_tokens), len(name_tokens)))

    if normalized_query.endswith(normalized_name) or normalized_name.endswith(normalized_query):
        return len(query_tokens) >= 3 and token_delta <= 1 and overlap_ratio >= 0.8

    # Country/state wrappers and light metadata tokens ("US", "VA", "national currency")
    # should not block near-exact pasted titles when almost all tokens align.
    if max(len(query_tokens), len(name_tokens)) >= 5 and token_delta <= 2 and overlap_ratio >= 0.85:
        return True

    return False


def _is_permutation_exact_title_match(normalized_query: str, normalized_name: str) -> bool:
    """Return True when punctuation-only word reordering hides an exact title.

    Provider titles often use commas to separate the head noun from qualifiers
    ("Unemployment, male ...").  User/direct-cert queries commonly drop that
    punctuation ("Unemployment male ...").  FTS can miss the literal title in
    those cases, and the stricter ordered matcher above may reject it even
    though the token sets are effectively identical.  This helper is deliberately
    narrow: it only accepts near-equal token bags with almost no unmatched words,
    so generic suffix matches still stay rejected.
    """
    if not normalized_query or not normalized_name:
        return False
    query_tokens = normalized_query.split()
    name_tokens = normalized_name.split()
    if len(query_tokens) > 1 and query_tokens[0] in {
        "export",
        "exports",
        "import",
        "imports",
        "reexport",
        "reexports",
        "reimport",
        "reimports",
    }:
        query_tokens = query_tokens[1:]
    if len(query_tokens) > 1 and query_tokens[0] == "of":
        query_tokens = query_tokens[1:]
    if min(len(query_tokens), len(name_tokens)) < 5:
        return False
    token_delta = abs(len(query_tokens) - len(name_tokens))
    if token_delta > 1:
        return False
    query_counts = Counter(query_tokens)
    name_counts = Counter(name_tokens)
    unmatched = sum((query_counts - name_counts).values()) + sum((name_counts - query_counts).values())
    shared = sum((query_counts & name_counts).values())
    overlap = shared / max(1, min(len(query_tokens), len(name_tokens)))
    return unmatched <= 1 and overlap >= 0.92


# ---------------------------------------------------------------------------
# Semantic code hints
# ---------------------------------------------------------------------------

def code_semantic_hint(provider: str, code: str) -> str:
    """
    Derive lightweight semantic hints from provider-native code patterns.

    This improves relevance scoring when resolver candidates are code-heavy
    and have limited human-readable metadata.
    """
    provider_norm = _normalize_provider_name(provider)
    code_upper = str(code or "").upper().strip()
    if not code_upper:
        return ""

    hints: List[str] = []

    if provider_norm in {"WORLDBANK", "WORLD BANK"}:
        if ".IMP." in code_upper:
            hints.extend(["imports", "import"])
        if ".EXP." in code_upper:
            hints.extend(["exports", "export"])
        if ".TRD." in code_upper:
            hints.extend(["trade openness", "trade"])
        if ".RSB." in code_upper:
            hints.extend(["trade balance", "external balance"])
        if ".CAB." in code_upper:
            hints.extend(["current account"])
        if ".DOD." in code_upper:
            hints.extend(["government debt", "public debt"])
        if ".REX.REER" in code_upper:
            hints.extend(["real effective exchange rate", "reer"])
        if ".WPI." in code_upper:
            hints.extend(["producer price", "ppi"])
        if ".CPI." in code_upper:
            hints.extend(["consumer price", "inflation", "cpi"])
        if ".DEFL." in code_upper:
            hints.extend(["gdp deflator"])
        if code_upper.endswith(".ZS"):
            hints.extend(["% of gdp", "share of gdp"])
        if ".YG." in code_upper or ".1524." in code_upper:
            hints.extend(["youth", "15 to 24 years"])

    if provider_norm == "FRED":
        if code_upper.startswith("DGS"):
            hints.extend(["government bond yield", "treasury yield"])
            tenor = code_upper.replace("DGS", "")
            if tenor.isdigit():
                hints.extend([f"{tenor}-year", f"{tenor} year"])
        if code_upper.startswith("GS") and code_upper[2:].isdigit():
            tenor = code_upper[2:]
            hints.extend([f"{tenor}-year", f"{tenor} year", "government bond yield"])
        if "PPI" in code_upper:
            hints.extend(["producer price", "ppi"])
        if "CPI" in code_upper:
            hints.extend(["consumer price", "cpi", "inflation"])
        if "FEDFUNDS" in code_upper or code_upper in {"DFF", "DFEDTARU", "DFEDTARL"}:
            hints.extend(["policy rate", "federal funds"])
        if code_upper.startswith("DEX"):
            hints.extend(["exchange rate", "fx"])
        if "M1" in code_upper:
            hints.extend(["money supply", "m1"])
        if "M2" in code_upper:
            hints.extend(["money supply", "m2"])
        if "M3" in code_upper:
            hints.extend(["money supply", "m3"])

    if provider_norm == "IMF":
        if "BCA" in code_upper:
            hints.extend(["current account"])
        if "NGDP" in code_upper:
            hints.extend(["gdp", "% of gdp"])
        if "EREER" in code_upper or "REER" in code_upper:
            hints.extend(["real effective exchange rate", "reer"])
        if "PCPIPCH" in code_upper:
            hints.extend(["consumer price", "inflation", "cpi"])
        if "PPPI" in code_upper or "PWPI" in code_upper:
            hints.extend(["producer price", "ppi"])
        if code_upper.startswith("LER"):
            hints.extend(["employment rate"])
        if code_upper.startswith("LUR"):
            hints.extend(["unemployment rate"])
        if "_FY" in code_upper:
            hints.extend(["fiscal"])
        if "_FM" in code_upper:
            hints.extend(["female", "women"])
        if "_ML" in code_upper:
            hints.extend(["male", "men"])
        if "_UR" in code_upper:
            hints.extend(["urban"])
        if "_RU" in code_upper:
            hints.extend(["rural"])
        if "_IFT" in code_upper:
            hints.extend(["informal"])
        if "15T24" in code_upper:
            hints.extend(["15 to 24 years", "youth"])
        if "1564" in code_upper:
            hints.extend(["15 to 64 years"])
        if "GE15" in code_upper:
            hints.extend(["15 years and over"])

    if provider_norm == "BIS":
        if code_upper == "WS_DSR":
            hints.extend(["debt service ratio"])
        if code_upper == "WS_SPP":
            hints.extend(["house prices"])
        if code_upper == "WS_CBPOL":
            hints.extend(["policy rate"])

    if provider_norm == "OECD":
        if "UNEM" in code_upper:
            hints.extend(["unemployment rate"])
        if "EMP" in code_upper and "UNEM" not in code_upper:
            hints.extend(["employment rate"])
        if "CPI" in code_upper:
            hints.extend(["consumer price", "inflation", "cpi"])
        if "PPI" in code_upper:
            hints.extend(["producer price", "ppi"])
        if code_upper in {"IRLT", "IRST"} or "IRLT" in code_upper:
            hints.extend(["long-term interest rate", "bond yield"])

    if provider_norm == "EUROSTAT":
        if "UNE_RT" in code_upper or "UNEMP" in code_upper:
            hints.extend(["unemployment rate"])
        if "HICP" in code_upper or "PRC_HICP" in code_upper:
            hints.extend(["consumer price", "inflation", "hicp"])
        if "GDP" in code_upper or "NAMA_10_GDP" in code_upper:
            hints.extend(["gdp"])

    return " ".join(dict.fromkeys(hints))


# ---------------------------------------------------------------------------
# Resolved indicator relevance scoring
# ---------------------------------------------------------------------------

def score_resolved_indicator_relevance(
    svc: Any,
    indicator_query: str,
    provider: str,
    resolved: Any,
) -> float:
    """Score semantic relevance between user indicator query and resolved candidate."""
    if not resolved:
        return -999.0

    provider_norm = _normalize_provider_name(provider or getattr(resolved, "provider", ""))
    code_text = str(getattr(resolved, "code", "") or "")
    code_hint = code_semantic_hint(provider_norm, code_text)
    resolved_metadata = getattr(resolved, "metadata", None) or {}
    metadata_indicator = str(resolved_metadata.get("indicator", "") or "")
    metadata_description = str(resolved_metadata.get("description", "") or "")
    synthetic_series = {
        "metadata": {
            "source": provider_norm,
            "indicator": " ".join(
                part for part in [
                    metadata_indicator,
                    str(getattr(resolved, "name", "") or ""),
                    metadata_description,
                    code_hint,
                    code_text,
                ] if part
            ),
            "seriesId": code_text,
        }
    }
    return score_series_relevance(indicator_query, synthetic_series)


# ---------------------------------------------------------------------------
# Resolution thresholds
# ---------------------------------------------------------------------------

def minimum_resolved_relevance_threshold(indicator_query: str) -> float:
    """
    Minimum semantic relevance required to accept a resolved indicator code.

    Keeps high-precision intents strict (imports/exports ratios, REER, HICP, etc.)
    while allowing broader queries to remain flexible.
    """
    normalized_query = str(indicator_query or "").strip().lower()
    cue_set = extract_indicator_cues(normalized_query)
    has_ratio_query = _has_ratio_cue(normalized_query)

    threshold = -0.40
    strict_precision_cues = _STRICT_PRECISION_CUES
    high_precision_cues = {
        "trade_openness",
        "gdp_deflator",
        "hicp",
        "producer_price",
        "real_effective_exchange_rate",
    }

    if cue_set & strict_precision_cues:
        threshold = max(threshold, 0.10)
    if cue_set & high_precision_cues:
        threshold = max(threshold, 0.35)
    if has_ratio_query and (cue_set & {"import", "export"}):
        threshold = max(threshold, 0.45)
    return threshold


def is_placeholder_indicator_code(code: Optional[str]) -> bool:
    """Return True when indicator code is a non-actionable placeholder."""
    normalized = str(code or "").strip().upper()
    if not normalized:
        return True
    return normalized in {
        "N/A",
        "NA",
        "NONE",
        "NULL",
        "UNKNOWN",
        "DYNAMIC",
        "AUTO",
        "-",
        "--",
        "TBD",
    }


def _exact_provider_code_literal_present(code: str, *texts: str) -> bool:
    """Return True only when the user/request text literally contains ``code``.

    This is mechanical provider-native passthrough evidence, not semantic
    inference.  It prevents LLM/parser-produced provider-looking strings from
    being promoted to ``exact_user_input`` solely because a deterministic
    plausibility rule thought the code looked compatible with the query.
    """
    normalized_code = str(code or "").strip()
    if not normalized_code:
        return False
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(normalized_code)}(?![A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )
    return any(pattern.search(str(text or "")) for text in texts if text)


def indicator_resolution_threshold(indicator_query: str, resolved_source: str) -> float:
    """
    Dynamic acceptance threshold for resolver output.

    Long natural-language indicator prompts and directional trade queries tend to
    score lower in lexical systems; use a slightly lower threshold there while
    keeping strict defaults for weakly-signaled queries.
    """
    threshold = 0.68
    normalized_query = str(indicator_query or "").strip().lower()
    cue_set = extract_indicator_cues(normalized_query)
    has_ratio_query = _has_ratio_cue(normalized_query)
    strict_precision_cues = _STRICT_PRECISION_CUES
    high_precision_cues = {
        "trade_openness",
        "gdp_deflator",
        "hicp",
        "producer_price",
        "real_effective_exchange_rate",
    }

    if cue_set:
        threshold = 0.64
    if len(normalized_query.split()) >= 6:
        threshold = min(threshold, 0.62)
    if resolved_source in {"catalog", "translator"}:
        threshold = min(threshold, 0.62)
    if cue_set & strict_precision_cues:
        threshold = max(threshold, 0.72)
    if cue_set & high_precision_cues:
        threshold = max(threshold, 0.76)
    if has_ratio_query and (cue_set & {"import", "export"}):
        threshold = max(threshold, 0.78)
    if resolved_source in {"catalog", "translator"} and (cue_set & high_precision_cues):
        threshold = min(threshold, 0.74)

    return threshold


# ---------------------------------------------------------------------------
# Plausibility checks
# ---------------------------------------------------------------------------

def is_resolved_indicator_plausible(
    svc: Any,
    provider: str,
    indicator_query: str,
    resolved_code: str,
    resolved_name: str = "",
) -> bool:
    """
    Lightweight semantic plausibility check for resolved provider codes.

    Prevents high-confidence but semantically off-target code matches from
    overriding clearer natural-language intent (especially for opaque FRED IDs).
    """
    provider_upper = _normalize_provider_name(provider)
    query_cues = extract_indicator_cues(indicator_query or "")
    code_upper = str(resolved_code or "").upper()
    query_lower = str(indicator_query or "").lower()
    candidate_text = " ".join(
        part
        for part in [
            resolved_name,
            code_semantic_hint(provider_upper, code_upper),
            code_upper,
        ]
        if part
    ).lower()
    statscan_labour_force_surface = (
        provider_upper in {"STATSCAN", "STATISTICS CANADA"}
        and "labour force characteristics" in candidate_text
    )

    if provider_upper == "IMF":
        detail_markers = _IMF_GENERIC_DETAIL_MARKERS.get(code_upper)
        if detail_markers:
            query_markers = {
                marker for marker in detail_markers
                if marker in query_lower
            }
            if query_markers and not any(marker in candidate_text for marker in query_markers):
                return False

    if provider_upper in {"WORLDBANK", "WORLD BANK"}:
        detail_markers = _WORLDBANK_GENERIC_DETAIL_MARKERS.get(code_upper)
        if detail_markers:
            query_markers = {
                marker for marker in detail_markers
                if marker in query_lower
            }
            if query_markers and not any(marker in candidate_text for marker in query_markers):
                return False

    if provider_upper == "OECD":
        detail_markers = _OECD_GENERIC_DETAIL_MARKERS.get(code_upper)
        if detail_markers:
            query_markers = {
                marker for marker in detail_markers
                if marker in query_lower
            }
            if query_markers and not any(marker in candidate_text for marker in query_markers):
                return False

    if provider_upper == "EUROSTAT":
        detail_markers = _EUROSTAT_GENERIC_DETAIL_MARKERS.get(code_upper)
        if detail_markers:
            query_markers = {
                marker for marker in detail_markers
                if marker in query_lower
            }
            if query_markers and not any(marker in candidate_text for marker in query_markers):
                return False

    if not query_cues:
        return True

    if statscan_labour_force_surface and query_cues & {"unemployment", "employment_rate", "employment_population"}:
        return True

    if specialization_mismatch_penalty(query_lower, candidate_text) >= 1.8:
        return False

    if "gdp_deflator" in query_cues and not any(
        token in code_upper for token in ("DEFL", "DEFLATOR", "GDPDEFL")
    ):
        return False

    if "hicp" in query_cues and provider_upper in {"WORLDBANK", "IMF", "FRED", "STATSCAN", "STATISTICS CANADA"}:
        return False

    has_ratio_query = _has_ratio_cue(query_lower)

    if "current_account" in query_cues and not any(
        token in code_upper for token in ("BCA", "CAB", "CURRENT", "CURR")
    ):
        return False

    if "real_effective_exchange_rate" in query_cues and not any(
        token in code_upper for token in ("EREER", "REER")
    ):
        return False
    if (
        "real_effective_exchange_rate" in query_cues
        and provider_upper in {"WORLDBANK", "WORLD BANK"}
        and code_upper == "REER"
    ):
        return False

    if "trade_openness" in query_cues:
        if provider_upper in {"WORLDBANK", "WORLD BANK"}:
            if code_upper in {"NE.RSB.GNFS.ZS", "BN.GSR.GNFS.CD"}:
                return False
            if "TRD.GNFS" not in code_upper:
                return False
        if provider_upper == "IMF" and "XS_GDP" not in code_upper:
            return False

    if "producer_price" in query_cues:
        if provider_upper in {"WORLDBANK", "WORLD BANK"} and not any(
            token in code_upper for token in ("WPI", "PPI", "FP.WPI")
        ):
            return False
        if provider_upper == "IMF" and not any(
            token in code_upper for token in ("PPI", "PPPI", "PWPI")
        ):
            return False
        if provider_upper == "FRED" and "PPI" not in code_upper:
            return False
        if provider_upper == "OECD" and "PPI" not in code_upper:
            return False

    if "house_prices" in query_cues:
        if provider_upper in {"WORLDBANK", "WORLD BANK", "IMF"}:
            return False
        if provider_upper == "BIS" and code_upper != "WS_SPP":
            return False
        if provider_upper == "FRED" and not any(
            token in code_upper for token in ("HPI", "CSUSHPI", "USSTHPI")
        ):
            return False
        if provider_upper == "EUROSTAT" and "HPI" not in code_upper:
            return False

    if provider_upper == "FRED":
        if "m1" in query_lower and "M1" not in code_upper:
            return False
        if "m2" in query_lower and "M2" not in code_upper:
            return False
        if "m3" in query_lower and "M3" not in code_upper:
            return False
        if ("10-year" in query_lower or "10 year" in query_lower) and not (
            "10" in code_upper or "DGS10" in code_upper or "GS10" in code_upper
        ):
            return False
        if "tenor_2y" in query_cues and not (
            "2" in code_upper or "DGS2" in code_upper or "GS2" in code_upper
        ):
            return False
        if "tenor_10y" in query_cues and not (
            "10" in code_upper or "DGS10" in code_upper or "GS10" in code_upper
        ):
            return False
        if "tenor_30y" in query_cues and not (
            "30" in code_upper or "DGS30" in code_upper or "GS30" in code_upper
        ):
            return False

    if provider_upper == "OECD":
        if "bond_yield" in query_cues and not any(
            token in code_upper for token in ("IRLT", "YIELD", "BOND")
        ):
            return False
        if "gdp_deflator" in query_cues and "DEFL" not in code_upper:
            return False
        if "hicp" in query_cues and "HICP" not in code_upper:
            return False
        if "trade_openness" in query_cues and not any(
            token in code_upper for token in ("TRADE", "XS_GDP", "BOP")
        ):
            return False
        if "producer_price" in query_cues and "PPI" not in code_upper:
            return False

    if provider_upper == "BIS":
        if "gap" not in query_lower and "GAP" in code_upper:
            return False
        if "gap" in query_lower and code_upper == "WS_TC":
            return False
        if "debt_gdp_ratio" in query_cues:
            if code_upper in {"WS_DSR", "WS_DEBT_SEC2_PUB"}:
                return False
            if not (query_cues & {"credit", "household_debt", "debt_service"}):
                return False
            if code_upper == "WS_TC" and not (query_cues & {"credit", "household_debt"}):
                return False

        if "debt_service" in query_cues and code_upper != "WS_DSR":
            return False
        if (
            "public_debt" in query_cues
            and code_upper.startswith("WS_")
            and not (query_cues & {"credit", "debt_service", "household_debt"})
        ):
            return False
        if "real_effective_exchange_rate" in query_cues and code_upper == "WS_XRU":
            return False

    if has_ratio_query and "trade_balance" in query_cues:
        if provider_upper in {"WORLDBANK", "WORLD BANK"} and code_upper in {"BN.GSR.GNFS.CD"}:
            return False

    return True


def extract_series_provider_and_code(svc: Any, series: Any) -> tuple[str, str]:
    """Extract normalized provider and provider-native code from one series."""
    meta = getattr(series, "metadata", None) if series is not None else None
    if not meta:
        return "", ""

    provider = _normalize_provider_name(str(getattr(meta, "source", "") or ""))
    series_id = str(getattr(meta, "seriesId", "") or "").strip()
    indicator = str(getattr(meta, "indicator", "") or "").strip()
    if series_id:
        return provider, series_id
    if indicator and svc._looks_like_provider_indicator_code(provider, indicator):
        return provider, indicator
    return provider, ""


def has_implausible_top_series(svc: Any, query: str, data: List[Any]) -> bool:
    """
    Check whether top-ranked result is semantically implausible for the query.

    This is used as a post-agent guardrail before final response emission.
    """
    if not data:
        return False

    provider, code = extract_series_provider_and_code(svc, data[0])
    if not provider or not code:
        return False

    indicator_name = ""
    meta = getattr(data[0], "metadata", None)
    if meta:
        indicator_name = str(getattr(meta, "indicator", "") or "")

    return not is_resolved_indicator_plausible(
        svc=svc,
        provider=provider,
        indicator_query=query,
        resolved_code=code,
        resolved_name=indicator_name,
    )


# ---------------------------------------------------------------------------
# BIS metadata normalization
# ---------------------------------------------------------------------------

def normalize_bis_metadata_labels(svc: Any, data: List[Any]) -> None:
    """
    Replace opaque provider indicator codes with human-readable labels when possible.

    Applies both to fresh and cached responses so user-facing metadata stays clear.
    """
    if not data:
        return

    for series in data:
        metadata = getattr(series, "metadata", None) if series is not None else None
        if not metadata:
            continue

        source = _normalize_provider_name(str(getattr(metadata, "source", "") or ""))
        indicator_value = str(getattr(metadata, "indicator", "") or "").strip()
        series_id_value = str(getattr(metadata, "seriesId", "") or "").strip().upper()
        description_value = str(getattr(metadata, "description", "") or "").strip()

        if source == "BIS":
            code_value = series_id_value
            if not code_value:
                indicator_upper = indicator_value.upper()
                if indicator_upper.startswith("WS_"):
                    code_value = indicator_upper

            if code_value:
                name, description = svc.bis_provider._lookup_dataflow_info(code_value)
                if name and (not indicator_value or indicator_value.upper() == code_value):
                    metadata.indicator = name
                    indicator_value = str(name).strip()
                if description and not description_value:
                    metadata.description = description
                    description_value = str(description).strip()

        # Generic fallback for all providers:
        # if indicator is code-like and we have a human-readable description,
        # promote description to user-facing indicator label.
        if description_value:
            indicator_code_like = svc._looks_like_provider_indicator_code(source, indicator_value)
            indicator_matches_series = bool(
                indicator_value
                and series_id_value
                and indicator_value.upper() == series_id_value.upper()
            )
            description_is_human = bool(re.search(r"[A-Za-z]", description_value)) and (" " in description_value)
            if description_is_human and (
                not indicator_value
                or indicator_code_like
                or indicator_matches_series
            ):
                metadata.indicator = description_value


# ---------------------------------------------------------------------------
# Concept provider override
# ---------------------------------------------------------------------------

def apply_concept_provider_override(
    svc: Any,
    provider: str,
    intent: ParsedIntent,
    params: dict,
) -> tuple[str, dict]:
    """Compatibility shim: do not force semantic provider/code overrides.

    Semantic catalog/rule-based provider or indicator remapping is intentionally
    disabled. Indicator/provider selection must be handled by retrieval plus LLM
    adjudication, exact user-provided codes/titles, or mechanical API plumbing.
    """
    return provider, params


# ---------------------------------------------------------------------------
# Disabled catalog availability remapping
# ---------------------------------------------------------------------------

def apply_catalog_availability_override(
    svc: Any,
    provider: str,
    intent: ParsedIntent,
    params: dict,
    fallback_excluded_providers: set,
) -> tuple[str, dict]:
    """Compatibility shim: do not reroute using catalog availability.

    Catalog availability was a semantic rule layer that could replace a valid
    provider-native selection with a forced provider/code. It is intentionally
    disabled under the no semantic shortcut rule.
    """
    return provider, params


# ---------------------------------------------------------------------------
# Indicator resolution pipeline
# ---------------------------------------------------------------------------

async def resolve_indicator_for_fetch(
    svc: Any,
    provider: str,
    intent: ParsedIntent,
    params: dict,
) -> dict:
    """Resolve and validate the indicator code for a fetch operation.

    Uses exact provider-native passthrough and IndicatorSelector (embed -> LLM
    pick) as final indicator authority. If those paths cannot decide, the
    fetch keeps provider-neutral semantic text and fails closed downstream
    rather than falling through to legacy catalog/translator shortcuts.

    Mutates intent.parameters and potentially intent.indicators (for
    WorldBank multi-indicator collapse). Returns the updated params dict.
    """
    if provider not in {"STATSCAN", "STATISTICS CANADA", "FRED", "IMF", "WORLDBANK", "EUROSTAT", "OECD", "BIS"}:
        return params

    def _apply_indicator_with_semantic_label(indicator_value: str, **extra: Any) -> dict:
        semantic_label = str(
            params.get("__semantic_indicator_label")
            or extra.get("__semantic_indicator_label")
            or ""
        ).strip()
        if not semantic_label:
            semantic_label = (
                select_indicator_query_for_resolution(svc, intent)
                or _effective_original_query(intent)
                or str(intent.indicators[0] if intent.indicators else "")
            ).strip()

        merged = {**params, "indicator": indicator_value, **extra}
        if semantic_label and not _looks_like_provider_indicator_code_local(provider, semantic_label):
            merged["__semantic_indicator_label"] = semantic_label
        return merged

    def _statscan_selected_product_extra(indicator_value: str, authority: str) -> Dict[str, str]:
        if provider not in {"STATSCAN", "STATISTICS CANADA"}:
            return {}
        product_id = str(indicator_value or "").strip()
        if not product_id:
            return {}
        return {
            "__statscan_product_id": product_id,
            "__statscan_product_authority": authority,
        }

    existing_indicator = str(params.get("indicator") or "").strip()
    if provider == "IMF" and not existing_indicator and len(intent.indicators or []) == 1:
        candidate_indicator = str((intent.indicators or [""])[0] or "").strip()
        catalog_exact_match = False
        if candidate_indicator and svc._looks_like_provider_indicator_code(provider, candidate_indicator):
            try:
                from .indicator_database import get_indicator_lookup

                indicator_entry = get_indicator_lookup().get(provider, candidate_indicator.upper())
                catalog_exact_match = _imf_catalog_entry_supports_exact_code(
                    candidate_indicator,
                    indicator_entry,
                )
            except Exception as exc:
                logger.debug(
                    "IMF parsed-code catalog check skipped for %s: %s",
                    candidate_indicator,
                    exc,
                )
        if catalog_exact_match:
            logger.info(
                "🔒 Using provider-native %s indicator from parsed intent without dynamic resolution: %s",
                provider,
                candidate_indicator,
            )
            params = _apply_indicator_with_semantic_label(
                candidate_indicator,
                __semantic_indicator_label=candidate_indicator,
                __exact_provider_code_match=True,
                __semantic_authority="exact_user_input",
                __decision_source="exact_code",
            )
            intent.parameters = params
            existing_indicator = candidate_indicator

    has_explicit_code = bool(
        existing_indicator
        and svc._looks_like_provider_indicator_code(provider, existing_indicator)
    )
    exact_match_locked = is_exact_match_locked(params)
    if (
        not has_explicit_code
        and exact_match_locked
        and provider == "IMF"
        and existing_indicator
    ):
        try:
            from .indicator_database import get_indicator_lookup

            indicator_entry = get_indicator_lookup().get("IMF", existing_indicator.upper())
            has_explicit_code = (
                bool(indicator_entry)
                and str(indicator_entry.get("category") or "").strip().upper() == "WEO"
            )
        except Exception as exc:
            logger.debug(
                "IMF exact WEO code catalog check skipped for %s: %s",
                existing_indicator,
                exc,
            )
    if (
        not has_explicit_code
        and exact_match_locked
        and provider in {"WORLDBANK", "WORLD BANK"}
        and existing_indicator
    ):
        try:
            from .indicator_database import get_indicator_lookup

            has_explicit_code = bool(get_indicator_lookup().get("WorldBank", existing_indicator))
        except Exception as exc:
            logger.debug(
                "WorldBank exact-code catalog check skipped for %s: %s",
                existing_indicator,
                exc,
            )

    # Path 1: Validate explicit code against query context
    if has_explicit_code and exact_match_locked:
        logger.info(
            "🔒 Keeping exact %s indicator code without plausibility override: %s",
            provider,
            existing_indicator,
        )
        params = _apply_indicator_with_semantic_label(
            existing_indicator,
            __semantic_authority="exact_user_input",
            __decision_source="exact_code",
            **_statscan_selected_product_extra(existing_indicator, "exact_user_input"),
        )
        intent.parameters = params
        return params

    existing_semantic_authority = str(params.get("__semantic_authority") or "")
    if (
        has_explicit_code
        and existing_semantic_authority in {"llm_adjudication", "post_fetch_semantic_judge"}
        and not is_placeholder_indicator_code(existing_indicator)
    ):
        # Do not let deterministic semantic plausibility rules overrule or
        # supply final semantic authority.  A selector/post-fetch-adjudicated
        # code has already passed an evidence gate; downstream fetch/verification
        # may still fail closed, but rule code must not make the semantic
        # accept/reject decision here or remap the selected product.
        params = _apply_indicator_with_semantic_label(
            existing_indicator,
            __semantic_authority=existing_semantic_authority,
            __decision_source=str(
                params.get("__decision_source")
                or ("llm_pick" if existing_semantic_authority == "llm_adjudication" else "post_fetch_semantic_judge")
            ),
            **(
                _statscan_selected_product_extra(existing_indicator, "llm_adjudication")
                if existing_semantic_authority == "llm_adjudication"
                else {}
            ),
        )
        intent.parameters = params
        return params

    if has_explicit_code:
        explicit_code_in_user_text = _exact_provider_code_literal_present(
            existing_indicator,
            _effective_original_query(intent),
            str(intent.originalQuery or ""),
            str(intent.resolvedQuery or ""),
        )
        if not explicit_code_in_user_text:
            logger.info(
                "🔎 Provider-looking %s indicator '%s' was not literal user input; "
                "refusing deterministic plausibility promotion and attempting selector resolution",
                provider,
                existing_indicator,
            )
            has_explicit_code = False

    if has_explicit_code:
        logger.info(
            "🔒 Keeping literal user-supplied %s indicator code: %s",
            provider,
            existing_indicator,
        )
        params = _apply_indicator_with_semantic_label(
            existing_indicator,
            __semantic_authority="exact_user_input",
            __decision_source="exact_code",
            **_statscan_selected_product_extra(existing_indicator, "exact_user_input"),
        )
        intent.parameters = params
        return params

    # Dynamic resolution (IndicatorSelector -> provider-neutral raw query)
    indicator_query = select_indicator_query_for_resolution(svc, intent)
    if not indicator_query and intent.indicators:
        indicator_query = str(intent.indicators[0] or "").strip()
    if not indicator_query:
        indicator_query = existing_indicator

    if not indicator_query:
        return params

    country_context = params.get("country")
    countries_context = params.get("countries") if isinstance(params.get("countries"), list) else None
    selected_query_override = (
        bool(intent.indicators)
        and indicator_query != str(intent.indicators[0] or "").strip()
    )

    # Path 1.5: IndicatorSelector (embed -> LLM pick) -- primary resolution.
    # For StatsCan, search on the distilled indicator phrase rather than the
    # full user query.  Geography/date words such as "Canada" or "in 2017"
    # are fetch parameters, not indicator semantics, and can cause unrelated
    # country-specific tables to outrank the intended measure.
    original_selector_query = (_effective_original_query(intent) or "").strip()
    if provider in {"STATSCAN", "STATISTICS CANADA"}:
        selector_query = (indicator_query or original_selector_query).strip()
    elif is_provider_locked(intent.parameters or {}) and not is_exact_match_locked(intent.parameters or {}):
        if len(intent.indicators or []) > 1:
            selector_query = (original_selector_query or indicator_query or "").strip()
        else:
            selector_query = (indicator_query or original_selector_query).strip()
    else:
        # For follow-ups, use resolvedQuery (e.g. "GDP per capita India")
        # rather than the raw follow-up text (e.g. "show from IMF instead").
        selector_query = (original_selector_query or indicator_query or "").strip()
    exact_title_query = (original_selector_query or selector_query).strip()
    if provider and looks_like_exact_provider_title_match(exact_title_query, provider):
        logger.info(
            "🎯 Resolving exact %s title match without semantic shortcuts: %s",
            provider,
            exact_title_query,
        )
        exact_match = find_exact_provider_title_match(exact_title_query, provider)
        if exact_match and exact_match.get("code"):
            exact_code = str(exact_match["code"])
            params = _apply_indicator_with_semantic_label(
                exact_code,
                __semantic_indicator_label=str(exact_match.get("name") or exact_title_query),
                __semantic_authority="exact_user_input",
                __decision_source="exact_title",
                __exact_indicator_title_match=True,
                **_statscan_selected_product_extra(exact_code, "exact_user_input"),
            )
            intent.parameters = params
            return params
        logger.info(
            "🚫 Exact-title detector found no provider-local code for %s: %s",
            provider,
            exact_title_query,
        )
        provider_locked_natural_language = (
            is_provider_locked(intent.parameters or params)
            and not has_explicit_code
            and not is_exact_match_locked(intent.parameters or params)
        )
        if not provider_locked_natural_language:
            params = _apply_indicator_with_semantic_label(indicator_query)
            params["__indicator_selection_status"] = "exact_title_unresolved"
            intent.parameters = params
            return params
        logger.info(
            "↪️ Continuing provider-locked %s natural-language query through selector after exact-title miss: %s",
            provider,
            selector_query,
        )

    selector_attempted = False
    selector_without_final_authority = False
    selector_source = ""
    selector_rejection_reason = ""
    selector_retry_query = ""
    try:
        from .indicator_selector import IndicatorSelector
        selector = IndicatorSelector()
        selector_attempted = True
        metadata_query = None
        if (
            provider == "FRED"
            and original_selector_query
            and indicator_query
            and _lost_explicit_frequency_tokens(original_selector_query, indicator_query)
        ):
            metadata_query = original_selector_query
        if metadata_query:
            selection = await selector.select(selector_query, provider, metadata_query=metadata_query)
        else:
            selection = await selector.select(selector_query, provider)
        selector_source = str(getattr(selection, "source", "") or "")
        selector_rejection_reason = str(getattr(selection, "rejection_reason", "") or "")
        selector_retry_query = str(getattr(selection, "retry_query", "") or "")
        if selection.code:
            selection_name = str(getattr(selection, "name", "") or "")
            if is_placeholder_indicator_code(selection.code):
                selector_without_final_authority = True
                selector_rejection_reason = "placeholder_llm_pick"
            else:
                logger.info(
                    "🎯 IndicatorSelector resolved: '%s' → %s [%s]",
                    indicator_query, selection.code, selection.source,
                )
                params = _apply_indicator_with_semantic_label(
                    selection.code,
                    __semantic_authority="llm_adjudication",
                    __decision_source="llm_pick",
                    __indicator_selection_source=selection.source,
                    **_statscan_selected_product_extra(selection.code, "llm_adjudication"),
                )
                if provider in {"WORLDBANK", "WORLD BANK"} and selected_query_override and len(intent.indicators) > 1:
                    logger.info(
                        "🔎 Collapsing World Bank multi-indicator intent to selector-resolved indicator '%s'",
                        selection.code,
                    )
                    intent.indicators = [selection.code]
                intent.parameters = params
                return params
        if selection.needs_user_choice:
            logger.info(
                "🔵 IndicatorSelector needs user choice: %d options",
                len(selection.options),
            )
            params = {**params, "__indicator_options": selection.options}
            intent.parameters = params
            selector_without_final_authority = True
        elif not selection.code:
            selector_without_final_authority = True
    except Exception as e:
        logger.debug("IndicatorSelector unavailable; failing closed without retired fallback: %s", e)
        selector_attempted = True
        selector_without_final_authority = True
        selector_source = "selector_unavailable"

    if (
        selector_attempted
        and selector_without_final_authority
    ):
        logger.info(
            "🚫 Skipping retired indicator fallback on no-shortcut path after selector source=%s",
            selector_source or "unknown",
        )
        params = _apply_indicator_with_semantic_label(indicator_query)
        params["__indicator_selection_status"] = selector_source or "no_decision"
        if selector_rejection_reason:
            params["__indicator_rejection_reason"] = selector_rejection_reason
        if selector_retry_query:
            params["__indicator_retry_query"] = selector_retry_query
        intent.parameters = params
        return params

    # No selector final authority. Keep provider-neutral semantic text and let
    # provider retrieval fail closed or ask for clarification; do not invoke the
    # retired catalog/translator shortcuts as final authority.
    params = _apply_indicator_with_semantic_label(indicator_query)
    params.setdefault("__indicator_selection_status", "no_decision")

    intent.parameters = params
    return params


# ---------------------------------------------------------------------------
# Indicator query selection
# ---------------------------------------------------------------------------

def select_indicator_query_for_resolution(svc: Any, intent: ParsedIntent) -> str:
    """
    Pick the best query string for indicator resolution.

    Uses LLM indicator text by default, but falls back to the original user
    query when semantic cues clearly mismatch.

    IMPORTANT: If the indicator looks like a provider-specific code (e.g.,
    NE.EXP.GNFS.ZS for WorldBank, EREER for IMF), prefer the original
    query text or distilled indicator phrase.
    """
    if not intent.indicators:
        return ""

    indicator_query = str(intent.indicators[0] or "").strip()
    if not indicator_query:
        return ""

    # For follow-up queries, originalQuery is the raw follow-up text
    # (e.g. "show from IMF instead") which has no indicator information.
    # Use resolvedQuery (e.g. "GDP per capita India") for resolution.
    original_query = _effective_original_query(intent)
    if not original_query:
        return indicator_query

    distilled_original = build_distilled_indicator_query(svc, original_query)
    semantic_indicator_label = str((intent.parameters or {}).get("__semantic_indicator_label") or "").strip()
    provider_locked = is_provider_locked(intent.parameters or {})

    def _fallback_to_original_or_distilled() -> str:
        if provider_locked:
            # Provider locking means "do not switch providers"; for StatsCan it
            # should not force geography/date words back into indicator
            # selection.  For non-exact provider-locked requests, prefer the
            # parsed/distilled indicator phrase over the full query so country
            # and provider words do not pollute candidate retrieval. Exact
            # provider-native codes/titles return before this selector path.
            if _normalize_provider_name(intent.apiProvider or "") in {"STATSCAN", "STATISTICS CANADA"}:
                return semantic_indicator_label or distilled_original or original_query
            if len(intent.indicators or []) > 1:
                return original_query or distilled_original or semantic_indicator_label
            return semantic_indicator_label or distilled_original or original_query
        return semantic_indicator_label or distilled_original or original_query

    # If the indicator looks like a provider-specific code, never use it
    # for cross-provider resolution -- prefer the original query text.
    provider = _normalize_provider_name(intent.apiProvider or "")
    if provider and svc._looks_like_provider_indicator_code(provider, indicator_query):
        logger.info(
            "🔎 Indicator '%s' looks like a %s-specific code. Using parsed/distilled query for resolution.",
            indicator_query,
            provider,
        )
        return _fallback_to_original_or_distilled()

    if provider and looks_like_exact_provider_title_match(original_query, provider):
        if provider_locked and not is_exact_match_locked(intent.parameters or {}) and len(intent.indicators or []) <= 1:
            logger.info(
                "🔎 Provider-locked original query looks like an exact %s title; using parsed/distilled indicator phrase for selector fallback.",
                provider,
            )
            return semantic_indicator_label or indicator_query or distilled_original or original_query
        logger.info(
            "🔎 Original query looks like an exact %s indicator title. Using original query for resolution.",
            provider,
        )
        return original_query

    indicator_lower = indicator_query.lower()
    if any(term in indicator_lower for term in ("discontinued", "deprecated", "legacy")):
        logger.info("🔎 Parsed indicator appears deprecated/discontinued. Using original query.")
        return _fallback_to_original_or_distilled()

    original_lower = original_query.lower()
    has_ratio_original = _has_ratio_cue(original_lower)
    has_ratio_indicator = _has_ratio_cue(indicator_lower)
    if has_ratio_original and not has_ratio_indicator:
        logger.info(
            "🔎 Indicator dropped GDP-ratio context. Using original query for resolution."
        )
        return _fallback_to_original_or_distilled()

    original_cues = extract_indicator_cues(original_query)
    indicator_cues = extract_indicator_cues(indicator_query)
    high_signal_exclusions = {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    high_signal_original_cues = {
        cue for cue in original_cues if cue not in high_signal_exclusions
    }
    high_signal_indicator_cues = {
        cue for cue in indicator_cues if cue not in high_signal_exclusions
    }

    if high_signal_original_cues and not (high_signal_original_cues & high_signal_indicator_cues):
        logger.info(
            "🔎 Indicator cue mismatch (original=%s, parsed=%s). Using original query for resolution.",
            sorted(high_signal_original_cues),
            sorted(high_signal_indicator_cues),
        )
        return _fallback_to_original_or_distilled()

    directional_cues = {"import", "export", "trade_balance"}
    original_directional = high_signal_original_cues & directional_cues
    indicator_directional = high_signal_indicator_cues & directional_cues
    if original_directional and not (original_directional & indicator_directional):
        logger.info(
            "🔎 Directional cue mismatch (original=%s, parsed=%s). Using original query for resolution.",
            sorted(original_directional),
            sorted(indicator_directional),
        )
        return _fallback_to_original_or_distilled()

    original_terms = tokenize_indicator_terms(original_query)
    indicator_terms = tokenize_indicator_terms(indicator_query)
    if original_terms and indicator_terms:
        overlap = len(original_terms & indicator_terms) / max(len(original_terms), 1)
        if overlap < 0.15:
            logger.info(
                "🔎 Low indicator-term overlap (%.2f). Using original query for resolution.",
                overlap,
            )
            return _fallback_to_original_or_distilled()

    # Ranking/comparison phrasing can contain execution words ("top", "rank",
    # "highest") that are poor resolver inputs. Prefer a distilled metric phrase.
    if (is_ranking_query(original_query) or is_comparison_query(original_query)) and distilled_original:
        return distilled_original

    # If parser returned a long natural-language sentence as the indicator,
    # prefer a distilled metric phrase for stable cross-provider resolution.
    if (
        len(indicator_query.split()) >= 8
        and distilled_original
        and not looks_like_exact_provider_title_match(original_query, provider)
    ):
        return distilled_original

    return indicator_query


# ---------------------------------------------------------------------------
# Query type detection
# ---------------------------------------------------------------------------

def is_ranking_query(query: str) -> bool:
    """Detect ranking/sorting intent from query phrasing."""
    query_lower = str(query or "").lower()
    return re.search(
        r"\b(rank|ranking|ranked|top(?:\s+\d+)?|highest|lowest|largest|smallest|best|worst)\b",
        query_lower,
    ) is not None


def is_comparison_query(query: str) -> bool:
    """Detect comparison intent from query phrasing."""
    query_lower = str(query or "").lower()
    return re.search(
        r"\b(compare|comparison|versus|vs|between|across|contrast)\b",
        query_lower,
    ) is not None


def is_temporal_split_query(query: str) -> bool:
    """Detect before/after time-split phrasing (for example, 'before and after 2018')."""
    query_lower = str(query or "").lower()
    if "before" not in query_lower or "after" not in query_lower:
        return False
    return bool(_YEAR_RE.search(query_lower))


def extract_top_n_from_query(query: str, default: int = 10) -> int:
    """Extract ranking limit from query text (for example, 'top 10')."""
    query_lower = str(query or "").lower()
    match = _TOP_N_RE.search(query_lower)
    if match:
        try:
            value = int(match.group(1))
            return max(1, min(100, value))
        except ValueError:
            return default
    if any(term in query_lower for term in ("highest", "lowest", "largest", "smallest", "best", "worst")):
        return 1
    return default


def extract_target_year_from_query(query: str) -> Optional[int]:
    """Extract explicit target year from query, if present."""
    query_text = str(query or "")
    years = [int(m) for m in _YEAR_RE.findall(query_text)]
    if not years:
        return None
    # For ranking-like phrasing, the latest stated year is usually intended target.
    return max(years)


# ---------------------------------------------------------------------------
# Distilled indicator query builder
# ---------------------------------------------------------------------------

def build_distilled_indicator_query(svc: Any, query: str) -> str:
    """
    Distill a noisy natural-language query into a stable metric phrase.

    This is used for cross-provider indicator resolution when the original
    phrasing contains ranking/comparison scaffolding.
    """
    query_text = str(query or "").strip()
    if not query_text:
        return ""

    query_lower = query_text.lower()
    cues = extract_indicator_cues(query_lower)
    if not cues:
        return ""

    has_ratio = _has_ratio_cue(query_lower)

    if (
        "trade_openness" in cues
        or "trade openness" in query_lower
        or "exports plus imports" in query_lower
        or "export plus import" in query_lower
    ):
        return "trade openness ratio (exports plus imports to GDP)"
    if "gdp_deflator" in cues:
        return "GDP deflator inflation"
    if "employment_population" in cues:
        return "employment to population ratio"
    # Check unemployment BEFORE employment_rate -- "unemployment rate"
    # produces both cues, and returning "employment rate" would be wrong.
    if "unemployment" in cues:
        return "unemployment rate"
    if "employment_rate" in cues:
        return "employment rate"
    if "producer_price" in cues:
        if "producer price index" in query_lower or "all commodities producer price index" in query_lower:
            return "producer price index"
        return "producer price inflation"
    if "house_prices" in cues:
        return "house price index"
    if "debt_service" in cues:
        return "debt service ratio"
    if "debt_gdp_ratio" in cues or "public_debt" in cues:
        return "government debt (% of GDP)"
    if "bond_yield" in cues:
        if "long-term interest rate" in query_lower or "long term interest rate" in query_lower:
            return "long-term interest rate"
        if "tenor_30y" in cues:
            return "30-year government bond yield"
        if "tenor_10y" in cues:
            return "10-year government bond yield"
        if "tenor_2y" in cues:
            return "2-year government bond yield"
        return "government bond yield"
    if "policy_rate" in cues:
        return "policy rate"
    if "money_supply" in cues:
        if "m1" in query_lower:
            return "M1 money supply"
        if "m2" in query_lower:
            return "M2 money supply"
        if "m3" in query_lower:
            return "M3 money supply"
        return "money supply"
    if "reserves" in cues:
        return "foreign exchange reserves"
    if "current_account" in cues:
        if any(
            term in query_lower
            for term in (
                "primary income",
                "investment income",
                "goods net",
                "repairs on goods",
                "royalties and license fees",
                "insurance and pension services",
                "services credit",
                "current account credit",
            )
        ):
            return query_text
        return "current account balance (% of GDP)"
    if "real_effective_exchange_rate" in cues:
        return "real effective exchange rate"
    if "exchange_rate" in cues:
        return "exchange rate"
    if "trade_balance" in cues:
        if has_ratio:
            return "trade balance (% of GDP)"
        return "trade balance"
    if "import" in cues:
        if has_ratio:
            return "imports as % of GDP"
        return "imports"
    if "export" in cues:
        if has_ratio:
            return "exports as % of GDP"
        return "exports"
    # NOTE: "unemployment" cue is handled earlier (before employment_rate)
    if "hicp" in cues:
        return "HICP inflation"
    if "inflation" in cues:
        if "hicp" in query_lower:
            return "HICP inflation"
        if "cpi" in query_lower:
            return "CPI inflation"
        return "inflation rate"
    if "credit" in cues:
        return "private sector credit to GDP"
    if "savings" in cues:
        return "gross savings (% of GDP)"
    if "gdp" in cues:
        if "growth" in query_lower:
            return "GDP growth"
        if "per capita" in query_lower:
            return "GDP per capita"
        return "GDP"

    return ""
