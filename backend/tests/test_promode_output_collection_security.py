"""Pro Mode output collection must never trust a path the jailed program wrote.

The per-exec output dir (jail /tmp) and work dir (jail /work) are fully
controlled by the generated program. Publishing, reading result.json, and
cleanup must operate only on verified regular files created/opened without
following links, so no entry there can make the backend publish, read,
re-mode, or delete a host file outside the tree.
"""

import os
import stat
from pathlib import Path

import pytest

from backend.services import secure_code_executor as sce
from backend.services.secure_code_executor import SecureCodeExecutor
from backend.services.session_storage import force_rmtree


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
    with caplog.at_level("WARNING", logger=sce.__name__):
        executor._collect_generated_files(out_dir, "sess")
    msgs = [r.getMessage() for r in caplog.records if "promode_output_rejected" in r.getMessage()]
    assert len(msgs) == 1
    assert "TOP_SECRET" not in msgs[0]


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
