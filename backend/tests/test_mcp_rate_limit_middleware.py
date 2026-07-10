"""Rate-limit middleware behavior for /mcp, the feedback endpoint, and the
fastapi_mcp self-call.

/mcp is the only otherwise-uncapped LLM-spend path. It used to be exempted
before IP resolution (any path starting with /mcp skipped the limiter), so a
remote MCP client could drive unlimited tool calls. The middleware now lets
/mcp fall through to IP resolution and applies:

  * GET /mcp        -> connection-open limit  (bucket "mcp_conn:{ip}", 12/min)
  * POST /mcp/...   -> per-path message limit  (bucket "{ip}:/mcp/messages", 30/min)

while still exempting loopback traffic (local MCP clients, our own MCP service,
and the fastapi_mcp in-process self-call to /api/query). A no-deploy off-switch
(MCP_RATE_LIMIT_ENABLED=false) restores the old fully-exempt behavior.

These tests drive the pure-ASGI middleware directly (no network) following the
same "call get_rate_limit_for_path / exercise the ASGI callable" pattern as
test_rate_limit_routing.py.
"""
from __future__ import annotations

import asyncio

import pytest
from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.main import RateLimitASGIMiddleware, get_rate_limit_for_path


# ---------------------------------------------------------------------------
# Static limit-table assertions (FIX 1 message limit + FIX 2 feedback limit)
# ---------------------------------------------------------------------------

def test_feedback_endpoint_is_strictly_limited():
    # Unauthenticated, persists JSON, emails the owner per submit — must not
    # inherit the 200/min default.
    assert get_rate_limit_for_path("/api/feedback") == "5/minute"


def test_mcp_messages_limit_is_registered():
    assert get_rate_limit_for_path("/mcp/messages") == "30/minute"


def test_core_limits_unchanged():
    # Regression guard: the /mcp and /api/feedback additions must not shadow
    # existing routes via the startswith() matching.
    assert get_rate_limit_for_path("/api/query") == "30/minute"
    assert get_rate_limit_for_path("/api/query/pro") == "10/minute"
    assert get_rate_limit_for_path("/api/auth/register") == "5/minute"
    assert get_rate_limit_for_path("/api/user/history") == "200/minute"


# ---------------------------------------------------------------------------
# ASGI middleware harness
# ---------------------------------------------------------------------------

class _FakeSettings:
    """Only the two attributes the middleware reads."""
    environment = "production"  # enable limiting (dev/test would bypass)
    trusted_proxies = ["127.0.0.1", "::1"]


class _InnerApp:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _make_mw(monkeypatch=None, mcp_enabled=True):
    """Fresh middleware + inner app + fresh in-memory limiter per test so
    rate-limit buckets never bleed between tests."""
    if monkeypatch is not None:
        monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "true" if mcp_enabled else "false")
    inner = _InnerApp()
    limiter = Limiter(key_func=get_remote_address)  # in-memory storage
    mw = RateLimitASGIMiddleware(inner, _FakeSettings(), limiter)
    return mw, inner


def _drive(mw, method, path, ip="203.0.113.7", xff=None):
    """Run one request through the middleware; return the HTTP status code."""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": (ip, 54321),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    assert start is not None, "middleware sent no response.start"
    return start["status"]


# ---------------------------------------------------------------------------
# Exemptions that MUST still hold
# ---------------------------------------------------------------------------

def test_health_still_early_exempt_from_remote(monkeypatch):
    mw, inner = _make_mw(monkeypatch)
    assert _drive(mw, "GET", "/api/health", ip="203.0.113.7") == 200
    assert inner.calls == 1


def test_loopback_mcp_stream_is_exempt(monkeypatch):
    # Local MCP clients / our own MCP service over loopback with no XFF.
    mw, inner = _make_mw(monkeypatch)
    for _ in range(30):  # far above the 12/min connection cap
        assert _drive(mw, "GET", "/mcp", ip="127.0.0.1") == 200
    assert inner.calls == 30


def test_fastapi_mcp_self_call_to_api_query_is_exempt(monkeypatch):
    # fastapi_mcp re-dispatches the query_data tool to POST /api/query over an
    # in-process ASGITransport: synthetic client 127.0.0.1, no XFF. Must never
    # be throttled even far above the 30/min /api/query limit.
    mw, inner = _make_mw(monkeypatch)
    for _ in range(60):
        assert _drive(mw, "POST", "/api/query", ip="127.0.0.1") == 200
    assert inner.calls == 60


# ---------------------------------------------------------------------------
# Remote /mcp is now throttled (the core FIX 1 change)
# ---------------------------------------------------------------------------

def test_remote_mcp_stream_connection_limited(monkeypatch):
    mw, inner = _make_mw(monkeypatch)
    # 12/min connection cap: first 12 opens pass, the 13th is throttled.
    for i in range(12):
        assert _drive(mw, "GET", "/mcp", ip="198.51.100.9") == 200, f"open {i} should pass"
    assert _drive(mw, "GET", "/mcp", ip="198.51.100.9") == 429
    assert inner.calls == 12


def test_remote_mcp_messages_limited_at_30(monkeypatch):
    mw, inner = _make_mw(monkeypatch)
    for i in range(30):
        assert _drive(mw, "POST", "/mcp/messages/", ip="198.51.100.10") == 200, f"msg {i}"
    assert _drive(mw, "POST", "/mcp/messages/", ip="198.51.100.10") == 429
    assert inner.calls == 30


def test_mcp_get_and_post_use_separate_buckets(monkeypatch):
    # The SSE connection bucket ("mcp_conn:") and the message bucket
    # ("{ip}:/mcp/messages") are independent: exhausting one must not throttle
    # the other.
    mw, inner = _make_mw(monkeypatch)
    ip = "198.51.100.11"
    for _ in range(12):
        assert _drive(mw, "GET", "/mcp", ip=ip) == 200
    assert _drive(mw, "GET", "/mcp", ip=ip) == 429  # connection bucket now full
    # Messages bucket is untouched.
    assert _drive(mw, "POST", "/mcp/messages/", ip=ip) == 200


# ---------------------------------------------------------------------------
# Off-switch (FIX 1e)
# ---------------------------------------------------------------------------

def test_off_switch_restores_full_exemption(monkeypatch):
    mw, inner = _make_mw(monkeypatch, mcp_enabled=False)
    # Well above both caps — all pass because MCP throttling is disabled.
    for _ in range(40):
        assert _drive(mw, "GET", "/mcp", ip="198.51.100.12") == 200
        assert _drive(mw, "POST", "/mcp/messages/", ip="198.51.100.12") == 200
    assert inner.calls == 80


def test_off_switch_does_not_disable_other_limits(monkeypatch):
    # Disabling MCP throttling must NOT loosen ordinary endpoints.
    mw, inner = _make_mw(monkeypatch, mcp_enabled=False)
    for _ in range(30):
        assert _drive(mw, "POST", "/api/query", ip="198.51.100.13") == 200
    assert _drive(mw, "POST", "/api/query", ip="198.51.100.13") == 429


# ---------------------------------------------------------------------------
# Regression: ordinary remote endpoints still limited by their own rule
# ---------------------------------------------------------------------------

def test_remote_query_still_limited_at_30(monkeypatch):
    mw, inner = _make_mw(monkeypatch)
    for _ in range(30):
        assert _drive(mw, "POST", "/api/query", ip="198.51.100.14") == 200
    assert _drive(mw, "POST", "/api/query", ip="198.51.100.14") == 429
