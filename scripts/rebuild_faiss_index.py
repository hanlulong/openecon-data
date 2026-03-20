#!/usr/bin/env python3
"""
Rebuild the FAISS vector index from metadata JSON files.

This mirrors the application's metadata-loading path so the rebuilt index uses:
- every metadata JSON file under ``backend/data/metadata``
- the same provider-prefixed deduplicated codes
- the same ``name + description[:200]`` text construction used for embeddings
- the embedding backend configured via environment variables

Usage:
    python3 scripts/rebuild_faiss_index.py
"""

import sys
import json
import logging
from pathlib import Path
from collections import defaultdict

# Add backend to Python path
backend_dir = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_dir.parent))

from backend.config import get_settings
from backend.services.faiss_vector_search import FAISSVectorSearch

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_indicators_from_json() -> tuple[list[dict], dict[str, int]]:
    """Load all indicators from metadata JSON files using application semantics."""
    metadata_dir = backend_dir / "data" / "metadata"
    all_indicators = []
    provider_counts: dict[str, int] = defaultdict(int)
    seen_codes: dict[str, int] = {}

    for metadata_file in sorted(metadata_dir.glob("*.json")):
        provider_fallback = metadata_file.stem.upper()

        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            provider = str(data.get("provider") or provider_fallback).strip().upper()
            indicators_list = data.get("indicators") or data.get("series") or []
            logger.info(f"📦 Loaded {len(indicators_list)} indicators from {provider}")

            for indicator in indicators_list:
                code = indicator.get("code") or indicator.get("id", "")
                name = indicator.get("name", "")
                if not code or not name:
                    continue

                base_unique_code = f"{provider}:{code}"
                if base_unique_code in seen_codes:
                    seen_codes[base_unique_code] += 1
                    unique_code = f"{base_unique_code}:{seen_codes[base_unique_code]}"
                else:
                    seen_codes[base_unique_code] = 0
                    unique_code = base_unique_code

                description = indicator.get("description", "")
                text = f"{name}. {description[:200]}" if description else name

                all_indicators.append({
                    "code": unique_code,
                    "original_code": code,
                    "name": text,
                    "provider": provider,
                })
                provider_counts[provider] += 1

        except Exception as e:
            logger.error(f"❌ Error loading {metadata_file.name}: {e}")
            continue

    return all_indicators, dict(sorted(provider_counts.items()))


def main():
    """Main rebuild workflow."""
    logger.info("=" * 70)
    logger.info("🔄 REBUILDING FAISS INDEX FROM JSON METADATA")
    logger.info("=" * 70)
    logger.info("")

    try:
        settings = get_settings()

        # Step 1: Load indicators from JSON metadata
        logger.info("📥 Loading indicators from JSON metadata files...")
        indicators, provider_counts = load_indicators_from_json()

        if not indicators:
            logger.error("❌ No indicators loaded. Exiting.")
            return 1

        logger.info(f"✅ Loaded {len(indicators)} indicators total")
        for provider, count in provider_counts.items():
            logger.info(f"   - {provider}: {count}")
        logger.info("")

        # Step 2: Initialize FAISS vector search
        logger.info("🔧 Initializing FAISSVectorSearch...")
        vector_search = FAISSVectorSearch(
            model_name=settings.embedding_model,
            index_dir=settings.vector_search_cache_dir,
            embedding_dimensions=settings.embedding_dimensions,
        )
        logger.info(f"   - Embedding model: {settings.embedding_model}")
        if settings.embedding_dimensions is not None:
            logger.info(f"   - Embedding dimensions override: {settings.embedding_dimensions}")
        logger.info(f"   - Index directory: {settings.vector_search_cache_dir}")
        logger.info("")

        # Step 3: Rebuild index
        logger.info("🔨 Rebuilding FAISS index...")
        logger.info(f"   - Total indicators: {len(indicators)}")
        logger.info("   - Batch size: 128 (for embeddings)")
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
