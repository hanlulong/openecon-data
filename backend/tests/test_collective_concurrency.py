"""Independent country/time follow-ups overlap I/O and commit as one result."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.services import query as query_module
from backend.services.conversation_state_v2 import FollowUpDelta
from backend.services.query import QueryService
from backend.tests.test_collective_outage_recovery import _series, _state
from backend.utils.retry import ProviderUnavailable


@pytest.fixture
def service(monkeypatch):
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    monkeypatch.setattr(svc, "_verify_execution_result", AsyncMock(return_value=None))
    monkeypatch.setattr(query_module.conversation_manager, "add_message_safe", lambda cid, *_a, **_kw: cid)
    saved = []
    monkeypatch.setattr(query_module.conversation_manager, "set_conversation_state", lambda cid, state: saved.append(state.model_copy(deep=True)))
    return svc, saved


async def execute(svc, delta, state=None):
    return await svc._execute_collective_answer_member_delta(
        query=delta.raw_query or "update comparison", conversation_id="concurrent-test",
        tracker=None, state=state or _state(), delta=delta,
    )


def _eight_members():
    return FollowUpDelta(added_countries=["MX", "US", "CA", "JP", "FR"], delta_type="add_country", raw_query="add these countries")


def test_member_limit_includes_fallback_and_preserves_original_order(service, monkeypatch):
    svc, saved = service
    active = 0
    peak = 0
    started = []
    countries = ["PE", "CO", "BR", "MX", "US", "CA", "JP", "FR"]

    async def run():
        first_batch = asyncio.Event()

        async def fetch(intent):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.append(intent.parameters["country"])
            if len(started) == 4:
                first_batch.set()
            raise ProviderUnavailable("simulated provider wait")

        async def fallback(intent, error):
            nonlocal active
            country = intent.parameters["country"]
            try:
                await first_batch.wait()
                # Deliberately complete in a different order from the targets.
                await asyncio.sleep((8 - countries.index(country)) * 0.002)
                return [_series(country, "IMF")]
            finally:
                active -= 1

        monkeypatch.setattr(svc, "_fetch_data", fetch)
        monkeypatch.setattr(svc, "_try_with_fallback", fallback)
        return await asyncio.wait_for(execute(svc, _eight_members()), timeout=2)

    result = asyncio.run(run())
    assert peak == 4
    assert active == 0
    assert not result.error
    assert [s.metadata.country for s in result.data] == countries
    assert len(saved) == 1
    assert {m.country for m in saved[0].active_answer_members} == set(countries)


def test_indicator_changes_keep_shared_resolution_sequential(service, monkeypatch):
    svc, saved = service
    active = 0
    peak = 0
    intents = []

    async def fetch(intent):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        intents.append(intent.model_copy(deep=True))
        try:
            await asyncio.sleep(0.005)
            series = _series(intent.parameters["country"])
            series.metadata.indicator = "Gross domestic product"
            series.metadata.seriesId = "NY.GDP.MKTP.CD"
            return [series]
        finally:
            active -= 1

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    delta = FollowUpDelta(changed_indicator="GDP", delta_type="indicator_change", raw_query="GDP for all")
    result = asyncio.run(execute(svc, delta))
    assert not result.error
    assert peak == 1
    assert intents[0].parameters["__delta_indicator_changed"] is True
    assert all(i.parameters.get("__decision_source") == "collective_shared_resolution" for i in intents[1:])
    assert len(saved) == 1


def test_failed_member_never_commits_other_concurrent_results(service, monkeypatch):
    svc, saved = service
    state = _state()
    original = state.model_dump()

    async def fetch(intent):
        country = intent.parameters["country"]
        await asyncio.sleep(0.005)
        if country == "CO":
            raise ProviderUnavailable("down")
        return [_series(country)]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    monkeypatch.setattr(svc, "_try_with_fallback", AsyncMock(return_value=None))
    result = asyncio.run(execute(svc, _eight_members(), state))
    assert result.error == "provider_unavailable"
    assert not saved
    assert state.model_dump() == original


def test_external_cancellation_awaits_active_and_queued_members(service, monkeypatch):
    svc, saved = service
    started = set()
    cancelled = set()

    async def run():
        all_slots_used = asyncio.Event()
        blocked = asyncio.Event()

        async def fetch(intent):
            country = intent.parameters["country"]
            started.add(country)
            if len(started) == 4:
                all_slots_used.set()
            try:
                await blocked.wait()
            finally:
                cancelled.add(country)

        monkeypatch.setattr(svc, "_fetch_data", fetch)
        task = asyncio.create_task(execute(svc, _eight_members()))
        await asyncio.wait_for(all_slots_used.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(started) == 4
    assert cancelled == started
    assert not saved


def test_self_cancelled_member_cannot_become_silent_partial_success(service, monkeypatch):
    svc, saved = service

    async def fetch(intent):
        if intent.parameters["country"] == "PE":
            raise asyncio.CancelledError()
        await asyncio.sleep(0)
        return [_series(intent.parameters["country"])]

    monkeypatch.setattr(svc, "_fetch_data", fetch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute(svc, _eight_members()))
    assert not saved


def test_unexpected_worker_error_cancels_and_awaits_siblings(service, monkeypatch):
    svc, saved = service
    cancelled = []
    started = []

    async def run():
        blocked = asyncio.Event()

        async def fetch(intent):
            country = intent.parameters["country"]
            started.append(country)
            if country == "PE":
                await asyncio.sleep(0.01)
                return [_series(country)]
            try:
                await blocked.wait()
            finally:
                cancelled.append(country)

        def rerank(query, data):
            raise RuntimeError("unexpected post-fetch error")

        monkeypatch.setattr(svc, "_fetch_data", fetch)
        monkeypatch.setattr(svc, "_rerank_data_by_query_relevance", rerank)
        with pytest.raises(ExceptionGroup):
            await execute(svc, _eight_members())

    asyncio.run(run())
    assert {"CO", "BR", "MX"} <= set(cancelled)
    assert set(cancelled) == set(started) - {"PE"}
    assert not saved
