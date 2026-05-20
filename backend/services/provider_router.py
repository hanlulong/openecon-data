"""Compatibility shim for legacy ProviderRouter imports.

Provider routing final authority now lives in :mod:`backend.routing.unified_router`.
This module preserves the old import path for tests, scripts, and docs without
reintroducing provider-selection shortcut rules.  Semantic/geography helpers are
kept as non-authoritative predicates only; ``route_provider`` delegates to the
UnifiedRouter final-authority contract.
"""

from __future__ import annotations

from typing import Any, Optional

from ..routing.country_resolver import CountryResolver
from ..routing.unified_router import (
    UnifiedRouter,
    correct_coingecko_misrouting as _correct_coingecko_misrouting,
    detect_explicit_provider as _detect_explicit_provider,
    route_provider as _route_provider,
    validate_routing as _validate_routing,
)


class ProviderRouter:
    """Legacy static-method facade over the unified no-shortcut router."""

    @staticmethod
    def route_provider(intent: Any, original_query: str) -> str:
        """Return the unified router's final provider decision."""
        return _route_provider(intent, original_query)

    @staticmethod
    def detect_explicit_provider(query: str) -> Optional[str]:
        """Detect explicit provider directives such as ``from IMF``."""
        return _detect_explicit_provider(query)

    @staticmethod
    def correct_coingecko_misrouting(provider: str, query: str, indicators: list) -> str:
        """Apply CoinGecko provider-contract rejection without semantic rerouting."""
        return _correct_coingecko_misrouting(provider, query, indicators)

    @staticmethod
    def validate_routing(provider: str, original_query: str, intent: Any) -> Optional[str]:
        """Return a routing warning when the unified compatibility check finds one."""
        return _validate_routing(provider, original_query, intent)

    @staticmethod
    def is_us_only_indicator(indicators: list[str]) -> bool:
        """Retired semantic shortcut hook; never grants provider authority."""
        return False

    @staticmethod
    def is_canadian_query(query: str, parameters: dict[str, Any] | None = None) -> bool:
        """Return true only for explicit, normalized Canadian country parameters."""
        _ = query
        params = parameters or {}
        country = str(params.get("country") or "").strip()
        countries = params.get("countries") if isinstance(params.get("countries"), list) else []
        if country and CountryResolver.normalize(country) == "CA":
            return True
        if any(CountryResolver.normalize(str(item)) == "CA" for item in countries):
            return True
        return False


__all__ = ["ProviderRouter", "UnifiedRouter"]
