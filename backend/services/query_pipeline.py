"""
Query pipeline stages for parsing, routing, and validation.

This module centralizes the first half of query execution so both primary and
fallback flows share the same intent extraction and routing behavior.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..models import ParsedIntent
from .parameter_validator import ParameterValidator
from .provider_router import ProviderRouter

if TYPE_CHECKING:
    from .query import QueryService

logger = logging.getLogger(__name__)


@dataclass
class ParseRouteResult:
    """Result of parse + route stage."""
    intent: ParsedIntent
    explicit_provider: Optional[str]
    routed_provider: str
    validation_warning: Optional[str]


@dataclass
class ValidationResult:
    """Result of validation + confidence stage."""
    is_multi_indicator: bool
    is_valid: bool
    validation_error: Optional[str]
    suggestions: Optional[Dict[str, Any]]
    is_confident: bool
    confidence_reason: Optional[str]


class QueryPipeline:
    """Reusable query execution stages shared by standard and orchestrated paths."""

    def __init__(self, query_service: "QueryService") -> None:
        self.query_service = query_service

    async def parse_and_route(
        self,
        query: str,
        history: Optional[List[str]] = None,
    ) -> ParseRouteResult:
        """
        Parse user query and apply deterministic routing guardrails.

        Returns:
            ParseRouteResult with routed intent and optional routing warning.
        """
        parsed_intent = await self.query_service.openrouter.parse_query(query, history or [])
        parsed_intent.originalQuery = query

        # Geography override is deterministic and should always be applied post-parse.
        self.query_service._apply_country_overrides(parsed_intent, query)

        explicit_provider_raw = self.query_service._detect_explicit_provider(query)
        explicit_provider = (
            self.query_service._normalize_provider_alias(explicit_provider_raw)
            if explicit_provider_raw
            else None
        )
        if explicit_provider:
            if parsed_intent.apiProvider != explicit_provider:
                logger.info(
                    "🎯 Enforcing explicit provider request: %s -> %s",
                    parsed_intent.apiProvider,
                    explicit_provider,
                )
            parsed_intent.apiProvider = explicit_provider
            routed_provider = explicit_provider
        else:
            routed_provider = await self.query_service._select_routed_provider(parsed_intent, query)
            if routed_provider != parsed_intent.apiProvider:
                logger.info(
                    "🔄 Provider routing: %s -> %s (deterministic+semantic)",
                    parsed_intent.apiProvider,
                    routed_provider,
                )
                parsed_intent.apiProvider = routed_provider

        validation_warning = ProviderRouter.validate_routing(routed_provider, query, parsed_intent)
        if validation_warning:
            logger.warning("Routing validation: %s", validation_warning)

        return ParseRouteResult(
            intent=parsed_intent,
            explicit_provider=explicit_provider,
            routed_provider=routed_provider,
            validation_warning=validation_warning,
        )

    def validate_intent(self, intent: ParsedIntent) -> ValidationResult:
        """
        Validate parsed intent and confidence in a shared stage.

        Multi-indicator queries skip strict validation/confidence checks because
        they are validated during individual fetch attempts.
        """
        is_multi_indicator = len(intent.indicators) > 1
        if is_multi_indicator:
            return ValidationResult(
                is_multi_indicator=True,
                is_valid=True,
                validation_error=None,
                suggestions=None,
                is_confident=True,
                confidence_reason=None,
            )

        is_valid, validation_error, suggestions = ParameterValidator.validate_intent(intent)
        if not is_valid:
            return ValidationResult(
                is_multi_indicator=False,
                is_valid=False,
                validation_error=validation_error,
                suggestions=suggestions,
                is_confident=False,
                confidence_reason=validation_error,
            )

        is_confident, confidence_reason = ParameterValidator.check_confidence(intent)
        return ValidationResult(
            is_multi_indicator=False,
            is_valid=True,
            validation_error=None,
            suggestions=suggestions,
            is_confident=is_confident,
            confidence_reason=confidence_reason,
        )
