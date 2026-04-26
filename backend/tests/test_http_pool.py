from __future__ import annotations

import asyncio

import pytest

from backend.services.http_pool import HTTPClientPool, close_http_pool, get_http1_client, get_http_client


def _run_in_new_loop(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _close_pool_sync() -> None:
    _run_in_new_loop(close_http_pool())


@pytest.fixture(autouse=True)
def reset_http_pool():
    _close_pool_sync()
    yield
    _close_pool_sync()


async def _get_client_id() -> int:
    return id(get_http_client())


def test_http_pool_reuses_client_within_single_loop() -> None:
    async def _within_loop() -> tuple[int, int]:
        return id(get_http_client()), id(get_http_client())

    first_id, second_id = _run_in_new_loop(_within_loop())
    assert first_id == second_id


def test_http_pool_reuses_http1_client_within_single_loop() -> None:
    async def _within_loop() -> tuple[int, int]:
        return id(get_http1_client()), id(get_http1_client())

    first_id, second_id = _run_in_new_loop(_within_loop())
    assert first_id == second_id


def test_http_pool_keeps_http1_client_separate_from_default_client() -> None:
    async def _within_loop() -> tuple[int, int]:
        return id(get_http_client()), id(get_http1_client())

    default_id, http1_id = _run_in_new_loop(_within_loop())
    assert default_id != http1_id


def test_http_pool_uses_separate_clients_across_event_loops() -> None:
    async def _get_client():
        return get_http_client()

    first_loop_client = _run_in_new_loop(_get_client())
    second_loop_client = _run_in_new_loop(_get_client())

    assert first_loop_client is not second_loop_client


def test_http_pool_close_resets_all_clients() -> None:
    _run_in_new_loop(_get_client_id())
    _run_in_new_loop(_get_client_id())

    _close_pool_sync()

    stats = HTTPClientPool.get_stats()
    assert stats["status"] == "not_initialized"
    assert stats["active_clients"] == 0
