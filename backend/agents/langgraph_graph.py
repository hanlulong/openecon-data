"""
LangGraph-based agent orchestration graph.

This module creates the agent workflow graph using LangGraph's StateGraph.
It coordinates the RouterAgent, DataAgent, ResearchAgent, ComparisonAgent,
and Pro Mode execution with persistent state across conversation turns.
"""
import logging
from typing import Optional, Literal, Dict, Any
import uuid

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command
from langchain_core.messages import HumanMessage, AIMessage

from backend.agents.langgraph_state import AgentState, LangGraphProcessingStep
from backend.memory.conversation_state import (
    DataReference,
    EntityContext,
    QueryType,
    ConversationState,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Node Functions
# ============================================================================

async def router_node(state: AgentState) -> Dict[str, Any]:
    """
    Router node: Classify query and resolve entity references.

    This node uses the RouterAgent to:
    1. Classify the query type (research, data_fetch, comparison, follow_up, analysis)
    2. Resolve entity references from conversation context
    3. Detect if Pro Mode is required
    """
    from backend.agents.router_agent import RouterAgent
    from backend.services.query_complexity import QueryComplexityAnalyzer

    logger.info("🔀 Router node processing query")

    # Get the latest user message
    messages = state.get("messages", [])
    if not messages:
        return {"error": "No query provided"}

    last_message = messages[-1]
    query = last_message.content if hasattr(last_message, 'content') else str(last_message)

    # Build conversation state from LangGraph state
    entity_ctx = state.get("entity_context")
    if entity_ctx is None:
        entity_ctx = EntityContext()
    elif isinstance(entity_ctx, dict):
        # Convert dict to EntityContext
        entity_ctx = EntityContext(
            current_datasets=entity_ctx.get("current_datasets", []),
            current_countries=entity_ctx.get("current_countries", []),
            current_indicators=entity_ctx.get("current_indicators", []),
            current_time_range=entity_ctx.get("current_time_range"),
            current_chart_type=entity_ctx.get("current_chart_type"),
            current_provider=entity_ctx.get("current_provider"),
        )

    conv_state = ConversationState(
        id=state.get("conversation_id", ""),
        entity_context=entity_ctx,
        data_references=state.get("data_references", {}),
    )

    # Classify query using RouterAgent
    router = RouterAgent()
    routing_result = router.classify(query, conv_state)

    # Extract query type and context from RoutingResult
    query_type = routing_result.query_type
    context = routing_result.context

    logger.info(f"📊 Query classified as: {query_type.value}")
    logger.debug(f"Resolved context: {context}")

    # Check if Pro Mode is required using complexity analyzer
    complexity = QueryComplexityAnalyzer.detect_complexity(query)
    requires_pro_mode = complexity.get("pro_mode_required", False)

    if requires_pro_mode:
        logger.info("🚀 Pro Mode required for this query")

    # Add processing step
    step = LangGraphProcessingStep(
        name="query_classification",
        status="completed",
        message=f"Query classified as {query_type.value}",
        metadata={"query_type": query_type.value, "requires_pro_mode": requires_pro_mode},
    )

    return {
        "query_type": query_type.value,
        "resolved_context": context,
        "requires_pro_mode": requires_pro_mode,
        "processing_steps": [step],
    }


async def research_node(state: AgentState) -> Dict[str, Any]:
    """
    Research node: Answer questions about data availability.

    Handles queries like "Does Eurostat have X?" without fetching data.
    """
    from backend.agents.research_agent import ResearchAgent

    logger.info("🔬 Research node processing query")

    messages = state.get("messages", [])
    query = messages[-1].content if messages else ""
    context = state.get("resolved_context", {})

    # Use ResearchAgent
    research_agent = ResearchAgent()

    # Build conversation state
    conv_state = ConversationState(
        id=state.get("conversation_id", ""),
        entity_context=state.get("entity_context") or EntityContext(),
        data_references=state.get("data_references", {}),
    )

    result = await research_agent.process(query, context, conv_state)

    # ResearchResponse is a dataclass with: available, message, datasets,
    # requires_calculation, calculation_components, alternative_sources

    # Add AI response message
    response_message = AIMessage(content=result.message or "I couldn't find information about that.")

    step = LangGraphProcessingStep(
        name="research",
        status="completed",
        message="Research completed",
        metadata={"available": result.available},
    )

    # Convert ResearchResponse to dict for state compatibility
    result_dict = {
        "available": result.available,
        "message": result.message,
        "datasets": result.datasets,
        "requires_calculation": result.requires_calculation,
        "calculation_components": result.calculation_components,
        "alternative_sources": result.alternative_sources,
    }

    return {
        "messages": [response_message],
        "result": result_dict,
        "processing_steps": [step],
    }


async def data_node(state: AgentState) -> Dict[str, Any]:
    """
    Data node: Fetch economic data with context awareness.

    Handles standard data fetch requests and stores DataReferences for follow-ups.
    """
    from backend.agents.data_agent import DataAgent
    from backend.main import get_query_service

    logger.info("📈 Data node processing query")

    messages = state.get("messages", [])
    query = messages[-1].content if messages else ""
    context = state.get("resolved_context", {})

    # Build conversation state
    conv_state = ConversationState(
        id=state.get("conversation_id", ""),
        entity_context=state.get("entity_context") or EntityContext(),
        data_references=state.get("data_references", {}),
    )

    # Get query service and openrouter service for DataAgent
    query_service = get_query_service()
    openrouter_service = query_service.openrouter if query_service else None

    # Use DataAgent with required services
    data_agent = DataAgent(query_service=query_service, openrouter_service=openrouter_service)
    result = await data_agent.process(query, context, conv_state)

    # DataResponse is a dataclass with attributes: success, data, data_reference, intent, message, error
    # Keep entity context from state (DataAgent doesn't update it)
    updated_context = state.get("entity_context") or EntityContext()
    updated_refs = dict(state.get("data_references", {}))

    # Get provider from intent if available
    provider = result.intent.apiProvider if result.intent else "unknown"

    # Merge new data reference if present
    if result.data_reference:
        updated_refs[result.data_reference.id] = result.data_reference

    step = LangGraphProcessingStep(
        name="data_fetch",
        status="completed" if result.success and result.data else "error",
        message=f"Fetched data from {provider}" if result.success else (result.error or "Data fetch failed"),
        metadata={"provider": provider, "data_count": len(result.data) if result.data else 0},
    )

    # Convert DataResponse to dict for state compatibility
    result_dict = {
        "success": result.success,
        "data": result.data,
        "data_reference": result.data_reference,
        "intent": result.intent,
        "message": result.message,
        "error": result.error,
        "provider": provider,
    }

    return {
        "entity_context": updated_context,
        "data_references": updated_refs,
        "result": result_dict,
        "parsed_intent": result.intent,
        "current_provider": provider,
        "processing_steps": [step],
    }


async def comparison_node(state: AgentState) -> Dict[str, Any]:
    """
    Comparison node: Handle multi-series comparisons and variant resolution.

    Handles queries like "Plot consolidated and unconsolidated on same graph".
    """
    from backend.agents.comparison_agent import ComparisonAgent

    logger.info("📊 Comparison node processing query")

    messages = state.get("messages", [])
    query = messages[-1].content if messages else ""
    context = state.get("resolved_context", {})

    # Build conversation state
    conv_state = ConversationState(
        id=state.get("conversation_id", ""),
        entity_context=state.get("entity_context") or EntityContext(),
        data_references=state.get("data_references", {}),
    )

    # Use ComparisonAgent
    comparison_agent = ComparisonAgent()
    result = await comparison_agent.process(query, context, conv_state)

    # ComparisonResponse is a dataclass with: success, datasets, data_references, chart_type,
    # merge_series, legend_labels, message, error

    # Update data references with comparison results
    updated_refs = dict(state.get("data_references", {}))
    if result.data_references:
        for ref in result.data_references:
            updated_refs[ref.id] = ref

    step = LangGraphProcessingStep(
        name="comparison",
        status="completed" if result.success and result.datasets else "error",
        message=result.message if result.success else (result.error or "Comparison failed"),
        metadata={"legend_labels": result.legend_labels, "chart_type": result.chart_type},
    )

    # Convert ComparisonResponse to dict for state compatibility
    result_dict = {
        "success": result.success,
        "data": result.datasets,  # Map to 'data' for consistency
        "datasets": result.datasets,
        "data_references": result.data_references,
        "chart_type": result.chart_type,
        "merge_series": result.merge_series,
        "legend_labels": result.legend_labels,
        "message": result.message,
        "error": result.error,
    }

    # Build parsed_intent from comparison result for response consistency
    parsed_intent = None
    if result.success and result.datasets:
        # Get provider and indicators from data references or dataset metadata
        provider = None
        indicators = []
        countries = []

        if result.data_references:
            for ref in result.data_references:
                if hasattr(ref, 'provider') and ref.provider and not provider:
                    provider = ref.provider
                if hasattr(ref, 'indicator') and ref.indicator:
                    indicators.append(ref.indicator)
                if hasattr(ref, 'country') and ref.country:
                    countries.append(ref.country)

        # Fallback to metadata if no provider from references
        if not provider and result.datasets:
            first_meta = result.datasets[0].metadata if result.datasets[0] else None
            if first_meta:
                provider = first_meta.source

        parsed_intent = {
            "apiProvider": provider or "UNKNOWN",
            "indicators": indicators or ["comparison"],
            "parameters": {
                "countries": countries,
                "chart_type": result.chart_type,
                "merge_series": result.merge_series,
            },
            "clarificationNeeded": False,
            "recommendedChartType": result.chart_type,
        }

    return {
        "data_references": updated_refs,
        "result": result_dict,
        "processing_steps": [step],
        "parsed_intent": parsed_intent,
    }


async def pro_mode_node(state: AgentState) -> Dict[str, Any]:
    """
    Pro Mode node: Execute AI-generated Python code with full context.

    This node:
    1. Builds available_data from DataReferences in conversation history
    2. Passes entity context to the code generator
    3. Executes generated code with session storage
    """
    from backend.services.grok import get_grok_service
    from backend.services.code_executor import get_code_executor
    from backend.services.session_storage import get_session_storage

    logger.info("🚀 Pro Mode node processing query")

    messages = state.get("messages", [])
    query = messages[-1].content if messages else ""
    conversation_id = state.get("conversation_id", "anonymous")
    session_id = conversation_id[:8]

    # BUILD AVAILABLE DATA FROM CONVERSATION HISTORY
    available_data: Dict[str, Any] = {}

    # 1. Include all DataReferences from previous fetches
    data_refs = state.get("data_references", {})
    for ref_id, ref in data_refs.items():
        if isinstance(ref, dict):
            # Already serialized
            indicator = ref.get("indicator", "data")
            country = ref.get("country", "")
        elif hasattr(ref, "indicator"):
            # DataReference object
            indicator = ref.indicator
            country = ref.country or ""
        else:
            continue

        key = f"{indicator}_{country}" if country else indicator
        key = key.replace(" ", "_").replace("-", "_").lower()

        if isinstance(ref, dict):
            available_data[key] = {
                "data": ref.get("data"),
                "metadata": ref.get("metadata"),
                "provider": ref.get("provider"),
                "unit": ref.get("unit"),
                "dataset_code": ref.get("dataset_code"),
            }
        else:
            available_data[key] = {
                "data": ref.data if hasattr(ref, "data") else None,
                "metadata": ref.metadata if hasattr(ref, "metadata") else None,
                "provider": ref.provider if hasattr(ref, "provider") else None,
                "unit": ref.unit if hasattr(ref, "unit") else None,
                "dataset_code": ref.dataset_code if hasattr(ref, "dataset_code") else None,
            }

    # 2. Include session storage keys
    try:
        session_storage = get_session_storage()
        session_keys = session_storage.list_keys(session_id)
        if session_keys:
            available_data["session_data_available"] = session_keys
    except Exception as e:
        logger.warning(f"Failed to get session keys: {e}")

    # 3. Include entity context for reference resolution
    entity_context = state.get("entity_context")
    if entity_context:
        if isinstance(entity_context, dict):
            available_data["_context"] = {
                "current_countries": entity_context.get("current_countries", []),
                "current_indicators": entity_context.get("current_indicators", []),
                "current_provider": entity_context.get("current_provider"),
                "current_chart_type": entity_context.get("current_chart_type"),
            }
        elif hasattr(entity_context, "current_countries"):
            available_data["_context"] = {
                "current_countries": entity_context.current_countries,
                "current_indicators": entity_context.current_indicators,
                "current_provider": entity_context.current_provider,
                "current_chart_type": entity_context.current_chart_type,
            }

    logger.info(f"Pro Mode available data keys: {list(available_data.keys())}")

    # Generate code with full context
    grok = get_grok_service()

    # Convert messages to conversation history format
    conversation_history = []
    for i, m in enumerate(messages[:-1]):  # Exclude current query
        content = m.content if hasattr(m, "content") else str(m)
        role = "user" if isinstance(m, HumanMessage) else "assistant"
        conversation_history.append({"role": role, "content": content})

    try:
        code = await grok.generate_code(
            query=query,
            conversation_history=conversation_history,
            available_data=available_data,
            session_id=session_id
        )

        # Execute code
        executor = get_code_executor()
        result = await executor.execute_code(code, session_id)

        step = LangGraphProcessingStep(
            name="pro_mode_execution",
            status="completed" if not result.error else "error",
            message="Code generated and executed",
            metadata={"code_length": len(code), "has_output": bool(result.output)},
        )

        return {
            "code_execution": {
                "code": code,
                "output": result.output,
                "error": result.error,
                "files": result.files,
            },
            "is_pro_mode": True,
            "processing_steps": [step],
        }

    except Exception as e:
        logger.error(f"Pro Mode execution failed: {e}")
        step = LangGraphProcessingStep(
            name="pro_mode_execution",
            status="error",
            message=f"Pro Mode failed: {str(e)}",
            metadata={},
        )
        return {
            "error": str(e),
            "is_pro_mode": True,
            "processing_steps": [step],
        }


async def clarification_node(state: AgentState) -> Dict[str, Any]:
    """
    Clarification node: Handle queries that need user clarification.

    Uses LangGraph's interrupt() to pause execution and wait for user input.
    This enables human-in-the-loop patterns for ambiguous queries.
    """
    # Increment clarification attempts counter
    current_attempts = state.get("clarification_attempts", 0) + 1
    MAX_CLARIFICATION_ATTEMPTS = 3

    logger.info(f"❓ Clarification needed (attempt {current_attempts}/{MAX_CLARIFICATION_ATTEMPTS})")

    # Check if we've exceeded max attempts - force a best-effort response
    if current_attempts >= MAX_CLARIFICATION_ATTEMPTS:
        logger.warning(f"Max clarification attempts ({MAX_CLARIFICATION_ATTEMPTS}) reached - proceeding with best effort")
        step = LangGraphProcessingStep(
            name="clarification_limit_reached",
            status="completed",
            message=f"Maximum clarification attempts ({MAX_CLARIFICATION_ATTEMPTS}) reached, proceeding with available context",
            metadata={"attempts": current_attempts},
        )
        return {
            "clarification_attempts": current_attempts,
            "needs_clarification": False,  # Force proceed without more clarification
            "query_type": "data_fetch",  # Default to data fetch
            "processing_steps": [step],
        }

    parsed_intent = state.get("parsed_intent", {})
    clarification_questions = []

    if isinstance(parsed_intent, dict):
        clarification_questions = parsed_intent.get("clarificationQuestions", [])
    elif hasattr(parsed_intent, "clarificationQuestions"):
        clarification_questions = parsed_intent.clarificationQuestions or []

    if not clarification_questions:
        clarification_questions = ["Could you please provide more details about your request?"]

    # Use LangGraph interrupt to pause and get human input
    # This will return control to the caller with the clarification request
    try:
        user_response = interrupt({
            "type": "clarification_needed",
            "questions": clarification_questions,
            "context": {
                "query_type": state.get("query_type"),
                "current_provider": state.get("current_provider"),
            }
        })

        # If we get here, user has provided a response
        if user_response:
            logger.info(f"Received clarification: {user_response[:100]}...")

            # Add user's clarification to messages
            messages = state.get("messages", [])
            messages.append(HumanMessage(content=user_response))

            step = LangGraphProcessingStep(
                name="clarification_received",
                status="completed",
                message="User provided clarification",
                metadata={"response_length": len(user_response)},
            )

            return {
                "messages": messages,
                "needs_clarification": False,
                "clarification_response": user_response,
                "clarification_attempts": current_attempts,
                "processing_steps": [step],
            }

    except Exception as e:
        logger.warning(f"Clarification interrupt failed: {e}")

    # If interrupt not supported or failed, return clarification questions in result
    step = LangGraphProcessingStep(
        name="clarification_requested",
        status="completed",
        message=f"Clarification questions generated (attempt {current_attempts}/{MAX_CLARIFICATION_ATTEMPTS})",
        metadata={"question_count": len(clarification_questions), "attempt": current_attempts},
    )

    return {
        "needs_clarification": True,
        "clarification_attempts": current_attempts,
        "result": {
            "clarification_needed": True,
            "questions": clarification_questions,
            "attempt": current_attempts,
            "max_attempts": MAX_CLARIFICATION_ATTEMPTS,
        },
        "processing_steps": [step],
    }


# ============================================================================
# Routing Functions
# ============================================================================

def route_after_router(state: AgentState) -> Literal["research", "data", "comparison", "pro_mode", "clarification"]:
    """
    Route to appropriate agent based on query classification.
    """
    # Check if clarification is needed
    parsed_intent = state.get("parsed_intent", {})
    if isinstance(parsed_intent, dict) and parsed_intent.get("clarificationNeeded"):
        return "clarification"
    elif hasattr(parsed_intent, "clarificationNeeded") and parsed_intent.clarificationNeeded:
        return "clarification"

    # Check if Pro Mode is required
    if state.get("requires_pro_mode"):
        return "pro_mode"

    query_type = state.get("query_type", "data_fetch")

    routing_map = {
        "research": "research",
        "data_fetch": "data",
        "comparison": "comparison",
        "follow_up": "data",  # Follow-ups use data agent with resolved context
        "analysis": "pro_mode",
        "clarification": "clarification",
    }

    return routing_map.get(query_type, "data")


def route_after_clarification(state: AgentState) -> Literal["router", "__end__"]:
    """
    Route after clarification - either re-route or end.
    """
    # If user provided clarification, re-route through router
    if state.get("clarification_response"):
        return "router"

    # If no response (API returned clarification questions), end
    return "__end__"


# ============================================================================
# Graph Creation
# ============================================================================

def create_agent_graph(checkpointer=None):
    """
    Create the LangGraph agent orchestration graph.

    Graph Structure:
    ```
    START → router → [research|data|comparison|pro_mode|clarification]
                      ↓          ↓         ↓           ↓            ↓
                     END        END       END         END      router (if response)
                                                                  or END
    ```

    Args:
        checkpointer: Optional checkpointer for state persistence.
                     Defaults to MemorySaver for in-memory persistence.

    Returns:
        Compiled StateGraph
    """
    # Create graph with AgentState schema
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("router", router_node)
    graph.add_node("research", research_node)
    graph.add_node("data", data_node)
    graph.add_node("comparison", comparison_node)
    graph.add_node("pro_mode", pro_mode_node)
    graph.add_node("clarification", clarification_node)

    # Add edges
    graph.add_edge(START, "router")

    # Conditional routing after router
    graph.add_conditional_edges(
        "router",
        route_after_router,
        {
            "research": "research",
            "data": "data",
            "comparison": "comparison",
            "pro_mode": "pro_mode",
            "clarification": "clarification",
        }
    )

    # Conditional routing after clarification (human-in-the-loop)
    graph.add_conditional_edges(
        "clarification",
        route_after_clarification,
        {
            "router": "router",  # Re-route with user's clarification
            "__end__": END,      # End if returning clarification questions to user
        }
    )

    # All specialist agents return to END
    graph.add_edge("research", END)
    graph.add_edge("data", END)
    graph.add_edge("comparison", END)
    graph.add_edge("pro_mode", END)

    # Use provided checkpointer or default to memory
    if checkpointer is None:
        checkpointer = MemorySaver()

    return graph.compile(checkpointer=checkpointer)


# ============================================================================
# Singleton Instance
# ============================================================================

_agent_graph = None
_checkpointer = None


def get_agent_graph():
    """
    Get or create the agent graph singleton.

    Returns:
        Compiled StateGraph instance
    """
    global _agent_graph, _checkpointer
    if _agent_graph is None:
        _checkpointer = MemorySaver()
        _agent_graph = create_agent_graph(_checkpointer)
        logger.info("✅ LangGraph agent graph initialized")
    return _agent_graph


def reset_agent_graph():
    """
    Reset the agent graph singleton.

    Useful for testing or reinitialization.
    """
    global _agent_graph, _checkpointer
    _agent_graph = None
    _checkpointer = None
