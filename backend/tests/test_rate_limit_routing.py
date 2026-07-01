"""Diagnostic endpoints must be throttled below the default limit.

Registration is open/self-service, so a route that requires only "any
authenticated user" is effectively public. /api/cache/clear flushes the
Redis + in-memory cache (cache-stampede DoS against upstream provider APIs)
and /api/performance/* leaks connection-pool/circuit-breaker internals; both
were inheriting the 200/min default. This guards the strict limits and checks
the ordinary query/auth routing didn't regress.
"""

from backend.main import get_rate_limit_for_path


def test_cache_clear_is_strictly_limited():
    assert get_rate_limit_for_path("/api/cache/clear") == "5/minute"


def test_performance_endpoints_are_limited():
    assert get_rate_limit_for_path("/api/performance/metrics") == "20/minute"
    assert get_rate_limit_for_path("/api/performance/status") == "20/minute"


def test_read_only_cache_stats_stays_default():
    # stats is read-only; it should not be caught by the cache/clear rule.
    assert get_rate_limit_for_path("/api/cache/stats") == "200/minute"


def test_core_query_and_auth_limits_unchanged():
    assert get_rate_limit_for_path("/api/query") == "30/minute"
    assert get_rate_limit_for_path("/api/query/pro") == "10/minute"
    assert get_rate_limit_for_path("/api/query/stream") == "30/minute"
    assert get_rate_limit_for_path("/api/auth/register") == "5/minute"
    assert get_rate_limit_for_path("/api/auth/login") == "10/minute"
    assert get_rate_limit_for_path("/api/user/history") == "200/minute"
