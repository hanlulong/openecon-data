"""Every query exit logs exactly ONE analytics row with a truthful outcome.

Mechanism under test (not a single query): the /api/query, /api/query/stream
and /api/query/pro endpoints each have several exits — data/clarification,
server deadline, processing failure, unhandled exception. Before this fix the
timeout and 500 exits returned BEFORE scheduling the Supabase row, so those
outcomes were invisible in analytics, and the stream path logged a NULL
processing_time_ms. Every row now also carries a STRUCTURAL request channel.

Supabase is never touched: ``schedule_query_log_to_supabase`` is replaced by a
recorder in every test.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

import backend.main as m
from backend.main import QueryRequest, query_endpoint, query_pro_endpoint
from backend.models import NormalizedData, QueryResponse, User


def _mock_request(headers: dict | None = None, host: str = "203.0.113.9"):
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = host
    return req


def _data_response(conversation_id: str = "c-data") -> QueryResponse:
    return QueryResponse(
        conversationId=conversation_id,
        clarificationNeeded=False,
        data=[
            NormalizedData.model_validate(
                {
                    "metadata": {
                        "source": "FRED",
                        "indicator": "GDP",
                        "country": "US",
                        "frequency": "quarterly",
                        "unit": "Billions",
                        "lastUpdated": "2024-01-01",
                    },
                    "data": [{"date": "2020-01-01", "value": 100}],
                }
            )
        ],
    )


@pytest.fixture
def rows(monkeypatch):
    """Recorder for scheduled Supabase rows (never hits Supabase)."""
    recorded: list[dict] = []
    monkeypatch.setattr(m, "schedule_query_log_to_supabase", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(m.settings, "anon_query_limit", 0)
    return recorded


# ---------------------------------------------------------------- /api/query


async def test_query_timeout_logs_one_timeout_row(rows, monkeypatch):
    async def _slow(*_a, **_k):
        await asyncio.sleep(0.2)

    monkeypatch.setattr(m.settings, "query_timeout_seconds", 0.01)
    monkeypatch.setattr(m.query_service, "process_query", _slow)

    resp = await query_endpoint(
        QueryRequest(query="Canada GDP", sessionId="s-1"),
        _mock_request({"user-agent": "Go-http-client/2.0"}),
        user=None,
    )

    assert resp.status_code == 504
    assert len(rows) == 1
    row = rows[0]
    assert row["response_status"] == "timeout"
    assert row["error_message"] == "request_timeout"
    assert row["processing_time_ms"] is not None
    assert row["session_id"] == "s-1"
    assert row["channel"] == "api:go-http-client"
    assert json.loads(resp.body)["processingTimeMs"] == row["processing_time_ms"]


async def test_query_processing_error_logs_one_server_error_row(rows, monkeypatch):
    async def _err(*_a, **_k):
        return QueryResponse(conversationId="c-err", clarificationNeeded=False, error="processing_error")

    monkeypatch.setattr(m.query_service, "process_query", _err)

    resp = await query_endpoint(
        QueryRequest(query="x"), _mock_request({"sec-fetch-mode": "cors"}), user=None
    )

    assert resp.status_code == 500
    assert [r["response_status"] for r in rows] == ["server_error"]
    assert rows[0]["conversation_id"] == "c-err"
    assert rows[0]["processing_time_ms"] is not None
    assert rows[0]["channel"] == "browser"


async def test_query_unhandled_exception_logs_then_propagates(rows, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(m.query_service, "process_query", _boom)

    with pytest.raises(RuntimeError):
        await query_endpoint(QueryRequest(query="x"), _mock_request(), user=None)

    assert [r["response_status"] for r in rows] == ["server_error"]


async def test_query_success_logs_one_row_with_channel(rows, monkeypatch):
    async def _ok(*_a, **_k):
        return _data_response()

    monkeypatch.setattr(m.query_service, "process_query", _ok)

    result = await query_endpoint(
        QueryRequest(query="US GDP"),
        _mock_request({"x-psm": "stone.coze.plugin", "user-agent": "Go-http-client/2.0"}),
        user=None,
    )

    assert isinstance(result, QueryResponse)
    assert len(rows) == 1
    assert rows[0]["response_status"] == "data"
    assert rows[0]["channel"] == "coze"
    assert rows[0]["processing_time_ms"] == result.processingTimeMs
    # Coze branding still keyed on the same (shared) structural detector.
    assert "data.openecon.ai" in (result.message or "")


async def test_query_emits_one_timing_line_per_exit(rows, monkeypatch, caplog):
    async def _slow(*_a, **_k):
        await asyncio.sleep(0.2)

    monkeypatch.setattr(m.settings, "query_timeout_seconds", 0.01)
    monkeypatch.setattr(m.query_service, "process_query", _slow)
    caplog.set_level("INFO", logger="backend.utils.processing_steps")

    await query_endpoint(QueryRequest(query="x"), _mock_request(), user=None)

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("query_timing ")]
    assert len(lines) == 1
    payload = json.loads(lines[0].split(" ", 1)[1])
    assert payload["endpoint"] == "/api/query"
    assert payload["outcome"] == "timeout"
    assert payload["total_ms"] is not None
    assert "llm" in payload and "step_ms" in payload


# ---------------------------------------------------------- /api/query/stream


@pytest.fixture
def stream_env(monkeypatch, rows):
    class _FakeSupabase:
        async def record_anonymous_query(self, *a, **k):
            return 1

    monkeypatch.setattr(m, "get_supabase_service", lambda: _FakeSupabase())
    monkeypatch.setattr(m.settings, "anon_identity_mode", "legacy")
    return rows


async def _post_stream(body, headers=None):
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/query/stream", json=body, headers=headers or {})
        return resp, resp.text


async def test_stream_success_sets_processing_time_before_logging(stream_env, monkeypatch):
    async def _ok(*_a, **_k):
        return _data_response("c-stream")

    monkeypatch.setattr(m.query_service, "process_query", _ok)

    resp, text = await _post_stream({"query": "US GDP", "sessionId": "s-st"},
                                    headers={"X-OpenEcon-Internal": "live_battery"})

    assert resp.status_code == 200
    assert len(stream_env) == 1
    row = stream_env[0]
    assert row["response_status"] == "data"
    assert row["processing_time_ms"] is not None  # was NULL on the stream path
    assert row["channel"] == "internal"
    assert row["client_name"] == "live_battery"
    data_events = [ln for ln in text.splitlines() if ln.startswith("data: ") and "processingTimeMs" in ln]
    assert data_events, "stream data event should carry processingTimeMs"


async def test_stream_query_failure_logs_one_server_error_row(stream_env, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(m.query_service, "process_query", _boom)

    resp, text = await _post_stream({"query": "x", "sessionId": "s-f"})

    assert "event: error" in text
    assert [r["response_status"] for r in stream_env] == ["server_error"]


async def test_stream_timeout_logs_one_timeout_row_and_stops(stream_env, monkeypatch):
    monkeypatch.setattr(m, "_SSE_STREAM_TIMEOUT_SECONDS", 0.05)
    cancelled = asyncio.Event()

    async def _slow(*_a, **_k):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return _data_response()

    monkeypatch.setattr(m.query_service, "process_query", _slow)

    resp, text = await asyncio.wait_for(_post_stream({"query": "x", "sessionId": "s-t"}), timeout=3)

    assert "timed out" in text
    assert text.rstrip().endswith("}")  # nothing streamed after the done event
    assert [r["response_status"] for r in stream_env] == ["timeout"]
    assert stream_env[0]["processing_time_ms"] is not None
    assert cancelled.is_set(), "timed-out stream must cancel the running query task"


# -------------------------------------------------------------- /api/query/pro


def _user() -> User:
    return User(id="u-1", email="u@example.com", passwordHash="x", name="U",
                createdAt=datetime.now(timezone.utc))


async def test_pro_timeout_logs_one_timeout_row(rows, monkeypatch):
    async def _slow_inner(*_a, **_k):
        await asyncio.sleep(0.2)

    monkeypatch.setattr(m.settings, "pro_query_timeout_seconds", 0.01)
    monkeypatch.setattr(m, "_pro_query_inner", _slow_inner)

    resp = await query_pro_endpoint(
        QueryRequest(query="regress x on y"),
        raw_request=_mock_request({"user-agent": "Mozilla/5.0"}),
        user=_user(),
    )

    assert resp.error == "request_timeout"
    assert len(rows) == 1
    assert rows[0]["response_status"] == "timeout"
    assert rows[0]["pro_mode"] is True
    assert rows[0]["channel"] == "browser"


async def test_pro_internal_failure_logs_one_server_error_row(rows, monkeypatch):
    def _broken_grok():
        raise RuntimeError("grok unavailable")

    import backend.services.grok as grok

    monkeypatch.setattr(grok, "get_grok_service", _broken_grok)

    resp = await query_pro_endpoint(QueryRequest(query="x"), raw_request=_mock_request(), user=_user())

    assert resp.error == "pro_mode_error"
    assert [r["response_status"] for r in rows] == ["server_error"]
    assert rows[0]["pro_mode"] is True


# ------------------------------------------------------- supabase payload shape


async def test_log_query_puts_channel_inside_intent_json():
    from backend.services.supabase_service import SupabaseService

    captured = {}

    class _Client:
        async def insert(self, table, data, timeout=None):
            captured.update(table=table, data=data)
            return {"ok": True}

    svc = SupabaseService.__new__(SupabaseService)
    svc.client = _Client()

    await svc.log_query(query="q", session_id="s", intent={"apiProvider": "FRED"},
                        response_status="timeout", channel="mcp", client_name="claude-ai")
    assert captured["table"] == "user_queries"
    assert captured["data"]["intent"] == {
        "apiProvider": "FRED",
        "_request_meta": {"channel": "mcp", "client_name": "claude-ai"},
    }
    # No new top-level column (no schema migration).
    assert "channel" not in captured["data"] and "client_name" not in captured["data"]

    captured.clear()
    await svc.log_query(query="q", session_id="s", channel="browser")
    assert captured["data"]["intent"] == {"_request_meta": {"channel": "browser"}}

    captured.clear()
    await svc.log_query(query="q", session_id="s")
    assert "intent" not in captured["data"]  # unchanged when no channel known
