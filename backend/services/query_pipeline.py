"""
Query pipeline stages for parsing, routing, and validation.

This module centralizes the first half of query execution so both primary and
fallback flows share the same intent extraction and routing behavior.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..models import ParsedIntent
from .parameter_validator import ParameterValidator
from ..routing.unified_router import validate_routing as unified_validate_routing
from ..utils.providers import normalize_provider_name

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

    @staticmethod
    def _metric_text_from_query(query: str) -> str:
        """Return a plain-language metric phrase when the parser invented a code.

        This is mechanical cleanup, not semantic remapping: remove obvious time
        scaffolding and keep the user's own wording so retrieval/LLM selection
        can adjudicate the correct provider-native code later.
        """
        text = str(query or "").strip()
        if not text:
            return ""
        text = re.sub(r"\b(?:in|for|during)\s+(?:19|20)\d{2}\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" ,;:")
        return text

    def _drop_llm_invented_indicator_codes(self, intent: ParsedIntent, query: str) -> None:
        """Prevent parser-invented provider codes from locking resolution.

        The LLM prompt forbids provider-native codes unless the user supplies
        them. If a code appears anyway and is not present in the raw query, drop
        it back to plain text. The downstream retrieval + LLM selector must pick
        the executable indicator.
        """
        provider = normalize_provider_name(intent.apiProvider or "")
        if not provider:
            return

        query_upper = str(query or "").upper()
        params = dict(intent.parameters or {})
        metric_text = self._metric_text_from_query(
            str(params.get("__semantic_indicator_label") or "").strip()
        )
        if not metric_text:
            distilled_builder = getattr(
                self.query_service,
                "_build_distilled_indicator_query",
                None,
            )
            if callable(distilled_builder):
                metric_text = distilled_builder(query)
        if not metric_text:
            metric_text = self._metric_text_from_query(query)
        if not metric_text:
            return

        provider_code_checker = getattr(
            self.query_service,
            "_looks_like_provider_indicator_code",
            None,
        )

        def _looks_like_invented_code(value: str) -> bool:
            value_text = str(value or "").strip()
            service_thinks_provider_code = (
                bool(provider_code_checker(provider, value_text))
                if callable(provider_code_checker)
                else False
            )
            return bool(
                value_text
                and (
                    service_thinks_provider_code
                    or (
                        re.fullmatch(r"[A-Z0-9_@.\-]{3,}", value_text)
                        and any(sep in value_text for sep in ("_", ".", "@"))
                    )
                )
            )

        changed = False
        clean_indicators: list[str] = []
        for indicator in intent.indicators or []:
            indicator_text = str(indicator or "").strip()
            if (
                indicator_text
                and _looks_like_invented_code(indicator_text)
                and indicator_text.upper() not in query_upper
            ):
                clean_indicators.append(metric_text)
                changed = True
            else:
                clean_indicators.append(indicator_text)

        param_indicator = str(params.get("indicator") or "").strip()
        if (
            param_indicator
            and _looks_like_invented_code(param_indicator)
            and param_indicator.upper() not in query_upper
        ):
            params.pop("indicator", None)
            changed = True

        if changed:
            logger.info(
                "Dropped parser-invented %s indicator code; using plain metric text '%s'",
                provider,
                metric_text,
            )
            intent.indicators = list(dict.fromkeys([item for item in clean_indicators if item])) or [metric_text]
            params["__semantic_indicator_label"] = metric_text
            intent.parameters = params

    async def parse_and_route(
        self,
        query: str,
        history: Optional[List[str]] = None,
        conversation_context: Optional[Dict[str, Any]] = None,
    ) -> ParseRouteResult:
        """
        Parse user query and apply deterministic routing guardrails.

        Args:
            query: The user query (or context-enriched query).
            history: Previous conversation messages.
            conversation_context: Optional dict with previous turn info for LLM follow-up detection.

        Returns:
            ParseRouteResult with routed intent and optional routing warning.
        """
        parsed_intent = await self.query_service.openrouter.parse_query(
            query, history or [], conversation_context=conversation_context
        )
        parsed_intent.originalQuery = query
        self._drop_llm_invented_indicator_codes(parsed_intent, query)

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
            params = dict(parsed_intent.parameters or {})
            params["__semantic_provider_locked"] = True
            parsed_intent.parameters = params
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

        validation_warning = unified_validate_routing(routed_provider, query, parsed_intent)
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
        params = intent.parameters or {}
        provider = normalize_provider_name(intent.apiProvider or "")
        coin_ids = params.get("coinIds") or []
        if not isinstance(coin_ids, list):
            coin_ids = []

        # Some providers encode a single semantic request as multiple asset
        # members in provider params.  Treat those as one comparison request,
        # not as a multi-indicator query.
        is_asset_comparison = provider == "COINGECKO" and len(coin_ids) > 1
        is_fx_pair_request = provider == "EXCHANGERATE" and bool(params.get("targetCurrency"))

        is_multi_indicator = len(intent.indicators) > 1 and not is_asset_comparison and not is_fx_pair_request
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
