"""Canonical provider name normalization and shared provider constants.

Single source of truth for mapping LLM outputs, user input, and internal
references to the standard uppercase provider names used throughout the system.
"""

# Canonical tuple of all data providers that support indicator codes.
# Used for cross-provider indicator code detection and reverse concept lookups.
ALL_PROVIDERS: tuple[str, ...] = (
    "FRED", "WORLDBANK", "IMF", "EUROSTAT", "BIS", "OECD", "STATSCAN",
)

# Provider name aliases → canonical form (uppercase)
PROVIDER_ALIASES = {
    # Comtrade variations
    "un comtrade": "COMTRADE",
    "un_comtrade": "COMTRADE",
    "uncomtrade": "COMTRADE",
    "comtrade": "COMTRADE",
    "un": "COMTRADE",
    # World Bank variations
    "world bank": "WORLDBANK",
    "worldbank": "WORLDBANK",
    "wb": "WORLDBANK",
    # Statistics Canada variations
    "statistics canada": "STATSCAN",
    "statisticscanada": "STATSCAN",
    "stats canada": "STATSCAN",
    "statcan": "STATSCAN",
    "statscan": "STATSCAN",
    # Exchange rate variations
    "exchangerate": "EXCHANGERATE",
    "exchange rate": "EXCHANGERATE",
    "exchangerate-api": "EXCHANGERATE",
    "exchange-rate": "EXCHANGERATE",
    "exchange rate api": "EXCHANGERATE",
    # FRED variations
    "fred": "FRED",
    "fred (federal reserve)": "FRED",
    "federal reserve": "FRED",
    # Other providers
    "imf": "IMF",
    "international monetary fund": "IMF",
    "bis": "BIS",
    "bank for international settlements": "BIS",
    "eurostat": "EUROSTAT",
    "oecd": "OECD",
    "coingecko": "COINGECKO",
    "coin gecko": "COINGECKO",
    # Special sentinel for catalog concepts with no available provider
    "not_available": "NOT_AVAILABLE",
}


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

    return provider.upper().strip()
