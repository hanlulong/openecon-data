"""The test suite must never write to production Supabase or production Redis.

Settings loads the repo-root .env, which on the production host carries the
real Supabase project credentials, NODE_ENV=production and (implicitly, via the
consumers' fallback) the production Redis at localhost:6379/0. Tests that
called the real endpoints wrote rows into production `user_queries` and conv:*
keys into production Redis db 0. These tests pin the isolation MECHANISM in
conftest.py and config.py — not one endpoint — so any path is covered:

* REDIS_URL is a real Settings field, so every consumer that already reads
  ``settings.redis_url`` (response cache, conversation persistence, shared
  rate limiter) follows it; the suite points it at an unreachable port.
* Supabase credentials are absent for every test, the Supabase singletons are
  reset, and client construction / AsyncSupabase I/O against the configured
  project is refused even if a test re-loads production config.
* A raw TCP connect to loopback:6379 is refused.
"""
from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config import Settings, get_settings

_PROD_DEFAULT_REDIS = "redis://localhost:6379/0"
_PROBE_URL = "https://isolation-probe.supabase.co"


# --- REDIS_URL is honoured -----------------------------------------------------

def test_redis_url_is_a_settings_field_with_unchanged_production_default(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)  # conftest sets it for the suite
    assert Settings(_env_file=None, JWT_SECRET="x", OPENROUTER_API_KEY="k").redis_url == _PROD_DEFAULT_REDIS
    custom = Settings(_env_file=None, JWT_SECRET="x", OPENROUTER_API_KEY="k", REDIS_URL="redis://cache.internal:6390/3")
    assert custom.redis_url == "redis://cache.internal:6390/3"


def test_suite_redis_points_away_from_production_db():
    assert get_settings().redis_url != _PROD_DEFAULT_REDIS
    assert ":6379" not in get_settings().redis_url


def test_every_redis_consumer_resolves_settings_redis_url(monkeypatch):
    import backend.main as main_mod
    from backend.services import conversation as conv_mod
    from backend.services import redis_cache as redis_cache_mod

    target = get_settings().redis_url
    # Shared rate limiter storage (built at import).
    assert main_mod._rl_redis_uri == target

    # Async response cache.
    seen = {}

    async def fake_from_url(url, **_kw):
        seen["cache"] = url
        raise ConnectionRefusedError("probe")

    monkeypatch.setattr(redis_cache_mod.redis, "from_url", fake_from_url)
    service = redis_cache_mod.RedisCacheService()
    assert asyncio.run(service.connect()) is False
    assert seen["cache"] == target

    # Sync conversation persistence.
    import redis as sync_redis

    def fake_sync_from_url(url, **_kw):
        seen["conversation"] = url
        raise ConnectionRefusedError("probe")

    monkeypatch.setattr(sync_redis.Redis, "from_url", staticmethod(fake_sync_from_url))
    monkeypatch.setattr(conv_mod, "_sync_redis_client", None)
    monkeypatch.setattr(conv_mod, "_sync_redis_last_attempt", 0.0)
    assert conv_mod._get_sync_redis() is None
    assert seen["conversation"] == target


def test_raw_connect_to_production_redis_port_is_refused(production_isolation_state):
    before = len(production_isolation_state.blocked_redis)
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            with pytest.raises(ConnectionRefusedError, match="test isolation"):
                sock.connect((host, 6379))
    assert len(production_isolation_state.blocked_redis) == before + 2
    del production_isolation_state.blocked_redis[before:]  # expected; keep the summary for real leaks


# --- Supabase ------------------------------------------------------------------

def test_supabase_credentials_absent_and_db_service_is_inert():
    from backend.services.supabase_service import get_supabase_service

    settings = get_settings()
    assert not settings.supabase_url and not settings.supabase_service_key and not settings.supabase_anon_key
    assert settings.supabase_enabled is False
    assert settings.environment == "test"
    assert get_supabase_service().client is None


def test_query_endpoint_logging_never_reaches_a_supabase_insert():
    """The exact production-polluting path: real query_endpoint, mocked
    process_query, real background Supabase logging task."""
    import backend.main as main_mod
    from backend.main import QueryRequest, query_endpoint
    from backend.models import QueryResponse
    from backend.services.async_supabase import AsyncSupabase

    request = MagicMock()
    request.headers = {}
    request.client.host = "127.0.0.1"

    async def run():
        result = await query_endpoint(QueryRequest(query="GDP", conversationId=None), request, user=None)
        # Let the scheduled log_query_to_supabase task run to completion.
        await asyncio.gather(*list(main_mod._background_tasks), return_exceptions=True)
        return result

    with (
        patch.object(main_mod.settings, "anon_query_limit", 0),
        patch.object(main_mod.query_service, "process_query",
                     AsyncMock(return_value=QueryResponse(conversationId="c", clarificationNeeded=False))),
        patch.object(AsyncSupabase, "insert", AsyncMock(side_effect=AssertionError("Supabase insert reached"))) as insert,
        patch.object(main_mod, "log_query_to_supabase", wraps=main_mod.log_query_to_supabase) as log_call,
    ):
        asyncio.run(run())

    assert log_call.await_count == 1  # the logging path really ran ...
    insert.assert_not_called()        # ... and wrote nothing


def test_client_for_configured_project_is_refused_and_recorded(production_isolation_state):
    from backend.services import async_supabase, supabase_service

    production_isolation_state.supabase_urls.add(_PROBE_URL)
    try:
        for module in (supabase_service, async_supabase):
            with pytest.raises(RuntimeError, match="test isolation"):
                module.create_client(_PROBE_URL + "/", "service-key")
        assert len(production_isolation_state.blocked_supabase) == 2
    finally:
        production_isolation_state.supabase_urls.discard(_PROBE_URL)
        del production_isolation_state.blocked_supabase[:]  # expected; don't fail teardown


def test_preexisting_async_client_io_to_configured_project_is_refused(production_isolation_state):
    from backend.services.async_supabase import AsyncSupabase

    with patch("backend.services.async_supabase.create_client", MagicMock()):
        client = AsyncSupabase(_PROBE_URL, "service-key")  # built before the URL is known
    production_isolation_state.supabase_urls.add(_PROBE_URL)
    try:
        # insert() swallows errors and returns None — exactly why the guard
        # records the attempt and conftest fails the test at teardown.
        assert asyncio.run(client.insert("user_queries", {"query": "x"})) is None
        assert any("AsyncSupabase I/O" in entry for entry in production_isolation_state.blocked_supabase)
        client.client.table.assert_not_called()
    finally:
        production_isolation_state.supabase_urls.discard(_PROBE_URL)
        del production_isolation_state.blocked_supabase[:]

