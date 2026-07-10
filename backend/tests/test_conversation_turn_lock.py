"""Per-conversation request-edge lock (FIX 5).

process_query is invoked bare from /api/query and /api/query/stream. Two
overlapping turns on the SAME conversationId (double-submit / client retry)
could previously interleave and lose a conversation-state update (the in-memory
ConversationState is last-writer-wins). The endpoints now serialize turns on a
per-conversationId asyncio.Lock. These tests cover the lock helpers directly:
identity, mutual exclusion, no serialization across different conversations,
the no-op path for one-shot anonymous queries, and self-bounding via the
WeakValueDictionary.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc

from backend.main import (
    _conversation_locks,
    _conversation_turn_lock,
    _get_conversation_lock,
)


def test_same_conversation_returns_same_lock():
    a = _get_conversation_lock("conv-1")
    b = _get_conversation_lock("conv-1")
    assert a is b


def test_different_conversations_get_distinct_locks():
    a = _get_conversation_lock("conv-A")
    b = _get_conversation_lock("conv-B")
    assert a is not b


def test_turn_lock_is_noop_without_conversation_id():
    # One-shot anonymous queries (no conversationId) must not serialize.
    ctx = _conversation_turn_lock(None)
    assert isinstance(ctx, contextlib.nullcontext)


def test_turn_lock_returns_lock_when_conversation_present():
    ctx = _conversation_turn_lock("conv-present")
    assert isinstance(ctx, asyncio.Lock)
    assert ctx is _get_conversation_lock("conv-present")


async def test_same_conversation_turns_are_serialized():
    """Two turns on the same conversation must not run concurrently."""
    order = []
    max_concurrent = 0
    active = 0

    async def turn(label):
        nonlocal active, max_concurrent
        async with _conversation_turn_lock("conv-serial"):
            active += 1
            max_concurrent = max(max_concurrent, active)
            order.append(f"{label}-start")
            await asyncio.sleep(0.05)  # hold the lock across an await point
            order.append(f"{label}-end")
            active -= 1

    await asyncio.gather(turn("t1"), turn("t2"))

    # Never overlapped, and each turn's start/end are adjacent (not interleaved).
    assert max_concurrent == 1
    assert order in (
        ["t1-start", "t1-end", "t2-start", "t2-end"],
        ["t2-start", "t2-end", "t1-start", "t1-end"],
    )


async def test_different_conversations_run_concurrently():
    """Turns on different conversations must be free to overlap."""
    max_concurrent = 0
    active = 0
    barrier = asyncio.Event()

    async def turn(cid):
        nonlocal active, max_concurrent
        async with _conversation_turn_lock(cid):
            active += 1
            max_concurrent = max(max_concurrent, active)
            # Wait until both turns are inside their (distinct) locks.
            if active >= 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=1.0)
            active -= 1

    await asyncio.gather(turn("conv-X"), turn("conv-Y"))
    assert max_concurrent == 2


async def test_no_conversation_id_turns_do_not_block_each_other():
    max_concurrent = 0
    active = 0
    barrier = asyncio.Event()

    async def turn():
        nonlocal active, max_concurrent
        async with _conversation_turn_lock(None):
            active += 1
            max_concurrent = max(max_concurrent, active)
            if active >= 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=1.0)
            active -= 1

    await asyncio.gather(turn(), turn())
    assert max_concurrent == 2


def test_idle_locks_are_garbage_collected():
    """The WeakValueDictionary self-bounds: a lock with no live holder is
    collected, so the map cannot grow without bound under many distinct
    conversationIds."""
    cid = "conv-ephemeral-xyz"
    lock = _get_conversation_lock(cid)
    assert cid in _conversation_locks
    del lock
    gc.collect()
    assert cid not in _conversation_locks
