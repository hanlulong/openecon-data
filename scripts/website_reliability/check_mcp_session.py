#!/usr/bin/env python3
"""Verify one legacy MCP SSE session without invoking any tools.

Usage: check_mcp_session.py https://data.openecon.ai/mcp
The SDK follows the advertised message endpoint, retaining its session query.
Only HEAD, initialization, initialized notification, and tools/list are sent.
"""

import argparse
import json
import logging
from datetime import timedelta
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError


class VerificationFailure(Exception):
    """A fixed diagnostic code that cannot contain session IDs or response data."""

    def __init__(self, code, *, http_status=None):
        super().__init__(code)
        self.http_status = http_status


def failure_diagnostic(error: BaseException) -> dict:
    """Describe bounded exception leaves without exposing their messages/URLs."""
    pending = [error]
    leaves = []
    seen = set()
    while pending and len(seen) < 64:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions[:64 - len(seen)])
        else:
            leaves.append(current)

    for current in leaves:
        if isinstance(current, httpx.HTTPStatusError):
            request, response = current.request, current.response
            method = request.method
            stage = "session"
            if method == "HEAD":
                stage = "head"
            elif method == "GET" and request.url.path.rstrip("/") == "/mcp":
                stage = "admission"
            elif method == "POST" and request.url.path.startswith("/mcp/messages"):
                stage = "messages"
            result = {
                "code": "capacity_rejected" if stage == "admission" and response.status_code in (429, 503) else "http_error",
                "stage": stage, "http_status": response.status_code,
            }
            if method in ("HEAD", "GET", "POST"):
                result["method"] = method
            retry = response.headers.get("retry-after", "").strip()
            if retry.isascii() and retry.isdecimal() and len(retry) <= 5 and int(retry) <= 86400:
                result["retry_after_seconds"] = int(retry)
            return result
    for current in leaves:
        if isinstance(current, VerificationFailure):
            if str(current) == "head_did_not_return_405":
                result = {"code": "head_did_not_return_405", "stage": "head", "method": "HEAD"}
                if type(current.http_status) is int and 100 <= current.http_status <= 599:
                    result["http_status"] = current.http_status
                return result
            if str(current) == "query_data_missing":
                return {"code": "query_data_missing", "stage": "discovery", "method": "POST"}
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return {"code": "verification_timeout", "stage": "session"}
        if isinstance(current, httpx.TransportError):
            return {"code": "transport_error", "stage": "session"}
        if isinstance(current, McpError):
            return {"code": "protocol_error", "stage": "session"}
    return {"code": "unexpected_error", "stage": "session"}


def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("/mcp", "/mcp/")
    ):
        raise argparse.ArgumentTypeError("Use an HTTP(S) /mcp URL without credentials or query parameters")
    return value.rstrip("/")


async def verify(url: str) -> dict:
    result = {"head_status": None, "initialized": False, "tools_listed": False, "session_closed": False}
    with anyio.fail_after(20):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.head(url)
            result["head_status"] = response.status_code
            if response.status_code != 405:
                raise VerificationFailure("head_did_not_return_405", http_status=response.status_code)

        def client_factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                auth=auth,
                follow_redirects=True,
                trust_env=False,
            )

        # Both context managers close in their finally paths, including timeout
        # and failure. Exactly one SSE connection is opened; there are no retries.
        async with sse_client(
            url,
            timeout=5,
            sse_read_timeout=15,
            httpx_client_factory=client_factory,
        ) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=8)) as session:
                await session.initialize()
                result["initialized"] = True
                listed = await session.list_tools()
                result["tools_listed"] = True
                result["tool_count"] = len(listed.tools)
                result["query_data_available"] = any(tool.name == "query_data" for tool in listed.tools)
                if not result["query_data_available"]:
                    raise VerificationFailure("query_data_missing")

        result["session_closed"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=endpoint, help="Full public or loopback legacy SSE /mcp URL")
    args = parser.parse_args()
    # SDK exception logs can contain advertised message URLs with session IDs.
    logging.disable(logging.CRITICAL)
    try:
        result = anyio.run(verify, args.url)
    except Exception as error:
        print(json.dumps({"ok": False, "diagnostic": failure_diagnostic(error)}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
