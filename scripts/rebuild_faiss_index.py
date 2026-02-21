#!/usr/bin/env python3
"""
Rebuild FAISS vector index from JSON metadata files.

This script loads all indicators from the metadata JSON files
and rebuilds the FAISS index for semantic search.

Usage:
    python3 scripts/rebuild_faiss_index.py
"""

import sys
import json
import logging
from pathlib import Path

# Add backend to Python path
backend_dir = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_dir.parent))

from backend.services.faiss_vector_search import FAISSVectorSearch

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_indicators_from_json() -> list[dict]:
    """Load all indicators from JSON metadata files."""
    metadata_dir = backend_dir / "data" / "metadata"

    providers = [
        "worldbank",
        "statscan",
        "imf",
        "bis",
        "eurostat",
        "oecd",
    ]

    all_indicators = []

    for provider in providers:
        metadata_file = metadata_dir / f"{provider}.json"
        if not metadata_file.exists():
            logger.warning(f"⚠️  Metadata file not found: {metadata_file}")
            continue

        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            indicators_list = data.get("indicators", [])
            logger.info(f"📦 Loaded {len(indicators_list)} indicators from {provider}")

            # Convert to the format expected by FAISSVectorSearch
            for indicator in indicators_list:
                all_indicators.append({
                    "code": indicator.get("code", ""),
                    "name": indicator.get("name", ""),
                    "provider": provider.upper(),
                })

        except Exception as e:
            logger.error(f"❌ Error loading {provider} metadata: {e}")
            continue

    return all_indicators


def main():
    """Main rebuild workflow."""
    logger.info("=" * 70)
    logger.info("🔄 REBUILDING FAISS INDEX FROM JSON METADATA")
    logger.info("=" * 70)
    logger.info("")

    try:
        # Step 1: Load indicators from JSON metadata
        logger.info("📥 Loading indicators from JSON metadata files...")
        indicators = load_indicators_from_json()

        if not indicators:
            logger.error("❌ No indicators loaded. Exiting.")
            return 1

        logger.info(f"✅ Loaded {len(indicators)} indicators total")
        logger.info("")

        # Step 2: Initialize FAISS vector search
        logger.info("🔧 Initializing FAISSVectorSearch...")
        vector_search = FAISSVectorSearch()
        logger.info("")

        # Step 3: Rebuild index
        logger.info("🔨 Rebuilding FAISS index...")
        logger.info(f"   - Total indicators: {len(indicators)}")
        logger.info(f"   - Batch size: 128 (for embeddings)")
        logger.info(f"   - Estimated time: 3-5 minutes")
        logger.info("")

        vector_search.index_indicators(
            indicators,
            batch_size=100,
            clear_existing=True
        )

        # Step 4: Verify index
        index_size = vector_search.get_index_size()
        logger.info("")
        logger.info("=" * 70)
        logger.info("✅ FAISS INDEX REBUILD COMPLETE")
        logger.info("=" * 70)
        logger.info(f"   - Indexed indicators: {index_size}")
        logger.info(f"   - Index directory: {vector_search.index_dir}")
        logger.info("")

        # Step 5: Test search
        logger.info("🔍 Testing vector search...")
        test_queries = [
            "GDP current US dollars",
            "unemployment rate",
            "fiscal balance",
            "HICP inflation",
            "real effective exchange rate",
        ]

        for query in test_queries:
            results = vector_search.search(query, limit=3)
            logger.info(f"\n  Query: '{query}'")
            for i, result in enumerate(results, 1):
                logger.info(
                    f"    {i}. [{result.provider:10}] {result.code:30} - {result.name[:60]:60} "
                    f"(sim: {result.similarity:.3f})"
                )

        logger.info("")
        logger.info("=" * 70)
        logger.info("🎉 ALL DONE - FAISS index ready for use!")
        logger.info("=" * 70)

        return 0

    except Exception as e:
        logger.error(f"\n❌ Error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
