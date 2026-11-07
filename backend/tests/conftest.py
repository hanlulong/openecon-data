"""
Shared pytest fixtures for OpenEcon Data backend tests.

This module provides common fixtures used across all test modules.
Import fixtures from here instead of defining them in individual test files.
"""
from __future__ import annotations

import os
import pytest
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# Set test environment before importing application modules
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DISABLE_MCP", "1")
os.environ.setdefault("DISABLE_BACKGROUND_JOBS", "1")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing")
# Collection must be hermetic: Settings() validates that OPENROUTER_API_KEY
# exists when LLM_PROVIDER=openrouter (the default), and pytest runs from
# backend/ where the repo-root .env is not picked up. Without this, the
# suite only collects when the shell happens to export a real key.
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key-not-real")
# Replay recorded query-embedding vectors by default so the suite is offline and
# deterministic (embedding_retrieval.search would otherwise hit the OpenAI/
# OpenRouter embeddings endpoint). `record` mode — which writes new fixtures by
# calling the real API — must be opted into explicitly, so never override it.
if os.environ.get("OPENECON_EMBED_FIXTURES") != "record":
    os.environ["OPENECON_EMBED_FIXTURES"] = "replay"
# Same contract for the selector-LLM adjudication seam (indicator_selector's
# _llm_pick): replay recorded chat-completion responses so the suite is offline
# and deterministic. A miss raises KeyError with the re-record command rather
# than silently returning a default. `record` mode — which calls the real LLM
# and writes new fixtures — must be opted into explicitly, so never override it.
if os.environ.get("OPENECON_SELECTOR_FIXTURES") != "record":
    os.environ["OPENECON_SELECTOR_FIXTURES"] = "replay"

# ---------------------------------------------------------------------------
# Production isolation. Settings reads the repo-root .env, which on the
# production host carries NODE_ENV=production, the real Supabase project
# credentials and (implicitly) the production Redis at localhost:6379/0. The
# suite used to inherit all three: query-endpoint tests wrote rows into the
# production `user_queries` table and conversation tests wrote conv:* keys into
# production Redis. Process env beats .env, but env_ignore_empty=True means an
# empty value cannot UNSET a key, so isolation is layered:
#   1. here, before any backend import: force the test environment and point
#      REDIS_URL at an unreachable loopback port (connection refused at once;
#      every Redis consumer already degrades to in-memory / fails open);
#   2. `isolate_production_backends` (autouse, per test) nulls the Supabase
#      credentials on the cached Settings object and resets the Supabase
#      singletons — per test, because some tests clear get_settings' cache;
#   3. guards (installed once per session) that refuse — and record — any
#      client built for, or I/O sent to, the configured Supabase project and
#      any TCP connect to loopback:6379, so a path that re-loads production
#      config is stopped even when its caller swallows the error.
# ---------------------------------------------------------------------------
os.environ["NODE_ENV"] = "test"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"

_PRODUCTION_REDIS_PORT = 6379
_SUPABASE_SETTING_FIELDS = ("supabase_url", "supabase_anon_key", "supabase_service_key")
_production_supabase_urls: set = set()
_supabase_opt_in = {"enabled": False}
_blocked_supabase_calls: List[str] = []
_blocked_redis_connects: List[str] = []


def _norm_url(url: Any) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _is_production_supabase(url: Any) -> bool:
    return bool(_norm_url(url)) and _norm_url(url) in _production_supabase_urls


def _refuse_production_supabase(what: str) -> None:
    _blocked_supabase_calls.append(what)
    raise RuntimeError(
        f"test isolation: refused {what} against the configured (production) "
        "Supabase project; mark the test @pytest.mark.supabase to opt in"
    )


def _is_production_redis_address(address: Any) -> bool:
    if not (isinstance(address, tuple) and len(address) >= 2):
        return False  # AF_UNIX paths etc.
    host, port = str(address[0]).lower(), address[1]
    loopback = host in {"localhost", "::1", "0:0:0:0:0:0:0:1"} or host.startswith("127.")
    return loopback and port == _PRODUCTION_REDIS_PORT


@pytest.fixture(scope="session", autouse=True)
def _production_backend_guards():
    """Install the session-wide Supabase / Redis guards (layer 3 above)."""
    import socket

    from backend.config import get_settings

    url = get_settings().supabase_url
    if url:
        _production_supabase_urls.add(_norm_url(url))

    patches = []

    def guard_create_client(module_name: str):
        import importlib

        module = importlib.import_module(module_name)
        real = module.create_client

        def guarded(url, key, *args, **kwargs):
            if _is_production_supabase(url) and not _supabase_opt_in["enabled"]:
                _refuse_production_supabase(f"{module_name}.create_client({url})")
            return real(url, key, *args, **kwargs)

        patches.append(patch.object(module, "create_client", guarded))

    for module_name in ("supabase", "backend.services.supabase_service", "backend.services.async_supabase"):
        guard_create_client(module_name)

    from backend.services.async_supabase import AsyncSupabase

    real_run_async = AsyncSupabase._run_async

    async def guarded_run_async(self, func, *args, **kwargs):
        # Every AsyncSupabase select/insert/update/delete/rpc funnels through
        # here — covers instances built before the client guard was installed.
        if _is_production_supabase(getattr(self, "url", None)) and not _supabase_opt_in["enabled"]:
            _refuse_production_supabase(f"AsyncSupabase I/O ({getattr(func, '__name__', func)})")
        return await real_run_async(self, func, *args, **kwargs)

    patches.append(patch.object(AsyncSupabase, "_run_async", guarded_run_async))

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded_connect(sock, address):
        if _is_production_redis_address(address):
            _blocked_redis_connects.append(repr(address))
            raise ConnectionRefusedError(f"test isolation: production Redis {address!r} is off-limits")
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if _is_production_redis_address(address):
            _blocked_redis_connects.append(repr(address))
            return 111  # ECONNREFUSED
        return real_connect_ex(sock, address)

    patches.append(patch.object(socket.socket, "connect", guarded_connect))
    patches.append(patch.object(socket.socket, "connect_ex", guarded_connect_ex))

    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


def _reset_supabase_singletons() -> None:
    try:
        from backend.services import supabase_service as _sb

        _sb._database_service = None
        _sb._auth_service = None
        _sb.get_supabase_client.cache_clear()
        _sb.get_supabase_anon_client.cache_clear()
    except Exception:
        pass
    try:
        import backend.services.auth_factory as _auth_factory

        _auth_factory._auth_service = None
    except Exception:
        pass


@pytest.fixture(autouse=True)
def isolate_production_backends(request, _production_backend_guards):
    """Run every test without production Supabase credentials (layer 2).

    A test marked ``@pytest.mark.supabase`` keeps whatever credentials Settings
    loaded and may reach the configured project — deliberate integration only.
    Fails the test if a guarded production-Supabase call was attempted, because
    the caller may have swallowed the guard's exception.
    """
    opted_in = request.node.get_closest_marker("supabase") is not None
    _supabase_opt_in["enabled"] = opted_in
    if not opted_in:
        from backend.config import get_settings

        settings = get_settings()
        for field in _SUPABASE_SETTING_FIELDS:
            setattr(settings, field, None)
        _reset_supabase_singletons()
    del _blocked_supabase_calls[:]
    yield
    _supabase_opt_in["enabled"] = False
    blocked = list(_blocked_supabase_calls)
    del _blocked_supabase_calls[:]
    if blocked:
        pytest.fail(
            "test reached the production Supabase project (blocked): " + "; ".join(blocked),
            pytrace=False,
        )


@pytest.fixture
def production_isolation_state():
    """Expose the guard bookkeeping to tests of the isolation layer itself."""
    from types import SimpleNamespace

    return SimpleNamespace(
        supabase_urls=_production_supabase_urls,
        blocked_supabase=_blocked_supabase_calls,
        blocked_redis=_blocked_redis_connects,
    )


def pytest_terminal_summary(terminalreporter):
    if _blocked_redis_connects:
        terminalreporter.write_line(
            f"test isolation: blocked {len(_blocked_redis_connects)} connection attempt(s) to "
            "production Redis (loopback:6379) — a code path ignores settings.redis_url"
        )


# ============================================================================
# Environment Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def test_environment():
    """Ensure test environment is set for all tests."""
    old_env = os.environ.copy()
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DISABLE_MCP"] = "1"
    os.environ["DISABLE_BACKGROUND_JOBS"] = "1"
    # Tests must not depend on the optional local selector endpoint (an SSH
    # tunnel that may or may not be up); empty overrides beat .env values.
    os.environ["SELECTOR_LLM_MODEL"] = ""
    os.environ["SELECTOR_LLM_BASE_URL"] = ""
    # Replay recorded query embeddings unless a recording pass explicitly asked
    # for record mode (see the module-level default above).
    if os.environ.get("OPENECON_EMBED_FIXTURES") != "record":
        os.environ["OPENECON_EMBED_FIXTURES"] = "replay"
    # Same for the selector-LLM adjudication seam.
    if os.environ.get("OPENECON_SELECTOR_FIXTURES") != "record":
        os.environ["OPENECON_SELECTOR_FIXTURES"] = "replay"
    yield
    os.environ.clear()
    os.environ.update(old_env)


@pytest.fixture(autouse=True)
def reset_provider_circuit_breakers():
    """Keep provider tests hermetic: the per-provider circuit breakers in
    providers/base.py are module-level, so one test simulating 5 failures
    trips the breaker and every later test against that provider fails with
    'circuit breaker OPEN'. Drop all breaker state before each test."""
    try:
        from backend.providers import base as _provider_base
        _provider_base._provider_breakers.clear()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def reset_selection_cache():
    """Keep selector tests hermetic: _SELECTION_CACHE in indicator_selector.py
    is module-level (per-worker, 6h TTL in production), so a confident pick
    cached by one test is served to a later test expecting the LLM to be
    consulted — an order-dependent flake (observed: two authority-contract
    tests failing under full-suite ordering, passing in isolation)."""
    try:
        from backend.services import indicator_selector as _sel
        _sel._SELECTION_CACHE.clear()
    except Exception:
        pass
    yield


# ============================================================================
# Data Fixtures
# ============================================================================

@pytest.fixture
def sample_data_points() -> List[Dict[str, Any]]:
    """Sample data points for testing."""
    return [
        {"date": "2023-01-01", "value": 100.0},
        {"date": "2023-02-01", "value": 105.2},
        {"date": "2023-03-01", "value": 103.8},
        {"date": "2023-04-01", "value": 108.1},
        {"date": "2023-05-01", "value": 110.5},
    ]


@pytest.fixture
def sample_metadata() -> Dict[str, Any]:
    """Sample metadata for testing."""
    return {
        "source": "FRED",
        "indicator": "Gross Domestic Product",
        "country": "US",
        "frequency": "quarterly",
        "unit": "Billions of Dollars",
        "lastUpdated": datetime.now().isoformat(),
        "seriesId": "GDP",
        "seasonalAdjustment": "Seasonally Adjusted Annual Rate",
        "dataType": "Level",
        "description": "Gross Domestic Product",
    }


@pytest.fixture
def sample_normalized_data(sample_data_points, sample_metadata) -> Dict[str, Any]:
    """Sample NormalizedData structure for testing."""
    return {
        "metadata": sample_metadata,
        "data": sample_data_points,
    }


@pytest.fixture
def sample_parsed_intent() -> Dict[str, Any]:
    """Sample ParsedIntent structure for testing."""
    return {
        "apiProvider": "FRED",
        "indicators": ["GDP"],
        "parameters": {
            "country": "US",
            "startDate": "2023-01-01",
            "endDate": "2023-12-31",
        },
        "clarificationNeeded": False,
        "clarificationQuestions": None,
        "confidence": 0.95,
        "recommendedChartType": "line",
        "originalQuery": "Show me US GDP",
        "needsDecomposition": False,
        "decompositionType": None,
        "decompositionEntities": None,
        "useProMode": False,
    }


# ============================================================================
# Service Mocks
# ============================================================================

@pytest.fixture
def mock_cache():
    """Mock cache service that returns None (cache miss)."""
    with patch("backend.services.cache.cache_service") as mock:
        mock.get.return_value = None
        mock.set.return_value = None
        mock.clear.return_value = None
        yield mock


@pytest.fixture
def mock_http_client():
    """Mock HTTP client for provider tests."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_response
    return mock_client


@pytest.fixture
def mock_openrouter_response():
    """Mock OpenRouter API response for LLM tests."""
    return {
        "choices": [
            {
                "message": {
                    "content": '{"apiProvider": "FRED", "indicators": ["GDP"], "parameters": {"country": "US"}}'
                }
            }
        ]
    }


# ============================================================================
# Auth Fixtures
# ============================================================================

@pytest.fixture
def test_user() -> Dict[str, Any]:
    """Sample test user data."""
    return {
        "id": "test-user-123",
        "email": "test@example.com",
        "created_at": datetime.now().isoformat(),
    }


@pytest.fixture
def auth_token() -> str:
    """Generate a test JWT token."""
    import jwt
    payload = {
        "sub": "test-user-123",
        "email": "test@example.com",
        "exp": datetime.now().timestamp() + 3600,
    }
    return jwt.encode(payload, os.environ.get("JWT_SECRET", "test-secret"), algorithm="HS256")


@pytest.fixture
def auth_headers(auth_token) -> Dict[str, str]:
    """HTTP headers with authorization token."""
    return {"Authorization": f"Bearer {auth_token}"}


# ============================================================================
# Provider Fixtures
# ============================================================================

@pytest.fixture
def fred_sample_response() -> Dict[str, Any]:
    """Sample FRED API response."""
    return {
        "observations": [
            {"date": "2023-01-01", "value": "26000.0"},
            {"date": "2023-04-01", "value": "26500.0"},
            {"date": "2023-07-01", "value": "27000.0"},
            {"date": "2023-10-01", "value": "27500.0"},
        ],
        "realtime_start": "2023-01-01",
        "realtime_end": "2023-12-31",
        "observation_start": "2023-01-01",
        "observation_end": "2023-12-31",
        "units": "Billions of Dollars",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": 4,
        "offset": 0,
        "limit": 100000,
    }


@pytest.fixture
def worldbank_sample_response() -> List[Any]:
    """Sample World Bank API response."""
    return [
        {"page": 1, "pages": 1, "per_page": 50, "total": 4},
        [
            {"date": "2023", "value": 25000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
            {"date": "2022", "value": 24000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
            {"date": "2021", "value": 23000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
            {"date": "2020", "value": 21000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
        ],
    ]


@pytest.fixture
def imf_sample_response() -> Dict[str, Any]:
    """Sample IMF API response."""
    return {
        "CompactData": {
            "DataSet": {
                "Series": {
                    "@REF_AREA": "US",
                    "@INDICATOR": "NGDP_RPCH",
                    "Obs": [
                        {"@TIME_PERIOD": "2023", "@OBS_VALUE": "2.5"},
                        {"@TIME_PERIOD": "2022", "@OBS_VALUE": "2.1"},
                        {"@TIME_PERIOD": "2021", "@OBS_VALUE": "5.9"},
                    ],
                }
            }
        }
    }


# ============================================================================
# Cleanup Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singleton services between tests."""
    yield
    # Reset auth service singleton
    try:
        import backend.services.auth_factory as auth_factory
        auth_factory._auth_service = None
    except ImportError:
        pass

    # Clear user store
    try:
        from backend.services.user_store import user_store
        user_store.clear()
    except ImportError:
        pass

    # Clear cache
    try:
        from backend.services.cache import cache_service
        cache_service.clear()
    except ImportError:
        pass
