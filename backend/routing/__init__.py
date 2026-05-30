"""
Unified Routing Module

This module provides a single source of truth for provider routing decisions.

Components:
- CountryResolver: Country normalization and region membership
- UnifiedRouter: Single routing entry point
"""

from .country_resolver import CountryResolver
from .unified_router import (
    UnifiedRouter,
    RoutingDecision,
    route_provider,
    detect_explicit_provider,
    detect_explicit_provider_match,
    correct_coingecko_misrouting,
    validate_routing,
)

__all__ = [
    "CountryResolver",
    "UnifiedRouter",
    "RoutingDecision",
    "route_provider",
    "detect_explicit_provider",
    "detect_explicit_provider_match",
    "correct_coingecko_misrouting",
    "validate_routing",
]
