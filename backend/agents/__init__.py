"""
Multi-Agent Architecture for econ-data-mcp

This module provides specialized agents for handling different query types:
- RouterAgent: Classifies queries and routes to appropriate specialist
- ResearchAgent: Answers data availability questions
- DataAgent: Fetches data with context awareness
- ComparisonAgent: Handles multi-series and comparison requests
- AgentOrchestrator: Legacy orchestrator (replaced by LangGraph)

LangGraph Integration (v1.0):
- AgentState: State schema for LangGraph
- get_agent_graph: Creates the LangGraph agent workflow
- create_initial_state: Helper to initialize agent state

Shared Utilities:
- create_data_reference: Standardized DataReference creation
- create_data_reference_from_response: Create from QueryResponse
- create_variant_reference: Create variant for comparisons
- PROVIDER_NAMES: Standard provider name mappings
"""

from .router_agent import RouterAgent
from .research_agent import ResearchAgent
from .data_agent import DataAgent
from .comparison_agent import ComparisonAgent
from .orchestrator import AgentOrchestrator

# LangGraph components
from .langgraph_state import AgentState, create_initial_state, LangGraphProcessingStep
from .langgraph_graph import get_agent_graph, create_agent_graph, reset_agent_graph

# Shared utilities
from .utils import (
    create_data_reference,
    create_data_reference_from_response,
    create_variant_reference,
    normalize_provider_name,
    extract_provider_from_query,
    PROVIDER_NAMES,
    PROVIDER_QUERY_PATTERNS,
)

__all__ = [
    # Agents
    "RouterAgent",
    "ResearchAgent",
    "DataAgent",
    "ComparisonAgent",
    "AgentOrchestrator",
    # LangGraph
    "AgentState",
    "LangGraphProcessingStep",
    "create_initial_state",
    "get_agent_graph",
    "create_agent_graph",
    "reset_agent_graph",
    # Utilities
    "create_data_reference",
    "create_data_reference_from_response",
    "create_variant_reference",
    "normalize_provider_name",
    "extract_provider_from_query",
    "PROVIDER_NAMES",
    "PROVIDER_QUERY_PATTERNS",
]
