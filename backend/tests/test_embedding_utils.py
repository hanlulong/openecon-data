from __future__ import annotations

from backend.embedding_utils import (
    is_openai_embedding_model,
    normalize_embedding_model_name,
    resolve_embedding_dimensions,
)


def test_normalize_embedding_model_name_strips_openai_prefix():
    assert normalize_embedding_model_name("openai/text-embedding-3-small") == "text-embedding-3-small"


def test_detects_openai_embedding_models():
    assert is_openai_embedding_model("text-embedding-3-small") is True
    assert is_openai_embedding_model("openai/text-embedding-3-large") is True
    assert is_openai_embedding_model("sentence-transformers/all-MiniLM-L6-v2") is False


def test_resolve_embedding_dimensions_uses_known_openai_defaults():
    assert resolve_embedding_dimensions("text-embedding-3-small") == 1536
    assert resolve_embedding_dimensions("openai/text-embedding-3-large") == 3072
    assert resolve_embedding_dimensions("text-embedding-3-small", 256) == 256
