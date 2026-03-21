from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from backend.services.faiss_vector_search import FAISSVectorSearch


class _FakeIndex:
    ntotal = 3

    def search(self, query_np, k):
        distances = np.array([[0.91, 0.82, 0.73]], dtype=np.float32)
        indices = np.array([[0, 1, 2]], dtype=np.int64)
        return distances, indices


def test_search_uses_raw_rank_score_when_provider_filter_skips_items():
    searcher = object.__new__(FAISSVectorSearch)
    searcher.index = _FakeIndex()
    searcher.metadata_list = [
        {"code": "A", "name": "WorldBank A", "provider": "WORLDBANK"},
        {"code": "B", "name": "FRED B", "provider": "FRED"},
        {"code": "C", "name": "WorldBank C", "provider": "WORLDBANK"},
    ]
    searcher.embed_text = lambda text: [0.0] * 384

    results = searcher.search("imports to gdp", limit=2, provider_filter="WORLDBANK")

    assert len(results) == 2
    assert results[0].code == "A"
    assert results[1].code == "C"
    # Second kept result should keep its original FAISS rank score (0.73), not 0.82.
    assert float(results[1].distance) == float(np.float32(0.73))


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
