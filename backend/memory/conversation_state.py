"""
Conversation state management for multi-turn conversations.

This module provides data structures for maintaining context across
conversation turns, including entity tracking and query classification.
"""
from __future__ import annotations

import uuid
from typing import Optional, List, Dict, Any
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
    """Context for entities and datasets mentioned in the conversation."""
    entity_type: str = ""  # "country", "indicator", "time_period", etc. Empty for initial context
    value: str = ""  # Entity value. Empty for initial context
    confidence: float = 1.0
    source_turn: int = 0  # Which conversation turn introduced this entity
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Runtime context used by RouterAgent/LangGraph/LangChain orchestrators.
    current_datasets: List["DataReference"] = field(default_factory=list)
    current_countries: List[str] = field(default_factory=list)
    current_indicators: List[str] = field(default_factory=list)
    current_time_range: Optional[Any] = None
    current_chart_type: Optional[str] = None
    current_provider: Optional[str] = None

    def add_dataset(self, ref: "DataReference") -> None:
        """Track the latest dataset and update derived context fields."""
        if not ref:
            return

        ref_id = str(ref.id or "").strip()
        if not ref_id:
            ref_id = str(uuid.uuid4())
            ref.id = ref_id

        if not any((existing.id or "") == ref_id for existing in self.current_datasets):
            self.current_datasets.append(ref)

        if ref.country and ref.country not in self.current_countries:
            self.current_countries.append(ref.country)
        for country in ref.countries or []:
            if country and country not in self.current_countries:
                self.current_countries.append(country)

        if ref.indicator and ref.indicator not in self.current_indicators:
            self.current_indicators.append(ref.indicator)
        if ref.provider:
            self.current_provider = ref.provider
        if ref.time_range:
            self.current_time_range = ref.time_range
        if ref.chart_type:
            self.current_chart_type = ref.chart_type

    def get_last_dataset(self) -> Optional["DataReference"]:
        """Return the most recently tracked dataset reference."""
        if not self.current_datasets:
            return None
        return self.current_datasets[-1]


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

    @staticmethod
    def _coerce_data_reference(ref: Any, fallback_id: Optional[str] = None) -> Optional[DataReference]:
        """Coerce dict/object payloads into DataReference instances."""
        if ref is None:
            return None
        if isinstance(ref, DataReference):
            if not ref.id:
                ref.id = fallback_id or str(uuid.uuid4())
            return ref
        if isinstance(ref, dict):
            payload = dict(ref)
            if not payload.get("id"):
                payload["id"] = fallback_id or str(uuid.uuid4())
            try:
                return DataReference(**payload)
            except TypeError:
                # Keep only known fields for forward/backward compatibility.
                allowed_fields = set(DataReference.__dataclass_fields__.keys())
                cleaned = {k: v for k, v in payload.items() if k in allowed_fields}
                cleaned.setdefault("id", fallback_id or str(uuid.uuid4()))
                return DataReference(**cleaned)
        return None

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
        # data_references can be list or dict (LangGraph uses dict).
        # Internally we keep a stable dict keyed by reference id.
        self.data_references: Dict[str, DataReference] = {}
        if isinstance(data_references, dict):
            for key, ref in data_references.items():
                coerced = self._coerce_data_reference(ref, fallback_id=str(key or "").strip() or None)
                if not coerced:
                    continue
                self.data_references[coerced.id] = coerced
        elif isinstance(data_references, list):
            for ref in data_references:
                coerced = self._coerce_data_reference(ref)
                if not coerced:
                    continue
                self.data_references[coerced.id] = coerced
        self.last_query_type: QueryType = QueryType.UNKNOWN
        self.metadata: Dict[str, Any] = {}
        # Also store entity_context directly for LangGraph compatibility
        self.entity_context = entity_context or EntityContext()
        for ref in self.data_references.values():
            self.entity_context.add_dataset(ref)

    def add_entity(self, entity: EntityContext) -> None:
        """Add an entity to the conversation context."""
        entity.source_turn = self.turn_count
        self.entities.append(entity)

    def add_data_reference(self, ref: DataReference) -> None:
        """Add a data reference from a successful query."""
        if ref is None:
            return
        coerced = self._coerce_data_reference(ref)
        if not coerced:
            return
        coerced.turn_id = self.turn_count
        self.data_references[coerced.id] = coerced
        self.entity_context.add_dataset(coerced)

    def get_data_references_map(self) -> Dict[str, DataReference]:
        """Get data references keyed by reference id."""
        return dict(self.data_references)

    def get_all_data_references(self) -> List[DataReference]:
        """Get all tracked data references in insertion order."""
        return list(self.data_references.values())

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

    @property
    def messages(self) -> List[Dict[str, Any]]:
        """Compatibility alias expected by older orchestrator paths."""
        return self.get_raw_history()

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
                    "id": r.id,
                    "indicator": r.indicator,
                    "countries": r.countries,
                    "time_range": r.time_range,
                    "provider": r.provider,
                    "turn_id": r.turn_id,
                }
                for r in self.get_all_data_references()
            ],
            "last_query_type": self.last_query_type.value,
        }
