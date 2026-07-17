"""Work-dir cleanup must survive the jail's unreadable tmpfs mountpoint.

The Pro Mode jail leaves behind ``.jailroot.*``: an EMPTY mode-700 dir owned
by the sandbox uid. Linux's fd-based shutil.rmtree cannot open it and gives
up before attempting rmdir, so every work dir leaked forever and re-warned on
each cleanup pass. force_rmtree falls back to a bottom-up sweep that rmdir's
empty-but-unreadable dirs via the parent (which the backend user owns).

The fixture reproduces the shape with chmod 000: unreadable even to its
owner, but removable via the parent — exactly the production failure mode.
"""

import os
import shutil
import time

import pytest

from backend.services.secure_code_executor import SecureCodeExecutor
from backend.services.session_storage import force_rmtree


@pytest.fixture
def workdir_with_jailroot(tmp_path):
    work = tmp_path / "work" / "abc123"
    work.mkdir(parents=True)
    (work / "script.py").write_text("print('x')")
    jailroot = work / ".jailroot.XYZ123"
    jailroot.mkdir()
    os.chmod(jailroot, 0o000)
    yield tmp_path, work, jailroot
    # Restore perms so pytest's tmp_path teardown never trips on leftovers.
    if jailroot.exists():
        os.chmod(jailroot, 0o700)


def test_plain_rmtree_leaks_the_workdir(workdir_with_jailroot):
    """Guard the premise: if plain rmtree ever handles this, the helper
    can be retired."""
    _tmp, work, _jail = workdir_with_jailroot
    shutil.rmtree(work, ignore_errors=True)
    assert work.exists(), "premise changed: plain rmtree now handles it"


def test_force_rmtree_removes_unreadable_empty_mountpoint(workdir_with_jailroot):
    _tmp, work, _jail = workdir_with_jailroot
    assert force_rmtree(work) is True
    assert not work.exists()


def test_force_rmtree_reports_truly_undeletable_content(workdir_with_jailroot):
    """A NON-empty unreadable dir genuinely cannot be removed without
    privileges — force_rmtree must say so instead of lying."""
    _tmp, work, jail = workdir_with_jailroot
    os.chmod(jail, 0o700)
    (jail / "stuck.txt").write_text("x")
    os.chmod(jail, 0o000)
    assert force_rmtree(work) is False
    assert work.exists()


def test_cleanup_old_sessions_prunes_jailroot_workdirs(workdir_with_jailroot):
    tmp_path, work, _jail = workdir_with_jailroot
    ex = SecureCodeExecutor(session_dir=tmp_path, public_dir=tmp_path / "public")

    old_ts = time.time() - 3 * 3600
    os.utime(work, (old_ts, old_ts))

    deleted = ex.cleanup_old_sessions(max_age_hours=24)

    assert not work.exists(), "orphaned work dir with jailroot must be pruned"
    assert deleted >= 1
