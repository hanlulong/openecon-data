"""Indicator resolution, plausibility checking, and distilled query building.

Extracted from query.py to reduce file size and isolate indicator resolution
logic into a testable, reusable module.

This module provides:
- Semantic code hints for provider-native indicator codes
- Plausibility checks for resolved indicator codes vs query intent
- Resolution threshold computation (dynamic acceptance levels)
- Disabled provider override shims kept only for compatibility
- Full indicator resolution pipeline (IndicatorSelector -> legacy resolver)
- Distilled indicator query building for cross-provider resolution
- Query type detection (ranking, comparison, temporal split)
- BIS metadata label normalization
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional

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


def _has_ratio_cue(text: str) -> bool:
    """Check if text contains a GDP ratio pattern (e.g., '% of GDP')."""
    return any(pattern in text for pattern in _GDP_RATIO_PATTERNS)

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
        return bool(re.fullmatch(r"[A-Z]{2,}\.[A-Z0-9]{2,}(?:\.[A-Z0-9]{2,}){1,4}", code_upper))
    if provider_upper == "BIS":
        return bool(code_upper.startswith("WS_") or re.fullmatch(r"BIS\.[A-Z0-9_]{3,}", code_upper))
    if provider_upper == "IMF":
        return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_\.]{2,}", code_upper))
    if provider_upper == "FRED":
        return bool(re.fullmatch(r"[A-Z0-9]{3,}", code_upper))
    if provider_upper in {"EUROSTAT", "OECD"}:
        return bool(re.fullmatch(r"[A-Z0-9_@\.]{3,}", code_upper))
    if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
        return bool(re.fullmatch(r"[A-Z0-9_]{3,}", code_upper))
    return False


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
    candidate_params = candidate.get("params")
    if isinstance(candidate_params, dict):
        params.update(candidate_params)
    if provider == "COINGECKO":
        params["coinIds"] = [code]

    countries = list(countries or [])
    if len(countries) == 1:
        params["country"] = countries[0]
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
    min_name_len = 4 if _normalize_provider_name(provider_name) == "COINGECKO" else 24

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

    for candidate in candidates:
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
    min_name_len = 3 if provider_key == "COINGECKO" else 24
    query_country_codes = _extract_country_codes_from_text(query_text)

    best_candidate: Optional[Dict[str, Any]] = None
    best_rank = (-999, -1, -999, -1, -999)
    seen_codes = set()

    def _unit_compatibility_rank(query: str, name: str) -> int:
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

        if query_wants_count and name_is_count and not name_is_ratio:
            return 2
        if query_wants_ratio and name_is_ratio and not name_is_count:
            return 2
        if query_wants_count and name_is_ratio and not name_is_count:
            return -2
        if query_wants_ratio and name_is_count and not name_is_ratio:
            return -2
        return 0

    candidates_by_input: list[tuple[str, dict[str, Any]]] = []
    exact_candidates_found = False
    try:
        exact_name_matches = getattr(lookup, "exact_name_matches", None)
        if callable(exact_name_matches):
            for candidate in exact_name_matches(search_inputs, provider=provider_name, limit=20):
                candidates_by_input.append((str(candidate.get("name") or ""), candidate))
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
                for alias in sorted(CountryResolver.COUNTRY_ALIASES.keys(), key=len, reverse=True):
                    cleaned_search = re.sub(
                        rf"^(?:{re.escape(str(alias).strip())})\s+",
                        " ",
                        cleaned_search,
                        flags=re.IGNORECASE,
                    )
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

    for search_text, candidate in candidates_by_input:
        code = str(candidate.get("code") or "")
        if code in seen_codes:
            continue
        seen_codes.add(code)
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
            unit_rank = _unit_compatibility_rank(normalized_text, normalized_name)
            shared_tokens = len(set(normalized_name.split()) & set(normalized_text.split()))
            rank = (unit_rank, country_rank, -token_delta, shared_tokens, -name_token_len)
            if rank > best_rank:
                best_candidate = candidate
                best_rank = rank
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

        normalized_punctuation = re.sub(r"[,:;()\[\]%/]+", " ", candidate)
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

        # Strip a leading country alias only when it appears as a plain prefix.
        for alias in sorted(CountryResolver.COUNTRY_ALIASES.keys(), key=len, reverse=True):
            stripped_country = re.sub(
                rf"^(?:{re.escape(str(alias).strip())})\s+",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip()
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

        if provider_key == "IMF":
            without_definition = re.sub(r"\bdefinition\b", " ", candidate, flags=re.IGNORECASE)
            without_definition = re.sub(r"\s+", " ", without_definition).strip(" ,;:-")
            if without_definition and without_definition != candidate and without_definition not in seen:
                queue.append(without_definition)

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


def _extract_country_codes_from_text(text: str) -> set[str]:
    """Extract ISO country codes from free text using alias matching."""
    query_text = str(text or "").strip().lower()
    if not query_text:
        return set()

    codes: set[str] = set()
    for alias in sorted(CountryResolver.COUNTRY_ALIASES.keys(), key=len, reverse=True):
        alias_text = str(alias).strip()
        if not alias_text:
            continue
        if re.search(
            rf"(?<![a-z0-9]){re.escape(alias_text)}(?![a-z0-9])",
            query_text,
            flags=re.IGNORECASE,
        ):
            normalized = CountryResolver.normalize(alias_text)
            if normalized:
                codes.add(normalized)
    return codes


def _country_reordered_exact_title_variants(candidate: str) -> list[str]:
    """Generate country-aware search variants for provider-native titles."""
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return []

    variants: list[str] = []
    for alias in sorted(CountryResolver.COUNTRY_ALIASES.keys(), key=len, reverse=True):
        alias_text = str(alias).strip()
        if not alias_text:
            continue

        suffix_match = re.match(
            rf"^(?P<head>.+?)\s+for\s+(?P<country>{re.escape(alias_text)})$",
            candidate_text,
            flags=re.IGNORECASE,
        )
        if suffix_match:
            head = suffix_match.group("head").strip(" ,;:-")
            country = suffix_match.group("country").strip()
            if head and country:
                variants.append(f"{country} {head}".strip())

        prefixed_match = re.match(
            rf"^(?P<country>{re.escape(alias_text)})\s*[-,:]\s*(?P<head>.+)$",
            candidate_text,
            flags=re.IGNORECASE,
        )
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
    *,
    _get_indicator_resolver: Any = None,
) -> dict:
    """Resolve and validate the indicator code for a fetch operation.

    Uses IndicatorSelector (embed -> LLM pick) as primary resolution,
    falling back to the legacy IndicatorResolver if embeddings are unavailable.

    Mutates intent.parameters and potentially intent.indicators (for
    WorldBank multi-indicator collapse). Returns the updated params dict.
    """
    if _get_indicator_resolver is None:
        from ..services.indicator_resolver import get_indicator_resolver as _get_indicator_resolver

    if provider not in {"STATSCAN", "STATISTICS CANADA", "FRED", "IMF", "WORLDBANK", "EUROSTAT", "OECD", "BIS"}:
        return params

    resolver = None

    def _resolver() -> Any:
        nonlocal resolver
        if resolver is None:
            resolver = _get_indicator_resolver()
        return resolver

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

    existing_indicator = str(params.get("indicator") or "").strip()
    if provider == "IMF" and not existing_indicator and len(intent.indicators or []) == 1:
        candidate_indicator = str((intent.indicators or [""])[0] or "").strip()
        catalog_exact_match = False
        if candidate_indicator and svc._looks_like_provider_indicator_code(provider, candidate_indicator):
            try:
                from .indicator_database import get_indicator_lookup

                catalog_exact_match = bool(get_indicator_lookup().get(provider, candidate_indicator.upper()))
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
            )
            intent.parameters = params
            existing_indicator = candidate_indicator

    has_explicit_code = bool(
        existing_indicator
        and svc._looks_like_provider_indicator_code(provider, existing_indicator)
    )
    exact_match_locked = is_exact_match_locked(params)

    # Path 1: Validate explicit code against query context
    if has_explicit_code and exact_match_locked:
        logger.info(
            "🔒 Keeping exact %s indicator code without plausibility override: %s",
            provider,
            existing_indicator,
        )
        params = _apply_indicator_with_semantic_label(existing_indicator)
        intent.parameters = params
        return params

    if has_explicit_code:
        plausibility_query = select_indicator_query_for_resolution(svc, intent)
        if not plausibility_query:
            plausibility_query = _effective_original_query(intent)
        if not plausibility_query:
            plausibility_query = str(intent.indicators[0] if intent.indicators else existing_indicator)

        if plausibility_query and not is_resolved_indicator_plausible(
            svc=svc,
            provider=provider,
            indicator_query=plausibility_query,
            resolved_code=existing_indicator,
        ):
            logger.info(
                "🔎 Explicit %s indicator '%s' conflicts with query context '%s'; attempting dynamic resolution",
                provider,
                existing_indicator,
                plausibility_query,
            )
            has_explicit_code = False

    if has_explicit_code:
        semantic_query = (
            _effective_original_query(intent)
            or str(params.get("__semantic_indicator_label") or "").strip()
            or str(intent.indicators[0] if intent.indicators else "")
        )
        semantic_label = str(params.get("__semantic_indicator_label") or "").strip()
        if is_resolved_indicator_plausible(
            svc=svc,
            provider=provider,
            indicator_query=semantic_query,
            resolved_code=existing_indicator,
            resolved_name=semantic_label,
        ):
            logger.info(
                "🔒 Keeping explicit %s indicator code: %s",
                provider,
                existing_indicator,
            )
            params = _apply_indicator_with_semantic_label(existing_indicator)
            intent.parameters = params
            return params
        logger.info(
            "🚫 Explicit %s indicator code rejected as implausible for query: %s -> %s",
            provider,
            semantic_query,
            existing_indicator,
        )
        has_explicit_code = False

    # Dynamic resolution (IndicatorSelector -> resolver -> raw query)
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
    else:
        # For follow-ups, use resolvedQuery (e.g. "GDP per capita India")
        # rather than the raw follow-up text (e.g. "show from IMF instead").
        selector_query = (original_selector_query or indicator_query or "").strip()
    exact_title_query = (original_selector_query or selector_query).strip()
    if provider and looks_like_exact_provider_title_match(exact_title_query, provider):
        logger.info(
            "🎯 Skipping IndicatorSelector for exact %s title match: %s",
            provider,
            exact_title_query,
        )
    else:
        try:
            from .indicator_selector import IndicatorSelector
            selector = IndicatorSelector()
            selection = await selector.select(selector_query, provider)
            if selection.code:
                selection_name = str(getattr(selection, "name", "") or "")
                if not is_resolved_indicator_plausible(
                    svc=svc,
                    provider=provider,
                    indicator_query=_effective_original_query(intent) or indicator_query,
                    resolved_code=selection.code,
                    resolved_name=selection_name,
                ):
                    logger.info(
                        "🚫 IndicatorSelector candidate rejected as implausible: '%s' → %s (%s)",
                        indicator_query,
                        selection.code,
                        selection_name or "<missing-name>",
                    )
                else:
                    logger.info(
                        "🎯 IndicatorSelector resolved: '%s' → %s [%s]",
                        indicator_query, selection.code, selection.source,
                    )
                    params = _apply_indicator_with_semantic_label(selection.code)
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
                # Don't return -- fall through to legacy resolver as backup
        except Exception as e:
            logger.debug("IndicatorSelector unavailable, using legacy resolver: %s", e)

    # Path 2: Legacy IndicatorResolver (catalog + database FTS + vector search)
    resolved = _resolver().resolve(
        indicator_query,
        provider=provider,
        country=country_context,
        countries=countries_context,
    )

    # Evaluate resolver result
    accepted_resolved = False
    if resolved:
        threshold = indicator_resolution_threshold(
            indicator_query=indicator_query,
            resolved_source=resolved.source,
        )
        relevance_threshold = minimum_resolved_relevance_threshold(
            indicator_query,
        )
        resolved_relevance = score_resolved_indicator_relevance(
            svc=svc,
            indicator_query=indicator_query,
            provider=provider,
            resolved=resolved,
        )
        accepted_resolved = resolved.confidence >= threshold
        if accepted_resolved and not is_resolved_indicator_plausible(
            svc=svc,
            provider=provider,
            indicator_query=indicator_query,
            resolved_code=resolved.code,
            resolved_name=" ".join(
                part
                for part in [
                    str(getattr(resolved, "name", "") or ""),
                    str((getattr(resolved, "metadata", None) or {}).get("indicator", "") or ""),
                    str((getattr(resolved, "metadata", None) or {}).get("description", "") or ""),
                ]
                if part
            ),
        ):
            accepted_resolved = False
        if accepted_resolved and resolved_relevance < relevance_threshold:
            accepted_resolved = False
        logger.info(
            (
                "🔍 IndicatorResolver candidate: '%s' → '%s' "
                "(conf=%.2f, src=%s, threshold=%.2f, relevance=%.2f, min_relevance=%.2f, accepted=%s)"
            ),
            indicator_query,
            resolved.code,
            resolved.confidence,
            resolved.source,
            threshold,
            resolved_relevance,
            relevance_threshold,
            accepted_resolved,
        )

    # Apply best result or fall back to raw query
    if accepted_resolved and resolved:
        params = _apply_indicator_with_semantic_label(resolved.code)
        if provider in {"WORLDBANK", "WORLD BANK"} and selected_query_override and len(intent.indicators) > 1:
            logger.info(
                "🔎 Collapsing World Bank multi-indicator intent to resolved indicator '%s' after semantic override",
                resolved.code,
            )
            intent.indicators = [resolved.code]
    else:
        params = _apply_indicator_with_semantic_label(indicator_query)

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
            # selection.  Other providers still prefer the full original query
            # because prior semantic labels can be polluted by fallback state.
            if _normalize_provider_name(intent.apiProvider or "") in {"STATSCAN", "STATISTICS CANADA"}:
                return semantic_indicator_label or distilled_original or original_query
            return original_query or distilled_original or semantic_indicator_label
        return semantic_indicator_label or distilled_original or original_query

    # If the indicator looks like a provider-specific code, never use it
    # for cross-provider resolution -- prefer the original query text.
    provider = _normalize_provider_name(intent.apiProvider or "")
    if provider and svc._looks_like_provider_indicator_code(provider, indicator_query):
        logger.info(
            "🔎 Indicator '%s' looks like a %s-specific code. Using original query for resolution.",
            indicator_query,
            provider,
        )
        return _fallback_to_original_or_distilled()

    if provider and looks_like_exact_provider_title_match(original_query, provider):
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
