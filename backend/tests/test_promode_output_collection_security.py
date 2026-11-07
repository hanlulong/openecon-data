"""Pro Mode output collection must never trust a path the jailed program wrote.

The per-exec output dir (jail /tmp), work dir and persistent session dir
(jail /sess) are fully controlled by the generated program. Publishing,
reading result.json, session-store reads/writes, and cleanup must operate only
on verified regular files created/opened without following links, so no entry
there can make the backend publish, read, overwrite, re-mode, or delete a host
file outside the tree.
"""

import asyncio
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from backend.services import session_storage as ss
from backend.services import untrusted_fs
from backend.services.secure_code_executor import SecureCodeExecutor
from backend.services.session_storage import SessionStorage, force_rmtree


@pytest.fixture
def executor(tmp_path):
    return SecureCodeExecutor(
        session_dir=tmp_path / "sessions",
        public_dir=tmp_path / "public",
    )


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "sessions" / "work" / "0123456789abcdef_out"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def host_secret(tmp_path):
    """A host file outside the tree, private to its owner (like the backend .env)."""
    secret = tmp_path / "host" / "secret.env"
    secret.parent.mkdir()
    secret.write_text("TOP_SECRET=1\n")
    os.chmod(secret, 0o600)
    return secret


def _published(executor):
    return sorted(p.name for p in executor.public_dir.iterdir())


def test_regular_file_is_published_0644_backend_owned(executor, out_dir):
    src = out_dir / "promode_chart.png"
    src.write_bytes(b"\x89PNG fake")
    os.chmod(src, 0o600)

    files = executor._collect_generated_files(out_dir, "sess")

    assert len(files) == 1
    dest = executor.public_dir / files[0]["name"]
    st = os.lstat(dest)
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o644
    assert st.st_uid == os.getuid()
    assert dest.read_bytes() == b"\x89PNG fake"
    assert files[0]["url"] == f"/static/promode/{files[0]['name']}"
    assert not os.path.lexists(src), "source must be consumed"


@pytest.mark.parametrize("suffix", [".csv", ".json", ".png"])
def test_symlink_to_host_file_is_not_published_and_target_mode_unchanged(
    executor, out_dir, host_secret, suffix
):
    link = out_dir / f"promode_data{suffix}"
    link.symlink_to(host_secret)

    files = executor._collect_generated_files(out_dir, "sess")

    assert files == []
    assert _published(executor) == []
    assert stat.S_IMODE(os.stat(host_secret).st_mode) == 0o600
    assert host_secret.read_text() == "TOP_SECRET=1\n"


def test_hardlink_is_rejected(executor, out_dir, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text("a,b\n")
    os.link(other, out_dir / "promode_linked.csv")

    assert executor._collect_generated_files(out_dir, "sess") == []
    assert _published(executor) == []
    assert other.read_text() == "a,b\n"


def test_fifo_is_rejected_without_blocking(executor, out_dir):
    os.mkfifo(out_dir / "promode_pipe.csv")
    assert executor._collect_generated_files(out_dir, "sess") == []
    assert _published(executor) == []


def test_directory_entry_is_rejected(executor, out_dir):
    (out_dir / "promode_dir.csv").mkdir()
    assert executor._collect_generated_files(out_dir, "sess") == []


def test_oversize_file_is_rejected(executor, out_dir, monkeypatch):
    monkeypatch.setattr(SecureCodeExecutor, "MAX_PUBLISHED_FILE_BYTES", 16)
    (out_dir / "promode_big.csv").write_bytes(b"x" * 17)
    (out_dir / "promode_ok.csv").write_bytes(b"x" * 16)

    files = executor._collect_generated_files(out_dir, "sess")

    assert [f["name"].endswith("_ok.csv") for f in files] == [True]
    assert len(_published(executor)) == 1


def test_existing_public_name_is_never_overwritten(executor, out_dir, host_secret):
    """O_EXCL: a pre-existing entry at the destination (including a link) is
    neither followed nor replaced."""
    (out_dir / "promode_chart.png").write_bytes(b"new")
    dest = executor.public_dir / f"promode_{out_dir.name}_chart.png"
    dest.symlink_to(host_secret)

    assert executor._collect_generated_files(out_dir, "sess") == []
    assert host_secret.read_text() == "TOP_SECRET=1\n"
    assert stat.S_IMODE(os.stat(host_secret).st_mode) == 0o600


def test_rejections_are_logged_without_contents(executor, out_dir, host_secret, caplog):
    (out_dir / "promode_x.csv").symlink_to(host_secret)
    with caplog.at_level("WARNING", logger=untrusted_fs.__name__):
        executor._collect_generated_files(out_dir, "sess")
    msgs = [r.getMessage() for r in caplog.records if "promode_output_rejected" in r.getMessage()]
    assert len(msgs) == 1
    assert "TOP_SECRET" not in msgs[0]


def test_rejection_log_cannot_be_forged_by_entry_name(executor, out_dir, host_secret, caplog):
    """Entry names are attacker-chosen; control characters are escaped (repr)."""
    (out_dir / "promode_x\nFAKE reason=ok\n.csv").symlink_to(host_secret)
    with caplog.at_level("WARNING", logger=untrusted_fs.__name__):
        executor._collect_generated_files(out_dir, "sess")
    msgs = [r.getMessage() for r in caplog.records if "promode_output_rejected" in r.getMessage()]
    assert len(msgs) == 1
    assert "\n" not in msgs[0]


def test_per_execution_file_count_cap(executor, out_dir, monkeypatch, caplog):
    monkeypatch.setattr(SecureCodeExecutor, "MAX_PUBLISHED_FILES", 3)
    for i in range(7):
        (out_dir / f"promode_{i}.csv").write_text("a\n")

    with caplog.at_level("WARNING", logger="backend.services.secure_code_executor"):
        files = executor._collect_generated_files(out_dir, "sess")

    assert len(files) == 3
    assert len(_published(executor)) == 3
    caps = [r.getMessage() for r in caplog.records if "reason=file_count_cap" in r.getMessage()]
    assert caps == ["promode_output_rejected reason=file_count_cap skipped=4 cap=3"]


def test_per_execution_total_bytes_budget(executor, out_dir, monkeypatch):
    monkeypatch.setattr(SecureCodeExecutor, "MAX_PUBLISHED_TOTAL_BYTES", 25)
    (out_dir / "promode_a.csv").write_bytes(b"x" * 10)
    (out_dir / "promode_b.csv").write_bytes(b"x" * 10)
    (out_dir / "promode_c.csv").write_bytes(b"x" * 10)  # would exceed the budget
    (out_dir / "promode_d.csv").write_bytes(b"x" * 5)   # still fits

    files = executor._collect_generated_files(out_dir, "sess")

    assert [f["name"].rsplit("_", 1)[-1] for f in files] == ["a.csv", "b.csv", "d.csv"]
    total = sum((executor.public_dir / f["name"]).stat().st_size for f in files)
    assert total <= 25


def test_collection_runs_off_the_event_loop(executor, monkeypatch):
    """Publishing copies bytes, so execute_code must not run it on the loop thread."""
    seen = {}

    async def fake_canary(self, force=False):
        return True

    async def fake_run(self, *args, **kwargs):
        return {"success": True, "output": "ok"}

    def fake_collect(self, tmp_out_dir, session_id):
        seen["thread"] = threading.current_thread()
        return []

    monkeypatch.setattr(SecureCodeExecutor, "canary_self_test", fake_canary)
    monkeypatch.setattr(SecureCodeExecutor, "_run_sandboxed", fake_run)
    monkeypatch.setattr(SecureCodeExecutor, "_collect_generated_files", fake_collect)
    monkeypatch.setattr(SecureCodeExecutor, "_layer2_active", lambda self: False)

    async def go():
        loop_thread = threading.current_thread()
        result = await executor.execute_code("print(1)", "sess-thread")
        return loop_thread, result

    loop_thread, result = asyncio.run(go())
    assert result["success"] is True
    assert seen["thread"] is not loop_thread


# --- SessionStorage: the session dir is jail /sess (writable by generated code)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "get_session_storage_dir", lambda: tmp_path / "sessions")
    return SessionStorage()


def test_session_save_replaces_planted_link_instead_of_writing_through(store, host_secret):
    sess_dir = store._get_session_dir("conv1")
    (sess_dir / "statscan_metadata.json").symlink_to(host_secret)

    assert store.save("conv1", "statscan_metadata", {"k": [1, 2]}) is True

    assert host_secret.read_text() == "TOP_SECRET=1\n"
    assert stat.S_IMODE(os.stat(host_secret).st_mode) == 0o600
    key = sess_dir / "statscan_metadata.json"
    st = os.lstat(key)
    assert stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
    assert stat.S_IMODE(st.st_mode) == ss.SESSION_FILE_MODE
    assert json.loads(key.read_text()) == {"k": [1, 2]}
    assert [p.name for p in sess_dir.iterdir()] == ["statscan_metadata.json"], "no temp left"


def test_session_save_round_trips_and_overwrites(store):
    assert store.save("conv1", "data", {"a": 1})
    assert store.save("conv1", "data", {"a": 2})
    assert store.load("conv1", "data") == {"a": 2}
    assert store.list_keys("conv1") == ["data"]


def test_session_load_does_not_follow_links(store, host_secret):
    sess_dir = store._get_session_dir("conv1")
    host_json = host_secret.with_suffix(".json")
    host_json.write_text('{"secret": true}')
    (sess_dir / "stolen.json").symlink_to(host_json)
    os.mkfifo(sess_dir / "pipe.json")

    assert store.load("conv1", "stolen") is None
    assert store.load("conv1", "pipe") is None
    assert store.load("conv1", "missing") is None


def test_session_list_keys_only_lists_regular_files(store, host_secret):
    sess_dir = store._get_session_dir("conv1")
    (sess_dir / "real.json").write_text("{}")
    (sess_dir / "link.json").symlink_to(host_secret)
    (sess_dir / "dir.json").mkdir()

    assert store.list_keys("conv1") == ["real"]


def test_result_file_read_does_not_follow_links(tmp_path, host_secret):
    link = tmp_path / "result.json"
    host_json = host_secret.with_suffix(".json")
    host_json.write_text('{"success": true, "output": "HOST DATA"}')
    link.symlink_to(host_json)

    assert SecureCodeExecutor._read_untrusted_text(link, 1024) is None

    real = tmp_path / "real.json"
    real.write_text('{"success": true}')
    assert SecureCodeExecutor._read_untrusted_text(real, 1024) == '{"success": true}'
    assert SecureCodeExecutor._read_untrusted_text(real, 4) is None


def test_force_rmtree_on_link_removes_only_the_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep")
    link = tmp_path / "work_link"
    link.symlink_to(outside, target_is_directory=True)

    assert force_rmtree(link) is True
    assert not os.path.lexists(link)
    assert keep.read_text() == "keep"


def test_force_rmtree_unlinks_inner_links_without_traversal(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "inner").symlink_to(outside, target_is_directory=True)

    assert force_rmtree(tree) is True
    assert keep.read_text() == "keep"


def test_sharing_perms_refuse_paths_resolving_outside_session_root(
    executor, tmp_path, monkeypatch
):
    import grp

    monkeypatch.setattr(SecureCodeExecutor, "_layer2_active", lambda self: True)
    monkeypatch.setattr(grp, "getgrnam", lambda name: grp.struct_group(("g", "x", os.getgid(), [])))
    outside = tmp_path / "host_dir"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    linked = executor.session_dir / "linked_session"
    linked.symlink_to(outside, target_is_directory=True)
    ancestor_mode_before = stat.S_IMODE(os.stat(tmp_path).st_mode)

    executor._apply_sharing_perms(linked)

    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == ancestor_mode_before


def test_sharing_perms_still_apply_inside_session_root(executor, monkeypatch):
    import grp

    monkeypatch.setattr(SecureCodeExecutor, "_layer2_active", lambda self: True)
    monkeypatch.setattr(grp, "getgrnam", lambda name: grp.struct_group(("g", "x", os.getgid(), [])))
    leaf = executor.session_dir / "work" / "abc"
    leaf.mkdir(parents=True, mode=0o700)

    executor._apply_sharing_perms(leaf)

    for d in (leaf, leaf.parent, executor.session_dir):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o2770
