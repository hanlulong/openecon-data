#!/usr/bin/env python3
"""
Daily catalog update script

This script updates the catalog database by re-indexing all providers.
It's designed to be run as a cron job.

Usage:
    python backend/data/update_catalog.py

Cron setup (run daily at 2 AM):
    0 2 * * * cd /home/hanlulong/OpenEcon && source backend/.venv/bin/activate && python backend/data/update_catalog.py >> /var/log/openecon/catalog_update.log 2>&1
"""

import asyncio
import os
import sys
import logging
from datetime import datetime

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.catalog_indexer import index_all_providers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    """Main update routine"""
    start_time = datetime.now()

    print(f"\n{'=' * 70}")
    print(f"OPENECON CATALOG UPDATE - {start_time.isoformat()}")
    print(f"{'=' * 70}\n")

    # Get database path
    db_path = os.path.join(os.path.dirname(__file__), "catalog.db")

    if not os.path.exists(db_path):
        logger.error(f"Database not found at {db_path}. Run init_database.py first.")
        sys.exit(1)

    # Run indexing
    logger.info("Starting catalog update...")

    try:
        results = await index_all_providers(db_path)

        # Print results
        print(f"\n{'=' * 70}")
        print("UPDATE RESULTS")
        print(f"{'=' * 70}\n")

        successful = 0
        failed = 0
        total_indicators = 0

        for provider, result in results.items():
            if result["success"]:
                successful += 1
                total_indicators += result["indicators_indexed"]
                print(
                    f"✅ {provider}: Indexed {result['indicators_indexed']:,} indicators"
                )
            else:
                failed += 1
                print(f"❌ {provider}: Failed - {result.get('error', 'Unknown error')}")

        # Summary
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print(f"\n{'=' * 70}")
        print("SUMMARY")
        print(f"{'=' * 70}\n")
        print(f"Start time: {start_time.isoformat()}")
        print(f"End time: {end_time.isoformat()}")
        print(f"Duration: {duration:.2f} seconds")
        print(f"Successful providers: {successful}")
        print(f"Failed providers: {failed}")
        print(f"Total indicators: {total_indicators:,}")

        if failed > 0:
            logger.warning(f"{failed} provider(s) failed to update")
            sys.exit(1)
        else:
            logger.info("Catalog update completed successfully")
            sys.exit(0)

    except Exception as e:
        logger.error(f"Fatal error during catalog update: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
