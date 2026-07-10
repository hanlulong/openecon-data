"""Background email task must be strongly referenced (FIX 3).

submit_feedback_async fires the blocking email send as a background task via
loop.create_task(asyncio.to_thread(...)). Previously the returned task was
stored nowhere, so it could be garbage-collected mid-flight and the
notification silently dropped. It is now added to a module-level set and
discarded via add_done_callback (the same pattern main.py uses for its
background tasks). These tests verify the task is tracked while running and
reaped when finished, and that the email actually gets sent.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from backend.models import FeedbackRequest
from backend.services.feedback import FeedbackService, _background_tasks


def _hermetic_service(monkeypatch):
    """FeedbackService that never touches disk, with a mockable email send."""
    svc = FeedbackService()
    svc._feedback = []
    monkeypatch.setattr(svc, "_save_feedback", lambda: None)
    return svc


async def test_async_submit_tracks_task_then_reaps_it(monkeypatch):
    svc = _hermetic_service(monkeypatch)
    sent = MagicMock(return_value=True)
    monkeypatch.setattr(svc, "_send_email_notification", sent)

    _background_tasks.clear()
    resp = await svc.submit_feedback_async(FeedbackRequest(type="bug", message="hi"))
    assert resp.success is True

    # The task is strongly referenced while in flight (the GC-hazard fix).
    assert len(_background_tasks) == 1
    task = next(iter(_background_tasks))

    # Let the to_thread email send run to completion.
    await task
    # Flush the add_done_callback (scheduled via call_soon on completion).
    await asyncio.sleep(0)

    sent.assert_called_once()
    # Task reaped from the tracking set once finished.
    assert task not in _background_tasks


async def test_email_failure_does_not_break_response_or_leak_task(monkeypatch):
    svc = _hermetic_service(monkeypatch)
    boom = MagicMock(side_effect=RuntimeError("smtp down"))
    monkeypatch.setattr(svc, "_send_email_notification", boom)

    _background_tasks.clear()
    resp = await svc.submit_feedback_async(FeedbackRequest(type="bug", message="hi"))
    # Response returns immediately, independent of email outcome.
    assert resp.success is True

    task = next(iter(_background_tasks))
    # The task raises internally; awaiting surfaces it, but the tracking set is
    # still cleaned up by the done-callback.
    with __import__("pytest").raises(RuntimeError):
        await task
    await asyncio.sleep(0)
    assert task not in _background_tasks
