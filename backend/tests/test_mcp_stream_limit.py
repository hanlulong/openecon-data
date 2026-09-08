"""Exercise concurrent response lifetimes without importing the production app."""

import asyncio

import pytest
from starlette.responses import PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

from backend.config import Settings
from backend.mcp_stream_limit import MCPStreamLimitMiddleware


class Connection:
    def __init__(self, app, client="192.0.2.1", method="GET", path="/mcp"):
        self.app = app
        self.scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": (client, 12345) if client else None,
        }
        self.messages = []
        self.incoming = asyncio.Queue()
        self.started = asyncio.Event()

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "http.response.start":
            self.started.set()

    async def run(self):
        await self.app(self.scope, self.incoming.get, self.send)

    async def open(self):
        self.task = asyncio.create_task(self.run())
        await asyncio.wait_for(self.started.wait(), 1)
        return self

    async def close(self):
        await self.incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 1)

    @property
    def status(self):
        return self.messages[0]["status"]


class StreamingApp:
    def __init__(self):
        self.streams_started = 0

    async def __call__(self, scope, receive, send):
        if scope["method"] == "GET" and scope["path"] in ("/mcp", "/mcp/"):
            self.streams_started += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b": ping\n\n", "more_body": True})
            while (await receive())["type"] != "http.disconnect":
                pass
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await PlainTextResponse("ok")(scope, receive, send)


def make_limit(app=None, maximum=2, per_client=2, resolver=None):
    return MCPStreamLimitMiddleware(
        app or StreamingApp(),
        maximum,
        per_client,
        resolver or (lambda request: request.client.host if request.client else None),
    )


def test_saturation_rejects_promptly_preserves_messages_and_reuses_released_slot():
    async def scenario():
        inner = StreamingApp()
        app = make_limit(inner)
        first, second = await asyncio.gather(Connection(app).open(), Connection(app).open())
        assert app.active == 2
        rejected = Connection(app)
        await asyncio.wait_for(rejected.run(), 0.2)
        assert rejected.status == 503
        assert inner.streams_started == 2
        headers = dict(rejected.messages[0]["headers"])
        assert headers[b"retry-after"] == b"30"
        assert headers[b"cache-control"] == b"no-store"
        for method, path in [("POST", "/mcp/messages/"), ("POST", "/mcp"), ("GET", "/api/health")]:
            request = Connection(app, method=method, path=path)
            await asyncio.wait_for(request.run(), 0.2)
            assert request.status == 200
        assert app.active == 2
        await first.close()
        replacement = await Connection(app).open()
        assert replacement.status == 200
        await asyncio.gather(second.close(), replacement.close())
        assert app.active == 0
        assert app.active_by_client == {}

    asyncio.run(scenario())


def test_simultaneous_openings_cannot_exceed_global_limit():
    async def scenario():
        app = make_limit(maximum=3, per_client=3)
        requests = await asyncio.gather(*(Connection(app, client=f"192.0.2.{i}").open() for i in range(20)))
        assert sum(request.status == 200 for request in requests) == 3
        assert app.active == 3
        assert app.rejected_global == 17
        await asyncio.gather(*(request.close() for request in requests))
        assert app.active == 0
        assert app.active_by_client == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("client", ["192.0.2.1", "127.0.0.1", "::1", None])
def test_per_client_allowance_includes_loopback_and_unknown_clients(client):
    async def scenario():
        app = make_limit(maximum=3, per_client=1)
        first = await Connection(app, client=client).open()
        rejected = Connection(app, client=client)
        await asyncio.wait_for(rejected.run(), 0.2)
        assert rejected.status == 429
        other = await Connection(app, client="198.51.100.1").open()
        assert other.status == 200
        await asyncio.gather(first.close(), other.close())
        assert app.active_by_client == {}

    asyncio.run(scenario())


def test_supplied_ip_resolver_controls_client_bucket():
    async def scenario():
        seen = []

        def resolver(request):
            seen.append(request.headers.get("x-forwarded-for"))
            return "resolved-client"

        app = make_limit(maximum=2, per_client=1, resolver=resolver)
        first = await Connection(app).open()
        second = Connection(app, client="198.51.100.1")
        second.scope["headers"] = [(b"x-forwarded-for", b"spoofed-client")]
        await second.run()
        assert second.status == 429
        assert seen == [None, "spoofed-client"]
        await first.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_disconnect_cycles_and_cancellation_release_capacity(path):
    async def scenario():
        app = make_limit(maximum=1, per_client=1)
        for _ in range(5):
            stream = await Connection(app, path=path).open()
            await stream.close()
            assert app.active == 0
            assert app.active_by_client == {}
        stream = await Connection(app, path=path).open()
        stream.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stream.task
        assert app.active == 0
        replacement = await Connection(app, path=path).open()
        await replacement.close()
        assert app.active_by_client == {}

    asyncio.run(scenario())


def test_downstream_failure_releases_capacity_and_propagates():
    async def scenario():
        attempts = 0

        async def failing_once(scope, receive, send):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transport failed")
            await PlainTextResponse("recovered")(scope, receive, send)

        app = make_limit(failing_once, maximum=1, per_client=1)
        with pytest.raises(RuntimeError, match="transport failed"):
            await Connection(app).run()
        assert app.active == 0
        assert app.active_by_client == {}
        replacement = Connection(app)
        await replacement.run()
        assert replacement.status == 200

    asyncio.run(scenario())


def test_send_failure_releases_capacity():
    async def scenario():
        app = make_limit(maximum=1, per_client=1)
        request = Connection(app)

        async def broken_send(message):
            raise OSError("client connection lost")

        with pytest.raises(OSError, match="client connection lost"):
            await app(request.scope, request.incoming.get, broken_send)
        assert app.active == 0
        assert app.active_by_client == {}
        replacement = await Connection(app).open()
        await replacement.close()

    asyncio.run(scenario())


def test_saturated_response_keeps_cors_and_retry_headers():
    async def scenario():
        gate = make_limit(maximum=1, per_client=1)
        app = CORSMiddleware(
            gate,
            allow_origins=["https://openecon.ai"],
            allow_credentials=True,
            expose_headers=["Retry-After"],
        )
        stream = await Connection(app).open()
        rejected = Connection(app)
        rejected.scope["headers"] = [(b"origin", b"https://openecon.ai")]
        await rejected.run()
        assert rejected.status == 503
        headers = dict(rejected.messages[0]["headers"])
        assert headers[b"access-control-allow-origin"] == b"https://openecon.ai"
        assert headers[b"access-control-expose-headers"] == b"Retry-After"
        await stream.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", [{"type": "lifespan"}, {"type": "websocket"}])
def test_non_http_scope_passes_through(scope):
    async def scenario():
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope)

        app = make_limit(inner)
        await app(scope, None, None)
        assert seen == [scope]
        assert app.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("method,path", [("HEAD", "/mcp"), ("GET", "/mcp-other"), ("GET", "/mcp/messages/")])
def test_unrelated_routes_do_not_take_stream_slots(method, path):
    async def scenario():
        app = make_limit(maximum=1, per_client=1)
        stream = await Connection(app).open()
        request = Connection(app, method=method, path=path)
        await asyncio.wait_for(request.run(), 0.2)
        assert request.status == 200
        assert app.active == 1
        await stream.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("maximum,per_client", [(0, 1), (1, 0), (-1, 1), (1, 2)])
def test_invalid_middleware_limits_fail_before_serving(maximum, per_client):
    with pytest.raises(ValueError):
        make_limit(maximum=maximum, per_client=per_client)


@pytest.mark.parametrize("maximum,per_client", [(0, 1), (1, 0), (-1, 1), (1, 2)])
def test_invalid_config_limits_fail_at_startup(maximum, per_client):
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            JWT_SECRET="test-only-secret",
            OPENROUTER_API_KEY="test-only-key",
            MCP_MAX_ACTIVE_STREAMS=maximum,
            MCP_MAX_STREAMS_PER_CLIENT=per_client,
        )
