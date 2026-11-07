"""Feedback payloads are bounded (FB-4).

FeedbackSessionInfo, FeedbackConversation.messages and rawMessages had no
limits, and nothing bounded the /api/feedback body (Apache's LimitRequestBody
is unlimited on this host), so one anonymous request could persist and email
an arbitrarily large document.

Mechanism under test:
  * auto-collected context is TRUNCATED server-side (never a 422 that would
    lose the user's report), with the most recent transcript turns kept;
  * RequestBodyLimitMiddleware refuses oversized bodies with 413 both from a
    declared Content-Length and from the counted stream (chunked / lying
    Content-Length), replays accepted bodies intact, and leaves other paths
    and methods untouched.
"""
from __future__ import annotations

import json

import httpx
import pytest

from backend.models import (
    FEEDBACK_MAX_CONTEXT_FIELD_CHARS,
    FEEDBACK_MAX_MESSAGES_CHARS,
    FEEDBACK_MAX_RAW_MESSAGE_CHARS,
    FEEDBACK_MAX_RAW_MESSAGES,
    FEEDBACK_TRUNCATION_MARKER,
    FeedbackRequest,
)
from backend.services.feedback import FeedbackService
from backend.utils.request_body_limit import RequestBodyLimitMiddleware

SESSION_FIELDS = ("url", "userAgent", "timestamp", "screenSize", "language", "timezone", "referrer")


def _session(value="x"):
    return {f: value for f in SESSION_FIELDS}


def _raw(i, content="hello"):
    return {"role": "user", "content": content, "timestamp": f"t{i}", "hasData": False, "dataCount": 0}


def test_oversized_context_fields_are_truncated_not_rejected():
    req = FeedbackRequest(
        type="bug",
        message="short report",
        sessionInfo=_session("u" * 10_000),
        conversation={
            "messages": "OLD" + "m" * 200_000 + "NEWEST",
            "messageCount": 250,
            "conversationId": "c" * 10_000,
            "rawMessages": [_raw(i, "z" * 20_000) for i in range(250)],
        },
    )
    for field in SESSION_FIELDS:
        value = getattr(req.sessionInfo, field)
        assert len(value) == FEEDBACK_MAX_CONTEXT_FIELD_CHARS
        assert value.endswith(FEEDBACK_TRUNCATION_MARKER)

    conv = req.conversation
    assert len(conv.messages) == FEEDBACK_MAX_MESSAGES_CHARS
    assert conv.messages.startswith(FEEDBACK_TRUNCATION_MARKER)
    assert conv.messages.endswith("NEWEST")  # the most recent turns survive
    assert len(conv.conversationId) == FEEDBACK_MAX_CONTEXT_FIELD_CHARS
    assert conv.messageCount == 250  # the true count is still reported

    assert len(conv.rawMessages) == FEEDBACK_MAX_RAW_MESSAGES
    assert conv.rawMessages[-1].timestamp == "t249"  # most recent kept
    assert conv.rawMessages[0].timestamp == f"t{250 - FEEDBACK_MAX_RAW_MESSAGES}"
    assert all(len(m.content) == FEEDBACK_MAX_RAW_MESSAGE_CHARS for m in conv.rawMessages)


def test_normal_submission_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(FeedbackService, "_send_email_notification", lambda self, fb: False)
    svc = FeedbackService(feedback_dir=tmp_path)
    conversation = {
        "messages": "[User]: GDP?\n\n---\n\n[Assistant]: Here it is",
        "messageCount": 2,
        "conversationId": "conv-1",
        "rawMessages": [_raw(0, "GDP?"), _raw(1, "Here it is")],
    }
    req = FeedbackRequest(type="bug", message="chart broke", sessionInfo=_session("v"),
                          conversation=conversation)
    record = svc.get_feedback_by_id(svc.submit_feedback(req).feedbackId)
    assert record["sessionInfo"] == _session("v")
    assert record["conversation"]["messages"] == conversation["messages"]
    assert [m["content"] for m in record["conversation"]["rawMessages"]] == ["GDP?", "Here it is"]
    assert FEEDBACK_TRUNCATION_MARKER not in json.dumps(record)


# --- ASGI body guard ----------------------------------------------------------

class _EchoApp:
    """Records the body it receives and answers 200."""

    def __init__(self):
        self.calls = 0
        self.body = None

    async def __call__(self, scope, receive, send):
        self.calls += 1
        chunks = []
        while True:
            frame = await receive()
            chunks.append(frame.get("body", b""))
            if not frame.get("more_body", False):
                break
        self.body = b"".join(chunks)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _drive(app, *, path="/api/feedback", method="POST", chunks=(b"",), content_length=None):
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    frames = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]

    async def receive():
        return frames.pop(0) if frames else {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_declared_content_length_over_limit_is_413_without_reading():
    inner = _EchoApp()
    guard = RequestBodyLimitMiddleware(inner, limits={"/api/feedback": 100})
    assert await _drive(guard, chunks=(b"x" * 10,), content_length=10_000) == 413
    assert inner.calls == 0


async def test_streamed_body_over_limit_is_413_even_without_content_length():
    inner = _EchoApp()
    guard = RequestBodyLimitMiddleware(inner, limits={"/api/feedback": 100})
    assert await _drive(guard, chunks=(b"x" * 60, b"x" * 60)) == 413
    # A lying (small) Content-Length is not trusted either.
    assert await _drive(guard, chunks=(b"x" * 60, b"x" * 60), content_length=10) == 413
    assert inner.calls == 0


async def test_body_within_limit_is_replayed_intact():
    inner = _EchoApp()
    guard = RequestBodyLimitMiddleware(inner, limits={"/api/feedback": 100})
    assert await _drive(guard, chunks=(b"a" * 50, b"b" * 50), content_length=100) == 200
    assert inner.body == b"a" * 50 + b"b" * 50


@pytest.mark.parametrize("path,method", [("/api/query", "POST"), ("/api/feedback", "GET"),
                                         ("/api/feedbackx", "POST")])
async def test_other_paths_and_methods_are_untouched(path, method):
    inner = _EchoApp()
    guard = RequestBodyLimitMiddleware(inner, limits={"/api/feedback": 100})
    assert await _drive(guard, path=path, method=method, chunks=(b"x" * 500,)) == 200
    assert inner.body == b"x" * 500


async def test_feedback_endpoint_rejects_body_over_2mb_and_accepts_normal(tmp_path, monkeypatch):
    import backend.main as m

    monkeypatch.setattr(FeedbackService, "_send_email_notification", lambda self, fb: False)
    store = FeedbackService(feedback_dir=tmp_path)
    monkeypatch.setattr(m, "feedback_service", store)

    names = [mw.cls.__name__ for mw in m.app.user_middleware]
    assert names.index("CORSMiddleware") < names.index("RequestBodyLimitMiddleware")

    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        huge = {"type": "bug", "message": "m",
                "conversation": {"messages": "x" * (2 * 1024 * 1024 + 1), "messageCount": 1}}
        resp = await client.post("/api/feedback", json=huge)
        assert resp.status_code == 413
        ok = await client.post("/api/feedback", json={"type": "feature", "message": "nice"})
        assert ok.status_code == 200, ok.text
    assert len(store.get_all_feedback()) == 1  # the oversized one was never stored
