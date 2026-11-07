"""Session storage for Pro Mode - persist data across executions within a conversation"""
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional

from backend.config import get_settings
from backend.services import untrusted_fs
from backend.utils import NumpyPandasEncoder, sanitize_identifier

logger = logging.getLogger(__name__)

# Largest session JSON the backend will read back. The jail's RLIMIT_FSIZE
# (100 MiB) bounds what generated code can write there; match it so nothing a
# real run saved is refused.
MAX_SESSION_FILE_BYTES = 100 * 1024 * 1024
# Mode of session files the backend writes. The jail (promode uid under
# Layer 2) must be able to read them back via load_session, including files
# saved before the session dir was group-shared, so keep the 0644 that the
# backend's 0022 umask produced before writes became link-safe.
SESSION_FILE_MODE = 0o644


def get_session_storage_dir() -> Path:
    """Get session storage directory with cross-platform default"""
    settings = get_settings()
    if settings.promode_session_dir:
        return Path(settings.promode_session_dir)

    # Default to system temp directory for cross-platform compatibility
    return Path(tempfile.gettempdir()) / "promode_sessions"


def force_rmtree(path: Path) -> bool:
    """Remove a session/work tree even when it contains dirs the backend
    user cannot READ but may still unlink via the parent.

    The Pro Mode jail leaves its detached tmpfs mountpoint behind: a
    mode-700 dir owned by the sandbox uid (``.jailroot.*``). Linux's
    fd-based ``shutil.rmtree`` fails to open it and gives up before ever
    attempting ``rmdir`` — even though the dir is empty and the parent's
    owner is allowed to remove it. Every such work dir then leaks and
    re-warns on every cleanup pass.

    Strategy: normal rmtree first, then a bottom-up sweep that unlinks
    what it can and ``rmdir``s empty-but-unreadable dirs via the parent.

    Never follows links: if ``path`` itself is a link only the link is
    removed (rmtree refuses links, and the fallback os.walk would otherwise
    descend into the link's TARGET and unlink files outside the tree); links
    found inside the tree are unlinked, never traversed.

    Returns True when ``path`` is gone afterwards.
    """
    target = Path(path)
    if os.path.islink(target):
        try:
            os.unlink(target)
        except OSError:
            pass
        return not os.path.lexists(target)
    shutil.rmtree(target, ignore_errors=True)
    if not os.path.lexists(target):
        return True
    for dirpath, dirnames, filenames in os.walk(target, topdown=False):
        for name in filenames:
            try:
                os.unlink(os.path.join(dirpath, name))
            except OSError:
                pass
        for name in dirnames:
            child = os.path.join(dirpath, name)
            try:
                if os.path.islink(child):
                    os.unlink(child)
                else:
                    os.rmdir(child)
            except OSError:
                pass
    try:
        os.rmdir(target)
    except OSError:
        pass
    return not os.path.lexists(target)


class SessionStorage:
    """Manage persistent storage for Pro Mode sessions.

    SECURITY INVARIANT: each session dir is bound read-write into the Pro Mode
    jail as /sess, so every ``<key>.json`` name in it may be a link (or FIFO,
    hardlink, ...) planted by generated code in an earlier run of the same
    conversation. Reads and writes therefore go through
    ``backend.services.untrusted_fs``: reads never follow a link, and writes
    replace the name with a new file instead of writing through it.
    """

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

            payload = json.dumps(value, cls=NumpyPandasEncoder, indent=2).encode("utf-8")
            untrusted_fs.replace_file_nofollow(file_path, payload, SESSION_FILE_MODE)

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
            if os.path.lexists(json_path):
                raw = untrusted_fs.read_untrusted_bytes(
                    json_path, MAX_SESSION_FILE_BYTES,
                    event=untrusted_fs.SESSION_REJECTED_EVENT,
                )
                if raw is None:
                    return None
                data = json.loads(raw.decode("utf-8"))
                logger.info(f"Loaded session data: {session_id}/{sanitized_key}")
                return data

            # Security: Refuse to load pickle files (arbitrary code execution risk)
            pkl_path = session_dir / f"{sanitized_key}.pkl"
            if os.path.lexists(pkl_path):
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

            # Only list JSON files (pickle is not supported for security reasons),
            # and only real regular files: a link/FIFO/dir named *.json is not
            # a key the backend will load.
            json_keys = {
                f.stem for f in session_dir.glob("*.json")
                if untrusted_fs.is_regular_file_nofollow(f)
            }
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
                if force_rmtree(session_dir):
                    logger.info(f"Cleared session: {session_id}")
                else:
                    logger.warning(f"Could not fully clear session dir: {session_id}")
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
                # "work" holds per-execution scratch dirs owned by the code
                # executor, which prunes them on its own (1h) schedule.
                if not session_dir.is_dir() or session_dir.name == "work":
                    continue
                try:
                    age = current_time - session_dir.stat().st_mtime
                    if age > max_age_seconds:
                        if force_rmtree(session_dir):
                            cleaned += 1
                            logger.info(f"Cleaned up old session: {session_dir.name}")
                        else:
                            logger.warning(
                                f"Could not fully remove old session: {session_dir.name}"
                            )
                except OSError as e:
                    # One undeletable dir must not abort cleanup of the rest.
                    logger.warning(f"Failed to clean up {session_dir}: {e}")

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
