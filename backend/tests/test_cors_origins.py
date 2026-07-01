"""CORS fallback must never trust localhost in production.

main.py sends allow_credentials=True, so any origin in the allow-list can make
credentialed reads of /api/*. Production with no ALLOWED_ORIGINS previously fell
back to localhost dev origins, letting a page on the victim's own loopback read
cookie-attached endpoints. The fallback is now environment-aware.
"""

from backend.config import Settings


def _settings(**over):
    # Construct by env-alias and with _env_file=None so the host .env doesn't
    # bleed real ALLOWED_ORIGINS/NODE_ENV into the assertion.
    base = {
        "OPENROUTER_API_KEY": "test-key",
        "JWT_SECRET": "x" * 32,
        "LLM_PROVIDER": "openrouter",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def test_production_fallback_excludes_localhost():
    s = _settings(NODE_ENV="production", APP_URL="https://data.openecon.ai")
    origins = s.effective_cors_origins
    assert "https://data.openecon.ai" in origins
    assert "https://www.data.openecon.ai" in origins
    assert not any("localhost" in o for o in origins)
    assert not any("127.0.0.1" in o for o in origins)


def test_explicit_origins_always_win():
    s = _settings(
        NODE_ENV="production",
        ALLOWED_ORIGINS="https://a.example,https://b.example",
    )
    assert s.effective_cors_origins == ["https://a.example", "https://b.example"]


def test_development_keeps_localhost():
    s = _settings(NODE_ENV="development", APP_URL="http://localhost:3001")
    origins = s.effective_cors_origins
    assert "http://localhost:5173" in origins
