"""Data fetching logic: provider dispatch, multi-indicator, and specialty fetchers.

Extracted from query.py to reduce file size and isolate the data retrieval
layer into a focused module.

This module provides:
- fetch_from_provider_dispatch: Route to the correct provider and call its API
- fetch_data: Full fetch pipeline (resolve, cache, dispatch, validate)
- fetch_multi_indicator_data: Parallel fetch for multi-indicator queries
- fetch_from_coingecko: CoinGecko cryptocurrency data fetching
- fetch_exchange_rate_with_historical_fallback: ExchangeRate + FRED fallback
- fetch_historical_exchange_from_fred: FRED historical exchange rates
- extract_exchange_rate_params: Currency pair extraction from query text
"""

from __future__ import annotations

import asyncio
import copy
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import httpx

from ..models import ExecutionPlan, Metadata, NormalizedData, ParsedIntent
from ..services.indicator_resolution import is_exact_match_locked
from ..services.provider_strategy import (
    PROVIDER_GROUP_AGGREGATES,
    REGION_AS_SERIES_PROVIDERS,
    canonical_aggregate_region,
    humanize_region_name,
    normalize_aggregate_geography,
    region_qualified_indicator_text,
)
from ..services.user_messages import get_message as _localized_message
from ..services.time_scope_provenance import stamped_user_time_scope as _stamped_user_time_scope
from ..services.follow_up_retrieval import (
    is_follow_up_intent as _is_follow_up_intent,
    structured_indicator_label as _structured_indicator_label,
)
from ..utils.imf_supportability import imf_exact_provider_surface_supportability_reason
from ..utils.providers import ALL_PROVIDERS, normalize_provider_name, provider_progress_labels
from ..utils.retry import retry_async, DataNotAvailableError
from ..services.time_range_defaults import apply_default_time_range
from ..utils.processing_steps import get_processing_tracker
from ..routing.country_resolver import CountryResolver

if TYPE_CHECKING:
    from ..providers.fred import FREDProvider
    from ..providers.worldbank import WorldBankProvider
    from ..providers.comtrade import ComtradeProvider
    from ..providers.statscan import StatsCanProvider
    from ..providers.imf import IMFProvider
    from ..providers.exchangerate import ExchangeRateProvider
    from ..providers.bis import BISProvider
    from ..providers.eurostat import EurostatProvider
    from ..providers.oecd import OECDProvider
    from ..providers.coingecko import CoinGeckoProvider

logger = logging.getLogger(__name__)

_KNOWN_COUNTRY_ALIASES = {
    alias
    for alias in CountryResolver.COUNTRY_ALIASES
    if len(alias) > 2 or alias.isalpha()
}
# Every alpha-2 country code the resolver can produce. Deriving the set from
# self-aliases only ("ca" -> "CA") silently dropped countries whose ISO code has
# no self-alias (the United Kingdom resolves via "uk"/"united kingdom" -> "GB"),
# so FRED's country-scope guard treated "United Kingdom"/"GB" as parser noise
# and could serve U.S. data for a U.K. request.
_KNOWN_ISO2_COUNTRIES = {
    code
    for code in CountryResolver.COUNTRY_ALIASES.values()
    if len(code) == 2 and code.isalpha()
}


def _normalize_known_country_to_iso2(country: Any) -> Optional[str]:
    """Normalize only real known countries; reject parser-noise tokens."""
    text = str(country or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered not in _KNOWN_COUNTRY_ALIASES and text.upper() not in _KNOWN_ISO2_COUNTRIES:
        return None
    normalized = CountryResolver.normalize(text)
    if normalized in _KNOWN_ISO2_COUNTRIES:
        return normalized
    return None


# ---------------------------------------------------------------------------
# Pre-compiled regex patterns
# ---------------------------------------------------------------------------

_CURRENCY_TO_RE = re.compile(r"\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b")
_CURRENCY_SLASH_RE = re.compile(r"\b([A-Z]{3})[/\-]([A-Z]{3})\b")
_CURRENCY_VS_RE = re.compile(r"\b([A-Z]{3})\s+VS\.?\s+([A-Z]{3})\b")
_CURRENCY_CODE_RE = re.compile(r"\b([A-Z]{3})\b")

# Country -> ISO-4217 currency, so "<country> exchange rate" (no explicit pair
# in the text) resolves to that country's currency instead of the USD->EUR
# default. This is structural reference data (like COUNTRY_CODE_MAPPINGS or the
# ISO_4217 code list), not a per-query mapping — it works for every country in
# the table. The upstream country override emits an English NAME for some
# countries and an ISO-3166 alpha-2 CODE for others (e.g. "Brazil" vs "JP"), so
# the map is keyed on BOTH forms (all lowercased) to be robust to either.
_COUNTRY_TO_CURRENCY: Dict[str, str] = {
    # By English name
    "united states": "USD", "usa": "USD", "us": "USD", "america": "USD",
    "eurozone": "EUR", "euro area": "EUR", "european union": "EUR",
    "germany": "EUR", "france": "EUR", "italy": "EUR", "spain": "EUR",
    "netherlands": "EUR", "belgium": "EUR", "austria": "EUR", "portugal": "EUR",
    "ireland": "EUR", "greece": "EUR", "finland": "EUR",
    "united kingdom": "GBP", "uk": "GBP", "britain": "GBP", "england": "GBP",
    "japan": "JPY", "china": "CNY", "switzerland": "CHF", "canada": "CAD",
    "australia": "AUD", "new zealand": "NZD", "india": "INR",
    "south korea": "KRW", "korea": "KRW", "brazil": "BRL", "russia": "RUB",
    "mexico": "MXN", "south africa": "ZAR", "turkey": "TRY", "türkiye": "TRY",
    "singapore": "SGD", "hong kong": "HKD", "sweden": "SEK", "norway": "NOK",
    "denmark": "DKK", "thailand": "THB", "malaysia": "MYR", "taiwan": "TWD",
    "indonesia": "IDR", "philippines": "PHP", "poland": "PLN", "hungary": "HUF",
    "czech republic": "CZK", "czechia": "CZK", "israel": "ILS", "chile": "CLP",
    "colombia": "COP", "argentina": "ARS", "peru": "PEN", "egypt": "EGP",
    "nigeria": "NGN", "saudi arabia": "SAR", "united arab emirates": "AED",
    "uae": "AED", "vietnam": "VND", "pakistan": "PKR", "bangladesh": "BDT",
    # By ISO-3166 alpha-2 code (lowercased)
    "us": "USD", "gb": "GBP", "jp": "JPY", "cn": "CNY", "ch": "CHF",
    "ca": "CAD", "au": "AUD", "nz": "NZD", "in": "INR", "kr": "KRW",
    "br": "BRL", "ru": "RUB", "mx": "MXN", "za": "ZAR", "tr": "TRY",
    "sg": "SGD", "hk": "HKD", "se": "SEK", "no": "NOK", "dk": "DKK",
    "th": "THB", "my": "MYR", "tw": "TWD", "id": "IDR", "ph": "PHP",
    "pl": "PLN", "hu": "HUF", "cz": "CZK", "il": "ILS", "cl": "CLP",
    "co": "COP", "ar": "ARS", "pe": "PEN", "eg": "EGP", "ng": "NGN",
    "sa": "SAR", "ae": "AED", "vn": "VND", "pk": "PKR", "bd": "BDT",
    "de": "EUR", "fr": "EUR", "it": "EUR", "es": "EUR", "nl": "EUR",
    "be": "EUR", "at": "EUR", "pt": "EUR", "ie": "EUR", "gr": "EUR",
    "fi": "EUR",
}
_TOP_N_RE = re.compile(r"\btop\s+(\d{1,3})\b")
_TIME_SCOPE_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_TIME_SCOPE_SINGLE_YEAR_RE = re.compile(
    r"\b(?:since|between|from|through|until|before|after|during|in|for)\s+"
    r"(?:19\d{2}|20\d{2})\b",
    flags=re.IGNORECASE,
)
_TIME_SCOPE_YEAR_RANGE_RE = re.compile(
    r"\b(?:19\d{2}|20\d{2})\s*(?:-|–|—|to|through|until)\s*"
    r"(?:19\d{2}|20\d{2})\b",
    flags=re.IGNORECASE,
)
_RECENCY_CUE_RE = re.compile(
    r"\b(?:latest|most recent|current|currently|today|yesterday|now)\b",
    flags=re.IGNORECASE,
)
_CURRENT_MEASUREMENT_CUE_RE = re.compile(
    r"\bcurrent\s+(?:"
    r"prices?|"
    r"us\$|u\.s\.\$|u\.s\. dollars?|us dollars?|dollars?|"
    r"lcu|local currency|national currency|"
    r"account|activity|assets?|liabilities?|"
    r"expenditure|expenditures|revenues?|costs?"
    r")(?=\W|$)",
    flags=re.IGNORECASE,
)
_DURATION_MEASUREMENT_CUE_RE = re.compile(
    r"\bwithin\s+\d{1,3}\s+quarters?\b|"
    r"\b\d{1,3}[-\s]+month\s+treasury\s+bill\b",
    flags=re.IGNORECASE,
)
# A constant-price BASE YEAR ("in 2015 dollars", "at 2010 prices",
# "chained 2012 dollars") denominates the values — it is NOT a request to
# filter the time window to that year. The d8264dc prompt teaches the LLM this,
# but the text time-scope heuristics still saw the bare year and marked the
# window user-set, suppressing the default-window strip and Eurostat sparse
# retry → false "no data". Strip the phrase before the single-year/range tests.
_CONSTANT_PRICE_BASE_YEAR_RE = re.compile(
    r"\b(?:constant|chained)\s+(?:19\d{2}|20\d{2})\b"
    r"|\b(?:19\d{2}|20\d{2})\s+(?:u\.?s\.?\s+)?(?:constant\s+|chained\s+)?(?:dollars?|prices?)\b",
    flags=re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Provider name normalization (shared utility — no circular imports)
# ---------------------------------------------------------------------------

def _normalize_provider_name(provider: str) -> str:
    from ..utils.providers import normalize_provider_name
    return normalize_provider_name(provider)


_PROVIDER_INTERNAL_SEMANTIC_MAP_PROVIDERS = {
    "STATSCAN",
    "STATISTICS CANADA",
    "OECD",
    "EUROSTAT",
    "IMF",
    "BIS",
    "FRED",
}


_STATSCAN_MECHANICAL_PRODUCT_AUTHORITIES = {
    "verified_conversation_state",
    "llm_adjudication",
    "exact_user_input",
}


def _has_statscan_mechanical_dimension_dispatch_authority(params: dict) -> bool:
    """Allow only mechanical StatsCan coordinate dispatch on a verified table.

    This is intentionally narrower than generic semantic authority.  A
    previously verified StatsCan product carried in conversation state may
    authorize a follow-up to filter/decompose that same provider-native cube.
    A first-turn product selected by exact provider-native input or
    IndicatorSelector LLM adjudication may also authorize provider-native
    coordinate dispatch.  This predicate must not choose a provider, product,
    or indicator from natural language.
    """
    product_id = str(params.get("__statscan_product_id") or "").strip()
    if not product_id:
        return False
    if len("".join(ch for ch in product_id if ch.isdigit())) < 8:
        return False
    product_authority = str(params.get("__statscan_product_authority") or "")
    if product_authority not in _STATSCAN_MECHANICAL_PRODUCT_AUTHORITIES:
        return False
    if product_authority == "verified_conversation_state":
        if not params.get("__delta_resolved"):
            return False
        if params.get("__delta_indicator_changed"):
            return False
    elif product_authority != str(params.get("__semantic_authority") or ""):
        return False
    return bool(params.get("__dimensions") or params.get("__statscan_decomposition_axis"))


def _has_provider_map_authority(params: dict) -> bool:
    """Return whether provider-internal maps may be used as API mechanics.

    On the default no-shortcut path, natural-language provider maps are never
    semantic final authority. They are allowed only after an exact
    user/provider-native target or an LLM selector decision has already
    established semantic
    authority.
    """
    if is_exact_match_locked(params):
        return True
    authority = str(params.get("__semantic_authority") or "")
    # verified_conversation_state is included unconditionally and on purpose:
    # the stamp is ONLY ever written when the indicator is unchanged from the
    # turn that verified it — both at the data_fetcher delta branch (guarded by
    # `not __delta_indicator_changed`) and in materialize_intent (state only
    # carries resolved_indicator_code forward when neither the indicator nor
    # the provider changed). The stamp's presence is therefore sufficient
    # proof; re-checking the __delta_* flags here is wrong because they are
    # stripped from params before the dispatch-level assert runs.
    return authority in {
        "exact_user_input",
        "llm_adjudication",
        "post_fetch_semantic_judge",
        "verified_conversation_state",
    }


def _assert_provider_map_authority(
    provider: str,
    intent: ParsedIntent,
    params: dict,
) -> None:
    """Fail closed before provider-internal semantic maps can become final.

    The default no-shortcut path must have explicit/LLM/post-fetch authority
    before dispatching to providers known to contain natural-language maps.
    """
    if provider not in _PROVIDER_INTERNAL_SEMANTIC_MAP_PROVIDERS:
        return
    if _has_provider_map_authority(params):
        return
    if provider == "STATSCAN" and _has_statscan_mechanical_dimension_dispatch_authority(params):
        return

    indicator_text = str(
        params.get("indicator")
        or (intent.indicators[0] if intent.indicators else "")
        or ""
    ).strip()
    raise DataNotAvailableError(
        "No-shortcut path blocked provider-internal map dispatch without "
        "final semantic authority. "
        f"provider={provider}, indicator={indicator_text or '<missing>'}, "
        f"selector_status={params.get('__indicator_selection_status') or 'unknown'}"
    )


def _execution_plan_candidate_code(intent: ParsedIntent, params: dict) -> str:
    candidates = [
        params.get("indicator"),
        params.get("seriesId"),
        params.get("series_id"),
        params.get("code"),
        (intent.indicators or [None])[0],
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return "UNKNOWN"


def _restore_semantic_indicator_label_for_generic_metadata(
    result: List[NormalizedData],
    params: dict,
) -> None:
    """Use the user's metric text when provider metadata only exposes a raw code.

    This is a display/traceability repair, not semantic remapping: the provider
    code selected by retrieval stays unchanged.  It prevents successful dynamic
    fetches from being reported as unhelpful labels such as ``Vector 41690914``.
    """
    semantic_label = str(params.get("__semantic_indicator_label") or "").strip()
    if not semantic_label:
        return

    resolved_code = str(params.get("indicator") or "").strip().lower()
    for series in result or []:
        metadata = getattr(series, "metadata", None)
        if metadata is None:
            continue
        current_indicator = str(getattr(metadata, "indicator", "") or "").strip()
        current_lower = current_indicator.lower()
        if (
            re.fullmatch(r"vector\s+\d+", current_lower)
            or (resolved_code and current_lower == resolved_code)
            or current_lower in {"", "unknown", "indicator"}
        ):
            metadata.indicator = semantic_label


def _fred_series_id_base_matches(series_id: Any, indicator: Any) -> bool:
    """True when a FRED seriesId names the SAME series as the resolved
    indicator, ignoring a ``:transform`` suffix and letter case."""
    base = str(series_id or "").strip().split(":", 1)[0].strip().upper()
    return bool(base) and base == str(indicator or "").strip().upper()


def _fred_kept_series_transform(params: dict, code: Any) -> Optional[str]:
    """The ``:transform`` suffix of a seriesId that FRED dispatch KEEPS (its
    base equals the resolved code). Dispatch and the provider_request contract
    share this so a year-over-year (``:pc1``) payload never shares a cache key
    with the levels payload of the same code."""
    for key in ("seriesId", "series_id"):
        raw = str((params or {}).get(key) or "").strip()
        if ":" in raw and _fred_series_id_base_matches(raw, code):
            suffix = raw.split(":", 1)[1].strip().lower()
            if suffix:
                return suffix
    return None


def _normalized_source_key(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().upper())


def _attach_requested_series_disclosure(
    result: List[NormalizedData],
    params: dict,
    intent: Optional[ParsedIntent],
) -> List[NormalizedData]:
    """Disclose when the code the USER named is not the code being served.

    Resolution stamps ``__requested_series_id`` for a seriesId that was
    literally in the current turn's text: with status ``lock`` when it was
    locked, otherwise with why it could not be (not found, unverifiable,
    already dead this turn, or a protected prior decision). Every seriesId
    drop downstream (FRED dispatch, exact-lock cleanup, country-scope
    discovery, cross-provider fallback) is therefore covered by this ONE
    per-request seam instead of being silent. Structural comparison of
    provider codes / sources only. Runs on fresh AND cache-hit results and
    decorates COPIES, so a request-specific note is never written into a
    shared cache entry.
    """
    requested = str((params or {}).get("__requested_series_id") or "").strip()
    if not requested or not result:
        return result
    status = str(params.get("__requested_series_id_status") or "").strip()
    served_codes = [
        str(getattr(getattr(series, "metadata", None), "seriesId", "") or "").strip()
        for series in result
    ]
    if status == "lock":
        # The named code WAS locked. Providers do not all echo a bare code as
        # metadata.seriesId (dimension suffixes, composite SDMX keys), so a
        # code comparison could raise a false alarm on a correct serve; the
        # structural signal for "something else was served" is a different
        # SOURCE (cross-provider fallback after the locked fetch failed).
        locked_source = _normalized_source_key(params.get("__requested_series_provider"))
        served_sources = {
            _normalized_source_key(getattr(getattr(series, "metadata", None), "source", ""))
            for series in result
        }
        if not locked_source or locked_source in served_sources:
            return result
    elif any(code.split(":", 1)[0].upper() == requested.upper() for code in served_codes if code):
        return result
    language = getattr(intent, "language", None)
    served_label = next((code for code in served_codes if code), "") or str(
        getattr(getattr(result[0], "metadata", None), "indicator", "") or ""
    )
    message_id = {
        "not_found": "requested_series_not_found",
        "unverified": "requested_series_unverified",
    }.get(status, "requested_series_substituted")
    note = _localized_message(
        message_id,
        language,
        requested=requested,
        served=served_label or _localized_message("requested_series_served_fallback", language),
    )
    if not note:
        return result
    decorated: List[NormalizedData] = []
    for index, series in enumerate(result):
        metadata = getattr(series, "metadata", None)
        if index != 0 or metadata is None or note in (metadata.notes or []):
            decorated.append(series)
            continue
        new_metadata = metadata.model_copy(update={"notes": [note] + list(metadata.notes or [])})
        decorated.append(series.model_copy(update={"metadata": new_metadata}))
    return decorated


def _years_from_params(params: dict) -> tuple[Optional[int], Optional[int]]:
    start_year = params.get("start_year")
    end_year = params.get("end_year")
    if start_year is None and params.get("startDate"):
        start_year = int(str(params["startDate"])[:4])
    if end_year is None and params.get("endDate"):
        end_year = int(str(params["endDate"])[:4])
    return start_year, end_year


def _user_set_time_scope(params: dict, original_query: str) -> bool:
    """Whether the active time window was set by the USER (vs framework default).

    Prefers the recorded provenance stamp (__time_scope_authority, written by
    apply_default_time_periods and carried through conversation state) over
    re-inferring from the current turn's text — text inference breaks on
    follow-ups whose raw text ("compare with France") carries no time words
    even though the dates in params were user-set turns ago. Falls back to
    the text heuristic when no stamp is present (legacy intents, caches).
    """
    stamped = _stamped_user_time_scope(params)
    if stamped is not None:
        return stamped
    return _query_has_explicit_time_scope(original_query or "")


def _query_has_explicit_time_scope(query: str) -> bool:
    """Detect whether the user explicitly constrained the requested time window."""
    query_text = str(query or "").strip()
    if not query_text:
        return False
    recency_query_text = _CURRENT_MEASUREMENT_CUE_RE.sub(" ", query_text)
    if _RECENCY_CUE_RE.search(recency_query_text):
        return True
    # Neutralize constant-price base years ("in 2015 dollars") before the year
    # tests so they don't count as an explicit time filter.
    query_text = _CONSTANT_PRICE_BASE_YEAR_RE.sub(" ", query_text)
    if _TIME_SCOPE_SINGLE_YEAR_RE.search(query_text):
        return True
    for match in _TIME_SCOPE_YEAR_RANGE_RE.finditer(query_text):
        # Exact catalog titles sometimes contain coverage years, e.g.
        # "(1998-2001) from Eurostat"; those title years should not make a
        # framework-applied recent default window strict.  Treat non-title
        # ranges such as "from OECD 2021-2024" or "2015 to 2019" as explicit.
        parenthesized_title_range = (
            match.start() > 0
            and query_text[match.start() - 1] == "("
            and match.end() < len(query_text)
            and query_text[match.end()] == ")"
        )
        if not parenthesized_title_range:
            return True
    duration_query_text = _DURATION_MEASUREMENT_CUE_RE.sub(" ", recency_query_text)
    if re.search(
        r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years|quarter|quarters)\b",
        duration_query_text,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _coingecko_contract_coin_ids(params: dict[str, Any]) -> list[str]:
    raw_coin_ids = params.get("coinIds")
    if isinstance(raw_coin_ids, list):
        return [str(cid).strip() for cid in raw_coin_ids if str(cid).strip()]
    if isinstance(raw_coin_ids, str):
        return [part.strip() for part in raw_coin_ids.split(",") if part.strip()]
    return []


def _coingecko_contract_vs_currency(params: dict[str, Any]) -> str:
    raw_vs = str(params.get("vsCurrency") or "usd").strip().lower()
    invalid_tokens = {
        "right",
        "now",
        "today",
        "current",
        "recent",
        "latest",
        "trend",
        "performance",
        "history",
        "historical",
    }
    return raw_vs if raw_vs not in invalid_tokens and re.fullmatch(r"[a-z]{3,10}", raw_vs) else "usd"


# CoinGecko measure: parse-schema enum value -> CoinGecko provider metric.
# The parse LLM fills ``parameters.metric`` (closed enum, any language, any
# negation); this table only RENAMES enum values to the provider vocabulary.
_COINGECKO_METRIC_ENUM: Dict[str, str] = {
    "price": "price",
    "market_cap": "market_cap",
    "volume": "volume",
    "price_change": "24h_change",
}
# Historical endpoints (market_chart / range) expose only these series.
_COINGECKO_HISTORICAL_METRICS = frozenset({"price", "market_cap", "volume"})
# Provider-vocabulary spellings of each enum value: the enum value itself, the
# provider's metric parameter, and CoinGecko's own /coins/markets field names
# (current_price, market_cap — long form "market capitalization" —,
# total_volume, price_change_24h / price_change_percentage_24h). Compared by
# EQUALITY after structural normalization; this is vocabulary, not a phrase
# list over free text.
_COINGECKO_METRIC_SPELLINGS: Dict[str, frozenset] = {
    "price": frozenset({"price", "currentprice"}),
    "market_cap": frozenset({"marketcap", "marketcapitalization", "marketcapitalisation"}),
    "volume": frozenset({"volume", "totalvolume"}),
    "price_change": frozenset({
        "pricechange", "24hchange", "pricechange24h", "pricechangepercentage24h",
    }),
}


def _coingecko_enum_member(value: Any) -> Optional[str]:
    """Exact (normalized) membership of ``value`` in the metric enum.

    Structural normalization only — lowercase, collapse spaces/underscores/
    hyphens, an optional plural "s" — then EQUALITY with one of the enum
    value's provider-vocabulary spellings. Never substring search, so a label
    like "price not market cap" matches nothing.
    """
    compact = re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())
    if not compact:
        return None
    for enum_value, forms in _COINGECKO_METRIC_SPELLINGS.items():
        if compact in forms or (compact.endswith("s") and compact[:-1] in forms):
            return enum_value
    return None


def _coingecko_requested_metric(intent: Optional[ParsedIntent], params: dict[str, Any]) -> str:
    """The ONE CoinGecko metric decision, shared by the cache contract and fetch.

    Order: the parse's ``parameters.metric`` enum -> the LLM-normalized
    ``intent.indicators`` labels by exact enum equality -> "price". The raw
    query text is never read: a substring rule over it served market caps for
    "prices not market caps" and could not see 市值/成交量 at all. Returns the
    ENUM value (price | market_cap | volume | price_change).
    """
    enum_value = _coingecko_enum_member((params or {}).get("metric"))
    if enum_value:
        return enum_value
    for label in (getattr(intent, "indicators", None) or []):
        enum_value = _coingecko_enum_member(label)
        if enum_value:
            return enum_value
    return "price"


def _coingecko_has_historical_time_scope(query: str) -> bool:
    """Return True only when a CoinGecko query asks for a time series/window.

    CoinGecko current-price catalog rows are short-lived assets where the
    simple-price endpoint can succeed even when the historical market-chart
    endpoint has no data.  Treat snapshot cues such as "current", "latest",
    "today", or "right now" as current requests, not historical windows.
    """
    query_text = str(query or "").strip()
    if not query_text:
        return False

    snapshot_neutral_text = re.sub(
        r"\b(?:latest|most recent|current|currently|today|now)\b|\bright now\b",
        " ",
        query_text,
        flags=re.IGNORECASE,
    )
    # A base year ("in 2015 dollars") is not a historical window request.
    snapshot_neutral_text = _CONSTANT_PRICE_BASE_YEAR_RE.sub(" ", snapshot_neutral_text)

    if re.search(r"\byesterday\b", query_text, flags=re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:last|past|previous|recent|since|between|through|until|before|after|during|over)\b",
        snapshot_neutral_text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\bfrom\s+(?:19\d{2}|20\d{2}|[A-Z][a-z]+\s+\d{1,2}(?:,\s*)?\s*(?:19\d{2}|20\d{2}))\b",
        snapshot_neutral_text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:historical|history|chart|trend|performance|time\s+series)\b",
        snapshot_neutral_text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years|quarter|quarters)\b",
        snapshot_neutral_text,
        flags=re.IGNORECASE,
    ):
        return True
    if _TIME_SCOPE_SINGLE_YEAR_RE.search(snapshot_neutral_text):
        return True
    return bool(_TIME_SCOPE_YEAR_RANGE_RE.search(snapshot_neutral_text))


# Provenance for a geography the provider DISPATCH chose because the request
# carried none (mirrors __time_scope_authority="default"). Disclosure only:
# the served data is unchanged; the response chokepoint
# (QueryService._disclose_default_geography) reads Metadata.geoScopeAuthority.
GEO_SCOPE_AUTHORITY_DEFAULT = "default"


def _mark_provider_default_geography(
    series_list: List[NormalizedData], default_scope: str,
) -> List[NormalizedData]:
    """Stamp a provider-default geography on the served series (never changes data)."""
    scope = str(default_scope or "").strip()
    if not scope:
        return series_list
    note = f"No geography was resolved from the request; showing the source default scope: {scope}."
    for series in series_list or []:
        metadata = getattr(series, "metadata", None)
        if metadata is None:
            continue
        metadata.geoScopeAuthority = GEO_SCOPE_AUTHORITY_DEFAULT
        notes = list(metadata.notes or [])
        if note not in notes:
            notes.append(note)
        metadata.notes = notes
    logger.info("🌍 Provider default geography disclosed: %s", scope)
    return series_list


def _native_aggregate_or_none(provider: str, value: Any) -> Optional[str]:
    """Provider-native id for an established aggregate entity, else None.

    Structural guard consulted before a provider's own region resolver: an
    established aggregate (euro area, EU, World) must never be member-expanded
    into a fan-out of member series. When the provider declares a native id
    (PROVIDER_GROUP_AGGREGATES) it is returned; when it declares none, the
    request fails honestly so fallback can try a provider that publishes the
    aggregate. Values that are not established aggregates return None.
    """
    region = canonical_aggregate_region(value)
    if region is None:
        return None
    native = PROVIDER_GROUP_AGGREGATES.get(_normalize_provider_name(provider), {}).get(region)
    if native:
        return native
    raise DataNotAvailableError(
        f"{provider} publishes no native {humanize_region_name(region)} aggregate; "
        "member-country series are not a substitute for the group value."
    )


def _provider_request_contract(provider: str, intent: ParsedIntent, params: dict) -> dict[str, Any]:
    provider_norm = _normalize_provider_name(provider) or "UNKNOWN"
    code = _execution_plan_candidate_code(intent, params)
    countries = params.get("countries")
    raw_country_list = [str(country).strip() for country in countries] if isinstance(countries, list) else []
    country_scope = list(raw_country_list)
    if not country_scope and params.get("country"):
        country_scope = [params.get("country")]
    if provider_norm == "FRED":
        normalized_scope = []
        for country in country_scope:
            normalized = _normalize_known_country_to_iso2(country)
            if normalized is not None:
                normalized_scope.append(normalized)
            elif canonical_aggregate_region(country):
                normalized_scope.append(str(country))
        country_scope = normalized_scope

    start_year, end_year = _years_from_params(params)
    contract: dict[str, Any] = {
        "provider": provider_norm,
        "code": code,
        "country_scope": [str(country) for country in country_scope if str(country or "").strip()],
        "start_year": start_year,
        "end_year": end_year,
    }

    if provider_norm == "FRED":
        contract["series_id"] = str(code or "").strip()
        contract["country"] = contract["country_scope"][0] if contract["country_scope"] else None
        # Only when present, so every transform-less key is unchanged.
        fred_transform = _fred_kept_series_transform(params, code)
        if fred_transform:
            contract["transform"] = fred_transform
        # Country-scope discovery searches FRED with dispatch-scoped text while
        # the verified code stays in ``indicator``; the search text is part of
        # the request identity (two discoveries must not share a cache entry).
        if _fred_discovery_search_is_current(params):
            contract["discovery_query"] = str(params.get("__fred_discovery_query") or "").strip()

    if provider_norm == "WORLDBANK":
        contract["indicator"] = str(code or "").strip()
        contract["country"] = contract["country_scope"][0] if len(contract["country_scope"]) == 1 else None
        contract["countries"] = contract["country_scope"] if len(contract["country_scope"]) > 1 else []

    if provider_norm == "EUROSTAT":
        contract["dataset_code"] = str(code or "").strip().lower()
        contract["filters"] = {
            key: value
            for key, value in params.items()
            if key not in {
                "country",
                "countries",
                "indicator",
                "startDate",
                "endDate",
                "start_year",
                "end_year",
                # CoinGecko-scoped parse slot (price|market_cap|volume|
                # price_change), never an SDMX dimension: a stray value must
                # not become a STRICT Eurostat member filter.
                "metric",
            }
            and not str(key).startswith("_")
        }

    if provider_norm == "STATSCAN":
        contract["indicator"] = str(code or "").strip()
        contract["country"] = contract["country_scope"][0] if contract["country_scope"] else None
        contract["dimensions"] = dict(params.get("__dimensions") or params.get("dimensions") or {})
        contract["decomposition_axis"] = str(params.get("__statscan_decomposition_axis") or "").strip() or None
        contract["product_id"] = str(params.get("__statscan_product_id") or "").strip() or None
        # Geography is part of the request identity: "Canada GDP" and
        # "Ontario GDP" must never share a cache entry.
        contract["geography"] = str(params.get("geography") or "").strip() or None

    if provider_norm == "IMF":
        contract["indicator"] = str(code or "").strip()
        contract["country"] = str(params.get("country") or "").strip() or None
        contract["countries"] = raw_country_list
        contract["default_country"] = "USA" if not contract["country_scope"] else None

    if provider_norm == "OECD":
        contract["indicator"] = str(code or "").strip()
        contract["country"] = str(params.get("country") or "").strip() or None
        contract["countries"] = raw_country_list

    if provider_norm == "COINGECKO":
        contract["coin_ids"] = _coingecko_contract_coin_ids(params)
        contract["vs_currency"] = _coingecko_contract_vs_currency(params)
        # ALWAYS present (default "price" included): price and market-cap
        # requests for the same coin/window share ``code`` (indicators[0]), so
        # without the metric they collided in _cache_identity, and every
        # pre-fix key was poisoned with market-cap payloads served as "price".
        contract["metric"] = _coingecko_requested_metric(intent, params)
        contract["days"] = params.get("days")
        contract["start_date"] = params.get("startDate")
        contract["end_date"] = params.get("endDate")

    # Frequency (and FRED's aggregation method) are part of the request
    # IDENTITY, not just the payload: two requests that differ ONLY in frequency
    # — e.g. annual vs quarterly GDP for the same series/country/window —
    # otherwise collide in _cache_identity, so the second request silently
    # receives the first's cached slice (F1b). Include these generically for
    # every provider (previously only Eurostat's catch-all filters carried
    # frequency), but ONLY when actually set: an absent frequency keeps the
    # contract byte-identical to the pre-fix version so the common
    # no-frequency case is not cache-invalidated.
    effective_frequency = str(params.get("frequency") or "").strip()
    if effective_frequency:
        contract["frequency"] = effective_frequency
    aggregation_method = str(params.get("aggregation_method") or "").strip()
    if aggregation_method:
        contract["aggregation_method"] = aggregation_method

    return contract


def _fred_requested_country_scope(provider_request: dict[str, Any], params: dict[str, Any]) -> list[str]:
    """Return the requested country scope for a FRED dispatch/cache identity."""
    raw_scope = provider_request.get("country_scope")
    if isinstance(raw_scope, list):
        return [str(country).strip() for country in raw_scope if str(country or "").strip()]

    scope = []
    if not scope:
        countries = params.get("countries")
        if isinstance(countries, list):
            scope = [str(country).strip() for country in countries if str(country or "").strip()]

    if not scope and params.get("country"):
        scope = [str(params.get("country")).strip()]

    return scope


def _raise_if_scope_outside_provider_coverage(
    provider: str,
    provider_request: dict[str, Any],
    params: dict[str, Any],
) -> None:
    """Fail closed when the requested countries are outside a provider's coverage.

    Structural: the shared coverage predicate evaluated on requested ISO
    codes.  Serving a provider's own-country series for a different
    requested country is wrong data — worse than an honest no-data that lets
    the fallback/disclosure path run.  Parser-noise tokens in the country
    slot (not a known country) are ignored, as in the FRED scope guard.
    """
    from .provider_fallback import provider_covers_country_list

    requested = [
        iso2
        for iso2 in (
            _normalize_known_country_to_iso2(country)
            for country in _fred_requested_country_scope(provider_request, params)
        )
        if iso2
    ]
    if requested and not provider_covers_country_list(provider, requested):
        raise DataNotAvailableError(
            f"{provider} does not cover the requested geography {requested}; "
            "its series are scoped to a different country."
        )


def _fred_exact_title_targets_country(
    params: dict[str, Any],
    intent: Optional[ParsedIntent],
    iso2: str,
) -> bool:
    """Return whether an exact FRED title itself names the requested country."""
    if not params.get("__exact_indicator_title_match"):
        return False

    title_parts = [str(indicator or "") for indicator in (intent.indicators if intent else [])]
    if params.get("__semantic_indicator_label"):
        title_parts.append(str(params.get("__semantic_indicator_label") or ""))
    title_text = " ".join(part for part in title_parts if part).lower()
    if not title_text:
        return False

    aliases = [
        alias
        for alias, code in CountryResolver.COUNTRY_ALIASES.items()
        # Only full names (>= 4 chars); 3-letter ISO3 codes ("per", "are", "can")
        # collide with English words and falsely match prose titles.
        if code == iso2 and len(alias) > 3
    ]
    return any(
        re.search(rf"\b{re.escape(alias.lower())}\b", title_text)
        for alias in aliases
    )


def _country_aliases_for_iso2(iso2: str) -> list[str]:
    normalized = str(iso2 or "").strip().upper()
    if not normalized:
        return []
    aliases = [
        alias
        for alias, code in CountryResolver.COUNTRY_ALIASES.items()
        # Only full names (>= 4 chars); 3-letter ISO3 codes collide with English
        # words ("per"->PE, "are"->AE, ...) and falsely match prose titles.
        if code == normalized and len(alias) > 3
    ]
    return sorted(set(aliases), key=len, reverse=True)


def _country_label_for_iso2(iso2: str, fallback: Any = None) -> str:
    fallback_text = str(fallback or "").strip()
    if fallback_text and len(fallback_text) > 2 and fallback_text.upper() != str(iso2 or "").upper():
        return fallback_text
    aliases = _country_aliases_for_iso2(iso2)
    return aliases[0].title() if aliases else str(iso2 or fallback_text or "").upper()


def _text_targets_country(text: str, iso2: str) -> bool:
    text_lower = str(text or "").lower()
    if not text_lower:
        return False
    return any(
        re.search(rf"\b{re.escape(alias.lower())}\b", text_lower)
        for alias in _country_aliases_for_iso2(iso2)
    )


def _fred_catalog_series_targets_country(series_id: Any, iso2: str) -> bool:
    """Return whether local provider-native FRED metadata names the country."""
    code = str(series_id or "").strip()
    if not code:
        return False
    try:
        from ..services.indicator_database import get_indicator_lookup

        metadata = get_indicator_lookup().get("FRED", code)
    except Exception:
        metadata = None
    if not metadata:
        return False
    text = " ".join(
        str(metadata.get(key) or "")
        for key in ("name", "description", "title")
    )
    return _text_targets_country(text, iso2)


# Keys the FRED country-scope discovery demotes so provider-native search can
# pick a country-titled series. They are snapshotted (not just dropped) so a
# later fetch for a DIFFERENT geography can restore the verified request.
_FRED_DISCOVERY_DEMOTED_KEYS = (
    "seriesId",
    "series_id",
    "__exact_provider_code_match",
    "__exact_indicator_title_match",
)
# Dispatch-scoped keys owned by the FRED country-scope discovery.  They are
# meaningful only for the geography in ``__fred_requested_country`` AND only
# for the pick in ``__fred_discovery_pick`` (the ``indicator`` the discovery
# decision was made for).  A later stage that re-picks ``indicator`` for the
# same request (same-provider alternate retry, a clarification choice,
# uncertain-match recovery) supersedes them.
_FRED_DISCOVERY_SCOPED_KEYS = (
    "__fred_country_scope_discovery",
    "__fred_requested_country",
    "__fred_discovery_query",
    "__fred_pre_discovery",
    "__fred_discovery_pick",
)


def _same_fred_pick(left: Any, right: Any) -> bool:
    return str(left or "").strip() == str(right or "").strip()


def _fred_discovery_snapshot_pick(params: dict[str, Any]) -> Any:
    snapshot = params.get("__fred_pre_discovery")
    return snapshot.get("indicator") if isinstance(snapshot, dict) else None


def _fred_discovery_search_is_current(params: dict[str, Any]) -> bool:
    """Whether dispatch should search FRED with ``__fred_discovery_query``.

    Only while ``indicator`` is still the pick the search text was derived
    for.  Once a later stage re-picks ``indicator`` the re-pick is fetched
    as-is and must pass the result-level country validation; a stale search
    text must never silently replace it.
    """
    if not params.get("__fred_country_scope_discovery"):
        return False
    if not str(params.get("__fred_discovery_query") or "").strip():
        return False
    return _same_fred_pick(params.get("indicator"), _fred_discovery_snapshot_pick(params))


def _is_conversation_follow_up(params: dict[str, Any], intent: Optional[ParsedIntent]) -> bool:
    """Structural follow-up signal: the turn was parsed/materialized as a follow-up.

    ``intent.isFollowUp`` is the effective signal at the fetch/dispatch call
    sites: materialize_intent sets it for every delta turn (turn_number > 0)
    and the parser sets it for LLM-detected follow-ups.  fetch_data strips
    ``__delta_resolved`` as an internal key before its FRED prep runs, so that
    key only counts for callers that prepare params before the strip.  Same
    predicate as follow_up_retrieval.is_follow_up_intent (one definition).
    """
    return _is_follow_up_intent(intent, params)


def _follow_up_concept_text(params: dict[str, Any], intent: Optional[ParsedIntent]) -> str:
    """Return the carried concept for a follow-up turn, never the delta utterance.

    On a follow-up the raw utterance is a DELTA ("compare with Canada",
    "那日本呢", "add the UK") that carries geography, not the concept.  The
    concept comes from the SAME structured-label chain the follow-up menu /
    clarification retrieval uses (follow_up_retrieval.structured_indicator_label):
    the merged semantic label, a non-code parsed indicator phrase, else the
    catalog title of a carried provider code.  ``__display_indicator_label``
    (raw query text by construction) and any candidate equal to the raw
    utterance never count, and an unknown bare code is not a concept
    ("Canada UNRATE" is not a concept search).  The parser's standalone
    ``resolvedQuery`` is the last resort, only when no structured label exists
    and only when it is not the utterance itself.
    """
    label = _structured_indicator_label(intent, params)
    if label:
        return label
    if intent is not None and intent.resolvedQuery:
        resolved = re.sub(r"\s+", " ", str(intent.resolvedQuery)).strip()
        raw_texts = {
            re.sub(r"\s+", " ", str(raw or "")).strip().lower()
            for raw in (intent.originalQuery, params.get("__original_query"))
        }
        if resolved and resolved.lower() not in raw_texts:
            return resolved
    return ""


def _strip_fred_provider_phrase(text: str) -> str:
    query_text = re.sub(
        r"\b(?:from|use|using|via)\s+fred\b",
        " ",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", query_text).strip(" ,;:")


def _fred_country_context_query(
    params: dict[str, Any],
    intent: Optional[ParsedIntent],
    raw_country: Any,
    iso2: str,
) -> str:
    """Build provider-search text from query/country context, not hardcoded maps.

    On a follow-up the raw query text is the delta utterance ("和日本比较",
    "compare with Japan"), which names the change, not the concept; there
    the text is rebuilt from the carried concept (_follow_up_concept_text,
    which shares follow_up_retrieval's structured-label chain) + the
    requested country. First-turn behavior is unchanged.
    """
    country_label = _country_label_for_iso2(iso2, raw_country)
    if _is_conversation_follow_up(params, intent):
        # Searching FRED for "compare with Canada" retrieved arbitrary Canada
        # series.  An empty concept leaves the request undiscovered, so the
        # fail-closed country guard and the existing fallback chain take over.
        concept = _strip_fred_provider_phrase(_follow_up_concept_text(params, intent))
        if not concept:
            return ""
        if _text_targets_country(concept, iso2):
            return concept
        return re.sub(r"\s+", " ", f"{country_label} {concept}").strip()

    original_query = str(
        params.get("__original_query")
        or (intent.originalQuery if intent else "")
        or ""
    ).strip()
    if original_query:
        query_text = _strip_fred_provider_phrase(original_query)
        if query_text:
            return query_text

    indicator = str(
        params.get("__semantic_indicator_label")
        or params.get("indicator")
        or ((intent.indicators or [""])[0] if intent and intent.indicators else "")
        or ""
    ).strip()
    return re.sub(r"\s+", " ", f"{country_label} {indicator}").strip()


def _fred_non_us_scope(
    provider_request: dict[str, Any],
    params: dict[str, Any],
) -> list[tuple[str, str]]:
    requested_scope = _fred_requested_country_scope(provider_request, params)
    normalized_scope = [
        (country, _normalize_known_country_to_iso2(country))
        for country in requested_scope
    ]
    return [
        (country, iso2)
        for country, iso2 in normalized_scope
        if iso2 and iso2 != "US"
    ]


def _restore_fred_pre_discovery_params(
    params: dict[str, Any],
    intent: Optional[ParsedIntent] = None,
) -> dict[str, Any]:
    """Undo a FRED discovery rewrite made for a different geography.

    Discovery keys are dispatch-scoped: a coverage backfill or sibling fetch
    that copies the parent's params for another country (e.g. the U.S. leg of
    a U.S.+Canada comparison) must fetch the verified request, not re-run the
    Canada discovery under a U.S. label.
    """
    restored = dict(params)
    snapshot = restored.get("__fred_pre_discovery")
    for key in _FRED_DISCOVERY_SCOPED_KEYS:
        restored.pop(key, None)
    if isinstance(snapshot, dict):
        # The snapshot is the verified request as it stood before discovery.
        # It wins over anything a child fetch re-resolved in between (a child
        # re-resolving from the raw follow-up utterance picked a Canada series
        # for the U.S. leg of "compare with Canada").
        for key in ("indicator", *_FRED_DISCOVERY_DEMOTED_KEYS):
            if key in snapshot:
                restored[key] = snapshot[key]
            elif key != "indicator":
                restored.pop(key, None)
    if not str(restored.get("indicator") or "").strip() and intent is not None and intent.indicators:
        restored["indicator"] = intent.indicators[0]
    return restored


def _prepare_fred_country_scope_params(
    provider_request: dict[str, Any],
    params: dict[str, Any],
    intent: Optional[ParsedIntent] = None,
) -> dict[str, Any]:
    """Allow FRED non-US requests to use provider-native series discovery.

    This does not map countries or concepts to specific FRED codes.  It only
    demotes an unsafe exact-code candidate when provider-native metadata does
    not prove that the selected series targets the requested non-US country, so
    FRED catalog/search retrieval can choose a country-scoped candidate.

    Invariants:

    * ``__semantic_indicator_label`` is never rewritten -- conversation-state
      persistence and cross-provider fallback read it.
    * ``indicator`` is never replaced by search text.  The only rewrite is
      restoring the verified pre-discovery request from the snapshot when a
      fetch for a DIFFERENT geography inherits the keys.  The FRED search
      text lives in the dispatch-scoped ``__fred_discovery_query``.
    * The search text applies only to the pick it was derived for
      (``__fred_discovery_pick``).  A later same-request re-pick is fetched
      as-is under the result-level country validation.
    """
    non_us_scope = _fred_non_us_scope(provider_request, params)
    if params.get("__fred_country_scope_discovery"):
        same_country = (
            len(non_us_scope) == 1
            and non_us_scope[0][1] == str(params.get("__fred_requested_country") or "").upper()
        )
        if not same_country:
            params = _restore_fred_pre_discovery_params(params, intent)
        else:
            decided_pick = params.get("__fred_discovery_pick", _fred_discovery_snapshot_pick(params))
            if _same_fred_pick(params.get("indicator"), decided_pick):
                return params
            raw_country, iso2 = non_us_scope[0]
            if _same_fred_pick(
                _fred_country_context_query(params, intent, raw_country, iso2),
                params.get("__fred_discovery_query"),
            ):
                # Same request, and a later stage chose a different series on
                # purpose: the selector's next-best code after the search
                # result failed validation (same-provider alternate retry), a
                # user's menu choice, or an uncertain-match recovery candidate.
                # Re-running the identical search would silently discard that
                # choice.  Keep the bookkeeping (the snapshot still restores the
                # verified request for other geographies) and let dispatch
                # fetch the re-pick, which must pass the result-level country
                # validation.
                repicked = dict(params)
                repicked["__fred_discovery_pick"] = params.get("indicator")
                return repicked
            # The request itself changed (a params copy carried another
            # concept's discovery keys): evaluate it from scratch.
            params = {
                key: value
                for key, value in params.items()
                if key not in _FRED_DISCOVERY_SCOPED_KEYS
            }

    if len(non_us_scope) != 1:
        return params

    raw_country, iso2 = non_us_scope[0]
    selected_series = (
        provider_request.get("series_id")
        or params.get("seriesId")
        or params.get("series_id")
        or params.get("indicator")
    )
    if _fred_exact_title_targets_country(params, intent, iso2) or _fred_catalog_series_targets_country(selected_series, iso2):
        return params

    search_query = _fred_country_context_query(params, intent, raw_country, iso2)
    if not search_query:
        return params

    prepared = dict(params)
    prepared["__fred_pre_discovery"] = {
        key: params[key]
        for key in ("indicator", *_FRED_DISCOVERY_DEMOTED_KEYS)
        if key in params
    }
    prepared["__fred_discovery_query"] = search_query
    prepared["__fred_discovery_pick"] = params.get("indicator")
    prepared["__fred_country_scope_discovery"] = True
    prepared["__fred_requested_country"] = iso2
    for key in _FRED_DISCOVERY_DEMOTED_KEYS:
        prepared.pop(key, None)
    return prepared


def _validate_fred_country_scope_result(
    series_list: list[NormalizedData],
    provider_request: dict[str, Any],
    params: dict[str, Any],
) -> None:
    requested_scope = _fred_requested_country_scope(provider_request, params)
    normalized_scope = [
        _normalize_known_country_to_iso2(country)
        for country in requested_scope
    ]
    non_us_scope = [iso2 for iso2 in normalized_scope if iso2 and iso2 != "US"]
    if len(non_us_scope) != 1:
        return

    requested_iso2 = non_us_scope[0]
    for series in series_list:
        metadata = getattr(series, "metadata", None)
        result_country = _normalize_known_country_to_iso2(getattr(metadata, "country", None))
        result_text = " ".join(
            str(value or "")
            for value in (
                getattr(metadata, "indicator", None),
                getattr(metadata, "description", None),
                getattr(metadata, "notes", None),
            )
        )
        if result_country == requested_iso2 or _text_targets_country(result_text, requested_iso2):
            continue
        raise DataNotAvailableError(
            "Selected FRED series does not match the requested country scope. "
            f"requested_country_scope={requested_scope} "
            f"returned_country={getattr(metadata, 'country', None)!r} "
            f"series_id={getattr(metadata, 'seriesId', None)!r}"
        )


def _raise_if_fred_country_scope_unsupported(
    provider_request: dict[str, Any],
    params: dict[str, Any],
    intent: Optional[ParsedIntent] = None,
) -> None:
    """Fail closed when a FRED exact request asks for unsupported geography.

    FRED contains both U.S. and international series. Returning the U.S. ``GDP``
    series for a request such as "Canada GDP from FRED" is worse than a no-data
    answer, but the guard must be series-level rather than provider-level:
    non-U.S. FRED series are allowed only when provider-native title/catalog
    metadata proves the selected series targets the requested country.
    """
    if params.get("__fred_country_scope_discovery"):
        return

    requested_scope = _fred_requested_country_scope(provider_request, params)
    if not requested_scope:
        return

    unsupported = []
    for country in requested_scope:
        normalized = _normalize_known_country_to_iso2(country)
        # Some parser paths store non-geographic tokens (for example FRED
        # frequency strings such as "1W") in the country slot.  The FRED
        # contract guard should fail closed only for real non-US country
        # scopes, not for unrelated parser noise.
        if normalized is None:
            continue
        if normalized == "US":
            continue
        if _fred_exact_title_targets_country(params, intent, normalized):
            continue
        selected_series = (
            provider_request.get("series_id")
            or params.get("seriesId")
            or params.get("series_id")
            or params.get("indicator")
        )
        if _fred_catalog_series_targets_country(selected_series, normalized):
            continue
        unsupported.append(country)
    if not unsupported:
        return

    raise DataNotAvailableError(
        "Selected FRED series does not match the requested country scope. "
        "OpenEcon will not return U.S.-scoped FRED data for a non-U.S. "
        f"country request. requested_country_scope={unsupported}"
    )


def _comtrade_dispatch_commodity(params: dict[str, Any]) -> Optional[str]:
    """Return the commodity value to send to Comtrade provider dispatch.

    Non-numeric broad indicator labels intentionally remain unset so aggregate
    trade can use the provider's TOTAL default. Literal HS heading/subheading
    text, however, is a concrete provider-native commodity surface; pass it
    through so the Comtrade provider can resolve it from catalog evidence or
    fail closed instead of silently querying TOTAL.
    """

    explicit_commodity = str(params.get("commodity") or "").strip()
    if explicit_commodity:
        return explicit_commodity

    indicator = str(params.get("indicator") or "").strip()
    if not indicator:
        return None
    if indicator.isdigit():
        return indicator

    try:
        from ..providers.comtrade import ComtradeProvider

        if ComtradeProvider._looks_like_specific_hs_heading(indicator):  # pylint: disable=protected-access
            return indicator
    except Exception:
        return None
    return None


# Provider error markers that mean "the commodity TEXT did not resolve to a
# single HS code" (comtrade.py), as opposed to a data/network failure.
_COMTRADE_TEXT_RESOLUTION_FAILURES = (
    "comtrade_commodity_unresolved",
    "comtrade_hs_subheading_unresolved",
    "comtrade_hs_subheading_ambiguous",
)
# The provider-legal HS code shape (comtrade.py _commodity_code tiers 1-2):
# 2-6 digits, optionally "HS"-prefixed (indicators.db stores the 96 chapter
# codes as HS01..HS97). Captures the bare digits that are dispatched.
_COMTRADE_HS_CODE_SHAPE = re.compile(r"^(?:HS\s*)?(\d{2,6})$", re.IGNORECASE)
# Selection provenance that was produced by THIS request's resolution step.
# verified_conversation_state (and anything else) may carry a previous turn's
# code, so it never authorizes replacing commodity text.
_COMTRADE_RETRY_AUTHORITIES = frozenset({"llm_adjudication", "exact_user_input"})


def _norm_surface(text: Any) -> str:
    return " ".join(re.findall(r"[0-9a-z]+", str(text or "").lower()))


def _comtrade_adjudicated_retry_code(
    params: dict[str, Any],
    dispatched_commodity: Optional[str],
    error: BaseException,
) -> Optional[str]:
    """Return the selector-adjudicated HS code to retry with, or None.

    Commodity TEXT is always dispatched first (it can be more specific than
    the selector's pick). Only when the provider could not resolve that text
    does a one-shot retry use ``params['indicator']``, and only if it is:
    - a concrete HS code shape (2-6 digits, optionally "HS"-prefixed —
      never TOTAL or a broad label); the bare digits are dispatched;
    - adjudicated by this request's resolver (``__semantic_authority``);
    - bound to THIS commodity surface: the selector's input label
      (``__semantic_indicator_label``) contains the commodity text, so a code
      carried from a previous turn ("crude oil" -> "now wheat") never serves
      a different commodity.
    All checks are structural (error code, code shape, provenance, string
    identity); none judges what the commodity means.
    """
    if not any(marker in str(error) for marker in _COMTRADE_TEXT_RESOLUTION_FAILURES):
        return None
    code_match = _COMTRADE_HS_CODE_SHAPE.match(str(params.get("indicator") or "").strip())
    if not code_match:
        return None
    code = code_match.group(1)
    if str(params.get("__semantic_authority") or "") not in _COMTRADE_RETRY_AUTHORITIES:
        return None
    commodity_surface = _norm_surface(dispatched_commodity)
    label_surface = _norm_surface(params.get("__semantic_indicator_label"))
    if not commodity_surface or not label_surface or code == commodity_surface:
        return None
    if f" {commodity_surface} " not in f" {label_surface} ":
        return None
    return code


def _cache_identity(fetch_strategy: str, provider_request: dict[str, Any], expected_shape: dict[str, Any]) -> dict[str, Any]:
    return {
        "fetch_strategy": fetch_strategy,
        "provider_request": provider_request,
        "expected_shape": expected_shape,
    }


def materialize_execution_plan(
    execution_plan: Optional[ExecutionPlan],
    *,
    provider: str,
    intent: ParsedIntent,
    params: dict,
    fetch_strategy: str = "provider_dispatch",
) -> ExecutionPlan:
    """Return an execution plan that reflects the exact provider-dispatch request."""
    plan = execution_plan or ExecutionPlan(
        provider=provider,
        candidate_id="UNKNOWN:UNKNOWN",
        fetch_strategy=fetch_strategy,
        params={},
        expected_shape={},
        verification_checks=[],
    )
    provider_norm = _normalize_provider_name(provider) or "UNKNOWN"
    if not intent.clarificationNeeded:
        params = normalize_aggregate_geography(provider_norm, params)
        expected = dict(plan.expected_shape or {})
        if "requested_countries" in expected:
            expected["requested_countries"] = normalize_aggregate_geography(
                provider_norm, {"countries": expected["requested_countries"]},
            )["countries"]
            plan.expected_shape = expected
    code = _execution_plan_candidate_code(intent, params)
    plan.provider = provider_norm
    plan.candidate_id = f"{provider_norm}:{str(code or '').strip().upper() or 'UNKNOWN'}"
    plan.fetch_strategy = fetch_strategy
    provider_request = _provider_request_contract(provider_norm, intent, params)
    cache_identity = _cache_identity(fetch_strategy, provider_request, dict(plan.expected_shape or {}))
    plan.provider_request = provider_request
    plan.cache_identity = cache_identity
    plan.params = {
        **dict(params),
        "__execution_plan_identity": cache_identity,
    }
    return plan


# Closed structural table: provider frequency labels / SDMX codes -> period code.
# Normalization only (no semantic decision); anything unrecognized is None and
# callers fall back to their previous year-level behavior.
_FREQUENCY_WORD_CODES: Dict[str, str] = {
    "annual": "A", "annually": "A", "yearly": "A", "year": "A", "years": "A",
    "semiannual": "S", "semiannually": "S", "biannual": "S", "semester": "S",
    "half": "S", "halfyearly": "S",
    "quarterly": "Q", "quarter": "Q", "quarters": "Q",
    "monthly": "M", "month": "M", "months": "M",
    "weekly": "W", "week": "W", "weeks": "W",
    "daily": "D", "day": "D", "days": "D",
}
_FREQUENCY_LETTER_CODES: Dict[str, str] = {
    "A": "A", "Y": "A", "S": "S", "H": "S", "Q": "Q", "M": "M", "W": "W", "D": "D", "B": "D",
}
# Coarseness order (finest first) — used to pick the COARSER of two signals.
_FREQUENCY_RANK = {"D": 0, "W": 1, "M": 2, "Q": 3, "S": 4, "A": 5}


def _frequency_code(value: Any) -> Optional[str]:
    """Normalize a frequency label ("Monthly", "Quarterly, End of Period", "M",
    "semi-annual", "Weekly, Ending Friday") to D/W/M/Q/S/A, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 1:
        return _FREQUENCY_LETTER_CODES.get(text.upper())
    lowered = text.lower().replace("semi-annual", "semiannual").replace("half-year", "halfyearly")
    for token in re.findall(r"[a-z]+", lowered):
        code = _FREQUENCY_WORD_CODES.get(token)
        if code:
            return code
    return None


def _spacing_frequency_code(dates: List[date]) -> Optional[str]:
    """Frequency implied by the median spacing of observation dates (None if <2)."""
    ordered = sorted(set(dates))
    if len(ordered) < 2:
        return None
    gaps = sorted((b - a).days for a, b in zip(ordered, ordered[1:]))
    median = gaps[len(gaps) // 2]
    if median >= 300:
        return "A"
    if median >= 150:
        return "S"
    if median >= 80:
        return "Q"
    if median >= 25:
        return "M"
    if median >= 5:
        return "W"
    return "D"


def _series_period_code(series: Any, dates: List[date]) -> Optional[str]:
    """Observation period for a series: the COARSER of its labeled frequency
    and its observed date spacing (a mislabeled series can only widen the
    bucket, never wrongly drop boundary observations). None when the label is
    unknown -> callers keep year-level comparison."""
    metadata = getattr(series, "metadata", None)
    labeled = _frequency_code(getattr(metadata, "frequency", None) if metadata else None)
    if labeled is None:
        return None
    spaced = _spacing_frequency_code(dates)
    if spaced is not None and _FREQUENCY_RANK[spaced] > _FREQUENCY_RANK[labeled]:
        return spaced
    return labeled


def _observation_period(obs_date: date, period_code: Optional[str]) -> tuple[date, date]:
    """Calendar bucket CONTAINING ``obs_date`` at the series frequency.

    Containment (not date+length) is deliberate: providers stamp periods at
    the start (2026-10-01) OR the end (2026-10-31) — both lie in October.
    Weekly uses a +/-6-day span so week-start and week-ending stamps both
    cover their week. Unknown frequency -> the whole calendar year (the
    previous year-level convention).
    """
    if period_code == "D":
        return obs_date, obs_date
    if period_code == "W":
        return obs_date - timedelta(days=6), obs_date + timedelta(days=6)
    year = obs_date.year
    if period_code == "M":
        first_month = last_month = obs_date.month
    elif period_code == "Q":
        first_month = ((obs_date.month - 1) // 3) * 3 + 1
        last_month = first_month + 2
    elif period_code == "S":
        first_month = 1 if obs_date.month <= 6 else 7
        last_month = first_month + 5
    else:
        first_month, last_month = 1, 12
    last_day = calendar.monthrange(year, last_month)[1]
    return date(year, first_month, 1), date(year, last_month, last_day)


def _window_bound(value: Any, *, is_end: bool) -> Optional[date]:
    """Parse a window bound (ISO date, YYYY-MM, YYYY, or int year).

    Never LOOSER than the previous year-level rule: a bound whose shape is
    not ISO ("2020-Q1", "2020/01/01") or whose month is out of range still
    yields its year's Jan-1 / Dec-31, and an out-of-range day ("2026-02-30")
    is clamped to the month's last day — so one malformed bound can never
    silently drop that side of the window. None only when no leading year.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    year_bound = None
    if len(text) >= 4 and text[:4].isdigit() and (len(text) == 4 or not text[4].isdigit()):
        year_bound = date(int(text[:4]), 12, 31) if is_end else date(int(text[:4]), 1, 1)
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:[Tt ].*)?", text)
    if not match:
        return year_bound
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else (12 if is_end else 1)
    if not 1 <= month <= 12:
        return year_bound
    last_day = calendar.monthrange(year, month)[1]
    if match.group(3):
        day = min(max(int(match.group(3)), 1), last_day)
    else:
        day = last_day if is_end else 1
    return date(year, month, day)


def _requested_window(params: dict[str, Any]) -> tuple[Optional[date], Optional[date]]:
    start = _window_bound(params.get("startDate"), is_end=False) or _window_bound(
        params.get("start_year"), is_end=False
    )
    end = _window_bound(params.get("endDate"), is_end=True) or _window_bound(
        params.get("end_year"), is_end=True
    )
    return start, end


def _observation_overlaps_window(
    obs_date: date,
    period_code: Optional[str],
    start: Optional[date],
    end: Optional[date],
) -> bool:
    """Keep an observation iff its calendar period overlaps [start, end].

    For year-aligned windows this is identical to the old year-level rule at
    every frequency; for sub-annual windows (Q4 2026, 2026-11, "last 3
    months") other periods of the same year no longer count as in-window.
    """
    obs_start, obs_end = _observation_period(obs_date, period_code)
    if start is not None and obs_end < start:
        return False
    if end is not None and obs_start > end:
        return False
    return True


def _point_iso_date(raw: Any) -> Optional[date]:
    from ..providers._sdmx.periods import period_to_iso_date

    normalized = period_to_iso_date(str(raw or ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[Tt ].+)?", normalized):
        return None
    try:
        return datetime.fromisoformat(normalized[:10]).date()
    except ValueError:
        return None


def _point_field(point: Any, key: str) -> Any:
    return point.get(key) if isinstance(point, dict) else getattr(point, key, None)


def _series_period_for_points(series: Any) -> Optional[str]:
    dates = [d for d in (_point_iso_date(_point_field(p, "date")) for p in (series.data or [])) if d]
    return _series_period_code(series, dates)


def _requested_frequency_code(params: dict[str, Any]) -> Optional[str]:
    """Requested frequency as a period code, from structured signals only.

    1. ``params['frequency']`` — the LLM-parsed / state-carried slot (delta
       frequency changes, carried state, clarification carries).
    2. ``params['__frequency_mismatch']['requested_frequency']`` — the
       selector's annotation on a pick KEPT despite a frequency-only conflict
       (the first-turn parse has no frequency slot, so without this the kept
       daily-for-monthly pick would be served silently). Comma-joined when
       several were requested: used only when they normalize to ONE code.
    """
    code = _frequency_code(params.get("frequency"))
    if code:
        return code
    mismatch = params.get("__frequency_mismatch")
    if not isinstance(mismatch, dict):
        return None
    codes = {
        c for c in (
            _frequency_code(token)
            for token in str(mismatch.get("requested_frequency") or "").split(",")
        ) if c
    }
    return next(iter(codes)) if len(codes) == 1 else None


def _disclose_frequency_mismatch(
    result: List[NormalizedData], params: dict[str, Any], language: Optional[str],
) -> List[NormalizedData]:
    """Disclose when the SERVED frequency differs from the requested one.

    One post-fetch reconciliation point (fresh fetches AND cache hits, every
    substitution path: kept frequency-conflict picks, exact-code locks,
    provider fallbacks, annual-only mirrors). Compares the structured
    requested frequency (``_requested_frequency_code``) with each series'
    SERVED Metadata.frequency via the closed code table; unknown on either
    side -> no note. Returns a
    list where annotated series are COPIES, so cached provider objects never
    accumulate per-request (per-language) notes. Idempotent.
    """
    requested = _requested_frequency_code(params)
    if not requested or not result:
        return result
    disclosed: List[NormalizedData] = []
    for series in result:
        metadata = getattr(series, "metadata", None)
        native = _frequency_code(getattr(metadata, "frequency", None)) if metadata is not None else None
        if not native or native == requested:
            disclosed.append(series)
            continue
        note = _localized_message(
            "frequency_mismatch_disclosure",
            language,
            requested=_localized_message(f"frequency_label_{requested}", language)
            or str(params.get("frequency")).strip(),
            native=_localized_message(f"frequency_label_{native}", language)
            or str(metadata.frequency).strip(),
        )
        existing = list(metadata.notes or [])
        if not note or note in existing:
            disclosed.append(series)
            continue
        annotated = series.model_copy(deep=True)
        annotated.metadata.notes = existing + [note]
        disclosed.append(annotated)
    return disclosed


def _enforce_user_time_window(
    params: dict[str, Any], original_query: str, result: List[NormalizedData]
) -> List[NormalizedData]:
    """Honesty filter: when the USER named a time window, never serve
    observations outside it.

    Observed live: "GDP of Tuvalu since 2030" produced startDate=2030 with a
    defaulted endDate in the past (inverted range), the provider's date param
    fell through, and the FULL 56-point history was served as a silent
    success. Same class: a frozen series (data ends 2019) served for
    "last 3 months". Membership is CALENDAR-PERIOD OVERLAP (shared with
    _classify_time_window_probe via _observation_overlaps_window): a point is
    kept iff the calendar bucket containing its date at the series frequency
    (the coarser of the labeled frequency and the observed date spacing)
    overlaps the window — so an annual 2021 point stays for a window starting
    2021-07-18, while other quarters/months of the same year no longer count
    for "Q4 2026" / "2026-11". Unknown frequency falls back to the previous
    YEAR-level rule. Applies ONLY when the user explicitly set the scope
    (_user_set_time_scope); provider-default windows keep their existing
    edge behavior. An all-dropped result flows into the standard
    no-data-for-period path, which the response finalizer explains in the
    user's language.
    """
    if not result or not _user_set_time_scope(params, original_query):
        return result

    def _year(value: Any) -> Optional[int]:
        text = str(value or "")[:4]
        return int(text) if text.isdigit() else None

    start_year = _year(params.get("startDate")) or _year(params.get("start_year"))
    end_year = _year(params.get("endDate")) or _year(params.get("end_year"))
    if start_year is None and end_year is None:
        return result
    window_start, window_end = _requested_window(params)

    filtered: List[NormalizedData] = []
    dropped_total = 0
    for series in result:
        points = list(series.data or [])
        period_code = _series_period_for_points(series)
        kept = []
        for point in points:
            point_date = _point_field(point, "date")
            year = _year(point_date)
            if year is None:
                kept.append(point)
                continue
            obs_date = _point_iso_date(point_date)
            if obs_date is not None and (window_start is not None or window_end is not None):
                # Period-overlap semantics (shared with the window probe).
                if _observation_overlaps_window(obs_date, period_code, window_start, window_end):
                    kept.append(point)
                continue
            if start_year is not None and year < start_year:
                continue
            if end_year is not None and year > end_year:
                continue
            kept.append(point)
        dropped_total += len(points) - len(kept)
        if kept:
            series.data = kept
            filtered.append(series)
    if dropped_total:
        logger.info(
            "Time-window honesty filter dropped %d observation(s) outside the "
            "user-requested window [%s..%s] (%d/%d series kept)",
            dropped_total, start_year, end_year, len(filtered), len(result),
        )
    return filtered


def _classify_time_window_probe(
    params: dict[str, Any], result: List[NormalizedData],
) -> tuple[List[NormalizedData], str]:
    """Copy provable in-window observations and classify known outside-only data.

    Unknown dates/nonfinite values cannot establish absence. Null values are
    missing observations. In-window = the observation's calendar period (at
    the series frequency) overlaps the requested window — the same predicate
    as _enforce_user_time_window; year-level when the frequency is unknown.
    """
    from ..providers._sdmx.periods import period_to_iso_date

    def observation_date(value):
        normalized = period_to_iso_date(value)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[Tt ].+)?", normalized):
            return None
        try:
            return datetime.fromisoformat(normalized).date()
        except ValueError:
            return None

    start, end = _requested_window(params)
    if start is None and end is None:
        return [], ""
    recovered = []
    latest = ""
    inconclusive = False
    for original in result or []:
        series = original.model_copy(deep=True)
        period_code = _series_period_for_points(series)
        kept = []
        for point in series.data or []:
            value = point.get("value") if isinstance(point, dict) else point.value
            if value is None:
                continue
            date = observation_date(point.get("date", "") if isinstance(point, dict) else point.date)
            try:
                finite = math.isfinite(value)
            except (TypeError, ValueError, OverflowError):
                finite = False
            if date is None or not finite:
                inconclusive = True
                continue
            latest = max(latest, date.isoformat())
            if _observation_overlaps_window(date, period_code, start, end):
                kept.append(point)
        if kept:
            series.data = kept
            recovered.append(series)
    return recovered, "" if inconclusive else latest


def _statscan_semantic_label(params: dict[str, Any], intent: Any, base: str = "") -> str:
    """Pick the SEMANTIC indicator label for StatsCan dimension-member matching.

    After selection, ``intent.indicators[0]`` frequently holds the resolved
    PRODUCT/VECTOR ID (e.g. "14100375") — the prefetch-resolution path installs
    the code without the ``__semantic_indicator_label`` param. A bare id
    carries zero semantic signal, so member matching for non-geography
    dimensions silently degraded to the first member (observed live: Ontario
    "unemployment rate" fetched the Population member because the label was
    digits). Structural shape check only (id-shaped strings skipped), never a
    semantic judgment — the label's meaning stays with the LLM layer.
    """
    def _is_semantic(text: Any) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        return not re.fullmatch(r"v?\d{5,10}", value.replace("-", ""))

    candidates = [
        params.get("__semantic_indicator_label"),
        params.get("__display_indicator_label"),
        str(intent.indicators[0]) if getattr(intent, "indicators", None) else "",
        str(getattr(intent, "originalQuery", "") or ""),
        base,
    ]
    for candidate in candidates:
        if _is_semantic(candidate):
            return str(candidate).strip()
    return str(base or "")


def _statscan_periods_from_date_range(params: dict[str, Any], default: int) -> int:
    explicit_periods = params.get("periods")
    if explicit_periods is not None:
        try:
            return int(explicit_periods)
        except (TypeError, ValueError):
            pass

    start_date = str(params.get("startDate") or "").strip()
    if not start_date:
        return default

    end_date = str(params.get("endDate") or "").strip() or datetime.utcnow().date().isoformat()
    try:
        start_dt = datetime.fromisoformat(start_date[:10])
        end_dt = datetime.fromisoformat(end_date[:10])
    except ValueError:
        return default

    months = max(1, ((end_dt.year - start_dt.year) * 12) + (end_dt.month - start_dt.month) + 1)
    return max(default, months)


# ---------------------------------------------------------------------------
# CoinGecko fetcher
# ---------------------------------------------------------------------------

async def fetch_from_coingecko(
    coingecko_provider: Any,
    intent: ParsedIntent,
    params: dict,
    execution_plan: Optional[ExecutionPlan] = None,
) -> list:
    """Fetch cryptocurrency data from CoinGecko.

    Handles:
    - Coin ID mapping (ticker symbols -> CoinGecko IDs)
    - Time period extraction from query text
    - Historical data (date range or days)
    - Current price/market_cap/volume/24h_change
    - Top N rankings by market cap

    Returns list of NormalizedData.
    """
    logger.info("CoinGecko Query Parameters:")
    logger.info(f"   - Indicators: {intent.indicators}")
    coingecko_request = dict((execution_plan.provider_request or {}) if execution_plan else {})
    default_time_range_applied = str(params.pop("__default_time_range_applied", "") or "").strip()
    if coingecko_request.get("start_date") and not params.get("startDate"):
        params["startDate"] = coingecko_request["start_date"]
    if coingecko_request.get("end_date") and not params.get("endDate"):
        params["endDate"] = coingecko_request["end_date"]
    if coingecko_request.get("days") and not params.get("days"):
        params["days"] = coingecko_request["days"]

    query_lower = intent.originalQuery.lower() if intent.originalQuery else ""
    has_historical_time_scope = _coingecko_has_historical_time_scope(query_lower)
    if default_time_range_applied == "coingecko_30d" and not has_historical_time_scope:
        params.pop("startDate", None)
        params.pop("endDate", None)

    # Extract time periods from query text
    mentions_time = has_historical_time_scope

    days_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+days?', query_lower)
    weeks_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+weeks?', query_lower)
    months_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+months?', query_lower)
    year_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+years?', query_lower)

    if not params.get("days"):
        extracted_days = None
        if days_match:
            extracted_days = int(days_match.group(1))
        elif weeks_match:
            extracted_days = int(weeks_match.group(1)) * 7
        elif months_match:
            extracted_days = int(months_match.group(1)) * 30
        elif year_match:
            extracted_days = int(year_match.group(1)) * 365
        elif mentions_time and not default_time_range_applied:
            extracted_days = 30
        if extracted_days:
            params["days"] = extracted_days
            params.pop("startDate", None)
            params.pop("endDate", None)

    # Parse coin IDs from params
    raw_coin_ids = coingecko_request.get("coin_ids")
    if not raw_coin_ids:
        raw_coin_ids = params.get("coinIds")
    if isinstance(raw_coin_ids, list):
        coin_ids = [str(cid).strip() for cid in raw_coin_ids if str(cid).strip()]
    elif isinstance(raw_coin_ids, str):
        coin_ids = [p.strip() for p in raw_coin_ids.split(",") if p.strip()]
    else:
        coin_ids = []
    if (
        not coin_ids
        and params.get("__exact_provider_code_match")
        and re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,127}", str(params.get("indicator") or ""))
    ):
        # Exact provider-code parser already verified this slug against the
        # local CoinGecko catalog.  Preserve it as the provider-native id so
        # generic fallback detection cannot silently fetch bitcoin.
        coin_ids = [str(params["indicator"]).strip().lower()]

    # Sanitize vs_currency
    raw_vs = str(coingecko_request.get("vs_currency") or params.get("vsCurrency") or "usd").strip().lower()
    invalid_tokens = {"right", "now", "today", "current", "recent", "latest",
                      "trend", "performance", "history", "historical"}
    vs_currency = raw_vs if raw_vs not in invalid_tokens and re.fullmatch(r"[a-z]{3,10}", raw_vs) else "usd"
    params["vsCurrency"] = vs_currency

    # Coin name -> CoinGecko ID mapping
    coin_map = {
        "bitcoin": "bitcoin", "btc": "bitcoin",
        "ethereum": "ethereum", "eth": "ethereum",
        "solana": "solana", "sol": "solana",
        "cardano": "cardano", "ada": "cardano",
        "polkadot": "polkadot", "dot": "polkadot",
        "avalanche": "avalanche-2", "avax": "avalanche-2",
        "polygon": "matic-network", "matic": "matic-network",
        "chainlink": "chainlink", "link": "chainlink",
        "uniswap": "uniswap", "uni": "uniswap",
        "dogecoin": "dogecoin", "doge": "dogecoin",
        "shiba": "shiba-inu", "shib": "shiba-inu",
        "ripple": "ripple", "xrp": "ripple",
        "binance": "binancecoin", "bnb": "binancecoin",
        "litecoin": "litecoin", "ltc": "litecoin",
        "tron": "tron", "trx": "tron",
        "stellar": "stellar", "xlm": "stellar",
        "cosmos": "cosmos", "atom": "cosmos",
        "near": "near", "nearprotocol": "near",
        "algorand": "algorand", "algo": "algorand",
    }

    # Map provided coin IDs
    if coin_ids:
        coin_ids = [coin_map.get(c.lower(), c) for c in coin_ids]
    else:
        # Auto-detect from indicators
        for indicator in (intent.indicators or []):
            ind_lower = indicator.lower().replace(" ", "")
            for name, cid in coin_map.items():
                if name in ind_lower:
                    coin_ids.append(cid)
                    break

        # Fallback: check query text
        if not coin_ids:
            for name, cid in coin_map.items():
                if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", query_lower):
                    if cid not in coin_ids:
                        coin_ids.append(cid)
            if not coin_ids:
                coin_ids = ["bitcoin"]

    # Remove duplicates while preserving order so comparison follow-ups do not
    # duplicate series for the same asset.
    coin_ids = list(dict.fromkeys(coin_ids))

    logger.info(f"   - Resolved coins: {coin_ids}, vs={vs_currency}")

    # The measure (price / market cap / volume / change) comes ONLY from the
    # shared structured decision (parse enum -> LLM indicator labels -> price),
    # the same one the provider_request contract keys the cache on.
    requested_metric = _coingecko_requested_metric(intent, params)
    provider_metric = _COINGECKO_METRIC_ENUM[requested_metric]

    # Historical data request
    if params.get("startDate") or params.get("endDate") or params.get("days"):
        hist_metric = provider_metric if provider_metric in _COINGECKO_HISTORICAL_METRICS else "price"

        if params.get("startDate") and params.get("endDate"):
            series_list = []
            for coin_id in coin_ids:
                data = await coingecko_provider.get_historical_data_range(
                    coin_id=coin_id, vs_currency=vs_currency,
                    from_date=params["startDate"], to_date=params["endDate"],
                    metric=hist_metric,
                )
                series_list.extend(data)
        else:
            days = params.get("days", 30)
            series_list = []
            for coin_id in coin_ids:
                data = await coingecko_provider.get_historical_data(
                    coin_id=coin_id, vs_currency=vs_currency,
                    metric=hist_metric, days=days,
                )
                series_list.extend(data)
        if hist_metric != provider_metric:
            # CoinGecko has no historical change series: disclose the
            # price substitution instead of silently ignoring the request.
            for series in series_list or []:
                meta = getattr(series, "metadata", None)
                if meta is not None:
                    meta.notes = (meta.notes or []) + [
                        "CoinGecko publishes no historical price-change series; "
                        "showing the price history for the requested window instead."
                    ]
        return series_list

    # Current data. The ranking TRIGGER is the pre-existing cue check (kept
    # unchanged); it never decides the MEASURE — that is the enum above, so a
    # ranking by market cap or by volume uses CoinGecko's native order.
    indicator_lower = " ".join(intent.indicators).lower() if intent.indicators else ""
    ranking_cue_text = f"{indicator_lower} {query_lower}".strip()
    ranking_keywords = ["top", "top 10", "top 5", "top 20", "ranking", "rankings", "largest", "biggest"]
    is_ranking = any(t in ranking_cue_text for t in ranking_keywords)

    if is_ranking and requested_metric in {"market_cap", "volume"}:
        top_n_match = _TOP_N_RE.search(query_lower)
        requested_n = int(top_n_match.group(1)) if top_n_match else 10
        per_page = max(1, min(250, requested_n))
        result = await coingecko_provider.get_market_data(
            vs_currency=vs_currency, order=f"{requested_metric}_desc", per_page=per_page,
        )
        if requested_n > per_page:
            # Disclose the API's page cap instead of silently serving fewer
            # entries than requested ("top 300" returned 250 with no note).
            for series in result or []:
                meta = getattr(series, "metadata", None)
                if meta is not None:
                    note = (
                        f"CoinGecko returns at most {per_page} entries per "
                        f"ranking; the requested top {requested_n} was truncated."
                    )
                    meta.notes = (meta.notes or []) + [note]
        return result

    return await coingecko_provider.get_simple_price(
        coin_ids=coin_ids, vs_currency=vs_currency, metric=provider_metric,
    )


# ---------------------------------------------------------------------------
# Exchange rate fetchers
# ---------------------------------------------------------------------------

async def fetch_exchange_rate_with_historical_fallback(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
) -> list:
    """Fetch exchange rate data, falling back to FRED for historical requests.

    The ExchangeRate-API free tier only supports current rates.
    For historical data, we fall back to FRED which has daily exchange
    rate series for 21 major currency pairs.

    Returns list of NormalizedData.
    Raises DataNotAvailableError if neither source can serve the request.
    """
    from datetime import datetime, timedelta

    logger.info("ExchangeRate Query Parameters:")
    logger.info(f"   - baseCurrency: {params.get('baseCurrency', 'USD')}")
    logger.info(f"   - targetCurrency: {params.get('targetCurrency')}")
    logger.info(f"   - startDate: {params.get('startDate')}")

    # Detect if user is requesting historical data.
    # Rely on LLM-provided startDate/endDate params instead of regex
    # pattern matching on query text -- the LLM handles semantic intent.
    has_historical = False
    start_date = params.get("startDate")
    end_date = params.get("endDate")
    if start_date or end_date:
        try:
            today = datetime.now().date()
            week_ago = today - timedelta(days=7)
            if start_date:
                start_dt = datetime.fromisoformat(start_date[:10]).date()
                if start_dt < week_ago:
                    has_historical = True
            if end_date and not has_historical:
                end_dt = datetime.fromisoformat(end_date[:10]).date()
                if end_dt < today - timedelta(days=1):
                    has_historical = True
        except (ValueError, AttributeError):
            pass

    if has_historical:
        logger.warning("ExchangeRate: Historical data requested -- falling back to FRED")
        result = await fetch_historical_exchange_from_fred(svc, intent, params)
        if result:
            return result
        raise DataNotAvailableError(
            "Historical exchange rate data is not available with the free ExchangeRate API tier. "
            "\n\n**Alternatives:**\n"
            "1. For **current rates**: Rephrase without time references\n"
            "2. For **historical rates**: Use a paid ExchangeRate API key\n"
            "3. For **Real Effective Exchange Rate** (REER): Ask for 'REER' (uses IMF data)\n\n"
            "Note: Some bilateral exchange rates are available via FRED for major currency pairs."
        )

    # Current rate -- use ExchangeRate-API
    series = await svc.exchangerate_provider.fetch_exchange_rate(
        base_currency=params.get("baseCurrency", "USD"),
        target_currency=params.get("targetCurrency"),
        target_currencies=params.get("targetCurrencies"),
    )
    return [series]


async def fetch_historical_exchange_from_fred(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
) -> Optional[list]:
    """Attempt to fetch historical exchange rate from FRED.

    FRED has daily exchange rate series for 21 major currency pairs.
    Returns list of NormalizedData on success, None if currency not supported.
    """
    base_currency = params.get("baseCurrency", "USD")
    target_currency = params.get("targetCurrency")

    if not target_currency:
        query_upper = (intent.originalQuery or "").upper()
        to_match = _CURRENCY_TO_RE.search(query_upper)
        slash_match = re.search(r'\b([A-Z]{3})[/\s](?:VS\s)?([A-Z]{3})\b', query_upper)
        if to_match:
            base_currency, target_currency = to_match.group(1), to_match.group(2)
        elif slash_match:
            base_currency, target_currency = slash_match.group(1), slash_match.group(2)

    if not target_currency:
        return None

    # FRED USD-based exchange rate series
    fred_fx = {
        "EUR": "DEXUSEU", "GBP": "DEXUSUK", "JPY": "DEXJPUS",
        "CAD": "DEXCAUS", "CHF": "DEXSZUS", "AUD": "DEXUSAL",
        "CNY": "DEXCHUS", "MXN": "DEXMXUS", "INR": "DEXINUS",
        "BRL": "DEXBZUS", "KRW": "DEXKOUS", "SEK": "DEXSDUS",
        "NOK": "DEXNOUS", "DKK": "DEXDNUS", "SGD": "DEXSIUS",
        "HKD": "DEXHKUS", "NZD": "DEXUSNZ", "ZAR": "DEXSFUS",
        "THB": "DEXTHUS", "MYR": "DEXMAUS", "TWD": "DEXTAUS",
    }

    target_upper = target_currency.upper()
    base_upper = base_currency.upper()

    # Every FRED series here is USD-based, so this fallback can only serve a
    # pair where exactly ONE leg is USD. The old code also fired when NEITHER
    # leg was USD and returned a single USD leg (e.g. DEXUSUK, USD/GBP)
    # relabeled as the requested cross rate ("EUR to GBP") — wrong data shown as
    # success. A cross rate is not a single FRED series, so fail closed and let
    # the caller surface "historical FX not available for this pair".
    fred_series_id = None
    if base_upper == "USD" and target_upper in fred_fx:
        fred_series_id = fred_fx[target_upper]
    elif target_upper == "USD" and base_upper in fred_fx:
        fred_series_id = fred_fx[base_upper]

    if not fred_series_id:
        return None

    try:
        series = await svc.fred_provider.fetch_series({
            "seriesId": fred_series_id,
            "startDate": params.get("startDate"),
            "endDate": params.get("endDate"),
        })
    except Exception as e:
        logger.warning(f"FRED exchange rate fallback failed: {e}")
        return None
    # The table above only says WHICH currency a series covers, never its quote
    # direction (FRED mixes "U.S. Dollars to One Euro" with "Swiss Francs to One
    # U.S. Dollar"). Orientation is read from the served series' own units text.
    return [orient_fx_series(series, base_upper, target_upper)]


# ---------------------------------------------------------------------------
# FX quote orientation (structural: derived from the served series' units text)
# ---------------------------------------------------------------------------

# FRED FX units are uniformly "<NUMERATOR> to One <DENOMINATOR>", i.e. the value
# is NUMERATOR units per one DENOMINATOR unit.
_FX_UNITS_QUOTE_RE = re.compile(r"^\s*(?P<num>.+?)\s+to\s+one\s+(?P<den>.+?)\s*$", re.IGNORECASE)


def _is_us_dollar_leg(text: str) -> bool:
    """Exact match of a units-text leg against the U.S. dollar.

    Exact (not substring) on purpose: "Hong Kong Dollars", "Canadian Dollars",
    "Singapore Dollars", "Taiwan Dollars", "Australian Dollar" all contain
    "Dollar" and must NOT read as the USD leg.
    """
    norm = re.sub(r"\s+", " ", (text or "").replace(".", "")).strip().lower()
    return norm in {"us dollar", "us dollars"}


def parse_fx_quote_orientation(units: Optional[str]) -> Optional[str]:
    """Return ``"usd_per_foreign"``, ``"foreign_per_usd"`` or None (unparseable).

    None means the units text does not state a single USD leg in the
    "<A> to One <B>" form — callers must then NOT assert a direction.
    """
    match = _FX_UNITS_QUOTE_RE.match(str(units or ""))
    if not match:
        return None
    num_is_usd = _is_us_dollar_leg(match.group("num"))
    den_is_usd = _is_us_dollar_leg(match.group("den"))
    if num_is_usd == den_is_usd:
        return None
    return "usd_per_foreign" if num_is_usd else "foreign_per_usd"


def orient_fx_series(series: Any, base: str, target: str) -> Any:
    """Return ``series`` expressed as ``target`` units per 1 ``base`` (one leg USD).

    The served object is never mutated (a deep copy is returned) so a cached or
    shared fetch result cannot be re-oriented twice. Behaviour by the native
    quote parsed from ``metadata.unit``:

    - matches base->target: relabel ``"<base> to <target> Exchange Rate"``;
    - opposite: invert every point (1/v; None and 0 become None, dates kept),
      unit ``"<target> per 1 <base>"``, description prefixed with the native
      quote it was inverted from, note naming the FRED id + native units;
    - unparseable: keep FRED's native title and unit, add a disclosure note —
      never assert a direction that was not verified.
    """
    base = str(base or "").upper()
    target = str(target or "").upper()
    if hasattr(series, "model_copy"):
        oriented = series.model_copy(deep=True)
    else:
        oriented = copy.deepcopy(series)
    metadata = oriented.metadata
    metadata.source = "FRED (Federal Reserve)"
    native_units = str(getattr(metadata, "unit", "") or "")
    series_id = getattr(metadata, "seriesId", None) or "series"
    notes = list(getattr(metadata, "notes", None) or [])

    if base == "USD" and target != "USD":
        wanted = "foreign_per_usd"
    elif target == "USD" and base != "USD":
        wanted = "usd_per_foreign"
    else:
        wanted = None
    native = parse_fx_quote_orientation(native_units)

    if wanted is None or native is None:
        notes.append(
            f"Quote direction of FRED {series_id} could not be verified from its units "
            f"({native_units or 'none'}); shown in FRED's native quote, not as {base} to {target}."
        )
        metadata.notes = notes
        return oriented

    metadata.indicator = f"{base} to {target} Exchange Rate"
    if native == wanted:
        return oriented

    for point in oriented.data:
        value = getattr(point, "value", None)
        try:
            point.value = (1.0 / float(value)) if value not in (None, 0) else None
        except (TypeError, ValueError, ZeroDivisionError):
            point.value = None
    metadata.unit = f"{target} per 1 {base}"
    # The rewritten unit uses ISO codes, so without this the native quote text
    # (the only served text naming both currencies) would vanish from every
    # field the post-fetch relevance gate and LLM judge read, and a correct,
    # inverted answer to "Swiss franc to US dollar" gets a clarification menu.
    # It still describes the SERVED object: what was fetched and how it was
    # transformed — never the request's words.
    native_description = str(getattr(metadata, "description", "") or "").strip()
    metadata.description = " ".join(
        part for part in (f"Inverse (1/x) of FRED {series_id} ({native_units}).", native_description) if part
    )
    notes.append(f"Inverted (1/x) from FRED {series_id} ({native_units}).")
    metadata.notes = notes
    return oriented


# ---------------------------------------------------------------------------
# Exchange rate parameter extraction
# ---------------------------------------------------------------------------

def extract_exchange_rate_params(params: dict, intent: ParsedIntent) -> dict:
    """Extract currency pair information from query and populate params.

    CRITICAL: This must be called BEFORE cache lookup to ensure each unique
    currency pair has its own cache entry. Without this, different currency
    queries could share the same incorrect cached data.

    Args:
        params: Current query parameters
        intent: Parsed intent with originalQuery

    Returns:
        Updated params with baseCurrency and targetCurrency populated
    """
    # If params already has both currencies, use them
    if params.get("baseCurrency") and params.get("targetCurrency"):
        logger.info(f"Currency params already set: {params.get('baseCurrency')} -> {params.get('targetCurrency')}")
        return params

    params = {**params}  # Create a copy to avoid mutation

    # Currency code mapping for common names/symbols
    currency_name_map = {
        "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD",
        "euro": "EUR", "euros": "EUR", "eur": "EUR",
        "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "sterling": "GBP", "british pound": "GBP",
        "yen": "JPY", "jpy": "JPY", "japanese yen": "JPY",
        "yuan": "CNY", "cny": "CNY", "renminbi": "CNY", "rmb": "CNY", "chinese yuan": "CNY",
        "franc": "CHF", "chf": "CHF", "swiss franc": "CHF",
        "rupee": "INR", "inr": "INR", "indian rupee": "INR",
        "won": "KRW", "krw": "KRW", "korean won": "KRW",
        "real": "BRL", "brl": "BRL", "brazilian real": "BRL",
        "ruble": "RUB", "rub": "RUB", "russian ruble": "RUB",
        "peso": "MXN", "mxn": "MXN", "mexican peso": "MXN",
        "rand": "ZAR", "zar": "ZAR", "south african rand": "ZAR",
        "lira": "TRY", "try": "TRY", "turkish lira": "TRY",
        "canadian dollar": "CAD", "cad": "CAD", "loonie": "CAD",
        "australian dollar": "AUD", "aud": "AUD", "aussie dollar": "AUD",
        "singapore dollar": "SGD", "sgd": "SGD",
        "hong kong dollar": "HKD", "hkd": "HKD",
        "new zealand dollar": "NZD", "nzd": "NZD", "kiwi dollar": "NZD",
    }

    query_text = (intent.originalQuery or "").upper()

    # Extract currency codes using various patterns
    base_currency = params.get("baseCurrency")
    target_currency = params.get("targetCurrency")

    # Pattern 0: preserve an already materialized currency-pair dimension from
    # multiround state, e.g. {"Currency Pair": "USD to CHF"}.
    # This is critical for short follow-ups like "show only the last 30 days"
    # where the raw query contains no currency codes.
    if not base_currency or not target_currency:
        dimension_pair = (
            (params.get("__dimensions") or {}).get("Currency Pair")
            or (params.get("dimensions") or {}).get("Currency Pair")
        )
        if dimension_pair:
            normalized_pair = str(dimension_pair).upper().strip()
            pair_match = _CURRENCY_TO_RE.search(normalized_pair) or _CURRENCY_SLASH_RE.search(normalized_pair)
            if pair_match:
                base_currency = pair_match.group(1)
                target_currency = pair_match.group(2)
                logger.info(
                    "Extracted currency pair from preserved dimensions: %s -> %s",
                    base_currency,
                    target_currency,
                )

    # Pattern 1: "X to Y" (e.g., "USD to EUR", "JPY to USD")
    to_match = _CURRENCY_TO_RE.search(query_text)
    if to_match:
        base_currency = to_match.group(1)
        target_currency = to_match.group(2)
        logger.info(f"Extracted from 'X to Y' pattern: {base_currency} -> {target_currency}")

    # Pattern 2: "X/Y" or "X-Y" (e.g., "USD/EUR", "EUR-GBP")
    if not base_currency or not target_currency:
        slash_match = _CURRENCY_SLASH_RE.search(query_text)
        if slash_match:
            base_currency = slash_match.group(1)
            target_currency = slash_match.group(2)
            logger.info(f"Extracted from 'X/Y' pattern: {base_currency} -> {target_currency}")

    # Pattern 3: "X vs Y" (e.g., "USD vs EUR")
    if not base_currency or not target_currency:
        vs_match = _CURRENCY_VS_RE.search(query_text)
        if vs_match:
            base_currency = vs_match.group(1)
            target_currency = vs_match.group(2)
            logger.info(f"Extracted from 'X vs Y' pattern: {base_currency} -> {target_currency}")

    # Pattern 4: Try to find any currency codes in the query
    if not base_currency or not target_currency:
        # Look for 3-letter currency codes
        all_codes = _CURRENCY_CODE_RE.findall(query_text)
        # Filter to known currency codes
        valid_codes = {"USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD",
                      "INR", "KRW", "BRL", "MXN", "ZAR", "TRY", "SGD", "HKD",
                      "NZD", "SEK", "NOK", "DKK", "THB", "MYR", "TWD", "RUB"}
        found_codes = [c for c in all_codes if c in valid_codes]
        if len(found_codes) >= 2 and not base_currency:
            base_currency = found_codes[0]
            target_currency = found_codes[1]
            logger.info(f"Extracted from code search: {base_currency} -> {target_currency}")
        elif len(found_codes) == 1:
            # Single 3-letter code found. Before defaulting the other operand,
            # check for a DIFFERENT currency referenced by NAME (e.g. "euro to USD",
            # "canadian dollar to USD") and pair them in query order — otherwise the
            # named counterpart is silently dropped in favour of the EUR/USD default.
            code = found_codes[0]
            name_lower = (intent.originalQuery or query_text or "").lower()
            named_code = None
            named_pos = None
            for nm, ncode in currency_name_map.items():
                if ncode == code:
                    continue
                pos = name_lower.find(nm)
                if pos != -1 and (named_pos is None or pos < named_pos):
                    named_code, named_pos = ncode, pos
            if named_code:
                code_pos = name_lower.find(code.lower())
                if code_pos == -1:
                    code_pos = len(name_lower) + 1
                if named_pos < code_pos:
                    base_currency, target_currency = named_code, code
                else:
                    base_currency, target_currency = code, named_code
            elif code == "USD":
                # Query is about USD, but we need a target; default to EUR.
                base_currency = "USD"
                target_currency = params.get("targetCurrency") or "EUR"
            else:
                # Other currency to USD
                base_currency = code
                target_currency = "USD"
            logger.info(f"Single code found: {base_currency} -> {target_currency}")

    # Pattern 5: Try common currency names in lowercase query
    if not base_currency or not target_currency:
        query_lower = (intent.originalQuery or "").lower()
        found_currencies: List[tuple] = []
        for name, code in currency_name_map.items():
            if name in query_lower:
                if code not in [c[1] for c in found_currencies]:
                    # Find position for ordering
                    pos = query_lower.find(name)
                    found_currencies.append((pos, code))
        # Sort by position in query
        found_currencies.sort(key=lambda x: x[0])
        if len(found_currencies) >= 2:
            base_currency = found_currencies[0][1]
            target_currency = found_currencies[1][1]
            logger.info(f"Extracted from currency names: {base_currency} -> {target_currency}")
        elif len(found_currencies) == 1:
            code = found_currencies[0][1]
            if code == "USD":
                base_currency = "USD"
                target_currency = params.get("targetCurrency") or "EUR"
            else:
                base_currency = code
                target_currency = "USD"
            logger.info(f"Single currency name found: {base_currency} -> {target_currency}")

    # Pattern 6: derive from the parsed COUNTRY when the text carried no pair.
    # "Brazil exchange rate" has no currency token, so without this it would
    # default to USD->EUR. Map the country to its currency and quote it against
    # USD (the natural reading of "<country> exchange rate"). Skipped when the
    # country's currency is USD itself, leaving the USD->EUR default to stand.
    if not base_currency and not target_currency:
        country = str(params.get("country") or "").strip().lower()
        country_currency = _COUNTRY_TO_CURRENCY.get(country)
        if country_currency and country_currency != "USD":
            base_currency = "USD"
            target_currency = country_currency
            logger.info(
                f"Derived currency from country '{country}': USD -> {country_currency}"
            )

    # Apply defaults if still not found
    if not base_currency:
        base_currency = "USD"
        logger.info("Defaulting baseCurrency to USD")
    if not target_currency:
        # Default to EUR if base is USD, otherwise to USD
        target_currency = "EUR" if base_currency == "USD" else "USD"
        logger.info(f"Defaulting targetCurrency to {target_currency}")

    params["baseCurrency"] = base_currency
    params["targetCurrency"] = target_currency

    return params


# ---------------------------------------------------------------------------
# Provider dispatch — the large switch that routes to individual providers
# ---------------------------------------------------------------------------

async def fetch_from_provider_dispatch(
    svc: Any,
    intent: ParsedIntent,
    execution_plan: ExecutionPlan,
) -> List[NormalizedData]:
    """Route to the correct provider and call its API.

    This is the core provider-dispatch switch extracted from the nested
    ``fetch_from_provider()`` closure inside ``_fetch_data``.

    Args:
        svc: QueryService instance (for accessing provider instances)
        intent: Parsed intent
        execution_plan: Materialized provider-dispatch request

    Returns:
        List of NormalizedData from the chosen provider.
    """
    provider = _normalize_provider_name(execution_plan.provider)
    params = dict(execution_plan.params or {})
    _assert_provider_map_authority(provider, intent, params)

    if provider == "FRED":
        fred_request = dict(execution_plan.provider_request or {})
        prepared_params = _prepare_fred_country_scope_params(fred_request, params, intent)
        if prepared_params != params:
            params = prepared_params
            fred_request = dict(materialize_execution_plan(
                execution_plan,
                provider=provider,
                intent=intent,
                params=params,
            ).provider_request or {})
        _raise_if_fred_country_scope_unsupported(fred_request, params, intent)
        # Decide BEFORE the indicator fill below: the search text applies only
        # while ``indicator`` is still the pick it was derived for.
        discovery_query = (
            str(params.get("__fred_discovery_query") or "").strip()
            if _fred_discovery_search_is_current(params)
            else ""
        )
        # Ensure params has indicator set
        if not params.get("indicator") and intent.indicators:
            params = {**params, "indicator": intent.indicators[0]}

        # Handle multiple indicators for FRED
        if len(intent.indicators) > 1:
            # Country-scope discovery is single-indicator only: this branch
            # fetches the verified code and the result validation below fails
            # closed for a non-U.S. scope it does not prove (per-indicator
            # discovery belongs with the deferred per-country/member fan-out).
            all_series = []
            for indicator in intent.indicators:
                indicator_params = {
                    **params,
                    "indicator": fred_request.get("series_id") or indicator,
                }
                series = await svc.fred_provider.fetch_series(indicator_params)
                all_series.append(series)
            _validate_fred_country_scope_result(all_series, fred_request, params)
            return all_series
        else:
            fred_params = {
                **params,
                # Post-resolution pick beats the pre-resolution plan snapshot
                # (same staleness class fixed for IMF in 5771155: the snapshot
                # can carry the semantic label / a stale code).  A country-scope
                # discovery searches with its dispatch-scoped text; only this
                # local provider call sees it, never intent.parameters.
                "indicator": discovery_query or params.get("indicator") or fred_request.get("series_id"),
            }
            # FredProvider gives seriesId precedence over indicator, so a
            # seriesId that differs from the resolved pick must still be
            # dropped here (else it silently overrides every resolution). The
            # drop is no longer SILENT for a code the user typed: resolution
            # stamps __requested_series_id and fetch_data discloses via
            # _attach_requested_series_disclosure. Compare the BASE code
            # (case-insensitively) so a ":pc1"-style transform on the locked
            # code survives; the contract keys on it (_fred_kept_series_transform).
            _fred_indicator = str(fred_params.get("indicator") or "").strip()
            for _sid_key in ("seriesId", "series_id"):
                _sid = fred_params.get(_sid_key)
                if not _sid:
                    continue
                if not _fred_series_id_base_matches(_sid, _fred_indicator):
                    fred_params.pop(_sid_key, None)
                    continue
                # Same series: send the resolved code's spelling + the suffix.
                _base, _sep, _suffix = str(_sid).strip().partition(":")
                fred_params[_sid_key] = f"{_fred_indicator}{_sep}{_suffix}" if _sep else _fred_indicator
            series = await svc.fred_provider.fetch_series(fred_params)
            _validate_fred_country_scope_result([series], fred_request, fred_params)
            return [series]

    if provider in {"WORLDBANK", "WORLD BANK"}:
        worldbank_request = dict(execution_plan.provider_request or {})
        # Post-resolution pick beats the plan snapshot (IMF staleness class).
        resolved_indicator = params.get("indicator") or worldbank_request.get("indicator")
        request_country = worldbank_request.get("country") or params.get("country")
        request_countries = worldbank_request.get("countries") or params.get("countries")
        allow_semantic_alternatives = bool(
            str(params.get("__semantic_authority") or "") == "llm_adjudication"
            and not is_exact_match_locked(params)
        )
        logger.info(
            "WorldBank dispatch: indicator=%s, country=%s, countries=%s, startDate=%s",
            resolved_indicator,
            request_country,
            request_countries,
            params.get("startDate"),
        )
        # Handle multiple indicators for World Bank
        if len(intent.indicators) > 1:
            all_data: List[NormalizedData] = []
            indicators_to_fetch = intent.indicators
            if resolved_indicator and len(intent.indicators) > 1:
                indicators_to_fetch = [str(resolved_indicator)]

            for indicator in indicators_to_fetch:
                data = await svc.world_bank_provider.fetch_indicator(
                    indicator=indicator,
                    country=request_country,
                    countries=request_countries,
                    start_date=params.get("startDate"),
                    end_date=params.get("endDate"),
                    _allow_semantic_alternatives=allow_semantic_alternatives,
                    _defaulted_all_country=bool(params.get("__worldbank_defaulted_country_all")),
                )
                all_data.extend(data if isinstance(data, list) else [data])
            return all_data
        else:
            indicator = str(resolved_indicator or (intent.indicators[0] if intent.indicators else ""))
            wb_result = await svc.world_bank_provider.fetch_indicator(
                indicator=indicator,
                country=request_country,
                countries=request_countries,
                start_date=params.get("startDate"),
                end_date=params.get("endDate"),
                _allow_semantic_alternatives=allow_semantic_alternatives,
                _defaulted_all_country=bool(params.get("__worldbank_defaulted_country_all")),
            )
            if isinstance(wb_result, list):
                logger.info(f"WorldBank returned: {len(wb_result)} series, data_pts={[len(r.data) for r in wb_result if r]}")
            else:
                logger.info(f"WorldBank returned: type={type(wb_result)}, data_pts={len(wb_result.data) if wb_result and wb_result.data else 0}")
            return wb_result

    if provider == "COMTRADE":
        indicators = [indicator.lower() for indicator in intent.indicators]
        flow_value = str(params.get("flow") or "").strip().upper()
        original_query_lower = str(intent.originalQuery or "").lower()
        explicit_trade_balance_query = (
            "trade balance" in original_query_lower
            or "balance of trade" in original_query_lower
            or flow_value == "BALANCE"
        )
        if (any("balance" in indicator for indicator in indicators) and flow_value not in {"EXPORT", "IMPORT"}) or explicit_trade_balance_query:
            series = await svc.comtrade_provider.fetch_trade_balance(
                reporter=params.get("reporter") or params.get("country") or "US",
                partner=params.get("partner"),
                start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                frequency=params.get("frequency", "annual"),
            )
            return [series]
        reporter_value = params.get("reporter") or params.get("country")
        reporters_value = params.get("reporters") or params.get("countries")
        if isinstance(reporters_value, list):
            reporters_clean = [str(value).strip() for value in reporters_value if str(value or "").strip()]
        else:
            reporters_clean = []
        partner_value = str(params.get("partner") or "").strip()

        def _norm_country(value: str) -> str:
            if not value:
                return ""
            try:
                return str(svc._normalize_country_to_iso2(value) or value).upper()
            except Exception:
                return value.upper()

        partner_norm = _norm_country(partner_value)
        if partner_norm:
            reporters_clean = [
                value for value in reporters_clean
                if _norm_country(value) != partner_norm
            ]
        if reporter_value:
            reporter_clean = str(reporter_value).strip()
            reporter_norm = _norm_country(reporter_clean)
            if reporter_clean and reporter_norm not in {_norm_country(value) for value in reporters_clean}:
                reporters_clean.insert(0, reporter_clean)
        if len(reporters_clean) > 1:
            if not reporter_value:
                reporter_value = reporters_clean[0]
            reporters_value = reporters_clean
        elif len(reporters_clean) == 1 and not reporter_value:
            reporter_value = reporters_clean[0]
            reporters_value = None
        elif reporter_value:
            # If there is only a single effective reporter, ignore broad
            # countries[] context to avoid duplicate/misaligned fan-out.
            reporters_value = None
        trade_kwargs = dict(
            reporter=reporter_value,
            reporters=reporters_value,
            partner=params.get("partner"),
            flow=params.get("flow"),
            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            frequency=params.get("frequency", "annual"),
        )
        dispatched_commodity = _comtrade_dispatch_commodity(params)
        try:
            series_list = await svc.comtrade_provider.fetch_trade_data(
                commodity=dispatched_commodity, **trade_kwargs
            )
        except DataNotAvailableError as text_error:
            retry_code = _comtrade_adjudicated_retry_code(params, dispatched_commodity, text_error)
            if not retry_code:
                raise
            logger.info(
                "Comtrade: commodity text %r unresolved (%s); retrying once with adjudicated HS %s",
                dispatched_commodity, str(text_error)[:80], retry_code,
            )
            series_list = await svc.comtrade_provider.fetch_trade_data(
                commodity=retry_code, **trade_kwargs
            )
            for series in series_list or []:
                meta = getattr(series, "metadata", None)
                if meta is None:
                    continue
                meta.notes = list(getattr(meta, "notes", None) or []) + [
                    f'Commodity "{dispatched_commodity}" did not resolve in the UN Comtrade '
                    f"HS catalog; served HS {retry_code} chosen by indicator selection."
                ]
        deduped: dict[tuple[str, str, str, str], Any] = {}
        for series in series_list:
            meta = getattr(series, "metadata", None)
            country = str(getattr(meta, "country", "") or "").strip()
            try:
                country_key = str(svc._normalize_country_to_iso2(country) or country).upper()
            except Exception:
                country_key = country.upper()
            key = (
                normalize_provider_name(getattr(meta, "source", "") or "UN Comtrade"),
                country_key,
                str(getattr(meta, "indicator", "") or "").strip().lower(),
                str(getattr(meta, "seriesId", "") or "").strip().upper(),
            )
            existing = deduped.get(key)
            if existing is None or len(country) > len(str(getattr(existing.metadata, "country", "") or "")):
                deduped[key] = series
        return list(deduped.values())

    if provider in {"STATSCAN", "STATISTICS CANADA"}:
        return await _fetch_from_statscan(svc, intent, params)

    if provider == "IMF":
        return await _fetch_from_imf(svc, intent, params, execution_plan)

    if provider in {"EXCHANGERATE", "EXCHANGE_RATE", "FX"}:
        return await fetch_exchange_rate_with_historical_fallback(svc, intent, params)

    if provider == "BIS":
        indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else "POLICY_RATE"))
        params["indicator"] = indicator
        bis_country = params.get("country")
        bis_countries = params.get("countries")
        if bis_country:
            bis_country = _native_aggregate_or_none("BIS", bis_country) or bis_country
        if isinstance(bis_countries, list):
            bis_countries = [
                _native_aggregate_or_none("BIS", item) or item for item in bis_countries
            ]
        return await svc.bis_provider.fetch_indicator(
            indicator=indicator,
            country=bis_country,
            countries=bis_countries,
            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            frequency=params.get("frequency", "M"),
        )

    if provider == "EUROSTAT":
        return await _fetch_from_eurostat(svc, intent, params, execution_plan)

    if provider == "OECD":
        return await _fetch_from_oecd(svc, intent, params, execution_plan)

    if provider in {"COINGECKO", "COIN GECKO"}:
        return await fetch_from_coingecko(svc.coingecko_provider, intent, params, execution_plan)

    if provider in {"CHINAMACRO", "CHINA MACRO"}:
        # Every ChinaMacro series is China-scoped and the fetch ignores
        # country, so a request for another geography must fail closed here
        # (the explicit-provider route bypasses the router's coverage
        # override; see QueryService._select_routed_provider).
        _raise_if_scope_outside_provider_coverage(
            provider, dict(execution_plan.provider_request or {}), params
        )
        # Params-first indicator precedence (staleness-class convention): the
        # post-resolution pick beats any pre-resolution snapshot.
        indicator = str(
            params.get("indicator")
            or (intent.indicators[0] if intent.indicators else "")
        )
        return [
            await svc.chinamacro_provider.fetch_indicator(
                indicator=indicator,
                start_date=params.get("startDate"),
                end_date=params.get("endDate"),
            )
        ]

    raise DataNotAvailableError(
        f"Provider {intent.apiProvider} is not yet implemented. Available providers: FRED, World Bank, Comtrade, StatsCan, IMF, ExchangeRate, BIS, Eurostat, OECD, CoinGecko, ChinaMacro"
    )


# ---------------------------------------------------------------------------
# Provider-specific helpers (kept private to this module)
# ---------------------------------------------------------------------------

async def _fetch_from_statscan(svc: Any, intent: ParsedIntent, params: dict) -> List[NormalizedData]:
    """Statistics Canada provider dispatch."""
    dimensions = params.get("dimensions", {})

    # Phase 3: pick up __dimensions from materialized ConversationState
    # These are dimension modifiers detected by the context-aware DeltaExtractor
    # and carried through materialize_intent().
    if not dimensions and params.get("__dimensions"):
        dimensions = params["__dimensions"]
        logger.info(f"StatsCan: using __dimensions from conversation state: {dimensions}")

    # Bridge the parse-level region annotation into StatsCan's geography
    # dimension. The parser deliberately keeps indicators region-free and
    # annotates subnationalRegion; without this bridge "Ontario GDP" served
    # NATIONAL Canada data (the query-text dimension scan does not cover every
    # dispatch path, and non-English queries never lexically match member
    # names). An explicit geography param still wins.
    _region = str(getattr(intent, "subnationalRegion", None) or "").strip()
    # Non-invasive: feed the region ONLY as a dimension modifier — the same
    # channel the query-text scan uses — so the dimension-aware dispatch paths
    # apply it via cube member matching. Deliberately NOT set as
    # params["geography"]: that steers dispatch into coordinate paths that
    # fail where the modifier path succeeds (observed live). If the selected
    # cube has no geography dimension, the modifier is unmatched and the
    # subnational fail-closed check turns the national result into an honest
    # explanation instead of mislabeled data.
    if _region and "geography" not in {str(k).lower() for k in dimensions}:
        dimensions = {**dimensions, "geography": _region}
        logger.info(f"StatsCan: geography dimension from subnationalRegion annotation: {_region}")

    entity = params.get("entity")
    indicator = params.get("indicator", intent.indicators[0] if intent.indicators else None)

    def _product_id_from_exact_value(value: Any) -> Optional[str]:
        """Return a StatsCan product ID only from exact provider-native input."""
        if value is None:
            return None
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        normalize_product_id = getattr(
            svc.statscan_provider,
            "_normalize_metadata_product_id",
            lambda raw: str(raw).zfill(8) if raw is not None else None,
        )
        if len(digits) in {8, 10}:
            return normalize_product_id(digits)
        if len(digits) >= 7:
            try:
                cached = svc.statscan_provider.PRODUCT_ID_CACHE.get(int(digits))
            except (TypeError, ValueError):
                cached = None
            if cached:
                return normalize_product_id(cached)
        return None

    state_product_id = (
        _product_id_from_exact_value(params.get("__statscan_product_id"))
        or _product_id_from_exact_value(params.get("productId"))
        or _product_id_from_exact_value(indicator)
    )

    # POST-pick region-coverage guard (region-coverage-aware selection,
    # mechanism b). When the selector picked a cube (llm_pick, retriable) and the
    # user named a sub-national region, verify the cube's Geography dimension
    # actually contains that region BEFORE any coordinate probing. If it does
    # not, raise DataNotAvailableError early so the _fetch_data same-provider
    # alternate-code retry re-adjudicates with THIS cube excluded — the
    # pre-adjudication coverage annotation then steers the retry toward a
    # covering cube — instead of fetching national data the subnational
    # fail-closed check would only discard. Scoped to llm_pick so exact
    # user/vector requests keep failing honestly rather than swapping series.
    _decision_source = str(
        params.get("__decision_source")
        or (intent.parameters or {}).get("__decision_source")
        or ""
    )
    if (
        _region
        and state_product_id
        and _decision_source == "llm_pick"
        and hasattr(svc.statscan_provider, "assert_region_supported_or_raise")
    ):
        await svc.statscan_provider.assert_region_supported_or_raise(state_product_id, _region)

    # --- Framework fix: detect "breakdown" meta-values in dimensions ---
    # When the LLM delta extractor produces a dimension value like "Province",
    # "province", "all provinces", "by province" etc., this means "show data
    # for ALL items in that dimension" (multi-entity breakdown), NOT "filter
    # to a single value". Route these to fetch_multi_province_data.
    _GEOGRAPHY_BREAKDOWN_VALUES = {
        "province", "provinces", "all provinces", "by province",
        "territory", "territories", "all territories",
        "provincial", "all", "each province",
    }

    def _is_dimension_breakdown(dim_values: dict) -> Optional[str]:
        """Check if any dimension value is a meta-category (breakdown request).

        Returns the dimension name if a breakdown is detected, else None.
        """
        for dim_name, dim_val in dim_values.items():
            if isinstance(dim_val, str) and dim_val.lower().strip() in _GEOGRAPHY_BREAKDOWN_VALUES:
                return dim_name
        return None

    dimension_decomposition_axis = str(params.get("__statscan_decomposition_axis") or "").strip()

    if dimension_decomposition_axis and state_product_id:
        query_text = str(
            params.get("__original_query")
            or intent.originalQuery
            or intent.resolvedQuery
            or ""
        )
        indicator_label = str(
            params.get("__semantic_indicator_label")
            or params.get("__display_indicator_label")
            or (intent.indicators[0] if intent.indicators else indicator)
            or indicator
            or ""
        )
        if query_text and hasattr(svc.statscan_provider, "extract_dimension_modifiers"):
            try:
                cube_metadata = await svc.statscan_provider._get_cube_metadata(state_product_id)
                metadata_dimensions = svc.statscan_provider.extract_dimension_modifiers(
                    query_text,
                    indicator_label,
                    state_product_id,
                    cube_metadata,
                )
                if metadata_dimensions:
                    # User-supplied/materialized dimensions remain authoritative;
                    # metadata extraction only fills fixed coordinates such as
                    # Geography=Ontario from the already-selected cube.
                    dimensions = {**metadata_dimensions, **(dimensions or {})}
                    params["dimensions"] = dimensions
                    logger.info(
                        "StatsCan: fixed dimensions from selected cube metadata: %s",
                        dimensions,
                    )
            except Exception as exc:
                logger.debug(
                    "StatsCan selected-cube dimension modifier extraction skipped: %s",
                    exc,
                )

    # EARLY DISPATCH: When __dimensions comes from the delta/merge path,
    # route directly to fetch_with_dimensions or fetch_multi_province_data.
    # This MUST happen before any other dispatch path (industry breakdown,
    # dynamic discovery, etc.) that could divert to the wrong table.
    if (dimensions and params.get("__dimensions")) or dimension_decomposition_axis:
        _base = params.get("__base_indicator") or (indicator or "").upper().replace(" ", "_").replace("-", "_")
        _has_exact_product = bool(state_product_id)

        if dimension_decomposition_axis and _has_exact_product:
            try:
                _product_id_dim = state_product_id

                if _product_id_dim:
                    dimension_periods = _statscan_periods_from_date_range(params, 60)
                    decomposition_params = {
                        "productId": _product_id_dim,
                        "indicator": _base,
                        "indicatorLabel": params.get("__semantic_indicator_label") or params.get("__display_indicator_label") or str(intent.indicators[0] if intent.indicators else _base),
                        "axis": dimension_decomposition_axis,
                        "periods": dimension_periods,
                        "dimensions": dimensions,
                        "startDate": params.get("startDate"),
                        "endDate": params.get("endDate"),
                    }
                    logger.info(
                        "StatsCan EARLY dimension decomposition dispatch: %s product=%s axis=%s",
                        _base,
                        _product_id_dim,
                        dimension_decomposition_axis,
                    )
                    results = await svc.statscan_provider.fetch_multi_dimension_data(decomposition_params)
                    return results if isinstance(results, list) else [results]
            except Exception as e:
                if "statscan_required_dimension_missing" in str(e):
                    raise
                logger.warning(f"StatsCan dimension decomposition dispatch failed: {e}. Trying product-preserving fallback.")
                if state_product_id:
                    try:
                        start_year = int(params["startDate"][:4]) if params.get("startDate") else None
                        end_year = int(params["endDate"][:4]) if params.get("endDate") else None
                        series = await svc.statscan_provider.fetch_with_dimensions(
                            base_indicator=_base,
                            modifiers=dimensions,
                            start_year=start_year,
                            end_year=end_year,
                            periods=params.get("periods", _statscan_periods_from_date_range(params, 240)),
                            indicator_label=(
                                params.get("__semantic_indicator_label")
                                or params.get("__display_indicator_label")
                                or str(intent.indicators[0] if intent.indicators else _base)
                            ),
                            product_id=state_product_id,
                        )
                        return series if isinstance(series, list) else [series]
                    except Exception as fallback_exc:
                        logger.warning(
                            "StatsCan product-preserving dimension fallback failed: %s. Falling through.",
                            fallback_exc,
                        )


        # Check if this is a dimension BREAKDOWN (e.g., Geography="Province")
        # vs a dimension FILTER (e.g., Geography="Ontario")
        _breakdown_dim = _is_dimension_breakdown(dimensions)
        if _breakdown_dim and state_product_id:
            try:
                # Resolve product ID for multi-province fetch
                _indicator_key_bp = _base
                _product_id = state_product_id

                if _product_id:
                    # Remove the breakdown dimension from the modifiers (it's not a real filter)
                    _non_breakdown_dims = {k: v for k, v in dimensions.items() if k != _breakdown_dim}
                    province_periods = _statscan_periods_from_date_range(params, 60)
                    province_params = {
                        "productId": _product_id,
                        "indicator": _base,
                        "indicatorLabel": params.get("__semantic_indicator_label") or params.get("__display_indicator_label") or str(intent.indicators[0] if intent.indicators else _base),
                        "provinces": "all",
                        "periods": province_periods,
                        "dimensions": _non_breakdown_dims,
                        "startDate": params.get("startDate"),
                        "endDate": params.get("endDate"),
                    }
                    logger.info(
                        f"StatsCan EARLY multi-province dispatch: {_base} product={_product_id} "
                        f"breakdown_dim={_breakdown_dim}"
                    )
                    results = await svc.statscan_provider.fetch_multi_province_data(province_params)
                    return results if isinstance(results, list) else [results]
            except Exception as e:
                logger.warning(f"StatsCan multi-province dispatch failed: {e}. Falling through.")

        if state_product_id:
            try:
                start_year = int(params["startDate"][:4]) if params.get("startDate") else None
                end_year = int(params["endDate"][:4]) if params.get("endDate") else None
                if state_product_id:
                    categorical_params = {
                        "productId": state_product_id,
                        "indicator": indicator or _base,
                        "indicatorLabel": params.get("__semantic_indicator_label") or params.get("__display_indicator_label") or str(intent.indicators[0] if intent.indicators else indicator or _base),
                        "periods": _statscan_periods_from_date_range(params, 240),
                        "dimensions": dimensions,
                        "startDate": params.get("startDate"),
                        "endDate": params.get("endDate"),
                    }
                    logger.info(
                        "StatsCan EARLY product-preserving dimension dispatch: product=%s dimensions=%s",
                        state_product_id,
                        dimensions,
                    )
                    series = await svc.statscan_provider.fetch_categorical_data(categorical_params)
                    return [series]
                logger.info(f"StatsCan EARLY dimension dispatch: {_base} + {dimensions}")
                series = await svc.statscan_provider.fetch_with_dimensions(
                    base_indicator=_base,
                    modifiers=dimensions,
                    start_year=start_year,
                    end_year=end_year,
                    periods=_statscan_periods_from_date_range(params, 240),
                    indicator_label=params.get("__semantic_indicator_label") or params.get("__display_indicator_label") or str(intent.indicators[0] if intent.indicators else _base),
                    product_id=state_product_id,
                )
                return [series]
            except Exception as e:
                logger.warning(f"StatsCan early dimension dispatch failed: {e}. Falling through.")

    # Check for industry/breakdown parameter
    industry = params.get("industry") or params.get("breakdown")
    if industry:
        logger.info("StatsCan breakdown requested: %s", industry)
        breakdown_params = {
            "indicator": indicator or str(intent.indicators[0] if intent.indicators else ""),
            "breakdown": industry,
            "productId": state_product_id,
            "vectorId": params.get("vectorId"),
            "startDate": params.get("startDate"),
            "endDate": params.get("endDate"),
            "periods": _statscan_periods_from_date_range(params, 240),
        }
        series = await svc.statscan_provider.fetch_with_breakdown(breakdown_params)
        return [series]

    # If entity is present (from decomposition), convert to dimension
    if entity and not dimensions:
        dimensions = {"geography": entity}

    # Exact StatsCan table-title/product requests already carry a provider-native
    # product ID.  Use the generic product dispatcher before metadata-derived
    # modifier extraction so words that appear in the table title (for example
    # Canada/provinces/United States/imports) are treated as table scope unless
    # the query supplied explicit dimensions elsewhere.
    if state_product_id and params.get("__exact_indicator_title_match") and not dimensions:
        logger.info("StatsCan exact product dispatch before modifier extraction: product=%s", state_product_id)
        dynamic_params = {
            "indicator": state_product_id,
            "indicatorLabel": params.get("__semantic_indicator_label") or params.get("__display_indicator_label") or str(intent.indicators[0] if intent.indicators else indicator or state_product_id),
            "periods": _statscan_periods_from_date_range(params, 240),
            "__exact_indicator_title_match": True,
        }
        if params.get("geography"):
            dynamic_params["geography"] = params.get("geography")
        elif str(params.get("country") or "").strip().upper() in {"CA", "CAN", "CANADA"}:
            dynamic_params["geography"] = "Canada"
        if params.get("startDate"):
            dynamic_params["startDate"] = params.get("startDate")
        if params.get("endDate"):
            dynamic_params["endDate"] = params.get("endDate")
        result = await svc.statscan_provider.fetch_dynamic_data(dynamic_params)
        return [result]

    # --- Dimension modifier detection (GENERAL, metadata-driven) ---
    # Before falling through to standard vector/dynamic fetch, check if the
    # query text contains dimension modifiers (province names, gender terms,
    # age terms, product categories, etc.) that should narrow the data.
    # This uses the table's actual metadata, NOT hardcoded modifier lists.
    if indicator and not dimensions and state_product_id:
        _indicator_label = params.get("__semantic_indicator_label") or params.get("__display_indicator_label") or str(
            intent.indicators[0] if intent.indicators else indicator
        )
        if state_product_id:
            query_text = intent.originalQuery or ""
            try:
                _cube_meta = await svc.statscan_provider._get_cube_metadata(state_product_id)
                _modifiers = svc.statscan_provider.extract_dimension_modifiers(
                    query_text, _indicator_label, state_product_id, _cube_meta,
                )
                if _modifiers:
                    logger.info(
                        "StatsCan dimension modifiers detected from selected product metadata: %s",
                        _modifiers,
                    )
                    params["__dimensions"] = _modifiers
                    params["__base_indicator"] = _indicator_label
                    params["__statscan_product_id"] = state_product_id
                    intent.parameters = params
                    start_year = int(params["startDate"][:4]) if params.get("startDate") else None
                    end_year = int(params["endDate"][:4]) if params.get("endDate") else None
                    series = await svc.statscan_provider.fetch_with_dimensions(
                        base_indicator=_indicator_label,
                        modifiers=_modifiers,
                        start_year=start_year,
                        end_year=end_year,
                        periods=_statscan_periods_from_date_range(params, 240),
                        product_id=state_product_id,
                    )
                    return [series]
            except Exception as e:
                logger.warning(
                    f"Dimension modifier extraction/fetch failed for {state_product_id}: {e}. "
                    f"Falling through to standard fetch."
                )

    # Use dimension-aware fetch when dimensions are specified
    if dimensions:
        # Phase 3: If dimensions came from conversation state (__dimensions),
        # use fetch_with_dimensions which is the general mechanism that
        # discovers table metadata and builds coordinates dynamically.
        # fetch_categorical_data is only for basic product ID / dimensions combos.
        _base = params.get("__base_indicator") or indicator or ""
        _indicator_label_for_dim = _statscan_semantic_label(params, intent, _base)

        # Check again for dimension breakdown (multi-province) in case early dispatch missed it
        _breakdown_dim_fb = _is_dimension_breakdown(dimensions)
        if _breakdown_dim_fb and state_product_id and params.get("__dimensions"):
            try:
                _product_id_fb = state_product_id
                if _product_id_fb:
                    _non_breakdown_dims_fb = {k: v for k, v in dimensions.items() if k != _breakdown_dim_fb}
                    province_params_fb = {
                        "productId": _product_id_fb,
                        "indicator": _indicator_label_for_dim,
                        "indicatorLabel": _indicator_label_for_dim,
                        "provinces": "all",
                        "periods": params.get("periods", 60),
                        "dimensions": _non_breakdown_dims_fb,
                        "startDate": params.get("startDate"),
                        "endDate": params.get("endDate"),
                    }
                    logger.info(
                        f"StatsCan fallback multi-province dispatch: {_indicator_label_for_dim} "
                        f"product={_product_id_fb}"
                    )
                    results_fb = await svc.statscan_provider.fetch_multi_province_data(province_params_fb)
                    return results_fb if isinstance(results_fb, list) else [results_fb]
            except Exception as e:
                logger.warning(f"StatsCan fallback multi-province dispatch failed: {e}. Falling through.")

        if state_product_id and params.get("__dimensions"):
            try:
                start_year = int(params["startDate"][:4]) if params.get("startDate") else None
                end_year = int(params["endDate"][:4]) if params.get("endDate") else None
                if state_product_id:
                    categorical_params = {
                        "productId": state_product_id,
                        "indicator": indicator or _indicator_label_for_dim,
                        "indicatorLabel": _indicator_label_for_dim,
                        "periods": _statscan_periods_from_date_range(params, 240),
                        "dimensions": dimensions,
                        "startDate": params.get("startDate"),
                        "endDate": params.get("endDate"),
                    }
                    logger.info(
                        "StatsCan product-preserving dimension dispatch: product=%s dimensions=%s",
                        state_product_id,
                        dimensions,
                    )
                    series = await svc.statscan_provider.fetch_categorical_data(categorical_params)
                    return [series]
                series = await svc.statscan_provider.fetch_with_dimensions(
                    base_indicator=_indicator_label_for_dim,
                    modifiers=dimensions,
                    start_year=start_year,
                    end_year=end_year,
                    periods=_statscan_periods_from_date_range(params, 240),
                    indicator_label=_indicator_label_for_dim,
                    product_id=state_product_id,
                )
                return [series]
            except Exception as e:
                logger.warning(
                    f"fetch_with_dimensions failed for {state_product_id} "
                    f"with __dimensions={dimensions}: {e}. Falling through."
                )

        if not state_product_id:
            raise DataNotAvailableError(
                "StatsCan dimension query requires a productId/table ID selected "
                "by metadata/LLM evidence; refusing to default to a provider-local table."
            )
        categorical_params = {
            "productId": state_product_id,
            "indicator": indicator or _indicator_label_for_dim,
            "indicatorLabel": _indicator_label_for_dim,
            "periods": params.get("periods", 20),
            "dimensions": dimensions,
        }
        series = await svc.statscan_provider.fetch_categorical_data(categorical_params)
        return [series]
    else:
        if params.get("vectorId") or (indicator and str(indicator).strip().isdigit() and not state_product_id):
            series = await svc.statscan_provider.fetch_series(params)
            return [series]
        if indicator:
            logger.info(f"Using dynamic discovery for StatsCan indicator: {indicator}")
            dynamic_params = {
                "indicator": indicator,
                "indicatorLabel": params.get("__semantic_indicator_label") or params.get("__display_indicator_label") or str(intent.indicators[0] if intent.indicators else indicator),
                "geography": params.get("geography"),
                "periods": _statscan_periods_from_date_range(params, 240),
            }
            if params.get("startDate"):
                dynamic_params["startDate"] = params.get("startDate")
            if params.get("endDate"):
                dynamic_params["endDate"] = params.get("endDate")
            try:
                result = await svc.statscan_provider.fetch_dynamic_data(dynamic_params)
                return [result]
            except DataNotAvailableError:
                logger.warning(f"Dynamic discovery failed for {indicator}, trying vector fetch")
                series = await svc.statscan_provider.fetch_series(params)
                return [series]
        else:
            raise DataNotAvailableError("No indicator specified for Statistics Canada query")


async def _fetch_from_imf(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
    execution_plan: Optional[ExecutionPlan] = None,
) -> List[NormalizedData]:
    """IMF provider dispatch."""
    imf_request = dict((execution_plan.provider_request or {}) if execution_plan else {})
    countries_param = (
        imf_request.get("countries")
        or imf_request.get("country_scope")
        or imf_request.get("country")
        or params.get("countries")
        or params.get("country")
    )
    # params["indicator"] holds the POST-RESOLUTION pick; the execution-plan
    # snapshot was materialized BEFORE selection and can still carry the
    # semantic LABEL ("debt to GDP ratio") — letting the snapshot win sent the
    # label to the provider, whose own catalog lookup re-resolved it to a
    # different sibling (GGXWDG_GDP, no USA data) while the adjudicated pick
    # (GGXWDG_NGDP, 31 pts for USA) never reached it. Freshest wins.
    resolved_indicator = str(
        params.get("indicator")
        or imf_request.get("indicator")
        or imf_request.get("code")
        or ""
    ).strip()
    request_start_year = imf_request.get("start_year")
    request_end_year = imf_request.get("end_year")
    if request_start_year is None and params.get("startDate"):
        request_start_year = int(str(params["startDate"])[:4])
    if request_end_year is None and params.get("endDate"):
        request_end_year = int(str(params["endDate"])[:4])

    # Resolve countries/regions to list of country codes
    def _resolve_imf_entity(item: Any) -> List[str]:
        native_aggregate = _native_aggregate_or_none("IMF", item)
        if native_aggregate:
            return [native_aggregate]
        return svc.imf_provider._resolve_countries(item)

    resolved_countries: List[str] = []
    default_geography: Optional[str] = None
    if isinstance(countries_param, list):
        for item in countries_param:
            resolved_countries.extend(_resolve_imf_entity(item))
    elif isinstance(countries_param, str):
        resolved_countries = _resolve_imf_entity(countries_param)
    else:
        default_geography = str(imf_request.get("default_country") or "USA")
        resolved_countries = [default_geography]

    def _disclose_default(series_list: List[NormalizedData]) -> List[NormalizedData]:
        if default_geography:
            return _mark_provider_default_geography(series_list, default_geography)
        return series_list

    # Remove duplicates while preserving order
    resolved_countries = list(dict.fromkeys(resolved_countries))

    logger.info(
        "IMF query resolved to %d countries: %s (from params: %s)",
        len(resolved_countries),
        resolved_countries[:10] if len(resolved_countries) > 10 else resolved_countries,
        countries_param,
    )

    async def _fetch_imf_series_batch(
        *,
        indicator: str,
        countries: List[str],
        start_year: Optional[int],
        end_year: Optional[int],
    ) -> List[NormalizedData]:
        """Fetch IMF data across countries using the provider's best interface."""
        batch_fetch = getattr(svc.imf_provider, "fetch_batch_indicator", None)
        if callable(batch_fetch):
            return await batch_fetch(
                indicator=indicator,
                countries=countries,
                start_year=start_year,
                end_year=end_year,
            )

        single_fetch = getattr(svc.imf_provider, "fetch_indicator", None)
        if not callable(single_fetch):
            raise AttributeError("IMF provider exposes neither fetch_batch_indicator nor fetch_indicator")

        series: List[NormalizedData] = []
        for country_code in countries:
            fetched = await single_fetch(
                indicator=indicator,
                country=country_code,
                start_year=start_year,
                end_year=end_year,
            )
            if isinstance(fetched, list):
                series.extend(fetched)
            elif fetched is not None:
                series.append(fetched)
        return series

    if len(resolved_countries) > 1:
        logger.info("Using IMF batch method for %d countries", len(resolved_countries))
        all_data: List[NormalizedData] = []
        indicators_to_fetch = list(intent.indicators or [])
        if resolved_indicator:
            indicators_to_fetch = [resolved_indicator]
        if not indicators_to_fetch:
            indicators_to_fetch = [resolved_indicator] if resolved_indicator else []

        for indicator in indicators_to_fetch:
            series_list = await _fetch_imf_series_batch(
                indicator=indicator,
                countries=resolved_countries,
                start_year=request_start_year,
                end_year=request_end_year,
            )
            all_data.extend(series_list)
        return _disclose_default(all_data)
    else:
        country = resolved_countries[0]
        if len(intent.indicators) > 1:
            all_data = []
            indicators_to_fetch = list(intent.indicators or [])
            if resolved_indicator:
                indicators_to_fetch = [resolved_indicator]
            for indicator in indicators_to_fetch:
                series_list = await _fetch_imf_series_batch(
                    indicator=indicator,
                    countries=[country],
                    start_year=request_start_year,
                    end_year=request_end_year,
                )
                all_data.extend(series_list)
            return _disclose_default(all_data)
        else:
            indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else ""))
            return _disclose_default(await _fetch_imf_series_batch(
                indicator=resolved_indicator or indicator,
                countries=[country],
                start_year=request_start_year,
                end_year=request_end_year,
            ))


async def _fetch_from_eurostat(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
    execution_plan: Optional[ExecutionPlan] = None,
) -> List[NormalizedData]:
    """Eurostat provider dispatch."""
    eurostat_request = dict((execution_plan.provider_request or {}) if execution_plan else {})
    # NOTE: Eurostat is the exception to the params-first rule (IMF staleness
    # class) — its plan dataset_code is a load-bearing contract (three tests
    # assert plan-first here) and its own resolution flow expects it.
    indicator = str(
        eurostat_request.get("dataset_code")
        or eurostat_request.get("code")
        or params.get("indicator")
        or (intent.indicators[0] if intent.indicators else "GDP")
    )
    params["indicator"] = indicator

    scoped_countries = list(eurostat_request.get("country_scope") or [])
    request_start_year = eurostat_request.get("start_year")
    request_end_year = eurostat_request.get("end_year")
    request_filters = dict(eurostat_request.get("filters") or {})
    original_query = str(params.get("__original_query") or intent.originalQuery or "")
    exact_dataset_code_requested = bool(
        indicator
        and re.search(rf"(?<![A-Z0-9_]){re.escape(indicator)}(?![A-Z0-9_])", original_query, flags=re.IGNORECASE)
    )
    exact_provider_surface_requested = exact_dataset_code_requested or is_exact_match_locked(params)
    explicit_time_scope = _user_set_time_scope(params, original_query)
    current_year = datetime.utcnow().year
    default_recent_start_year = current_year - 5
    should_retry_sparse_history = (
        not explicit_time_scope
        and request_start_year is not None
        and int(request_start_year) >= default_recent_start_year
    )
    # Some exact Eurostat catalog titles are sparse or historical one-off
    # tables.  When the framework supplied only a recent default window (not a
    # user time constraint), retry against a broad mechanical history window
    # rather than letting the default window create a false no-data result.
    fallback_start_year = 1990

    async def _fetch_indicator_with_sparse_history_retry(country_code: str) -> NormalizedData:
        fetch_kwargs = {
            "indicator": indicator,
            "country": country_code,
            "start_year": request_start_year,
            "end_year": request_end_year,
        }
        if request_filters:
            fetch_kwargs["filters"] = request_filters
        try:
            return await svc.eurostat_provider.fetch_indicator(**fetch_kwargs)
        except DataNotAvailableError:
            if not should_retry_sparse_history or fallback_start_year >= int(request_start_year):
                raise
            logger.info(
                "Eurostat sparse-history retry: indicator=%s country=%s start_year=%s -> %s",
                indicator,
                country_code,
                request_start_year,
                fallback_start_year,
            )
            fetch_kwargs["start_year"] = fallback_start_year
            return await svc.eurostat_provider.fetch_indicator(**fetch_kwargs)

    country_param = params.get("country") or (scoped_countries[0] if len(scoped_countries) == 1 else None)
    countries_param = params.get("countries") or scoped_countries

    # EU aggregate codes that should NOT expand
    EU_AGGREGATES = {"EU", "EU27", "EU27_2020", "EU28", "EA", "EA19", "EA20", "EUROZONE", "EURO_AREA"}

    is_multi_country = isinstance(countries_param, list) and len(countries_param) > 1

    if not is_multi_country and isinstance(country_param, str):
        upper_country = country_param.upper().replace(" ", "_")
        if upper_country not in EU_AGGREGATES and not canonical_aggregate_region(country_param):
            from ..routing.country_resolver import CountryResolver

            expanded = CountryResolver.expand_region(country_param)
            if expanded:
                countries_param = expanded
                is_multi_country = True
                logger.info(f"Expanded Eurostat region '{country_param}' to {len(expanded)} countries via CountryResolver")
            else:
                SUB_REGION_MAPPINGS = {
                    "BENELUX": ["BE", "NL", "LU"],
                    "BALTIC": ["EE", "LV", "LT"],
                    "DACH": ["DE", "AT", "CH"],
                    "IBERIAN": ["ES", "PT"],
                    "VISEGRAD": ["PL", "CZ", "SK", "HU"],
                    "V4": ["PL", "CZ", "SK", "HU"],
                }
                if upper_country in SUB_REGION_MAPPINGS:
                    countries_param = SUB_REGION_MAPPINGS[upper_country]
                    is_multi_country = True
                    logger.info(f"Expanded Eurostat sub-region '{country_param}' to: {countries_param}")

    if is_multi_country:
        logger.info(f"Multi-country Eurostat query detected: {countries_param}")
        eurostat_sem = asyncio.Semaphore(5)

        async def _fetch_eurostat_country(country_code: str) -> Optional[NormalizedData]:
            async with eurostat_sem:
                try:
                    return await _fetch_indicator_with_sparse_history_retry(country_code)
                except Exception as e:
                    logger.warning(f"Failed to fetch {indicator} for {country_code}: {e}")
                    return None

        eurostat_results = await asyncio.gather(
            *[_fetch_eurostat_country(c) for c in countries_param],
            return_exceptions=True,
        )
        series_list = [
            r for r in eurostat_results
            if isinstance(r, NormalizedData)
        ]

        if not series_list:
            raise DataNotAvailableError(f"No Eurostat data available for {indicator} in any requested countries")

        return series_list

    # Single country query (default to EU aggregate if not specified)
    single_country = country_param if country_param else ("__ALL__" if exact_provider_surface_requested else "EU27_2020")
    series = await _fetch_indicator_with_sparse_history_retry(single_country)
    if not country_param and not exact_provider_surface_requested:
        return _mark_provider_default_geography([series], single_country)
    return [series]


async def _fetch_from_oecd(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
    execution_plan: Optional[ExecutionPlan] = None,
) -> List[NormalizedData]:
    """OECD provider dispatch."""
    oecd_request = dict((execution_plan.provider_request or {}) if execution_plan else {})
    indicator = str(
        oecd_request.get("indicator")
        or oecd_request.get("code")
        or params.get("indicator")
        or (intent.indicators[0] if intent.indicators else "GDP")
    )
    params["indicator"] = indicator

    scoped_countries = list(oecd_request.get("country_scope") or [])
    country_param = oecd_request.get("country") or params.get("country")
    countries_param = oecd_request.get("countries") or params.get("countries") or scoped_countries
    request_start_year = oecd_request.get("start_year")
    request_end_year = oecd_request.get("end_year")
    original_query = str(params.get("__original_query") or intent.originalQuery or "")
    exact_dataset_code_requested = bool(
        indicator
        and re.search(rf"(?<![A-Z0-9_]){re.escape(indicator)}(?![A-Z0-9_])", original_query, flags=re.IGNORECASE)
    )
    exact_provider_surface_requested = exact_dataset_code_requested or is_exact_match_locked(params)

    # Handle LLM parsing "OECD unemployment" as countries=["ALL_OECD"]
    if countries_param and len(countries_param) == 1:
        c = countries_param[0].upper().replace(" ", "_")
        if c in ("OECD", "ALL_OECD", "ALL_OECD_COUNTRIES", "OECD_COUNTRIES"):
            logger.info(f"Converting countries=['{countries_param[0]}'] to OECD aggregate query")
            country_param = "OECD"
            countries_param = []

    # Exact provider-native OECD surfaces can have provider-default REF_AREA
    # constraints in SDMX structure metadata; preserving that default avoids
    # injecting arbitrary countries into exact catalog/table requests.  For
    # broad natural-language OECD questions, keep the legacy OECD aggregate
    # behavior instead of widening all no-country prompts to provider defaults.
    if not country_param and not countries_param:
        if exact_provider_surface_requested:
            logger.info("No country specified for exact OECD surface, using provider-native default scope")
            country_param = None
        else:
            logger.info("No country specified for broad OECD query, using OECD aggregate scope")
            country_param = "OECD"

    # Detect multi-country requests including region names
    expanded_countries: List[str] = []
    if isinstance(country_param, str):
        if country_param.upper() in ("OECD", "OECD_AVERAGE"):
            expanded_countries = ["OECD"]
        else:
            expanded_countries = svc.oecd_provider.expand_countries(country_param)

    is_multi_country = (
        isinstance(countries_param, list) and len(countries_param) > 1
    ) or (
        len(expanded_countries) > 1
    )

    if is_multi_country:
        logger.info("Multi-country OECD query detected")
        try:
            countries = countries_param if countries_param else expanded_countries
            series_list = await svc.oecd_provider.fetch_multi_country(
                indicator=indicator,
                countries=countries,
                start_year=request_start_year,
                end_year=request_end_year,
            )
            return series_list
        except Exception as exc:
            error_msg = str(exc).lower()
            temporarily_unavailable = any(
                token in error_msg
                for token in ("rate limit", "429", "circuit", "timeout", "timed out", "temporarily unavailable")
            )
            if temporarily_unavailable:
                logger.warning("OECD multi-country temporarily unavailable: %s", exc)
                raise DataNotAvailableError(
                    f"OECD temporarily unavailable for multi-country request: {exc}"
                ) from exc
            raise

    try:
        series = await svc.oecd_provider.fetch_indicator(
            indicator=indicator,
            country=country_param,
            start_year=request_start_year,
            end_year=request_end_year,
        )
        return [series]
    except Exception as exc:
        error_msg = str(exc).lower()
        temporarily_unavailable = any(
            token in error_msg
            for token in ("rate limit", "429", "circuit", "timeout", "timed out", "temporarily unavailable")
        )
        if temporarily_unavailable:
            logger.warning("OECD temporarily unavailable for %s: %s", country_param, exc)
            raise DataNotAvailableError(
                f"OECD temporarily unavailable for {country_param or 'OECD'}: {exc}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Main fetch orchestration
# ---------------------------------------------------------------------------

def _carried_indicator_dispatchable_as_is(
    svc: Any,
    provider: str,
    indicator_name: str,
    indicator_changed: bool,
    resolved_code_present: bool,
) -> bool:
    """State-reflecting gate for the PHASE A "use carried indicator as-is" path (FIX 2).

    The fast path dispatches the carried indicator directly — and stamps
    ``verified_conversation_state`` authority — WITHOUT re-resolving. That is
    only safe when the carried value actually reflects a dispatchable code:

    * a resolved provider code is already present in ``params["indicator"]``, or
    * the carried indicator itself looks like a provider-native code for the
      dispatch provider.

    A bare human label ("GDP", "unemployment rate") carried after ``merge_state``
    nulled the resolved code — a frequency change (F1a) or a geography switch on
    a geography-encoded provider (FIX 1b) — must NOT be dispatched as-is and
    re-stamped verified; it has to fall through to normal resolution so a code is
    re-resolved under the new constraints. Returns ``False`` (→ resolve) unless
    we are confident the carried value is dispatchable.
    """
    if indicator_changed:
        return False
    if resolved_code_present:
        return True
    return bool(
        indicator_name
        and svc._looks_like_provider_indicator_code(provider, indicator_name)
    )


# ---------------------------------------------------------------------------
# Structured window-coverage fact for no_data_in_requested_window failures.
#
# The legacy string marker ("no_data_in_requested_window ... latest_available=")
# stays verbatim in str(exc) for every substring consumer; the coverage below is
# carried as an ATTRIBUTE (``exc.window_coverage``) so the response layer can
# scope its explanation to the SERVED series (code + title) rather than the
# user's concept, and the subnational fail-closed can test FETCHED metadata.
# Free text (titles) never goes into the regex-parsed string.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowCoverage:
    """FETCHED observation coverage of the series behind a window miss.

    ``earliest`` is set ONLY from the unwindowed diagnostic probe (the minimum
    finite observation date it returned) and is "" whenever it is not provable
    — never from a windowed fetch, catalog start dates, or derived metadata.
    Dates are observational ("retrieved"), not publication-schedule claims.
    """

    provider: str
    latest: str
    earliest: str = ""
    window_start_year: Optional[int] = None
    window_end_year: Optional[int] = None
    series_metadata: tuple = ()

    def single_series(self) -> Optional[Metadata]:
        """The served series' metadata when exactly ONE identity was fetched."""
        identities = {}
        for meta in self.series_metadata:
            key = (
                str(getattr(meta, "seriesId", "") or "").strip().upper(),
                str(getattr(meta, "indicator", "") or "").strip(),
                str(getattr(meta, "country", "") or "").strip().upper(),
            )
            identities.setdefault(key, meta)
        return next(iter(identities.values())) if len(identities) == 1 else None

    def direction(self) -> str:
        """'before' / 'after' when the window lies wholly outside the
        retrieved span (year-level, matching the window convention), else ''."""
        def _y(text: str) -> Optional[int]:
            head = str(text or "")[:4]
            return int(head) if head.isdigit() else None

        earliest_year, latest_year = _y(self.earliest), _y(self.latest)
        if (
            self.window_start_year is not None
            and latest_year is not None
            and self.window_start_year > latest_year
        ):
            return "after"
        if (
            self.window_end_year is not None
            and earliest_year is not None
            and self.window_end_year < earliest_year
        ):
            return "before"
        return ""


def _window_bound_years(params: dict) -> tuple[Optional[int], Optional[int]]:
    """Year bounds of the requested window; never raises (structural parse)."""
    def _y(value: Any) -> Optional[int]:
        head = str(value or "")[:4]
        return int(head) if head.isdigit() else None

    params = params or {}
    return (
        _y(params.get("startDate")) or _y(params.get("start_year")),
        _y(params.get("endDate")) or _y(params.get("end_year")),
    )


def _probe_earliest_observation(result: List[NormalizedData]) -> str:
    """Minimum finite observation date of an UNWINDOWED probe, or "".

    Mirrors the probe classifier's conclusiveness rule: any non-null point with
    an unparseable date or non-finite value makes the bound unprovable ("").
    """
    from ..providers._sdmx.periods import period_to_iso_date

    earliest = ""
    for series in result or []:
        for point in getattr(series, "data", None) or []:
            value = point.get("value") if isinstance(point, dict) else getattr(point, "value", None)
            if value is None:
                continue
            raw = point.get("date", "") if isinstance(point, dict) else getattr(point, "date", "")
            normalized = period_to_iso_date(raw)
            try:
                finite = math.isfinite(value)
            except (TypeError, ValueError, OverflowError):
                finite = False
            if not finite or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[Tt ].+)?", str(normalized or "")):
                return ""
            day = str(normalized)[:10]
            if not earliest or day < earliest:
                earliest = day
    return earliest


def _window_marker_error(
    marker_text: str,
    *,
    provider: str,
    params: dict,
    latest: str,
    series: List[NormalizedData],
    earliest: str = "",
) -> DataNotAvailableError:
    """Build the window-marker exception with its structured coverage attached."""
    exc = DataNotAvailableError(marker_text)
    try:
        start_year, end_year = _window_bound_years(params)
        exc.window_coverage = WindowCoverage(  # type: ignore[attr-defined]
            provider=str(provider or ""),
            latest=str(latest or "")[:10],
            earliest=str(earliest or "")[:10],
            window_start_year=start_year,
            window_end_year=end_year,
            series_metadata=tuple(
                series_item.metadata.model_copy(deep=True)
                for series_item in (series or [])
                if getattr(series_item, "metadata", None) is not None
            ),
        )
    except Exception as coverage_exc:  # coverage is advisory; marker must still raise
        logger.debug("Window coverage capture skipped: %s", coverage_exc)
    return exc


async def fetch_data(
    svc: Any,
    intent: ParsedIntent,
    execution_plan: Optional[ExecutionPlan] = None,
) -> List[NormalizedData]:
    """Full data fetch pipeline: resolve indicator, check cache, dispatch to provider, validate.

    This is the primary entry point extracted from ``QueryService._fetch_data``.

    Args:
        svc: QueryService instance
        intent: Parsed intent to fetch data for

    Returns:
        List of NormalizedData from the selected provider.
    """
    logger.info(f"_fetch_data called: provider={intent.apiProvider}, indicators={intent.indicators}")

    provider = _normalize_provider_name(intent.apiProvider)

    # Early exit: concept is not available from any provider
    if provider == "NOT_AVAILABLE":
        indicator_text = intent.indicators[0] if intent.indicators else "this indicator"
        logger.info(f"Not available: {indicator_text} -- no provider carries this data")
        return [NormalizedData(
            metadata=Metadata(
                source="Catalog",
                indicator=f"{indicator_text} -- Not Available",
                frequency="N/A",
                unit="N/A",
                lastUpdated="",
                description=(
                    f"'{indicator_text}' is not currently available through any of our "
                    f"data providers. This may be because the data has been discontinued, "
                    f"archived, or is only available through specialized sources."
                ),
            ),
            data=[],
        )]
    params = intent.parameters or {}
    if not intent.clarificationNeeded:
        params = normalize_aggregate_geography(provider, params)
        intent.parameters = params

    # Pre-flight geographic split
    if not params.get("__geo_split_child"):
        split_result = await svc._preflight_geographic_split(intent)
        if split_result is not None:
            return split_result
    # Strip the recursion guard
    if "__geo_split_child" in params:
        params = {k: v for k, v in params.items() if k != "__geo_split_child"}
        intent.parameters = params

    fallback_excluded_providers = {
        _normalize_provider_name(str(candidate))
        for candidate in (params.get("__fallback_excluded_providers") or [])
        if candidate
    }
    fallback_excluded_providers.discard("")
    tracker = get_processing_tracker()

    ranking_scope_query = str(intent.originalQuery or "").strip()
    if not ranking_scope_query and intent.indicators:
        ranking_scope_query = " ".join(str(indicator) for indicator in intent.indicators if indicator)
    params = svc._maybe_expand_ranking_country_scope(ranking_scope_query, provider, params)
    intent.parameters = params

    if provider == "IMF":
        supportability_reason = imf_exact_provider_surface_supportability_reason(
            intent.originalQuery or ranking_scope_query,
            intent.indicators or [],
            params,
        )
        if supportability_reason:
            raise DataNotAvailableError(
                "IMF query targets a detailed IMF public-data surface that is "
                "not yet executable by OpenEcon's production IMF dataset-family "
                "routing. This is a fail-closed supportability block, not a "
                "broad-proxy substitution. "
                f"reason={supportability_reason}"
            )

    # When __dimensions is present (from delta/merge path for dimension changes),
    # the indicator and provider are fully resolved. Skip all overrides.
    # When __delta_resolved is present (time/country/indicator follow-ups),
    # still resolve the indicator code but use the indicator name as the query
    # (not the raw follow-up text like "last 20 years") to prevent mismatches.
    _has_dimensions = bool(params.get("__dimensions"))
    _is_delta_resolved = bool(params.get("__delta_resolved"))
    if _has_dimensions:
        logger.info(
            "Skipping indicator resolution for delta-resolved dimensions: "
            "indicator=%s, provider=%s, dimensions=%s",
            params.get("indicator"), provider, params.get("__dimensions"),
        )
    elif _is_delta_resolved:
        _indicator_name = intent.indicators[0] if intent.indicators else ""
        _indicator_changed = bool(params.get("__delta_indicator_changed"))
        # State-reflecting gate (FIX 2): the carried indicator may be dispatched
        # as-is (and re-stamped verified) only when a resolved code is present or
        # the carried value looks like a provider code for THIS provider. A bare
        # label carried after merge_state nulled the resolved code (frequency
        # change F1a, or geography switch FIX 1b) must fall through to normal
        # resolution instead of being dispatched — and re-verified — unchanged.
        _resolved_code_present = bool(str(params.get("indicator") or "").strip())
        if _carried_indicator_dispatchable_as_is(
            svc, provider, _indicator_name, _indicator_changed, _resolved_code_present
        ):
            # Indicator preserved from prior turn — use as-is, no resolution.
            params["indicator"] = _indicator_name
            intent.parameters = params
            # The carried code reached conversation state via a verified fetch
            # on a prior turn; a country/time-only follow-up must keep its
            # dispatch authority so the no-shortcut gate does not block
            # provider-internal codes (Eurostat/StatsCan/IMF) on a new country
            # ("Germany inflation" -> "what about France" reuses TEC00118).
            # The authority predicate already scopes this to __delta_resolved
            # with the indicator unchanged; only fill when nothing stronger
            # (exact_user_input / llm_adjudication) is already present.
            if not str(params.get("__semantic_authority") or ""):
                params["__semantic_authority"] = "verified_conversation_state"
            logger.info(
                "Delta-resolved: indicator unchanged, using '%s' directly",
                _indicator_name,
            )
        else:
            # New indicator, provider changed, OR a bare carried label with no
            # resolved code — resolve using the indicator name as query.  When
            # the indicator name is a provider-specific code
            # from a *prior* provider (e.g. A191RL1Q225SBEA from FRED, but
            # we're now routing to WorldBank), resolve via the catalog concept
            # name instead (e.g. "gdp growth") so the new provider can find
            # its own indicator code.
            _saved_query = intent.originalQuery
            _resolution_query = _indicator_name
            _is_code = _indicator_name and any(
                svc._looks_like_provider_indicator_code(p, _indicator_name)
                for p in ALL_PROVIDERS
            )
            if _is_code:
                from ..services.catalog_service import (
                    find_concept_by_term,
                    find_concepts_by_code,
                    is_provider_available,
                )
                _all_concepts: list[str] = []
                for _p in ALL_PROVIDERS:
                    _concepts = find_concepts_by_code(_p, _indicator_name)
                    if _concepts:
                        _all_concepts = _concepts
                        break
                if _all_concepts:
                    # First prefer a concept that matches the semantic label or
                    # the raw follow-up text, then prefer one available on the
                    # target provider.
                    # e.g., PRC_HICP_AIND maps to both "hicp_inflation"
                    # (Eurostat-only) and "inflation" (WorldBank/FRED/etc).
                    # When switching to WorldBank, pick "inflation".
                    _best_concept = _all_concepts[0]
                    _target_provider = provider  # already set to the new provider
                    _context_queries = []
                    _semantic_label = str(params.get("__semantic_indicator_label") or "").strip()
                    if _semantic_label:
                        _context_queries.append(_semantic_label)
                    if _saved_query:
                        _context_queries.append(str(_saved_query))
                    for _context_query in _context_queries:
                        _context_concept = find_concept_by_term(_context_query)
                        if (
                            _context_concept
                            and _context_concept in _all_concepts
                            and is_provider_available(_context_concept, _target_provider)
                        ):
                            _best_concept = _context_concept
                            break
                    for _c in _all_concepts:
                        if is_provider_available(_c, _target_provider):
                            _best_concept = _c
                            break
                    _resolution_query = _best_concept.replace("_", " ")
                    logger.info(
                        "Delta-resolved: indicator '%s' is a code, "
                        "using concept '%s' for cross-provider resolution "
                        "(target=%s, candidates=%s)",
                        _indicator_name, _resolution_query,
                        _target_provider, _all_concepts,
                    )
                    # Also update the intent's indicators so downstream
                    # resolution uses the concept name, not the code
                    intent.indicators = [_resolution_query]

            intent.originalQuery = _resolution_query
            logger.info(
                "Delta-resolved: indicator changed to '%s', resolving (query overridden from '%s')",
                _resolution_query, _saved_query,
            )
            params = await svc._resolve_indicator_for_fetch(provider, intent, params)
            intent.originalQuery = _saved_query
    else:
        # PHASE B: Resolve indicator code via unified resolution pipeline
        params = await svc._resolve_indicator_for_fetch(provider, intent, params)

    _assert_provider_map_authority(provider, intent, params)

    internal_param_keys = {
        "__fallback_excluded_providers",
        "__qualifier_checked",
        "__geo_split_child",
        "__delta_resolved",
        "__delta_indicator_changed",
        "__statscan_product_authority",
        "__semantic_provider_locked",
        "__indicator_options",
        # Read by resolve_indicator_for_fetch (above) to exclude dead codes;
        # must not reach the provider dispatch or cache identity.
        "__exclude_indicator_codes",
    }
    # Window continuity on delta paths: a follow-up that keeps the indicator
    # and carries the window the PREVIOUS turn already served (dates present
    # before this fetch applies any default) must not have that window
    # stripped up front just because the series is exact-locked — the prior
    # turn served data in it, and the user's follow-up continues it. Read
    # here because __delta_* flags are removed just below. Provenance stays
    # truthful (a 'default' window stays 'default'), so the broadened
    # exact-window retry still widens it when a newly added member has no
    # observations inside it.
    carried_delta_window = bool(
        params.get("__delta_resolved")
        and not params.get("__delta_indicator_changed")
        and any(params.get(key) for key in ("startDate", "endDate", "start_year", "end_year"))
    )
    if _has_statscan_mechanical_dimension_dispatch_authority(params):
        # These flags are still needed by the dispatch-level no-shortcut gate.
        # They authorize only a verified StatsCan table coordinate operation,
        # not a broader semantic provider/indicator map.
        internal_param_keys = internal_param_keys - {
            "__delta_resolved",
            "__delta_indicator_changed",
            "__statscan_product_authority",
        }
    if any(key in params for key in internal_param_keys):
        params = {k: v for k, v in params.items() if k not in internal_param_keys}
        intent.parameters = params

    # Ensure cache identity distinguishes semantically different requests even
    # when the provider-specific resolution pipeline leaves params["indicator"]
    # unset (common for dynamic providers and some follow-up deltas).
    if "indicator" not in params and intent.indicators and len(intent.indicators) == 1:
        indicator_for_cache = str(intent.indicators[0] or "").strip()
        if indicator_for_cache:
            params["indicator"] = indicator_for_cache
            intent.parameters = params

    # Apply smart default time ranges based on provider
    logger.info(f"Before defaults - provider={provider}, startDate={params.get('startDate')}, endDate={params.get('endDate')}")
    params = apply_default_time_range(provider, params)
    logger.info(f"After defaults - startDate={params.get('startDate')}, start_year={params.get('start_year')}")
    if provider == "COINGECKO" and params.get("__default_time_range_applied") == "coingecko_30d":
        params = dict(params)
        if not _coingecko_has_historical_time_scope(intent.originalQuery or ""):
            params.pop("startDate", None)
            params.pop("endDate", None)
        params.pop("__default_time_range_applied", None)
    intent.parameters = params

    # For ExchangeRate queries, extract currency pairs BEFORE cache lookup
    if provider == "EXCHANGERATE":
        params = extract_exchange_rate_params(params, intent)
        intent.parameters = params
        logger.info(f"ExchangeRate: Cache params after currency extraction: baseCurrency={params.get('baseCurrency')}, targetCurrency={params.get('targetCurrency')}")

    # Include originalQuery in params for cache key differentiation.
    # This ensures "CPI shelter Canada" and "CPI energy Canada" get
    # separate cache entries even though both resolve to indicator=CPI.
    if intent.originalQuery and "__original_query" not in params:
        params["__original_query"] = intent.originalQuery

    # Exact provider title/code matches are provider-native catalog targets. If
    # the user did not ask for a time window, strip friendly default dates before
    # dispatch/cache materialization so sparse/stale exact series are not
    # blocked by a recent default window. Explicit time scopes remain strict.
    def _strip_default_window_for_exact_match() -> bool:
        return bool(
            is_exact_match_locked(params)
            and not carried_delta_window
            and not _user_set_time_scope(params, intent.originalQuery or "")
            and any(params.get(key) for key in ("startDate", "endDate", "start_year", "end_year"))
        )

    if (
        provider in {"FRED", "WORLDBANK", "WORLD BANK", "STATSCAN", "STATISTICS CANADA", "IMF"}
        and _strip_default_window_for_exact_match()
    ):
        params = dict(params)
        for key in ("startDate", "endDate", "start_year", "end_year"):
            params.pop(key, None)
        intent.parameters = params

    # Provider-locked exact-title Comtrade rows represent a specific commodity
    # code/title.  A friendly recent default window can make discontinued or
    # sparse flows look unsupported, so remove only non-user-specified defaults
    # before dispatch/cache identity materialization.
    if (
        provider == "COMTRADE"
        and _strip_default_window_for_exact_match()
    ):
        params = dict(params)
        for key in ("startDate", "endDate", "start_year", "end_year"):
            params.pop(key, None)
        intent.parameters = params

    if provider == "FRED" and is_exact_match_locked(params):
        exact_indicator = str(params.get("indicator") or "").strip()
        if exact_indicator:
            params = dict(params)
            changed = False
            for key in ("seriesId", "series_id"):
                raw_series_id = str(params.get(key) or "").strip()
                if raw_series_id and raw_series_id != exact_indicator:
                    params.pop(key, None)
                    changed = True
            if changed:
                intent.parameters = params

    if provider == "FRED":
        prepared_params = _prepare_fred_country_scope_params({}, params, intent)
        if prepared_params != params:
            params = prepared_params
            intent.parameters = params

    if not intent.clarificationNeeded:
        params = normalize_aggregate_geography(provider, params)
        intent.parameters = params

    execution_plan = materialize_execution_plan(
        execution_plan,
        provider=provider,
        intent=intent,
        params=params,
    )

    if provider == "FRED":
        _raise_if_fred_country_scope_unsupported(
            dict(execution_plan.provider_request or {}),
            params,
            intent,
        )

    cached = await svc._get_from_cache(execution_plan.provider, execution_plan.params)
    if cached:
        logger.info("Cache hit for %s", execution_plan.provider)
        result_list = cached if isinstance(cached, list) else [cached]
        svc._normalize_bis_metadata_labels(result_list)
        _restore_semantic_indicator_label_for_generic_metadata(result_list, params)
        result_list = _attach_requested_series_disclosure(result_list, params, intent)
        result_list = _disclose_frequency_mismatch(result_list, params, getattr(intent, "language", None))
        if tracker:
            with tracker.track(
                "cache_hit",
                "Served instantly from cache",
                {
                    "provider": execution_plan.provider,
                    "indicator_count": len(intent.indicators),
                },
            ) as update_cache_metadata:
                update_cache_metadata({
                    "series_count": len(result_list),
                    "cached": True,
                })
                return result_list
        return result_list

    logger.info("Cache miss for %s, fetching from API", execution_plan.provider)

    exact_query_without_time_scope = bool(
        is_exact_match_locked(params)
        and not _user_set_time_scope(params, intent.originalQuery or "")
    )

    window_probe_attempted = False

    async def _probe_user_time_window() -> Optional[List[NormalizedData]]:
        nonlocal window_probe_attempted
        if (
            window_probe_attempted
            or not _user_set_time_scope(params, intent.originalQuery or "")
            or not any(params.get(key) for key in ("startDate", "endDate", "start_year", "end_year"))
        ):
            return None
        window_probe_attempted = True
        probe_intent = intent.model_copy(deep=True)
        probe_params = copy.deepcopy(params)
        for key in ("startDate", "endDate", "start_year", "end_year"):
            probe_params.pop(key, None)
        probe_intent.parameters = probe_params
        probe_plan = materialize_execution_plan(
            execution_plan=execution_plan.model_copy(deep=True),
            provider=provider, intent=probe_intent, params=probe_params,
        )
        try:
            probe_result = await fetch_from_provider_dispatch(svc, probe_intent, probe_plan)
        except Exception:
            return None  # A failed diagnostic establishes no window/coverage fact.
        recovered, latest = _classify_time_window_probe(params, probe_result)
        if recovered:
            return recovered
        if latest:
            # The probe is UNWINDOWED, so its minimum observation is the only
            # provable "earliest retrieved" bound (never the windowed fetch).
            earliest = _probe_earliest_observation(probe_result)
            raise _window_marker_error(
                "no_data_in_requested_window: the selected series has no "
                "observations inside the user-requested time window. "
                f"latest_available={latest}"
                + (f" earliest_available={earliest}" if earliest else ""),
                provider=execution_plan.provider or provider,
                params=params,
                latest=latest,
                earliest=earliest,
                series=probe_result,
            )
        return None

    async def _dispatch_with_exact_window_retry() -> List[NormalizedData]:
        nonlocal execution_plan, params
        try:
            return await fetch_from_provider_dispatch(svc, intent, execution_plan)
        except DataNotAvailableError as exc:
            if "no_data_in_requested_window" in str(exc):
                raise
            recovered = await _probe_user_time_window()
            if recovered:
                return recovered
            if not (
                exact_query_without_time_scope
                and any(params.get(key) for key in ("startDate", "endDate", "start_year", "end_year"))
            ):
                raise
            broadened_params = dict(params)
            for key in ("startDate", "endDate", "start_year", "end_year"):
                broadened_params.pop(key, None)
            retry_plan = materialize_execution_plan(
                execution_plan=None,
                provider=provider,
                intent=intent,
                params=broadened_params,
            )
            logger.info(
                "Retrying exact %s query without default time window after provider no-data: indicator=%s query=%s",
                provider,
                broadened_params.get("indicator"),
                intent.originalQuery,
            )
            retry_result = await fetch_from_provider_dispatch(svc, intent, retry_plan)
            params = broadened_params
            execution_plan = retry_plan
            intent.parameters = broadened_params
            return retry_result

    if tracker:
        provider_display = provider_progress_labels().get(
            execution_plan.provider, execution_plan.provider
        )
        fetch_message = f"Retrieving data from {provider_display}..."

        with tracker.track(
            "fetching_data",
            fetch_message,
            {
                "provider": execution_plan.provider,
                "indicator_count": len(intent.indicators),
            },
        ) as update_fetch_metadata:
            provider_start = time.perf_counter()
            result = await _dispatch_with_exact_window_retry()
            provider_elapsed = time.perf_counter() - provider_start
            logger.info(f"Provider {execution_plan.provider} fetch: {provider_elapsed:.2f}s")
            update_fetch_metadata({
                "series_count": len(result),
                "cached": False,
                "fetch_time_ms": round(provider_elapsed * 1000, 1),
            })
    else:
        provider_start = time.perf_counter()
        result = await _dispatch_with_exact_window_retry()
        provider_elapsed = time.perf_counter() - provider_start
        logger.info(f"Provider {execution_plan.provider} fetch: {provider_elapsed:.2f}s")
    if (
        exact_query_without_time_scope
        and (not result or (len(result) == 1 and not result[0].data))
        and any(params.get(key) for key in ("startDate", "endDate", "start_year", "end_year"))
    ):
        broadened_params = dict(params)
        for key in ("startDate", "endDate", "start_year", "end_year"):
            broadened_params.pop(key, None)
        intent.parameters = broadened_params
        retry_plan = materialize_execution_plan(
            execution_plan=None,
            provider=provider,
            intent=intent,
            params=broadened_params,
        )
        logger.info(
            "Retrying exact %s query without default time window: indicator=%s query=%s",
            provider,
            broadened_params.get("indicator"),
            intent.originalQuery,
        )
        retry_result = await fetch_from_provider_dispatch(svc, intent, retry_plan)
        if retry_result and not (len(retry_result) == 1 and not retry_result[0].data):
            result = retry_result
            params = broadened_params
            execution_plan = retry_plan

    if not result or not any(series.data for series in result):
        recovered = await _probe_user_time_window()
        if recovered:
            result = recovered

    _had_observations = bool(
        result and any(getattr(series, "data", None) for series in result)
    )
    _latest_obs = ""
    if _had_observations:
        for _series in result:
            for _point in getattr(_series, "data", None) or []:
                _pd = str(
                    _point.get("date") if isinstance(_point, dict)
                    else getattr(_point, "date", "") or ""
                )
                if len(_pd) >= 4 and _pd[:4].isdigit() and _pd > _latest_obs:
                    _latest_obs = _pd
    _pre_window_series = list(result or [])
    result = _enforce_user_time_window(params, intent.originalQuery or "", result)
    if _had_observations and not result:
        # The provider HAS this series — every observation just falls outside
        # the user's requested window (not-yet-published periods, frozen
        # series, future starts). Marked distinctly because NO alternate code
        # and NO fallback provider can fix a window constraint; the
        # latest-available date lets the response layer tell the user exactly
        # how far the data goes instead of a generic "no data".
        # No earliest bound here: this payload came from a WINDOWED request,
        # so its minimum date is not the series' first observation.
        raise _window_marker_error(
            "no_data_in_requested_window: the series exists but has no "
            "observations inside the user-requested time window. "
            f"latest_available={_latest_obs or 'unknown'}",
            provider=execution_plan.provider or provider,
            params=params,
            latest=_latest_obs,
            series=_pre_window_series,
        )

    if not result or (len(result) == 1 and not result[0].data):
        raise DataNotAvailableError(
            f"No data available from {execution_plan.provider} for the requested parameters. "
            f"The data may not exist or may not be available for the specified time period or location."
        )

    svc._normalize_bis_metadata_labels(result)
    _restore_semantic_indicator_label_for_generic_metadata(result, params)

    # Validate data before returning
    from backend.services.data_validator import get_data_validator
    validator = get_data_validator()
    for data_series in result:
        validation_result = validator.validate(data_series)
        validator.log_validation_results(data_series, validation_result)
        if not validation_result.valid or validation_result.confidence < 0.5:
            logger.warning(
                f"Data quality concern for {data_series.metadata.indicator if data_series.metadata else 'UNKNOWN'}: "
                f"confidence={validation_result.confidence:.2f}, issues={len(validation_result.issues)}"
            )

    await svc._save_to_cache(
        execution_plan.provider,
        execution_plan.params,
        result if len(result) > 1 else result[0],
    )
    # After caching: both disclosures are per-request (the requested code /
    # the language), never cached data.
    result = _attach_requested_series_disclosure(result, params, intent)
    return _disclose_frequency_mismatch(result, params, getattr(intent, "language", None))


async def fetch_multi_indicator_data(svc: Any, intent: ParsedIntent) -> List[NormalizedData]:
    """Fetch data for multiple indicators by making separate API calls for each.

    Args:
        svc: QueryService instance
        intent: Parsed intent with multiple indicators

    Returns:
        Combined list of NormalizedData from all indicator fetches.
    """
    from ..services.parameter_validator import ParameterValidator

    all_data: List[NormalizedData] = []
    explicit_provider = svc._normalize_provider_alias(
        svc._detect_explicit_provider(intent.originalQuery or "")
    )

    if _normalize_provider_name(explicit_provider or intent.apiProvider) == "IMF":
        supportability_reason = imf_exact_provider_surface_supportability_reason(
            intent.originalQuery or "",
            intent.indicators or [],
            intent.parameters or {},
        )
        if supportability_reason:
            raise DataNotAvailableError(
                "IMF query targets a detailed IMF public-data surface that is "
                "not yet executable by OpenEcon's production IMF dataset-family "
                "routing. This is a fail-closed supportability block, not a "
                "broad-proxy substitution. "
                f"reason={supportability_reason}"
            )

    # Ensure default time periods are applied to base intent first
    if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
        logger.info("Applying default time periods to multi-indicator query...")
        ParameterValidator.apply_default_time_periods(intent)

    # Create separate intents for each indicator
    fetch_tasks = []
    for indicator in intent.indicators:
        params = dict(intent.parameters) if intent.parameters else {}

        params["indicator"] = indicator
        # Strip internal flags — each sub-indicator needs independent
        # resolution through the full pipeline.
        # Without this, "GDP" gets treated as a literal FRED code instead of
        # being resolved to the best GDP indicator.
        params.pop("__delta_resolved", None)
        params.pop("__delta_indicator_changed", None)

        single_provider = _normalize_provider_name(intent.apiProvider)
        if explicit_provider:
            single_provider = explicit_provider
        else:
            try:
                routing_intent = ParsedIntent(
                    apiProvider=single_provider,
                    indicators=[indicator],
                    parameters=dict(params),
                    clarificationNeeded=False,
                    originalQuery=intent.originalQuery,
                )
                routed_provider = await svc._select_routed_provider(
                    routing_intent,
                    f"{indicator} {intent.originalQuery or ''}".strip(),
                )
                if routed_provider:
                    single_provider = routed_provider
            except Exception as exc:
                logger.debug(
                    "Multi-indicator provider routing failed for '%s': %s",
                    indicator,
                    exc,
                )

        # Build a contextual query for indicator resolution.
        # Include country info so the resolution system has full context.
        country_text = ""
        countries_list = params.get("countries") or []
        country_single = params.get("country") or ""
        if countries_list:
            country_text = f" for {', '.join(str(c) for c in countries_list[:3])}"
        elif country_single:
            country_text = f" for {country_single}"
        # Region-as-series providers (see provider_strategy) need the region in
        # the retrieval text or the national series wins for a state request.
        indicator = region_qualified_indicator_text(intent, single_provider, indicator)
        narrowed_query = f"{indicator}{country_text}"

        # Provenance: this query string is PIPELINE-SYNTHESIZED (indicator +
        # country text), not user-typed. The exact-title fail-closed contract
        # applies only to what the user actually wrote — a synthesized string
        # that happens to look title-shaped ("unemployment rate for United
        # States") must fall through to the selector on an exact-title miss,
        # not dead-end the whole sub-fetch (observed live: the unemployment
        # leg of "US GDP and unemployment rate" failed entirely).
        params = {**params, "__synthetic_subquery": True}

        single_intent = ParsedIntent(
            apiProvider=single_provider,
            indicators=[indicator],
            parameters=params,
            clarificationNeeded=False,
            confidence=intent.confidence,
            recommendedChartType=intent.recommendedChartType,
            originalQuery=narrowed_query,
        )

        task = retry_async(
            lambda i=single_intent: fetch_data(svc, i),
            max_attempts=2,
            initial_delay=0.5,
            # Do NOT retry DataNotAvailableError here: providers already run
            # transient transport retries (3 attempts via the BaseProvider
            # helpers) before raising it terminally, so re-entering the
            # provider doubles every HTTP attempt and burns API quota, while
            # deterministic "no data" never heals on retry. It propagates
            # immediately (retry_async only catches `exceptions`). The outer
            # retry remains for orchestration-level timeouts and raw httpx
            # errors that escape provider wrapping.
            exceptions=(httpx.HTTPError, asyncio.TimeoutError),
        )
        fetch_tasks.append(task)

    # Fetch all indicators in parallel with a total timeout
    num_countries = len(
        (intent.parameters.get("countries") or []) if intent.parameters else []
    )
    total_timeout = min(90, 45 + max(0, num_countries - 3) * 5)
    logger.info(
        "Fetching %s indicators in parallel (timeout=%ds, countries=%d)...",
        len(fetch_tasks), total_timeout, num_countries,
    )

    # Create explicit tasks so that, on a total-timeout, indicators that have
    # ALREADY completed are preserved instead of being discarded. asyncio.wait
    # returns (done, pending) when the timeout elapses; we harvest results from
    # the done tasks and cancel the pending ones. This keeps every successful
    # indicator even if one slow indicator blows the overall budget.
    tasks = [asyncio.ensure_future(t) for t in fetch_tasks]
    done, pending = await asyncio.wait(
        tasks,
        timeout=total_timeout,
        return_when=asyncio.ALL_COMPLETED,
    )

    if pending:
        logger.warning(
            "Multi-indicator fetch timed out after %ds -- returning %d/%d "
            "completed indicators and cancelling %d pending",
            total_timeout, len(done), len(tasks), len(pending),
        )
        for task in pending:
            task.cancel()

    # Harvest results in original task-creation order so the enumerate() loop
    # below stays index-aligned with intent.indicators for logging. Completed
    # tasks contribute their result (or their exception); still-pending tasks
    # contribute a TimeoutError sentinel that the loop already tolerates.
    results: list = []
    for task in tasks:
        if task in done:
            exc = task.exception()
            if exc is not None:
                results.append(exc)
            else:
                results.append(task.result())
        else:
            results.append(asyncio.TimeoutError("indicator fetch timed out"))

    # Collect successful results, tracking which indicators produced nothing so
    # a PARTIAL success can surface a transparency note instead of silently
    # dropping the missing series (F6). An empty list counts as a failure for
    # that indicator (a provider that returned [] served nothing usable).
    failed_indicators: list[str] = []
    for i, result in enumerate(results):
        indicator_name = intent.indicators[i] if i < len(intent.indicators) else "unknown"
        if isinstance(result, Exception):
            logger.warning("Failed to fetch indicator %s: %s", indicator_name, result)
            failed_indicators.append(indicator_name)
            continue
        if isinstance(result, list):
            if result:
                all_data.extend(result)
            else:
                failed_indicators.append(indicator_name)
        elif result is not None:
            all_data.append(result)
        else:
            failed_indicators.append(indicator_name)

    if not all_data:
        raise DataNotAvailableError(
            f"Could not fetch any of the requested indicators: {', '.join(intent.indicators)}"
        )

    # Partial success: at least one indicator resolved but others did not.
    # Attach a transparency note (reusing the metadata.notes convention from the
    # StatsCan coordinate-substitution fix) naming the missing indicators so the
    # response can explain the gap rather than presenting a silently short list.
    if failed_indicators:
        missing = ", ".join(dict.fromkeys(failed_indicators))
        # Localize the transparency note to the query's language (available on
        # the intent here); English is the guaranteed fallback.
        note = _localized_message(
            "multi_indicator_partial", getattr(intent, "language", None), missing=missing,
        ) or (
            f"Could not fetch data for: {missing}. The results shown cover only "
            "the indicators that resolved successfully; the missing series may "
            "be unavailable from the selected provider(s) at this scope."
        )
        first_meta = all_data[0].metadata
        existing_notes = list(first_meta.notes or [])
        existing_notes.append(note)
        first_meta.notes = existing_notes

    logger.info("Successfully fetched %s datasets for %s indicators", len(all_data), len(intent.indicators))
    return all_data
