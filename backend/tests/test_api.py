from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

# Disable side services during tests so import-time setup stays lightweight
os.environ["NODE_ENV"] = "test"
os.environ["DISABLE_MCP"] = "1"
os.environ["DISABLE_BACKGROUND_JOBS"] = "1"

from backend.main import (  # noqa: E402  # pylint: disable=C0413
    QueryRequest,
    RegisterRequest,
    LoginRequest,
    get_request_conversation_id,
    health,
    query_endpoint,
    register,
    login,
    me,
)
from backend.models import NormalizedData, QueryResponse
from backend.services.user_store import user_store
from backend.services.mock_auth import MockAuthService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        user_store.clear()
        # Reset the auth factory singleton before each test
        import backend.services.auth_factory as auth_factory
        auth_factory._auth_service = None

    def test_health_endpoint(self) -> None:
        result = asyncio.run(health())
        self.assertEqual(result.status, "ok")
        self.assertIn("openrouter", result.services)

    def test_query_endpoint(self) -> None:
        mock_response = QueryResponse(
            conversationId="123",
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

        with patch("backend.main.query_service.process_query", AsyncMock(return_value=mock_response)) as process_query:
            result = asyncio.run(
                query_endpoint(
                    QueryRequest(query="GDP", conversationId=None),
                    user=None,
                )
            )

        self.assertIsInstance(result, QueryResponse)
        self.assertEqual(result.conversationId, "123")
        self.assertFalse(result.clarificationNeeded)
        process_query.assert_awaited_once_with("GDP", None, auto_pro_mode=True)

    def test_query_endpoint_uses_anonymous_session_id_as_conversation(self) -> None:
        mock_response = QueryResponse(conversationId="session-123", clarificationNeeded=True)

        with patch("backend.main.query_service.process_query", AsyncMock(return_value=mock_response)) as process_query:
            result = asyncio.run(
                query_endpoint(
                    QueryRequest(query="1", conversationId=None, sessionId="session-123"),
                    user=None,
                )
            )

        self.assertEqual(result.conversationId, "session-123")
        process_query.assert_awaited_once_with("1", "session-123", auto_pro_mode=True)

    def test_query_endpoint_timeout_without_session_returns_json_504(self) -> None:
        async def slow_process_query(*_args, **_kwargs):
            await asyncio.sleep(0.05)

        with (
            patch("backend.main.settings.query_timeout_seconds", 0.001),
            patch("backend.main.query_service.process_query", AsyncMock(side_effect=slow_process_query)) as process_query,
        ):
            result = asyncio.run(
                query_endpoint(
                    QueryRequest(query="Canada GDP", conversationId=None, sessionId=None),
                    user=None,
                )
            )

        self.assertEqual(result.status_code, 504)
        body = json.loads(result.body)
        self.assertEqual(body["error"], "request_timeout")
        self.assertFalse(body["clarificationNeeded"])
        self.assertIsInstance(body["conversationId"], str)
        self.assertTrue(body["conversationId"])
        process_query.assert_awaited_once_with("Canada GDP", None, auto_pro_mode=True)

    def test_get_request_conversation_id_prefers_explicit_conversation(self) -> None:
        request = QueryRequest(query="GDP", conversationId="conv-1", sessionId="session-1")
        self.assertEqual(get_request_conversation_id(request, user=None), "conv-1")

    def test_get_request_conversation_id_uses_anonymous_session(self) -> None:
        request = QueryRequest(query="GDP", conversationId=None, sessionId="session-1")
        self.assertEqual(get_request_conversation_id(request, user=None), "session-1")

    def test_auth_flow(self) -> None:
        """Test auth flow using MockAuthService (simulates dev mode without Supabase)."""
        # Create a fresh MockAuthService for this test
        mock_auth = MockAuthService()

        async def run_auth_flow():
            # Register a new user
            register_payload = RegisterRequest(
                name="Test User",
                email="test_api_flow@example.com",
                password="Secret12345A"
            )
            register_response = await register(register_payload)
            self.assertTrue(register_response.success, f"Registration failed: {register_response.error}")
            token = register_response.token
            self.assertIsNotNone(token)

            # Login with the same user
            login_payload = LoginRequest(
                email=register_payload.email,
                password=register_payload.password
            )
            login_response = await login(login_payload)
            self.assertTrue(login_response.success, f"Login failed: {login_response.error}")
            self.assertIsNotNone(login_response.token)

            # Verify user info via /me endpoint
            user_model = await mock_auth.get_user_from_token(login_response.token or "")
            self.assertIsNotNone(user_model)
            me_response = await me(user=user_model)
            self.assertEqual(me_response.email, register_payload.email)

        # Patch the auth_factory singleton to use our mock
        with patch("backend.services.auth_factory._auth_service", mock_auth), \
             patch("backend.services.auth_factory.get_auth_service_singleton", return_value=mock_auth), \
             patch("backend.main.get_auth_service_singleton", return_value=mock_auth):
            asyncio.run(run_auth_flow())


if __name__ == "__main__":
    unittest.main()
