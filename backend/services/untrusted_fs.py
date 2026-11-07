"""Backend-side access to filesystem entries a jailed Pro Mode program can write.

The per-execution output dir (jail /tmp), the per-execution work dir and the
persistent session dir (jail /sess) are writable by generated code, so every
NAME inside them is attacker-controlled: it may be a symlink to a host file
(the backend .env, source files, ~/.ssh), a hardlink, a FIFO, a directory, or
arbitrarily large. Backend code must never use a path-following
open/chmod/move on such a name. These helpers are the one place that touches
them, and they only ever:

- READ a verified single-link regular file through an ``O_NOFOLLOW`` fd whose
  inode matches the pre-open ``lstat`` (a swap in between is detected);
- WRITE by creating a NEW file (``O_CREAT|O_EXCL|O_NOFOLLOW``) and renaming it
  over the name — ``rename`` replaces a link planted there, it never follows
  it — with the mode set by ``fchmod`` on our own fd.

Rejected entries are logged once each as a structured warning carrying the
entry name (``repr``, so control characters cannot forge log lines) and the
reason, never the contents.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Event names (stable, grep-able in the journal).
OUTPUT_REJECTED_EVENT = "promode_output_rejected"
SESSION_REJECTED_EVENT = "promode_session_entry_rejected"

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def reject_untrusted_entry(
    path: Path, reason: str, *, event: str = OUTPUT_REJECTED_EVENT, **detail: Any
) -> None:
    """One structured warning per rejected sandbox-written entry (no contents)."""
    logger.warning(
        "%s entry=%r reason=%s %s",
        event,
        Path(path).name,
        reason,
        " ".join(f"{k}={v}" for k, v in sorted(detail.items())),
    )


def is_regular_file_nofollow(path: Path) -> bool:
    """True only for a plain regular file at ``path`` itself (links are not followed)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def open_untrusted_regular_file(
    path: Path, max_bytes: int, *, event: str = OUTPUT_REJECTED_EVENT
) -> Optional[int]:
    """Open a sandbox-written entry for reading WITHOUT following links.

    Returns a read-only fd only when the entry is a plain regular file with a
    single link and size <= max_bytes, verified both before (lstat) and after
    (fstat on the opened fd, same st_dev/st_ino) the open so a swap in
    between is detected. O_NOFOLLOW refuses a symlink at open time and
    O_NONBLOCK keeps a swapped-in FIFO from blocking the backend. Returns
    None (after logging) for anything else. Caller owns the fd.
    """
    try:
        st = os.lstat(path)
    except OSError as e:
        reject_untrusted_entry(path, "lstat_failed", event=event, errno=e.errno)
        return None
    if not stat.S_ISREG(st.st_mode):
        reject_untrusted_entry(path, "not_regular_file", event=event, mode=oct(st.st_mode))
        return None
    if st.st_nlink != 1:
        reject_untrusted_entry(path, "multiple_links", event=event, nlink=st.st_nlink)
        return None
    if st.st_size > max_bytes:
        reject_untrusted_entry(path, "too_large", event=event, size=st.st_size, cap=max_bytes)
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC)
    except OSError as e:
        reject_untrusted_entry(path, "open_refused", event=event, errno=e.errno)
        return None
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            reject_untrusted_entry(path, "changed_during_open", event=event)
        elif not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1:
            reject_untrusted_entry(path, "not_regular_file_after_open", event=event)
        elif fst.st_size > max_bytes:
            reject_untrusted_entry(
                path, "too_large", event=event, size=fst.st_size, cap=max_bytes
            )
        else:
            return fd
    except OSError as e:
        reject_untrusted_entry(path, "fstat_failed", event=event, errno=e.errno)
    os.close(fd)
    return None


def read_untrusted_bytes(
    path: Path, max_bytes: int, *, event: str = OUTPUT_REJECTED_EVENT
) -> Optional[bytes]:
    """Read a sandbox-written regular file (no link following, size-capped).

    A file that grows past ``max_bytes`` while being read is refused rather
    than returned truncated.
    """
    fd = open_untrusted_regular_file(path, max_bytes, event=event)
    if fd is None:
        return None
    try:
        chunks, total = [], 0
        while total <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            reject_untrusted_entry(path, "too_large_during_read", event=event, cap=max_bytes)
            return None
        return b"".join(chunks)
    except OSError as e:
        reject_untrusted_entry(path, "read_failed", event=event, errno=e.errno)
        return None
    finally:
        os.close(fd)


def read_untrusted_text(
    path: Path, max_bytes: int, *, event: str = OUTPUT_REJECTED_EVENT
) -> Optional[str]:
    """``read_untrusted_bytes`` decoded as UTF-8 (undecodable bytes replaced)."""
    raw = read_untrusted_bytes(path, max_bytes, event=event)
    return None if raw is None else raw.decode("utf-8", errors="replace")


def replace_file_nofollow(path: Path, data: bytes, mode: int) -> None:
    """Atomically make ``path`` a NEW backend-owned regular file holding ``data``.

    The bytes go into a fresh temp file in the same directory (mkstemp:
    O_CREAT|O_EXCL|O_NOFOLLOW), its mode is set with fchmod on our fd, and it
    is renamed over ``path``. Whatever was at ``path`` — including a link the
    jailed program planted — is replaced, never followed or written through.
    Raises OSError on failure (the temp file is removed).
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
