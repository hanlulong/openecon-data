"""T14 — Pro Mode empty/prose code generation must fail loudly, not silently.

_extract_code_from_markdown used to return text.strip() unconditionally. An
EMPTY extraction then parsed as an empty-but-valid AST and "executed
successfully" with no output (a false success), while PROSE with no code block
reached the executor as a misleading "Security violations: Syntax error".
It now raises a distinct CodeGenerationError with a self-sufficient message so
both Pro Mode endpoints surface a clean failure. Valid code — fenced or bare —
is unaffected.

All model calls are mocked; zero real network.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.services.grok import CodeGenerationError, GrokService


def _svc() -> GrokService:
    # Bypass __init__ (which reads settings/keys) — the extraction helpers are
    # pure and need no instance state.
    return GrokService.__new__(GrokService)


# --- extraction: happy paths (unchanged behaviour) ---------------------------

def test_fenced_python_block_extracts_code():
    svc = _svc()
    text = "Here you go:\n```python\nprint('hi')\nx = 1\n```\n"
    assert svc._extract_code_from_markdown(text) == "print('hi')\nx = 1"


def test_generic_fence_extracts_code():
    svc = _svc()
    text = "```\nprint('hi')\n```"
    assert svc._extract_code_from_markdown(text) == "print('hi')"


def test_bare_code_without_fences_is_accepted():
    svc = _svc()
    # The prompt asks for code only, so bare (unfenced) code is the common case.
    code = "import pandas as pd\ndf = pd.DataFrame({'a': [1, 2]})\nprint(df)"
    assert svc._extract_code_from_markdown(code) == code


# --- extraction: failure paths (new behaviour) -------------------------------

def test_empty_string_raises_code_generation_error():
    svc = _svc()
    with pytest.raises(CodeGenerationError):
        svc._extract_code_from_markdown("")


def test_whitespace_only_raises():
    svc = _svc()
    with pytest.raises(CodeGenerationError):
        svc._extract_code_from_markdown("   \n\t  ")


def test_empty_fenced_block_raises():
    svc = _svc()
    with pytest.raises(CodeGenerationError):
        svc._extract_code_from_markdown("```python\n```")


def test_prose_without_code_block_raises():
    svc = _svc()
    prose = "I'm sorry, but I can't help with that request. Please try rephrasing."
    with pytest.raises(CodeGenerationError):
        svc._extract_code_from_markdown(prose)


def test_error_message_is_self_sufficient():
    svc = _svc()
    with pytest.raises(CodeGenerationError) as exc:
        svc._extract_code_from_markdown("Here is an explanation of the data instead of code.")
    msg = str(exc.value).lower()
    assert "retry" in msg or "rephrase" in msg


# --- generate_code propagates the error (both endpoints re-raise it) ---------

class _FakeResponse:
    status_code = 200

    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeAsyncClient:
    _content = ""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **k):
        return _FakeResponse(_FakeAsyncClient._content)


def _grok_for_generate() -> GrokService:
    svc = GrokService.__new__(GrokService)
    svc.base_url = "http://local"
    svc.api_key = "EMPTY"
    svc.app_url = "https://openecon.ai"
    svc.model = "gpt-oss-120b"
    svc.visualization_templates = {}
    return svc


@pytest.mark.asyncio
async def test_generate_code_raises_on_prose():
    svc = _grok_for_generate()
    _FakeAsyncClient._content = "I cannot generate that code for you."
    with patch("backend.services.grok.httpx.AsyncClient", _FakeAsyncClient), \
         patch.object(svc, "_build_system_prompt", return_value="sys"), \
         patch.object(svc, "_detect_visualization_type", return_value=None):
        with pytest.raises(CodeGenerationError):
            await svc.generate_code("plot GDP")


@pytest.mark.asyncio
async def test_generate_code_raises_on_empty():
    svc = _grok_for_generate()
    _FakeAsyncClient._content = ""
    with patch("backend.services.grok.httpx.AsyncClient", _FakeAsyncClient), \
         patch.object(svc, "_build_system_prompt", return_value="sys"), \
         patch.object(svc, "_detect_visualization_type", return_value=None):
        with pytest.raises(CodeGenerationError):
            await svc.generate_code("plot GDP")


@pytest.mark.asyncio
async def test_generate_code_returns_valid_code():
    svc = _grok_for_generate()
    _FakeAsyncClient._content = "```python\nprint('ok')\n```"
    with patch("backend.services.grok.httpx.AsyncClient", _FakeAsyncClient), \
         patch.object(svc, "_build_system_prompt", return_value="sys"), \
         patch.object(svc, "_detect_visualization_type", return_value=None):
        code = await svc.generate_code("print ok")
    assert code == "print('ok')"
