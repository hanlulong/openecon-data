"""Independent legacy SSE regressions; no production app or external requests.

The real SDK test uses one ephemeral loopback listener and a dummy local tool.
Other tests inject ASGI lifecycles directly so failure cleanup is deterministic.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import socket
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi_mcp import FastApiMCP
from mcp import ClientSession
from mcp.client.sse import sse_client
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
import uvicorn

from backend.mcp_stream_limit import MCPStreamLimitMiddleware
from backend.mcp_transport import LegacySSETransport, MCPPostBodyLimitMiddleware


class ASGIRequest:
    def __init__(self, app, method="GET", path="/mcp", headers=(), body=b""):
        parsed = urlsplit(path)
        self.app = app
        self.scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "scheme": "http", "method": method, "path": parsed.path,
            "raw_path": parsed.path.encode(), "root_path": "",
            "query_string": parsed.query.encode(),
            "headers": [(b"host", b"127.0.0.1"), *headers],
            "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 12346),
        }
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})
        self.messages = []
        self.endpoint_ready = asyncio.Event()
        self.endpoint = None
        self.task = None

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "http.response.body":
            for line in message.get("body", b"").decode().splitlines():
                if line.startswith("data: /mcp/messages/"):
                    self.endpoint = line.removeprefix("data: ")
                    self.endpoint_ready.set()

    async def run(self):
        await self.app(self.scope, self.incoming.get, self.send)

    async def open(self):
        self.task = asyncio.create_task(self.run())
        await asyncio.wait_for(self.endpoint_ready.wait(), 1)
        return self

    async def close(self):
        self.incoming.put_nowait({"type": "http.disconnect"})
        if self.task is not None:
            await asyncio.wait_for(self.task, 1)

    @property
    def status(self):
        return next(message["status"] for message in self.messages if message["type"] == "http.response.start")

    @property
    def headers(self):
        return dict(next(message["headers"] for message in self.messages if message["type"] == "http.response.start"))


class ControlledServer:
    """A protocol-server stand-in whose receive and exit behavior tests transport boundaries."""

    def __init__(self, consume=True, fail=False):
        self.consume = consume
        self.fail = fail
        self.release = asyncio.Event()
        self.received = asyncio.Event()
        self.messages = []
        self.streams = []

    def create_initialization_options(self, **kwargs):
        return object()

    async def run(self, reader, writer, options, **kwargs):
        self.streams.append((reader, writer))
        if not self.consume:
            await self.release.wait()
            if self.fail:
                raise RuntimeError("isolated protocol failure")
            return
        async for message in reader:
            self.messages.append(message)
            self.received.set()

    def assert_streams_closed(self):
        for pair in self.streams:
            for stream in pair:
                stats = stream.statistics()
                assert stats.open_send_streams == 0
                assert stats.open_receive_streams == 0


def mounted(server=None, dependencies=None, timeout=0.05):
    server = server or ControlledServer()
    app = FastAPI()
    transport = LegacySSETransport(server, post_send_timeout=timeout)
    transport.mount(app, path="/mcp", dependencies=dependencies)
    limiter = MCPStreamLimitMiddleware(app, 2, 2, lambda request: request.client.host)
    return limiter, transport, server


async def post(app, endpoint, body=None, headers=()):
    request = ASGIRequest(
        app, method="POST", path=endpoint,
        headers=[(b"content-type", b"application/json"), *headers],
        body=body if body is not None else b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    await asyncio.wait_for(request.run(), 1)
    return request


@asynccontextmanager
async def loopback_server(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        app, lifespan="off", access_log=False, log_config=None,
        timeout_graceful_shutdown=1,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(2):
            while not server.started:
                await asyncio.sleep(0.005)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 3)
        listener.close()


def test_real_sdk_initializes_lists_calls_local_tool_and_releases_repeated_sessions():
    async def scenario():
        app = FastAPI()
        calls = []

        @app.get("/audit/echo", operation_id="audit_echo")
        async def echo(value: str, request: Request):
            calls.append((value, request.headers.get("authorization")))
            return {"value": value}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://isolated-test"
        ) as internal_http:
            mcp = FastApiMCP(app, include_operations=["audit_echo"], http_client=internal_http)
            transport = LegacySSETransport(mcp.server)
            transport.mount(app)
            limiter = MCPStreamLimitMiddleware(app, 2, 2, lambda request: request.client.host)
            async with asyncio.timeout(12), loopback_server(limiter) as base:
                for _ in range(3):
                    async with sse_client(
                        base + "/mcp", headers={"Authorization": "Bearer isolated-test"},
                        timeout=2, sse_read_timeout=3,
                    ) as streams:
                        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=2)) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            assert [tool.name for tool in tools.tools] == ["audit_echo"]
                            result = await session.call_tool("audit_echo", arguments={"value": "dummy-result"})
                            assert not result.isError
                            assert any("dummy-result" in block.text for block in result.content if hasattr(block, "text"))
                    async with asyncio.timeout(1):
                        while transport.active_sessions or limiter.active:
                            await asyncio.sleep(0.005)
                assert calls == [("dummy-result", "Bearer isolated-test")] * 3

    asyncio.run(scenario())


def test_disconnect_cycles_remove_session_and_closed_endpoint_returns_404():
    async def scenario():
        app, transport, server = mounted()
        for _ in range(5):
            stream = await ASGIRequest(app).open()
            assert transport.active_sessions == app.active == 1
            endpoint = stream.endpoint
            await stream.close()
            assert transport.active_sessions == app.active == 0
            assert (await post(app, endpoint)).status == 404
        server.assert_streams_closed()

    asyncio.run(scenario())


def test_disconnect_cancels_inflight_local_tool_in_real_sdk_server():
    async def scenario():
        tool_app = FastAPI()
        entered = asyncio.Event()
        finished = asyncio.Event()

        @tool_app.get("/audit/wait", operation_id="audit_wait")
        async def wait_tool():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tool_app), base_url="http://isolated-test"
        ) as internal_http:
            mcp = FastApiMCP(tool_app, include_operations=["audit_wait"], http_client=internal_http)
            app, transport, _ = mounted(mcp.server)
            stream = await ASGIRequest(app).open()
            try:
                initialize = {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "isolated-test", "version": "1"},
                    },
                }
                assert (await post(app, stream.endpoint, json.dumps(initialize).encode())).status == 202
                async with asyncio.timeout(1):
                    while not any(b"protocolVersion" in message.get("body", b"") for message in stream.messages):
                        await asyncio.sleep(0.001)
                assert (await post(app, stream.endpoint, b'{"jsonrpc":"2.0","method":"notifications/initialized"}')).status == 202
                call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "audit_wait", "arguments": {}}}
                assert (await post(app, stream.endpoint, json.dumps(call).encode())).status == 202
                await asyncio.wait_for(entered.wait(), 1)
            finally:
                await stream.close()
            await asyncio.wait_for(finished.wait(), 1)
            assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["send", "cancel", "server", "server_return"])
def test_all_abnormal_exit_paths_remove_session_close_streams_and_release_admission(failure):
    async def scenario():
        server = ControlledServer(consume=False, fail=failure == "server")
        app, transport, _ = mounted(server)
        stream = ASGIRequest(app)
        if failure == "send":
            original_send = stream.send

            async def broken_send(message):
                await original_send(message)
                if message["type"] == "http.response.body" and message.get("body"):
                    raise OSError("isolated send failure")

            stream.send = broken_send
        await stream.open()
        if failure == "cancel":
            stream.task.cancel()
        elif failure in ("server", "server_return"):
            server.release.set()
        if failure == "server_return":
            await asyncio.wait_for(stream.task, 1)
        else:
            with pytest.raises(BaseException) as caught:
                await asyncio.wait_for(stream.task, 1)
            assert not isinstance(caught.value, TimeoutError), "Transport only cleaned up after the test forcibly timed out"

            def has_expected_cause(error):
                expected = {"send": OSError, "cancel": asyncio.CancelledError, "server": RuntimeError}[failure]
                return isinstance(error, expected) or any(has_expected_cause(child) for child in getattr(error, "exceptions", ()))

            assert has_expected_cause(caught.value)
        assert transport.active_sessions == app.active == 0
        server.assert_streams_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [b"\xff", b"{", b"[]", b'{"jsonrpc":"1.0","id":1,"method":"tools/list"}'])
def test_invalid_message_returns_400_without_enqueue_and_keeps_live_session(body):
    async def scenario():
        app, transport, server = mounted()
        stream = await ASGIRequest(app).open()
        try:
            response = await post(app, stream.endpoint, body=body)
            assert response.status == 400
            assert server.messages == []
            assert transport.active_sessions == 1
            assert (await post(app, stream.endpoint)).status == 202
            await asyncio.wait_for(server.received.wait(), 1)
        finally:
            await stream.close()
        assert transport.active_sessions == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("query, expected", [("", 400), ("?session_id=bad-id", 400), (f"?session_id={uuid4().hex}", 404)])
def test_missing_invalid_and_unknown_session_ids_fail_without_starting_session(query, expected):
    async def scenario():
        app, transport, server = mounted()
        response = await post(app, "/mcp/messages/" + query)
        assert response.status == expected
        assert transport.active_sessions == 0
        assert server.messages == []

    asyncio.run(scenario())


def test_enqueue_timeout_returns_retryable_503_and_disconnect_still_cleans_up():
    async def scenario():
        app, transport, server = mounted(ControlledServer(consume=False))
        stream = await ASGIRequest(app).open()
        try:
            response = await post(app, stream.endpoint)
            assert response.status == 503
            assert int(response.headers[b"retry-after"]) > 0
            assert response.headers[b"cache-control"] == b"no-store"
            assert server.messages == []
        finally:
            await stream.close()
        assert transport.active_sessions == app.active == 0
        server.assert_streams_closed()

    asyncio.run(scenario())


def test_disconnect_during_enqueue_returns_404_and_never_accepts_lost_message():
    async def scenario():
        app, transport, server = mounted(ControlledServer(consume=False), timeout=0.5)
        stream = await ASGIRequest(app).open()
        delivery = asyncio.create_task(post(app, stream.endpoint))
        await asyncio.sleep(0.02)
        await stream.close()
        response = await delivery
        assert response.status == 404
        assert transport.active_sessions == 0
        server.assert_streams_closed()

    asyncio.run(scenario())


def test_accepted_message_preserves_http_request_metadata():
    async def scenario():
        app, transport, server = mounted()
        stream = await ASGIRequest(app).open()
        try:
            response = await post(app, stream.endpoint, headers=[(b"authorization", b"Bearer isolated-test")])
            assert response.status == 202
            await asyncio.wait_for(server.received.wait(), 1)
            metadata_request = server.messages[0].metadata.request_context
            assert metadata_request.method == "POST"
            assert metadata_request.url.path == "/mcp/messages/"
            assert metadata_request.headers["authorization"] == "Bearer isolated-test"
        finally:
            await stream.close()
        assert transport.active_sessions == 0

    asyncio.run(scenario())


def test_route_dependencies_gate_both_sse_and_message_post():
    async def scenario():
        async def authorization(request: Request):
            if request.headers.get("authorization") != "Bearer isolated-test":
                raise HTTPException(401, "Unauthorized")

        app, transport, server = mounted(dependencies=[Depends(authorization)])
        rejected = ASGIRequest(app)
        await rejected.run()
        assert rejected.status == 401
        assert transport.active_sessions == 0
        stream = await ASGIRequest(app, headers=[(b"authorization", b"Bearer isolated-test")]).open()
        try:
            assert (await post(app, stream.endpoint)).status == 401
            assert server.messages == []
            assert (await post(app, stream.endpoint, headers=[(b"authorization", b"Bearer isolated-test")])).status == 202
        finally:
            await stream.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("method,path,status", [("HEAD", "/mcp", 405), ("POST", "/mcp", 405), ("GET", "/mcp/", 307), ("GET", "/mcp/messages/", 405), ("GET", "/mcp-other", 404)])
def test_legacy_route_behavior_is_preserved(method, path, status):
    async def scenario():
        app, transport, _ = mounted()
        request = ASGIRequest(app, method=method, path=path)
        await asyncio.wait_for(request.run(), 1)
        assert request.status == status
        assert transport.active_sessions == app.active == 0

    asyncio.run(scenario())


class BodyConsumer:
    def __init__(self):
        self.called = False
        self.body = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.body = await Request(scope, receive).body()
        await PlainTextResponse("ok")(scope, receive, send)


def guarded(consumer, maximum=8, timeout=0.05):
    # The consumer represents the logger's body peek plus the message route.
    return CORSMiddleware(
        MCPPostBodyLimitMiddleware(consumer, max_body_bytes=maximum, read_timeout=timeout),
        allow_origins=["https://openecon.ai"], expose_headers=["Retry-After"],
    )


@pytest.mark.parametrize("chunks", [[b"123456789"], [b"12345", b"6789"]])
def test_body_size_rejection_precedes_logging_for_single_frame_and_chunked_bodies(chunks):
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer), "POST", "/mcp/messages/", headers=[(b"origin", b"https://openecon.ai")])
        request.incoming = asyncio.Queue()
        for index, chunk in enumerate(chunks):
            request.incoming.put_nowait({"type": "http.request", "body": chunk, "more_body": index != len(chunks) - 1})
        await asyncio.wait_for(request.run(), 1)
        assert request.status == 413
        assert not consumer.called
        assert request.headers[b"access-control-allow-origin"] == b"https://openecon.ai"

    asyncio.run(scenario())


def test_content_length_rejection_does_not_wait_for_client_body():
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer), "POST", "/mcp/messages/", headers=[(b"content-length", b"9")])
        request.incoming = asyncio.Queue()
        await asyncio.wait_for(request.run(), 0.2)
        assert request.status == 413
        assert not consumer.called

    asyncio.run(scenario())


def test_body_read_deadline_cannot_interrupt_413_response_and_send_second_status():
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer, timeout=0.01), "POST", "/mcp/messages/", body=b"123456789")
        original_send = request.send

        async def slow_response_send(message):
            await original_send(message)
            if message["type"] == "http.response.start":
                await asyncio.sleep(0.04)

        request.send = slow_response_send
        await asyncio.wait_for(request.run(), 0.3)
        statuses = [message["status"] for message in request.messages if message["type"] == "http.response.start"]
        assert statuses == [413]
        assert not consumer.called

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/mcp", "/mcp/messages/", "/mcp-other"])
def test_total_body_deadline_protects_every_logging_peek_path(path):
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer), "POST", path, headers=[(b"origin", b"https://openecon.ai")])
        request.incoming = asyncio.Queue()
        request.incoming.put_nowait({"type": "http.request", "body": b"a", "more_body": True})
        await asyncio.wait_for(request.run(), 0.3)
        assert request.status == 408
        assert not consumer.called
        assert request.headers[b"access-control-allow-origin"] == b"https://openecon.ai"

    asyncio.run(scenario())


def test_within_limit_chunked_body_is_replayed_byte_for_byte():
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer), "POST", "/mcp/messages/")
        request.incoming = asyncio.Queue()
        request.incoming.put_nowait({"type": "http.request", "body": b"1234", "more_body": True})
        request.incoming.put_nowait({"type": "http.request", "body": b"5678", "more_body": False})
        await asyncio.wait_for(request.run(), 1)
        assert request.status == 200
        assert consumer.body == b"12345678"

    asyncio.run(scenario())


def test_unrelated_upload_is_not_limited_by_mcp_body_policy():
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer), "POST", "/api/upload", body=b"x" * 100)
        await asyncio.wait_for(request.run(), 1)
        assert request.status == 200
        assert consumer.body == b"x" * 100

    asyncio.run(scenario())


def test_body_disconnect_does_not_invoke_logger_or_message_handler():
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer), "POST", "/mcp/messages/")
        request.incoming = asyncio.Queue()
        request.incoming.put_nowait({"type": "http.request", "body": b"a", "more_body": True})
        request.incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(request.run(), 1)
        assert not consumer.called

    asyncio.run(scenario())


def test_body_deadline_is_total_time_even_when_client_keeps_sending_chunks():
    async def scenario():
        consumer = BodyConsumer()
        request = ASGIRequest(guarded(consumer, timeout=0.05), "POST", "/mcp/messages/")
        request.incoming = asyncio.Queue()

        async def trickle():
            for _ in range(5):
                request.incoming.put_nowait({"type": "http.request", "body": b"x", "more_body": True})
                await asyncio.sleep(0.03)

        sender = asyncio.create_task(trickle())
        try:
            await asyncio.wait_for(request.run(), 0.12)
            assert request.status == 408
            assert not consumer.called
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    asyncio.run(scenario())


def test_default_post_cap_holds_completed_body_buffers_until_inner_app_returns():
    async def scenario():
        holds = []

        async def holding_app(scope, receive, send):
            assert await Request(scope, receive).body() == b"12345678"
            release = asyncio.Event()
            holds.append(release)
            await release.wait()
            await PlainTextResponse("ok")(scope, receive, send)

        app = guarded(holding_app)
        requests = [ASGIRequest(app, "POST", "/mcp/messages/", body=b"12345678") for _ in range(32)]
        tasks = [asyncio.create_task(request.run()) for request in requests]

        async def await_holds(count):
            async with asyncio.timeout(1):
                while len(holds) < count:
                    await asyncio.sleep(0.001)

        try:
            await await_holds(32)
            rejected = ASGIRequest(app, "POST", "/mcp/messages/", body=b"12345678", headers=[(b"origin", b"https://openecon.ai")])
            await asyncio.wait_for(rejected.run(), 0.2)
            assert rejected.status == 503
            assert rejected.headers[b"retry-after"] == b"5"
            assert rejected.headers[b"cache-control"] == b"no-store"
            assert rejected.headers[b"access-control-allow-origin"] == b"https://openecon.ai"
            assert len(holds) == 32
            holds[0].set()
            await asyncio.wait_for(tasks[0], 1)
            replacement = ASGIRequest(app, "POST", "/mcp/messages/", body=b"12345678")
            tasks.append(asyncio.create_task(replacement.run()))
            await await_holds(33)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Cancellation of all remaining admitted requests also restores slots.
        fresh = ASGIRequest(app, "POST", "/mcp/messages/", body=b"12345678")
        fresh_task = asyncio.create_task(fresh.run())
        try:
            await await_holds(34)
            holds[-1].set()
            await asyncio.wait_for(fresh_task, 1)
            assert fresh.status == 200
        finally:
            if not fresh_task.done():
                fresh_task.cancel()
                await asyncio.gather(fresh_task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelling_slow_body_read_releases_post_slot():
    async def scenario():
        consumer = BodyConsumer()
        app = MCPPostBodyLimitMiddleware(
            consumer, max_body_bytes=8, read_timeout=1, max_concurrent_requests=1,
        )
        slow = ASGIRequest(app, "POST", "/mcp/messages/")
        slow.incoming = asyncio.Queue()
        reading = asyncio.Event()

        async def receive():
            reading.set()
            return await slow.incoming.get()

        task = asyncio.create_task(app(slow.scope, receive, slow.send))
        await asyncio.wait_for(reading.wait(), 1)
        try:
            rejected = ASGIRequest(app, "POST", "/mcp/messages/", body=b"ok")
            await asyncio.wait_for(rejected.run(), 0.2)
            assert rejected.status == 503
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        healthy = ASGIRequest(app, "POST", "/mcp/messages/", body=b"ok")
        await asyncio.wait_for(healthy.run(), 1)
        assert healthy.status == 200
        assert consumer.body == b"ok"

    asyncio.run(scenario())
