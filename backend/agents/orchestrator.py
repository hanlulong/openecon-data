"""
Agent Orchestrator for Multi-Agent Query Processing

This module coordinates the multi-agent architecture:
1. RouterAgent classifies the query
2. Routes to appropriate specialist agent
3. Manages conversation state
4. Returns unified response
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..memory.conversation_state import ConversationState, DataReference, QueryType
from ..memory.state_manager import ConversationStateManager
from ..models import QueryResponse, NormalizedData, ParsedIntent

from .router_agent import RouterAgent, RoutingResult
from .research_agent import ResearchAgent
from .data_agent import DataAgent
from .comparison_agent import ComparisonAgent

logger = logging.getLogger(__name__)

# Deep Agent integration (lazy import to avoid circular dependencies)
_deep_agent_orchestrator = None


class AgentOrchestrator:
    """
    Coordinates multi-agent query processing.

    This replaces direct query processing with agent-based routing.
    """

    def __init__(
        self,
        state_manager: Optional[ConversationStateManager] = None,
        router_agent: Optional[RouterAgent] = None,
        research_agent: Optional[ResearchAgent] = None,
        data_agent: Optional[DataAgent] = None,
        comparison_agent: Optional[ComparisonAgent] = None,
        query_service=None,
        openrouter_service=None,
        metadata_search=None,
        enable_deep_agents: bool = True,
    ):
        """
        Initialize orchestrator with agents and services.

        Args:
            state_manager: Manages conversation states
            router_agent: Classifies and routes queries
            research_agent: Handles availability questions
            data_agent: Handles data fetching
            comparison_agent: Handles multi-series comparisons
            query_service: Legacy query service for data fetching
            openrouter_service: LLM service for parsing
            metadata_search: Service for metadata search
            enable_deep_agents: Enable Deep Agents for complex multi-step queries
        """
        # State management
        self.state_manager = state_manager or ConversationStateManager()

        # Agents
        self.router = router_agent or RouterAgent()
        self.research = research_agent or ResearchAgent(metadata_search=metadata_search)
        self.data = data_agent or DataAgent(
            query_service=query_service,
            openrouter_service=openrouter_service
        )
        self.comparison = comparison_agent or ComparisonAgent()

        # Legacy services
        self.query_service = query_service
        self.openrouter_service = openrouter_service

        # Deep Agents integration (LangChain 1.2 + LangGraph 1.0.5)
        self.enable_deep_agents = enable_deep_agents
        self._deep_agent = None
        if enable_deep_agents and query_service:
            self._initialize_deep_agent()

    def _initialize_deep_agent(self) -> None:
        """Initialize Deep Agent orchestrator for complex queries."""
        try:
            from ..services.deep_agent_orchestrator import (
                DeepAgentOrchestrator,
                DeepAgentConfig,
            )

            config = DeepAgentConfig(
                enable_planning=True,
                enable_subagents=True,
                max_concurrent_subagents=5,
                planning_threshold=3,
            )

            self._deep_agent = DeepAgentOrchestrator(
                query_service=self.query_service,
                config=config,
            )
            logger.info("Deep Agent orchestrator initialized successfully")

        except Exception as e:
            logger.warning(f"Failed to initialize Deep Agent: {e}. Continuing without Deep Agents.")
            self._deep_agent = None

    def _should_use_deep_agent(self, query: str, routing_result: RoutingResult) -> bool:
        """
        Determine if a query should use Deep Agents.

        Deep Agents are used for:
        1. Region queries (G7, BRICS, EU, ASEAN, Nordic, etc.)
        2. Comparison queries with multiple entities
        3. Complex multi-step queries
        4. Queries explicitly requesting multiple providers
        """
        if not self._deep_agent:
            return False

        query_lower = query.lower()

        # PRIORITY 1: Check for region groups (G7, BRICS, EU, ASEAN, Nordic, etc.)
        # These ALWAYS use Deep Agent for parallel country fetching
        from ..routing.country_resolver import CountryResolver
        regions = CountryResolver.detect_regions_in_query(query)
        if regions:
            expanded_count = len(CountryResolver.expand_regions_in_query(query))
            logger.info(f"Using Deep Agent for region query: {regions} ({expanded_count} countries)")
            return True

        # Check for comparison with multiple items
        comparison_keywords = ["compare", "vs", "versus", "and", "both", "all"]
        has_comparison = any(kw in query_lower for kw in comparison_keywords)

        # Check for multiple countries
        country_count = sum(1 for c in [
            "us", "usa", "uk", "germany", "france", "japan", "china",
            "canada", "india", "brazil"
        ] if c in query_lower)

        # Check for multiple indicators
        indicator_count = sum(1 for i in [
            "gdp", "unemployment", "inflation", "trade", "exports", "imports"
        ] if i in query_lower)

        # Use Deep Agent if query has multiple entities
        if has_comparison and (country_count > 1 or indicator_count > 1):
            logger.info(f"Using Deep Agent for complex query: {country_count} countries, {indicator_count} indicators")
            return True

        return False

    async def process_query(
        self,
        query: str,
        conversation_id: Optional[str] = None,
    ) -> QueryResponse:
        """
        Process a query using multi-agent architecture.

        Args:
            query: User's natural language query
            conversation_id: Optional conversation ID for follow-ups

        Returns:
            QueryResponse with results or clarification
        """
        # Get or create conversation state
        conv_id = self.state_manager.get_or_create(conversation_id)
        state = self.state_manager.get(conv_id)

        logger.info(f"Orchestrator processing: '{query[:50]}...' (conv: {conv_id[:8]})")

        # Route query
        routing_result = self.router.classify(query, state)

        logger.info(f"Routed to: {routing_result.query_type.value}")

        # Add user message to state
        self.state_manager.add_user_message(conv_id, query, routing_result.query_type)

        # Check if we should use Deep Agents for this query
        if self._should_use_deep_agent(query, routing_result):
            logger.info("Delegating to Deep Agent for parallel processing")
            return await self._handle_deep_agent_query(query, routing_result, state, conv_id)

        # Process based on query type
        try:
            if routing_result.query_type == QueryType.RESEARCH:
                response = await self._handle_research(query, routing_result, state, conv_id)

            elif routing_result.query_type == QueryType.COMPARISON:
                response = await self._handle_comparison(query, routing_result, state, conv_id)

            elif routing_result.query_type == QueryType.FOLLOW_UP:
                response = await self._handle_follow_up(query, routing_result, state, conv_id)

            elif routing_result.query_type == QueryType.ANALYSIS:
                # For analysis, delegate to Pro Mode via legacy service
                response = await self._handle_analysis(query, routing_result, state, conv_id)

            else:  # DATA_FETCH or UNKNOWN
                response = await self._handle_data_fetch(query, routing_result, state, conv_id)

            # Update state with result
            if response.data:
                self._update_state_with_data(conv_id, response)

            return response

        except Exception as e:
            logger.error(f"Orchestrator error: {e}")
            return QueryResponse(
                conversationId=conv_id,
                error=str(e),
                clarificationNeeded=False,
            )

    async def _handle_deep_agent_query(
        self,
        query: str,
        routing: RoutingResult,
        state: Optional[ConversationState],
        conv_id: str
    ) -> QueryResponse:
        """
        Handle complex queries using Deep Agents (LangChain 1.2 + LangGraph 1.0.5).

        Deep Agents provide:
        - Planning capability for multi-step queries
        - Parallel data fetching via subagents
        - Context management for complex workflows
        """
        try:
            if not self._deep_agent:
                # Fallback to standard processing
                logger.warning("Deep Agent not available, falling back to standard processing")
                return await self._handle_data_fetch(query, routing, state, conv_id)

            # Execute via Deep Agent
            result = await self._deep_agent.execute(
                query=query,
                conversation_id=conv_id,
            )

            if not result.get("success"):
                error_msg = result.get("error", "Unknown Deep Agent error")
                logger.error(f"Deep Agent execution failed: {error_msg}")
                return QueryResponse(
                    conversationId=conv_id,
                    error=error_msg,
                    clarificationNeeded=False,
                )

            # Extract data from results
            data: List[NormalizedData] = []
            if result.get("data"):
                data = result["data"] if isinstance(result["data"], list) else [result["data"]]
            elif result.get("results"):
                # Parallel execution results
                for item in result["results"]:
                    if item.get("result", {}).get("data"):
                        data.extend(item["result"]["data"])

            # Build response with planning info
            todos = result.get("todos", [])
            message = None
            if todos:
                completed = sum(1 for t in todos if t.get("status") == "completed")
                message = f"Completed {completed}/{len(todos)} planned tasks"

            # Update state with results
            if data:
                for d in data:
                    ref = self._create_reference_from_data(query, d)
                    if ref:
                        self.state_manager.add_assistant_message(
                            conv_id,
                            f"Retrieved {ref.indicator}",
                            data_reference=ref
                        )

            return QueryResponse(
                conversationId=conv_id,
                data=data if data else None,
                intent=result.get("intent"),
                message=message,
                clarificationNeeded=False,
            )

        except Exception as e:
            logger.error(f"Deep Agent query error: {e}", exc_info=True)
            # Fallback to standard processing
            return await self._handle_data_fetch(query, routing, state, conv_id)

    def _create_reference_from_data(
        self,
        query: str,
        data: NormalizedData
    ) -> Optional[DataReference]:
        """Create a data reference from a single NormalizedData object."""
        if not data or not data.metadata:
            return None

        import uuid

        return DataReference(
            id=str(uuid.uuid4()),
            query=query,
            provider=data.metadata.source or "UNKNOWN",
            dataset_code=data.metadata.seriesId,
            indicator=data.metadata.indicator or "",
            country=data.metadata.country,
            time_range=(data.metadata.startDate, data.metadata.endDate) if data.metadata.startDate else None,
            unit=data.metadata.unit or "",
            frequency=data.metadata.frequency or "",
            metadata=data.metadata.model_dump() if hasattr(data.metadata, "model_dump") else {},
        )

    async def _handle_research(
        self,
        query: str,
        routing: RoutingResult,
        state: ConversationState,
        conv_id: str
    ) -> QueryResponse:
        """Handle research/availability questions"""
        result = await self.research.process(query, routing.context)

        # Add response to conversation
        self.state_manager.add_assistant_message(conv_id, result.message)

        return QueryResponse(
            conversationId=conv_id,
            message=result.message,
            clarificationNeeded=False,
            # No data - just answering a question
        )

    async def _handle_comparison(
        self,
        query: str,
        routing: RoutingResult,
        state: ConversationState,
        conv_id: str
    ) -> QueryResponse:
        """Handle comparison/multi-series requests"""
        # Set up data fetcher for comparison agent
        if not self.comparison.data_fetcher and self.query_service:
            async def fetch_variant(params):
                # Build query from params
                q = f"{params.get('indicator', 'data')} for {params.get('country', 'EU')}"
                result = await self.query_service.process_query(q)
                return result.data[0] if result.data else None

            self.comparison.data_fetcher = fetch_variant

        result = await self.comparison.process(query, routing.context, state)

        if not result.success:
            return QueryResponse(
                conversationId=conv_id,
                error=result.error,
                clarificationNeeded=False,
            )

        # Add data references to state
        for ref in result.data_references:
            self.state_manager.add_assistant_message(
                conv_id,
                f"Retrieved {ref.indicator}",
                data_reference=ref
            )

        # Build intent from comparison result for response consistency
        intent = None
        if result.datasets and result.data_references:
            # Get provider from first data reference or first dataset metadata
            provider = None
            indicators = []
            countries = []

            for ref in result.data_references:
                if ref.provider and not provider:
                    provider = ref.provider
                if ref.indicator:
                    indicators.append(ref.indicator)
                if ref.country:
                    countries.append(ref.country)

            # Fallback to metadata if no data references
            if not provider and result.datasets:
                first_meta = result.datasets[0].metadata
                provider = first_meta.source if first_meta else "UNKNOWN"

            intent = ParsedIntent(
                apiProvider=provider or "UNKNOWN",
                indicators=indicators or ["comparison"],
                parameters={
                    "countries": countries,
                    "chart_type": result.chart_type,
                    "merge_series": result.merge_series,
                },
                clarificationNeeded=False,
                recommendedChartType=result.chart_type,
            )

        return QueryResponse(
            conversationId=conv_id,
            intent=intent,
            data=result.datasets,
            message=result.message,
            clarificationNeeded=False,
        )

    async def _handle_follow_up(
        self,
        query: str,
        routing: RoutingResult,
        state: ConversationState,
        conv_id: str
    ) -> QueryResponse:
        """Handle follow-up requests"""
        result = await self.data.process(query, routing.context, state)

        if not result.success:
            # Fall back to standard processing
            logger.info("Follow-up failed, falling back to standard processing")
            return await self._handle_data_fetch(query, routing, state, conv_id)

        # Add response to state
        if result.data_reference:
            self.state_manager.add_assistant_message(
                conv_id,
                result.message or "Data retrieved",
                data_reference=result.data_reference
            )

        return QueryResponse(
            conversationId=conv_id,
            data=result.data,
            intent=result.intent,
            message=result.message,
            clarificationNeeded=False,
        )

    async def _handle_analysis(
        self,
        query: str,
        routing: RoutingResult,
        state: ConversationState,
        conv_id: str
    ) -> QueryResponse:
        """Handle analysis requests (Pro Mode)"""
        # Delegate to legacy query service's Pro Mode
        if self.query_service:
            # Enable Pro Mode and include context
            return await self.query_service.process_query(
                query,
                conversation_id=conv_id,
                auto_pro_mode=True,
            )

        return QueryResponse(
            conversationId=conv_id,
            error="Analysis mode not available",
            clarificationNeeded=False,
        )

    async def _handle_data_fetch(
        self,
        query: str,
        routing: RoutingResult,
        state: ConversationState,
        conv_id: str
    ) -> QueryResponse:
        """Handle standard data fetch requests"""
        # Use legacy query service for now
        if self.query_service:
            result = await self.query_service.process_query(query, conversation_id=conv_id)

            # Create data reference if we got data
            if result.data:
                ref = self._create_reference_from_result(query, result)
                self.state_manager.add_assistant_message(
                    conv_id,
                    f"Retrieved {len(result.data)} series",
                    data_reference=ref
                )

            return result

        return QueryResponse(
            conversationId=conv_id,
            error="Query service not available",
            clarificationNeeded=False,
        )

    def _update_state_with_data(self, conv_id: str, response: QueryResponse) -> None:
        """Update conversation state with fetched data"""
        if not response.data:
            return

        state = self.state_manager.get(conv_id)
        if not state:
            return

        # Update entity context
        for data in response.data:
            if data.metadata:
                if data.metadata.country:
                    if data.metadata.country not in state.entity_context.current_countries:
                        state.entity_context.current_countries.append(data.metadata.country)
                if data.metadata.indicator:
                    if data.metadata.indicator not in state.entity_context.current_indicators:
                        state.entity_context.current_indicators.append(data.metadata.indicator)
                if data.metadata.source:
                    state.entity_context.current_provider = data.metadata.source

        # Update last result
        state.last_result = response

    def _create_reference_from_result(
        self,
        query: str,
        result: QueryResponse
    ) -> Optional[DataReference]:
        """Create a data reference from query result"""
        if not result.data:
            return None

        import uuid
        first = result.data[0]
        metadata = first.metadata

        return DataReference(
            id=str(uuid.uuid4()),
            query=query,
            provider=metadata.source or "UNKNOWN",
            dataset_code=metadata.seriesId,
            indicator=metadata.indicator or "",
            country=metadata.country,
            time_range=(metadata.startDate, metadata.endDate) if metadata.startDate else None,
            unit=metadata.unit or "",
            frequency=metadata.frequency or "",
            metadata=metadata.model_dump() if hasattr(metadata, "model_dump") else {},
            chart_type=result.intent.recommendedChartType if result.intent else "line",
        )


# Create default orchestrator (will be initialized with services later)
agent_orchestrator: Optional[AgentOrchestrator] = None


def get_orchestrator() -> AgentOrchestrator:
    """Get or create the global orchestrator instance"""
    global agent_orchestrator
    if agent_orchestrator is None:
        agent_orchestrator = AgentOrchestrator()
    return agent_orchestrator


def initialize_orchestrator(
    query_service=None,
    openrouter_service=None,
    metadata_search=None,
) -> AgentOrchestrator:
    """Initialize the global orchestrator with services"""
    global agent_orchestrator
    agent_orchestrator = AgentOrchestrator(
        query_service=query_service,
        openrouter_service=openrouter_service,
        metadata_search=metadata_search,
    )
    return agent_orchestrator
