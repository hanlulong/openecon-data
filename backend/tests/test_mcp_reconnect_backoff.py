"""Initialization backoff preserves protocol delivery and bounded cleanup."""

import asyncio

import anyio
import pytest
from fastapi import FastAPI
from mcp.client.sse import sse_client
from pydantic import ValidationError

from backend.config import Settings
from backend.mcp_stream_limit import MCPStreamLimitMiddleware
from backend.mcp_transport import LegacySSETransport
from backend.tests.test_mcp_session_lease import ReplyServer, rpc, wait_response
from backend.tests.test_mcp_transport import ASGIRequest, loopback_server, post


def mounted(**options):
    server = ReplyServer()
    transport = LegacySSETransport(
        server, initialize_timeout=0.06, idle_timeout=0.09,
        post_send_timeout=0.025, **options,
    )
    app = FastAPI()
    transport.mount(app)
    return MCPStreamLimitMiddleware(app, 2, 2, lambda request: request.client.host), transport, server


def retry_frames(stream):
    return [message["body"] for message in stream.messages if b"retry:" in message.get("body", b"")]


def test_uninitialized_expiry_sends_named_retry_before_eof_and_fresh_client_works():
    async def scenario():
        app, transport, server = mounted()
        stream = await ASGIRequest(app).open()
        assert not retry_frames(stream)
        await asyncio.wait_for(stream.task, 0.5)
        assert retry_frames(stream) == [b"event: mcp-retry\r\nretry: 300000\r\n\r\n"]
        assert stream.messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        assert (await post(app, stream.endpoint)).status == 404
        assert app.active == transport.active_sessions == 0
        server.assert_streams_closed()
        fresh = await ASGIRequest(app).open()
        await post(app, fresh.endpoint, rpc("initialize"))
        assert "result" in await wait_response(fresh)
        assert not retry_frames(fresh)
        await fresh.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("close_reason", ["initialized_idle", "initialized_disconnect", "disconnect"])
def test_normal_closure_does_not_send_initialization_backoff(close_reason):
    async def scenario():
        app, transport, _ = mounted()
        stream = await ASGIRequest(app).open()
        if close_reason.startswith("initialized"):
            await post(app, stream.endpoint, rpc("initialize"))
            await wait_response(stream)
        if close_reason == "initialized_idle":
            await asyncio.wait_for(stream.task, 0.5)
        else:
            await stream.close()
        assert not retry_frames(stream)
        assert app.active == transport.active_sessions == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["blocked", "send_error", "cancel"])
def test_retry_send_failure_cannot_retain_session_or_capacity(failure):
    async def scenario():
        app, transport, server = mounted()
        stream = ASGIRequest(app)
        original_send = stream.send
        entered = asyncio.Event()

        async def fail_retry(message):
            if b"retry:" in message.get("body", b""):
                entered.set()
                if failure == "send_error":
                    raise OSError("isolated closed connection")
                await asyncio.Event().wait()
            await original_send(message)

        stream.send = fail_retry
        await stream.open()
        await asyncio.wait_for(entered.wait(), 0.5)
        if failure == "cancel":
            stream.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream.task
        elif failure == "send_error":
            with pytest.raises(ExceptionGroup, match="TaskGroup"):
                await stream.task
        else:
            await asyncio.wait_for(stream.task, 0.5)
        assert app.active == transport.active_sessions == 0
        server.assert_streams_closed()
        assert (await post(app, stream.endpoint)).status == 404

    asyncio.run(scenario())


def test_python_sdk_ignores_retry_control_event_without_jsonrpc_parse_error(caplog):
    async def scenario():
        app, transport, _ = mounted()
        async with asyncio.timeout(3), loopback_server(app) as base:
            async with sse_client(base + "/mcp", timeout=1, sse_read_timeout=1) as (reader, _):
                with pytest.raises(anyio.EndOfStream):
                    await reader.receive()
            assert app.active == transport.active_sessions == 0
        assert "Error parsing server message" not in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, 999, 3600001, True, 1000.5, "300000", None, float("inf")])
def test_transport_rejects_invalid_initialization_retry(value):
    with pytest.raises(ValueError, match="retry"):
        mounted(initialize_retry_milliseconds=value)


@pytest.mark.parametrize("value", ["0", "999", "3600001", "1.5", "nan"])
def test_settings_reject_invalid_initialization_retry(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JWT_SECRET="isolated", MCP_INITIALIZE_RETRY_MILLISECONDS=value)


@pytest.mark.parametrize("value", [1000, 60000, 3600000])
def test_retry_setting_from_environment_reaches_sse(monkeypatch, value):
    monkeypatch.setenv("MCP_INITIALIZE_RETRY_MILLISECONDS", str(value))
    settings = Settings(_env_file=None, JWT_SECRET="isolated")

    async def scenario():
        app, _, _ = mounted(initialize_retry_milliseconds=settings.mcp_initialize_retry_milliseconds)
        stream = await ASGIRequest(app).open()
        await asyncio.wait_for(stream.task, 0.5)
        assert retry_frames(stream) == [f"event: mcp-retry\r\nretry: {value}\r\n\r\n".encode()]

    asyncio.run(scenario())
