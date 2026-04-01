"""
Embedding-based Indicator Retrieval Service.

Architecture decision (2026-04-01):
  FTS5 keyword search: 30% top-5 accuracy — fails on vocabulary mismatches
  FAISS MiniLM-L6:      0% top-5 accuracy — model too generic for economic terms
  OpenAI embed-3-small: 80% top-5 accuracy — understands semantic meaning

This service replaces FTS5 as the primary retrieval layer. It embeds all
indicator names with OpenAI text-embedding-3-small and finds the nearest
indicators for any natural language query using cosine similarity.

Pipeline:  Query → OpenAI embed → top-20 nearest → LLM picks best match
Cost:      ~$0.0001 per query (1 embedding call) + ~$0.001 LLM selection
Latency:   ~300ms embedding + ~2s LLM = ~2.3s total
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
INDEX_DIR = Path(__file__).parent.parent / "data" / "openai_embeddings"
INDEX_FILE = INDEX_DIR / "indicator_embeddings.npz"
META_FILE = INDEX_DIR / "indicator_metadata.json"


class EmbeddingRetrieval:
    """Retrieve indicators by semantic similarity using OpenAI embeddings."""

    def __init__(self):
        self._embeddings: Optional[np.ndarray] = None
        self._codes: List[str] = []
        self._names: List[str] = []
        self._providers: List[str] = []
        self._client = None
        self._loaded = False

    def _get_client(self):
        if self._client is None:
            import openai
            from ..config import Settings
            settings = Settings()
            # Use OpenAI key if available, fall back to OpenRouter
            api_key = os.environ.get("OPENAI_API_KEY") or getattr(settings, "openai_api_key", None)
            base_url = None
            if not api_key:
                api_key = settings.openrouter_api_key
                base_url = "https://openrouter.ai/api/v1"
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        return self._client

    def _load_index(self) -> bool:
        """Load pre-built embedding index from disk."""
        if self._loaded:
            return True
        if not INDEX_FILE.exists() or not META_FILE.exists():
            return False
        try:
            data = np.load(INDEX_FILE)
            self._embeddings = data["embeddings"].astype(np.float16)  # Keep as float16 to save RAM
            with open(META_FILE) as f:
                meta = json.load(f)
            self._codes = meta["codes"]
            self._names = meta["names"]
            self._providers = meta["providers"]
            # Normalize for cosine similarity (in float32 for precision, store as float16)
            emb_f32 = self._embeddings.astype(np.float32)
            norms = np.linalg.norm(emb_f32, axis=1, keepdims=True)
            norms[norms == 0] = 1
            self._embeddings = (emb_f32 / norms).astype(np.float16)
            del emb_f32
            self._loaded = True
            logger.info(
                "Loaded embedding index: %d indicators, dim=%d",
                len(self._codes), self._embeddings.shape[1],
            )
            return True
        except Exception as e:
            logger.error("Failed to load embedding index: %s", e)
            return False

    def build_index(self, batch_size: int = 500) -> None:
        """Build embedding index for all indicators in the database."""
        from .indicator_database import IndicatorDatabase

        db = IndicatorDatabase()
        conn = db._get_connection()
        c = conn.cursor()

        c.execute("SELECT code, name, provider FROM indicators WHERE LENGTH(name) > 3")
        rows = c.fetchall()
        logger.info("Building embedding index for %d indicators...", len(rows))

        codes = [r[0] for r in rows]
        names = [r[1] for r in rows]
        providers = [r[2] for r in rows]

        client = self._get_client()
        all_embeddings = []
        start = time.time()

        for i in range(0, len(names), batch_size):
            batch = names[i : i + batch_size]
            # Truncate very long names
            batch = [n[:200] for n in batch]
            try:
                resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
                batch_embs = [d.embedding for d in resp.data]
                all_embeddings.extend(batch_embs)
            except Exception as e:
                logger.error("Embedding batch %d failed: %s", i, e)
                # Fill with zeros for failed batch
                all_embeddings.extend([[0.0] * EMBEDDING_DIM] * len(batch))

            if (i + batch_size) % 5000 == 0 or i + batch_size >= len(names):
                elapsed = time.time() - start
                logger.info(
                    "  Embedded %d/%d (%.0fs)", min(i + batch_size, len(names)), len(names), elapsed
                )

        embeddings = np.array(all_embeddings, dtype=np.float32)

        # Save to disk
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(INDEX_FILE, embeddings=embeddings)
        with open(META_FILE, "w") as f:
            json.dump({"codes": codes, "names": names, "providers": providers}, f)

        elapsed = time.time() - start
        logger.info("Embedding index built: %d indicators in %.0fs", len(codes), elapsed)

        # Load into memory
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self._embeddings = embeddings / norms
        self._codes = codes
        self._names = names
        self._providers = providers
        self._loaded = True

    def search(
        self,
        query: str,
        provider: Optional[str] = None,
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """Find the top-k most similar indicators for a query.

        Args:
            query: Natural language query
            provider: Optional provider filter
            top_k: Number of results to return

        Returns:
            List of dicts with code, name, provider, score
        """
        if not self._load_index():
            logger.warning("Embedding index not available. Run build_index() first.")
            return []

        client = self._get_client()
        try:
            resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[query])
            query_emb = np.array(resp.data[0].embedding, dtype=np.float32)
            query_emb = query_emb / np.linalg.norm(query_emb)
        except Exception as e:
            logger.error("Query embedding failed: %s", e)
            return []

        # Cosine similarity (cast to float32 for matmul)
        sims = query_emb @ self._embeddings.astype(np.float32).T

        # Filter by provider if specified
        if provider:
            provider_upper = provider.upper()
            mask = np.array([p.upper() == provider_upper for p in self._providers])
            sims = np.where(mask, sims, -1)

        top_indices = np.argsort(-sims)[:top_k]

        results = []
        for idx in top_indices:
            if sims[idx] <= 0:
                break
            results.append({
                "code": self._codes[idx],
                "name": self._names[idx],
                "provider": self._providers[idx],
                "score": float(sims[idx]),
            })

        return results


# Singleton
_instance: Optional[EmbeddingRetrieval] = None


def get_embedding_retrieval() -> EmbeddingRetrieval:
    global _instance
    if _instance is None:
        _instance = EmbeddingRetrieval()
    return _instance
