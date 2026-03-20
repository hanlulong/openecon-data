from __future__ import annotations

OPENAI_EMBEDDING_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def normalize_embedding_model_name(model_name: str | None) -> str:
    """Normalize provider-prefixed embedding model names."""
    normalized = (model_name or "").strip()
    if normalized.lower().startswith("openai/"):
        return normalized.split("/", 1)[1].strip()
    return normalized


def is_openai_embedding_model(model_name: str | None) -> bool:
    """Return True when the model name refers to an OpenAI embedding model."""
    normalized = normalize_embedding_model_name(model_name).lower()
    return normalized.startswith("text-embedding-")


def resolve_embedding_dimensions(
    model_name: str | None,
    configured_dimensions: int | None = None,
) -> int | None:
    """Resolve dimensions for known OpenAI embedding models."""
    if configured_dimensions is not None:
        return configured_dimensions
    normalized = normalize_embedding_model_name(model_name).lower()
    return OPENAI_EMBEDDING_DIMENSIONS.get(normalized)
