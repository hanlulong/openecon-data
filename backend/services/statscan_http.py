"""StatsCan-scoped HTTP client (curl_cffi, Chrome TLS impersonation).

Statistics Canada's WDS host (www150.statcan.gc.ca) sits behind a WAF that
silently stalls python/OpenSSL's default TLS ClientHello: raw TCP connects in
~0.1s and `curl` completes the request in ~0.6s, but httpx's TLS handshake
times out at 8s (a JA3/JA4 fingerprint block). curl_cffi drives libcurl with
BoringSSL and impersonates a real Chrome TLS+HTTP2 fingerprint, which the WAF
must allow — it cannot block the Chrome fingerprint without blocking real
browsers. Verified: with `impersonate="chrome"` the same getCubeMetadata POST
returns HTTP 200 in ~0.3s.

This client is **StatsCan-only**; every other provider stays on the shared
httpx pool (`http_pool.get_http_client`). It exposes the small slice of the
httpx.AsyncClient surface the StatsCan provider uses — ``post`` / ``get`` /
``stream`` — and, critically, **re-raises curl_cffi failures as the httpx
exception types the provider already handles** (``httpx.HTTPStatusError`` from
``raise_for_status``; ``httpx.TimeoutException`` / ``httpx.ConnectError`` from
transport failures), so the StatsCan provider code is unchanged apart from which
client it acquires. If another provider's host ever starts fingerprint-blocking,
it can opt into the same client via this one seam.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Impersonation target. "chrome" tracks a recent stable Chrome fingerprint as
# curl_cffi updates; pin a specific version (e.g. "chrome131") only if a future
# WAF change requires it.
_IMPERSONATE = "chrome"

# One curl_cffi AsyncSession (connection pool) PER RUNNING EVENT LOOP.
#
# A curl_cffi AsyncSession binds to the loop it first runs on (its AsyncCurl
# schedules timers, socket readers and a timeout task there), so a single
# process-wide session breaks every later loop with "... attached to a different
# loop" — e.g. scripts or tests that call ``asyncio.run`` more than once, or any
# helper that runs a coroutine on a worker-thread loop. This mirrors
# ``http_pool``'s per-loop clients, with one difference: the session holds a
# STRONG reference to its loop, so a ``WeakKeyDictionary`` keyed by the loop
# would never release the entry. Instead each session is owned by a small
# lifetime task on its own loop; when the loop shuts down (``asyncio.run`` /
# ``asyncio.Runner`` / uvicorn exit all cancel outstanding tasks), that task
# closes the session on the still-running loop and drops the entry. Entries for
# loops closed WITHOUT cancelling their tasks are pruned on the next access.
#
# Value: (session, owner_task). The task reference is kept here because asyncio
# itself only holds tasks weakly.
_loop_sessions: Dict[asyncio.AbstractEventLoop, Tuple[Any, "asyncio.Task[None]"]] = {}
_loop_sessions_lock = threading.Lock()  # loops may live on different threads


def _translate_kwargs(kw: Dict[str, Any]) -> Dict[str, Any]:
    """Map the few httpx kwarg names that differ in curl_cffi."""
    if "follow_redirects" in kw:
        kw["allow_redirects"] = kw.pop("follow_redirects")
    return kw


def _to_httpx_exc(exc: Exception) -> Exception:
    """Map a curl_cffi transport error onto the httpx exception the provider
    catches, so existing `except httpx.TimeoutException/ConnectError` works."""
    msg = str(exc)
    code = getattr(exc, "code", None)
    # curl code 28 == CURLE_OPERATION_TIMEDOUT
    if code == 28 or "timed out" in msg.lower() or "timeout" in msg.lower():
        return httpx.TimeoutException(msg)
    return httpx.ConnectError(msg)


class _Resp:
    """Thin facade over a curl_cffi Response.

    Delegates the read surface (status_code / content / json / text / headers)
    to the underlying response, but overrides ``raise_for_status`` to raise an
    ``httpx.HTTPStatusError`` so the provider's handlers catch it unchanged.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def raise_for_status(self) -> Any:
        sc = self._raw.status_code
        if sc >= 400:
            req = httpx.Request("GET", str(getattr(self._raw, "url", "") or "https://www150.statcan.gc.ca"))
            raise httpx.HTTPStatusError(
                f"HTTP {sc}",
                request=req,
                response=httpx.Response(sc, request=req, content=getattr(self._raw, "content", b"") or b""),
            )
        return self._raw


class _StreamResp:
    """Facade for a streaming curl_cffi response, exposing httpx's aiter_bytes."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.status_code = raw.status_code
        self.headers = raw.headers

    def raise_for_status(self) -> Any:
        return _Resp(self._raw).raise_for_status()

    async def aiter_bytes(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        async for chunk in self._raw.aiter_content(chunk_size):
            yield chunk


class StatsCanHTTPClient:
    """httpx.AsyncClient-shaped facade over a curl_cffi AsyncSession.

    With no explicit ``session`` the facade resolves the current loop's pooled
    session at request time (``_session_for_running_loop``).
    """

    def __init__(self, session: Optional[Any] = None) -> None:
        self._fixed_session = session

    @property
    def _session(self) -> Any:
        if self._fixed_session is not None:
            return self._fixed_session
        return _session_for_running_loop()

    async def post(self, url: str, **kw: Any) -> _Resp:
        try:
            return _Resp(await self._session.post(url, **_translate_kwargs(kw)))
        except httpx.HTTPError:
            raise
        except Exception as exc:  # curl_cffi RequestsError, etc.
            raise _to_httpx_exc(exc) from exc

    async def get(self, url: str, **kw: Any) -> _Resp:
        try:
            return _Resp(await self._session.get(url, **_translate_kwargs(kw)))
        except httpx.HTTPError:
            raise
        except Exception as exc:
            raise _to_httpx_exc(exc) from exc

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kw: Any):
        kw = _translate_kwargs(kw)
        try:
            raw = await self._session.request(method, url, stream=True, **kw)
        except httpx.HTTPError:
            raise
        except Exception as exc:
            raise _to_httpx_exc(exc) from exc
        try:
            yield _StreamResp(raw)
        finally:
            try:
                await raw.aclose()
            except Exception:
                pass


async def _own_session(loop: asyncio.AbstractEventLoop, session: Any) -> None:
    """Keep ``session`` alive for its loop's lifetime, then close it on that loop."""
    try:
        await loop.create_future()  # never resolves; ends when the loop cancels us
    finally:
        with _loop_sessions_lock:
            entry = _loop_sessions.get(loop)
            if entry is not None and entry[0] is session:
                _loop_sessions.pop(loop, None)
        try:
            await session.close()
        except Exception as exc:  # best-effort cleanup at loop shutdown
            logger.debug("StatsCan HTTP session close failed: %s", exc)


def _session_for_running_loop() -> Any:
    """Return the curl_cffi session bound to the CURRENT running loop."""
    loop = asyncio.get_running_loop()
    with _loop_sessions_lock:
        entry = _loop_sessions.get(loop)
        if entry is not None and not getattr(entry[0], "_closed", False):
            return entry[0]
        # Drop sessions whose loop closed without cancelling its tasks (their
        # owner never ran); nothing can be awaited on a closed loop.
        for stale_loop in [lp for lp in list(_loop_sessions) if lp.is_closed()]:
            _loop_sessions.pop(stale_loop, None)
        if entry is not None:
            entry[1].cancel()  # closed session: let its owner finish cleanup
        from curl_cffi.requests import AsyncSession

        session = AsyncSession(impersonate=_IMPERSONATE)
        owner = loop.create_task(_own_session(loop, session), name="statscan-http-session-owner")
        _loop_sessions[loop] = (session, owner)
    logger.info("StatsCan HTTP client initialized (curl_cffi impersonate=%s)", _IMPERSONATE)
    return session


def get_statscan_http_client() -> StatsCanHTTPClient:
    """Return the StatsCan HTTP client (curl_cffi, Chrome TLS impersonation).

    The returned facade is loop-agnostic: every request uses the connection
    pool of the event loop it is awaited on (see ``_loop_sessions``), so the
    same client works on the uvicorn loop, under ``asyncio.run`` in scripts, and
    across per-test loops.
    """
    return StatsCanHTTPClient()
