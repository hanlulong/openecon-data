from __future__ import annotations

from pathlib import Path

from backend.config import Settings
from backend.services import llm as llm_module


def test_settings_defaults_match_env_example_for_llm_provider():
    settings = Settings(
        _env_file=None,
        JWT_SECRET="test-secret",
        OPENROUTER_API_KEY="test-openrouter-key",
    )

    assert settings.llm_provider == "openrouter"
    assert settings.llm_model == "openai/gpt-4o-mini"


def test_create_llm_provider_uses_openrouter_defaults(monkeypatch):
    settings = Settings(
        _env_file=None,
        JWT_SECRET="test-secret",
        OPENROUTER_API_KEY="test-openrouter-key",
    )
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings)

    provider = llm_module.create_llm_provider()

    assert isinstance(provider, llm_module.OpenRouterProvider)
    assert provider.model == "openai/gpt-4o-mini"


def test_env_example_has_no_rule_shortcut_rollback_flags():
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    values = {}
    for raw_line in env_example.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    assert values["USE_HYBRID_ROUTER"] == "false"
    assert values["USE_SEMANTIC_PROVIDER_ROUTER"] == "false"
    assert values["USE_LITELLM_ROUTER_FALLBACK"] == "false"
    assert "ALLOW_LEGACY_INDICATOR_RESOLVER_FINAL_AUTHORITY" not in values
    assert "ALLOW_LEGACY_PROVIDER_MAP_FINAL_AUTHORITY" not in values
    assert "ALLOW_LEGACY_CATALOG_FALLBACK_FINAL_AUTHORITY" not in values
