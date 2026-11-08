""".env.example must load as-is, and list settings accept every documented form.

A copied .env.example is the self-host starting point (scripts/setup.sh,
README), so Settings must build from it with only the two required secrets
added, keep mock auth on, and parse ALLOWED_ORIGINS / TRUSTED_PROXIES from
the comma form it documents as well as JSON lists and single values.
"""

import shutil
from pathlib import Path

import pytest

from backend.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ISOLATED_VARS = (
    "ALLOWED_ORIGINS",
    "TRUSTED_PROXIES",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_KEY",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ISOLATED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    return monkeypatch


def _write_env(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / ".env"
    path.write_text(f"JWT_SECRET=test-jwt-secret\n{extra}", encoding="utf-8")
    return path


def test_env_example_loads_with_mock_auth(clean_env, tmp_path):
    copy = tmp_path / ".env"
    shutil.copy(ENV_EXAMPLE, copy)
    settings = Settings(_env_file=copy)
    assert settings.allowed_origins == ["http://localhost:5173", "http://localhost:3000"]
    assert settings.supabase_enabled is False
    assert settings.allow_mock_auth is True


ORIGIN_FORMS = [
    ("http://localhost:5173,http://localhost:3000", ["http://localhost:5173", "http://localhost:3000"]),
    (" http://localhost:5173 , http://localhost:3000 ", ["http://localhost:5173", "http://localhost:3000"]),
    ('["http://localhost:5173","http://localhost:3000"]', ["http://localhost:5173", "http://localhost:3000"]),
    ("https://data.openecon.ai", ["https://data.openecon.ai"]),
]


@pytest.mark.parametrize(("value", "expected"), ORIGIN_FORMS)
def test_allowed_origins_from_dotenv(clean_env, tmp_path, value, expected):
    settings = Settings(_env_file=_write_env(tmp_path, f"ALLOWED_ORIGINS={value}\n"))
    assert settings.allowed_origins == expected


@pytest.mark.parametrize(("value", "expected"), ORIGIN_FORMS)
def test_allowed_origins_from_environment(clean_env, tmp_path, value, expected):
    clean_env.setenv("ALLOWED_ORIGINS", value)
    settings = Settings(_env_file=_write_env(tmp_path))
    assert settings.allowed_origins == expected


@pytest.mark.parametrize("via", ["dotenv", "environment"])
def test_trusted_proxies_comma_form(clean_env, tmp_path, via):
    if via == "dotenv":
        env_file = _write_env(tmp_path, "TRUSTED_PROXIES=127.0.0.1,::1\n")
    else:
        clean_env.setenv("TRUSTED_PROXIES", "127.0.0.1,::1")
        env_file = _write_env(tmp_path)
    assert Settings(_env_file=env_file).trusted_proxies == ["127.0.0.1", "::1"]


def test_list_defaults_are_unchanged(clean_env, tmp_path):
    settings = Settings(_env_file=_write_env(tmp_path))
    assert settings.allowed_origins == []
    assert settings.trusted_proxies == ["127.0.0.1", "::1"]
