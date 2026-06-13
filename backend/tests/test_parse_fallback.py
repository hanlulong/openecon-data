"""Parse-path LLM fallback ladder.

The availability contract: a runtime failure of the configured non-OpenRouter
provider (e.g. the local vLLM tunnel dropping mid-flight) must degrade COST,
not availability — parse_query falls through to a direct OpenRouter call with
settings.llm_fallback_model. When the configured provider IS OpenRouter there
is nothing different to try, so the error propagates.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.openrouter import OpenRouterService


def _make_service(provider: str) -> OpenRouterService:
    service = OpenRouterService.__new__(OpenRouterService)
    service.api_key = "test-key"
    service.settings = SimpleNamespace(
        llm_provider=provider,
        llm_fallback_model="openai/gpt-oss-120b",
    )
    service.instructor_client = None  # force the manual ladder
    service.llm_provider = object()  # truthy: provider initialized fine
    return service


VALID_INTENT_JSON = json.dumps(
    {
        "apiProvider": "FRED",
        "indicators": ["GDP"],
        "parameters": {"country": "US"},
        "clarificationNeeded": False,
    }
)


class _FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": VALID_INTENT_JSON}}]}


class _FakeAsyncClient:
    """Captures the request body sent to OpenRouter."""

    last_request_body = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.last_request_body = json
        return _FakeResponse()


@pytest.mark.asyncio
async def test_runtime_provider_failure_falls_through_to_openrouter():
    service = _make_service("vllm")
    with patch.object(
        service, "_parse_with_provider", new=AsyncMock(side_effect=RuntimeError("tunnel down"))
    ), patch("backend.services.openrouter.httpx.AsyncClient", _FakeAsyncClient):
        intent = await service.parse_query("US GDP")

    assert intent.apiProvider == "FRED"
    body = _FakeAsyncClient.last_request_body
    assert body is not None, "_parse_direct never called OpenRouter"
    assert body["model"] == "openai/gpt-oss-120b"
    # Reasoning models spend completion tokens on reasoning before the JSON
    # answer; the old 300-token cap would starve them.
    assert body["max_tokens"] >= 1500


@pytest.mark.asyncio
async def test_openrouter_provider_failure_propagates():
    service = _make_service("openrouter")
    _FakeAsyncClient.last_request_body = None
    with patch.object(
        service, "_parse_with_provider", new=AsyncMock(side_effect=RuntimeError("boom"))
    ), patch("backend.services.openrouter.httpx.AsyncClient", _FakeAsyncClient):
        with pytest.raises(RuntimeError, match="boom"):
            await service.parse_query("US GDP")

    assert _FakeAsyncClient.last_request_body is None, (
        "must not retry OpenRouter against itself"
    )


@pytest.mark.asyncio
async def test_provider_success_skips_direct_fallback():
    service = _make_service("vllm")
    _FakeAsyncClient.last_request_body = None
    from backend.models import ParsedIntent

    good = ParsedIntent.model_validate(json.loads(VALID_INTENT_JSON))
    with patch.object(
        service, "_parse_with_provider", new=AsyncMock(return_value=good)
    ), patch("backend.services.openrouter.httpx.AsyncClient", _FakeAsyncClient):
        intent = await service.parse_query("US GDP")

    assert intent is good
    assert _FakeAsyncClient.last_request_body is None
