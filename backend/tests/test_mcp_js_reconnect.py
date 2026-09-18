"""Opt-in real JavaScript interoperability; use an existing SDK, never install one.

Set MCP_JS_SDK_ROOT to an installed @modelcontextprotocol/sdk package directory.
The test server and its empty tool list use only an ephemeral loopback listener.
"""

import asyncio
import json
import os
from pathlib import Path
import shutil
import time

from fastapi import FastAPI
from mcp.server.lowlevel import Server
import pytest

from backend.mcp_stream_limit import MCPStreamLimitMiddleware
from backend.mcp_transport import LegacySSETransport
from backend.tests.test_mcp_transport import loopback_server


@pytest.mark.parametrize("retry_milliseconds", [6000, 300000])
def test_actual_javascript_sdk_obeys_retry_and_fresh_client_recovers(retry_milliseconds):
    sdk_root = os.environ.get("MCP_JS_SDK_ROOT")
    node = shutil.which("node")
    if not sdk_root or not node:
        pytest.skip("Set MCP_JS_SDK_ROOT and provide Node to run the real-client regression")
    assert (Path(sdk_root) / "dist/cjs/client/sse.js").is_file()

    async def scenario():
        server = Server("isolated-reconnect-regression")

        @server.list_tools()
        async def list_tools():
            return []

        app = FastAPI()
        transport = LegacySSETransport(
            server, initialize_timeout=0.5, idle_timeout=1,
            initialize_retry_milliseconds=retry_milliseconds,
        )
        transport.mount(app)
        limiter = MCPStreamLimitMiddleware(app, 3, 3, lambda request: request.client.host)
        gets, methods = [], []

        async def observed(scope, receive, send):
            if scope["type"] != "http":
                return await limiter(scope, receive, send)
            record = None
            if scope["method"] == "GET":
                record = {"start": time.monotonic(), "body": b""}
                gets.append(record)

            async def receiving():
                frame = await receive()
                if scope["method"] == "POST" and frame.get("body"):
                    methods.append(json.loads(frame["body"])["method"])
                return frame

            async def sending(frame):
                if record is not None:
                    record["body"] += frame.get("body", b"")
                    if frame["type"] == "http.response.start":
                        record["status"] = frame["status"]
                    elif frame.get("more_body") is False:
                        record["end"] = time.monotonic()
                await send(frame)

            await limiter(scope, receiving, sending)

        async with loopback_server(observed) as base:
            process = await asyncio.create_subprocess_exec(
                node, str(Path(__file__).with_name("mcp_js_reconnect.cjs")),
                base + "/mcp", str(retry_milliseconds),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 35)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            assert process.returncode == 0, stderr.decode()
            result = json.loads(stdout)
            assert result["fresh_tools_ok"]
            assert len(gets) == (3 if retry_milliseconds == 300000 else 4)
            assert all(record["status"] == 200 for record in gets)
            assert b"retry:" not in gets[0]["body"]
            assert gets[1]["body"].endswith(
                f"event: mcp-retry\r\nretry: {retry_milliseconds}\r\n\r\n".encode()
            )
            assert b"retry:" not in gets[2]["body"]  # Explicit fresh client.
            assert 2.8 <= gets[1]["start"] - gets[0]["end"] < 5
            assert gets[2]["start"] - gets[1]["end"] < 2
            if retry_milliseconds != 300000:
                assert 5.8 <= gets[3]["start"] - gets[1]["end"] < 9
            assert methods.count("initialize") == methods.count("tools/list") == 2
            assert methods.count("notifications/initialized") == 2
            assert transport.active_sessions == limiter.active == 0

    asyncio.run(scenario())
