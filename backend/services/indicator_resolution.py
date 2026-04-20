"""Indicator resolution, plausibility checking, and distilled query building.

Extracted from query.py to reduce file size and isolate indicator resolution
logic into a testable, reusable module.

This module provides:
- Semantic code hints for provider-native indicator codes
- Plausibility checks for resolved indicator codes vs query intent
- Resolution threshold computation (dynamic acceptance levels)
- Catalog-based provider override and availability routing
- Full indicator resolution pipeline (IndicatorSelector -> legacy resolver -> translator)
- Distilled indicator query building for cross-provider resolution
- Query type detection (ranking, comparison, temporal split)
- BIS metadata label normalization
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

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
    for search_text in search_inputs:
        try:
            for candidate in lookup.search(search_text, provider=provider_name, limit=5):
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
    best_rank = (-1, -1)
    seen_codes = set()
    for search_text in search_inputs:
        try:
            results = lookup.search(search_text, provider=provider_name, limit=5)
        except Exception:
            continue
        for candidate in results:
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
                for normalized_query in (
                    re.sub(r"[^a-z0-9]+", " ", candidate_query.lower()).strip()
                    for candidate_query in search_inputs
                )
                if normalized_query
            ):
                candidate_country_codes = _extract_country_codes_from_text(candidate_name)
                country_rank = len(query_country_codes & candidate_country_codes)
                rank = (country_rank, len(normalized_name))
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

        normalized_punctuation = re.sub(r"[,:()\[\]%]+", " ", candidate)
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
    """
    Re-route provider using catalog concept availability from the original query.

    This prevents semantically impossible provider/concept combinations from
    continuing into indicator resolution (e.g., public debt ratio on BIS).
    """
    # For explicit provider detection, use the raw query text (user may have
    # said "from IMF"). For concept search, use the effective query which
    # is resolvedQuery for follow-ups (e.g. "GDP per capita India").
    raw_query = str(intent.originalQuery or "").strip()
    original_query = _effective_original_query(intent)
    explicit_provider_requested = _normalize_provider_name(
        svc._detect_explicit_provider(raw_query) or ""
    )
    # When the user explicitly requested this provider (e.g., "from IMF"),
    # we still need to do the concept lookup and inject the canonical
    # indicator code (e.g., GDP growth rate → NGDP_RPCH).  We just must
    # NOT re-route to a different provider.
    explicit_provider_locked = bool(
        explicit_provider_requested and explicit_provider_requested == provider
    )
    if params.get("__semantic_provider_locked"):
        explicit_provider_locked = True

    blocked_override_providers = {
        _normalize_provider_name(str(candidate))
        for candidate in (params.get("__fallback_excluded_providers") or [])
        if candidate
    }
    blocked_override_providers.discard("")
    in_fallback_context = bool(blocked_override_providers)

    distilled_query = build_distilled_indicator_query(svc, original_query) if original_query else ""
    concept_candidates: List[str] = []
    if original_query:
        concept_candidates.append(original_query)

    if distilled_query and distilled_query not in concept_candidates:
        concept_candidates.append(distilled_query)

    fallback_query = select_indicator_query_for_resolution(svc, intent)
    if fallback_query and fallback_query not in concept_candidates:
        concept_candidates.append(fallback_query)

    if not concept_candidates:
        return provider, params

    if provider and looks_like_exact_provider_title_match(original_query, provider):
        logger.info(
            "📋 Skipping concept/provider override for exact %s title match: %s",
            provider,
            original_query,
        )
        return provider, params

    def _apply_indicator_with_semantic_label(indicator_value: str, **extra: Any) -> dict:
        semantic_label = str(params.get("__semantic_indicator_label") or "").strip()
        if not semantic_label:
            semantic_label = (
                select_indicator_query_for_resolution(svc, intent)
                or _effective_original_query(intent)
                or str(intent.indicators[0] if intent.indicators else "")
            ).strip()

        merged = {**params, "indicator": indicator_value, **extra}
        if semantic_label and not svc._looks_like_provider_indicator_code(provider, semantic_label):
            merged["__semantic_indicator_label"] = semantic_label
        return merged

    try:
        from .catalog_service import (
            find_concept_by_term,
            get_best_provider,
            get_variant_for_query,
            is_indicator_code_for_concept,
            is_provider_available,
            is_provider_explicitly_excluded,
        )

        concept_name = None
        matched_query = ""
        matched_candidates: List[tuple[str, str]] = []
        for candidate in concept_candidates:
            matched_concept = find_concept_by_term(candidate)
            if matched_concept:
                matched_candidates.append((matched_concept, candidate))
        if matched_candidates:
            concept_name, matched_query = max(
                matched_candidates,
                key=lambda item: (
                    len(str(item[0]).split("_")),
                    len(str(item[1]).strip()),
                ),
            )
        if not concept_name:
            return provider, params

        query_lower = original_query.lower()
        bilateral_trade_request = False
        if provider == "COMTRADE":
            router = getattr(svc, "unified_router", None)
            if router is not None and original_query:
                try:
                    bilateral_trade_request = bool(
                        router._is_bilateral_trade_query(query_lower, original_query)
                    )
                except Exception:
                    bilateral_trade_request = False
            if not bilateral_trade_request:
                bilateral_trade_request = bool(params.get("partner") or params.get("reporter"))
            if bilateral_trade_request and concept_name in {"trade", "exports", "imports", "trade_balance"}:
                logger.info(
                    "📋 Concept override skipped: preserving COMTRADE for bilateral trade concept '%s'",
                    concept_name,
                )
                return provider, params

        # Large-catalog country-specific providers (e.g. StatsCan with 40K+
        # tables) often have data the catalog hasn't mapped yet.  When the
        # concept doesn't *explicitly* exclude the current provider (i.e. it's
        # not in ``not_available``), keep the provider and let its FTS5 /
        # embedding indicator resolver find the right table -- don't reroute
        # to a global provider just because the catalog entry is incomplete.
        _LARGE_CATALOG_COUNTRY_PROVIDERS = {"STATSCAN"}
        if (
            provider in _LARGE_CATALOG_COUNTRY_PROVIDERS
            and not is_provider_explicitly_excluded(concept_name, provider)
            and not is_provider_available(concept_name, provider)
        ):
            logger.info(
                "📋 Concept override skipped: %s not explicitly excluded for '%s' "
                "(catalog incomplete, letting provider resolver handle it)",
                provider,
                concept_name,
            )
            return provider, params

        countries_ctx = params.get("countries") if isinstance(params.get("countries"), list) else None
        if not countries_ctx:
            country_value = params.get("country") or params.get("region")
            countries_ctx = [country_value] if country_value else None
        coverage_ok = svc._provider_covers_country_list(provider, countries_ctx or [])

        # Evaluate current provider suitability (availability + coverage + confidence)
        preferred_provider, preferred_code, preferred_confidence = get_best_provider(
            concept_name,
            countries_ctx,
            preferred_provider=provider,
        )
        preferred_provider_normalized = _normalize_provider_name(preferred_provider or "")
        provider_supported = (
            bool(preferred_provider_normalized)
            and preferred_provider_normalized == provider
        )
        if not coverage_ok:
            provider_supported = False
            preferred_confidence = max(0.0, float(preferred_confidence or 0.0) - 0.25)

        best_provider, best_code, best_confidence = get_best_provider(concept_name, countries_ctx)
        best_provider_normalized = _normalize_provider_name(best_provider or "")
        requested_country_count = len(countries_ctx or [])
        broad_global_providers = {"WORLDBANK", "IMF"}
        niche_coverage_providers = {"OECD", "EUROSTAT", "FRED", "STATSCAN"}

        # Keep existing provider when:
        # 1) it is available and suitable for requested coverage, and
        # 2) no clearly better provider exists.
        if provider_supported:
            confidence_gain = float(best_confidence or 0.0) - float(preferred_confidence or 0.0)
            low_confidence_current = float(preferred_confidence or 0.0) < 0.80
            if (
                requested_country_count >= 2
                and provider in broad_global_providers
                and best_provider_normalized in niche_coverage_providers
                and confidence_gain < (0.18 if low_confidence_current else 0.28)
            ):
                return provider, params
            current_indicator = params.get("indicator")
            current_indicator_is_code = (
                bool(current_indicator)
                and svc._looks_like_provider_indicator_code(provider, str(current_indicator))
            )
            canonical_code = preferred_code if provider_supported else best_code
            # Some provider mnemonics (e.g. OECD "GDP", "IRLT") look code-like
            # but are still coarse aliases. If the catalog has a more specific
            # canonical code for the same provider, do not let the short alias
            # block canonical code injection.
            current_indicator_text = str(current_indicator or "")
            if (
                provider == "OECD"
                and canonical_code
                and current_indicator
                and current_indicator_is_code
                and canonical_code != current_indicator_text
                and "@" not in current_indicator_text
                and not current_indicator_text.startswith("DSD_")
                and not any(separator in current_indicator_text for separator in ("_", ".", "-"))
                and len(current_indicator_text) <= 8
            ):
                # Only bare mnemonic aliases like GDP/IRLT/CPI should be
                # auto-upgraded to a canonical OECD dataflow here. Structured
                # OECD codes with separators (for example LFS_UNEM_A) already
                # encode meaningful scope and should survive as explicit
                # provider-native inputs.
                current_indicator_is_code = False
            current_indicator_matches_concept = False
            current_indicator_plausible = True
            current_indicator_text_upper = current_indicator_text.upper()
            if current_indicator and current_indicator_is_code:
                try:
                    current_indicator_matches_concept = is_indicator_code_for_concept(
                        concept_name,
                        provider,
                        current_indicator_text,
                    )
                except Exception:
                    current_indicator_matches_concept = False

                try:
                    from .indicator_resolver import get_indicator_resolver

                    _resolver = get_indicator_resolver()
                    current_meta = _resolver.lookup.get(provider, current_indicator_text) if provider else None
                    current_name = (current_meta.get("name", "") if current_meta else "")
                    current_indicator_plausible = is_resolved_indicator_plausible(
                        svc=svc,
                        provider=provider,
                        indicator_query=original_query or matched_query or current_indicator_text,
                        resolved_code=current_indicator_text,
                        resolved_name=current_name,
                    )
                except Exception:
                    current_indicator_plausible = True

            should_replace_existing_code = bool(
                canonical_code
                and current_indicator
                and current_indicator_is_code
                and current_indicator_text_upper != str(canonical_code).upper()
                and (not current_indicator_matches_concept and not current_indicator_plausible)
            )

            if canonical_code and (not current_indicator or not current_indicator_is_code or should_replace_existing_code):
                # Unified semantic verification -- single source of truth
                from .indicator_resolver import get_indicator_resolver
                _resolver = get_indicator_resolver()
                code_meta = _resolver.lookup.get(provider, canonical_code) if provider else None
                code_name = (code_meta.get("name", "") if code_meta else "")

                # Cycle 27 fix: Frequency-aware variant override.
                # If user explicitly requested a frequency (monthly/quarterly/etc.)
                # and the canonical code's frequency doesn't match, try a catalog
                # variant that does.  This is a general fix using existing catalog
                # variant infrastructure.
                _query_lower = (original_query or "").lower()
                _freq_hints = {
                    "monthly": "monthly", "month": "monthly",
                    "quarterly": "quarterly", "quarter": "quarterly",
                    "weekly": "weekly", "week": "weekly",
                    "daily": "daily", "day": "daily",
                }
                _requested_freq = None
                for hint, freq in _freq_hints.items():
                    if hint in _query_lower:
                        _requested_freq = freq
                        break
                if _requested_freq:
                    code_freq = (code_meta.get("frequency", "") if code_meta else "").lower()
                    if code_freq and code_freq != _requested_freq:
                        # Try variant lookup for frequency match
                        variant_code, _ = get_variant_for_query(
                            concept_name, provider, original_query, countries_ctx,
                        )
                        if variant_code and variant_code != canonical_code:
                            var_meta = _resolver.lookup.get(provider, variant_code) if provider else None
                            var_freq = (var_meta.get("frequency", "") if var_meta else "").lower()
                            if var_freq == _requested_freq:
                                logger.info(
                                    "📋 Frequency variant override: %s (%s) → %s (%s) for query freq=%s",
                                    canonical_code, code_freq, variant_code, var_freq, _requested_freq,
                                )
                                canonical_code = variant_code
                                code_meta = var_meta
                                code_name = (var_meta.get("name", "") if var_meta else "")

                # Cycle 29: ALWAYS try variant lookup if query has variant
                # discriminator keywords that don't appear in the canonical code's
                # name.  This catches cases like "core CPI" where the catalog
                # matches "inflation" → CPIAUCSL but the user wants the "core"
                # variant CPILFESL.  Skipping the discriminator check is fine for
                # confidence reasons, but variant lookup uses the catalog's own
                # explicit variant definitions, not heuristics.
                _query_lower_var = (original_query or "").lower()
                _variant_keywords = (
                    "core", "headline", "ppp", "constant", "real", "nominal",
                    "growth", "per capita", "pce",
                    "monthly", "quarterly", "weekly", "daily",
                    "brent", "wti", "west texas intermediate",
                )
                _has_variant_hint = any(
                    kw in _query_lower_var for kw in _variant_keywords
                )
                _name_lower_var = (code_name or "").lower()
                if _has_variant_hint:
                    # Check if any variant keyword in query is missing from
                    # the primary's name — if so, try variant lookup
                    _missing_in_primary = [
                        kw for kw in _variant_keywords
                        if kw in _query_lower_var and kw not in _name_lower_var
                    ]
                    if _missing_in_primary:
                        variant_code, _ = get_variant_for_query(
                            concept_name, provider, original_query, countries_ctx,
                        )
                        if variant_code and variant_code != canonical_code:
                            var_meta = _resolver.lookup.get(provider, variant_code) if provider else None
                            var_name = (var_meta.get("name", "") if var_meta else "")
                            logger.info(
                                "📋 Variant override (cycle 29): %s → %s for query "
                                "'%s' (missing keywords: %s)",
                                canonical_code, variant_code,
                                original_query[:50],
                                _missing_in_primary,
                            )
                            canonical_code = variant_code
                            code_meta = var_meta
                            code_name = var_name

                # High-confidence catalog matches (>= 0.90) are authoritative —
                # the concept itself already encodes the query's semantic intent
                # (e.g. gdp_growth concept already implies "growth").  Discriminator
                # checks should NOT override the catalog in these cases.
                _skip_discriminator = float(preferred_confidence or 0.0) >= 0.90
                if _skip_discriminator:
                    logger.debug(
                        "📋 Skipping discriminator check for high-confidence catalog match "
                        "'%s' (confidence=%.2f, concept=%s)",
                        canonical_code, float(preferred_confidence or 0.0), concept_name,
                    )
                if not _skip_discriminator and not svc._verify_semantic_discriminators(original_query, canonical_code, code_name):
                    # Primary variant doesn't match query discriminators (e.g.,
                    # user asked for "GDP per capita PPP" but primary is current US$).
                    # Try named catalog variants (ppp, growth, constant, etc.)
                    # BEFORE falling through to expensive IndicatorSelector/metadata search.
                    variant_code, variant_conf = get_variant_for_query(
                        concept_name, provider, original_query, countries_ctx,
                    )
                    if variant_code:
                        # Verify the variant passes semantic discriminators
                        var_meta = _resolver.lookup.get(provider, variant_code) if provider else None
                        var_name = (var_meta.get("name", "") if var_meta else "")
                        if svc._verify_semantic_discriminators(original_query, variant_code, var_name):
                            logger.info(
                                "📋 Catalog variant resolved: %s → %s for %s (concept=%s)",
                                canonical_code, variant_code, provider, concept_name,
                            )
                            params = _apply_indicator_with_semantic_label(
                                variant_code,
                                __catalog_resolved=True,
                                __catalog_concept=concept_name,
                            )
                            intent.parameters = params
                            if not intent.indicators or len(intent.indicators) <= 1:
                                intent.indicators = [variant_code]
                            return provider, params
                        else:
                            logger.info(
                                "📋 Catalog variant %s also failed discriminator check, continuing",
                                variant_code,
                            )

                    # No matching variant found — try provider-agnostic resolution
                    # but only if the user didn't explicitly lock this provider
                    if not explicit_provider_locked:
                        alt_provider, alt_code, alt_conf = get_best_provider(
                            concept_name, countries_ctx
                        )
                        alt_normalized = _normalize_provider_name(alt_provider or "")
                        if alt_normalized and alt_normalized != provider and alt_code:
                            alt_meta = _resolver.lookup.get(alt_normalized, alt_code) if alt_normalized else None
                            alt_name = (alt_meta.get("name", "") if alt_meta else "").lower()
                            # Check discriminators from the original query
                            disc_set = getattr(_resolver, '_semantic_discriminators', set())
                            query_discs = {d for d in disc_set if d in original_query.lower()}
                            from ..services.indicator_clarification import find_missing_discriminators as _find_missing
                            alt_missing = _find_missing(
                                query_discs, alt_name, alt_code.lower(),
                            )
                            if not alt_missing:
                                logger.info(
                                    "🔄 Switching provider %s → %s (code %s matches discriminators better)",
                                    provider, alt_normalized, alt_code,
                                )
                                params = {**params, "indicator": alt_code}
                                intent.parameters = params
                                intent.indicators = [alt_code]
                                intent.apiProvider = alt_normalized
                                return alt_normalized, params
                    return provider, params

                params = _apply_indicator_with_semantic_label(
                    canonical_code,
                    __catalog_resolved=True,
                    __catalog_concept=concept_name,
                )
                intent.parameters = params
                distinct_indicators = {str(value) for value in (intent.indicators or []) if value}
                if not distinct_indicators or len(distinct_indicators) == 1:
                    intent.indicators = [canonical_code]
                logger.info(
                    "📋 Concept code override: %s -> %s for provider %s (concept=%s)",
                    matched_query,
                    canonical_code,
                    provider,
                    concept_name,
                )

            if (
                not best_provider_normalized
                or best_provider_normalized == provider
                or best_confidence < (preferred_confidence + 0.08)
            ):
                return provider, params
        else:
            # If catalog still says provider is available but suitability could not be confirmed,
            # only override when we have a concrete better provider.
            if coverage_ok and is_provider_available(concept_name, provider) and (
                not best_provider_normalized or best_provider_normalized == provider
            ):
                return provider, params

        # When the user explicitly requested this provider, never re-route
        # to a different one.  Code injection (if any) already happened above.
        if explicit_provider_locked:
            return provider, params

        if (
            in_fallback_context
            and best_provider_normalized
            and best_provider_normalized != provider
        ):
            logger.info(
                "📋 Concept override skipped in fallback context: keeping %s instead of rerouting to %s",
                provider,
                best_provider_normalized,
            )
            return provider, params

        alt_provider = best_provider
        alt_code = best_code
        alt_provider_normalized = best_provider_normalized
        if not alt_provider_normalized or alt_provider_normalized == provider:
            return provider, params
        if alt_provider_normalized in blocked_override_providers:
            logger.info(
                "📋 Concept override skipped: candidate provider %s is blocked in this fallback context",
                alt_provider_normalized,
            )
            return provider, params

        logger.info(
            "📋 Concept override: rerouting '%s' from %s to %s (best_conf=%.2f, current_conf=%.2f, coverage_ok=%s)",
            concept_name,
            provider,
            alt_provider_normalized,
            best_confidence,
            preferred_confidence,
            provider_supported,
        )
        provider = alt_provider_normalized
        intent.apiProvider = alt_provider or provider

        if alt_code:
            params = _apply_indicator_with_semantic_label(
                alt_code,
                __catalog_resolved=True,
                __catalog_concept=concept_name,
            )
            intent.parameters = params
            if not intent.indicators or len(intent.indicators) <= 1:
                intent.indicators = [alt_code]
            logger.info(
                "📋 Concept override indicator: %s -> %s",
                matched_query,
                alt_code,
            )
    except Exception as exc:
        logger.debug("Concept provider override skipped: %s", exc)

    return provider, params


# ---------------------------------------------------------------------------
# Catalog availability override
# ---------------------------------------------------------------------------

def apply_catalog_availability_override(
    svc: Any,
    provider: str,
    intent: ParsedIntent,
    params: dict,
    fallback_excluded_providers: set,
) -> tuple[str, dict]:
    """Check catalog availability and re-route to a better provider if needed.

    If the current provider is in the not_available list for the resolved
    indicator, proactively switches to the best alternative provider.
    Respects explicit provider requests from the user.

    Returns (provider, params) -- both may be updated.
    """
    indicator_term = (params.get("indicator") or (intent.indicators[0] if intent.indicators else ""))
    logger.info(f"📋 Catalog check: indicator='{indicator_term}', provider='{provider}'")

    original_query = intent.originalQuery or ""
    explicit_provider_requested = _normalize_provider_name(svc._detect_explicit_provider(original_query) or "")
    if params.get("__semantic_provider_locked"):
        logger.info(f"📋 Skipping catalog override - semantic clarification locked {provider}")
        return provider, params

    if explicit_provider_requested and explicit_provider_requested == provider:
        logger.info(f"📋 Skipping catalog override - user explicitly requested {provider}")
        return provider, params

    if not indicator_term or not provider:
        return provider, params

    try:
        from .catalog_service import find_concept_by_term, get_best_provider, is_provider_available
        concept = find_concept_by_term(indicator_term)
        logger.info(f"📋 Catalog concept: '{concept}' for term '{indicator_term}'")
        bilateral_trade_request = False
        if provider == "COMTRADE":
            router = getattr(svc, "unified_router", None)
            if router is not None and original_query:
                try:
                    bilateral_trade_request = bool(
                        router._is_bilateral_trade_query(original_query.lower(), original_query)
                    )
                except Exception:
                    bilateral_trade_request = False
            if not bilateral_trade_request:
                bilateral_trade_request = bool(params.get("reporter") or params.get("partner"))
            if bilateral_trade_request and concept in {"trade", "exports", "imports", "trade_balance"}:
                logger.info(
                    "📋 Catalog availability override skipped: preserving COMTRADE for bilateral trade concept '%s'",
                    concept,
                )
                return provider, params
        if concept and not is_provider_available(concept, provider):
            countries_ctx = params.get("countries") if isinstance(params.get("countries"), list) else None
            if not countries_ctx:
                country = params.get("country") or params.get("region")
                countries_ctx = [country] if country else None

            alt_provider, alt_code, _ = get_best_provider(concept, countries_ctx)
            if alt_provider and alt_provider.upper() != provider:
                alt_provider_normalized = _normalize_provider_name(alt_provider)
                if (
                    fallback_excluded_providers
                    and alt_provider_normalized
                    and alt_provider_normalized != provider
                ):
                    logger.info(
                        "📋 Catalog reroute skipped in fallback context: keeping %s instead of %s",
                        provider,
                        alt_provider_normalized,
                    )
                elif alt_provider_normalized in fallback_excluded_providers:
                    logger.info(
                        "📋 Catalog reroute skipped: candidate provider %s is blocked in this fallback context",
                        alt_provider_normalized,
                    )
                else:
                    logger.info(
                        "📋 Catalog: %s not available for '%s', routing to %s",
                        provider,
                        indicator_term,
                        alt_provider,
                    )
                    intent.apiProvider = alt_provider
                    provider = alt_provider_normalized

                    if alt_code:
                        params = {**params, "indicator": alt_code}
                        intent.parameters = params
                        if not intent.indicators or len(intent.indicators) == 1:
                            intent.indicators = [alt_code]
                        logger.info(
                            "📋 Catalog remapped indicator for %s: %s -> %s",
                            provider,
                            indicator_term,
                            alt_code,
                        )
    except Exception as e:
        logger.warning(f"Catalog availability check failed: {e}")

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
    resolver = _get_indicator_resolver()

    if provider not in {"STATSCAN", "STATISTICS CANADA", "FRED", "IMF", "WORLDBANK", "EUROSTAT", "OECD", "BIS"}:
        return params

    def _apply_indicator_with_semantic_label(indicator_value: str, **extra: Any) -> dict:
        semantic_label = str(params.get("__semantic_indicator_label") or "").strip()
        if not semantic_label:
            semantic_label = (
                select_indicator_query_for_resolution(svc, intent)
                or _effective_original_query(intent)
                or str(intent.indicators[0] if intent.indicators else "")
            ).strip()

        merged = {**params, "indicator": indicator_value, **extra}
        if semantic_label and not svc._looks_like_provider_indicator_code(provider, semantic_label):
            merged["__semantic_indicator_label"] = semantic_label
        return merged

    # --- StatsCan fast-path: if intent.indicators match a known vector or coordinate
    # mapping, lock the indicator immediately and skip the IndicatorSelector.
    if provider in {"STATSCAN", "STATISTICS CANADA"}:
        _sc = svc.statscan_provider
        for hr_indicator in (intent.indicators or []):
            hr_key = hr_indicator.upper().replace(" ", "_").replace("-", "_")
            if hr_key in _sc.VECTOR_MAPPINGS or hr_key in _sc.COORDINATE_PRODUCT_MAPPINGS:
                logger.info(
                    "StatsCan fast-path: intent indicator '%s' matches known mapping '%s'; "
                    "skipping IndicatorSelector to use verified vector/coordinate",
                    hr_indicator, hr_key,
                )
                params = _apply_indicator_with_semantic_label(
                    hr_key,
                    __catalog_resolved=True,
                )
                intent.parameters = params
                return params

    existing_indicator = str(params.get("indicator") or "").strip()
    has_explicit_code = bool(
        existing_indicator
        and svc._looks_like_provider_indicator_code(provider, existing_indicator)
    )
    exact_match_locked = bool(
        params.get("__exact_provider_code_match") or params.get("__exact_indicator_title_match")
    )

    # Qualifier-aware indicator recovery: when the LLM strips qualifiers
    # ("GDP growth G7" -> indicators=["GDP"]), recover the full indicator
    # from the original query before any resolution path.
    # SKIP if indicator is already a provider-specific code (e.g., SP.POP.GROW,
    # NY.GDP.MKTP.KD.ZG) -- these are already resolved and should not be augmented.
    if not params.get("__qualifier_checked"):
        original_q = _effective_original_query(intent).lower()
        # Use the indicator TEXT (from LLM or intent) not the resolved CODE
        indicator_text = ""
        if intent.indicators:
            indicator_text = str(intent.indicators[0] or "").strip().lower()
        if not indicator_text:
            indicator_text = existing_indicator.lower() if existing_indicator else ""
        # Skip qualifier recovery if indicator is already a provider code
        _skip_qualifier = svc._looks_like_provider_indicator_code(
            provider, indicator_text.upper()
        ) if indicator_text else False
        _qual_pairs = [
            ("per capita", " per capita"),
            ("per person", " per capita"),
            ("growth rate", " growth rate"),
            ("growth", " growth"),
        ]
        for qual, suffix in _qual_pairs:
            if _skip_qualifier:
                break
            if qual in original_q and qual not in indicator_text:
                augmented = indicator_text + suffix
                # Verify the augmented query resolves to a different (better) code
                aug_code = svc._get_direct_provider_indicator_translation(
                    provider=provider, indicator_query=augmented.strip(),
                )
                if aug_code and aug_code != existing_indicator:
                    existing_indicator = aug_code
                    has_explicit_code = True
                    params = _apply_indicator_with_semantic_label(
                        aug_code,
                        __catalog_resolved=True,
                        __qualifier_checked=True,
                    )
                    intent.parameters = params
                    logger.info(
                        "📝 Qualifier recovered: '%s' + '%s' → %s",
                        indicator_text, suffix.strip(), aug_code,
                    )
                break
        params = {**params, "__qualifier_checked": True}
        intent.parameters = params

    # Catalog-resolved codes may be the BASE indicator (e.g., total unemployment).
    # If the query has variant qualifiers (female, youth, male, rural, per capita, PPP),
    # let the IndicatorSelector find the specific variant instead of locking to base.
    _catalog_variant_fallback: Optional[str] = None
    if has_explicit_code and params.get("__catalog_resolved"):
        original_query = _effective_original_query(intent).lower()
        variant_qualifiers = {
            "female", "male", "youth", "young", "adolescent", "adult", "elderly",
            "rural", "urban", "primary", "secondary", "tertiary",
            "per capita", "per person", "per 1000", "per 100",
            "ppp", "constant", "real", "nominal", "net", "gross",
        }
        matched_qualifiers = {q for q in variant_qualifiers if q in original_query}

        # If qualifiers are found, check whether the catalog-resolved
        # indicator's name already contains them.
        if matched_qualifiers:
            resolved_name_lower = ""
            try:
                _lookup = _get_indicator_resolver().lookup
                _meta = _lookup.get(provider, existing_indicator)
                if _meta:
                    resolved_name_lower = (_meta.get("name") or "").lower()
            except Exception:
                pass
            if not resolved_name_lower and intent.indicators:
                resolved_name_lower = str(intent.indicators[0] or "").lower()
            unresolved_qualifiers = {
                q for q in matched_qualifiers if q not in resolved_name_lower
            }
            matched_qualifiers = unresolved_qualifiers

        has_variant = bool(matched_qualifiers)
        if not has_variant:
            logger.info(
                "🔒 Keeping catalog-resolved %s indicator: %s",
                provider, existing_indicator,
            )
            params = _apply_indicator_with_semantic_label(existing_indicator)
            intent.parameters = params
            return params
        else:
            logger.info(
                "🔓 Catalog resolved %s but query has variant qualifiers %s — letting IndicatorSelector refine",
                existing_indicator, matched_qualifiers,
            )
            # Clear catalog lock so IndicatorSelector can run.
            # Track original catalog code so we can fall back to it
            # instead of running the expensive legacy resolver.
            has_explicit_code = False
            _catalog_variant_fallback = existing_indicator

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

    # Path 2-4: Dynamic resolution (direct translation -> resolver -> raw query)
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

    # Qualifier preservation: the LLM sometimes drops key modifiers
    original_q = _effective_original_query(intent).lower()
    iq_lower = indicator_query.lower()
    _qualifier_map = [
        ("per capita", " per capita"),
        ("per person", " per capita"),
        ("growth rate", " growth rate"),
        ("growth", " growth"),
    ]
    for qualifier, suffix in _qualifier_map:
        if qualifier in original_q and qualifier not in iq_lower:
            indicator_query = indicator_query + suffix
            logger.info(
                "📝 Qualifier preserved: added '%s' to indicator query -> '%s'",
                suffix.strip(), indicator_query,
            )
            break

    # Path 1.5: IndicatorSelector (embed -> LLM pick) -- primary resolution
    # For follow-ups, use resolvedQuery (e.g. "GDP per capita India") rather
    # than the raw follow-up text (e.g. "show from IMF instead").
    selector_query = (_effective_original_query(intent) or indicator_query or "").strip()
    if provider and looks_like_exact_provider_title_match(selector_query, provider):
        logger.info(
            "🎯 Skipping IndicatorSelector for exact %s title match: %s",
            provider,
            selector_query,
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
                    intent.parameters = params
                    return params
            if selection.needs_user_choice:
                logger.info(
                    "🔵 IndicatorSelector needs user choice: %d options",
                    len(selection.options),
                )
                params = {**params, "__indicator_options": selection.options}
                intent.parameters = params
                if _catalog_variant_fallback:
                    # Catalog already resolved a base code; IndicatorSelector offered
                    # options for the variant.  Return now instead of also running the
                    # expensive legacy resolver -- the options are already the best we
                    # can offer without a user decision.
                    logger.info(
                        "🔒 Catalog-variant path: returning user-choice options without legacy fallback",
                    )
                    return params
                # Don't return -- fall through to legacy resolver as backup
        except Exception as e:
            if _catalog_variant_fallback:
                # IndicatorSelector failed but we have the catalog base code --
                # use it directly instead of running the legacy resolver.
                logger.info(
                    "🔒 IndicatorSelector unavailable; falling back to catalog-resolved code: %s (error: %s)",
                    _catalog_variant_fallback, e,
                )
                params = _apply_indicator_with_semantic_label(_catalog_variant_fallback)
                intent.parameters = params
                return params
            logger.debug("IndicatorSelector unavailable, using legacy resolver: %s", e)

    # When the catalog already resolved a code and IndicatorSelector ran but
    # found nothing (no code, no user-choice options), fall back to the
    # catalog code rather than running the legacy resolver redundantly.
    if _catalog_variant_fallback:
        logger.info(
            "🔒 IndicatorSelector returned no result for variant query; "
            "keeping catalog-resolved code: %s",
            _catalog_variant_fallback,
        )
        params = _apply_indicator_with_semantic_label(_catalog_variant_fallback)
        intent.parameters = params
        return params

    # Path 2: Legacy IndicatorResolver (catalog + database FTS + vector search)
    resolved = resolver.resolve(
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

    # Path 3: Direct translation fallback (fuzzy IndicatorTranslator)
    direct_translation_code: Optional[str] = None
    if not accepted_resolved:
        direct_translation = svc._get_direct_provider_indicator_translation(
            provider=provider,
            indicator_query=indicator_query,
        )
        if direct_translation and svc._looks_like_provider_indicator_code(provider, direct_translation):
            _resolver_inst = _get_indicator_resolver()
            _lookup = getattr(_resolver_inst, 'lookup', None)
            _meta = _lookup.get(provider, direct_translation) if _lookup else None
            _name = (_meta.get("name", "") if _meta else "")
            if not svc._verify_semantic_discriminators(
                _effective_original_query(intent) or indicator_query,
                direct_translation,
                _name,
            ):
                direct_translation = None

        if direct_translation and svc._looks_like_provider_indicator_code(provider, direct_translation):
            logger.info(
                "📌 Using direct %s indicator translation fallback: '%s' -> '%s'",
                provider,
                indicator_query,
                direct_translation,
            )
            direct_translation_code = str(direct_translation)

    # Path 4: Apply best result or fall back to raw query
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
    provider_locked = bool((intent.parameters or {}).get("__semantic_provider_locked"))

    def _fallback_to_original_or_distilled() -> str:
        if provider_locked:
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
