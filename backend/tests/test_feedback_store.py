"""Feedback persistence must be multi-writer safe (FB-1).

Production runs two uvicorn workers (separate processes) over one data dir.
The old store loaded feedback.json once per process, appended in memory and
rewrote the whole file, so each worker silently overwrote the other's records
(a 2026-11-06 submission got HTTP 200 + an email but was missing from disk),
and a corrupt read reset the list to [] and then overwrote the file.

These tests pin the MECHANISM, not one record: every writer (other service
instances, other threads, other processes) persists; reads are on demand;
corrupt data is quarantined/skipped, never wiped; the legacy migration is
locked and idempotent.
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import threading

import pytest

from backend.models import FeedbackRequest
from backend.services.feedback import (
    FEEDBACK_STORE_NAME,
    LEGACY_STORE_NAME,
    FeedbackService,
)


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    # Never reach a real mail service from these tests.
    monkeypatch.setattr(FeedbackService, "_send_email_notification", lambda self, fb: False)


def _req(i) -> FeedbackRequest:
    return FeedbackRequest(type="bug", message=f"message {i}")


def _ids(svc: FeedbackService) -> set:
    return {r["id"] for r in svc.get_all_feedback()}


def test_two_service_instances_on_one_dir_both_persist(tmp_path):
    """Two workers = two instances; neither may overwrite the other."""
    worker_a = FeedbackService(feedback_dir=tmp_path)
    worker_b = FeedbackService(feedback_dir=tmp_path)

    a1 = worker_a.submit_feedback(_req("a1")).feedbackId
    b1 = worker_b.submit_feedback(_req("b1")).feedbackId
    a2 = worker_a.submit_feedback(_req("a2")).feedbackId

    expected = {a1, b1, a2}
    assert _ids(worker_a) == expected
    assert _ids(worker_b) == expected
    assert _ids(FeedbackService(feedback_dir=tmp_path)) == expected
    assert worker_b.get_feedback_by_id(a1)["message"] == "message a1"


def test_fifty_concurrent_threads_all_persist(tmp_path):
    svc = FeedbackService(feedback_dir=tmp_path)
    ids: list = []
    ids_lock = threading.Lock()
    barrier = threading.Barrier(50)

    def submit(i):
        barrier.wait()
        fid = svc.submit_feedback(_req(i)).feedbackId
        with ids_lock:
            ids.append(fid)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == 50
    assert _ids(svc) == set(ids)
    # Every line is a complete JSON record (no interleaved partial writes).
    lines = (tmp_path / FEEDBACK_STORE_NAME).read_bytes().splitlines()
    assert len(lines) == 50
    assert all(isinstance(json.loads(line), dict) for line in lines)


def _child_writer(directory, n, queue):
    from backend.services.feedback import FeedbackService as _Svc

    _Svc._send_email_notification = lambda self, fb: False
    svc = _Svc(feedback_dir=directory)
    queue.put([svc.submit_feedback(FeedbackRequest(type="other", message=f"p{i}")).feedbackId
               for i in range(n)])


def test_separate_processes_all_persist(tmp_path):
    """The production shape: separate worker processes, one directory."""
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_child_writer, args=(str(tmp_path), 15, queue)) for _ in range(2)]
    for p in procs:
        p.start()
    written = set()
    for _ in procs:
        written.update(queue.get(timeout=60))
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert len(written) == 30
    assert _ids(FeedbackService(feedback_dir=tmp_path)) == written


async def test_async_submit_persists(tmp_path):
    svc = FeedbackService(feedback_dir=tmp_path)
    resp = await svc.submit_feedback_async(_req("async"))
    assert svc.get_feedback_by_id(resp.feedbackId)["message"] == "message async"


def test_legacy_store_migrates_once_and_is_kept(tmp_path):
    legacy = [
        {"id": "legacy-1", "type": "bug", "message": "old one"},
        {"id": "legacy-2", "type": "feature", "message": "old two"},
    ]
    legacy_text = json.dumps(legacy, indent=2)
    (tmp_path / LEGACY_STORE_NAME).write_text(legacy_text)

    first = FeedbackService(feedback_dir=tmp_path)
    assert _ids(first) == {"legacy-1", "legacy-2"}
    assert not (tmp_path / LEGACY_STORE_NAME).exists()
    assert (tmp_path / f"{LEGACY_STORE_NAME}.migrated").read_text() == legacy_text

    # A second worker (and a re-run) must not migrate again or duplicate.
    second = FeedbackService(feedback_dir=tmp_path)
    second.submit_feedback(_req("new"))
    records = second.get_all_feedback()
    assert [r["id"] for r in records].count("legacy-1") == 1
    assert len(records) == 3


def test_migration_is_idempotent_after_partial_crash(tmp_path):
    """Crash between append and rename: re-migration skips present ids."""
    (tmp_path / FEEDBACK_STORE_NAME).write_text(json.dumps({"id": "legacy-1"}) + "\n")
    (tmp_path / LEGACY_STORE_NAME).write_text(json.dumps([{"id": "legacy-1"}, {"id": "legacy-2"}]))
    # An earlier migration's sidecar must not be overwritten either.
    (tmp_path / f"{LEGACY_STORE_NAME}.migrated").write_text("earlier")

    svc = FeedbackService(feedback_dir=tmp_path)
    ids = [r["id"] for r in svc.get_all_feedback()]
    assert sorted(ids) == ["legacy-1", "legacy-2"]
    assert (tmp_path / f"{LEGACY_STORE_NAME}.migrated").read_text() == "earlier"
    assert len(list(tmp_path.glob(f"{LEGACY_STORE_NAME}.migrated-*"))) == 1


def test_truncated_legacy_store_is_quarantined_not_wiped(tmp_path, caplog):
    truncated = '[{"id": "legacy-1", "type": "bug", "message": "cut off mid-wri'
    (tmp_path / LEGACY_STORE_NAME).write_text(truncated)

    svc = FeedbackService(feedback_dir=tmp_path)
    with caplog.at_level(logging.ERROR, logger="openecon.feedback"):
        new_id = svc.submit_feedback(_req("after corruption")).feedbackId

    quarantined = list(tmp_path.glob(f"{LEGACY_STORE_NAME}.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == truncated  # preserved byte-for-byte
    assert not (tmp_path / LEGACY_STORE_NAME).exists()
    assert _ids(svc) == {new_id}
    assert any("quarantined" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def test_corrupt_jsonl_line_is_skipped_and_logged(tmp_path, caplog):
    svc = FeedbackService(feedback_dir=tmp_path)
    first = svc.submit_feedback(_req(1)).feedbackId
    with open(tmp_path / FEEDBACK_STORE_NAME, "ab") as f:
        f.write(b'{"id": "torn", "mess\n')
    last = svc.submit_feedback(_req(2)).feedbackId

    with caplog.at_level(logging.ERROR, logger="openecon.feedback"):
        assert _ids(svc) == {first, last}
    assert any("corrupt feedback record" in r.getMessage() for r in caplog.records)


def test_construction_does_no_disk_io(tmp_path):
    """The module singleton is built at import time; it must not create dirs
    or migrate anything until feedback is actually used."""
    target = tmp_path / "not-yet"
    FeedbackService(feedback_dir=target)
    assert not target.exists()
