"""
Vector search service with support for multiple backends (FAISS and ChromaDB).

This service provides semantic search capabilities using dense vector embeddings.
It complements the existing FTS5 BM25 search for a hybrid retrieval approach.

Backend Selection:
- FAISS (default): 100x faster than ChromaDB, <100ms load time, <5ms search
- ChromaDB: More features, slower startup, better for persistent search
"""

__all__ = ['VectorSearchService', 'VectorSearchResult', 'VECTOR_SEARCH_AVAILABLE']

import os
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from openai import OpenAI

from ..config import get_settings
from ..embedding_utils import (
    is_openai_embedding_model,
    normalize_embedding_model_name,
    resolve_embedding_dimensions,
)

logger = logging.getLogger(__name__)

# Optional vector search dependencies - gracefully degrade if not available
VECTOR_SEARCH_AVAILABLE = False
FAISS_AVAILABLE = False
CHROMA_AVAILABLE = False

try:
    import faiss
    FAISS_AVAILABLE = True
    logger.info("✅ FAISS available (faiss-cpu)")
except ImportError:
    logger.debug("ℹ️  FAISS not available")

try:
    import chromadb
    from chromadb.config import Settings
    CHROMA_AVAILABLE = True
    logger.info("✅ ChromaDB available (chromadb, sentence-transformers)")
except ImportError:
    logger.debug("ℹ️  ChromaDB not available")

# Vector search is available if either backend is available
VECTOR_SEARCH_AVAILABLE = FAISS_AVAILABLE or CHROMA_AVAILABLE

if VECTOR_SEARCH_AVAILABLE:
    logger.info(f"✅ Vector search available (FAISS: {FAISS_AVAILABLE}, ChromaDB: {CHROMA_AVAILABLE})")
else:
    logger.warning("⚠️ Vector search dependencies not available. Falling back to BM25-only search.")
    logger.info("Install with: pip install faiss-cpu sentence-transformers chromadb")

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None


@dataclass
class VectorSearchResult:
    """Result from vector search."""
    code: str
    name: str
    provider: str
    distance: float  # Lower is better (cosine distance)

    @property
    def similarity(self) -> float:
        """Convert distance to similarity score (0-1, higher is better)."""
        return 1.0 - self.distance


class VectorSearchService:
    """
    Vector search service with multiple backend support (FAISS or ChromaDB).

    Features:
    - FAISS backend: 100x faster than ChromaDB (<100ms load, <5ms search)
    - ChromaDB backend: More features, slower startup
    - Automatic fallback if preferred backend not available
    - Supports sentence-transformers or OpenAI embedding models

    The service automatically selects the best available backend:
    1. FAISS if available and enabled (default)
    2. ChromaDB if FAISS not available
    3. Disabled if neither available
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        persist_directory: str = "backend/data/chroma_db",
        collection_name: str = "economic_indicators",
        use_faiss: bool = True,  # Prefer FAISS by default (100x faster)
        embedding_dimensions: Optional[int] = None,
    ):
        """
        Initialize the vector search service.

        Args:
            model_name: Embedding model name for vector search
            persist_directory: Directory to persist Chroma database (if using ChromaDB)
            collection_name: Name of the Chroma collection (if using ChromaDB)
            use_faiss: Prefer FAISS backend if available (default: True)
            embedding_dimensions: Optional OpenAI embedding dimension override
        """
        settings = get_settings()
        resolved_model_name = normalize_embedding_model_name(
            model_name or settings.embedding_model
        )
        resolved_dimensions = resolve_embedding_dimensions(
            resolved_model_name,
            embedding_dimensions if embedding_dimensions is not None else settings.embedding_dimensions,
        )

        if is_openai_embedding_model(resolved_model_name) and resolved_dimensions is None:
            raise ValueError(
                "Unknown OpenAI embedding dimensions for model "
                f"'{resolved_model_name}'. Set EMBEDDING_DIMENSIONS explicitly."
            )

        self.settings = settings
        self.model_name = resolved_model_name
        self.is_openai_embedding = is_openai_embedding_model(resolved_model_name)
        self.embedding_dimensions = resolved_dimensions
        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self.model = None
        self.client = None
        self.collection = None  # For ChromaDB
        self.backend = None  # For FAISS
        self.use_faiss = use_faiss
        self._initialized = False  # Track lazy initialization

        # Check if vector search dependencies are available
        if not VECTOR_SEARCH_AVAILABLE:
            logger.warning("⚠️ Vector search dependencies not available. Service disabled.")
            return

        logger.info(f"🚀 Initializing VectorSearchService (lazy mode)")
        logger.info(f"   - Model: {self.model_name}")
        logger.info(f"   - Prefer FAISS: {use_faiss}")
        logger.info(f"   - Model and backend will be loaded on first use")

    def _ensure_initialized(self):
        """Lazily initialize model and backend on first use."""
        if self._initialized:
            return

        if not VECTOR_SEARCH_AVAILABLE:
            raise RuntimeError("Vector search dependencies not available")

        logger.info(f"⏳ Performing lazy initialization of VectorSearchService...")

        # Load embedding model
        self._load_model()

        # Initialize backend
        self._init_backend()

        self._initialized = True
        logger.info(f"✅ VectorSearchService lazy initialization complete")

    def _load_model(self):
        try:
            if self.is_openai_embedding:
                if not self.settings.openai_api_key:
                    raise ValueError("OPENAI_API_KEY is required for OpenAI embeddings")

                logger.info(f"📥 Initializing OpenAI embedding client: {self.model_name}")
                self.model = OpenAI(
                    api_key=self.settings.openai_api_key,
                    base_url=self.settings.openai_base_url,
                    organization=self.settings.openai_org_id,
                )
                logger.info(f"✅ OpenAI embedding client ready")
                logger.info(f"   - Embedding dimension: {self.embedding_dimensions}")
                return

            if SentenceTransformer is None:
                logger.error("❌ sentence-transformers not available")
                return

            logger.info(f"📥 Loading embedding model: {self.model_name}")
            self.model = SentenceTransformer(self.model_name)
            logger.info(f"✅ Model loaded: {self.model_name}")
            logger.info(f"   - Embedding dimension: {self.model.get_sentence_embedding_dimension()}")
        except Exception as e:
            logger.error(f"❌ Failed to load model: {e}", exc_info=True)
            raise

    def _init_backend(self):
        """Initialize the preferred backend (FAISS or ChromaDB)."""
        # Try FAISS first if enabled
        if self.use_faiss and FAISS_AVAILABLE:
            try:
                logger.info("📦 Initializing FAISS backend")
                from .faiss_vector_search import FAISSVectorSearch
                self.backend = FAISSVectorSearch(
                    model_name=self.model_name,
                    embedding_dim=self.embedding_dimensions or 384,
                    embedding_dimensions=self.embedding_dimensions,
                )
                logger.info(f"✅ FAISS backend initialized")
                return
            except Exception as e:
                logger.warning(f"⚠️  Failed to initialize FAISS backend: {e}")
                logger.info("Falling back to ChromaDB...")

        # Fall back to ChromaDB
        if CHROMA_AVAILABLE:
            try:
                logger.info("📦 Initializing ChromaDB backend")
                self._init_chroma()
                logger.info(f"✅ ChromaDB backend initialized")
            except Exception as e:
                logger.error(f"❌ Failed to initialize ChromaDB backend: {e}", exc_info=True)
                raise
        else:
            logger.error("❌ No vector search backends available")
            raise RuntimeError("FAISS and ChromaDB both unavailable")

    def _init_chroma(self):
        """Initialize Chroma client and collection."""
        try:
            logger.info(f"📦 Initializing Chroma client (persist_directory: {self.persist_directory})")

            # Create persist directory if it doesn't exist
            os.makedirs(self.persist_directory, exist_ok=True)

            # Initialize Chroma client
            self.client = chromadb.PersistentClient(
                path=self.persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                )
            )

            # Get or create collection using ChromaDB's built-in method
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}  # Use cosine similarity
            )
            doc_count = self.collection.count()
            if doc_count > 0:
                logger.info(f"✅ Loaded existing collection: {self.collection_name}")
                logger.info(f"   - Document count: {doc_count}")
            else:
                logger.info(f"✅ Created new collection: {self.collection_name}")

        except Exception as e:
            logger.error(f"❌ Failed to initialize Chroma: {e}", exc_info=True)
            raise

    def embed_text(self, text: str) -> List[float]:
        """
        Generate embedding vector for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        self._ensure_initialized()
        if self.is_openai_embedding:
            return self._embed_batch_openai([text])[0]
        return self.model.encode(text, convert_to_numpy=True).tolist()

    def embed_batch(self, texts: List[str], batch_size: int = 128) -> List[List[float]]:
        """
        Generate embeddings for multiple texts efficiently.

        Optimized batch size: 128 (3-4x improvement vs 32)

        Args:
            texts: List of texts to embed
            batch_size: Batch size for encoding (default: 128 for better throughput)

        Returns:
            List of embedding vectors
        """
        self._ensure_initialized()
        if self.is_openai_embedding:
            return self._embed_batch_openai(texts)
        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=True,
            convert_to_numpy=True
        )
        return embeddings.tolist()

    def _embed_batch_openai(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts using OpenAI."""
        request_kwargs: Dict[str, Any] = {
            "input": texts,
            "model": self.model_name,
            "encoding_format": "float",
        }
        if self.embedding_dimensions is not None:
            request_kwargs["dimensions"] = self.embedding_dimensions

        response = self.model.embeddings.create(**request_kwargs)
        return [item.embedding for item in response.data]

    def index_indicators(self, indicators: List[Dict[str, Any]], batch_size: int = 100, clear_existing: bool = True):
        """
        Index economic indicators in the vector database.

        Args:
            indicators: List of indicator dicts with keys: code, name, provider
            batch_size: Batch size for indexing (default: 100)
            clear_existing: Whether to clear existing collection before indexing (default: True)
        """
        if not indicators:
            logger.warning("⚠️  No indicators provided for indexing")
            return

        self._ensure_initialized()

        # Delegate to appropriate backend
        if self.backend is not None:
            # Using FAISS
            self.backend.index_indicators(indicators, batch_size, clear_existing)
        elif self.collection is not None:
            # Using ChromaDB
            self._index_indicators_chroma(indicators, batch_size, clear_existing)
        else:
            logger.error("❌ No vector search backend available")

    def _index_indicators_chroma(self, indicators: List[Dict[str, Any]], batch_size: int = 100, clear_existing: bool = True):
        """Index indicators using ChromaDB backend."""
        logger.info(f"📊 Indexing {len(indicators)} indicators into ChromaDB...")

        # Clear existing collection if re-indexing and clear_existing is True
        if clear_existing:
            current_count = self.collection.count()
            if current_count > 0:
                logger.info(f"🗑️  Clearing existing {current_count} documents...")
                # Get all document IDs and delete them (preserving collection)
                try:
                    # Get all documents in the collection
                    results = self.collection.get()
                    if results and results.get("ids"):
                        all_ids = results["ids"]
                        logger.info(f"   Deleting {len(all_ids)} documents...")
                        # Delete in batches to avoid timeout
                        batch_size_delete = 1000
                        for i in range(0, len(all_ids), batch_size_delete):
                            batch_ids = all_ids[i:i+batch_size_delete]
                            self.collection.delete(ids=batch_ids)
                        logger.info(f"   ✅ Deleted {len(all_ids)} documents")
                except Exception as e:
                    logger.warning(f"   ⚠️ Error clearing documents, recreating collection: {e}")
                    # Fallback: delete and recreate collection
                    self.client.delete_collection(name=self.collection_name)
                    import time
                    time.sleep(0.5)  # Small delay to avoid race condition
                    self.collection = self.client.get_or_create_collection(
                        name=self.collection_name,
                        metadata={"hnsw:space": "cosine"}
                    )

        # Process in batches
        total = len(indicators)
        for i in range(0, total, batch_size):
            batch = indicators[i:i+batch_size]
            batch_end = min(i + batch_size, total)

            logger.info(f"   Processing batch {i+1}-{batch_end}/{total}...")

            # Prepare batch data
            ids = [ind["code"] for ind in batch]
            documents = [ind["name"] for ind in batch]
            metadatas = [
                {
                    "code": ind["code"],
                    "name": ind["name"],
                    "provider": ind["provider"],
                    **({"original_code": ind["original_code"]} if "original_code" in ind else {})
                }
                for ind in batch
            ]

            # Generate embeddings (batch size: 128 for better throughput)
            embeddings = self.embed_batch(documents, batch_size=128)

            # Add to collection
            self.collection.add(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas
            )

        final_count = self.collection.count()
        logger.info(f"✅ Indexing complete: {final_count} indicators indexed")

    def search(
        self,
        query: str,
        limit: int = 10,
        where: Optional[Dict[str, Any]] = None
    ) -> List[VectorSearchResult]:
        """
        Search for indicators using vector similarity.

        Args:
            query: Search query text
            limit: Maximum number of results (default: 10)
            where: Optional metadata filter (e.g., {"provider": "WORLDBANK"})

        Returns:
            List of VectorSearchResult ordered by similarity (best first)
        """
        # Return empty list if not available
        if not VECTOR_SEARCH_AVAILABLE:
            logger.debug("Vector search not available, returning empty results")
            return []

        try:
            self._ensure_initialized()
        except Exception as e:
            logger.warning(f"⚠️ Failed to initialize vector search: {e}")
            return []

        # Delegate to appropriate backend
        if self.backend is not None:
            # Using FAISS
            provider_filter = where.get("provider") if where else None
            return self.backend.search(query, limit, provider_filter)
        elif self.collection is not None:
            # Using ChromaDB
            return self._search_chroma(query, limit, where)
        else:
            logger.debug("Vector search not initialized")
            return []

    def _search_chroma(self, query: str, limit: int = 10, where: Optional[Dict[str, Any]] = None) -> List[VectorSearchResult]:
        """Search using ChromaDB backend."""
        # Check if collection is empty
        if self.collection.count() == 0:
            logger.warning("⚠️  Vector database is empty - call index_indicators() first")
            return []

        # Generate query embedding
        query_embedding = self.embed_text(query)

        # Search in Chroma
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=limit,
            where=where,
            include=["metadatas", "distances"]
        )

        # Convert to VectorSearchResult objects
        search_results = []
        if results and results["ids"] and len(results["ids"]) > 0:
            for i, doc_id in enumerate(results["ids"][0]):
                metadata = results["metadatas"][0][i]
                distance = results["distances"][0][i]

                search_results.append(VectorSearchResult(
                    code=metadata["code"],
                    name=metadata["name"],
                    provider=metadata["provider"],
                    distance=distance
                ))

        return search_results

    def is_indexed(self) -> bool:
        """Check if the vector database has been indexed."""
        try:
            self._ensure_initialized()
        except Exception:
            return False

        if self.backend is not None:
            return self.backend.is_indexed()
        elif self.collection is not None:
            return self.collection.count() > 0
        return False

    def get_index_size(self) -> int:
        """Get the number of indexed indicators."""
        try:
            self._ensure_initialized()
        except Exception:
            return 0

        if self.backend is not None:
            return self.backend.get_index_size()
        elif self.collection is not None:
            return self.collection.count()
        return 0

    def reset(self):
        """Reset the vector database (delete all data)."""
        logger.warning("🗑️  Resetting vector database...")
        if self.backend is not None:
            self.backend.reset()
        elif self.collection is not None:
            self.client.delete_collection(name=self.collection_name)
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
        logger.info("✅ Vector database reset")


# Singleton instance
_vector_search_service: Optional[VectorSearchService] = None


def get_vector_search_service() -> VectorSearchService:
    """Get or create the singleton VectorSearchService instance."""
    global _vector_search_service
    if _vector_search_service is None:
        _vector_search_service = VectorSearchService()
    return _vector_search_service
