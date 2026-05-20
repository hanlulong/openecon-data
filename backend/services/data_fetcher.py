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
from datetime import datetime
import logging
import re
import time
from typing import Any, List, Optional, TYPE_CHECKING

from ..models import ExecutionPlan, Metadata, NormalizedData, ParsedIntent
from ..services.indicator_resolution import is_exact_match_locked
from ..utils.imf_supportability import imf_exact_provider_surface_supportability_reason
from ..utils.providers import ALL_PROVIDERS, normalize_provider_name
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
_KNOWN_ISO2_COUNTRIES = {
    code
    for alias, code in CountryResolver.COUNTRY_ALIASES.items()
    if alias.lower() == code.lower() and code.isalpha()
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
    return str(params.get("__semantic_authority") or "") in {
        "exact_user_input",
        "llm_adjudication",
        "post_fetch_semantic_judge",
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


def _years_from_params(params: dict) -> tuple[Optional[int], Optional[int]]:
    start_year = params.get("start_year")
    end_year = params.get("end_year")
    if start_year is None and params.get("startDate"):
        start_year = int(str(params["startDate"])[:4])
    if end_year is None and params.get("endDate"):
        end_year = int(str(params["endDate"])[:4])
    return start_year, end_year


def _query_has_explicit_time_scope(query: str) -> bool:
    """Detect whether the user explicitly constrained the requested time window."""
    query_text = str(query or "").strip()
    if not query_text:
        return False
    recency_query_text = _CURRENT_MEASUREMENT_CUE_RE.sub(" ", query_text)
    if _RECENCY_CUE_RE.search(recency_query_text):
        return True
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
            }
            and not str(key).startswith("_")
        }

    if provider_norm == "STATSCAN":
        contract["indicator"] = str(code or "").strip()
        contract["country"] = contract["country_scope"][0] if contract["country_scope"] else None
        contract["dimensions"] = dict(params.get("__dimensions") or params.get("dimensions") or {})
        contract["decomposition_axis"] = str(params.get("__statscan_decomposition_axis") or "").strip() or None
        contract["product_id"] = str(params.get("__statscan_product_id") or "").strip() or None

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
        contract["days"] = params.get("days")
        contract["start_date"] = params.get("startDate")
        contract["end_date"] = params.get("endDate")

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
        if code == iso2 and len(alias) > 2
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
        if code == normalized and len(alias) > 2
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


def _fred_country_context_query(
    params: dict[str, Any],
    intent: Optional[ParsedIntent],
    raw_country: Any,
    iso2: str,
) -> str:
    """Build provider-search text from query/country context, not hardcoded maps."""
    original_query = str(
        params.get("__original_query")
        or (intent.originalQuery if intent else "")
        or ""
    ).strip()
    if original_query:
        query_text = re.sub(
            r"\b(?:from|use|using|via)\s+fred\b",
            " ",
            original_query,
            flags=re.IGNORECASE,
        )
        query_text = re.sub(r"\s+", " ", query_text).strip(" ,;:")
        if query_text:
            return query_text

    country_label = _country_label_for_iso2(iso2, raw_country)
    indicator = str(
        params.get("__semantic_indicator_label")
        or params.get("indicator")
        or ((intent.indicators or [""])[0] if intent and intent.indicators else "")
        or ""
    ).strip()
    return re.sub(r"\s+", " ", f"{country_label} {indicator}").strip()


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
    """
    if params.get("__fred_country_scope_discovery"):
        return params

    requested_scope = _fred_requested_country_scope(provider_request, params)
    normalized_scope = [
        (country, _normalize_known_country_to_iso2(country))
        for country in requested_scope
    ]
    non_us_scope = [
        (country, iso2)
        for country, iso2 in normalized_scope
        if iso2 and iso2 != "US"
    ]
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
    prepared["indicator"] = search_query
    prepared["__semantic_indicator_label"] = search_query
    prepared["__fred_country_scope_discovery"] = True
    prepared["__fred_requested_country"] = iso2
    prepared.pop("seriesId", None)
    prepared.pop("series_id", None)
    prepared.pop("__exact_provider_code_match", None)
    prepared.pop("__exact_indicator_title_match", None)
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
    code = _execution_plan_candidate_code(intent, params)
    provider_norm = _normalize_provider_name(provider) or "UNKNOWN"
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

    indicator_lower = " ".join(intent.indicators).lower() if intent.indicators else ""
    metric_text = f"{indicator_lower} {query_lower}".strip()

    # Historical data request
    if params.get("startDate") or params.get("endDate") or params.get("days"):
        hist_metric = "price"
        if any(t in metric_text for t in ["market cap", "market capitalization", "marketcap"]):
            hist_metric = "market_cap"
        elif any(t in metric_text for t in ["volume", "trading volume", "24h volume"]):
            hist_metric = "volume"

        if params.get("startDate") and params.get("endDate"):
            series_list = []
            for coin_id in coin_ids:
                data = await coingecko_provider.get_historical_data_range(
                    coin_id=coin_id, vs_currency=vs_currency,
                    from_date=params["startDate"], to_date=params["endDate"],
                    metric=hist_metric,
                )
                series_list.extend(data)
            return series_list
        else:
            days = params.get("days", 30)
            series_list = []
            for coin_id in coin_ids:
                data = await coingecko_provider.get_historical_data(
                    coin_id=coin_id, vs_currency=vs_currency,
                    metric=hist_metric, days=days,
                )
                series_list.extend(data)
            return series_list

    # Current data
    ranking_keywords = ["top", "top 10", "top 5", "top 20", "ranking", "rankings", "largest", "biggest"]
    is_ranking = any(t in metric_text for t in ranking_keywords)

    if is_ranking and "market cap" in metric_text:
        top_n_match = _TOP_N_RE.search(query_lower)
        per_page = int(top_n_match.group(1)) if top_n_match else 10
        per_page = max(1, min(250, per_page))
        return await coingecko_provider.get_market_data(
            vs_currency=vs_currency, order="market_cap_desc", per_page=per_page,
        )

    metric = "price"
    if any(t in metric_text for t in ["volume", "trading volume", "24h volume", "24-hour volume"]):
        metric = "volume"
    elif any(t in metric_text for t in ["market cap", "market capitalization", "marketcap"]):
        metric = "market_cap"
    elif re.search(
        r"(?<![a-z0-9])(?:24h|24-hour|24 hour)\s+(?:price\s+)?change(?![a-z0-9])"
        r"|(?<![a-z0-9])price\s+change(?![a-z0-9])"
        r"|(?<![a-z0-9])(?:percent|percentage|%)\s+change(?![a-z0-9])",
        metric_text,
    ):
        metric = "24h_change"

    return await coingecko_provider.get_simple_price(
        coin_ids=coin_ids, vs_currency=vs_currency, metric=metric,
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

    fred_series_id = None
    if target_upper in fred_fx and target_upper != "USD":
        fred_series_id = fred_fx[target_upper]
    elif base_upper in fred_fx and target_upper == "USD":
        fred_series_id = fred_fx[base_upper]
    elif base_upper != "USD" and target_upper != "USD":
        fred_series_id = fred_fx.get(base_upper) or fred_fx.get(target_upper)

    if not fred_series_id:
        return None

    try:
        series = await svc.fred_provider.fetch_series({
            "seriesId": fred_series_id,
            "startDate": params.get("startDate"),
            "endDate": params.get("endDate"),
        })
        series.metadata.indicator = f"{base_upper} to {target_upper} Exchange Rate"
        series.metadata.source = "FRED (Federal Reserve)"
        return [series]
    except Exception as e:
        logger.warning(f"FRED exchange rate fallback failed: {e}")
        return None


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
            # Single currency found - treat as "X to USD" or "USD to X"
            code = found_codes[0]
            if code == "USD":
                # Query is about USD, but we need a target
                # Default to EUR as most common pair
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
        # Ensure params has indicator set
        if not params.get("indicator") and intent.indicators:
            params = {**params, "indicator": intent.indicators[0]}

        # Handle multiple indicators for FRED
        if len(intent.indicators) > 1:
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
                "indicator": fred_request.get("series_id") or params.get("indicator"),
            }
            if fred_params.get("seriesId") and fred_params.get("seriesId") != fred_params.get("indicator"):
                fred_params.pop("seriesId", None)
            if fred_params.get("series_id") and fred_params.get("series_id") != fred_params.get("indicator"):
                fred_params.pop("series_id", None)
            series = await svc.fred_provider.fetch_series(fred_params)
            _validate_fred_country_scope_result([series], fred_request, fred_params)
            return [series]

    if provider in {"WORLDBANK", "WORLD BANK"}:
        worldbank_request = dict(execution_plan.provider_request or {})
        resolved_indicator = worldbank_request.get("indicator") or params.get("indicator")
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
        series_list = await svc.comtrade_provider.fetch_trade_data(
            reporter=reporter_value,
            reporters=reporters_value,
            partner=params.get("partner"),
            commodity=_comtrade_dispatch_commodity(params),
            flow=params.get("flow"),
            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            frequency=params.get("frequency", "annual"),
        )
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
        return await svc.bis_provider.fetch_indicator(
            indicator=indicator,
            country=params.get("country"),
            countries=params.get("countries"),
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

    raise DataNotAvailableError(
        f"Provider {intent.apiProvider} is not yet implemented. Available providers: FRED, World Bank, Comtrade, StatsCan, IMF, ExchangeRate, BIS, Eurostat, OECD, CoinGecko"
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
                        "indicatorLabel": params.get("__semantic_indicator_label") or str(intent.indicators[0] if intent.indicators else _base),
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
                        "indicatorLabel": params.get("__semantic_indicator_label") or str(intent.indicators[0] if intent.indicators else _base),
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
                        "indicatorLabel": params.get("__semantic_indicator_label") or str(intent.indicators[0] if intent.indicators else indicator or _base),
                        "periods": params.get("periods", 240),
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
                    periods=params.get("periods", 240),
                    indicator_label=params.get("__semantic_indicator_label") or str(intent.indicators[0] if intent.indicators else _base),
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
            "periods": params.get("periods", 240),
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
            "indicatorLabel": params.get("__semantic_indicator_label") or str(intent.indicators[0] if intent.indicators else indicator or state_product_id),
            "periods": params.get("periods", 240),
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
        _indicator_label = params.get("__semantic_indicator_label") or str(
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
                        periods=params.get("periods", 240),
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
        _indicator_label_for_dim = params.get("__semantic_indicator_label") or str(
            intent.indicators[0] if intent.indicators else _base
        )

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
                        "periods": params.get("periods", 240),
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
                    periods=params.get("periods", 240),
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
                "indicatorLabel": params.get("__semantic_indicator_label") or str(intent.indicators[0] if intent.indicators else indicator),
                "geography": params.get("geography"),
                "periods": params.get("periods", 240),
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
    resolved_indicator = str(
        imf_request.get("indicator")
        or imf_request.get("code")
        or params.get("indicator")
        or ""
    ).strip()
    request_start_year = imf_request.get("start_year")
    request_end_year = imf_request.get("end_year")
    if request_start_year is None and params.get("startDate"):
        request_start_year = int(str(params["startDate"])[:4])
    if request_end_year is None and params.get("endDate"):
        request_end_year = int(str(params["endDate"])[:4])

    # Resolve countries/regions to list of country codes
    resolved_countries: List[str] = []
    if isinstance(countries_param, list):
        for item in countries_param:
            resolved_countries.extend(svc.imf_provider._resolve_countries(item))
    elif isinstance(countries_param, str):
        resolved_countries = svc.imf_provider._resolve_countries(countries_param)
    else:
        resolved_countries = [str(imf_request.get("default_country") or "USA")]

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
        return all_data
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
            return all_data
        else:
            indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else ""))
            return await _fetch_imf_series_batch(
                indicator=resolved_indicator or indicator,
                countries=[country],
                start_year=request_start_year,
                end_year=request_end_year,
            )


async def _fetch_from_eurostat(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
    execution_plan: Optional[ExecutionPlan] = None,
) -> List[NormalizedData]:
    """Eurostat provider dispatch."""
    eurostat_request = dict((execution_plan.provider_request or {}) if execution_plan else {})
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
    explicit_time_scope = _query_has_explicit_time_scope(original_query)
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
        if upper_country not in EU_AGGREGATES:
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
        if not _indicator_changed:
            # Indicator preserved from prior turn — use as-is, no resolution
            params["indicator"] = _indicator_name
            intent.parameters = params
            logger.info(
                "Delta-resolved: indicator unchanged, using '%s' directly",
                _indicator_name,
            )
        else:
            # New indicator or provider changed — resolve using indicator name
            # as query.  When the indicator name is a provider-specific code
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
    }
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
    if (
        provider in {"FRED", "WORLDBANK", "WORLD BANK", "STATSCAN", "STATISTICS CANADA", "IMF"}
        and is_exact_match_locked(params)
        and not _query_has_explicit_time_scope(intent.originalQuery or "")
        and any(params.get(key) for key in ("startDate", "endDate", "start_year", "end_year"))
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
        and is_exact_match_locked(params)
        and not _query_has_explicit_time_scope(intent.originalQuery or "")
        and any(params.get(key) for key in ("startDate", "endDate", "start_year", "end_year"))
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
        and not _query_has_explicit_time_scope(intent.originalQuery or "")
    )

    async def _dispatch_with_exact_window_retry() -> List[NormalizedData]:
        nonlocal execution_plan, params
        try:
            return await fetch_from_provider_dispatch(svc, intent, execution_plan)
        except DataNotAvailableError:
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
        provider_names = {
            "FRED": "Federal Reserve",
            "WORLDBANK": "World Bank",
            "COMTRADE": "UN Comtrade",
            "STATSCAN": "Statistics Canada",
            "BIS": "Bank for International Settlements",
            "EUROSTAT": "Eurostat",
            "OECD": "OECD",
            "COINGECKO": "CoinGecko",
        }
        provider_display = provider_names.get(execution_plan.provider, execution_plan.provider)
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
    return result


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
        narrowed_query = f"{indicator}{country_text}"

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

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*fetch_tasks, return_exceptions=True),
            timeout=total_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Multi-indicator fetch timed out after %ds -- returning partial results",
            total_timeout,
        )
        results = []

    # Collect successful results
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            indicator_name = intent.indicators[i] if i < len(intent.indicators) else "unknown"
            logger.warning("Failed to fetch indicator %s: %s", indicator_name, result)
            continue
        if isinstance(result, list):
            all_data.extend(result)
        else:
            all_data.append(result)

    if not all_data:
        raise DataNotAvailableError(
            f"Could not fetch any of the requested indicators: {', '.join(intent.indicators)}"
        )

    logger.info("Successfully fetched %s datasets for %s indicators", len(all_data), len(intent.indicators))
    return all_data
