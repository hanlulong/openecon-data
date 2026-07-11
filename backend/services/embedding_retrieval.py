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
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
INDEX_DIR = Path(__file__).parent.parent / "data" / "openai_embeddings"
INDEX_FILE = INDEX_DIR / "indicator_embeddings.npz"
META_FILE = INDEX_DIR / "indicator_metadata.json"
# Pre-normalized float32 matrix stored as a raw .npy so every process can
# np.load(..., mmap_mode="r") it. The OS page cache then shares ONE copy of
# the ~2GB matrix across all workers instead of each process holding its own
# in-RAM copy (3 processes × 1.9GB previously).
INDEX_NPY = INDEX_DIR / "indicator_embeddings_normalized.npy"


def _write_normalized_npy_atomic(embeddings: np.ndarray) -> None:
    """Atomically write the pre-normalized float32 matrix to INDEX_NPY.

    Writes to a temp file in the same directory then os.replace()s it so
    readers never see a partially written file (os.replace is atomic on the
    same filesystem). Safe to call while other processes have the old file
    mmap'd — their mapping keeps pointing at the old inode.
    """
    import tempfile

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(INDEX_DIR), prefix=INDEX_NPY.stem + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            np.save(f, embeddings)
        # mkstemp creates 0600 files; match the other index artifacts so any
        # service user can mmap the file read-only.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, INDEX_NPY)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def convert_index_to_mmap(force: bool = False) -> bool:
    """Convert INDEX_FILE (npz) into the pre-normalized .npy used for mmap.

    Idempotent: skips work when INDEX_NPY already exists and is newer than the
    npz (pass force=True to rebuild anyway). Loads the full matrix into RAM
    transiently (~2GB for 330K×1536 float32) — run explicitly via
    `python -m backend.services.embedding_retrieval`, NOT from the loader,
    so concurrent worker processes never race on the conversion.
    """
    if not INDEX_FILE.exists():
        logger.error("Cannot convert: %s does not exist", INDEX_FILE)
        return False
    if (
        not force
        and INDEX_NPY.exists()
        and INDEX_NPY.stat().st_mtime >= INDEX_FILE.stat().st_mtime
    ):
        logger.info("Normalized index already up to date: %s", INDEX_NPY)
        return True

    start = time.time()
    logger.info("Loading %s ...", INDEX_FILE)
    data = np.load(INDEX_FILE)
    emb = data["embeddings"].astype(np.float32)
    # Same normalization as the legacy in-RAM path: L2 rows, zero rows kept.
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1
    emb /= norms
    _write_normalized_npy_atomic(emb)
    logger.info(
        "Wrote pre-normalized index %s (%d×%d float32, %.0fMB) in %.0fs",
        INDEX_NPY, emb.shape[0], emb.shape[1],
        emb.nbytes / (1024 * 1024), time.time() - start,
    )
    return True


class EmbeddingRetrieval:
    """Retrieve indicators by semantic similarity using OpenAI embeddings."""

    def __init__(self):
        self._embeddings: Optional[np.ndarray] = None
        self._codes: List[str] = []
        self._names: List[str] = []
        self._providers: List[str] = []
        self._client = None
        self._loaded = False
        # Guard lazy init so concurrent threads (the selector now offloads
        # retrieval via asyncio.to_thread) can't double-load the ~1GB index or
        # build two embedding clients.
        self._load_lock = threading.Lock()
        self._client_lock = threading.Lock()

    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:  # another thread built it
                return self._client
            import openai
            from ..config import Settings
            settings = Settings()
            # Use OpenAI key if available, fall back to OpenRouter
            api_key = os.environ.get("OPENAI_API_KEY") or getattr(settings, "openai_api_key", None)
            base_url = None
            if not api_key:
                api_key = settings.openrouter_api_key
                base_url = "https://openrouter.ai/api/v1"
            # Bound every embeddings.create call: without an explicit timeout
            # the OpenAI client defaults to ~600s, so a hung endpoint can pin a
            # thread-pool thread (the selector offloads retrieval via
            # asyncio.to_thread) for ~10 minutes and starve the pool.
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=30)
        return self._client

    def _load_index(self) -> bool:
        """Load pre-built embedding index from disk.

        Preferred path: INDEX_NPY, a pre-normalized float32 .npy opened with
        mmap_mode="r". The matrix is never copied into process RAM — pages are
        faulted in on demand and shared across ALL processes (uvicorn workers,
        MCP service) through the OS page cache, so N processes cost ~2GB once
        instead of N×1.9GB.

        Fallback: the compressed npz (full RAM load + normalize), used only
        when the .npy is missing. Run
        `python -m backend.services.embedding_retrieval` to create it.
        """
        if self._loaded:
            return True
        with self._load_lock:
            if self._loaded:  # another thread finished the load while we waited
                return True
            return self._load_index_locked()

    def _load_index_locked(self) -> bool:
        if not META_FILE.exists() or not (INDEX_NPY.exists() or INDEX_FILE.exists()):
            return False
        try:
            with open(META_FILE) as f:
                meta = json.load(f)
            self._codes = meta["codes"]
            self._names = meta["names"]
            self._providers = meta["providers"]

            embeddings = None
            mmap_loaded = False
            if INDEX_NPY.exists():
                candidate = np.load(INDEX_NPY, mmap_mode="r")
                # Guard against a stale/foreign .npy: row count must match the
                # metadata and dtype must be float32 (search assumes both).
                if candidate.dtype == np.float32 and candidate.shape[0] == len(self._codes):
                    embeddings = candidate  # pre-normalized, read-only memmap
                    mmap_loaded = True
                else:
                    logger.warning(
                        "Ignoring %s (dtype=%s, rows=%d, metadata rows=%d) — "
                        "stale or invalid; falling back to npz. Re-run "
                        "`python -m backend.services.embedding_retrieval` to rebuild it.",
                        INDEX_NPY, candidate.dtype, candidate.shape[0], len(self._codes),
                    )

            if embeddings is None:
                logger.warning(
                    "Pre-normalized index %s not usable — loading %s fully into RAM "
                    "(~2GB per process). Run "
                    "`python -m backend.services.embedding_retrieval` once to enable "
                    "the memory-mapped shared index.",
                    INDEX_NPY.name, INDEX_FILE.name,
                )
                data = np.load(INDEX_FILE)
                # Normalize for cosine similarity and keep as float32 in memory.
                emb_f32 = data["embeddings"].astype(np.float32)
                norms = np.linalg.norm(emb_f32, axis=1, keepdims=True)
                norms[norms == 0] = 1
                embeddings = emb_f32 / norms

            self._embeddings = embeddings
            # Pre-compute per-provider masks for fast filtering
            self._provider_masks: dict[str, np.ndarray] = {}
            providers_upper = [p.upper() for p in self._providers]
            for prov in set(providers_upper):
                self._provider_masks[prov] = np.array(
                    [p == prov for p in providers_upper], dtype=bool
                )
            self._loaded = True
            if mmap_loaded:
                logger.info(
                    "Loaded embedding index: %d indicators, dim=%d "
                    "(float32 mmap, shared page cache)",
                    len(self._codes), self._embeddings.shape[1],
                )
            else:
                logger.info(
                    "Loaded embedding index: %d indicators, dim=%d (float32, %.0fMB RAM)",
                    len(self._codes), self._embeddings.shape[1],
                    self._embeddings.nbytes / (1024 * 1024),
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

        c.execute(
            "SELECT code, name, provider, synonyms FROM indicators WHERE LENGTH(name) > 3"
        )
        rows = c.fetchall()
        logger.info("Building embedding index for %d indicators...", len(rows))

        codes = [r[0] for r in rows]
        names = [r[1] for r in rows]
        providers = [r[2] for r in rows]

        def _embed_text(name: str, synonyms) -> str:
            """Name plus user-vocabulary synonyms as the embedded text.

            Names alone miss how users phrase concepts: StatsCan cube titles
            name the SURVEY ("Labour force characteristics, monthly") while
            the metric ("unemployment rate") lives in synonyms; WDI's
            SP.POP.TOTL is titled "Population, total" while users write
            "world population". Structural aliases (legacy table numbers)
            add no semantics but little noise at this scale. Capped so one
            row can't dominate the token budget.
            """
            base = str(name or "")[:200]
            syn = str(synonyms or "").strip()
            if not syn:
                return base
            return f"{base} | {syn[:300]}"

        embed_texts = [_embed_text(r[1], r[3]) for r in rows]

        client = self._get_client()
        # Preallocate the float32 matrix: accumulating 330K x 1536 floats as
        # Python lists needs >12 GB and OOM-kills the build; the array is 2 GB.
        embeddings = np.zeros((len(embed_texts), EMBEDDING_DIM), dtype=np.float32)
        start = time.time()

        for i in range(0, len(embed_texts), batch_size):
            batch = embed_texts[i : i + batch_size]
            try:
                resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
                for j, d in enumerate(resp.data):
                    embeddings[i + j] = d.embedding
            except Exception as e:
                logger.error("Embedding batch %d failed: %s", i, e)
                # Leave zeros for the failed batch

            if (i + batch_size) % 5000 == 0 or i + batch_size >= len(names):
                elapsed = time.time() - start
                logger.info(
                    "  Embedded %d/%d (%.0fs)", min(i + batch_size, len(names)), len(names), elapsed
                )

        # Save to disk
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(INDEX_FILE, embeddings=embeddings)
        with open(META_FILE, "w") as f:
            json.dump({"codes": codes, "names": names, "providers": providers}, f)

        elapsed = time.time() - start
        logger.info("Embedding index built: %d indicators in %.0fs", len(codes), elapsed)

        # Load into memory (normalize IN PLACE — a divided copy doubles peak RSS)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings /= norms
        self._embeddings = embeddings
        self._codes = codes
        self._names = names
        self._providers = providers
        self._loaded = True

        # Also persist the pre-normalized .npy so worker processes keep using
        # the memory-mapped shared index after a rebuild (atomic replace —
        # processes mmap'ing the old file are unaffected until they reload).
        try:
            _write_normalized_npy_atomic(self._embeddings)
            logger.info("Wrote pre-normalized mmap index: %s", INDEX_NPY)
        except Exception as e:
            logger.error("Failed to write pre-normalized index %s: %s", INDEX_NPY, e)

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

        # Cosine similarity — embeddings are pre-normalized float32
        # (read-only memmap on the fast path; matmul works directly on it)
        sims = query_emb @ self._embeddings.T

        # Filter by provider using pre-computed masks (3ms vs 65ms for list comp)
        if provider:
            provider_upper = provider.upper()
            mask = self._provider_masks.get(provider_upper)
            if mask is not None:
                sims = np.where(mask, sims, -1)
            else:
                # Unknown provider — no results
                return []

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
_instance_lock = threading.Lock()


def get_embedding_retrieval() -> EmbeddingRetrieval:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:  # double-checked: only build once under threads
                _instance = EmbeddingRetrieval()
    return _instance


if __name__ == "__main__":
    # Explicit one-shot conversion: npz → pre-normalized mmap-able .npy.
    # Usage: python -m backend.services.embedding_retrieval [--force]
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(0 if convert_index_to_mmap(force="--force" in sys.argv[1:]) else 1)
