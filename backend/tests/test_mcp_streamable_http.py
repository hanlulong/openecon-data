"""Streamable HTTP (MCP 2025-03-26+) on POST /mcp next to legacy SSE on GET /mcp.

Everything runs against isolated apps: in-process ASGI, TestClient lifespans,
or one ephemeral loopback listener. No production service or external host is
contacted; tool self-calls go to local stub backends.

The mechanism under test is the dispatcher, not any one client: every GET
stays on legacy SSE unless it echoes a session id this server issued on a
Streamable HTTP response (then 405, as the spec allows); POST goes to the SDK
manager in stateless JSON mode; unsupported protocol-version stamps on
follow-up requests are tolerated like the legacy transport always did.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import shutil
import sys

import anyio
import httpx
import mcp.types as types
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.types import LATEST_PROTOCOL_VERSION

from backend.mcp_stream_limit import MCPStreamLimitMiddleware
from backend.mcp_transport import (
    SDK_STREAMABLE_LOGGER,
    LegacySSETransport,
    MCPPostBodyLimitMiddleware,
    StatelessStreamableHTTP,
    StatelessTerminateRaceFilter,
    serve_streamable,
)
from backend.tests.test_mcp_rate_limit_middleware import _drive, _make_mw
from backend.tests.test_mcp_transport import ASGIRequest, loopback_server

ACCEPT_BOTH = "application/json, text/event-stream"
ACCEPT_CODEX = "text/event-stream, application/json"  # Codex sends this order.
LEGACY_HEX = "e9f717f4" + "0" * 24  # Shape of our legacy SSE session ids.
OUR_SESSION = "stateless-" + "2" * 32  # Shape of the ids StatelessStreamableHTTP issues.


def isolated_server(entered=None, release=None):
    server = Server("isolated-streamable")

    @server.list_tools()
    async def list_tools():
        schema = {"type": "object", "properties": {"value": {"type": "string"}}}
        return [
            types.Tool(name="echo", description="Echo a value", inputSchema=schema),
            types.Tool(name="wait", description="Block until released", inputSchema=schema),
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "wait":
            entered.set()
            await release.wait()
        return [types.TextContent(type="text", text=f"echo:{arguments.get('value')}")]

    return server


def build(server=None, *, streamable=True):
    """App + transports + stream limiter, as backend.main wires them."""
    server = server or isolated_server()
    app = FastAPI()
    transport = LegacySSETransport(server, post_send_timeout=0.2)
    handler = StatelessStreamableHTTP(server) if streamable else None
    transport.mount(app, streamable=handler)
    limiter = MCPStreamLimitMiddleware(app, 4, 4, lambda request: request.client.host)
    return limiter, transport, handler


def lifespan_app(server=None, streamable=None):
    """An app whose lifespan runs the Streamable HTTP manager like main.lifespan."""
    server = server or isolated_server()
    streamable = streamable or StatelessStreamableHTTP(server)

    @asynccontextmanager
    async def lifespan(app):
        async with serve_streamable(streamable):
            yield

    app = FastAPI(lifespan=lifespan)
    transport = LegacySSETransport(server)
    transport.mount(app, streamable=streamable)
    return app, transport, streamable


def rpc(method, request_id=1, **params):
    message = {"jsonrpc": "2.0", "method": method}
    if params:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    return message


def initialize(version="2025-06-18"):
    return rpc(
        "initialize", protocolVersion=version, capabilities={},
        clientInfo={"name": "isolated-client", "version": "1"},
    )


def post(client, message, accept=ACCEPT_BOTH, **headers):
    return client.post(
        "/mcp", content=json.dumps(message),
        headers={"accept": accept, "content-type": "application/json", **headers},
    )


# ---------------------------------------------------------------------------
# Real SDK clients
# ---------------------------------------------------------------------------

def observed(app, log):
    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def sending(message):
            if message["type"] == "http.response.start":
                log.append((scope["method"], scope["path"], message["status"]))
            await send(message)

        await app(scope, receive, sending)

    return wrapped


def test_python_sdk_streamable_client_initializes_lists_and_calls_without_legacy_sessions():
    async def scenario():
        app, transport, streamable = build()
        requests = []
        async with asyncio.timeout(15), serve_streamable(streamable), \
                loopback_server(observed(app, requests)) as base:
            for _ in range(3):
                async with streamablehttp_client(base + "/mcp", timeout=3, sse_read_timeout=3) as (
                    read, write, get_session_id,
                ):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=3)) as session:
                        init = await session.initialize()
                        assert init.protocolVersion == LATEST_PROTOCOL_VERSION
                        tools = await session.list_tools()
                        assert [tool.name for tool in tools.tools] == ["echo", "wait"]
                        result = await session.call_tool("echo", {"value": "py-value"})
                        assert not result.isError
                        assert result.content[0].text == "echo:py-value"
                        assert get_session_id().startswith("stateless-")
                        # The optional notification stream (GET with our id)
                        # must not open a legacy SSE session.
                        assert transport.active_sessions == 0
                async with asyncio.timeout(1):
                    while app.active:
                        await asyncio.sleep(0.005)
        assert transport.active_sessions == 0
        gets = [status for method, _, status in requests if method == "GET"]
        assert gets and set(gets) == {405}
        assert {status for method, _, status in requests if method == "DELETE"} == {405}
        assert {status for method, _, status in requests if method == "POST"} <= {200, 202}

    asyncio.run(scenario())


def test_real_javascript_streamable_client_round_trip():
    sdk_root = os.environ.get("MCP_JS_SDK_ROOT")
    node = shutil.which("node")
    if not sdk_root or not node:
        pytest.skip("Set MCP_JS_SDK_ROOT and provide Node to run the real-client regression")
    assert (Path(sdk_root) / "dist/cjs/client/streamableHttp.js").is_file()

    async def scenario():
        app, transport, streamable = build()
        requests = []
        async with serve_streamable(streamable), loopback_server(observed(app, requests)) as base:
            process = await asyncio.create_subprocess_exec(
                node, str(Path(__file__).with_name("mcp_js_streamable.cjs")), base + "/mcp",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            assert process.returncode == 0, stderr.decode()[-2000:]
            async with asyncio.timeout(1):
                while app.active:
                    await asyncio.sleep(0.005)
        return json.loads(stdout), requests, transport

    result, requests, transport = asyncio.run(scenario())
    assert result["tool_names"] == ["echo", "wait"]
    assert result["text"] == "echo:js-value" and not result["is_error"]
    assert result["session_id"].startswith("stateless-")
    assert result["protocol_version"] == LATEST_PROTOCOL_VERSION
    assert result["errors"] == []
    # The TS client always opens the optional GET stream after initialization;
    # it must get 405 and never become a legacy SSE session.
    assert [status for method, _, status in requests if method == "GET"] == [405]
    assert transport.active_sessions == 0


def test_legacy_sse_client_still_works_next_to_streamable():
    async def scenario():
        app, transport, streamable = build()
        async with asyncio.timeout(10), serve_streamable(streamable), loopback_server(app) as base:
            async with sse_client(base + "/mcp", timeout=2, sse_read_timeout=3) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=2)) as session:
                    await session.initialize()
                    result = await session.call_tool("echo", {"value": "legacy"})
                    assert result.content[0].text == "echo:legacy"
            async with asyncio.timeout(1):
                while transport.active_sessions or app.active:
                    await asyncio.sleep(0.005)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Raw HTTP: what observed client stacks actually send
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("accept", [ACCEPT_BOTH, ACCEPT_CODEX])
@pytest.mark.parametrize("requested", ["2025-06-18", "2025-03-26", "2025-11-25", "2026-07-28"])
def test_raw_initialize_returns_json_and_negotiates_a_supported_version(accept, requested):
    app, _, _ = lifespan_app()
    with TestClient(app) as client:
        response = post(client, initialize(requested), accept=accept)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["mcp-session-id"].startswith("stateless-")
    body = response.json()
    expected = requested if requested in ("2025-06-18", "2025-03-26") else LATEST_PROTOCOL_VERSION
    assert body["result"]["protocolVersion"] == expected


@pytest.mark.parametrize("stamp", [None, "2025-06-18", "2025-11-25", "2026-07-28", "not-a-version"])
def test_followup_requests_tolerate_protocol_version_stamps(stamp):
    """Clients newer than the SDK stamp their own version after initialize;
    the legacy transport never validated it, so neither may streamable."""
    app, _, _ = lifespan_app()
    headers = {"mcp-session-id": "stateless-" + "1" * 32}
    if stamp:
        headers["mcp-protocol-version"] = stamp
    with TestClient(app) as client:
        assert post(client, rpc("notifications/initialized", None), **headers).status_code == 202
        listed = post(client, rpc("tools/list", 2), **headers)
        called = post(client, rpc("tools/call", 3, name="echo", arguments={"value": "v"}), **headers)
        pinged = post(client, rpc("ping", 4), **headers)
    assert listed.status_code == 200 and [t["name"] for t in listed.json()["result"]["tools"]] == ["echo", "wait"]
    assert called.status_code == 200 and called.json()["result"]["content"][0]["text"] == "echo:v"
    assert pinged.status_code == 200 and pinged.json()["result"] == {}
    # A request that already carries a session id keeps it: none is re-issued.
    assert "mcp-session-id" not in listed.headers


def test_initialize_carrying_a_foreign_session_id_still_gets_our_marker():
    """A client re-initializing with a stale or legacy id must still learn an
    id of ours, or its notification GET would open a legacy SSE session."""
    app, transport, _ = lifespan_app()
    with TestClient(app) as client:
        response = post(client, initialize(), **{"mcp-session-id": LEGACY_HEX})
        assert response.status_code == 200
        issued = response.headers["mcp-session-id"]
        assert issued.startswith("stateless-")
        stream = client.get("/mcp", headers={"accept": "text/event-stream", "mcp-session-id": issued})
        assert stream.status_code == 405 and transport.active_sessions == 0


def test_post_without_event_stream_accept_is_406_not_rewritten():
    app, _, _ = lifespan_app()
    with TestClient(app) as client:
        assert post(client, initialize(), accept="application/json").status_code == 406


def test_host_behind_proxy_is_accepted_without_dns_rebinding_rejection():
    app, _, _ = lifespan_app()
    with TestClient(app, base_url="https://data.openecon.ai") as client:
        response = post(
            client, initialize(),
            **{"x-forwarded-for": "198.51.100.20", "x-forwarded-proto": "https",
               "x-forwarded-host": "data.openecon.ai"},
        )
    assert response.status_code == 200


def test_typescript_style_sequence_never_opens_a_legacy_session():
    app, transport, _ = lifespan_app()
    with TestClient(app) as client:
        init = post(client, initialize("2025-11-25"))
        session_id = init.headers["mcp-session-id"]
        headers = {"mcp-session-id": session_id, "mcp-protocol-version": "2025-06-18"}
        assert post(client, rpc("notifications/initialized", None), **headers).status_code == 202
        stream = client.get("/mcp", headers={"accept": "text/event-stream", **headers})
        assert stream.status_code == 405
        assert stream.headers["allow"] == "GET, POST"
        assert transport.active_sessions == 0
        assert post(client, rpc("tools/list", 2), **headers).status_code == 200
        assert client.delete("/mcp", headers=headers).status_code == 405


@pytest.mark.parametrize("headers", [
    [],
    [(b"mcp-protocol-version", b"2025-06-18")],  # EventSource reconnects send this.
    [(b"mcp-session-id", LEGACY_HEX.encode())],  # Reuse of the legacy session hex.
])
def test_every_legacy_get_still_receives_the_endpoint_event(headers):
    async def scenario():
        app, transport, _ = build()
        stream = await ASGIRequest(app, headers=[(b"accept", b"text/event-stream"), *headers]).open()
        assert stream.endpoint.startswith("/mcp/messages/?session_id=")
        assert transport.active_sessions == 1
        await stream.close()
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


def test_kill_switch_mount_keeps_post_and_delete_at_405(monkeypatch):
    from backend import main

    app, transport, handler = build(streamable=False)
    assert handler is None
    with TestClient(app) as client:
        assert post(client, initialize()).status_code == 405
        assert client.delete("/mcp").status_code == 405
    monkeypatch.setenv("MCP_STREAMABLE_HTTP_ENABLED", "false")
    assert main.mcp_streamable_http_enabled() is False
    monkeypatch.delenv("MCP_STREAMABLE_HTTP_ENABLED")
    assert main.mcp_streamable_http_enabled() is True


# ---------------------------------------------------------------------------
# Lifespan: one fresh SDK manager per entry; never a 500
# ---------------------------------------------------------------------------

def test_post_without_running_manager_is_503_with_retry_after():
    app, _, streamable = build()
    assert not streamable.running
    with TestClient(app) as client:  # Plain app: nothing runs the manager.
        response = post(client, initialize())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


def test_sequential_lifespans_each_get_a_fresh_manager():
    app, _, streamable = lifespan_app()
    for _ in range(2):
        with TestClient(app) as client:
            assert streamable.running
            assert post(client, initialize()).status_code == 200
        assert not streamable.running


def test_start_failure_does_not_fail_startup():
    class BrokenStreamable(StatelessStreamableHTTP):
        @asynccontextmanager
        async def run(self):
            raise RuntimeError("isolated start failure")
            yield  # pragma: no cover

    server = isolated_server()
    app, _, _ = lifespan_app(server, BrokenStreamable(server))
    with TestClient(app) as client:
        assert post(client, initialize()).status_code == 503
        assert client.get("/mcp/messages/").status_code == 405  # App still serves.


def _stub_heavy_startup(monkeypatch):
    from backend import main
    from backend.services import catalog_validator, embedding_retrieval

    class _NoIndex:
        def _load_index(self):
            return False

    monkeypatch.setattr(embedding_retrieval, "get_embedding_retrieval", lambda: _NoIndex())
    monkeypatch.setattr(catalog_validator, "run_validation", lambda: None)
    monkeypatch.setattr(main.settings, "enable_metadata_loading", False)
    monkeypatch.setattr(main.settings, "disable_background_jobs", True)
    return main


def test_main_lifespan_runs_the_mounted_streamable(monkeypatch):
    """backend.main.lifespan itself (heavy startup pieces stubbed) starts and
    stops the manager stored on app.state, across repeated lifespans."""
    main = _stub_heavy_startup(monkeypatch)

    server = isolated_server()
    streamable = StatelessStreamableHTTP(server)
    app = FastAPI(lifespan=main.lifespan)
    app.state.mcp_streamable = streamable
    LegacySSETransport(server).mount(app, streamable=streamable)
    for _ in range(2):
        with TestClient(app) as client:
            assert streamable.running
            assert post(client, initialize()).status_code == 200
        assert not streamable.running


def test_main_lifespan_finishes_shutdown_when_the_manager_exit_fails(monkeypatch):
    main = _stub_heavy_startup(monkeypatch)
    from backend.services import http_pool

    closed = []

    async def recording_close():
        closed.append(True)

    monkeypatch.setattr(http_pool, "close_http_pool", recording_close)

    class ExitFails(StatelessStreamableHTTP):
        @asynccontextmanager
        async def run(self):
            yield
            raise RuntimeError("isolated shutdown failure")

    app = FastAPI(lifespan=main.lifespan)
    app.state.mcp_streamable = ExitFails(isolated_server())
    with TestClient(app):
        pass
    assert closed == [True]  # The steps after the manager still ran.


# ---------------------------------------------------------------------------
# SDK error noise: the stateless terminate race must never reach the journal
# ---------------------------------------------------------------------------

def _sdk_problems(caplog):
    return [
        (record.name, record.levelname, record.getMessage())
        for record in caplog.records
        if record.name.startswith("mcp.") and record.levelno >= logging.WARNING
    ]


def test_streamable_exchanges_log_no_sdk_warnings_or_errors(caplog):
    """mcp 1.18 terminates each stateless transport after its response, which
    makes its message router log an ERROR traceback per request unless the
    benign race is filtered. Covers SDK client traffic, a raw ping and a 400."""
    caplog.set_level(logging.WARNING)

    async def scenario():
        app, transport, streamable = build()
        async with asyncio.timeout(15), serve_streamable(streamable), loopback_server(app) as base:
            async with streamablehttp_client(base + "/mcp", timeout=3, sse_read_timeout=3) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=3)) as session:
                    await session.initialize()
                    await session.list_tools()
                    await session.call_tool("echo", {"value": "quiet"})
            headers = {"accept": ACCEPT_BOTH, "content-type": "application/json"}
            async with httpx.AsyncClient(timeout=3) as client:
                pinged = await client.post(base + "/mcp", content=json.dumps(rpc("ping")), headers=headers)
                malformed = await client.post(base + "/mcp", content=b"{not json", headers=headers)
            # The router task fails after the response has been delivered.
            await asyncio.sleep(0.3)
        return pinged.status_code, malformed.status_code

    assert asyncio.run(scenario()) == (200, 400)
    assert _sdk_problems(caplog) == []


def _record_with(message, exc=None):
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException:
            exc_info = sys.exc_info()
    return logging.LogRecord(SDK_STREAMABLE_LOGGER, logging.ERROR, __file__, 1, message, None, exc_info)


def test_terminate_race_filter_drops_only_the_benign_router_record():
    race = StatelessTerminateRaceFilter()
    assert not race.filter(_record_with("Error in message router", anyio.ClosedResourceError()))
    assert not race.filter(_record_with("Error in message router", anyio.BrokenResourceError()))
    # Genuine failures stay visible.
    assert race.filter(_record_with("Error in message router", ValueError("real router bug")))
    assert race.filter(_record_with("Error in message router"))
    assert race.filter(_record_with("Error handling POST request", anyio.ClosedResourceError()))
    assert race.filter(_record_with("Stateless session crashed", RuntimeError("crash")))
    installed = logging.getLogger(SDK_STREAMABLE_LOGGER).filters
    assert sum(isinstance(f, StatelessTerminateRaceFilter) for f in installed) == 1
    # The logger level is not raised to hide errors.
    assert logging.getLogger(SDK_STREAMABLE_LOGGER).getEffectiveLevel() <= logging.WARNING


# ---------------------------------------------------------------------------
# The notification-stream GET is not a legacy stream open
# ---------------------------------------------------------------------------

def test_stateless_session_get_skips_stream_admission_even_at_capacity(caplog):
    caplog.set_level(logging.WARNING, logger="openecon.mcp_stream_limit")

    async def scenario():
        app, transport, _ = build()  # Stream limit: 4 global, 4 per client.
        legacy = [await ASGIRequest(app, headers=[(b"accept", b"text/event-stream")]).open() for _ in range(4)]
        assert app.active == 4
        probe = ASGIRequest(app, headers=[(b"mcp-session-id", OUR_SESSION.encode())])
        await asyncio.wait_for(probe.run(), 1)
        assert probe.status == 405 and probe.headers[b"allow"] == b"GET, POST"
        assert app.active == 4 and app.rejected_global == app.rejected_client == 0
        for stream in legacy:
            await stream.close()
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())
    assert not [r for r in caplog.records if r.name == "openecon.mcp_stream_limit"]


def _drive_with_headers(mw, path, ip, headers):
    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": list(headers), "client": (ip, 54321),
        "server": ("testserver", 80), "scheme": "http",
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_stateless_session_get_spends_the_generic_bucket_not_stream_opens(monkeypatch):
    mw, _ = _make_mw(monkeypatch)
    ip = "198.51.100.31"
    marked = [(b"mcp-session-id", OUR_SESSION.encode())]
    for i in range(20):
        assert _drive_with_headers(mw, "/mcp", ip, marked) == 200, f"marked {i}"
    # The legacy stream-open budget (12/min) is untouched by those GETs ...
    for i in range(12):
        assert _drive_with_headers(mw, "/mcp", ip, []) == 200, f"legacy open {i}"
    assert _drive_with_headers(mw, "/mcp", ip, []) == 429
    # ... and the marked GETs are still bounded, by the generic 200/min bucket.
    for i in range(180):
        assert _drive_with_headers(mw, "/mcp", ip, marked) == 200, f"marked {20 + i}"
    assert _drive_with_headers(mw, "/mcp", ip, marked) == 429


# ---------------------------------------------------------------------------
# Shared capacity, body limit and rate-limit bucket
# ---------------------------------------------------------------------------

def test_streamable_posts_hold_the_shared_post_allowance_for_the_whole_exchange():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        app, transport, streamable = build(isolated_server(entered, release))
        gated = MCPPostBodyLimitMiddleware(app, max_concurrent_requests=1)
        headers = [(b"accept", ACCEPT_BOTH.encode()), (b"content-type", b"application/json")]
        async with serve_streamable(streamable):
            blocked = ASGIRequest(gated, "POST", "/mcp", headers, json.dumps(
                rpc("tools/call", 1, name="wait", arguments={"value": "slow"})).encode())
            task = asyncio.create_task(blocked.run())
            await asyncio.wait_for(entered.wait(), 2)
            rejected = ASGIRequest(gated, "POST", "/mcp", headers, json.dumps(rpc("ping")).encode())
            await asyncio.wait_for(rejected.run(), 1)
            assert rejected.status == 503 and rejected.headers[b"retry-after"] == b"5"
            release.set()
            await asyncio.wait_for(task, 2)
            assert blocked.status == 200
            assert gated.active_requests == 0
            oversized = ASGIRequest(gated, "POST", "/mcp", headers, b"x" * (1024 * 1024 + 1))
            await asyncio.wait_for(oversized.run(), 1)
            assert oversized.status == 413

    asyncio.run(scenario())


def test_streamable_post_shares_the_message_rate_limit_bucket(monkeypatch):
    mw, inner = _make_mw(monkeypatch)
    ip = "198.51.100.30"
    for i in range(15):
        assert _drive(mw, "POST", "/mcp", ip=ip) == 200, f"streamable {i}"
        assert _drive(mw, "POST", "/mcp/messages/", ip=ip) == 200, f"legacy {i}"
    assert _drive(mw, "POST", "/mcp", ip=ip) == 429
    assert _drive(mw, "POST", "/mcp/messages/", ip=ip) == 429
    # The connection-open bucket is separate.
    assert _drive(mw, "GET", "/mcp", ip=ip) == 200


# ---------------------------------------------------------------------------
# Real query_data surface (tool name, schema, description, instructions)
# ---------------------------------------------------------------------------

PINNED_QUERY_DATA_SCHEMA = {
    "properties": {
        "conversationId": {
            "anyOf": [{"maxLength": 100, "type": "string"}, {"type": "null"}],
            "description": "Conversation ID for follow-ups", "title": "conversationId", "type": "string",
        },
        "query": {
            "description": "Natural language query (max 5000 chars)", "maxLength": 5000,
            "minLength": 1, "title": "query", "type": "string",
        },
        "sessionId": {
            "anyOf": [{"maxLength": 100, "type": "string"}, {"type": "null"}],
            "description": "Session ID for anonymous user tracking", "title": "sessionId", "type": "string",
        },
    },
    "required": ["query"], "title": "query_dataArguments", "type": "object",
}


def test_query_data_over_streamable_with_stub_backend():
    from backend import main

    seen = []
    stub = FastAPI()

    @stub.post("/api/query")
    async def stub_query(body: dict):
        seen.append(body)
        return {"conversationId": "conv-1", "clarificationNeeded": False,
                "data": [{"metadata": {"source": "STUB"}, "data": [{"date": "2026-10-01", "value": 50.2}]}]}

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stub), base_url="http://isolated-test",
        ) as internal_http:
            mcp = main.create_mcp_server(main.app, http_client=internal_http)
            app, _, streamable = build(mcp.server)
            async with asyncio.timeout(15), serve_streamable(streamable), loopback_server(app) as base:
                async with streamablehttp_client(base + "/mcp", timeout=3) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                        init = await session.initialize()
                        tools = (await session.list_tools()).tools
                        result = await session.call_tool("query_data", {"query": "中国制造业PMI最近12个月"})
        return init, tools, result

    init, tools, result = asyncio.run(scenario())
    assert init.serverInfo.version == main.app.version
    assert init.instructions == main.MCP_SERVER_INSTRUCTIONS and "conversationId" in init.instructions
    assert [tool.name for tool in tools] == ["query_data"]
    assert tools[0].inputSchema == PINNED_QUERY_DATA_SCHEMA
    # The route text alone: fastapi_mcp's generated "### Responses" example is
    # stripped, keeping the tool under the 1,024-char cap some APIs enforce.
    assert tools[0].description == f"{main.QUERY_DATA_SUMMARY}\n\n{main.QUERY_DATA_DESCRIPTION}"
    assert len(tools[0].description) <= 1024
    assert main.DATA_COVERAGE_TEXT in tools[0].description
    assert not result.isError and "50.2" in result.content[0].text
    assert seen == [{"query": "中国制造业PMI最近12个月"}]


def test_generated_responses_section_is_stripped_structurally():
    from backend.main import strip_generated_responses

    assert strip_generated_responses("Summary\n\nBody\n\n### Responses:\n\n**200**: x") == "Summary\n\nBody"
    assert strip_generated_responses("Summary only") == "Summary only"
    assert strip_generated_responses(None) is None


def test_coverage_text_names_every_provider_module():
    """Drift guard: a new provider module must be named in the coverage text
    agents see (OECD only as on-explicit-request)."""
    import importlib
    import pkgutil

    import backend.providers as providers
    from backend import main
    from backend.providers.base import BaseProvider

    names = {
        "FREDProvider": "FRED", "WorldBankProvider": "World Bank", "IMFProvider": "IMF",
        "EurostatProvider": "Eurostat", "StatsCanProvider": "Statistics Canada", "BISProvider": "BIS",
        "ComtradeProvider": "UN Comtrade", "ExchangeRateProvider": "exchange rates",
        "CoinGeckoProvider": "CoinGecko", "ChinaMacroProvider": "ChinaMacro",
        "OECDProvider": "OECD only when explicitly requested",
    }
    found = set()
    for module in pkgutil.iter_modules(providers.__path__):
        loaded = importlib.import_module(f"backend.providers.{module.name}")
        for value in vars(loaded).values():
            if isinstance(value, type) and issubclass(value, BaseProvider) and value is not BaseProvider:
                found.add(value.__name__)
    assert found and found <= set(names), f"name new providers in DATA_COVERAGE_TEXT: {found - set(names)}"
    for cls in found:
        assert names[cls] in main.DATA_COVERAGE_TEXT


# ---------------------------------------------------------------------------
# Log volume: one record per MCP POST, level by JSON-RPC method and status
# ---------------------------------------------------------------------------

LOG_NAME = "backend.utils.logging_security"


def _records(caplog):
    out = []
    for record in caplog.records:
        if record.name == LOG_NAME:
            entry = json.loads(record.getMessage())
            out.append((record.levelno, entry["message"], entry["data"]))
    return out


def _through_logging(method, path, body=b"", status=202, headers=()):
    from backend.main import SecureLoggingASGIMiddleware

    async def inner(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    request = ASGIRequest(
        SecureLoggingASGIMiddleware(inner), method, path,
        [(b"user-agent", b"isolated-agent/1"), *headers], body,
    )
    asyncio.run(request.run())


@pytest.mark.parametrize("path,body,status,level,with_headers", [
    ("/mcp/messages/?session_id=x", rpc("ping"), 202, logging.DEBUG, False),
    ("/mcp/messages/?session_id=x", rpc("notifications/initialized", None), 202, logging.DEBUG, False),
    ("/mcp", rpc("ping"), 200, logging.DEBUG, False),
    ("/mcp", rpc("tools/list"), 200, logging.INFO, False),
    ("/mcp/messages/?session_id=x", rpc("tools/call", name="query_data"), 202, logging.INFO, True),
    ("/mcp", rpc("tools/call", name="query_data"), 200, logging.INFO, True),
    ("/mcp", initialize(), 200, logging.INFO, True),
    ("/mcp", rpc("ping"), 429, logging.INFO, True),
    ("/mcp/messages/?session_id=x", rpc("ping"), 404, logging.INFO, True),
    ("/mcp", [rpc("ping")], 200, logging.INFO, True),  # Batch: not one method.
    ("/mcp", "not json", 400, logging.INFO, True),
])
def test_mcp_post_is_one_record_with_level_from_method_and_status(caplog, path, body, status, level, with_headers):
    caplog.set_level(logging.DEBUG, logger=LOG_NAME)
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    _through_logging("POST", path, raw, status)
    records = _records(caplog)
    assert not [r for r in records if r[1] in ("Request received", "Request completed")]
    request_records = [r for r in records if r[1] == "MCP request"]
    assert len(request_records) == 1
    got_level, _, data = request_records[0]
    assert got_level == level
    assert data["status_code"] == status and data["path"] == path.split("?")[0]
    assert ("headers" in data) is with_headers
    assert "query_params" not in data or with_headers  # No session id on compact lines.
    if isinstance(body, dict) and "method" in body:
        assert data["mcp_method"] == body["method"]


@pytest.mark.parametrize("status,level,with_headers", [
    (405, logging.DEBUG, False),  # The designed answer, once per streamable session.
    (503, logging.INFO, True),    # Anything else is unexpected: keep it visible.
])
def test_stateless_session_get_is_one_record_not_a_session_open(caplog, status, level, with_headers):
    caplog.set_level(logging.DEBUG, logger=LOG_NAME)
    _through_logging("GET", "/mcp", status=status, headers=[(b"mcp-session-id", OUR_SESSION.encode())])
    records = _records(caplog)
    assert not [r for r in records if r[1] in ("Request received", "Request completed")]
    request_records = [r for r in records if r[1] == "MCP request"]
    assert len(request_records) == 1
    got_level, _, data = request_records[0]
    assert got_level == level and data["status_code"] == status
    assert ("headers" in data) is with_headers


def test_streamable_initialize_still_logs_client_identity(caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    _through_logging("POST", "/mcp", json.dumps(initialize("2025-11-25")).encode(), 200)
    connected = [d for _, m, d in _records(caplog) if m == "MCP client connected"]
    assert connected and connected[0]["mcp_client_name"] == "isolated-client"
    assert connected[0]["protocol_version"] == "2025-11-25"


@pytest.mark.parametrize("headers", [(), ((b"mcp-session-id", LEGACY_HEX.encode()),)])
def test_get_mcp_open_keeps_info_with_user_agent_and_headers(caplog, headers):
    caplog.set_level(logging.DEBUG, logger=LOG_NAME)
    _through_logging("GET", "/mcp", status=200, headers=headers)
    received = [(lvl, d) for lvl, m, d in _records(caplog) if m == "Request received"]
    assert len(received) == 1
    assert received[0][0] == logging.INFO
    assert received[0][1]["user_agent"] == "isolated-agent/1" and "headers" in received[0][1]


def test_sdk_per_message_loggers_are_capped_at_warning():
    import backend.main  # noqa: F401  (applies the caps)

    for name in ("mcp.server.lowlevel.server", "mcp.server.streamable_http"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def _access_line(path, status, method="POST"):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("198.51.100.1:5000", method, path, "1.1", status), None,
    )


def test_uvicorn_access_lines_are_dropped_only_for_successful_mcp_requests():
    from backend.main import MCPAccessLogFilter

    access = MCPAccessLogFilter()
    # Duplicates of SecureLogging's own MCP records.
    assert not access.filter(_access_line("/mcp", 200))
    assert not access.filter(_access_line("/mcp/messages/?session_id=abc", 202))
    assert not access.filter(_access_line("/mcp", 200, method="GET"))
    # The only record of POST-pool rejections (answered outside SecureLogging).
    for status in (408, 413, 429, 503):
        assert access.filter(_access_line("/mcp", status))
    assert access.filter(_access_line("/api/query", 200))
    assert access.filter(logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "other", None, None))
    installed = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, MCPAccessLogFilter) for f in installed) == 1
