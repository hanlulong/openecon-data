"""Finite legacy SSE sessions, using isolated ASGI clients and local SDK tools."""

import asyncio
from datetime import timedelta
import json

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.message import SessionMessage
from mcp.types import ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from backend.mcp_stream_limit import MCPStreamLimitMiddleware
from backend.mcp_transport import LegacySSETransport
from backend.tests.test_mcp_transport import ASGIRequest, ControlledServer, loopback_server, post


class ReplyServer(ControlledServer):
    async def run(self, reader, writer, options, **kwargs):
        self.streams.append((reader, writer))
        async for item in reader:
            self.messages.append(item)
            self.received.set()
            message = item.message.root
            if isinstance(message, JSONRPCRequest):
                await writer.send(SessionMessage(JSONRPCMessage(JSONRPCResponse(
                    jsonrpc="2.0", id=message.id, result={},
                ))))


def leased(server=None, initialize_timeout=0.08, idle_timeout=0.12, enqueue_timeout=0.03):
    server = server or ReplyServer()
    app = FastAPI()
    transport = LegacySSETransport(
        server, post_send_timeout=enqueue_timeout,
        initialize_timeout=initialize_timeout, idle_timeout=idle_timeout,
    )
    transport.mount(app)
    limiter = MCPStreamLimitMiddleware(app, 2, 2, lambda request: request.client.host)
    return limiter, transport, server


def rpc(method, request_id=1, **params):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    return json.dumps(message).encode()


async def wait_response(stream, request_id=1):
    async with asyncio.timeout(1):
        while True:
            for message in stream.messages:
                for line in message.get("body", b"").decode().splitlines():
                    if line.startswith("data: {"):
                        data = json.loads(line[6:])
                        if data.get("id") == request_id:
                            return data
            await asyncio.sleep(0.001)


def assert_clean_eof(stream):
    assert stream.messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


def test_uninitialized_expiry_releases_capacity_with_clean_eof_and_old_post_404():
    async def scenario():
        app, transport, server = leased()
        stream = await ASGIRequest(app).open()
        endpoint = stream.endpoint
        await asyncio.wait_for(stream.task, 0.6)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0
        assert (await post(app, endpoint)).status == 404
        server.assert_streams_closed()
        fresh = await ASGIRequest(app).open()
        assert transport.active_sessions == 1
        await fresh.close()

    asyncio.run(scenario())


def test_successful_initialize_gets_idle_interval_after_initial_deadline():
    async def scenario():
        app, transport, _ = leased(initialize_timeout=0.05, idle_timeout=0.14)
        stream = await ASGIRequest(app).open()
        assert (await post(app, stream.endpoint, rpc("initialize"))).status == 202
        await wait_response(stream)
        await asyncio.sleep(0.075)
        assert not stream.task.done(), "Successful initialization must replace the initial deadline"
        await asyncio.wait_for(stream.task, 0.5)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("traffic", ["notification", "ping", "initialize_error", "invalid_json"])
def test_activity_without_successful_initialization_cannot_extend_deadline(traffic):
    class InitErrorServer(ReplyServer):
        async def run(self, reader, writer, options, **kwargs):
            async for item in reader:
                if isinstance(item.message.root, JSONRPCRequest):
                    await writer.send(SessionMessage(JSONRPCMessage(JSONRPCError(
                        jsonrpc="2.0", id=item.message.root.id,
                        error=ErrorData(code=-32602, message="isolated invalid parameters"),
                    ))))

    async def scenario():
        app, transport, _ = leased(
            InitErrorServer() if traffic == "initialize_error" else ReplyServer(),
            initialize_timeout=0.09, idle_timeout=0.8,
        )
        stream = await ASGIRequest(app).open()
        for _ in range(3):
            body = {
                "notification": rpc("notifications/initialized", None),
                "ping": rpc("ping"),
                "initialize_error": rpc("initialize"),
                "invalid_json": b"invalid-json",
            }[traffic]
            response = await post(app, stream.endpoint, body)
            assert response.status == (400 if traffic == "invalid_json" else 202)
            await asyncio.sleep(0.02)
        await asyncio.wait_for(stream.task, 0.25)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("notification", [False, True])
def test_valid_client_activity_renews_only_its_initialized_session(notification):
    async def scenario():
        app, transport, _ = leased(initialize_timeout=0.6, idle_timeout=0.1)
        active = await ASGIRequest(app).open()
        idle = await ASGIRequest(app).open()
        for stream in (active, idle):
            await post(app, stream.endpoint, rpc("initialize"))
            await wait_response(stream)
        for request_id in (2, 3, 4):
            await asyncio.sleep(0.055)
            body = rpc("notifications/initialized", None) if notification else rpc("ping", request_id)
            assert (await post(app, active.endpoint, body)).status == 202
            if not notification:
                await wait_response(active, request_id)
            assert not active.task.done()
        assert idle.task.done()
        assert_clean_eof(idle)
        assert transport.active_sessions == app.active == 1
        await asyncio.wait_for(active.task, 0.4)
        assert_clean_eof(active)

    asyncio.run(scenario())


class HoldingServer(ReplyServer):
    async def run(self, reader, writer, options, **kwargs):
        self.streams.append((reader, writer))

        async def reply(item):
            message = item.message.root
            if not isinstance(message, JSONRPCRequest):
                return
            if message.method == "tools/call":
                self.received.set()
                await self.release.wait()
            await writer.send(SessionMessage(JSONRPCMessage(JSONRPCResponse(
                jsonrpc="2.0", id=message.id, result={},
            ))))

        async with anyio.create_task_group() as group:
            async for item in reader:
                self.messages.append(item)
                group.start_soon(reply, item)


def test_long_call_and_duplicate_id_preserve_pending_until_response_then_idle():
    async def scenario():
        server = HoldingServer()
        app, transport, _ = leased(server, initialize_timeout=0.1, idle_timeout=0.08)
        stream = await ASGIRequest(app).open()
        await post(app, stream.endpoint, rpc("initialize"))
        await wait_response(stream)
        assert (await post(app, stream.endpoint, rpc("tools/call", "long"))).status == 202
        await asyncio.wait_for(server.received.wait(), 0.3)
        assert (await post(app, stream.endpoint, rpc("tools/call", "long"))).status == 400
        await asyncio.sleep(0.2)
        assert not stream.task.done(), "An accepted long call must outlive the idle lease"
        assert len(server.messages) == 2
        server.release.set()
        await wait_response(stream, "long")
        await asyncio.sleep(0.035)
        assert not stream.task.done(), "Response completion starts a fresh idle interval"
        await asyncio.wait_for(stream.task, 0.3)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


def test_response_send_must_finish_before_initialization_or_idle_is_completed():
    async def scenario():
        app, transport, _ = leased(initialize_timeout=0.08, idle_timeout=0.4, enqueue_timeout=0.02)
        stream = ASGIRequest(app)
        original_send = stream.send
        blocked = asyncio.Event()

        async def block_response(message):
            if b'"result"' in message.get("body", b""):
                blocked.set()
                await asyncio.Event().wait()
            await original_send(message)

        stream.send = block_response
        await stream.open()
        await post(app, stream.endpoint, rpc("initialize"))
        await asyncio.wait_for(blocked.wait(), 0.3)
        # A blocked initialization response cannot evade the initial deadline.
        await asyncio.wait_for(stream.task, 0.4)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


def test_idle_expiry_with_blocked_eof_has_bounded_close_grace():
    async def scenario():
        app, transport, _ = leased(initialize_timeout=0.06, enqueue_timeout=0.025)
        stream = ASGIRequest(app)
        original_send = stream.send
        final_started = asyncio.Event()

        async def block_eof(message):
            if message["type"] == "http.response.body" and message.get("more_body") is False:
                final_started.set()
                await asyncio.Event().wait()
            await original_send(message)

        stream.send = block_eof
        await stream.open()
        await asyncio.wait_for(stream.task, 0.3)
        assert final_started.is_set()
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_failed_enqueue_before_handoff_does_not_pin_session(failure):
    async def scenario():
        server = ControlledServer(consume=False)
        app, transport, _ = leased(server, initialize_timeout=0.1, enqueue_timeout=0.025)
        stream = await ASGIRequest(app).open()
        request = ASGIRequest(app, "POST", stream.endpoint, body=rpc("initialize"))
        task = asyncio.create_task(request.run())
        if failure == "cancel":
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 0.2)
            assert request.status == 503
        assert not next(iter(transport._sessions.values())).pending
        await asyncio.wait_for(stream.task, 0.3)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_enqueue_failure_after_handoff_still_protects_accepted_call(monkeypatch, failure):
    async def scenario():
        server = HoldingServer()
        app, transport, _ = leased(server, initialize_timeout=0.2, idle_timeout=0.07, enqueue_timeout=0.025)
        stream = await ASGIRequest(app).open()
        await post(app, stream.endpoint, rpc("initialize"))
        await wait_response(stream)
        session = next(iter(transport._sessions.values()))
        original_send = session.writer.send

        async def pause_after_handoff(item):
            await original_send(item)
            await server.received.wait()
            await asyncio.Event().wait()

        monkeypatch.setattr(session.writer, "send", pause_after_handoff)
        request = ASGIRequest(app, "POST", stream.endpoint, body=rpc("tools/call", 2))
        task = asyncio.create_task(request.run())
        await asyncio.wait_for(server.received.wait(), 0.2)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 0.2)
            assert request.status == 503
        await asyncio.sleep(0.16)
        assert not stream.task.done()
        server.release.set()
        await wait_response(stream, 2)
        await asyncio.wait_for(stream.task, 0.3)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["post_send_timeout", "initialize_timeout", "idle_timeout"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_transport_rejects_nonpositive_or_nonfinite_timeouts(name, value):
    with pytest.raises(ValueError, match="finite and positive"):
        LegacySSETransport(ReplyServer(), **{name: value})


def test_installed_sdk_long_call_idle_eof_and_fresh_session_tool_recovery():
    async def scenario():
        app = FastAPI()
        calls = []

        @app.get("/echo", operation_id="lease_echo")
        async def echo(value: str, delay: float = 0):
            calls.append(value)
            await asyncio.sleep(delay)
            return {"value": value}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as internal:
            mcp = FastApiMCP(app, include_operations=["lease_echo"], http_client=internal)
            transport = LegacySSETransport(mcp.server, initialize_timeout=0.5, idle_timeout=0.12)
            transport.mount(app)
            limiter = MCPStreamLimitMiddleware(app, 2, 2, lambda request: request.client.host)
            async with asyncio.timeout(8), loopback_server(limiter) as base:
                for value in ("long-before-expiry", "fresh-after-expiry"):
                    async with sse_client(base + "/mcp", timeout=1, sse_read_timeout=1) as streams:
                        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=1)) as client:
                            await client.initialize()
                            assert [tool.name for tool in (await client.list_tools()).tools] == ["lease_echo"]
                            result = await client.call_tool("lease_echo", {"value": value, "delay": 0.28})
                            assert not result.isError
                            assert any(value in block.text for block in result.content if hasattr(block, "text"))
                            async with asyncio.timeout(0.7):
                                while transport.active_sessions or limiter.active:
                                    await asyncio.sleep(0.005)
                            # Legacy SDK does not create another connection automatically.
                            await asyncio.sleep(0.02)
                            assert transport.active_sessions == limiter.active == 0
                assert calls == ["long-before-expiry", "fresh-after-expiry"]

    asyncio.run(scenario())


def test_actual_sdk_cancellation_and_invalid_request_error_clear_pending():
    async def scenario():
        app = FastAPI()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        @app.get("/block", operation_id="lease_block")
        async def block():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as internal:
            mcp = FastApiMCP(app, include_operations=["lease_block"], http_client=internal)
            transport = LegacySSETransport(mcp.server, initialize_timeout=0.5, idle_timeout=0.08)
            transport.mount(app)
            limiter = MCPStreamLimitMiddleware(app, 2, 2, lambda request: request.client.host)
            stream = await ASGIRequest(limiter).open()
            await post(limiter, stream.endpoint, rpc(
                "initialize", protocolVersion="2024-11-05", capabilities={},
                clientInfo={"name": "isolated-lease-test", "version": "1"},
            ))
            assert "result" in await wait_response(stream)
            await post(limiter, stream.endpoint, rpc("notifications/initialized", None))
            await post(limiter, stream.endpoint, rpc("tools/call", 2, name="lease_block", arguments={}))
            await asyncio.wait_for(started.wait(), 0.5)
            await asyncio.sleep(0.12)
            assert not stream.task.done()
            await post(limiter, stream.endpoint, rpc("notifications/cancelled", None, requestId=2))
            assert "error" in await wait_response(stream, 2)
            await asyncio.wait_for(cancelled.wait(), 0.5)
            await post(limiter, stream.endpoint, rpc("nonexistent/method", 3))
            assert "error" in await wait_response(stream, 3)
            await asyncio.wait_for(stream.task, 0.4)
            assert_clean_eof(stream)
            assert transport.active_sessions == limiter.active == 0

    asyncio.run(scenario())


def test_server_sse_heartbeats_do_not_renew_initialized_idle_lease(monkeypatch):
    import backend.mcp_transport as module

    original_response = module.EventSourceResponse

    def frequent_heartbeat(content, **kwargs):
        return original_response(content, **{**kwargs, "ping": 0.01})

    monkeypatch.setattr(module, "EventSourceResponse", frequent_heartbeat)

    async def scenario():
        app, transport, _ = leased(idle_timeout=0.08)
        stream = await ASGIRequest(app).open()
        await post(app, stream.endpoint, rpc("initialize"))
        await wait_response(stream)
        await asyncio.wait_for(stream.task, 0.3)
        assert any(message.get("body", b"").startswith(b": ping") for message in stream.messages)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


def test_pending_response_send_protects_call_and_completion_starts_idle_interval():
    async def scenario():
        app, transport, _ = leased(initialize_timeout=0.3, idle_timeout=0.07)
        stream = ASGIRequest(app)
        original_send = stream.send
        unblock = asyncio.Event()
        entered = asyncio.Event()

        async def delayed_response(message):
            if b'"id":2' in message.get("body", b""):
                entered.set()
                await unblock.wait()
            await original_send(message)

        stream.send = delayed_response
        await stream.open()
        await post(app, stream.endpoint, rpc("initialize"))
        await wait_response(stream)
        await post(app, stream.endpoint, rpc("ping", 2))
        await asyncio.wait_for(entered.wait(), 0.3)
        await asyncio.sleep(0.16)
        assert not stream.task.done()
        unblock.set()
        await wait_response(stream, 2)
        await asyncio.sleep(0.025)
        assert not stream.task.done()
        await asyncio.wait_for(stream.task, 0.3)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


def test_response_before_post_returns_does_not_recreate_completed_pending_id(monkeypatch):
    async def scenario():
        app, transport, _ = leased(initialize_timeout=0.3, idle_timeout=0.1, enqueue_timeout=0.3)
        stream = await ASGIRequest(app).open()
        session = next(iter(transport._sessions.values()))
        original_send = session.writer.send
        release_post = asyncio.Event()

        async def delayed_post_return(item):
            await original_send(item)
            await release_post.wait()

        monkeypatch.setattr(session.writer, "send", delayed_post_return)
        request = ASGIRequest(app, "POST", stream.endpoint, body=rpc("initialize", 0))
        task = asyncio.create_task(request.run())
        await wait_response(stream, 0)
        assert session.pending == {}
        monkeypatch.setattr(session.writer, "send", original_send)
        assert (await post(app, stream.endpoint, rpc("ping", 0))).status == 202
        release_post.set()
        await asyncio.wait_for(task, 0.2)
        assert request.status == 202
        await asyncio.wait_for(stream.task, 0.3)
        assert_clean_eof(stream)

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["MCP_INITIALIZE_TIMEOUT_SECONDS", "MCP_IDLE_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", "-Infinity"])
def test_settings_reject_invalid_lease_values(key, value):
    from backend.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as rejected:
        Settings(_env_file=None, JWT_SECRET="isolated-test", LLM_PROVIDER="vllm", **{key: value})
    assert any(error["loc"] == (key,) for error in rejected.value.errors())


def test_cancelled_transfer_is_discarded_before_protocol_handoff(monkeypatch):
    class GatedServer(ReplyServer):
        async def run(self, reader, writer, options, **kwargs):
            self.streams.append((reader, writer))
            await self.release.wait()
            await super().run(reader, writer, options, **kwargs)

    async def scenario():
        server = GatedServer()
        app, transport, _ = leased(server, initialize_timeout=0.2, enqueue_timeout=0.1)
        stream = await ASGIRequest(app).open()
        session = next(iter(transport._sessions.values()))
        adapter = server.streams[0][0]
        original_receive = adapter.reader.receive
        original_send = session.writer.send
        transferred = asyncio.Event()
        allow_handoff = asyncio.Event()

        async def pause_received_delivery():
            item = await original_receive()
            transferred.set()
            await allow_handoff.wait()
            return item

        async def pause_sender_completion(item):
            await original_send(item)
            await asyncio.Event().wait()

        monkeypatch.setattr(adapter.reader, "receive", pause_received_delivery)
        monkeypatch.setattr(session.writer, "send", pause_sender_completion)
        server.release.set()
        request = ASGIRequest(app, "POST", stream.endpoint, body=rpc("initialize"))
        task = asyncio.create_task(request.run())
        await asyncio.wait_for(transferred.wait(), 0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        allow_handoff.set()
        await asyncio.sleep(0.02)
        assert server.messages == [], "Cancelled envelopes must not turn into untracked SDK requests"
        assert session.pending == {}
        await asyncio.wait_for(stream.task, 0.4)
        assert_clean_eof(stream)
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


def test_post_body_finishing_after_session_expiry_cannot_enqueue():
    async def scenario():
        app, transport, server = leased(initialize_timeout=0.08)
        stream = await ASGIRequest(app).open()
        request = ASGIRequest(app, "POST", stream.endpoint)
        request.incoming.get_nowait()
        request.incoming.put_nowait({"type": "http.request", "body": b"", "more_body": True})
        task = asyncio.create_task(request.run())
        await asyncio.wait_for(stream.task, 0.3)
        request.incoming.put_nowait({"type": "http.request", "body": rpc("initialize"), "more_body": False})
        await asyncio.wait_for(task, 0.2)
        assert request.status == 404
        assert server.messages == []
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())
