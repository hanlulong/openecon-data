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

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

import httpx

logger = logging.getLogger(__name__)

# Impersonation target. "chrome" tracks a recent stable Chrome fingerprint as
# curl_cffi updates; pin a specific version (e.g. "chrome131") only if a future
# WAF change requires it.
_IMPERSONATE = "chrome"

_session = None  # lazy module singleton: one connection pool per process


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
    """httpx.AsyncClient-shaped facade over a curl_cffi AsyncSession."""

    def __init__(self, session: Any) -> None:
        self._session = session

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


def get_statscan_http_client() -> StatsCanHTTPClient:
    """Return the StatsCan HTTP client (curl_cffi, Chrome TLS impersonation).

    Lazy singleton — the curl_cffi AsyncSession binds to the running event loop
    (the single uvicorn loop in production) and pools connections for the
    process lifetime.
    """
    global _session
    if _session is None:
        from curl_cffi.requests import AsyncSession

        _session = AsyncSession(impersonate=_IMPERSONATE)
        logger.info("StatsCan HTTP client initialized (curl_cffi impersonate=%s)", _IMPERSONATE)
    return StatsCanHTTPClient(_session)
