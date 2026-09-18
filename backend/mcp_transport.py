"""Legacy MCP SSE with bounded request intake and deterministic session cleanup.

Tool discovery and execution remain owned by the installed MCP server. These
routes retain the existing SSE protocol without patching installed packages.
"""

import logging
import math
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import UUID, uuid4

import anyio
from anyio.abc import ObjectReceiveStream
from anyio.streams.memory import MemoryObjectSendStream
from fastapi import FastAPI, Request
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from mcp.types import JSONRPCError, JSONRPCMessage, JSONRPCRequest, JSONRPCResponse
from pydantic import ValidationError
from sse_starlette import EventSourceResponse
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("openecon.mcp_transport")
RETRY_HEADERS = {"Retry-After": "5", "Cache-Control": "no-store"}


class MCPPostBodyLimitMiddleware:
    """Bound concurrent POST requests, including buffering before request logs.

    The allowance lasts through the inner ASGI request. It does not count tool
    tasks that the protocol server continues running after HTTP 202 delivery.
    Like the application's stream allowance, this counter is per process.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int = 1024 * 1024,
        read_timeout: float = 10.0,
        max_concurrent_requests: int = 32,
    ) -> None:
        if max_body_bytes <= 0 or read_timeout <= 0 or max_concurrent_requests <= 0:
            raise ValueError("MCP POST limits must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.read_timeout = read_timeout
        self.max_concurrent_requests = max_concurrent_requests
        self.active_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith("/mcp")
        ):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_size = int(value)
            except ValueError:
                continue  # Actual received bytes are always checked below.
            if declared_size > self.max_body_bytes:
                await JSONResponse(
                    {"detail": "MCP message body is too large."},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return

        if self.active_requests >= self.max_concurrent_requests:
            await JSONResponse(
                {"detail": "MCP message capacity reached. Retry shortly."},
                status_code=503,
                headers=RETRY_HEADERS,
            )(scope, receive, send)
            return

        self.active_requests += 1
        try:
            buffer = bytearray()
            size = 0
            try:
                with anyio.fail_after(self.read_timeout):
                    while True:
                        frame = await receive()
                        if frame["type"] == "http.disconnect":
                            return
                        chunk = frame.get("body", b"")
                        size += len(chunk)
                        if size > self.max_body_bytes:
                            break
                        buffer.extend(chunk)
                        if not frame.get("more_body", False):
                            break
            except TimeoutError:
                await JSONResponse(
                    {"detail": "MCP message body read timed out."},
                    status_code=408,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return

            if size > self.max_body_bytes:
                await JSONResponse(
                    {"detail": "MCP message body is too large."},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return

            body = bytes(buffer)
            buffer.clear()
            replayed = False

            async def replay_receive():
                nonlocal body, replayed
                if not replayed:
                    replayed = True
                    frame = {"type": "http.request", "body": body, "more_body": False}
                    body = b""
                    return frame
                return await receive()

            await self.app(scope, replay_receive, send)
        finally:
            self.active_requests -= 1


class _SessionResponse(Response):
    def __init__(self, transport, endpoint: str):
        super().__init__(content=b"", media_type="text/event-stream")
        self.transport = transport
        self.endpoint = endpoint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.transport.serve(scope, receive, send, self.endpoint)


@dataclass
class _Delivery:
    item: SessionMessage
    delivered: bool = False
    abandoned: bool = False


@dataclass
class _Session:
    writer: MemoryObjectSendStream
    opened_at: float
    last_activity: float
    initialized: bool = False
    expired: bool = False
    expiry_reason: str | None = None
    pending: dict = field(default_factory=dict)
    changed: anyio.Event = field(default_factory=anyio.Event)


class _DeliveryReader(ObjectReceiveStream[SessionMessage]):
    """Separate accepted SDK requests from cancelled HTTP enqueue attempts."""

    def __init__(self, reader, session: _Session):
        self.reader = reader
        self.session = session

    async def receive(self) -> SessionMessage:
        while True:
            delivery = await self.reader.receive()
            if self.session.expired:
                raise anyio.EndOfStream
            if delivery.abandoned:
                continue
            delivery.delivered = True
            self.session.last_activity = anyio.current_time()
            self.session.changed.set()
            return delivery.item

    async def aclose(self) -> None:
        await self.reader.aclose()

    def statistics(self):
        return self.reader.statistics()


class LegacySSETransport:
    def __init__(
        self, server, *, post_send_timeout: float = 5.0,
        initialize_timeout: float = 30.0, idle_timeout: float = 300.0,
        initialize_retry_milliseconds: int = 300000,
    ) -> None:
        if any(not math.isfinite(value) or value <= 0 for value in (
            post_send_timeout, initialize_timeout, idle_timeout,
        )):
            raise ValueError("MCP timeouts must be finite and positive")
        if (
            type(initialize_retry_milliseconds) is not int
            or not 1000 <= initialize_retry_milliseconds <= 3600000
        ):
            raise ValueError("MCP initialization retry must be an integer from 1000 to 3600000 milliseconds")
        self.server = server
        self.post_send_timeout = post_send_timeout
        self.initialize_timeout = initialize_timeout
        self.idle_timeout = idle_timeout
        self.initialize_retry_milliseconds = initialize_retry_milliseconds
        self._sessions = {}

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    def mount(self, app: FastAPI, path: str = "/mcp", dependencies=None) -> None:
        path = "/" + path.strip("/")
        endpoint = path + "/messages/"

        @app.get(path, include_in_schema=False, operation_id="mcp_connection", dependencies=dependencies)
        async def connect(request: Request):
            return _SessionResponse(self, endpoint)

        @app.post(endpoint, include_in_schema=False, operation_id="mcp_messages", dependencies=dependencies)
        async def message(request: Request):
            return await self.post_message(request)

    async def serve(self, scope: Scope, receive: Receive, send: Send, endpoint: str) -> None:
        incoming_writer, incoming_reader = anyio.create_memory_object_stream[_Delivery](0)
        outgoing_writer, outgoing_reader = anyio.create_memory_object_stream[SessionMessage](0)
        session_id = uuid4()
        opened_at = anyio.current_time()
        session = _Session(incoming_writer, opened_at, opened_at)
        self._sessions[session_id] = session
        logger.info("MCP session opened active=%d", self.active_sessions)
        try:
            message_path = quote(scope.get("root_path", "").rstrip("/") + endpoint)

            async def events():
                yield {"event": "endpoint", "data": f"{message_path}?session_id={session_id.hex}"}
                async for item in outgoing_reader:
                    message = item.message.root
                    pending = session.pending.get(message.id) if isinstance(
                        message, (JSONRPCResponse, JSONRPCError)
                    ) else None
                    yield {
                        "event": "message",
                        "data": item.message.model_dump_json(by_alias=True, exclude_none=True),
                    }
                    # The iterator resumes only after ASGI send completed.
                    if pending is not None and session.pending.get(message.id) is pending:
                        session.pending.pop(message.id)
                        if isinstance(message, JSONRPCResponse) and pending.item.message.root.method == "initialize":
                            session.initialized = True
                        session.last_activity = anyio.current_time()
                        session.changed.set()
                if session.expiry_reason == "initialize_timeout":
                    # A named control event avoids an empty JSON-RPC message in Python clients.
                    yield {"event": "mcp-retry", "retry": self.initialize_retry_milliseconds}

            async with anyio.create_task_group() as group:
                response_finished = anyio.Event()

                async def run_server():
                    try:
                        await self.server.run(
                            _DeliveryReader(incoming_reader, session),
                            outgoing_writer,
                            self.server.create_initialization_options(
                                notification_options=None, experimental_capabilities={}
                            ),
                            raise_exceptions=False,
                        )
                    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                        if not session.expired:
                            raise
                    finally:
                        if not session.expired:
                            group.cancel_scope.cancel()

                async def run_response():
                    try:
                        await EventSourceResponse(events(), ping=15)(scope, receive, send)
                    finally:
                        response_finished.set()
                        group.cancel_scope.cancel()

                async def watch_lease():
                    while True:
                        if not session.initialized:
                            deadline = session.opened_at + self.initialize_timeout
                            reason = "initialize_timeout"
                        elif session.pending:
                            deadline = None
                        else:
                            deadline = session.last_activity + self.idle_timeout
                            reason = "idle_timeout"
                        remaining = None if deadline is None else deadline - anyio.current_time()
                        if remaining is not None and remaining <= 0:
                            session.expired = True
                            session.expiry_reason = reason
                            self._sessions.pop(session_id, None)
                            logger.info("MCP session expired reason=%s active=%d", reason, self.active_sessions)
                            # Finish SSE normally before cancelling the protocol task.
                            outgoing_writer.close()
                            with anyio.move_on_after(self.post_send_timeout):
                                await response_finished.wait()
                            group.cancel_scope.cancel()
                            return
                        changed = session.changed
                        with anyio.move_on_after(remaining):
                            await changed.wait()
                        if changed.is_set():
                            session.changed = anyio.Event()

                group.start_soon(run_server)
                group.start_soon(run_response)
                group.start_soon(watch_lease)
        finally:
            if self._sessions.get(session_id) is session:
                self._sessions.pop(session_id)
            # Synchronous closes work even inside an already-cancelled scope.
            for stream in (incoming_writer, incoming_reader, outgoing_writer, outgoing_reader):
                stream.close()
            logger.info("MCP session closed active=%d", self.active_sessions)

    async def post_message(self, request: Request) -> Response:
        value = request.query_params.get("session_id")
        if value is None:
            return JSONResponse({"detail": "session_id is required"}, status_code=400)
        try:
            session_id = UUID(hex=value)
        except ValueError:
            return JSONResponse({"detail": "Invalid session ID"}, status_code=400)
        session = self._sessions.get(session_id)
        if session is None:
            return JSONResponse({"detail": "Could not find session"}, status_code=404)

        try:
            message = JSONRPCMessage.model_validate_json(await request.body())
        except ValidationError:
            return JSONResponse({"error": "Could not parse message"}, status_code=400)

        if self._sessions.get(session_id) is not session or session.expired:
            return JSONResponse({"detail": "Could not find session"}, status_code=404)

        item = SessionMessage(message, metadata=ServerMessageMetadata(request_context=request))
        delivery = _Delivery(item)
        request_id = message.root.id if isinstance(message.root, JSONRPCRequest) else None
        if request_id is not None:
            if request_id in session.pending:
                return JSONResponse({"detail": "Request ID is already pending"}, status_code=400)
            session.pending[request_id] = delivery
            session.changed.set()
        accepted = False
        try:
            with anyio.fail_after(self.post_send_timeout):
                await session.writer.send(delivery)
            accepted = True
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            if self._sessions.get(session_id) is session:
                self._sessions.pop(session_id)
            return JSONResponse({"detail": "Could not find session"}, status_code=404)
        except TimeoutError:
            return JSONResponse(
                {"detail": "MCP message queue is busy. Retry shortly."},
                status_code=503,
                headers=RETRY_HEADERS,
            )
        finally:
            if not accepted and not delivery.delivered:
                delivery.abandoned = True
                if request_id is not None and session.pending.get(request_id) is delivery:
                    session.pending.pop(request_id)
                    session.changed.set()
        if self._sessions.get(session_id) is not session or session.expired:
            return JSONResponse({"detail": "Could not find session"}, status_code=404)
        return JSONResponse({"message": "Accepted"}, status_code=202)
