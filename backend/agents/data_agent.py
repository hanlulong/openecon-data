"""
Data Agent for Context-Aware Data Fetching

This agent handles standard data fetch requests with:
- Context awareness from previous queries
- Entity reference resolution
- Automatic data reference creation for follow-ups
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..memory.conversation_state import ConversationState, DataReference
from ..models import NormalizedData, ParsedIntent

logger = logging.getLogger(__name__)


@dataclass
class DataResponse:
    """Response from data agent"""
    success: bool
    data: List[NormalizedData] = field(default_factory=list)
    data_reference: Optional[DataReference] = None
    intent: Optional[ParsedIntent] = None
    message: Optional[str] = None
    error: Optional[str] = None


class DataAgent:
    """
    Enhanced data fetching with context awareness.

    Key responsibilities:
    1. Use resolved entity references from router
    2. Apply follow-up modifications to base datasets
    3. Create data references for future follow-ups
    4. Ensure proper metadata extraction (especially units)
    """

    def __init__(
        self,
        query_service=None,
        openrouter_service=None
    ):
        """
        Initialize with required services.

        Args:
            query_service: Service for fetching data from providers
            openrouter_service: Service for LLM query parsing
        """
        self.query_service = query_service
        self.openrouter_service = openrouter_service

    async def process(
        self,
        query: str,
        context: Dict[str, Any],
        state: Optional[ConversationState] = None,
        pre_resolved_intent=None,
    ) -> DataResponse:
        """
        Process a data fetch request.

        Args:
            query: Original user query
            context: Context from router (may include resolved references)
            state: Current conversation state
            pre_resolved_intent: If provided, skip LLM re-parsing and use this intent directly.
                This preserves concept overrides, indicator resolution, and semantic
                validation from the main process_query pipeline.

        Returns:
            DataResponse with fetched data and reference
        """
        logger.info(f"DataAgent processing: {query[:50]}...")

        # Check if this is a follow-up with resolved context
        if context.get("follow_up_mode") and context.get("base_dataset"):
            return await self._process_follow_up(query, context, state)

        # Standard data fetch (pass pre-resolved intent to skip re-parsing)
        return await self._process_standard(query, context, state, pre_resolved_intent=pre_resolved_intent)

    async def _process_standard(
        self,
        query: str,
        context: Dict[str, Any],
        state: Optional[ConversationState],
        pre_resolved_intent=None,
    ) -> DataResponse:
        """Process a standard (non-follow-up) data fetch"""
        if not self.query_service:
            return DataResponse(
                success=False,
                error="Query service not available",
            )

        try:
            # Get conversation history if available
            history = state.get_raw_history() if state else []

            # Use pre-resolved intent if available (avoids re-parsing which
            # would lose concept overrides and indicator resolution).
            if pre_resolved_intent:
                intent = pre_resolved_intent
                logger.info(f"Using pre-resolved intent: {intent.apiProvider}/{intent.parameters.get('indicator', intent.indicators)}")
            elif self.openrouter_service:
                intent = await self.openrouter_service.parse_query(query, history)
            else:
                # Fallback - use query service's parsing
                intent = None

            # Ensure originalQuery is set so qualifier preservation
            # can detect "growth"/"per capita" even when the LLM drops them.
            if intent and not intent.originalQuery:
                intent.originalQuery = query

            # Fetch data
            result = await self._fetch_data(query, intent, context)

            if not result or not result.data:
                return DataResponse(
                    success=False,
                    error="No data returned from provider",
                )

            # CRITICAL FIX: Filter None values from data list
            # This handles cases where parallel fetches return [None, NormalizedData, None]
            valid_data = [d for d in result.data if d is not None]
            if not valid_data:
                return DataResponse(
                    success=False,
                    error="No valid data returned from provider (all results were None)",
                )

            # Create data reference for follow-ups
            data_ref = self._create_data_reference(query, valid_data, intent)

            # Safe source extraction with fallback
            source = valid_data[0].metadata.source if valid_data[0].metadata else "UNKNOWN"

            return DataResponse(
                success=True,
                data=valid_data,
                data_reference=data_ref,
                intent=result.intent,
                message=f"Retrieved {len(valid_data)} series from {source}",
            )

        except Exception as e:
            logger.error(f"Data fetch failed: {e}")
            # Include intent in error response for fallback mechanism
            return DataResponse(
                success=False,
                intent=intent if 'intent' in locals() else None,
                error=str(e),
            )

    async def _process_follow_up(
        self,
        query: str,
        context: Dict[str, Any],
        state: Optional[ConversationState]
    ) -> DataResponse:
        """Process a follow-up request with resolved context"""
        base_dataset: DataReference = context.get("base_dataset")

        if not base_dataset:
            return DataResponse(
                success=False,
                error="No base dataset available for follow-up",
            )

        logger.info(f"Follow-up on: {base_dataset.indicator}")

        # Apply modifications from follow-up request
        modified_params = self._apply_follow_up_modifications(base_dataset, context)

        # Check if we just need to modify visualization
        if self._is_visualization_only(context):
            # Reuse existing data with different visualization
            return DataResponse(
                success=True,
                data=[],  # Frontend will use existing data
                data_reference=base_dataset,
                message=f"Updating visualization for {base_dataset.indicator}",
            )

        # Fetch new data with modified parameters
        try:
            result = await self._fetch_with_params(modified_params)

            if not result or not result.data:
                return DataResponse(
                    success=False,
                    error="No data returned for follow-up query",
                )

            # CRITICAL FIX: Filter None values from data list
            valid_data = [d for d in result.data if d is not None]
            if not valid_data:
                return DataResponse(
                    success=False,
                    error="No valid data returned for follow-up query",
                )

            # Create new data reference
            data_ref = self._create_data_reference(query, valid_data, None)
            data_ref.variants = [context.get("dataset_variant")] if context.get("dataset_variant") else []

            return DataResponse(
                success=True,
                data=valid_data,
                data_reference=data_ref,
                message=f"Retrieved follow-up data: {data_ref.indicator}",
            )

        except Exception as e:
            logger.error(f"Follow-up fetch failed: {e}")
            return DataResponse(
                success=False,
                error=str(e),
            )

    def _apply_follow_up_modifications(
        self,
        base_dataset: DataReference,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply modifications from follow-up context to base dataset"""
        params = {
            "provider": base_dataset.provider,
            "indicator": base_dataset.indicator,
            "startDate": base_dataset.time_range[0] if base_dataset.time_range else None,
            "endDate": base_dataset.time_range[1] if base_dataset.time_range else None,
        }

        # Preserve multi-country context when present.
        if base_dataset.countries:
            params["countries"] = list(base_dataset.countries)
        elif base_dataset.country:
            params["country"] = base_dataset.country

        # Apply frequency change
        if context.get("frequency"):
            params["frequency"] = context["frequency"]

        # Apply variant change (e.g., consolidated -> unconsolidated)
        if context.get("dataset_variant"):
            params["variant"] = context["dataset_variant"]
            # Add variant-specific dimension filters
            if base_dataset.provider == "EUROSTAT":
                if context["dataset_variant"] == "unconsolidated":
                    params["dimensions"] = {"conso": "N"}
                elif context["dataset_variant"] == "consolidated":
                    params["dimensions"] = {"conso": "S"}

        return params

    def _is_visualization_only(self, context: Dict[str, Any]) -> bool:
        """Check if follow-up only changes visualization, not data"""
        vis_only_keys = ["chart_type", "merge_with_previous"]
        data_modifying_keys = ["frequency", "dataset_variant", "time_range"]

        has_vis_changes = any(context.get(k) for k in vis_only_keys)
        has_data_changes = any(context.get(k) for k in data_modifying_keys)

        return has_vis_changes and not has_data_changes

    async def _fetch_data(
        self,
        query: str,
        intent: Optional[ParsedIntent],
        context: Dict[str, Any]
    ) -> Any:
        """Fetch data using query service's _fetch_data method directly.

        This avoids the infinite loop that would occur if we called process_query,
        which would trigger LangGraph orchestration again.
        """
        if not self.query_service:
            raise ValueError("Query service not available")

        # Use explicit provider if specified
        explicit_provider = context.get("explicit_provider")
        if explicit_provider and intent:
            intent.apiProvider = explicit_provider

        # If no intent, parse the query first
        if not intent:
            history = []  # No history available here
            intent = await self.openrouter_service.parse_query(query, history)

        # Keep LangGraph data node aligned with the main query pipeline:
        # apply country overrides and hybrid provider routing (if enabled).
        self.query_service._apply_country_overrides(intent, query)
        if explicit_provider:
            routed_provider = explicit_provider
        else:
            routed_provider = await self.query_service._select_routed_provider(intent, query)

        if routed_provider != intent.apiProvider:
            logger.info(f"🔄 DataAgent: Provider routing override: {intent.apiProvider} → {routed_provider}")
            intent.apiProvider = routed_provider

        # Store original query for downstream processing (helps with currency extraction, etc.)
        if not intent.originalQuery:
            intent.originalQuery = query

        # Use query service's _fetch_data directly (avoids LangGraph loop)
        data = await self.query_service._fetch_data(intent)

        # Wrap in a result-like object
        class FetchResult:
            def __init__(self, data, intent):
                self.data = data
                self.intent = intent

        return FetchResult(data=data, intent=intent)

    async def _fetch_with_params(self, params: Dict[str, Any]) -> Any:
        """Fetch data with specific parameters.

        Uses query service's _fetch_data directly to avoid LangGraph loop.
        """
        if not self.query_service:
            raise ValueError("Query service not available")

        # Build intent from params
        intent = ParsedIntent(
            apiProvider=params.get("provider", "FRED"),
            indicators=[params.get("indicator", "")],
            parameters=params,
            clarificationNeeded=False,
        )

        # Use query service's _fetch_data directly
        data = await self.query_service._fetch_data(intent)

        # Wrap in a result-like object
        class FetchResult:
            def __init__(self, data, intent):
                self.data = data
                self.intent = intent

        return FetchResult(data=data, intent=intent)

    def _create_data_reference(
        self,
        query: str,
        data: List[NormalizedData],
        intent: Optional[ParsedIntent]
    ) -> DataReference:
        """Create a data reference from fetched data"""
        if not data:
            return DataReference(
                id=str(uuid.uuid4()),
                query=query,
                provider="UNKNOWN",
            )

        first = data[0]
        metadata = first.metadata
        countries = []
        for series in data:
            if (
                series
                and getattr(series, "metadata", None)
                and series.metadata.country
                and series.metadata.country not in countries
            ):
                countries.append(series.metadata.country)

        return DataReference(
            id=str(uuid.uuid4()),
            query=query,
            provider=metadata.source or "UNKNOWN",
            dataset_code=metadata.seriesId,
            indicator=metadata.indicator or "",
            country=metadata.country or (countries[0] if countries else None),
            countries=countries,
            time_range=(metadata.startDate, metadata.endDate) if metadata.startDate else None,
            unit=metadata.unit or "",
            frequency=metadata.frequency or "",
            metadata=metadata.model_dump() if hasattr(metadata, "model_dump") else {},
            data=[{"date": d.date, "value": d.value} for d in first.data] if first.data else None,
            chart_type=intent.recommendedChartType if intent else "line",
        )


# Singleton instance
data_agent = DataAgent()
