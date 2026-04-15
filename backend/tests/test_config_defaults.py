from __future__ import annotations

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
