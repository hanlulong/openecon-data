"""Session storage for Pro Mode - persist data across executions within a conversation"""
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional

from backend.config import get_settings
from backend.utils import NumpyPandasEncoder, sanitize_identifier

logger = logging.getLogger(__name__)


def get_session_storage_dir() -> Path:
    """Get session storage directory with cross-platform default"""
    settings = get_settings()
    if settings.promode_session_dir:
        return Path(settings.promode_session_dir)

    # Default to system temp directory for cross-platform compatibility
    return Path(tempfile.gettempdir()) / "promode_sessions"


class SessionStorage:
    """Manage persistent storage for Pro Mode sessions"""

    def __init__(self):
        self.base_dir = get_session_storage_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _validate_session_id(self, session_id: str) -> str:
        """Validate and sanitize session ID to prevent path traversal"""
        return sanitize_identifier(session_id, "session ID")

    def _validate_key(self, key: str) -> str:
        """Validate and sanitize key to prevent path traversal"""
        return sanitize_identifier(key, "key")

    def _get_session_dir(self, session_id: str) -> Path:
        """Get directory for a specific session with path traversal protection"""
        sanitized_id = self._validate_session_id(session_id)
        session_dir = self.base_dir / sanitized_id

        # Verify the resolved path is still within base_dir (defense in depth)
        try:
            resolved = session_dir.resolve()
            base_resolved = self.base_dir.resolve()
            if not str(resolved).startswith(str(base_resolved)):
                raise ValueError(f"Path traversal attempt detected: {session_id}")
        except Exception as e:
            logger.error(f"Path validation failed for session {session_id}: {e}")
            raise ValueError("Invalid session path")

        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def save(self, session_id: str, key: str, value: Any) -> bool:
        """
        Save data to session storage using JSON

        Args:
            session_id: Unique session identifier
            key: Data key/name
            value: Python object to store (must be JSON-serializable)

        Returns:
            True if save was successful, False otherwise
        """
        try:
            session_dir = self._get_session_dir(session_id)
            sanitized_key = self._validate_key(key)
            file_path = session_dir / f"{sanitized_key}.json"

            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(value, f, cls=NumpyPandasEncoder, indent=2)

            logger.info(f"Saved session data: {session_id}/{sanitized_key}")
            return True
        except ValueError as e:
            logger.error(f"Invalid key for session data {session_id}/{key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to save session data {session_id}/{key}: {e}")
            return False

    def load(self, session_id: str, key: str) -> Optional[Any]:
        """
        Load data from session storage

        Args:
            session_id: Unique session identifier
            key: Data key/name

        Returns:
            Stored object or None if not found
        """
        try:
            session_dir = self._get_session_dir(session_id)
            sanitized_key = self._validate_key(key)

            # Load JSON only (pickle is not supported for security reasons)
            json_path = session_dir / f"{sanitized_key}.json"
            if json_path.exists():
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                logger.info(f"Loaded session data: {session_id}/{sanitized_key}")
                return data

            # Security: Refuse to load pickle files (arbitrary code execution risk)
            pkl_path = session_dir / f"{sanitized_key}.pkl"
            if pkl_path.exists():
                logger.error(f"Refusing to load legacy pickle file: {session_id}/{sanitized_key} - security risk. Please recreate the session data.")
                # Delete the dangerous pickle file
                try:
                    pkl_path.unlink()
                    logger.info(f"Deleted legacy pickle file: {session_id}/{sanitized_key}")
                except Exception:
                    pass
                return None

            return None
        except ValueError as e:
            logger.error(f"Invalid key for session data {session_id}/{key}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to load session data {session_id}/{key}: {e}")
            return None

    def list_keys(self, session_id: str) -> List[str]:
        """
        List all stored keys for a session

        Args:
            session_id: Unique session identifier

        Returns:
            List of stored data keys
        """
        try:
            session_dir = self._get_session_dir(session_id)
            if not session_dir.exists():
                return []

            # Only list JSON files (pickle is not supported for security reasons)
            json_keys = {f.stem for f in session_dir.glob("*.json")}
            return list(json_keys)
        except Exception as e:
            logger.error(f"Failed to list session keys {session_id}: {e}")
            return []

    def clear_session(self, session_id: str) -> None:
        """
        Clear all data for a session

        Args:
            session_id: Unique session identifier
        """
        try:
            session_dir = self._get_session_dir(session_id)
            if session_dir.exists():
                shutil.rmtree(session_dir)
                logger.info(f"Cleared session: {session_id}")
        except Exception as e:
            logger.error(f"Failed to clear session {session_id}: {e}")

    def cleanup_old_sessions(self, max_age_hours: int = 24) -> int:
        """
        Clean up old session directories

        Args:
            max_age_hours: Maximum age in hours

        Returns:
            Number of sessions cleaned up
        """
        cleaned = 0
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600

        try:
            if not self.base_dir.exists():
                return 0

            for session_dir in self.base_dir.iterdir():
                if session_dir.is_dir():
                    age = current_time - session_dir.stat().st_mtime
                    if age > max_age_seconds:
                        shutil.rmtree(session_dir)
                        cleaned += 1
                        logger.info(f"Cleaned up old session: {session_dir.name}")

            return cleaned
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")
            return cleaned


# Singleton instance
_session_storage: Optional[SessionStorage] = None


def get_session_storage() -> SessionStorage:
    """Get or create SessionStorage singleton"""
    global _session_storage
    if _session_storage is None:
        _session_storage = SessionStorage()
    return _session_storage
