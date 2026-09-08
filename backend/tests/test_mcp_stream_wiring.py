"""Check the admission policy against the application's actual proxy resolver."""

import pytest
from starlette.requests import Request

from backend.main import app, resolve_client_ip


@pytest.mark.parametrize(
    "direct,forwarded,expected",
    [
        ("198.51.100.1", "192.0.2.99", "198.51.100.1"),
        ("127.0.0.1", "192.0.2.99, 198.51.100.1", "198.51.100.1"),
        ("127.0.0.1", "192.0.2.99, 198.51.100.1, ::1", "198.51.100.1"),
        ("127.0.0.1", None, "127.0.0.1"),
    ],
)
def test_actual_proxy_resolver_ignores_spoofed_client_prefix(direct, forwarded, expected):
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    request = Request({"type": "http", "client": (direct, 1234), "headers": headers})
    assert resolve_client_ip(request, ["127.0.0.1", "::1"]) == expected


def test_overload_responses_receive_logging_and_cors_before_rate_limiter():
    names = [middleware.cls.__name__ for middleware in app.user_middleware]
    assert names.index("CORSMiddleware") < names.index("MCPPostBodyLimitMiddleware")
    assert names.index("MCPPostBodyLimitMiddleware") < names.index("SecureLoggingASGIMiddleware")
    assert names.index("SecureLoggingASGIMiddleware") < names.index("MCPStreamLimitMiddleware")
    assert names.index("MCPStreamLimitMiddleware") < names.index("RateLimitASGIMiddleware")
