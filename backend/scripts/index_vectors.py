#!/usr/bin/env python3
"""
Script to index all economic indicators in the Chroma vector database.

This script:
1. Loads all indicators from the SQLite catalog database
2. Generates embeddings using sentence-transformers
3. Indexes them in Chroma for semantic search

Usage:
    python backend/scripts/index_vectors.py
"""

import sys
import os
import logging
import sqlite3
from pathlib import Path

# Add backend to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir.parent))

from backend.services.vector_search import VectorSearchService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_indicators_from_catalog(catalog_path: str):
    """
    Load all indicators from the SQLite catalog database.

    Args:
        catalog_path: Path to catalog.db

    Returns:
        List of indicator dicts with keys: code, name, provider
    """
    logger.info(f"📂 Loading indicators from: {catalog_path}")

    if not os.path.exists(catalog_path):
        raise FileNotFoundError(f"Catalog database not found: {catalog_path}")

    conn = sqlite3.connect(catalog_path)
    cursor = conn.cursor()

    # Load all indicators
    cursor.execute("""
        SELECT code, name, provider
        FROM indicators
        ORDER BY provider, code
    """)

    indicators = []
    for row in cursor.fetchall():
        indicators.append({
            "code": row[0],
            "name": row[1],
            "provider": row[2]
        })

    conn.close()

    logger.info(f"✅ Loaded {len(indicators)} indicators")
    return indicators


def main():
    """Main indexing function."""
    logger.info("="*70)
    logger.info("🚀 STARTING VECTOR INDEXING")
    logger.info("="*70)

    # Paths
    catalog_path = os.path.join(backend_dir, "data", "catalog.db")

    # Step 1: Load indicators from catalog
    indicators = load_indicators_from_catalog(catalog_path)

    if not indicators:
        logger.error("❌ No indicators found in catalog database")
        return 1

    # Step 2: Initialize vector search service
    logger.info("🔧 Initializing VectorSearchService...")
    vector_service = VectorSearchService()

    # Step 3: Index indicators
    logger.info("📊 Starting indexing process...")
    logger.info(f"   Total indicators: {len(indicators)}")
    logger.info(f"   Batch size: 100")
    logger.info(f"   Estimated time: ~3-5 minutes")
    logger.info("")

    try:
        vector_service.index_indicators(indicators, batch_size=100)
    except Exception as e:
        logger.error(f"❌ Indexing failed: {e}", exc_info=True)
        return 1

    # Step 4: Verify indexing
    index_size = vector_service.get_index_size()
    logger.info("")
    logger.info("="*70)
    logger.info("✅ INDEXING COMPLETE")
    logger.info("="*70)
    logger.info(f"   Indexed: {index_size} indicators")
    logger.info(f"   Database: {vector_service.persist_directory}")
    logger.info("")

    # Step 5: Test search
    logger.info("🔍 Testing vector search...")
    test_queries = [
        "GDP current US dollars",
        "unemployment rate",
        "population total",
        "inflation CPI"
    ]

    for query in test_queries:
        results = vector_service.search(query, limit=3)
        logger.info(f"\n  Query: '{query}'")
        for i, result in enumerate(results, 1):
            logger.info(f"    {i}. {result.code:30} - {result.name[:50]:50} (similarity: {result.similarity:.3f})")

    logger.info("")
    logger.info("="*70)
    logger.info("🎉 ALL DONE - Vector database is ready for use!")
    logger.info("="*70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
