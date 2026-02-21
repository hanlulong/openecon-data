#!/usr/bin/env python3
"""
Metadata Loader Service

Loads indicator metadata from JSON files into ChromaDB for fast semantic search.
Integrates with existing VectorSearchService.
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from .vector_search import VectorSearchService, VECTOR_SEARCH_AVAILABLE

logger = logging.getLogger(__name__)


class MetadataLoader:
    """
    Load indicator metadata from JSON files into ChromaDB.

    Supports all data providers with a unified schema:
    {
        "provider": "WorldBank"|"FRED"|"IMF"|...,
        "indicators": [
            {
                "id": str,
                "code": str,
                "name": str,
                "description": str,
                "aliases": [str],
                "searchable_text": str,
                ...
            }
        ]
    }
    """

    def __init__(
        self,
        metadata_dir: str = "backend/data/metadata",
        vector_search: Optional[VectorSearchService] = None
    ):
        """
        Initialize metadata loader.

        Args:
            metadata_dir: Directory containing provider JSON files
            vector_search: VectorSearchService instance (creates new if None)
        """
        self.metadata_dir = Path(metadata_dir)
        self._vector_search = vector_search  # Store provided instance
        self._vector_search_initialized = False
        self.stats = {
            "providers_loaded": 0,
            "total_indicators": 0,
            "load_time": 0,
        }

    @property
    def vector_search(self) -> VectorSearchService:
        """
        Lazily initialize VectorSearchService on first access.
        This avoids blocking startup with model loading and FAISS initialization.
        """
        if self._vector_search is None and not self._vector_search_initialized:
            logger.info("🚀 Lazily initializing VectorSearchService (on-demand)...")
            try:
                self._vector_search = VectorSearchService()
                self._vector_search_initialized = True
                logger.info("✅ VectorSearchService initialized")
            except Exception as e:
                logger.error(f"❌ Failed to initialize VectorSearchService: {e}")
                self._vector_search_initialized = True  # Mark as attempted
                raise
        return self._vector_search

    async def load_all(self) -> Dict[str, Any]:
        """
        Load all provider metadata files.

        Returns:
            Dict with loading statistics
        """
        if not VECTOR_SEARCH_AVAILABLE:
            logger.warning("⚠️ Vector search not available - skipping metadata loading")
            return {
                "success": False,
                "error": "Vector search dependencies not installed"
            }

        start_time = datetime.now()
        logger.info(f"📥 Loading metadata from {self.metadata_dir}")

        if not self.metadata_dir.exists():
            logger.warning(f"⚠️ Metadata directory not found: {self.metadata_dir}")
            return {
                "success": False,
                "error": "Metadata directory not found"
            }

        # Find all JSON files
        json_files = list(self.metadata_dir.glob("*.json"))
        if not json_files:
            logger.warning(f"⚠️ No metadata files found in {self.metadata_dir}")
            return {
                "success": False,
                "error": "No metadata files found"
            }

        # Initialize vector search if needed
        if not hasattr(self.vector_search, 'collection') or self.vector_search.collection is None:
            # VectorSearchService initializes in __init__, just check it's ready
            pass

        # Check if collection is already indexed with the correct number of indicators
        try:
            current_count = self.vector_search.collection.count()
            expected_count = 31725  # FRED (2283) + IMF (132) + WorldBank (29310)

            if current_count == expected_count:
                logger.info(f"✅ Vector database already indexed with {current_count} indicators - skipping re-indexing")
                self.stats["providers_loaded"] = 3
                self.stats["total_indicators"] = expected_count
                self.stats["load_time"] = 0
                return {
                    "success": True,
                    **self.stats
                }
            elif current_count > 0:
                logger.info(f"⚠️ Vector database has {current_count} indicators (expected {expected_count}) - re-indexing")
        except Exception as e:
            logger.warning(f"⚠️ Error checking collection count: {e}")

        # Collect all indicators from all providers first
        all_indicators = []

        # Load each provider
        for json_file in sorted(json_files):
            try:
                result = await self.load_provider(json_file, index_immediately=False)
                if result["success"]:
                    self.stats["providers_loaded"] += 1
                    self.stats["total_indicators"] += result["indicators_loaded"]
                    all_indicators.extend(result["indicators"])
            except Exception as e:
                logger.error(f"❌ Error loading {json_file.name}: {e}")
                continue

        # Index all indicators at once
        # Skip re-indexing if collection already has data (speeds up startup)
        if all_indicators:
            try:
                # Check if collection already has data
                existing_count = self.vector_search.collection.count() if hasattr(self.vector_search, 'collection') and self.vector_search.collection else 0

                if existing_count > 0:
                    logger.info(f"✅ Skipping re-index - collection already has {existing_count:,} indicators")
                    self.stats["total_indicators"] = existing_count
                else:
                    logger.info(f"📊 Indexing {len(all_indicators)} total indicators...")
                    self.vector_search.index_indicators(
                        indicators=all_indicators,
                        batch_size=100,
                        clear_existing=True  # Let index_indicators handle clearing
                    )
                    logger.info(f"✅ Successfully indexed {len(all_indicators)} indicators")
            except Exception as e:
                logger.error(f"❌ Error indexing indicators: {e}")
                return {
                    "success": False,
                    "error": f"Failed to index indicators: {str(e)}"
                }

        elapsed = (datetime.now() - start_time).total_seconds()
        self.stats["load_time"] = elapsed

        logger.info(
            f"✅ Loaded {self.stats['total_indicators']} indicators "
            f"from {self.stats['providers_loaded']} providers in {elapsed:.2f}s"
        )

        return {
            "success": True,
            **self.stats
        }

    async def load_provider(self, json_file: Path, index_immediately: bool = True) -> Dict[str, Any]:
        """
        Load indicators from a single provider JSON file.

        Args:
            json_file: Path to provider JSON file
            index_immediately: If True, index immediately; if False, return indicators for batch indexing

        Returns:
            Dict with loading results (includes "indicators" key if index_immediately=False)
        """
        logger.info(f"   Loading {json_file.name}...")

        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"   ❌ Invalid JSON in {json_file.name}: {e}")
            return {"success": False, "error": str(e)}

        provider = data.get("provider", json_file.stem.upper())

        # Handle both "indicators" and "series" keys (FRED uses "series")
        indicators = data.get("indicators") or data.get("series", [])

        if not indicators:
            logger.warning(f"   ⚠️ No indicators found in {json_file.name}")
            return {"success": False, "error": "No indicators"}

        # Prepare indicators for indexing
        indicators_to_index = []
        seen_codes = {}  # Track code counts for deduplication

        for indicator in indicators:
            try:
                # Get code (required)
                code = indicator.get("code") or indicator.get("id", "")
                if not code:
                    continue

                # Get name (required for indexing)
                name = indicator.get("name", "")
                if not name:
                    continue

                # Make code unique across providers and handle within-provider duplicates
                # Format: provider:code (e.g., "WorldBank:GDP", "FRED:GDP")
                base_unique_code = f"{provider}:{code}"

                # Handle duplicates within same provider by adding suffix
                if base_unique_code in seen_codes:
                    seen_codes[base_unique_code] += 1
                    unique_code = f"{base_unique_code}:{seen_codes[base_unique_code]}"
                else:
                    seen_codes[base_unique_code] = 0
                    unique_code = base_unique_code

                # Prepare indicator dict for VectorSearchService
                ind_dict = {
                    "code": unique_code,  # Use unique code to avoid duplicates
                    "name": name,
                    "provider": provider,
                    "original_code": code  # Store original code for reference
                }

                # Add optional description for richer embeddings
                description = indicator.get("description", "")
                if description:
                    # Combine name and description for better embeddings
                    ind_dict["name"] = f"{name}. {description[:200]}"

                indicators_to_index.append(ind_dict)

            except Exception as e:
                # Use a safe fallback for code in error message
                safe_code = indicator.get("code") or indicator.get("id", "unknown")
                logger.warning(f"   ⚠️ Error processing indicator {safe_code}: {e}")
                continue

        if not indicators_to_index:
            logger.warning(f"   ⚠️ No valid indicators processed from {json_file.name}")
            return {"success": False, "error": "No valid indicators"}

        # Either index immediately or return for batch indexing
        if index_immediately:
            # Index in vector store using existing API
            try:
                self.vector_search.index_indicators(
                    indicators=indicators_to_index,
                    batch_size=100
                )

                logger.info(f"   ✅ Loaded {len(indicators_to_index)} indicators from {provider}")
                return {
                    "success": True,
                    "provider": provider,
                    "indicators_loaded": len(indicators_to_index)
                }

            except Exception as e:
                logger.error(f"   ❌ Error adding to vector store: {e}")
                return {"success": False, "error": str(e)}
        else:
            # Return indicators for batch indexing later
            logger.info(f"   ✅ Prepared {len(indicators_to_index)} indicators from {provider}")
            return {
                "success": True,
                "provider": provider,
                "indicators_loaded": len(indicators_to_index),
                "indicators": indicators_to_index
            }

    async def search(self, query: str, top_k: int = 10, provider: Optional[str] = None):
        """
        Search indicators by natural language query.

        Args:
            query: Search query
            top_k: Number of results to return
            provider: Optional provider filter

        Returns:
            List of search results
        """
        if not hasattr(self.vector_search, 'collection') or self.vector_search.collection is None:
            logger.warning("⚠️ Vector search not initialized")
            return []

        results = self.vector_search.search(
            query=query,
            limit=top_k
        )

        # Filter by provider if specified
        if provider:
            results = [r for r in results if r.provider == provider]

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get loading statistics."""
        return self.stats

    async def reload(self):
        """Reload all metadata (useful for updates)."""
        logger.info("🔄 Reloading metadata...")

        # Reset stats
        self.stats = {
            "providers_loaded": 0,
            "total_indicators": 0,
            "load_time": 0,
        }

        # Load all (index_indicators() already clears collection)
        return await self.load_all()
