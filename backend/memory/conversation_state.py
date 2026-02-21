"""
Conversation state management for multi-turn conversations.

This module provides data structures for maintaining context across
conversation turns, including entity tracking and query classification.
"""
from typing import TypedDict, Optional, List, Dict, Any
from enum import Enum
from dataclasses import dataclass, field


class QueryType(str, Enum):
    """Types of queries the system can handle."""
    DATA_FETCH = "data_fetch"
    COMPARISON = "comparison"
    ANALYSIS = "analysis"
    FOLLOW_UP = "follow_up"
    CLARIFICATION = "clarification"
    UNKNOWN = "unknown"


@dataclass
class EntityContext:
    """Context for an entity mentioned in the conversation."""
    entity_type: str = ""  # "country", "indicator", "time_period", etc. Empty for initial context
    value: str = ""  # Entity value. Empty for initial context
    confidence: float = 1.0
    source_turn: int = 0  # Which conversation turn introduced this entity
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DataReference:
    """Reference to data fetched in previous turns.

    This class is used by multiple agents to track data context.
    All fields are optional with defaults for flexible initialization.
    """
    # Core identifiers
    id: str = ""  # UUID for this reference
    query: str = ""  # Original query that fetched this data
    provider: str = ""  # Data provider (FRED, WorldBank, etc.)

    # Data identification
    indicator: str = ""  # Indicator name/code
    dataset_code: Optional[str] = None  # Provider-specific dataset code
    country: Optional[str] = None  # Country/region code
    countries: List[str] = field(default_factory=list)  # Multiple countries

    # Time and metadata
    time_range: Optional[Any] = None  # Can be str or tuple (start, end)
    unit: str = ""  # Unit of measurement
    frequency: str = ""  # Data frequency (M, Q, A, etc.)
    metadata: Dict[str, Any] = field(default_factory=dict)  # Full metadata

    # Data and visualization
    data: Optional[List[Dict[str, Any]]] = None  # Actual data points
    chart_type: str = "line"  # Recommended chart type
    variants: List[str] = field(default_factory=list)  # Data variants

    # Legacy fields for backwards compatibility
    turn_id: int = 0  # Which conversation turn
    data_summary: Optional[Dict[str, Any]] = None  # Summary stats


class ConversationState:
    """
    Manages state across conversation turns.

    Tracks entities, data references, and query context to enable
    multi-turn conversations with context awareness.
    """

    def __init__(
        self,
        conversation_id: str = "",
        *,  # Force keyword arguments for optional params
        id: str = None,  # Alias for conversation_id (used by LangGraph)
        entity_context: Optional[EntityContext] = None,
        data_references: Optional[Dict[str, DataReference]] = None,
    ):
        # Allow 'id' as alias for conversation_id (LangGraph compatibility)
        self.conversation_id = id if id is not None else conversation_id
        self.turn_count = 0
        self.entities: List[EntityContext] = []
        if entity_context:
            self.entities.append(entity_context)
        # data_references can be list or dict (LangGraph uses dict)
        if isinstance(data_references, dict):
            self.data_references: List[DataReference] = list(data_references.values())
        elif isinstance(data_references, list):
            self.data_references = data_references
        else:
            self.data_references = []
        self.last_query_type: QueryType = QueryType.UNKNOWN
        self.metadata: Dict[str, Any] = {}
        # Also store entity_context directly for LangGraph compatibility
        self.entity_context = entity_context

    def add_entity(self, entity: EntityContext) -> None:
        """Add an entity to the conversation context."""
        entity.source_turn = self.turn_count
        self.entities.append(entity)

    def add_data_reference(self, ref: DataReference) -> None:
        """Add a data reference from a successful query."""
        ref.turn_id = self.turn_count
        self.data_references.append(ref)

    def get_entities_by_type(self, entity_type: str) -> List[EntityContext]:
        """Get all entities of a specific type."""
        return [e for e in self.entities if e.entity_type == entity_type]

    def get_recent_countries(self) -> List[str]:
        """Get countries mentioned in recent turns."""
        country_entities = self.get_entities_by_type("country")
        return [e.value for e in country_entities[-5:]]  # Last 5

    def get_recent_indicators(self) -> List[str]:
        """Get indicators mentioned in recent turns."""
        indicator_entities = self.get_entities_by_type("indicator")
        return [e.value for e in indicator_entities[-5:]]

    def increment_turn(self) -> None:
        """Increment the turn counter."""
        self.turn_count += 1

    def get_raw_history(self) -> List[Dict[str, Any]]:
        """Get conversation history as raw list (for LangGraph compatibility)."""
        # Return a list of messages/context, or empty list if none
        return self.metadata.get("history", [])

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to dictionary."""
        return {
            "conversation_id": self.conversation_id,
            "turn_count": self.turn_count,
            "entities": [
                {
                    "entity_type": e.entity_type,
                    "value": e.value,
                    "confidence": e.confidence,
                    "source_turn": e.source_turn,
                }
                for e in self.entities
            ],
            "data_references": [
                {
                    "indicator": r.indicator,
                    "countries": r.countries,
                    "time_range": r.time_range,
                    "provider": r.provider,
                    "turn_id": r.turn_id,
                }
                for r in self.data_references
            ],
            "last_query_type": self.last_query_type.value,
        }
