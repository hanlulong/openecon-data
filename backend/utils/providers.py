"""Canonical provider identity registry and shared provider constants.

Single source of truth for provider IDENTITY: canonical names, display/source
labels, the indicators.db name, and the alias tiers used to recognise a
provider name in LLM output, internal labels, and user text.  Every
identity/alias/display view in the codebase is DERIVED from ``PROVIDER_SPECS``
so a new provider cannot be half-registered again (ChinaMacro was missing
from the explicit-request detector and the substitution note while present
elsewhere).

This module holds reference data only — no query-meaning keywords, topic
cues, or per-indicator mappings belong here.  Capability declarations
(dispatch, cache TTLs, rate limits, lookbacks, geography predicates) live with
their consumers.

Alias tiers (kept SEPARATE on purpose — explicit detection is the router's
absolute top authority, so one over-broad alias hard-routes many queries):

- ``normalize_aliases``: lenient provider-name strings (LLM output, internal
  labels, served ``metadata.source`` labels).  May contain loose forms such as
  ``"un"`` or ``"exchange rate"``; NEVER matched against free user text.
- ``bare_aliases``: a standalone mention in user text counts as an explicit
  request.  Only coined/unambiguous names belong here.
- ``directive_aliases``: count as an explicit request only after a source
  directive ("from", "using", "via", "according to", 来自, 使用 ...).  Names
  that are also ordinary language ("china macro") live here, never bare.

Registry ORDER is behavior: the bare tier is first-match in this order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Canonical tuple of all data providers that support indicator codes.
# Used for cross-provider indicator code detection and reverse concept lookups.
# (Capability declaration, not identity — intentionally not derived.)
ALL_PROVIDERS: tuple[str, ...] = (
    "FRED", "WORLDBANK", "IMF", "EUROSTAT", "BIS", "OECD", "STATSCAN",
)


@dataclass(frozen=True)
class ProviderSpec:
    """Identity reference data for one data provider."""

    canonical: str  # uppercase namespace: "WORLDBANK"
    name: str  # router / indicators.db / option-label form: "WorldBank"
    display_name: str  # user-facing name: "World Bank"
    description: str  # one-line informational-answer copy
    normalize_aliases: Tuple[str, ...]
    bare_aliases: Tuple[str, ...] = ()
    directive_aliases: Tuple[str, ...] = ()
    # A leading "<name> " (e.g. "IMF GDP forecast") counts as explicit,
    # unless followed by a membership/geography word (see the router).
    start_of_query: bool = False
    progress_label: Optional[str] = None  # "Retrieving data from <label>..."

    @property
    def progress_display(self) -> str:
        return self.progress_label or self.display_name


PROVIDER_SPECS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        canonical="OECD",
        name="OECD",
        display_name="OECD",
        description="Cross-country economic data for OECD member nations",
        normalize_aliases=("oecd",),
        bare_aliases=("oecd data",),
        directive_aliases=("oecd",),
        start_of_query=True,
    ),
    ProviderSpec(
        canonical="FRED",
        name="FRED",
        display_name="FRED",
        description="US economic data — 800K+ time series from the Federal Reserve Bank of St. Louis",
        normalize_aliases=(
            "fred", "fred (federal reserve)", "federal reserve",
            "st. louis fed", "stlouisfed",
        ),
        bare_aliases=("fred", "federal reserve", "st. louis fed", "stlouisfed"),
        directive_aliases=("fred", "federal reserve", "st. louis fed", "stlouisfed"),
        progress_label="Federal Reserve",
    ),
    ProviderSpec(
        canonical="WORLDBANK",
        name="WorldBank",
        display_name="World Bank",
        description="Global development indicators — 16,000+ series covering 200+ countries",
        normalize_aliases=("world bank", "worldbank", "wb"),
        bare_aliases=("world bank", "worldbank", "wb data", "world bank data"),
        directive_aliases=("world bank", "worldbank"),
    ),
    ProviderSpec(
        canonical="COMTRADE",
        name="Comtrade",
        display_name="UN Comtrade",
        description="International trade data — bilateral import/export flows by commodity",
        normalize_aliases=(
            "un comtrade", "un_comtrade", "uncomtrade", "comtrade", "un",
            "united nations comtrade",
        ),
        bare_aliases=("comtrade", "un comtrade", "united nations comtrade"),
        directive_aliases=("comtrade", "un comtrade", "united nations comtrade"),
    ),
    ProviderSpec(
        canonical="STATSCAN",
        name="StatsCan",
        display_name="Statistics Canada",
        description="Canadian economic and social data — 40,000+ tables",
        normalize_aliases=(
            "statistics canada", "statisticscanada", "stats canada", "statcan", "statscan",
        ),
        bare_aliases=("statscan", "statistics canada", "stats canada"),
        directive_aliases=("statscan", "statistics canada", "stats canada", "statcan"),
    ),
    ProviderSpec(
        canonical="IMF",
        name="IMF",
        display_name="IMF",
        description="International financial data — balance of payments, exchange rates, fiscal indicators",
        normalize_aliases=("imf", "international monetary fund", "the imf"),
        bare_aliases=("international monetary fund", "imf data"),
        directive_aliases=("imf", "the imf", "international monetary fund"),
        start_of_query=True,
    ),
    ProviderSpec(
        canonical="BIS",
        name="BIS",
        display_name="BIS",
        description=(
            "Central bank statistics — credit, debt securities, property prices "
            "from the Bank for International Settlements"
        ),
        normalize_aliases=("bis", "bank for international settlements"),
        bare_aliases=("bank for international settlements", "bis data"),
        directive_aliases=("bis", "bank for international settlements"),
        start_of_query=True,
        progress_label="Bank for International Settlements",
    ),
    ProviderSpec(
        canonical="EUROSTAT",
        name="Eurostat",
        display_name="Eurostat",
        description="European Union statistics — demographics, trade, economy across EU members",
        normalize_aliases=("eurostat",),
        bare_aliases=("eurostat data",),
        directive_aliases=("eurostat",),
        start_of_query=True,
    ),
    ProviderSpec(
        canonical="EXCHANGERATE",
        name="ExchangeRate",
        display_name="ExchangeRate-API",
        description="Foreign exchange rates — 160+ currencies with daily updates",
        normalize_aliases=(
            "exchangerate", "exchange rate", "exchangerate-api", "exchange-rate",
            "exchange rate api", "exchange rate-api",
        ),
        bare_aliases=("exchangerate", "exchange rate api"),
        directive_aliases=(
            "exchangerate", "exchange rate api", "exchange rate-api", "exchangerate-api",
        ),
    ),
    ProviderSpec(
        canonical="COINGECKO",
        name="CoinGecko",
        display_name="CoinGecko",
        description="Cryptocurrency market data — prices, volumes, market caps",
        normalize_aliases=("coingecko", "coin gecko"),
        bare_aliases=("coingecko", "coin gecko"),
        directive_aliases=("coingecko", "coin gecko"),
    ),
    ProviderSpec(
        canonical="CHINAMACRO",
        name="ChinaMacro",
        display_name="ChinaMacro",
        description=(
            "Fresh Chinese headline macro data — PMI, M2, total social financing, "
            "CPI/PPI, 10-year yield (live from EastMoney/MOFCOM)"
        ),
        normalize_aliases=("chinamacro", "china macro", "china_macro", "china-macro"),
        # Only the coined single token may match bare: "China macro outlook"
        # is ordinary English and must not hard-route.
        bare_aliases=("chinamacro",),
        directive_aliases=("chinamacro", "china macro", "china-macro"),
    ),
)

# Served-label sentinels that are not data providers (the catalog
# "not available" stub is labelled "Catalog").  A substitution note must
# never claim "Showing results from Catalog".
NON_PROVIDER_SOURCES: frozenset[str] = frozenset({"NOT_AVAILABLE", "CATALOG"})

PROVIDER_SPECS_BY_CANONICAL: Dict[str, ProviderSpec] = {
    spec.canonical: spec for spec in PROVIDER_SPECS
}


def _build_provider_aliases() -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for spec in PROVIDER_SPECS:
        for alias in (spec.canonical, spec.name, *spec.normalize_aliases):
            aliases.setdefault(alias.lower().strip(), spec.canonical)
    # Special sentinel for catalog concepts with no available provider
    aliases["not_available"] = "NOT_AVAILABLE"
    return aliases


# Provider name aliases → canonical form (uppercase).  Derived — edit
# PROVIDER_SPECS, not this dict.
PROVIDER_ALIASES: Dict[str, str] = _build_provider_aliases()


def normalize_provider_name(provider: str) -> str:
    """Normalize provider name to canonical form.

    Handles variations like 'UN COMTRADE', 'UN Comtrade', 'World Bank', etc.
    Returns uppercase canonical name like 'COMTRADE', 'WORLDBANK', etc.

    Returns empty string for empty/None input.
    """
    if not provider:
        return ""

    normalized = PROVIDER_ALIASES.get(provider.lower().strip())
    if normalized:
        return normalized

    # Providers may annotate their serving label with an attribution suffix —
    # e.g. "ChinaMacro (NBS via EastMoney datacenter)". The parenthetical is
    # display metadata, not identity: strip it so label-vs-namespace
    # comparisons (conversation-state persist, cache identity) see the same
    # provider the router used. Structural (text shape), not semantic.
    if "(" in provider:
        prefix = provider.split("(", 1)[0].strip()
        if prefix:
            normalized = PROVIDER_ALIASES.get(prefix.lower())
            return normalized or prefix.upper()

    return provider.upper().strip()


def get_provider_spec(provider: str) -> Optional[ProviderSpec]:
    """Return the registered spec for any provider label/alias, else None."""
    return PROVIDER_SPECS_BY_CANONICAL.get(normalize_provider_name(provider))


def _compact_provider_key(value: str) -> str:
    compact = str(value or "").strip().replace("_", " ").replace("-", " ").upper()
    return " ".join(compact.split())


def db_provider_lookup() -> Dict[str, str]:
    """Compact-uppercase provider label → indicators.db provider name."""
    lookup: Dict[str, str] = {}
    for spec in PROVIDER_SPECS:
        for alias in (spec.canonical, spec.name, spec.display_name, *spec.normalize_aliases):
            key = _compact_provider_key(alias)
            if key:
                lookup.setdefault(key, spec.name)
    return lookup


def provider_option_labels() -> Dict[str, str]:
    """Canonical provider → bracket label used in clarification options."""
    return {spec.canonical: spec.name for spec in PROVIDER_SPECS}


def provider_progress_labels() -> Dict[str, str]:
    """Canonical provider → name shown in "Retrieving data from ..." steps."""
    return {spec.canonical: spec.progress_display for spec in PROVIDER_SPECS}


def provider_title_aliases(provider: str) -> Optional[list[str]]:
    """Provider-name forms that may wrap a pasted title ("... from <alias>")."""
    spec = get_provider_spec(provider)
    if spec is None:
        return None
    ordered: list[str] = []
    for alias in (*spec.directive_aliases, *spec.normalize_aliases):
        if alias not in ordered:
            ordered.append(alias)
    return ordered
