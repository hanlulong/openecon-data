"""Request-scoped deadline budget for provider I/O (deflation only).

The query endpoints enforce a hard deadline by cancelling the whole request
from outside (``asyncio.wait_for`` / the stream timeout). Nothing below the
endpoint knew that deadline, so a provider retry loop could start a 30s retry
with 5s of request left and the user got a 504 instead of an honest answer.

This module carries the deadline down to the provider layer through a
contextvar, so the retry core can:

- refuse to START a new provider operation inside the final
  ``answer_reserve_s`` seconds (no I/O, typed no-verdict);
- stop retrying, and cap a RETRY attempt's timeout, so that a retry leaves
  ``retry_reserve_s`` seconds for cross-provider fallback and the answer.
  Inside the FINAL provider stage (``final_provider_stage``: the cross-provider
  fallback fetch itself, or a fetch no fallback can follow) there is no later
  stage to leave time for, so a retry only has to fit before the answer
  reserve.

Every operation here only SHORTENS work: no timeout or retry count is ever
raised, and a first attempt's timeout is never touched. With no budget active
(tests, scripts, prewarm, background jobs, Pro Mode) every helper answers as
if time were unlimited, so behaviour is unchanged.

Structural only: monotonic time against an explicit deadline. Stdlib only and
deliberately independent of ``retry.py`` (which imports from here).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

# A retry attempt shorter than this cannot plausibly get an answer; do not
# start one (and never clamp a retry's timeout below it).
MIN_RETRY_ATTEMPT_SECONDS = 2.0
# Cross-provider fallback needs at least this much I/O time to be worth
# starting (a fallback resolves an indicator and fetches from scratch).
MIN_FALLBACK_IO_SECONDS = 10.0


@dataclass
class RequestBudget:
    """One request's deadline (``time.monotonic`` seconds) and its reserves."""

    deadline: float
    answer_reserve_s: float
    retry_reserve_s: float
    closed: bool = False

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


_CURRENT: ContextVar[Optional[RequestBudget]] = ContextVar(
    "openecon_request_budget", default=None
)

# TASK-LOCAL marker (never a field on the shared RequestBudget): concurrent
# fetch legs of one request share the budget object, but only the legs that run
# inside ``final_provider_stage`` may spend the retry reserve. A contextvar set
# inside a task is invisible to its siblings and to the caller once reset.
# None: no stage decided yet; True/False: an enclosing caller decided whether
# this is the final provider stage (an inner fetch never re-decides it).
_FINAL_STAGE: ContextVar[Optional[bool]] = ContextVar("openecon_final_provider_stage", default=None)


def current_budget() -> Optional[RequestBudget]:
    """The active request budget, or None when unset or already closed.

    A closed budget reads as None so a detached task spawned during the
    request (it copied the context) reverts to unbounded behaviour instead of
    inheriting a dead deadline.
    """
    budget = _CURRENT.get()
    if budget is None or budget.closed:
        return None
    return budget


def start_request_budget(
    total_seconds: float,
    *,
    answer_reserve_seconds: float,
    retry_reserve_seconds: float,
) -> Tuple[RequestBudget, Token]:
    """Start a budget of ``total_seconds`` in the current context.

    A nested budget may only SHORTEN an active outer one. The retry reserve is
    never smaller than the answer reserve.
    """
    deadline = time.monotonic() + max(0.0, float(total_seconds))
    outer = current_budget()
    if outer is not None:
        deadline = min(deadline, outer.deadline)
    answer = max(0.0, float(answer_reserve_seconds))
    retry = max(answer, float(retry_reserve_seconds))
    budget = RequestBudget(deadline=deadline, answer_reserve_s=answer, retry_reserve_s=retry)
    token = _CURRENT.set(budget)
    return budget, token


def end_request_budget(budget: Optional[RequestBudget], token: Optional[Token]) -> None:
    """Close ``budget`` and restore the previous context value.

    Closing first matters: tasks that copied the context keep a reference to
    the budget object and must stop honouring it once the request is over.
    Resetting a token from another context raises; that is swallowed (the
    budget is closed either way).
    """
    if budget is not None:
        budget.closed = True
    if token is None:
        return
    try:
        _CURRENT.reset(token)
    except (ValueError, RuntimeError):
        pass


@contextmanager
def request_budget(
    total_seconds: float,
    *,
    answer_reserve_seconds: float,
    retry_reserve_seconds: float,
) -> Iterator[RequestBudget]:
    """Context-manager form of start/end_request_budget."""
    budget, token = start_request_budget(
        total_seconds,
        answer_reserve_seconds=answer_reserve_seconds,
        retry_reserve_seconds=retry_reserve_seconds,
    )
    try:
        yield budget
    finally:
        end_request_budget(budget, token)


def io_remaining() -> Optional[float]:
    """Seconds left for provider I/O (deadline minus the answer reserve), or
    None when no budget is active."""
    budget = current_budget()
    if budget is None:
        return None
    return budget.remaining() - budget.answer_reserve_s


def budget_allows(seconds: float = 0.0) -> bool:
    """True when more than ``seconds`` of I/O time is left (always True with
    no budget)."""
    remaining = io_remaining()
    if remaining is None:
        return True
    return remaining > seconds


@contextmanager
def provider_stage(*, final: bool) -> Iterator[None]:
    """Decide, for the enclosed provider work, whether it is the request's
    LAST provider stage.

    ``final=True``: no cross-provider fallback can follow it, so its retries
    are checked against the answer reserve instead of the retry reserve (whose
    only purpose is leaving time for that fallback). ``final=False`` records
    that a later stage IS scheduled, so an inner fetch does not re-decide.
    Task-local: it covers the current task and the tasks it spawns, and is
    reset on exit.
    """
    token = _FINAL_STAGE.set(bool(final))
    try:
        yield
    finally:
        _FINAL_STAGE.reset(token)


def final_provider_stage():
    """``provider_stage(final=True)``."""
    return provider_stage(final=True)


def in_final_provider_stage() -> bool:
    """True inside ``final_provider_stage`` (for the current task)."""
    return bool(_FINAL_STAGE.get())


def provider_stage_decided() -> bool:
    """True when an enclosing ``provider_stage`` already decided the stage."""
    return _FINAL_STAGE.get() is not None


def _retry_reserve(budget: RequestBudget) -> float:
    """Seconds a retry must leave: the retry reserve while a later
    cross-provider fallback stage is still possible, else the answer reserve."""
    return budget.answer_reserve_s if _FINAL_STAGE.get() else budget.retry_reserve_s


def retry_fits(upcoming_sleep: float) -> bool:
    """True when a retry that first sleeps ``upcoming_sleep`` still leaves a
    useful attempt before the retry reserve (the answer reserve in the final
    provider stage; always True with no budget)."""
    budget = current_budget()
    if budget is None:
        return True
    return (
        budget.remaining() - float(upcoming_sleep or 0.0) - _retry_reserve(budget)
        >= MIN_RETRY_ATTEMPT_SECONDS
    )


def retry_attempt_timeout(requested: float) -> float:
    """The timeout for a RETRY attempt: ``requested`` capped so the attempt
    ends before the retry reserve (the answer reserve in the final provider
    stage), never below MIN_RETRY_ATTEMPT_SECONDS and never above
    ``requested`` (no budget: ``requested`` unchanged)."""
    budget = current_budget()
    if budget is None:
        return requested
    cap = budget.remaining() - _retry_reserve(budget)
    if cap >= requested:
        return requested
    # min(): a requested timeout below the floor is never raised to it.
    return min(requested, max(MIN_RETRY_ATTEMPT_SECONDS, cap))


def fallback_allowed() -> bool:
    """True when enough I/O time is left to start cross-provider fallback."""
    return budget_allows(MIN_FALLBACK_IO_SECONDS)
