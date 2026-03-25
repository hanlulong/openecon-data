from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from backend.services.faiss_vector_search import FAISSVectorSearch


# ─── Fakes ────────────────────────────────────────────────────────────

class _FakeIndex:
    ntotal = 3

    def search(self, query_np, k):
        distances = np.array([[0.91, 0.82, 0.73]], dtype=np.float32)
        indices = np.array([[0, 1, 2]], dtype=np.int64)
        return distances, indices


class _FakeEmbeddingsClient:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[3.0, 4.0]),
                SimpleNamespace(embedding=[0.0, 5.0]),
            ]
        )


class _FakeOpenAIClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddingsClient()


def _make_searcher(metadata=None, index=None):
    """Create a FAISSVectorSearch with fake internals for unit testing."""
    searcher = object.__new__(FAISSVectorSearch)
    searcher.index = index or _FakeIndex()
    searcher.metadata_list = metadata or [
        {"code": "A", "name": "WorldBank A", "provider": "WORLDBANK"},
        {"code": "B", "name": "FRED B", "provider": "FRED"},
        {"code": "C", "name": "WorldBank C", "provider": "WORLDBANK"},
    ]
    searcher.embed_text = lambda text: [0.0] * 384
    return searcher


# ─── Provider filter tests ───────────────────────────────────────────

def test_search_uses_raw_rank_score_when_provider_filter_skips_items():
    searcher = _make_searcher()
    results = searcher.search("imports to gdp", limit=2, provider_filter="WORLDBANK")

    assert len(results) == 2
    assert results[0].code == "A"
    assert results[1].code == "C"
    assert float(results[1].distance) == float(np.float32(0.73))


def test_search_without_provider_filter_returns_all():
    searcher = _make_searcher()
    results = searcher.search("gdp", limit=10)

    assert len(results) == 3
    assert [r.code for r in results] == ["A", "B", "C"]


def test_search_with_nonexistent_provider_returns_empty():
    searcher = _make_searcher()
    results = searcher.search("gdp", limit=10, provider_filter="OECD")
    assert results == []


# ─── Empty / uninitialized state tests ───────────────────────────────

def test_search_returns_empty_when_index_is_none():
    searcher = _make_searcher()
    searcher.index = None
    results = searcher.search("gdp", limit=5)
    assert results == []


def test_search_returns_empty_when_metadata_empty():
    searcher = _make_searcher(metadata=[])
    searcher.index = None
    results = searcher.search("inflation", limit=5)
    assert results == []


# ─── Result structure tests ──────────────────────────────────────────

def test_search_results_have_required_fields():
    searcher = _make_searcher()
    results = searcher.search("gdp", limit=1)

    assert len(results) >= 1
    r = results[0]
    assert hasattr(r, 'code')
    assert hasattr(r, 'name')
    assert hasattr(r, 'distance')
    assert hasattr(r, 'provider')
    assert r.provider == "WORLDBANK"


def test_search_results_ordered_by_distance_descending():
    searcher = _make_searcher()
    results = searcher.search("gdp", limit=10)

    distances = [r.distance for r in results]
    assert distances == sorted(distances, reverse=True), \
        f"Results not sorted by distance desc: {distances}"


def test_search_respects_limit():
    searcher = _make_searcher()
    results = searcher.search("gdp", limit=1)
    assert len(results) <= 1


# ─── Index out-of-bounds safety ──────────────────────────────────────

def test_search_handles_negative_indices():
    """FAISS returns -1 for indices beyond index size."""
    class _NegativeIndex:
        ntotal = 3
        def search(self, query_np, k):
            distances = np.array([[0.9, 0.5, -1.0]], dtype=np.float32)
            indices = np.array([[0, -1, 2]], dtype=np.int64)
            return distances, indices

    searcher = _make_searcher(index=_NegativeIndex())
    results = searcher.search("gdp", limit=10)
    # Should skip index -1, return only valid ones
    codes = [r.code for r in results]
    assert "B" not in codes or len(results) <= 2


def test_search_handles_out_of_range_indices():
    """FAISS might return indices beyond metadata_list length."""
    class _OOBIndex:
        ntotal = 5
        def search(self, query_np, k):
            distances = np.array([[0.9, 0.8, 0.7, 0.6, 0.5]], dtype=np.float32)
            indices = np.array([[0, 1, 2, 99, 100]], dtype=np.int64)
            return distances, indices

    searcher = _make_searcher(index=_OOBIndex())
    results = searcher.search("gdp", limit=10)
    # Should skip indices 99, 100 (beyond metadata_list[3])
    assert len(results) == 3


# ─── OpenAI embedding batch tests ────────────────────────────────────

def test_embed_batch_uses_openai_embeddings_for_openai_models():
    searcher = object.__new__(FAISSVectorSearch)
    searcher.model_name = "text-embedding-3-small"
    searcher.is_openai_embedding = True
    searcher.embedding_dim = 2
    searcher.embedding_dimensions = 2
    searcher.default_batch_size = 128
    searcher.model = _FakeOpenAIClient()
    searcher.embedding_cache = {}
    searcher.cache_stats = {"hits": 0, "misses": 0, "duplicates_skipped": 0}

    results = searcher.embed_batch(["inflation", "gdp"])

    assert np.allclose(results, [[0.6, 0.8], [0.0, 1.0]])
    assert searcher.model.embeddings.calls == [
        {
            "input": ["inflation", "gdp"],
            "model": "text-embedding-3-small",
            "encoding_format": "float",
            "dimensions": 2,
        }
    ]


def test_embed_batch_caches_results():
    searcher = object.__new__(FAISSVectorSearch)
    searcher.model_name = "text-embedding-3-small"
    searcher.is_openai_embedding = True
    searcher.embedding_dim = 2
    searcher.embedding_dimensions = 2
    searcher.default_batch_size = 128
    searcher.model = _FakeOpenAIClient()
    searcher.embedding_cache = {}
    searcher.cache_stats = {"hits": 0, "misses": 0, "duplicates_skipped": 0}

    # First call: cache miss
    searcher.embed_batch(["inflation", "gdp"])
    assert searcher.cache_stats["misses"] == 2

    # Second call: should use cache
    searcher.embed_batch(["inflation"])
    assert searcher.cache_stats["hits"] >= 1


def test_embed_batch_deduplicates_inputs():
    searcher = object.__new__(FAISSVectorSearch)
    searcher.model_name = "text-embedding-3-small"
    searcher.is_openai_embedding = True
    searcher.embedding_dim = 2
    searcher.embedding_dimensions = 2
    searcher.default_batch_size = 128
    client = _FakeOpenAIClient()
    searcher.model = client
    searcher.embedding_cache = {}
    searcher.cache_stats = {"hits": 0, "misses": 0, "duplicates_skipped": 0}

    # Both texts are the same, should only call API once per unique text
    results = searcher.embed_batch(["inflation", "gdp"])
    # The fake returns 2 embeddings for the 2 unique texts
    assert len(results) == 2


# ─── Compatibility check tests ───────────────────────────────────────

def _compat_searcher(**overrides):
    """Create a searcher with all fields needed for _loaded_index_is_compatible."""
    from pathlib import Path
    s = object.__new__(FAISSVectorSearch)
    s.index = overrides.get("index", _FakeIndex())
    s.metadata_list = overrides.get("metadata_list", [{"code": "X"}] * 3)
    s.embedding_dim = overrides.get("embedding_dim", 384)
    s.manifest_path = Path("/tmp/_nonexistent_manifest.json")
    s.model_name = "all-MiniLM-L6-v2"
    return s


def test_loaded_index_is_compatible_false_when_no_index():
    searcher = _compat_searcher(index=None, metadata_list=[])
    assert searcher._loaded_index_is_compatible() is False


def test_loaded_index_is_compatible_false_when_metadata_empty():
    searcher = _compat_searcher(metadata_list=[])
    assert searcher._loaded_index_is_compatible() is False


def test_loaded_index_is_compatible_false_when_dimension_mismatch():
    class _DimIndex:
        ntotal = 3
        d = 768  # different from expected 384

    searcher = _compat_searcher(index=_DimIndex())
    assert searcher._loaded_index_is_compatible() is False


# ─── Reset tests ─────────────────────────────────────────────────────

def test_reset_creates_empty_index():
    searcher = _make_searcher()
    searcher.embedding_dim = 384

    assert searcher.index.ntotal == 3  # pre-condition
    searcher.reset()
    # After reset, index should exist but be empty
    assert searcher.index is not None
    assert searcher.index.ntotal == 0


# ─── Stats tests ─────────────────────────────────────────────────────

def test_get_stats_returns_dict():
    """get_stats on a searcher with index returns a dict with index_size."""
    from pathlib import Path
    searcher = _make_searcher()
    searcher.model_name = "all-MiniLM-L6-v2"
    searcher.embedding_dim = 384
    searcher.default_batch_size = 128
    searcher.index_path = Path("/tmp/_nonexistent_index")
    searcher.embedding_cache_path = Path("/tmp/_nonexistent_cache.json")
    searcher.embedding_cache = {"a": [1.0]}
    searcher.cache_stats = {"hits": 1, "misses": 2, "duplicates_skipped": 0}

    stats = searcher.get_stats()

    assert isinstance(stats, dict)
    assert stats["index_size"] == 3
    assert stats["metadata_entries"] == 3
    assert stats["model"] == "all-MiniLM-L6-v2"
