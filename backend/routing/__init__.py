"""
Unified Routing Module

This module provides a single source of truth for provider routing decisions.
It consolidates the logic from:
- provider_router.py (deterministic routing)
- deep_agent_orchestrator.py (scoring-based routing)
- catalog_service.py (YAML-based indicator mappings)

Components:
- CountryResolver: Country normalization and region membership
- KeywordMatcher: Pattern detection for explicit provider mentions
- UnifiedRouter: Main routing entry point
"""

from .country_resolver import CountryResolver
from .keyword_matcher import KeywordMatcher
from .unified_router import UnifiedRouter, RoutingDecision

__all__ = [
    "CountryResolver",
    "KeywordMatcher",
    "UnifiedRouter",
    "RoutingDecision",
]
