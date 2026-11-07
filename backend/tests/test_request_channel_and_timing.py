"""Structural request-channel attribution, the query_timing summary line, and
LLM connection reuse through the shared http pool."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
import pytest

from backend.utils.processing_steps import (
    ProcessingTracker,
    activate_processing_tracker,
    emit_query_timing,
    record_llm_call,
    reset_processing_tracker,
)
from backend.utils.request_channel import (
    MCP_CLIENT_HEADER,
    derive_request_channel,
    install_mcp_channel_tagging,
    is_coze_request,
    ua_family,
)

REMOTE = "198.51.100.7"
LOOP = "127.0.0.1"


@pytest.mark.parametrize(
    "headers, host, expected_channel, expected_client",
    [
        # Coze mesh headers win over everything else.
        ({"x-psm": "stone.coze.plugin_runtime", "user-agent": "Go-http-client/2.0"}, LOOP, "coze", None),
        ({"x-tlb-server-name": "data.coze.cn"}, REMOTE, "coze", None),
        # MCP self-call tag, only from a direct loopback peer.
        ({MCP_CLIENT_HEADER: "claude-ai", "user-agent": "python-httpx/0.27"}, LOOP, "mcp", "claude-ai"),
        ({MCP_CLIENT_HEADER: ""}, LOOP, "mcp", "unknown"),
        ({MCP_CLIENT_HEADER: "spoof", "user-agent": "curl/8.5.0"}, REMOTE, "api:curl", None),
        # Proxied (XFF) traffic is never "direct loopback", even via Apache on 127.0.0.1.
        ({MCP_CLIENT_HEADER: "spoof", "x-forwarded-for": "198.51.100.7"}, LOOP, "api:unknown", None),
        ({"x-forwarded-for": "198.51.100.7", "user-agent": "curl/8.5.0"}, LOOP, "api:curl", None),
        # Internal batteries: an analytics label, honored from any peer.
        ({"X-OpenEcon-Internal": "live_battery"}, LOOP, "internal", "live_battery"),
        ({"x-openecon-internal": "run_accuracy_probes", "user-agent": "Python-urllib/3.12"}, REMOTE,
         "internal", "run_accuracy_probes"),
        # Browsers: fetch-metadata headers or a Mozilla UA family.
        ({"Sec-Fetch-Mode": "cors", "user-agent": "Something/1"}, REMOTE, "browser", None),
        ({"user-agent": "Mozilla/5.0 (X11; Linux x86_64)"}, REMOTE, "browser", None),
        # Untagged local callers.
        ({"user-agent": "curl/8.5.0"}, LOOP, "loopback", None),
        # Everything else: api:<ua product token>.
        ({"user-agent": "Go-http-client/2.0"}, REMOTE, "api:go-http-client", None),
        ({"user-agent": "python-httpx/0.27.0"}, REMOTE, "api:python-httpx", None),
        ({}, REMOTE, "api:unknown", None),
    ],
)
def test_channel_derivation_table(headers, host, expected_channel, expected_client):
    got = derive_request_channel(headers, direct_host=host)
    assert got == {"channel": expected_channel, "client_name": expected_client}


def test_channel_derivation_works_on_starlette_headers():
    from starlette.datastructures import Headers

    h = Headers(raw=[(b"x-psm", b"stone.coze.x"), (b"user-agent", b"Go-http-client/2.0")])
    assert is_coze_request(h)
    assert derive_request_channel(h, direct_host=REMOTE)["channel"] == "coze"


def test_ua_family_is_structural():
    assert ua_family("Go-http-client/2.0") == "go-http-client"
    assert ua_family("curl/8.5.0") == "curl"
    assert ua_family("  ") == "unknown"
    assert ua_family(None) == "unknown"
    assert ua_family("Weird Agent!!/1 extra") == "weird"


async def test_mcp_tagging_hook_adds_client_name_from_session_context():
    """The hook reads clientInfo.name from the MCP request context that the
    low-level server sets around a tools/call handler."""
    from mcp.server.lowlevel.server import request_ctx

    seen = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get(MCP_CLIENT_HEADER)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://apiserver")
    assert install_mcp_channel_tagging(client)
    assert install_mcp_channel_tagging(client)  # idempotent

    session = SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="cursor-vscode")))
    token = request_ctx.set(SimpleNamespace(session=session))
    try:
        await client.post("/api/query", json={"query": "x"})
    finally:
        request_ctx.reset(token)
    assert seen["header"] == "cursor-vscode"
    assert len(client.event_hooks["request"]) == 1

    # Outside any MCP request context the tag degrades to "unknown".
    await client.post("/api/query", json={"query": "x"})
    assert seen["header"] == "unknown"
    await client.aclose()


async def test_fastapi_mcp_self_call_is_attributed_to_mcp_channel():
    """End to end through fastapi_mcp's real in-process self-call client: the
    API endpoint sees the tag from a direct loopback peer -> channel 'mcp'."""

    from fastapi_mcp import FastApiMCP
    from mcp.server.lowlevel.server import request_ctx

    from backend.utils.request_channel import derive_channel_for_request

    app = FastAPI()
    seen = {}

    @app.post("/api/query", operation_id="query_data")
    async def _query(body: dict, request: Request):
        seen.update(derive_channel_for_request(request))
        return {"ok": True}

    mcp = FastApiMCP(app, include_operations=["query_data"])
    assert install_mcp_channel_tagging(getattr(mcp, "_http_client", None))

    session = SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="claude-ai")))
    token = request_ctx.set(SimpleNamespace(session=session))
    try:
        await mcp._execute_api_tool(
            client=mcp._http_client,
            tool_name="query_data",
            arguments={"query": "US GDP"},
            operation_map=mcp.operation_map,
            http_request_info=None,
        )
    finally:
        request_ctx.reset(token)
    assert seen == {"channel": "mcp", "client_name": "claude-ai"}


def test_install_mcp_tagging_tolerates_missing_client():
    assert install_mcp_channel_tagging(None) is False


# ------------------------------------------------------------- query_timing


def test_timing_summary_aggregates_steps_and_llm_calls(caplog):
    tracker = ProcessingTracker()
    tracker.add_step("parsing_query", "p", duration_ms=120.0)
    tracker.add_step("fetching_data", "f", duration_ms=300.0)
    tracker.add_step("fetching_data", "f", duration_ms=200.0)
    tracker.add_step("applying_fallback", "fb", status="in-progress")  # no duration
    token = activate_processing_tracker(tracker)
    try:
        record_llm_call("vllm.generate", 900.0, ok=True)
        record_llm_call("vllm.generate", 1100.0, ok=False)
        record_llm_call("openrouter.parse_direct", 400.0)
    finally:
        reset_processing_tracker(token)
    record_llm_call("vllm.generate", 5.0)  # no active tracker -> no-op

    caplog.set_level("INFO", logger="backend.utils.processing_steps")
    payload = emit_query_timing(tracker, endpoint="/api/query", outcome="data", total_ms=2500.04,
                                channel="browser", unused=None)

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("query_timing ")]
    assert len(lines) == 1
    assert json.loads(lines[0].split(" ", 1)[1]) == payload
    assert payload["endpoint"] == "/api/query"
    assert payload["outcome"] == "data"
    assert payload["total_ms"] == 2500.0
    assert payload["channel"] == "browser"
    assert "unused" not in payload
    assert payload["step_ms"] == {"parsing_query": 120.0, "fetching_data": 500.0}
    assert payload["step_count"] == {"parsing_query": 1, "fetching_data": 2}
    assert payload["llm"]["calls"] == 3
    assert payload["llm"]["ms"] == 2400.0
    assert payload["llm"]["by_site"]["vllm.generate"] == {"calls": 2, "ms": 2000.0, "errors": 1}


def test_emit_query_timing_without_tracker():
    payload = emit_query_timing(None, endpoint="/api/query/stream", outcome="timeout", total_ms=None)
    assert payload == {"endpoint": "/api/query/stream", "outcome": "timeout", "total_ms": None}


# ------------------------------------------------- LLM pooled-client reuse


class _PoolClient:
    def __init__(self, response):
        self.calls = []
        self._response = response

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def _ok_completion():
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "{\"a\": 1}"}}], "usage": {}},
        request=httpx.Request("POST", "http://x"),
    )


@pytest.mark.parametrize("provider_name", ["vllm", "openrouter", "ollama"])
async def test_llm_providers_reuse_shared_pool_with_per_request_timeout(monkeypatch, provider_name):
    import backend.services.llm as llm

    pool = _PoolClient(_ok_completion())
    monkeypatch.setattr(llm, "get_http_client", lambda: pool)

    def _no_fresh_clients(*_a, **_k):
        raise AssertionError("LLM call built a fresh httpx.AsyncClient")

    monkeypatch.setattr(llm.httpx, "AsyncClient", _no_fresh_clients)

    if provider_name == "vllm":
        provider = llm.VLLMProvider(base_url="http://localhost:8000", model="gpt-oss-120b", timeout=77.0)
        site = "vllm.generate"
    elif provider_name == "openrouter":
        provider = llm.OpenRouterProvider(api_key="k", model="openai/gpt-oss-120b", timeout=77.0)
        site = "openrouter.generate"
    else:
        provider = llm.OllamaProvider(base_url="http://localhost:11434", model="m", timeout=77.0)
        site = "ollama.generate"

    tracker = ProcessingTracker()
    token = activate_processing_tracker(tracker)
    try:
        await provider.generate("hello")
        await provider.generate("again")
    finally:
        reset_processing_tracker(token)

    assert len(pool.calls) == 2  # both calls went through the ONE pooled client
    assert all(kw.get("timeout") == 77.0 for _, kw in pool.calls)
    summary = tracker.timing_summary()["llm"]
    assert summary["calls"] == 2
    assert summary["by_site"][site]["calls"] == 2
    assert summary["by_site"][site]["errors"] == 0


async def test_vllm_http_error_still_raises_and_is_counted_as_error(monkeypatch):
    import backend.services.llm as llm

    err = httpx.Response(503, json={}, request=httpx.Request("POST", "http://x"))
    monkeypatch.setattr(llm, "get_http_client", lambda: _PoolClient(err))
    provider = llm.VLLMProvider(base_url="http://localhost:8000", model="gpt-oss-120b")

    tracker = ProcessingTracker()
    token = activate_processing_tracker(tracker)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await provider.generate("hello")
    finally:
        reset_processing_tracker(token)
    assert tracker.timing_summary()["llm"]["by_site"]["vllm.generate"]["errors"] == 1


def test_shared_pool_keepalive_suits_llm_tunnel():
    from backend.services.http_pool import HTTPClientPool

    assert HTTPClientPool._KEEPALIVE_EXPIRY >= 60.0
