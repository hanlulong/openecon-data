#!/usr/bin/env python3
"""
Update Metadata Script

This script fetches the latest metadata from all data provider APIs and updates
the local JSON files. It can update all providers or specific ones.

Usage:
    python scripts/update_metadata.py [--providers FRED,WorldBank,IMF] [--verify]
"""

import sys
import asyncio
import argparse
import logging
from pathlib import Path
from typing import List, Optional

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import all metadata extractors
EXTRACTORS = {}

try:
    from extract_bis_metadata import extract_bis_metadata
    EXTRACTORS['BIS'] = extract_bis_metadata
except ImportError:
    logger.warning("BIS extractor not available")

try:
    from extract_eurostat_metadata import extract_eurostat_metadata
    EXTRACTORS['Eurostat'] = extract_eurostat_metadata
except ImportError:
    logger.warning("Eurostat extractor not available")

try:
    from extract_imf_metadata import extract_imf_metadata
    EXTRACTORS['IMF'] = extract_imf_metadata
except ImportError:
    logger.warning("IMF extractor not available")

try:
    from extract_oecd_metadata import extract_oecd_metadata
    EXTRACTORS['OECD'] = extract_oecd_metadata
except ImportError:
    logger.warning("OECD extractor not available")


async def update_provider_metadata(provider: str) -> bool:
    """
    Update metadata for a specific provider.

    Args:
        provider: Provider name (e.g., "WorldBank", "IMF")

    Returns:
        True if successful, False otherwise
    """
    if provider not in EXTRACTORS:
        logger.warning(f"No extractor available for {provider}")
        return False

    logger.info(f"📥 Updating metadata for {provider}...")

    try:
        await EXTRACTORS[provider]()
        logger.info(f"✅ Successfully updated {provider} metadata")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to update {provider} metadata: {e}", exc_info=True)
        return False


async def update_all_providers(providers: Optional[List[str]] = None) -> dict:
    """
    Update metadata for all providers or a subset.

    Args:
        providers: Optional list of specific providers to update

    Returns:
        Dict with update results
    """
    results = {
        "total": 0,
        "succeeded": 0,
        "failed": 0,
        "providers": {}
    }

    # Determine which providers to update
    to_update = providers if providers else list(EXTRACTORS.keys())

    for provider in to_update:
        results["total"] += 1
        success = await update_provider_metadata(provider)

        results["providers"][provider] = "success" if success else "failed"
        if success:
            results["succeeded"] += 1
        else:
            results["failed"] += 1

    return results


def verify_metadata_files():
    """Verify that metadata files exist and are valid."""
    import json

    metadata_dir = Path("backend/data/metadata")
    results = {
        "total": 0,
        "valid": 0,
        "invalid": []
    }

    logger.info("🔍 Verifying metadata files...")

    for json_file in sorted(metadata_dir.glob("*.json")):
        results["total"] += 1

        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            provider = data.get("provider", json_file.stem.upper())
            indicators = data.get("indicators") or data.get("series", [])

            results["valid"] += 1
            logger.info(f"✅ {json_file.name:20s} - {provider:15s} - {len(indicators):,} indicators")

        except Exception as e:
            results["invalid"].append({
                "file": json_file.name,
                "error": str(e)
            })
            logger.error(f"❌ {json_file.name}: {e}")

    logger.info(f"\n📊 Verification Results:")
    logger.info(f"   - Total files: {results['total']}")
    logger.info(f"   - Valid: {results['valid']}")
    logger.info(f"   - Invalid: {len(results['invalid'])}")

    return results


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Update provider metadata")
    parser.add_argument(
        "--providers",
        type=str,
        help="Comma-separated list of providers to update (e.g., 'BIS,IMF,OECD')"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify metadata files after update"
    )

    args = parser.parse_args()

    # Parse provider list if specified
    providers_to_update = None
    if args.providers:
        providers_to_update = [p.strip() for p in args.providers.split(',')]
        logger.info(f"Updating specific providers: {', '.join(providers_to_update)}")

    # Update metadata
    results = await update_all_providers(providers_to_update)

    # Print results
    logger.info("\n" + "="*80)
    logger.info("UPDATE SUMMARY")
    logger.info("="*80)
    logger.info(f"Total providers: {results['total']}")
    logger.info(f"Succeeded: {results['succeeded']}")
    logger.info(f"Failed: {results['failed']}")
    logger.info("\nDetails:")
    for provider, status in results['providers'].items():
        status_icon = "✅" if status == "success" else "❌"
        logger.info(f"  {status_icon} {provider}: {status}")
    logger.info("="*80)

    # Verify if requested
    if args.verify:
        verify_metadata_files()

    # Return error code if any failed
    if results['failed'] > 0:
        sys.exit(1)


if __name__ == "__main__":
    # Change to metadata_extractors directory for imports
    metadata_extractors_dir = Path(__file__).parent / "metadata_extractors"
    sys.path.insert(0, str(metadata_extractors_dir))

    asyncio.run(main())
