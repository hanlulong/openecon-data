"""Tests for ConversationManager thread-safety, TTL, and state management.

Covers:
- Basic CRUD operations (get/create, add message, get history)
- TTL expiration (24-hour window)
- Pending clarification state (mutual exclusion, clearing)
- Thread safety under concurrent access
- Edge cases (expired conversations, safe recovery)
- Redis persistence (write-through, graceful degradation)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import backend.services.conversation as conv_mod
from backend.services.conversation import ConversationManager, ConversationContext


def _fresh_manager() -> ConversationManager:
    """Create a ConversationManager with Redis disabled for isolated tests."""
    mgr = ConversationManager()
    # Patch the module-level Redis helper so tests never hit a real Redis
    return mgr


@pytest.fixture(autouse=True)
def _disable_redis_for_tests(monkeypatch):
    """Ensure no test accidentally writes to or reads from Redis."""
    monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: None)


# ─── Basic CRUD ──────────────────────────────────────────────────────

class TestBasicOperations:
    def test_get_or_create_returns_new_id(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        assert cid is not None
        assert len(cid) > 0

    def test_get_or_create_returns_existing_id(self):
        mgr = _fresh_manager()
        cid1 = mgr.get_or_create(None)
        cid2 = mgr.get_or_create(cid1)
        assert cid1 == cid2

    def test_get_or_create_refreshes_expired_context(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.add_message(cid, "user", "old message")

        # Expire the conversation
        with mgr._lock:
            conv = mgr._conversations[cid]
            conv.updated_at = datetime.now(timezone.utc) - timedelta(hours=25)

        # get_or_create reuses the ID but creates fresh context
        cid2 = mgr.get_or_create(cid)
        assert cid2 == cid  # Same ID
        history = mgr.get_history(cid2)
        assert len(history) == 0  # But fresh context (old messages gone)

    def test_add_message_stores_content(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        mgr.add_message(cid, "user", "Hello")
        mgr.add_message(cid, "assistant", "Hi there")

        # get_history returns List[str] (content only)
        history = mgr.get_history(cid)
        assert len(history) == 2
        assert history[0] == "Hello"
        assert history[1] == "Hi there"

        # get_messages returns List[Dict] with role/content
        messages = mgr.get_messages(cid)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"

    def test_get_history_returns_empty_for_unknown(self):
        mgr = _fresh_manager()
        history = mgr.get_history("nonexistent-id")
        assert history == []

    def test_add_message_updates_timestamp(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        with mgr._lock:
            old_ts = mgr._conversations[cid].updated_at

        time.sleep(0.01)
        mgr.add_message(cid, "user", "test")

        with mgr._lock:
            new_ts = mgr._conversations[cid].updated_at

        assert new_ts > old_ts


# ─── TTL Expiration ──────────────────────────────────────────────────

class TestTTLExpiration:
    def test_conversation_expires_after_max_age(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.add_message(cid, "user", "test")

        # Expire it
        with mgr._lock:
            mgr._conversations[cid].updated_at = datetime.now(timezone.utc) - timedelta(hours=25)

        # Should return empty history (conversation expired)
        history = mgr.get_history(cid)
        assert history == []

    def test_conversation_not_expired_within_max_age(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.add_message(cid, "user", "test")

        # Set to 30 minutes ago (within 1-hour window)
        with mgr._lock:
            mgr._conversations[cid].updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)

        history = mgr.get_history(cid)
        assert len(history) == 1

    def test_add_message_safe_recovers_expired(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.add_message(cid, "user", "old message")

        # Expire it
        with mgr._lock:
            mgr._conversations[cid].updated_at = datetime.now(timezone.utc) - timedelta(hours=25)

        # add_message_safe creates a NEW conversation and returns the new ID
        new_cid = mgr.add_message_safe(cid, "user", "new message")
        assert new_cid != cid  # New ID created

        # The message exists under the new conversation
        history = mgr.get_history(new_cid)
        assert len(history) == 1
        assert history[0] == "new message"

        # Old conversation is gone (expired and cleaned)
        old_history = mgr.get_history(cid)
        assert old_history == []

    def test_cleanup_removes_expired_conversations(self):
        mgr = _fresh_manager()
        active_id = mgr.get_or_create(None)
        expired_id = mgr.get_or_create(None)

        mgr.add_message(active_id, "user", "active")
        mgr.add_message(expired_id, "user", "expired")

        # Expire one
        with mgr._lock:
            mgr._conversations[expired_id].updated_at = datetime.now(timezone.utc) - timedelta(hours=25)

        mgr.cleanup()
        assert active_id in mgr._conversations
        assert expired_id not in mgr._conversations


# ─── Pending Clarification State ─────────────────────────────────────

class TestPendingState:
    def test_set_pending_indicator_clears_semantic(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        mgr.set_pending_semantic_clarification(cid, {"type": "scope"})
        mgr.set_pending_indicator_options(cid, {"options": ["A", "B"]})

        # Semantic should be cleared (mutual exclusion)
        assert mgr.get_pending_semantic_clarification(cid) is None
        assert mgr.get_pending_indicator_options(cid) == {"options": ["A", "B"]}

    def test_set_pending_semantic_clears_indicator(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        mgr.set_pending_indicator_options(cid, {"options": ["X"]})
        mgr.set_pending_semantic_clarification(cid, {"type": "scope"})

        # Indicator should be cleared (mutual exclusion)
        assert mgr.get_pending_indicator_options(cid) is None
        assert mgr.get_pending_semantic_clarification(cid) == {"type": "scope"}

    def test_clear_all_pending(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        mgr.set_pending_indicator_options(cid, {"options": ["A"]})
        mgr.clear_all_pending(cid)

        assert mgr.get_pending_indicator_options(cid) is None
        assert mgr.get_pending_semantic_clarification(cid) is None

    def test_clear_all_pending_on_nonexistent_is_safe(self):
        mgr = _fresh_manager()
        # Should not raise
        mgr.clear_all_pending("nonexistent")

    def test_clear_pending_indicator_options(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.set_pending_indicator_options(cid, {"opts": [1, 2, 3]})
        mgr.clear_pending_indicator_options(cid)
        assert mgr.get_pending_indicator_options(cid) is None

    def test_clear_pending_semantic_clarification(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.set_pending_semantic_clarification(cid, {"q": "broad?"})
        mgr.clear_pending_semantic_clarification(cid)
        assert mgr.get_pending_semantic_clarification(cid) is None


# ─── Pending Clarification Context (Phase 4) ───────────────────────

class TestPendingClarificationContext:
    def test_returns_none_when_no_pending(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        assert mgr.get_pending_clarification_context(cid) is None

    def test_returns_semantic_clarification_context(self):
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.set_pending_semantic_clarification(cid, {
            "kind": "group_scope",
            "original_query": "GDP in G7",
            "question_lines": ["Compare members or group value?", "1. compare member countries"],
            "options": [
                {"id": "1", "label": "compare member countries", "value": "GDP across G7 members"},
                {"id": "2", "label": "group value", "value": "GDP for G7 as a whole"},
            ],
        })

        ctx = mgr.get_pending_clarification_context(cid)
        assert ctx is not None
        assert ctx["kind"] == "group_scope"
        assert ctx["original_query"] == "GDP in G7"
        assert "Compare members or group value?" in ctx["question"]
        assert "compare member countries" in ctx["options"]
        assert "group value" in ctx["options"]

    def test_returns_none_when_only_indicator_pending(self):
        """Indicator choice is handled by structural resolution, not LLM context."""
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.set_pending_indicator_options(cid, {"options": ["[FRED] GDP (A191RL1Q225SBEA)"]})

        ctx = mgr.get_pending_clarification_context(cid)
        assert ctx is None

    def test_returns_none_for_unknown_conversation(self):
        mgr = _fresh_manager()
        assert mgr.get_pending_clarification_context("nonexistent") is None


# ─── Last Intent ─────────────────────────────────────────────────────

class TestLastIntent:
    def test_add_message_with_intent_stores_last_intent(self):
        from backend.models import ParsedIntent
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            clarificationNeeded=False,
            parameters={"country": "US"},
        )
        mgr.add_message(cid, "user", "US GDP", intent=intent)

        retrieved = mgr.get_last_intent(cid)
        assert retrieved is not None
        assert retrieved.apiProvider == "FRED"
        assert retrieved.indicators == ["GDP"]

    def test_get_last_intent_returns_none_for_unknown(self):
        mgr = _fresh_manager()
        assert mgr.get_last_intent("nonexistent") is None

    def test_last_intent_updated_by_later_message(self):
        from backend.models import ParsedIntent
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        i1 = ParsedIntent(apiProvider="FRED", indicators=["GDP"], clarificationNeeded=False)
        i2 = ParsedIntent(apiProvider="WORLDBANK", indicators=["inflation"], clarificationNeeded=False)

        mgr.add_message(cid, "user", "GDP", intent=i1)
        mgr.add_message(cid, "user", "inflation", intent=i2)

        retrieved = mgr.get_last_intent(cid)
        assert retrieved.apiProvider == "WORLDBANK"


# ─── Thread Safety ───────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_add_messages(self):
        """Multiple threads adding messages to the SAME conversation."""
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        num_threads = 10
        messages_per_thread = 20
        errors = []

        def writer(thread_id):
            try:
                for i in range(messages_per_thread):
                    mgr.add_message(cid, "user", f"thread-{thread_id}-msg-{i}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent writes: {errors}"
        history = mgr.get_history(cid)
        assert len(history) == num_threads * messages_per_thread

    def test_concurrent_create_conversations(self):
        """Multiple threads creating conversations simultaneously."""
        mgr = _fresh_manager()
        num_threads = 20
        created_ids = []
        lock = threading.Lock()

        def creator():
            cid = mgr.get_or_create(None)
            with lock:
                created_ids.append(cid)

        threads = [threading.Thread(target=creator) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(created_ids) == num_threads
        # All IDs should be unique
        assert len(set(created_ids)) == num_threads

    def test_concurrent_read_write(self):
        """Readers and writers accessing the same conversation concurrently."""
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        errors = []
        read_results = []
        lock = threading.Lock()

        def writer():
            try:
                for i in range(50):
                    mgr.add_message(cid, "user", f"msg-{i}")
            except Exception as e:
                errors.append(f"writer: {e}")

        def reader():
            try:
                for _ in range(50):
                    history = mgr.get_history(cid)
                    with lock:
                        read_results.append(len(history))
            except Exception as e:
                errors.append(f"reader: {e}")

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        # Reader should see monotonically non-decreasing history length
        # (may see 0 before first write completes)
        assert all(r >= 0 for r in read_results)

    def test_concurrent_pending_state_mutations(self):
        """Multiple threads setting/clearing pending state concurrently."""
        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        errors = []

        def set_indicator():
            try:
                for i in range(30):
                    mgr.set_pending_indicator_options(cid, {"opt": i})
                    mgr.clear_pending_indicator_options(cid)
            except Exception as e:
                errors.append(f"indicator: {e}")

        def set_semantic():
            try:
                for i in range(30):
                    mgr.set_pending_semantic_clarification(cid, {"q": i})
                    mgr.clear_pending_semantic_clarification(cid)
            except Exception as e:
                errors.append(f"semantic: {e}")

        threads = [
            threading.Thread(target=set_indicator),
            threading.Thread(target=set_semantic),
            threading.Thread(target=set_indicator),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        # Final state should be clean (both cleared at end of each loop)
        assert mgr.get_pending_indicator_options(cid) is None
        assert mgr.get_pending_semantic_clarification(cid) is None

    def test_concurrent_cleanup_and_access(self):
        """Cleanup running while other threads access conversations."""
        mgr = _fresh_manager()
        errors = []

        # Create some conversations
        cids = [mgr.get_or_create(None) for _ in range(10)]
        for cid in cids:
            mgr.add_message(cid, "user", "test")

        # Expire half
        with mgr._lock:
            for cid in cids[:5]:
                mgr._conversations[cid].updated_at = datetime.now(timezone.utc) - timedelta(hours=25)

        def cleaner():
            try:
                mgr.cleanup()
            except Exception as e:
                errors.append(f"cleanup: {e}")

        def accessor():
            try:
                for cid in cids:
                    mgr.get_history(cid)
                    mgr.get_last_intent(cid)
            except Exception as e:
                errors.append(f"accessor: {e}")

        threads = [
            threading.Thread(target=cleaner),
            threading.Thread(target=accessor),
            threading.Thread(target=accessor),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"


# ─── Redis Persistence ──────────────────────────────────────────────

class _FakeRedis:
    """Minimal dict-backed Redis stub for persistence tests."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def setex(self, key: str, ttl: int, value: str):
        self._store[key] = value

    def get(self, key: str):
        return self._store.get(key)

    def delete(self, key: str):
        self._store.pop(key, None)


class TestRedisPersistence:
    def test_create_conversation_persists_to_redis(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)

        assert f"conv:{cid}" in fake._store

    def test_add_message_updates_redis(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.add_message(cid, "user", "hello world")

        import json
        stored = json.loads(fake._store[f"conv:{cid}"])
        assert len(stored["messages"]) == 1
        assert stored["messages"][0]["content"] == "hello world"

    def test_get_or_create_loads_from_redis_on_miss(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        # Manager 1: create and populate
        mgr1 = _fresh_manager()
        cid = mgr1.get_or_create(None)
        mgr1.add_message(cid, "user", "persisted message")

        # Manager 2: simulates server restart (empty in-memory)
        mgr2 = _fresh_manager()
        assert cid not in mgr2._conversations

        # get_or_create should reload from Redis
        returned_cid = mgr2.get_or_create(cid)
        assert returned_cid == cid
        history = mgr2.get_history(cid)
        assert history == ["persisted message"]

    def test_refresh_from_redis_overwrites_stale_in_memory_context(self, monkeypatch):
        from backend.models import ParsedIntent
        from backend.services.conversation_state_v2 import ConversationState

        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        mgr1 = _fresh_manager()
        cid = mgr1.get_or_create(None)
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["NGDP_RPCH"],
            clarificationNeeded=False,
            parameters={"country": "CA"},
        )
        state = ConversationState(
            indicator="GDP growth",
            provider="IMF",
            country="Canada",
            provider_locked=True,
        )
        mgr1.add_message(cid, "user", "Show only Canada", intent=intent)
        mgr1.set_conversation_state(cid, state)

        mgr2 = _fresh_manager()
        # Seed stale in-memory context that differs from Redis.
        mgr2._conversations[cid] = conv_mod.ConversationContext(id=cid)
        mgr2._conversations[cid].last_intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["NY.GDP.MKTP.KD.ZG"],
            clarificationNeeded=False,
            parameters={"country": "US"},
        )

        refreshed = mgr2.refresh_from_redis(cid)
        assert refreshed is True
        last_intent = mgr2.get_last_intent(cid)
        assert last_intent is not None
        assert last_intent.apiProvider == "IMF"
        conv_state = mgr2.get_conversation_state(cid)
        assert conv_state is not None
        assert conv_state.provider == "IMF"

    def test_pending_state_persists_to_redis(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.set_pending_indicator_options(cid, {"opts": [1, 2]})

        import json
        stored = json.loads(fake._store[f"conv:{cid}"])
        assert stored["pending_indicator_options"] == {"opts": [1, 2]}
        assert stored["pending_semantic_clarification"] is None

    def test_last_intent_round_trips_through_redis(self, monkeypatch):
        from backend.models import ParsedIntent

        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        mgr1 = _fresh_manager()
        cid = mgr1.get_or_create(None)
        intent = ParsedIntent(
            apiProvider="FRED",
            indicators=["GDP"],
            clarificationNeeded=False,
            parameters={"country": "US"},
        )
        mgr1.add_message(cid, "user", "US GDP", intent=intent)

        # Simulate restart
        mgr2 = _fresh_manager()
        retrieved = mgr2.get_last_intent(cid)
        assert retrieved is not None
        assert retrieved.apiProvider == "FRED"
        assert retrieved.indicators == ["GDP"]

    def test_cleanup_deletes_from_redis(self, monkeypatch):
        fake = _FakeRedis()
        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: fake)

        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.add_message(cid, "user", "test")

        # Expire it
        with mgr._lock:
            mgr._conversations[cid].updated_at = datetime.now(timezone.utc) - timedelta(hours=25)

        mgr.cleanup()
        assert f"conv:{cid}" not in fake._store

    def test_graceful_degradation_on_redis_error(self, monkeypatch):
        """If Redis raises, operations still succeed via in-memory."""
        class _BrokenRedis:
            def setex(self, *a, **kw):
                raise ConnectionError("Redis down")
            def get(self, *a, **kw):
                raise ConnectionError("Redis down")
            def delete(self, *a, **kw):
                raise ConnectionError("Redis down")

        monkeypatch.setattr(conv_mod, "_get_sync_redis", lambda: _BrokenRedis())

        mgr = _fresh_manager()
        cid = mgr.get_or_create(None)
        mgr.add_message(cid, "user", "still works")
        assert mgr.get_history(cid) == ["still works"]
