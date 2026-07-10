"""End-to-end stream endpoint behavior for FIX 7 (SSE keepalive), FIX 5 (stream
per-conversation lock), and FIX 8 (anon token header).

Drives /api/query/stream through an in-process ASGI transport with a mocked
query_service so no real provider/LLM work happens. Verifies:
  * a slow query emits a ": keepalive" comment frame (proxies don't idle-time
    out; a long step doesn't look hung),
  * the normal data + done events still stream (the lock + keepalive changes
    didn't break the happy path),
  * an anonymous caller receives an X-OE-Session token header (shadow mode),
  * two overlapping turns on the SAME conversation are serialized (the second
    starts only after the first releases the lock).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import backend.main as m
from backend.models import QueryResponse


@pytest.fixture(autouse=True)
def _stub_backends(monkeypatch):
    """Neutralize external calls so the endpoint runs hermetically."""
    class _FakeSupabase:
        async def record_anonymous_query(self, *a, **k):
            return 1  # under any limit → never blocked

    monkeypatch.setattr(m, "get_supabase_service", lambda: _FakeSupabase())
    # Don't schedule real Supabase logging tasks.
    monkeypatch.setattr(m, "schedule_query_log_to_supabase", lambda **k: None)
    monkeypatch.setattr(m.settings, "anon_identity_mode", "shadow")
    yield


async def _post_stream(body, headers=None):
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/query/stream", json=body, headers=headers or {})
        text = resp.text
    return resp, text


async def test_keepalive_emitted_on_slow_query(monkeypatch):
    # Shorten the keepalive interval so we don't wait 15s.
    monkeypatch.setattr(m, "_SSE_KEEPALIVE_INTERVAL", 0.02)

    async def _slow_process_query(*args, **kwargs):
        # Longer than several 0.1s poll cycles so a keepalive must fire while
        # the query runs and no step events are flowing.
        await asyncio.sleep(0.35)
        return QueryResponse(conversationId="conv-keepalive", clarificationNeeded=False)

    monkeypatch.setattr(m.query_service, "process_query", _slow_process_query)

    resp, text = await _post_stream({"query": "slow one", "sessionId": "sess-k"})
    assert resp.status_code == 200
    assert ": keepalive" in text          # FIX 7
    assert "event: done" in text          # stream completed normally


async def test_happy_path_streams_data_and_issues_token(monkeypatch):
    async def _fast_process_query(*args, **kwargs):
        return QueryResponse(conversationId="conv-fast", clarificationNeeded=False)

    monkeypatch.setattr(m.query_service, "process_query", _fast_process_query)

    resp, text = await _post_stream({"query": "quick", "sessionId": "sess-h"})
    assert resp.status_code == 200
    assert "event: data" in text
    assert "event: done" in text
    # FIX 8: anonymous caller with no token gets one minted on the SSE headers.
    token = resp.headers.get("X-OE-Session")
    assert token
    from backend.services.anon_token import verify_anon_token
    assert verify_anon_token(token) == "sess-h"  # sid seeded from sessionId


async def test_same_conversation_streams_are_serialized(monkeypatch):
    """Two concurrent streams on one conversationId must not overlap inside
    process_query — the second acquires the lock only after the first releases."""
    active = 0
    max_concurrent = 0

    async def _tracked_process_query(*args, **kwargs):
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        await asyncio.sleep(0.15)
        active -= 1
        return QueryResponse(conversationId="conv-shared", clarificationNeeded=False)

    monkeypatch.setattr(m.query_service, "process_query", _tracked_process_query)

    body = {"query": "x", "conversationId": "conv-shared", "sessionId": "sess-s"}
    await asyncio.gather(_post_stream(body), _post_stream(body))
    assert max_concurrent == 1  # serialized by the per-conversation lock


async def test_non_stream_query_issues_token_header(monkeypatch):
    # The non-stream /api/query gained a `response: Response` param for FIX 8;
    # confirm FastAPI still injects deps correctly and the token header rides
    # along with the returned QueryResponse.
    async def _fast(*args, **kwargs):
        return QueryResponse(conversationId="conv-ns", clarificationNeeded=False)

    monkeypatch.setattr(m.query_service, "process_query", _fast)
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/query", json={"query": "hi", "sessionId": "sess-ns"})
    assert resp.status_code == 200
    token = resp.headers.get("X-OE-Session")
    assert token
    from backend.services.anon_token import verify_anon_token
    assert verify_anon_token(token) == "sess-ns"


async def test_different_conversations_stream_concurrently(monkeypatch):
    active = 0
    max_concurrent = 0
    started = asyncio.Event()

    async def _tracked_process_query(*args, **kwargs):
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        if active >= 2:
            started.set()
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        active -= 1
        # conversationId echoed back per call
        return QueryResponse(conversationId=kwargs.get("conversation_id") or "c", clarificationNeeded=False)

    monkeypatch.setattr(m.query_service, "process_query", _tracked_process_query)

    await asyncio.gather(
        _post_stream({"query": "a", "conversationId": "conv-A", "sessionId": "sa"}),
        _post_stream({"query": "b", "conversationId": "conv-B", "sessionId": "sb"}),
    )
    assert max_concurrent == 2  # distinct conversations are not serialized
