"""Provider strategy helpers extracted from QueryService.

Pure functions that decide which provider is best for a given country/scope,
collect target countries from parameters, and check provider coverage.
These functions have no dependency on QueryService state.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..utils.providers import normalize_provider_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Geographic scope metadata
# ---------------------------------------------------------------------------
# Map from country-specific provider to the ISO2 codes it covers.
# "None" means global (covers everything) -- used as a sentinel.
PROVIDER_GEO_SCOPE: Dict[str, Optional[set]] = {
    "FRED": {"US"},
    "STATSCAN": {"CA"},
    # Eurostat and OECD are handled dynamically via CountryResolver
}

# Providers whose sub-country regional data lives in SEPARATE SERIES with
# region-qualified titles (FRED: "Unemployment Rate in Texas" = TXUR). For
# these, the region must be part of the indicator retrieval text or the
# national series resolves for a state request. Dimension-modeled providers
# (StatsCan geography members) must NOT get the region in the retrieval text —
# it pollutes cube selection; their dimension extraction applies the region.
# Structural data-model fact per provider, not a semantic rule.
REGION_AS_SERIES_PROVIDERS = frozenset({"FRED"})

# Home country per region-as-series provider: series for the HOME country are
# rarely country-titled (FRED "CPIAUCSL" has no "United States" in common
# usage), but INTERNATIONAL series are ("Consumer Price Index: Total for
# India") — so the country belongs in the retrieval text exactly when it is
# NOT the provider's home ("CPI inflation" + country=IN retrieved zero India
# series; the monthly Indian CPI ranks #1 once "India" joins the text).
# Structural data-model fact, same family as the region rule above.
PROVIDER_HOME_COUNTRY = {"FRED": "US"}

# Providers the pipeline may only auto-FETCH/SERVE when the user explicitly
# names them. OECD's public SDMX surface is rate-limited (60 req/hr) and its
# coverage overlaps StatsCan/Eurostat/IMF/WorldBank, so auto-fanning-out to it
# burns the request budget AND lets a national OECD series override a better
# provider's result in uncertain-match recovery (observed live: OECD national
# unemployment beat fetched StatsCan Ontario data, which the subnational
# fail-closed check then discarded — user got nothing). Capability fact,
# consulted generically; explicit user requests ("from OECD") still work.
MANUAL_ONLY_PROVIDERS = frozenset({"OECD"})


def provider_is_auto_routable(provider: str, explicit_provider: str = "") -> bool:
    """False for providers that require an explicit user request (see
    MANUAL_ONLY_PROVIDERS); True when the user named the provider."""
    normalized = normalize_provider_name(provider or "")
    if normalized not in MANUAL_ONLY_PROVIDERS:
        return True
    return normalize_provider_name(explicit_provider or "") == normalized


# Providers whose series codes are GEOGRAPHY-ENCODED: a single code names a
# specific country/region (FRED "UNRATE" = US only, "TXUR" = Texas; StatsCan
# vectors bind a province/country; COMTRADE encodes the reporter; CoinGecko has
# no country axis at all). For these, a country switch INVALIDATES a carried
# resolved code — the code cannot be reused for a different geography, so it
# must be dropped and re-resolved. Country-AGNOSTIC providers (World Bank, IMF,
# Eurostat, OECD, BIS) take the country as a SEPARATE parameter, so the SAME
# code (e.g. "NY.GDP.PCAP.CD", Eurostat "TEC00118") is correct for every
# country and MUST be preserved across a country switch. Structural provider
# data-model metadata, not a semantic rule.
GEOGRAPHY_ENCODED_PROVIDERS = frozenset({"FRED", "STATSCAN", "COMTRADE", "COINGECKO"})

# Provider-NATIVE aggregate geography codes for country groups (keys match
# CountryResolver.detect_regions_in_query outputs). When the routed provider
# publishes an official aggregate for the requested group, the pipeline serves
# it directly instead of asking "compare members or one value?" — the official
# series is population/PPP-weighted by the source (correct), while member
# averaging is not. Groups absent here (G7/G20/BRICS: no official aggregate
# series at these providers) keep the clarification. Structural reference
# data, consulted generically.
PROVIDER_GROUP_AGGREGATES: Dict[str, Dict[str, str]] = {
    "EUROSTAT": {
        "EUROZONE": "EA20",
        "EU": "EU27_2020",
    },
    "WORLDBANK": {
        "EUROZONE": "EMU",
        "EU": "EUU",
        "WORLD": "WLD",
    },
    "IMF": {
        # DataMapper accepts region aggregates for WEO-style codes.
        "EUROZONE": "EA",
    },
}


def region_qualified_indicator_text(
    intent: Any,
    provider: str,
    indicator_text: str,
    is_code: Optional[Callable[[str, str], bool]] = None,
) -> str:
    """Prepend intent.subnationalRegion to indicator retrieval text for
    region-as-series providers (REGION_AS_SERIES_PROVIDERS).

    FRED carries US state data in SEPARATE region-titled series (TXUR =
    "Unemployment Rate in Texas"), so the region must be in the retrieval text
    or the national series resolves for a state request. Returns the text
    unchanged when the provider isn't region-as-series, no region is set, the
    region is already present, or the text already looks like a provider code
    (per the injected is_code check — injected to avoid circular imports).
    StatsCan is excluded by the frozenset, so its cube selection never sees
    region text; its dimension extraction applies geography separately.
    """
    region = str(getattr(intent, "subnationalRegion", None) or "").strip()
    text = str(indicator_text or "").strip()
    if not region or not text:
        return text
    if normalize_provider_name(provider) not in REGION_AS_SERIES_PROVIDERS:
        return text
    if region.lower() in text.lower():
        return text
    if is_code is not None:
        try:
            if is_code(provider, text):
                return text
        except Exception:
            pass
    return f"{region} {text}"


def country_qualified_indicator_text(intent: Any, provider: str, indicator_text: str) -> str:
    """Append the NON-HOME country to retrieval text for region-as-series
    providers (see PROVIDER_HOME_COUNTRY): FRED's international series are
    country-TITLED, so "CPI inflation" with country=India retrieves zero
    Indian series while "CPI inflation India" ranks the monthly Indian CPI
    #1. Home-country text is left untouched (home series are not
    country-titled and adding "United States" would skew US retrieval)."""
    text = str(indicator_text or "").strip()
    provider_norm = normalize_provider_name(provider)
    if not text or provider_norm not in REGION_AS_SERIES_PROVIDERS:
        return text
    home = str(PROVIDER_HOME_COUNTRY.get(provider_norm) or "").strip().upper()
    country = str(
        (getattr(intent, "parameters", None) or {}).get("country") or ""
    ).strip()
    if not country or country.lower() in text.lower():
        return text
    try:
        from ..routing.country_resolver import CountryResolver

        iso2 = str(CountryResolver.normalize(country) or "").strip().upper()
    except Exception:
        iso2 = ""
    if not iso2 or iso2 == home:
        return text
    return f"{text} {country}"


# ---------------------------------------------------------------------------
# Country collection
# ---------------------------------------------------------------------------

def collect_target_countries(parameters: Optional[dict]) -> List[str]:
    """Extract ordered country context from query parameters.

    Returns a de-duplicated list preserving insertion order.
    """
    if not parameters:
        return []

    countries: List[str] = []
    for key in ("countries", "reporters", "partner"):
        value = parameters.get(key)
        if isinstance(value, list):
            countries.extend(str(item) for item in value if item)
        elif value:
            countries.append(str(value))

    for key in ("country", "reporter"):
        value = parameters.get(key)
        if value:
            countries.append(str(value))

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(countries))


# ---------------------------------------------------------------------------
# Scope / comparison detection
# ---------------------------------------------------------------------------

def _has_comparison_markers(query: str) -> bool:
    """Detect comparison intent from query phrasing.

    Mirrors :func:`indicator_resolution.is_comparison_query` plus additional
    multi-country markers.
    """
    query_lower = str(query or "").lower()
    comparison_re = re.search(
        r"\b(compare|comparison|versus|vs|between|across|contrast)\b",
        query_lower,
    )
    return bool(
        comparison_re
        or "member countries" in query_lower
        or "by country" in query_lower
        or "country by country" in query_lower
        or "each country" in query_lower
    )


def provider_supports_requested_scope(
    provider: str,
    query: str,
    countries: Optional[List[str]],
) -> bool:
    """Check whether *provider* can handle the requested comparison scope.

    Returns ``False`` when, for example, OECD is asked to compare more than
    8 countries in a comparison-style query (which would hit rate limits).
    """
    if not countries:
        return True

    provider_upper = normalize_provider_name(provider)
    country_count = len([c for c in countries if c])

    if provider_upper == "OECD" and country_count > 8 and _has_comparison_markers(query):
        return False

    return True


# ---------------------------------------------------------------------------
# Single-country provider selection
# ---------------------------------------------------------------------------

def get_provider_for_single_country(
    iso2: str,
    concept_query: str,
    original_provider: str,
) -> Tuple[str, Optional[str]]:
    """Return ``(provider, indicator_code)`` best suited for *one* country.

    Uses the catalog to find the best provider that covers the given
    country, falling back to *original_provider* when the catalog has no
    opinion, and to WorldBank as the last resort (global coverage).
    """
    from .catalog_service import find_concept_by_term, get_best_provider
    from .provider_fallback import provider_covers_country_list

    concept = find_concept_by_term(concept_query)
    if concept:
        prov, code, conf = get_best_provider(concept, countries=[iso2])
        if prov and conf > 0.0:
            return normalize_provider_name(prov), code

    # If the original provider actually covers this country, keep it.
    if provider_covers_country_list(original_provider, [iso2]):
        return original_provider, None

    # Last resort: WorldBank has global coverage.
    return "WORLDBANK", None
