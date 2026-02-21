"""
LangGraph state schema for agent orchestration.

This module defines the state structure used by the LangGraph agent graph
to maintain context across conversation turns.
"""
from typing import TypedDict, Optional, List, Dict, Any, Annotated
from operator import add
from langgraph.graph import MessagesState

from backend.memory.conversation_state import DataReference, EntityContext, QueryType


class LangGraphProcessingStep(TypedDict):
    """A single processing step for tracking LangGraph agent progress.

    Note: This is different from backend.models.ProcessingStep which is used for API responses.
    This TypedDict is used for internal LangGraph state tracking.
    """
    name: str
    status: str  # "pending", "in_progress", "completed", "error"
    message: str
    metadata: Optional[Dict[str, Any]]


class AgentState(MessagesState):
    """
    Extended state for economic data agents.

    Inherits from MessagesState which provides:
    - messages: List of conversation messages (automatically accumulated)

    Additional fields for economic data context:
    - conversation_id: Unique conversation identifier
    - entity_context: Tracked entities (countries, indicators, datasets)
    - data_references: Previously fetched data for follow-up queries
    - query_type: Classification of current query
    - resolved_context: Context resolved by RouterAgent
    - requires_pro_mode: Whether Pro Mode is needed
    - parsed_intent: The parsed query intent
    - result: Query result data
    - error: Error message if any
    - processing_steps: Progress tracking
    """

    # Conversation tracking
    conversation_id: str

    # Entity context (persisted across turns)
    entity_context: EntityContext

    # Data references from previous fetches (key: ref_id, value: DataReference)
    data_references: Dict[str, Any]  # Using Any to avoid serialization issues

    # Current query classification
    query_type: Optional[str]  # "research", "data_fetch", "comparison", "follow_up", "analysis"

    # Resolved context from RouterAgent
    resolved_context: Dict[str, Any]

    # Pro Mode flag
    requires_pro_mode: bool

    # Parsed intent from LLM
    parsed_intent: Optional[Dict[str, Any]]

    # Query result
    result: Optional[Dict[str, Any]]

    # Code execution result (for Pro Mode)
    code_execution: Optional[Dict[str, Any]]

    # Pro Mode flag in result
    is_pro_mode: bool

    # Error message
    error: Optional[str]

    # Processing steps for progress tracking
    processing_steps: Annotated[List[LangGraphProcessingStep], add]

    # Current provider being used
    current_provider: Optional[str]

    # Current indicators being fetched
    current_indicators: List[str]

    # Clarification attempt counter (prevents infinite loops)
    clarification_attempts: int


def create_initial_state(
    conversation_id: str,
    query: str,
    existing_context: Optional[EntityContext] = None,
    existing_refs: Optional[Dict[str, Any]] = None,
) -> AgentState:
    """
    Create initial state for a new query.

    Args:
        conversation_id: Unique conversation identifier
        query: User's query string
        existing_context: Previous entity context (for follow-up queries)
        existing_refs: Previous data references (for follow-up queries)

    Returns:
        Initialized AgentState
    """
    from langchain_core.messages import HumanMessage

    return AgentState(
        messages=[HumanMessage(content=query)],
        conversation_id=conversation_id,
        entity_context=existing_context or EntityContext(),
        data_references=existing_refs or {},
        query_type=None,
        resolved_context={},
        requires_pro_mode=False,
        parsed_intent=None,
        result=None,
        code_execution=None,
        is_pro_mode=False,
        error=None,
        processing_steps=[],
        current_provider=None,
        current_indicators=[],
        clarification_attempts=0,
    )


def merge_state_with_history(
    current_state: AgentState,
    history_messages: List[Any],
) -> AgentState:
    """
    Merge current state with conversation history.

    Args:
        current_state: Current agent state
        history_messages: Previous conversation messages

    Returns:
        State with merged message history
    """
    # Prepend history to current messages
    merged_messages = history_messages + current_state.get("messages", [])

    return {
        **current_state,
        "messages": merged_messages,
    }
