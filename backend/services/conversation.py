from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..models import ParsedIntent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synchronous Redis helper (lazy singleton)
# ---------------------------------------------------------------------------

_sync_redis_client = None
_sync_redis_init_attempted = False
_sync_redis_lock = threading.Lock()


def _get_sync_redis():
    """Return a synchronous redis.Redis client, or None if unavailable."""
    global _sync_redis_client, _sync_redis_init_attempted

    if _sync_redis_init_attempted:
        return _sync_redis_client

    with _sync_redis_lock:
        if _sync_redis_init_attempted:
            return _sync_redis_client
        _sync_redis_init_attempted = True
        try:
            import redis as _redis

            # Use the same URL resolution strategy as redis_cache.py
            redis_url = "redis://localhost:6379/0"
            try:
                from ..config import get_settings
                settings = get_settings()
                redis_url = (
                    getattr(settings, "redis_url", None)
                    or getattr(settings, "REDIS_URL", None)
                    or redis_url
                )
            except Exception:
                pass

            client = _redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            client.ping()
            _sync_redis_client = client
            logger.info("ConversationManager: connected to Redis for persistence")
        except Exception as exc:
            logger.warning(
                "ConversationManager: Redis unavailable (%s), using in-memory only",
                exc,
            )
            _sync_redis_client = None

    return _sync_redis_client


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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
    pending_indicator_options: Optional[Dict[str, Any]] = None
    pending_semantic_clarification: Optional[Dict[str, Any]] = None
    # Phase 1 dual-write: ConversationState stored alongside last_intent
    conversation_state: Optional[Any] = None  # ConversationState (avoid circular import at class level)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

_REDIS_KEY_PREFIX = "conv:"

# Per-conversation cross-worker lock (production runs 2 uvicorn workers, so the
# in-process RLock does not serialize them). The locked section is sub-second
# (re-read + field mutate + SETEX); the TTL is a generous safety auto-expire in
# case a worker dies mid-section, and the wait bound keeps a contended write
# from blocking the event loop for long (it proceeds degraded after the wait,
# never dropping the write).
_CONV_LOCK_TTL_MS = 10_000
_CONV_LOCK_WAIT_S = 2.0
# Token-guarded release: only the owner that set the token may delete the lock,
# so a slow worker can't release a lock that already expired and was re-taken.
_CONV_LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _get_ttl_seconds() -> int:
    """Return conversation TTL in seconds from config (default 24 hours)."""
    try:
        from ..config import get_settings
        return get_settings().conversation_ttl_hours * 3600
    except Exception:
        return 24 * 3600


def _serialize_context(ctx: ConversationContext) -> str:
    """Serialize a ConversationContext to a JSON string for Redis storage."""
    # Serialize conversation_state (ConversationState pydantic model)
    conv_state_data = None
    if ctx.conversation_state is not None:
        try:
            conv_state_data = ctx.conversation_state.model_dump(mode="json")
        except Exception:
            logger.debug("Failed to serialize conversation_state, skipping")

    data: Dict[str, Any] = {
        "id": ctx.id,
        "created_at": ctx.created_at.isoformat(),
        "updated_at": ctx.updated_at.isoformat(),
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp.isoformat(),
            }
            for m in ctx.messages
        ],
        "last_intent": ctx.last_intent.model_dump(mode="json") if ctx.last_intent else None,
        "pending_indicator_options": ctx.pending_indicator_options,
        "pending_semantic_clarification": ctx.pending_semantic_clarification,
        "conversation_state": conv_state_data,
    }
    return json.dumps(data)


def _deserialize_context(raw: str) -> ConversationContext:
    """Deserialize a JSON string from Redis into a ConversationContext."""
    data = json.loads(raw)
    messages = [
        ConversationMessage(
            role=m["role"],
            content=m["content"],
            timestamp=datetime.fromisoformat(m["timestamp"]),
        )
        for m in data.get("messages", [])
    ]
    last_intent = None
    if data.get("last_intent"):
        try:
            last_intent = ParsedIntent.model_validate(data["last_intent"])
        except Exception:
            logger.debug("Failed to deserialize last_intent from Redis, ignoring")

    # Deserialize conversation_state (backward-compatible: may be absent)
    conversation_state = None
    raw_state = data.get("conversation_state")
    if raw_state:
        try:
            from .conversation_state_v2 import ConversationState
            conversation_state = ConversationState.model_validate(raw_state)
        except Exception:
            logger.debug("Failed to deserialize conversation_state from Redis, ignoring")

    return ConversationContext(
        id=data["id"],
        messages=messages,
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
        last_intent=last_intent,
        pending_indicator_options=data.get("pending_indicator_options"),
        pending_semantic_clarification=data.get("pending_semantic_clarification"),
        conversation_state=conversation_state,
    )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ConversationManager:
    MAX_MESSAGES = 200  # Cap messages per conversation to prevent memory leaks

    @property
    def MAX_AGE(self) -> timedelta:
        """Conversation max age derived from config (default 24h)."""
        return timedelta(seconds=_get_ttl_seconds())

    def __init__(self) -> None:
        self._conversations: Dict[str, ConversationContext] = {}
        self._lock = threading.RLock()  # Reentrant lock for future-proofing

    # ── Redis helpers (best-effort, never raise) ─────────────────────

    def _redis_save(self, ctx: ConversationContext) -> None:
        """Write-through: persist context to Redis. Fails silently."""
        try:
            client = _get_sync_redis()
            if client is None:
                return
            key = f"{_REDIS_KEY_PREFIX}{ctx.id}"
            client.setex(key, _get_ttl_seconds(), _serialize_context(ctx))
        except Exception as exc:
            logger.debug("Redis save failed for %s: %s", ctx.id, exc)

    def _redis_load(self, conversation_id: str) -> Optional[ConversationContext]:
        """Try to load a conversation from Redis. Returns None on miss/error."""
        try:
            client = _get_sync_redis()
            if client is None:
                return None
            raw = client.get(f"{_REDIS_KEY_PREFIX}{conversation_id}")
            if raw is None:
                return None
            ctx = _deserialize_context(raw)
            # Check TTL/expiration at application level too
            if self._is_expired(ctx):
                self._redis_delete(conversation_id)
                return None
            return ctx
        except Exception as exc:
            logger.debug("Redis load failed for %s: %s", conversation_id, exc)
            return None

    def _redis_delete(self, conversation_id: str) -> None:
        """Delete a conversation from Redis. Fails silently."""
        try:
            client = _get_sync_redis()
            if client is not None:
                client.delete(f"{_REDIS_KEY_PREFIX}{conversation_id}")
        except Exception as exc:
            logger.debug("Redis delete failed for %s: %s", conversation_id, exc)

    def _locked_redis_update(self, conversation_id, mutate_fn, *, require_existing=True):
        """Cross-worker-safe read-merge-write for ONE conversation.

        Production runs 2 uvicorn workers; the per-process ``self._lock`` does
        not serialize them, and ``_redis_save`` blind-overwrites the whole blob,
        so two workers handling the same conversation erase each other's turn.
        This wraps a SHORT critical section — re-read the FRESH context from
        Redis, apply ONLY this operation's field via ``mutate_fn``, then save —
        in a per-conversation Redis lock. Because each writer mutates only its
        own field on top of the freshest base, concurrent writers to DIFFERENT
        fields (e.g. add_message vs set_conversation_state) no longer clobber.
        The expensive LLM/provider work happens OUTSIDE this call, so the lock
        is held for milliseconds.

        ``mutate_fn(ctx)`` must mutate only the field(s) this operation owns.
        Returns the saved context, or None when ``require_existing`` is True and
        the conversation does not exist.
        """
        client = _get_sync_redis()
        lock_key = f"{_REDIS_KEY_PREFIX}lock:{conversation_id}"
        token = uuid.uuid4().hex
        acquired = False
        if client is not None:
            import time as _time
            deadline = _time.monotonic() + _CONV_LOCK_WAIT_S
            while True:
                try:
                    if client.set(lock_key, token, nx=True, px=_CONV_LOCK_TTL_MS):
                        acquired = True
                        break
                except Exception as exc:
                    logger.debug("conv lock acquire error for %s: %s", conversation_id, exc)
                    break
                if _time.monotonic() >= deadline:
                    logger.warning(
                        "conv lock wait timed out for %s; proceeding without it (degraded)",
                        conversation_id,
                    )
                    break
                _time.sleep(0.02)
        try:
            with self._lock:
                # Re-read the freshest context: Redis is the cross-worker source
                # of truth, so we see any turn the other worker just committed.
                # (_redis_load already drops expired entries.)
                fresh = self._redis_load(conversation_id) if client is not None else None
                if fresh is None:
                    cached = self._conversations.get(conversation_id)
                    if cached is not None and self._is_expired(cached):
                        # Honor TTL on the in-memory fallback too, matching the
                        # old _get_locked behavior (callers like add_message_safe
                        # rely on expired conversations being treated as absent).
                        self._conversations.pop(conversation_id, None)
                        self._redis_delete(conversation_id)
                        cached = None
                    fresh = cached
                if fresh is None:
                    if require_existing:
                        return None
                    fresh = ConversationContext(id=conversation_id)
                mutate_fn(fresh)
                fresh.updated_at = self._now()
                self._conversations[conversation_id] = fresh
                self._redis_save(fresh)
                return fresh
        finally:
            if acquired and client is not None:
                try:
                    client.eval(_CONV_LOCK_RELEASE_LUA, 1, lock_key, token)
                except Exception:
                    pass

    # ── Core helpers ─────────────────────────────────────────────────

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _is_expired(self, conversation: ConversationContext, now: Optional[datetime] = None) -> bool:
        # Use last activity time so active conversations do not expire mid-session.
        check_time = now or self._now()
        return check_time - conversation.updated_at > self.MAX_AGE

    def _get_locked(self, conversation_id: str) -> Optional[ConversationContext]:
        """Get conversation from in-memory cache, falling back to Redis.

        Must be called while holding self._lock.
        """
        conversation = self._conversations.get(conversation_id)

        # If not in memory, try Redis (handles server restart case)
        if conversation is None:
            conversation = self._redis_load(conversation_id)
            if conversation is not None:
                self._conversations[conversation_id] = conversation

        if not conversation:
            return None
        if self._is_expired(conversation):
            self._conversations.pop(conversation_id, None)
            self._redis_delete(conversation_id)
            return None
        return conversation

    def create_conversation(self) -> str:
        conversation_id = str(uuid.uuid4())
        with self._lock:
            ctx = ConversationContext(id=conversation_id)
            self._conversations[conversation_id] = ctx
            self._redis_save(ctx)
        return conversation_id

    def _get(self, conversation_id: str) -> Optional[ConversationContext]:
        with self._lock:
            return self._get_locked(conversation_id)

    def add_message(self, conversation_id: str, role: str, content: str, intent: Optional[ParsedIntent] = None) -> None:
        def _mut(ctx: ConversationContext) -> None:
            # Append onto the FRESH message list so a concurrent worker's just-
            # appended turn is preserved (the old code appended to a possibly
            # stale in-memory copy, then blind-saved, dropping the other turn).
            ctx.messages.append(
                ConversationMessage(role=role, content=content, timestamp=self._now())
            )
            if len(ctx.messages) > self.MAX_MESSAGES:
                ctx.messages = ctx.messages[-self.MAX_MESSAGES:]
            if intent:
                ctx.last_intent = intent

        if self._locked_redis_update(conversation_id, _mut) is None:
            raise ValueError("Conversation not found")

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
            logger.warning(f"Conversation {conversation_id} expired, creating new one")
            new_id = self.create_conversation()
            self.add_message(new_id, role, content, intent=intent)
            return new_id

    def get_history(self, conversation_id: str) -> List[str]:
        with self._lock:
            conversation = self._get_locked(conversation_id)
            if not conversation:
                return []
            return [message.content for message in list(conversation.messages)]

    def get_messages(self, conversation_id: str) -> List[Dict[str, str]]:
        """
        Get conversation messages formatted for AI API calls

        Returns:
            List of message dicts with 'role' and 'content' keys
        """
        with self._lock:
            conversation = self._get_locked(conversation_id)
            if not conversation:
                return []
            return [
                {"role": msg.role, "content": msg.content}
                for msg in list(conversation.messages)
            ]

    def get_last_intent(self, conversation_id: str) -> Optional[ParsedIntent]:
        """Return the latest stored intent for a conversation."""
        with self._lock:
            conversation = self._get_locked(conversation_id)
            if not conversation or conversation.last_intent is None:
                return None
            return conversation.last_intent.model_copy(deep=True)

    def restore_last_intent(self, conversation_id: str, intent: Optional[ParsedIntent]) -> None:
        """Restore last_intent to a previous value (e.g. after a processing error).

        This prevents failed queries from corrupting conversation state.
        When a round fails, the previous successful intent is restored so
        that subsequent follow-ups can still reference it correctly.
        """
        self._locked_redis_update(
            conversation_id,
            lambda ctx: setattr(ctx, "last_intent", intent),
        )

    # ── Conversation State (v2 architecture) ─────────────────────────

    def get_conversation_state(self, conversation_id: str):
        """Return the ConversationState for a conversation, or None."""
        with self._lock:
            conversation = self._get_locked(conversation_id)
            if not conversation:
                logger.debug("get_conversation_state(%s): conversation NOT FOUND", conversation_id)
                return None
            if conversation.conversation_state is None:
                # The in-memory version may be stale if state was saved by a
                # different code path (e.g., guaranteed save in main.py) after
                # the conversation was already loaded. Try Redis as fallback.
                redis_ctx = self._redis_load(conversation_id)
                if redis_ctx and redis_ctx.conversation_state is not None:
                    conversation.conversation_state = redis_ctx.conversation_state
                    logger.debug("get_conversation_state(%s): recovered state from Redis", conversation_id)
                else:
                    logger.debug("get_conversation_state(%s): state is None (msgs=%d)", conversation_id, len(conversation.messages))
                    return None
            _s = conversation.conversation_state
            logger.debug("get_conversation_state(%s): FOUND state (indicator=%s, provider=%s, base=%s, cube=%s)",
                         conversation_id, getattr(_s, 'indicator', '?'), getattr(_s, 'provider', '?'),
                         getattr(_s, 'base_indicator', '?'), getattr(_s, 'statscan_cube_metadata', None) is not None)
            # Return a deep copy to prevent external mutation
            return conversation.conversation_state.model_copy(deep=True)

    def set_conversation_state(self, conversation_id: str, state) -> None:
        """Persist a ConversationState for a conversation.

        Parameters
        ----------
        conversation_id : str
            The conversation to update.
        state : ConversationState
            The new conversation state to store.
        """
        result = self._locked_redis_update(
            conversation_id,
            lambda ctx: setattr(ctx, "conversation_state", state),
        )
        if result is None:
            logger.warning("set_conversation_state(%s): conversation NOT FOUND — state NOT saved!", conversation_id)
            return
        logger.debug("set_conversation_state(%s): SAVED (indicator=%s, provider=%s, base=%s, cube=%s, turn=%d)",
                     conversation_id, getattr(state, 'indicator', '?'), getattr(state, 'provider', '?'),
                     getattr(state, 'base_indicator', '?'), getattr(state, 'statscan_cube_metadata', None) is not None,
                     getattr(state, 'turn_number', -1))

    def restore_conversation_state(self, conversation_id: str, state) -> None:
        """Restore conversation_state to a previous value after a failure.

        Mirrors :meth:`restore_last_intent` for the v2 state model.
        """
        self._locked_redis_update(
            conversation_id,
            lambda ctx: setattr(ctx, "conversation_state", state),
        )

    def refresh_from_redis(self, conversation_id: str) -> bool:
        """Refresh in-memory context from Redis when persisted state exists.

        This keeps long-lived server memory from drifting behind the
        persisted conversation source of truth across requests.
        """
        with self._lock:
            redis_ctx = self._redis_load(conversation_id)
            if redis_ctx is None:
                return False
            if self._is_expired(redis_ctx):
                self._conversations.pop(conversation_id, None)
                self._redis_delete(conversation_id)
                return False
            self._conversations[conversation_id] = redis_ctx
            return True

    def get_pending_clarification_context(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Return pending clarification details for LLM context building.

        Checks both semantic and indicator pending states and returns a dict
        with the clarification question and options, suitable for inclusion
        in the LLM conversation context.

        Returns:
            Dict with keys: question, options, original_query, kind
            or None if no pending clarification exists.
        """
        with self._lock:
            conversation = self._get_locked(conversation_id)
            if not conversation:
                return None

            # Check semantic clarification first (e.g., group scope)
            semantic = conversation.pending_semantic_clarification
            if semantic and isinstance(semantic, dict):
                question_lines = semantic.get("question_lines") or []
                options = semantic.get("options") or []
                option_labels = []
                for opt in options:
                    if isinstance(opt, dict):
                        option_labels.append(opt.get("label", opt.get("value", str(opt))))
                    else:
                        option_labels.append(str(opt))
                return {
                    "kind": semantic.get("kind", "semantic"),
                    "question": " ".join(question_lines) if question_lines else "Please clarify your query.",
                    "options": option_labels,
                    "original_query": semantic.get("original_query", ""),
                }

            return None

    def clear_all_pending(self, conversation_id: str) -> None:
        """Clear all pending clarification states for a conversation.

        Call at the start of process_query to prevent stale state
        from interfering with new queries.
        """
        def _mut(ctx: ConversationContext) -> None:
            ctx.pending_indicator_options = None
            ctx.pending_semantic_clarification = None
        self._locked_redis_update(conversation_id, _mut)

    def set_pending_indicator_options(self, conversation_id: str, payload: Dict[str, Any]) -> None:
        """Persist pending indicator-choice clarification options for a conversation.

        Clears any pending semantic clarification (mutual exclusion).
        """
        def _mut(ctx: ConversationContext) -> None:
            ctx.pending_indicator_options = dict(payload or {})
            ctx.pending_semantic_clarification = None  # mutual exclusion
        if self._locked_redis_update(conversation_id, _mut) is None:
            raise ValueError("Conversation not found")

    def get_pending_indicator_options(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Get pending indicator-choice clarification payload if present."""
        with self._lock:
            conversation = self._get_locked(conversation_id)
            if not conversation:
                return None
            pending = conversation.pending_indicator_options
            return dict(pending) if isinstance(pending, dict) else None

    def clear_pending_indicator_options(self, conversation_id: str) -> None:
        """Clear pending indicator-choice clarification state."""
        self._locked_redis_update(
            conversation_id,
            lambda ctx: setattr(ctx, "pending_indicator_options", None),
        )

    def set_pending_semantic_clarification(self, conversation_id: str, payload: Dict[str, Any]) -> None:
        """Persist pending semantic clarification state for a conversation.

        Clears any pending indicator options (mutual exclusion).
        """
        def _mut(ctx: ConversationContext) -> None:
            ctx.pending_semantic_clarification = dict(payload or {})
            ctx.pending_indicator_options = None  # mutual exclusion
        if self._locked_redis_update(conversation_id, _mut) is None:
            raise ValueError("Conversation not found")

    def get_pending_semantic_clarification(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Get pending semantic clarification payload if present."""
        with self._lock:
            conversation = self._get_locked(conversation_id)
            if not conversation:
                return None
            pending = conversation.pending_semantic_clarification
            return dict(pending) if isinstance(pending, dict) else None

    def clear_pending_semantic_clarification(self, conversation_id: str) -> None:
        """Clear pending semantic clarification state."""
        self._locked_redis_update(
            conversation_id,
            lambda ctx: setattr(ctx, "pending_semantic_clarification", None),
        )

    def get_or_create(self, conversation_id: Optional[str]) -> str:
        """
        Get existing conversation or create new one.

        IMPORTANT: If a conversation_id is provided but doesn't exist,
        we create a conversation WITH THE PROVIDED ID (not a new UUID).
        This is critical for Pro Mode session storage continuity.

        On server restart, conversations are recovered from Redis transparently
        via _get_locked().

        Args:
            conversation_id: Optional conversation ID

        Returns:
            Conversation ID (existing or new)
        """
        if conversation_id:
            # Check if conversation exists (in-memory or Redis)
            if self._get(conversation_id):
                return conversation_id
            # Create conversation with the provided ID (don't generate new UUID).
            # Go through the locked update with require_existing=False: it
            # re-reads Redis first, so if the OTHER worker already created (and
            # populated) this id in the race window, that context is preserved
            # instead of being clobbered by a fresh empty blob.
            self._locked_redis_update(
                conversation_id, lambda ctx: None, require_existing=False
            )
            return conversation_id
        # No ID provided - generate new one
        return self.create_conversation()

    def cleanup(self) -> None:
        with self._lock:
            now = self._now()
            expired = [
                cid
                for cid, context in self._conversations.items()
                if self._is_expired(context, now)
            ]
            for cid in expired:
                self._conversations.pop(cid, None)
                self._redis_delete(cid)


conversation_manager = ConversationManager()
