from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..models import ParsedIntent


@dataclass
class ConversationMessage:
    role: str
    content: str
    timestamp: datetime


@dataclass
class ConversationContext:
    id: str
    messages: List[ConversationMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_intent: Optional[ParsedIntent] = None


class ConversationManager:
    MAX_AGE = timedelta(hours=1)

    def __init__(self) -> None:
        self._conversations: Dict[str, ConversationContext] = {}
        self._lock = threading.Lock()

    def create_conversation(self) -> str:
        conversation_id = str(uuid.uuid4())
        with self._lock:
            self._conversations[conversation_id] = ConversationContext(id=conversation_id)
        return conversation_id

    def _get(self, conversation_id: str) -> Optional[ConversationContext]:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if not conversation:
                return None
            if datetime.now(timezone.utc) - conversation.created_at > self.MAX_AGE:
                self._conversations.pop(conversation_id, None)
                return None
            return conversation

    def add_message(self, conversation_id: str, role: str, content: str, intent: Optional[ParsedIntent] = None) -> None:
        conversation = self._get(conversation_id)
        if not conversation:
            raise ValueError("Conversation not found")

        conversation.messages.append(
            ConversationMessage(role=role, content=content, timestamp=datetime.now(timezone.utc))
        )
        if intent:
            conversation.last_intent = intent
        conversation.updated_at = datetime.now(timezone.utc)

    def add_message_safe(
        self,
        conversation_id: str,
        role: str,
        content: str,
        intent: Optional[ParsedIntent] = None
    ) -> str:
        """
        Add message with automatic recovery from expired conversations.

        Args:
            conversation_id: Conversation ID to add message to
            role: Message role (user/assistant)
            content: Message content
            intent: Optional parsed intent

        Returns:
            Conversation ID (may be different if conversation expired and new one created)
        """
        try:
            self.add_message(conversation_id, role, content, intent=intent)
            return conversation_id
        except ValueError:
            # Conversation expired - create new one
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Conversation {conversation_id} expired, creating new one")
            new_id = self.create_conversation()
            self.add_message(new_id, role, content, intent=intent)
            return new_id

    def get_history(self, conversation_id: str) -> List[str]:
        conversation = self._get(conversation_id)
        if not conversation:
            return []
        return [message.content for message in conversation.messages]

    def get_messages(self, conversation_id: str) -> List[Dict[str, str]]:
        """
        Get conversation messages formatted for AI API calls

        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        conversation = self._get(conversation_id)
        if not conversation:
            return []
        return [
            {"role": msg.role, "content": msg.content}
            for msg in conversation.messages
        ]

    def get_or_create(self, conversation_id: Optional[str]) -> str:
        """
        Get existing conversation or create new one.

        IMPORTANT: If a conversation_id is provided but doesn't exist,
        we create a conversation WITH THE PROVIDED ID (not a new UUID).
        This is critical for Pro Mode session storage continuity.

        Args:
            conversation_id: Optional conversation ID

        Returns:
            Conversation ID (existing or new)
        """
        if conversation_id:
            # Check if conversation exists
            if self._get(conversation_id):
                return conversation_id
            # Create conversation with the provided ID (don't generate new UUID)
            with self._lock:
                self._conversations[conversation_id] = ConversationContext(id=conversation_id)
            return conversation_id
        # No ID provided - generate new one
        return self.create_conversation()

    def cleanup(self) -> None:
        with self._lock:
            expired = [
                cid
                for cid, context in self._conversations.items()
                if datetime.now(timezone.utc) - context.created_at > self.MAX_AGE
            ]
            for cid in expired:
                self._conversations.pop(cid, None)


conversation_manager = ConversationManager()
