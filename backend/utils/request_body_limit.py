"""Per-path request-body size limits enforced at the ASGI layer.

FastAPI/Starlette buffer the whole request body before model validation, so a
pydantic max_length only runs AFTER an arbitrarily large body has been read
into memory, and Apache on this host leaves LimitRequestBody at its default
(0 = unlimited). This middleware bounds the body before the application sees
it, for the paths that opt in:

* a declared ``Content-Length`` above the limit is refused with 413 without
  reading the body;
* the received stream is counted as it arrives (covers chunked uploads and a
  lying Content-Length) and refused with 413 once it exceeds the limit;
* a body that dribbles in slower than ``read_timeout`` gets 408.

Accepted bodies (at most ``limit`` bytes) are buffered and replayed to the app
unchanged, mirroring MCPPostBodyLimitMiddleware. Purely structural: no
inspection of body content.
"""
from __future__ import annotations

from typing import Mapping, Optional

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_NO_STORE = {"Cache-Control": "no-store"}


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limits: Mapping[str, int],
        read_timeout: float = 30.0,
    ) -> None:
        if not limits or any(v <= 0 for v in limits.values()) or read_timeout <= 0:
            raise ValueError("body limits and read_timeout must be positive")
        self.app = app
        self.limits = {path.rstrip("/") or "/": int(v) for path, v in limits.items()}
        self.read_timeout = read_timeout

    def limit_for(self, path: str) -> Optional[int]:
        """Exact path or any sub-path of a configured prefix."""
        normalized = path.rstrip("/") or "/"
        for prefix, limit in self.limits.items():
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return limit
        return None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
        await JSONResponse({"detail": detail}, status_code=status, headers=_NO_STORE)(
            scope, receive, send
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = None
        if scope["type"] == "http" and scope.get("method") in _BODY_METHODS:
            limit = self.limit_for(scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return

        too_large = f"Request body exceeds the {limit}-byte limit for this endpoint."
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    continue  # the received byte count is enforced below
                if declared > limit:
                    await self._reject(scope, receive, send, 413, too_large)
                    return

        buffer = bytearray()
        try:
            with anyio.fail_after(self.read_timeout):
                while True:
                    frame = await receive()
                    if frame["type"] == "http.disconnect":
                        return
                    buffer.extend(frame.get("body", b""))
                    if len(buffer) > limit:
                        await self._reject(scope, receive, send, 413, too_large)
                        return
                    if not frame.get("more_body", False):
                        break
        except TimeoutError:
            await self._reject(scope, receive, send, 408, "Request body read timed out.")
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
