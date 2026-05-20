"""Provider-native CoinGecko supportability classification for validation sampling.

This module identifies catalog rows that cannot produce an ordinary
user-answerability prompt under the current CoinGecko runtime contract.  It is
metadata-only sampler logic: no natural-language provider routing, no popular
coin fallback, and no pass/fail adjudication.
"""

from __future__ import annotations

import re
from typing import Final

COINGECKO_NON_EXECUTABLE_ASSET_SLUG_REASON: Final[str] = (
    "coingecko_non_executable_asset_slug"
)
COINGECKO_CURRENT_PRICE_UNAVAILABLE_REASON: Final[str] = (
    "coingecko_current_price_unavailable"
)

_RUNTIME_EXACT_SLUG_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9][a-z0-9\-]{1,127}$",
    flags=re.IGNORECASE,
)


def _has_informative_asset_name(name: str | None) -> bool:
    for token in re.findall(r"[A-Za-z0-9]+", str(name or "")):
        if len(token) >= 2:
            return True
    return False


def coingecko_catalog_sampler_supportability_reason(
    code: str | None,
    name: str | None = None,
    category: str | None = None,
    raw_metadata: object = None,
) -> str | None:
    """Return supportability reason for non-executable CoinGecko asset ids.

    CoinGecko current-price exact provider-code execution accepts slug-shaped
    ids such as ``bitcoin`` or ``01-token``.  A small long-tail slice of the
    catalog contains provider ids such as ``_`` or leading-punctuation ids and
    display names with no informative alphanumeric label.  Those rows cannot be
    turned into ordinary user prompts without inventing a semantic alias, so
    validation inventories them instead of sampling them as evidence.
    """
    del raw_metadata  # Supportability is code/name/category-only.
    normalized_code = str(code or "").strip()
    category_lower = str(category or "").strip().lower()
    if category_lower and "cryptocurrency" not in category_lower:
        return None
    if _RUNTIME_EXACT_SLUG_RE.fullmatch(normalized_code):
        return None
    if _has_informative_asset_name(name):
        return None
    return COINGECKO_NON_EXECUTABLE_ASSET_SLUG_REASON


def coingecko_catalog_price_supportability_reason(
    *,
    code: str | None,
    simple_price_payload: object,
    vs_currency: str = "usd",
) -> str | None:
    """Classify provider-contract current-price availability for one asset id.

    This consumes a CoinGecko ``/simple/price`` response that was requested by
    exact provider-native slug.  Returning a reason means the provider recognized
    the id but did not publish the requested metric in that response.
    """
    normalized_code = str(code or "").strip().lower()
    currency = str(vs_currency or "usd").strip().lower()
    if not normalized_code or not isinstance(simple_price_payload, dict):
        return None
    coin_payload = simple_price_payload.get(normalized_code)
    if not isinstance(coin_payload, dict):
        return None
    if currency in coin_payload and coin_payload.get(currency) is not None:
        return None
    return COINGECKO_CURRENT_PRICE_UNAVAILABLE_REASON
