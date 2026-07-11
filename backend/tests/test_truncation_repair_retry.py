"""T5 — truncation auto-repair must feed the retry loop, not silently ship a
wrong-but-valid intent.

fix_truncated_json can close an unterminated string ("United King) into a valid
but SHORTER value, which parses into a structurally-valid ParsedIntent carrying
a corrupted field. The parse layer now (a) reports when a repair was applied via
parse_json_response_tracked, and (b) treats a repaired parse as a soft failure
that retries the LLM, accepting the repaired object only once retries are
exhausted. It also raises the manual-fallback max_tokens 500 -> 1500 so the
reasoning model stops truncating in the first place.

All LLM calls are mocked; zero real network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.services import openrouter as openrouter_mod
from backend.services.json_parser import (
    parse_json_response,
    parse_json_response_tracked,
)
from backend.services.openrouter import OpenRouterService


@pytest.fixture(autouse=True)
def _reset_circuit():
    openrouter_mod._reset_local_circuit()
    yield
    openrouter_mod._reset_local_circuit()


# --- Unit: the tracking flag -------------------------------------------------

def test_valid_json_reports_no_repair():
    parsed, repaired = parse_json_response_tracked('{"apiProvider": "FRED"}')
    assert parsed == {"apiProvider": "FRED"}
    assert repaired is False


def test_json_extracted_from_prose_reports_no_repair():
    parsed, repaired = parse_json_response_tracked(
        'Here is the intent:\n{"apiProvider": "FRED"}\nHope that helps!'
    )
    assert parsed["apiProvider"] == "FRED"
    assert repaired is False


def test_truncated_json_reports_repair_and_corrupts_value():
    truncated = '{"apiProvider": "FRED", "indicators": ["GDP"], "parameters": {"country": "United King'
    parsed, repaired = parse_json_response_tracked(truncated)
    assert repaired is True
    # The repair closed the string mid-token — the value is now WRONG.
    assert parsed["parameters"]["country"] == "United King"


def test_backward_compatible_wrapper_returns_only_dict():
    result = parse_json_response('{"apiProvider": "FRED"}')
    assert result == {"apiProvider": "FRED"}


# --- Integration: retry behaviour in _parse_with_provider --------------------

def _make_service() -> OpenRouterService:
    service = OpenRouterService.__new__(OpenRouterService)
    service.api_key = "test-key"
    service.settings = SimpleNamespace(llm_provider="vllm", llm_fallback_model="openai/gpt-oss-120b")
    service.instructor_client = None
    service.llm_provider = SimpleNamespace(generate=AsyncMock())
    return service


def _gen_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# clarificationNeeded/apiProvider/indicators appear BEFORE the truncation point
# so the repaired object still passes ParsedIntent validation — the corruption
# is confined to the country value that was cut mid-token.
_TRUNCATED = (
    '{"apiProvider": "FRED", "clarificationNeeded": false, "indicators": ["GDP"], '
    '"parameters": {"country": "United King'
)
_COMPLETE = json.dumps(
    {
        "apiProvider": "FRED",
        "clarificationNeeded": False,
        "indicators": ["GDP"],
        "parameters": {"country": "United Kingdom"},
    }
)


@pytest.mark.asyncio
async def test_repaired_parse_retries_and_prefers_complete_response():
    service = _make_service()
    service.llm_provider.generate.side_effect = [
        _gen_response(_TRUNCATED),   # attempt 1: truncation-repaired -> soft fail, retry
        _gen_response(_COMPLETE),    # attempt 2: complete -> accepted
    ]
    with patch.object(service, "_system_prompt", return_value="sys"):
        intent = await service.parse_query("UK GDP")

    assert intent.parameters["country"] == "United Kingdom"
    assert service.llm_provider.generate.await_count == 2


@pytest.mark.asyncio
async def test_repaired_parse_accepted_only_after_retries_exhausted():
    service = _make_service()
    # Every attempt truncates; the repaired object is accepted on the last try.
    service.llm_provider.generate.side_effect = [
        _gen_response(_TRUNCATED),
        _gen_response(_TRUNCATED),
        _gen_response(_TRUNCATED),
    ]
    with patch.object(service, "_system_prompt", return_value="sys"):
        intent = await service.parse_query("UK GDP")

    assert intent.parameters["country"] == "United King"  # corrupted, last-resort
    assert service.llm_provider.generate.await_count == 3  # exhausted all retries


@pytest.mark.asyncio
async def test_manual_fallback_requests_1500_max_tokens():
    service = _make_service()
    service.llm_provider.generate.side_effect = [_gen_response(_COMPLETE)]
    with patch.object(service, "_system_prompt", return_value="sys"):
        await service.parse_query("UK GDP")

    kwargs = service.llm_provider.generate.await_args.kwargs
    assert kwargs["max_tokens"] == 1500
