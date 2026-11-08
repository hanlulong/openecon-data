"""/api/debug/conversation-state answers only in dev mode, only when
ALLOW_TEST_USER is exactly "true", and only for the caller's own conversation.
Every refusal is the same 404, so the route cannot confirm that an ID exists."""

from datetime import datetime, timezone

import httpx
import pytest

import backend.main as m
from backend.config import Settings
from backend.models import User

NOT_FOUND = {"error": "Not found"}


def _user(user_id: str) -> User:
    return User(
        id=user_id,
        email=f"{user_id}@example.test",
        passwordHash="",
        name=user_id,
        createdAt=datetime.now(timezone.utc),
    )


@pytest.fixture
def as_user():
    def login(user_id: str) -> None:
        async def fake_required_user():
            return _user(user_id)

        m.app.dependency_overrides[m.get_required_user] = fake_required_user

    yield login
    m.app.dependency_overrides.pop(m.get_required_user, None)


async def _get(conversation_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            f"/api/debug/conversation-state/{conversation_id}",
            headers={"Authorization": "Bearer t"},
        )


@pytest.mark.parametrize("flag", [None, "false", "0", "", "yes"])
async def test_flag_other_than_true_hides_the_route(monkeypatch, as_user, flag):
    if flag is None:
        monkeypatch.delenv("ALLOW_TEST_USER", raising=False)
    else:
        monkeypatch.setenv("ALLOW_TEST_USER", flag)
    as_user("alice")
    own = m.conversation_manager.create_conversation(owner_key="user:alice")
    response = await _get(own)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_true_flag_outside_dev_mode_hides_the_route(monkeypatch, as_user):
    monkeypatch.setenv("ALLOW_TEST_USER", "true")
    monkeypatch.setattr(Settings, "dev_mode", property(lambda self: False))
    as_user("alice")
    own = m.conversation_manager.create_conversation(owner_key="user:alice")
    response = await _get(own)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_other_users_and_missing_conversations_look_identical(monkeypatch, as_user):
    monkeypatch.setenv("ALLOW_TEST_USER", "true")
    as_user("alice")
    bobs = m.conversation_manager.create_conversation(owner_key="user:bob")
    anonymous = m.conversation_manager.create_conversation(owner_key="anon:")
    unowned = m.conversation_manager.create_conversation()
    responses = [
        await _get(bobs),
        await _get(anonymous),
        await _get(unowned),
        await _get("00000000-0000-4000-8000-000000000000"),
    ]
    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert all(r.json() == NOT_FOUND for r in responses)


async def test_owner_sees_their_own_conversation(monkeypatch, as_user):
    monkeypatch.setenv("ALLOW_TEST_USER", "true")
    as_user("alice")
    own = m.conversation_manager.create_conversation(owner_key="user:alice")
    response = await _get(own)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is True
    assert body["conversation_id"] == own
