"""Bound legacy MCP streams without occupying workers while waiting for a slot."""

import logging
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("openecon.mcp_stream_limit")


class MCPStreamLimitMiddleware:
    """Admission control for one ASGI process/event loop, including local clients.

    Multiple server workers require a shared admission counter or gateway limit.
    The allowance covers the entire response, not just response headers.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_streams: int,
        max_per_client: int,
        client_ip_resolver: Callable[[Request], str | None],
    ) -> None:
        if not 0 < max_per_client <= max_streams:
            raise ValueError("MCP stream limits require 0 < per-client <= global")
        self.app = app
        self.max_streams = max_streams
        self.max_per_client = max_per_client
        self.client_ip_resolver = client_ip_resolver
        self.active = 0
        self.active_by_client: dict[str, int] = {}
        self.rejected_global = 0
        self.rejected_client = 0
        logger.info(
            "MCP stream limits enabled global=%d per_client=%d scope=single_process",
            max_streams,
            max_per_client,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "GET"
            or scope["path"] not in ("/mcp", "/mcp/")
        ):
            await self.app(scope, receive, send)
            return

        client = self.client_ip_resolver(Request(scope)) or "unknown"
        client_active = self.active_by_client.get(client, 0)
        status = None
        if self.active >= self.max_streams:
            self.rejected_global += 1
            status = 503
        elif client_active >= self.max_per_client:
            self.rejected_client += 1
            status = 429

        if status is not None:
            logger.warning(
                "MCP stream rejected status=%d active=%d global_rejections=%d client_rejections=%d",
                status,
                self.active,
                self.rejected_global,
                self.rejected_client,
            )
            await JSONResponse(
                {"detail": "MCP stream capacity reached. Retry after closing unused sessions."},
                status_code=status,
                headers={"Retry-After": "30", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return

        # No await between checking and claiming capacity on this event loop.
        self.active += 1
        self.active_by_client[client] = client_active + 1
        try:
            logger.info("MCP stream admitted active=%d limit=%d", self.active, self.max_streams)
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
            remaining = self.active_by_client[client] - 1
            if remaining:
                self.active_by_client[client] = remaining
            else:
                del self.active_by_client[client]
            logger.info("MCP stream released active=%d limit=%d", self.active, self.max_streams)
