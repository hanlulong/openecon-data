"""T8 — local-provider elapsed-time budget + circuit breaker in parse_query.

A wedged local LLM tunnel (accepts TCP, never responds) used to make every
parse attempt hang to the full llm_timeout across two retrying stages, so a
single dead tunnel turned every query into a 504 despite a healthy hosted
fallback. parse_query now caps total time spent on the LOCAL provider and trips
a process-wide circuit so subsequent requests skip local while it is down.

All providers are mocked; there are zero real LLM calls.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import ParsedIntent
from backend.services import openrouter as openrouter_mod
from backend.services.openrouter import OpenRouterService


def _intent(marker_provider: str) -> ParsedIntent:
    return ParsedIntent.model_validate(
        {
            "apiProvider": marker_provider,
            "indicators": ["GDP"],
            "parameters": {"country": "US"},
            "clarificationNeeded": False,
        }
    )


def _make_service(provider: str = "vllm", *, instructor: bool = True, health: bool = True) -> OpenRouterService:
    service = OpenRouterService.__new__(OpenRouterService)
    service.api_key = "test-key"
    service.settings = SimpleNamespace(
        llm_provider=provider,
        llm_fallback_model="openai/gpt-oss-120b",
    )
    service.instructor_client = object() if instructor else None
    service.instructor_model = "local-model"
    provider_mock = SimpleNamespace(health_check=AsyncMock(return_value=health))
    service.llm_provider = provider_mock
    return service


@pytest.fixture(autouse=True)
def _reset_circuit_and_shrink_budget(monkeypatch):
    """Hermetic circuit state + a tiny budget so 'wedged tunnel' tests are fast."""
    openrouter_mod._reset_local_circuit()
    monkeypatch.setattr(openrouter_mod, "_LOCAL_ATTEMPT_BUDGET_S", 0.2)
    monkeypatch.setattr(openrouter_mod, "_CIRCUIT_OPEN_SECONDS", 60.0)
    yield
    openrouter_mod._reset_local_circuit()


@pytest.mark.asyncio
async def test_wedged_local_falls_back_to_hosted_within_budget_and_trips_circuit():
    service = _make_service("vllm")

    async def _hang(*args, **kwargs):
        await asyncio.sleep(5)  # simulate a tunnel that never responds

    service._parse_with_instructor = AsyncMock(side_effect=_hang)
    service._parse_with_provider = AsyncMock(side_effect=_hang)
    service._parse_direct = AsyncMock(return_value=_intent("HOSTED"))

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    intent = await service.parse_query("US GDP")
    elapsed = loop.time() - t0

    # Hosted fallback served the query...
    assert intent.apiProvider == "HOSTED"
    service._parse_direct.assert_awaited_once()
    # ...well within the request deadline (budget 0.2s, not 120s+).
    assert elapsed < 3.0, f"cascade took {elapsed:.2f}s — budget not enforced"
    # ...and the circuit is now open so the next request skips local.
    assert openrouter_mod._local_circuit_is_open()


@pytest.mark.asyncio
async def test_healthy_local_uses_instructor_unchanged():
    service = _make_service("vllm")
    good = _intent("LOCAL")
    service._parse_with_instructor = AsyncMock(return_value=good)
    service._parse_with_provider = AsyncMock()
    service._parse_direct = AsyncMock()

    intent = await service.parse_query("US GDP")

    assert intent is good
    service._parse_with_instructor.assert_awaited_once()
    service._parse_with_provider.assert_not_awaited()
    service._parse_direct.assert_not_awaited()
    assert not openrouter_mod._local_circuit_is_open()


@pytest.mark.asyncio
async def test_open_circuit_with_failed_health_skips_local_entirely():
    service = _make_service("vllm", health=False)
    openrouter_mod._trip_local_circuit()

    service._parse_with_instructor = AsyncMock(return_value=_intent("LOCAL"))
    service._parse_with_provider = AsyncMock(return_value=_intent("LOCAL"))
    service._parse_direct = AsyncMock(return_value=_intent("HOSTED"))

    intent = await service.parse_query("US GDP")

    assert intent.apiProvider == "HOSTED"
    # Cheap 5s health probe consulted; local stages skipped completely.
    service.llm_provider.health_check.assert_awaited_once()
    service._parse_with_instructor.assert_not_awaited()
    service._parse_with_provider.assert_not_awaited()
    service._parse_direct.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_circuit_with_recovered_health_resumes_local():
    service = _make_service("vllm", health=True)
    openrouter_mod._trip_local_circuit()
    good = _intent("LOCAL")
    service._parse_with_instructor = AsyncMock(return_value=good)
    service._parse_direct = AsyncMock()

    intent = await service.parse_query("US GDP")

    assert intent is good
    service.llm_provider.health_check.assert_awaited_once()
    service._parse_with_instructor.assert_awaited_once()
    service._parse_direct.assert_not_awaited()
    # Circuit closed again after confirmed recovery.
    assert not openrouter_mod._local_circuit_is_open()


@pytest.mark.asyncio
async def test_openrouter_provider_ignores_circuit_and_budget():
    """Non-local config must behave exactly as before: no health probe, no
    circuit consultation, even if a stale circuit flag is set."""
    service = _make_service("openrouter", health=True)
    openrouter_mod._trip_local_circuit()  # should be irrelevant for openrouter
    good = _intent("OR")
    service._parse_with_instructor = AsyncMock(return_value=good)
    service._parse_direct = AsyncMock()

    intent = await service.parse_query("US GDP")

    assert intent is good
    service.llm_provider.health_check.assert_not_awaited()
    service._parse_with_instructor.assert_awaited_once()


@pytest.mark.asyncio
async def test_instructor_non_timeout_error_still_tries_provider_fallback():
    """A local instructor FAILURE that is not a timeout must not trip the
    circuit and must still fall through to the manual provider stage."""
    service = _make_service("vllm")
    service._parse_with_instructor = AsyncMock(side_effect=ValueError("bad json schema"))
    good = _intent("LOCAL_FALLBACK")
    service._parse_with_provider = AsyncMock(return_value=good)
    service._parse_direct = AsyncMock()

    intent = await service.parse_query("US GDP")

    assert intent is good
    service._parse_with_provider.assert_awaited_once()
    service._parse_direct.assert_not_awaited()
    assert not openrouter_mod._local_circuit_is_open()
