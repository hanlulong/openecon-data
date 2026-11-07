"""Invalid contact emails must not cost the owner the notification (FB-3).

A 2026-11-06 probe submitted an email containing CRLF. It was passed straight
into Resend's reply_to, Resend rejected the WHOLE message ("422 - Invalid
reply_to field"), so the owner was never notified while the client got 200.

Mechanism under test: contact addresses are structurally normalized — only a
single plain addr-spec becomes reply_to / mailto; anything else is kept as
escaped "unvalidated contact" text and the submission still succeeds; a
Resend 422 with reply_to set is retried exactly once without it; delivery
failures are logged at ERROR with the feedback id.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

import backend.services.feedback as fb
from backend.models import FeedbackRequest, User
from backend.services.feedback import FeedbackService, normalize_contact_email

INVALID = [
    "user@example.com\r\nBcc: victim@example.org",
    "user@exa mple.com",
    "user@example.com other@example.org",
    "user@example.com,other@example.org",
    "user@example.com; other@example.org",
    "Display Name <user@example.com>",
    "<user@example.com>",
    "no-at-sign",
    "@example.com",
    "user@",
    "user@@example.com",
    "user\x00@example.com",
    "user\u200b@example.com",  # zero-width space (format char)
]


class _Resp:
    def __init__(self, status_code, text="{}"):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "RESEND_API_KEY", "test-key")
    return FeedbackService(feedback_dir=tmp_path)


@pytest.fixture
def resend(monkeypatch):
    """Record every Resend payload; scripted status codes (default 200)."""
    calls: list = []
    statuses: list = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(dict(json))
        return _Resp(statuses.pop(0) if statuses else 200)

    monkeypatch.setattr(fb.httpx, "post", fake_post)
    return calls, statuses


@pytest.mark.parametrize("raw", INVALID)
def test_invalid_contact_is_kept_unvalidated_and_never_reply_to(svc, resend, raw):
    calls, _ = resend
    resp = svc.submit_feedback(FeedbackRequest(type="bug", message="hi", email=raw))
    assert resp.success is True  # never reject the user over the address

    record = svc.get_feedback_by_id(resp.feedbackId)
    assert record["email"] is None
    assert record["emailUnvalidated"] == raw

    assert len(calls) == 1
    assert "reply_to" not in calls[0]
    html_body = calls[0]["html"]
    assert "mailto:" not in html_body
    # The raw value is visible to the owner, with control chars made explicit
    # (it cannot add/forge lines in the plain-text body).
    assert fb.UNVALIDATED_CONTACT_LABEL in calls[0]["text"]
    if any(c in raw for c in "\r\n"):
        assert raw not in calls[0]["text"]


def test_valid_contact_is_stripped_used_as_reply_to_and_quoted_mailto(svc, resend):
    calls, _ = resend
    resp = svc.submit_feedback(FeedbackRequest(type="bug", message="hi", email="  a+b@example.com "))
    record = svc.get_feedback_by_id(resp.feedbackId)
    assert record["email"] == "a+b@example.com"
    assert record["emailUnvalidated"] is None
    assert calls[0]["reply_to"] == "a+b@example.com"
    assert 'href="mailto:a%2Bb@example.com"' in calls[0]["html"]


def test_verified_account_email_is_reply_to_fallback(svc, resend):
    calls, _ = resend
    user = User(id="u1", email="acct@example.com", passwordHash="", name="N",
                createdAt=datetime.now(timezone.utc))
    svc.submit_feedback(FeedbackRequest(type="bug", message="hi", email="bad\r\n@x"), user=user)
    assert calls[0]["reply_to"] == "acct@example.com"


def test_resend_422_with_reply_to_retries_exactly_once_without_it(svc, resend):
    calls, statuses = resend
    statuses.extend([422, 200])
    entry = {"id": "fid-1", "type": "bug", "message": "m", "email": "user@example.com",
             "timestamp": "t"}
    assert svc._send_via_resend(entry) is True
    assert len(calls) == 2
    assert calls[0]["reply_to"] == "user@example.com"
    assert "reply_to" not in calls[1]


def test_resend_422_twice_logs_error_with_feedback_id(svc, resend, caplog):
    calls, statuses = resend
    statuses.extend([422, 422])
    entry = {"id": "fid-2", "type": "bug", "message": "m", "email": "user@example.com",
             "timestamp": "t"}
    with caplog.at_level(logging.ERROR, logger="openecon.feedback"):
        assert svc._send_via_resend(entry) is False
    assert len(calls) == 2  # exactly one retry, no loop
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("fid-2" in m and "NOT delivered" in m for m in errors)


@pytest.mark.parametrize("status,email", [(422, None), (500, "user@example.com")])
def test_no_retry_when_reply_to_cannot_be_the_cause(svc, resend, status, email):
    calls, statuses = resend
    statuses.append(status)
    entry = {"id": "fid-3", "type": "bug", "message": "m", "email": email, "timestamp": "t"}
    assert svc._send_via_resend(entry) is False
    assert len(calls) == 1


@pytest.mark.parametrize("raw", INVALID)
def test_normalizer_rejects_structurally_invalid(raw):
    assert normalize_contact_email(raw) == (None, raw)


@pytest.mark.parametrize("raw,expected", [
    ("user@example.com", "user@example.com"),
    ("  first.last+tag@sub.example.co.uk\t", "first.last+tag@sub.example.co.uk"),
    ("user@example.com\r\n", "user@example.com"),  # edge whitespace is stripped, not fatal
])
def test_normalizer_accepts_and_strips_plain_addresses(raw, expected):
    assert normalize_contact_email(raw) == (expected, None)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_normalizer_treats_blank_as_absent(raw):
    assert normalize_contact_email(raw) == (None, None)
