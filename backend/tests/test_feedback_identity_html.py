"""Feedback identity provenance + escape-by-default owner email (FB-2).

/api/feedback is anonymous-capable, so body userId/userName are client CLAIMS
(a 2026-11-06 probe submitted userId='victim-user-id-12345'). The owner's
notification email rendered those claims as plain identity and interpolated
userName/email/userId into the HTML unescaped.

Mechanism under test:
  * verified identity comes ONLY from the bearer-token user; when one exists
    the client claims are dropped, otherwise they are labelled
    "claimed (unverified, client-supplied)";
  * every value reaching the HTML body is html-escaped — checked generically
    by filling EVERY string field of the request model (recursively, so new
    fields are covered automatically) with markup and asserting none survives.
"""
from __future__ import annotations

import typing
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import BaseModel

from backend.models import FeedbackRequest, User
from backend.services.feedback import CLAIMED_LABEL, FeedbackService

FORGED_ID = "victim-user-id-12345"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(FeedbackService, "_send_email_notification", lambda self, fb: False)
    return FeedbackService(feedback_dir=tmp_path)


def _user(name="Real Name", uid="11111111-2222-3333-4444-555555555555") -> User:
    return User(
        id=uid, email="real@example.com", passwordHash="", name=name,
        createdAt=datetime.now(timezone.utc),
    )


def test_forged_identity_on_anonymous_request_is_labelled_claimed(svc):
    resp = svc.submit_feedback(
        FeedbackRequest(type="bug", message="hi", userId=FORGED_ID, userName="Victim")
    )
    record = svc.get_feedback_by_id(resp.feedbackId)
    assert record["identityVerified"] is False
    assert record["userId"] is None and record["userName"] is None
    assert record["claimedUserId"] == FORGED_ID
    assert record["claimedUserName"] == "Victim"

    html_body = svc._format_feedback_html(record)
    text_body = svc._format_feedback_text(record)
    for body in (html_body, text_body):
        assert "(verified)" not in body
        assert CLAIMED_LABEL in body
        assert FORGED_ID in body  # still visible to the owner, but as a claim
    claimed_line = next(line for line in text_body.splitlines() if FORGED_ID in line)
    assert CLAIMED_LABEL in claimed_line


def test_verified_user_drops_client_claims(svc):
    user = _user()
    resp = svc.submit_feedback(
        FeedbackRequest(type="bug", message="hi", userId=FORGED_ID, userName="Victim"),
        user=user,
    )
    record = svc.get_feedback_by_id(resp.feedbackId)
    assert record["identityVerified"] is True
    assert record["userId"] == user.id and record["userName"] == user.name
    assert record["verifiedEmail"] == user.email
    assert record["claimedUserId"] is None and record["claimedUserName"] is None

    html_body = svc._format_feedback_html(record)
    assert FORGED_ID not in html_body and "Victim" not in html_body
    assert "User ID (verified)" in html_body and user.id in html_body
    assert CLAIMED_LABEL not in html_body


# --- generic fuzz: no request field can inject markup -----------------------

def _payload(tag: str) -> str:
    return f'<zq{tag} onx="1">"\'&</zq{tag}>'


def _fill(model_cls: type[BaseModel], tag: str) -> dict:
    """Build kwargs for model_cls with markup in EVERY string field."""
    values = {}
    for name, field in model_cls.model_fields.items():
        if any(getattr(m, "pattern", None) for m in field.metadata):
            continue  # enum-like (type=bug|feature|other); default/valid below
        values[name] = _fill_annotation(field.annotation, f"{tag}{name}")
    return values


def _fill_annotation(annotation, tag):
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin is typing.Union or (origin is not None and str(origin) == "<class 'types.UnionType'>"):
        return _fill_annotation(args[0], tag)
    if origin in (list, typing.List):
        return [_fill_annotation(args[0], f"{tag}i{i}") for i in range(2)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation(**_fill(annotation, tag))
    if annotation is str:
        return _payload(tag)
    if annotation is bool:
        return True
    if annotation is int:
        return 3
    raise AssertionError(f"unhandled annotation {annotation!r}; extend the fuzzer")


@pytest.mark.parametrize("verified", [False, True])
def test_no_raw_markup_from_any_request_field_reaches_html(svc, verified):
    request = FeedbackRequest(type="bug", **_fill(FeedbackRequest, "r"))
    user = _user(name=_payload("username")) if verified else None
    resp = svc.submit_feedback(request, user=user)
    html_body = svc._format_feedback_html(svc.get_feedback_by_id(resp.feedbackId))

    assert "<zq" not in html_body
    assert "</zq" not in html_body
    assert 'onx="1"' not in html_body  # no attribute breakout either
    # Values are still present, just escaped.
    assert "&lt;zq" in html_body


# --- endpoint wiring --------------------------------------------------------

async def _post_feedback(m, body, headers=None):
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/feedback", json=body, headers=headers or {})


async def test_endpoint_uses_bearer_identity_not_body_claims(tmp_path, monkeypatch):
    import backend.main as m

    store = FeedbackService(feedback_dir=tmp_path)
    monkeypatch.setattr(FeedbackService, "_send_email_notification", lambda self, fb: False)
    monkeypatch.setattr(m, "feedback_service", store)
    user = _user()

    body = {"type": "bug", "message": "hi", "userId": FORGED_ID, "userName": "Victim"}
    anon = await _post_feedback(m, body)
    assert anon.status_code == 200, anon.text
    anon_record = store.get_feedback_by_id(anon.json()["feedbackId"])
    assert anon_record["identityVerified"] is False
    assert anon_record["claimedUserId"] == FORGED_ID and anon_record["userId"] is None

    async def _fake_optional_user():
        return user

    m.app.dependency_overrides[m.get_optional_user] = _fake_optional_user
    try:
        authed = await _post_feedback(m, body, headers={"Authorization": "Bearer t"})
    finally:
        m.app.dependency_overrides.pop(m.get_optional_user, None)
    assert authed.status_code == 200, authed.text
    authed_record = store.get_feedback_by_id(authed.json()["feedbackId"])
    assert authed_record["identityVerified"] is True
    assert authed_record["userId"] == user.id
    assert authed_record["claimedUserId"] is None


@pytest.mark.parametrize("field,limit", [("userId", 100), ("userName", 200)])
def test_claimed_identity_fields_are_bounded(field, limit):
    FeedbackRequest(type="bug", **{field: "x" * limit})
    with pytest.raises(Exception):
        FeedbackRequest(type="bug", **{field: "x" * (limit + 1)})
