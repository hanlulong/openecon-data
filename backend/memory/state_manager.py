"""
State manager for conversation state persistence.

Manages the lifecycle of conversation states across the application.
"""
from typing import Dict, Optional
from .conversation_state import ConversationState, EntityContext, DataReference


class ConversationStateManager:
    """
    Manages conversation states across the application.

    Provides a singleton interface for accessing and managing
    conversation states for multi-turn interactions.
    """

    def __init__(self):
        self._states: Dict[str, ConversationState] = {}

    def get_state(self, conversation_id: str) -> ConversationState:
        """Get or create a conversation state."""
        if conversation_id not in self._states:
            self._states[conversation_id] = ConversationState(conversation_id)
        return self._states[conversation_id]

    def get_or_create(self, conversation_id: Optional[str]) -> str:
        """
        Get existing conversation or create new one.

        Args:
            conversation_id: Optional conversation ID

        Returns:
            Conversation ID (existing or new)
        """
        import uuid
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
        # Ensure state exists
        self.get_state(conversation_id)
        return conversation_id

    def get(self, conversation_id: str) -> Optional[ConversationState]:
        """Get a conversation state if it exists (returns None if not found)."""
        return self._states.get(conversation_id)

    def has_state(self, conversation_id: str) -> bool:
        """Check if a conversation state exists."""
        return conversation_id in self._states

    def delete_state(self, conversation_id: str) -> bool:
        """Delete a conversation state."""
        if conversation_id in self._states:
            del self._states[conversation_id]
            return True
        return False

    def update(
        self,
        conversation_id: str,
        entity_context: Optional[EntityContext] = None,
        data_references: Optional[Dict[str, DataReference]] = None,
    ) -> None:
        """Update a conversation state with new entity context and/or data references."""
        state = self.get_state(conversation_id)
        if entity_context:
            state.entity_context = entity_context
            if entity_context not in state.entities:
                state.entities.append(entity_context)
        if data_references:
            for key, ref in data_references.items():
                if ref not in state.data_references:
                    state.data_references.append(ref)

    def clear_old_states(self, max_age_turns: int = 100) -> int:
        """Clear states older than max_age_turns."""
        to_delete = [
            cid for cid, state in self._states.items()
            if state.turn_count > max_age_turns
        ]
        for cid in to_delete:
            del self._states[cid]
        return len(to_delete)

    def add_user_message(
        self,
        conversation_id: str,
        message: str,
        query_type: Optional[str] = None
    ) -> None:
        """
        Add a user message to the conversation state.

        Args:
            conversation_id: Conversation ID
            message: User message content
            query_type: Optional query type classification
        """
        state = self.get_state(conversation_id)
        state.increment_turn()
        if query_type:
            from .conversation_state import QueryType
            if isinstance(query_type, QueryType):
                state.last_query_type = query_type
            elif isinstance(query_type, str):
                try:
                    state.last_query_type = QueryType(query_type)
                except ValueError:
                    state.last_query_type = QueryType.UNKNOWN
        # Store in metadata history
        if "history" not in state.metadata:
            state.metadata["history"] = []
        state.metadata["history"].append({
            "role": "user",
            "content": message,
            "turn": state.turn_count,
        })

    def add_assistant_message(
        self,
        conversation_id: str,
        message: str,
        data_reference: Optional[DataReference] = None
    ) -> None:
        """
        Add an assistant message to the conversation state.

        Args:
            conversation_id: Conversation ID
            message: Assistant message content
            data_reference: Optional data reference from query
        """
        state = self.get_state(conversation_id)
        if data_reference:
            state.add_data_reference(data_reference)
        # Store in metadata history
        if "history" not in state.metadata:
            state.metadata["history"] = []
        state.metadata["history"].append({
            "role": "assistant",
            "content": message,
            "turn": state.turn_count,
        })


# Alias for backwards compatibility
StateManager = ConversationStateManager

# Singleton instance
_state_manager: Optional[ConversationStateManager] = None


def get_state_manager() -> ConversationStateManager:
    """Get the singleton state manager instance."""
    global _state_manager
    if _state_manager is None:
        _state_manager = ConversationStateManager()
    return _state_manager
