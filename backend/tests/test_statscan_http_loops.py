"""The StatsCan curl_cffi client must work on every event loop, not just the first.

A curl_cffi AsyncSession binds to the loop it first runs on. The client used to
keep ONE process-wide session, so the second ``asyncio.run`` (scripts, per-test
loops, helpers that run a coroutine on a worker-thread loop) failed with
"... attached to a different loop". These tests exercise the mechanism against
a local HTTP server (no network): several sequential loops, a loop on another
thread, and the lifetime contract (a loop's session is closed and released when
that loop shuts down, so loops do not accumulate sessions).
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.services import statscan_http
from backend.services.statscan_http import get_statscan_http_client


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output quiet
        pass


@pytest.fixture
def local_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


async def _get_status(url: str) -> int:
    response = await get_statscan_http_client().get(url)
    response.raise_for_status()
    return response.status_code


def test_client_works_across_sequential_event_loops(local_url):
    # Each asyncio.run is a fresh loop; all must succeed (the old process-wide
    # session raised "attached to a different loop" from the second on).
    assert [asyncio.run(_get_status(local_url)) for _ in range(3)] == [200, 200, 200]


def test_client_works_on_a_worker_thread_loop(local_url):
    results: list = []

    def run_in_thread():
        try:
            results.append(asyncio.run(_get_status(local_url)))
        except Exception as exc:  # surface the failure to the main thread
            results.append(exc)

    asyncio.run(_get_status(local_url))  # main-thread loop first
    worker = threading.Thread(target=run_in_thread)
    worker.start()
    worker.join(timeout=30)
    assert results == [200]


def test_one_session_per_loop_and_released_at_loop_shutdown(local_url):
    async def two_requests_same_loop():
        client = get_statscan_http_client()
        await client.get(local_url)
        await client.post(local_url, json={"a": 1})
        loop = asyncio.get_running_loop()
        session, _owner = statscan_http._loop_sessions[loop]
        # A second facade on the same loop reuses that loop's pool.
        assert get_statscan_http_client()._session is session
        return loop, session

    loop, session = asyncio.run(two_requests_same_loop())
    # asyncio.run cancelled the owner task, which closed the session and
    # dropped the entry — nothing keeps the finished loop alive.
    assert loop not in statscan_http._loop_sessions
    assert getattr(session, "_closed", False) is True


def test_explicit_session_is_used_verbatim():
    sentinel = object()
    assert statscan_http.StatsCanHTTPClient(sentinel)._session is sentinel


# --- Callers must not spin a second loop for StatsCan metadata ---------------
# DeltaExtractor.extract() is synchronous and runs on the request's event loop.
# Its dimension-modifier handler used to fetch missing cube metadata by running
# asyncio.run(...) on a worker thread while blocking the request loop for up to
# 10s. It now reads only network-free metadata (conversation state, then the
# provider's in-process / bundled caches) and otherwise declines.

def _dimension_extractor(*, cube_cache=None, local_metadata=None):
    from unittest.mock import AsyncMock, MagicMock

    from backend.services.delta_extractor import DeltaExtractor

    statscan = MagicMock()
    statscan.PRODUCT_ID_CACHE = {}
    statscan._normalize_metadata_product_id = lambda pid: "".join(ch for ch in str(pid) if ch.isdigit())[:8]
    statscan._cube_metadata_cache = dict(cube_cache or {})
    statscan._statscan_metadata_service.get_local_cube_metadata = MagicMock(return_value=local_metadata)
    statscan._get_cube_metadata = AsyncMock(side_effect=AssertionError("live fetch from sync extract()"))
    statscan.extract_dimension_modifiers = MagicMock(return_value={"sex": "female"})
    qs = MagicMock()
    qs.statscan_provider = statscan
    return DeltaExtractor(qs), statscan


def _statscan_state():
    from backend.services.conversation_state_v2 import ConversationState

    return ConversationState(
        indicator="unemployment rate",
        base_indicator="UNEMPLOYMENT_RATE",
        provider="StatsCan",
        country="CA",
        statscan_product_id="14100287",
        statscan_cube_metadata=None,
    )


_CUBE = {"dimension": [{"dimensionNameEn": "Sex", "member": []}]}


@pytest.mark.parametrize("source", ["cube_cache", "local_metadata"])
def test_dimension_modifier_uses_network_free_cube_metadata(source):
    kwargs = {"cube_cache": {"14100287": _CUBE}} if source == "cube_cache" else {"local_metadata": _CUBE}
    extractor, statscan = _dimension_extractor(**kwargs)

    delta = extractor._try_dimension_modifier("show female", _statscan_state())

    assert delta is not None and delta.added_dimensions == {"sex": "female"}
    assert statscan.extract_dimension_modifiers.call_args.kwargs["cube_metadata"] == _CUBE
    statscan._get_cube_metadata.assert_not_called()


def test_dimension_modifier_declines_inside_running_loop_without_live_fetch():
    extractor, statscan = _dimension_extractor()

    async def on_request_loop():
        return extractor._try_dimension_modifier("show female", _statscan_state())

    assert asyncio.run(on_request_loop()) is None
    statscan._get_cube_metadata.assert_not_called()
    statscan.extract_dimension_modifiers.assert_not_called()
