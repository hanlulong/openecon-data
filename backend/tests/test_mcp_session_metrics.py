"""Lifecycle measurements must reflect delivery and never contain client data."""

import asyncio
import logging

import pytest
from mcp.shared.message import SessionMessage
from mcp.types import ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCRequest

from backend.tests.test_mcp_session_lease import ReplyServer, leased, rpc, wait_response
from backend.tests.test_mcp_transport import ASGIRequest, post


LOGGER = "openecon.mcp_transport"
PRIVATE = "private-client-content-not-for-logs"


class InitErrorServer(ReplyServer):
    async def run(self, reader, writer, options, **kwargs):
        self.streams.append((reader, writer))
        async for item in reader:
            message = item.message.root
            if isinstance(message, JSONRPCRequest):
                await writer.send(SessionMessage(JSONRPCMessage(JSONRPCError(
                    jsonrpc="2.0", id=message.id,
                    error=ErrorData(code=-32602, message=PRIVATE),
                ))))


@pytest.mark.parametrize("case", [
    "closed", "initialize_timeout", "idle_timeout", "blocked_initialize", "failed_initialize",
])
def test_close_metrics_follow_delivery_and_preserve_privacy(caplog, case):
    caplog.set_level(logging.INFO, logger=LOGGER)
    captured = {}

    async def scenario():
        app, transport, server = leased(
            InitErrorServer() if case == "failed_initialize" else ReplyServer(),
            initialize_timeout=0.15, idle_timeout=0.08, enqueue_timeout=0.02,
        )
        stream = ASGIRequest(app, headers=[(b"authorization", PRIVATE.encode())])
        stream.scope["client"] = ("192.0.2.67", 12345)
        blocked = asyncio.Event()
        original_send = stream.send

        async def block_response(message):
            if b'"result"' in message.get("body", b""):
                blocked.set()
                await asyncio.Event().wait()
            await original_send(message)

        if case == "blocked_initialize":
            stream.send = block_response
        await stream.open()
        captured["session_id"] = stream.endpoint.split("session_id=")[1]
        session = next(iter(transport._sessions.values()))

        if case != "initialize_timeout":
            await post(app, stream.endpoint, rpc("initialize", PRIVATE, private=PRIVATE))
            if case == "blocked_initialize":
                await asyncio.wait_for(blocked.wait(), 0.3)
                assert session.initialized_at is None
            else:
                await wait_response(stream, PRIVATE)

        if case == "closed":
            # Repeated successful responses must retain the first completion time.
            first = session.initialized_at
            assert first is not None
            await asyncio.sleep(0.005)
            await post(app, stream.endpoint, rpc("initialize", "second-private-id"))
            await wait_response(stream, "second-private-id")
            assert session.initialized_at == first
            await stream.close()
        else:
            await asyncio.wait_for(stream.task, 0.6)

        assert transport.active_sessions == app.active == 0
        server.assert_streams_closed()

    asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    # Existing opened/closed records plus one expiry record, with no added logs.
    assert len(messages) == (2 if case == "closed" else 3)
    closed = [m for m in messages if m.startswith("MCP session closed ")]
    assert len(closed) == 1
    fields = dict(item.split("=", 1) for item in closed[0].split()[3:])
    initialized = case in {"closed", "idle_timeout"}
    assert fields["active"] == "0"
    assert fields["initialized"] == str(initialized)
    assert int(fields["pending_count"]) == (1 if case == "blocked_initialize" else 0)
    lifetime = int(fields["lifetime_ms"])
    initialization = int(fields["initialization_ms"])
    assert lifetime >= 0
    assert 0 <= initialization <= lifetime if initialized else initialization == -1
    expected_reason = "other" if case == "closed" else (
        "idle_timeout" if case == "idle_timeout" else "initialize_timeout"
    )
    assert fields["reason"] == expected_reason
    for value in (PRIVATE, "second-private-id", captured["session_id"], "192.0.2.67"):
        assert all(value not in message for message in messages)


def test_zero_initialization_timestamp_is_not_the_missing_sentinel(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def scenario():
        app, transport, _ = leased(initialize_timeout=0.3, idle_timeout=0.3)
        stream = await ASGIRequest(app).open()
        await post(app, stream.endpoint, rpc("initialize"))
        await wait_response(stream)
        session = next(iter(transport._sessions.values()))
        session.opened_at = session.initialized_at = 0.0
        await stream.close()

    asyncio.run(scenario())
    closed = [r.getMessage() for r in caplog.records
              if r.name == LOGGER and r.getMessage().startswith("MCP session closed ")]
    assert len(closed) == 1
    assert " initialization_ms=0 " in closed[0]
