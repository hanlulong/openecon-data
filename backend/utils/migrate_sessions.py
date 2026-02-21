"""Migration utility to convert pickle session files to JSON format."""
import json
import logging
import pickle
from pathlib import Path

from backend.config import get_settings
from backend.services.session_storage import NumpyPandasEncoder, get_session_storage_dir

logger = logging.getLogger(__name__)


def migrate_pickle_to_json() -> dict:
    """
    Migrate existing pickle session files to JSON format.

    Returns:
        dict with migration statistics: {
            'total_files': int,
            'migrated': int,
            'failed': int,
            'skipped': int
        }
    """
    stats = {
        'total_files': 0,
        'migrated': 0,
        'failed': 0,
        'skipped': 0
    }

    try:
        session_dir = get_session_storage_dir()

        if not session_dir.exists():
            logger.info("No session directory found, skipping migration")
            return stats

        # Find all pickle files recursively
        pkl_files = list(session_dir.rglob("*.pkl"))
        stats['total_files'] = len(pkl_files)

        if stats['total_files'] == 0:
            logger.info("No pickle files found to migrate")
            return stats

        logger.info(f"Found {stats['total_files']} pickle files to migrate")

        for pkl_file in pkl_files:
            try:
                # Check if JSON version already exists
                json_file = pkl_file.with_suffix('.json')
                if json_file.exists():
                    logger.debug(f"Skipping {pkl_file.name} - JSON version already exists")
                    stats['skipped'] += 1
                    continue

                # Load pickle data
                with open(pkl_file, 'rb') as f:
                    data = pickle.load(f)

                # Save as JSON
                with open(json_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, cls=NumpyPandasEncoder, indent=2)

                # Delete pickle file after successful migration
                pkl_file.unlink()

                stats['migrated'] += 1
                logger.info(f"✅ Migrated: {pkl_file.relative_to(session_dir)}")

            except Exception as e:
                stats['failed'] += 1
                logger.error(f"❌ Failed to migrate {pkl_file}: {e}")

        # Log summary
        logger.info(f"Migration complete: {stats['migrated']} migrated, {stats['failed']} failed, {stats['skipped']} skipped")

        return stats

    except Exception as e:
        logger.error(f"Error during session migration: {e}")
        return stats


if __name__ == "__main__":
    # Allow running as a standalone script
    logging.basicConfig(level=logging.INFO)
    result = migrate_pickle_to_json()
    print(f"\n=== Migration Results ===")
    print(f"Total pickle files: {result['total_files']}")
    print(f"Successfully migrated: {result['migrated']}")
    print(f"Failed: {result['failed']}")
    print(f"Skipped (already migrated): {result['skipped']}")
