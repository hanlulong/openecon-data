"""cleanup_old_sessions must prune published Pro Mode media by age.

Every Pro Mode chart/CSV/JSON was written to public_dir permanently and never
cleaned — the one true unbounded disk leak. cleanup now removes public_dir
files older than max_age_hours while leaving fresh ones.
"""

import os
import time

from backend.services.secure_code_executor import SecureCodeExecutor


def test_old_published_media_pruned_fresh_kept(tmp_path):
    session_dir = tmp_path / "sessions"
    public_dir = tmp_path / "public"
    ex = SecureCodeExecutor(session_dir=session_dir, public_dir=public_dir)

    old_file = public_dir / "old_chart.png"
    new_file = public_dir / "new_chart.png"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")

    # Age the old file to 48h; leave the new one current.
    old_ts = time.time() - 48 * 3600
    os.utime(old_file, (old_ts, old_ts))

    deleted = ex.cleanup_old_sessions(max_age_hours=24)

    assert not old_file.exists(), "stale published media should be pruned"
    assert new_file.exists(), "fresh published media must be kept"
    assert deleted >= 1
