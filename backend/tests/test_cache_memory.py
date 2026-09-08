"""Bounded cache ownership/accounting regressions; no providers or services."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
import asyncio

import pytest
from pydantic import BaseModel, ConfigDict, PrivateAttr

from backend.models import NormalizedData
from backend.services import cache as cache_module
from backend.services.cache import CacheService
from backend.services.redis_cache import RedisCacheService


def series():
    return NormalizedData.model_validate({
        "metadata": {
            "source": "FRED", "indicator": "GDP", "country": "US",
            "frequency": "quarterly", "unit": "Billions",
        },
        "data": [{"date": "2025-01-01", "value": 100.0}],
    })


def test_write_only_workload_enforces_existing_entry_ceiling(monkeypatch):
    monkeypatch.setattr(CacheService, "MAX_CACHE_ENTRIES", 3)
    cache = CacheService()
    for number in range(5):
        cache.set(str(number), number)
        assert cache.get_stats()["keys"] <= 3
    assert cache.get("0") is None
    assert cache.get("1") is None
    assert cache.get("4") == 4


def test_raw_nested_metadata_is_owned_on_both_write_and_read():
    cache = CacheService()
    original = [{"nested": {"codes": ["GDP"]}}]
    cache.set("metadata", original)
    original[0]["nested"]["codes"].append("caller mutation")
    first = cache.get("metadata")
    assert first == [{"nested": {"codes": ["GDP"]}}]
    first[0]["nested"]["codes"].append("reader mutation")
    assert cache.get("metadata") == [{"nested": {"codes": ["GDP"]}}]


def test_raw_stale_read_cannot_mutate_retained_payload(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    cache = CacheService()
    cache.set("metadata", {"codes": ["GDP"]}, ttl=1)
    clock[0] += 2
    stale = cache.get_stale("metadata")
    stale["codes"].append("mutation")
    assert cache.get_stale("metadata") == {"codes": ["GDP"]}


def test_normalized_data_copies_preserve_shared_references_without_aliasing_cache():
    cache = CacheService()
    item = series()
    cache.cache_data("FRED", {}, [item, item])
    item.data[0].value = -1
    first = cache.get_data("FRED", {})
    assert first[0] is first[1]
    assert first[0].data[0].value == 100.0
    first[0].data[0].value = -2
    assert cache.get_data("FRED", {})[0].data[0].value == 100.0
    stale = cache.get_data_stale("FRED", {})
    stale[0].data[0].value = -3
    assert cache.get_data_stale("FRED", {})[0].data[0].value == 100.0


def test_estimated_byte_budget_evicts_oldest_write_and_counts_replacements():
    sample = CacheService()
    sample.set("a", b"x" * 1024)
    entry_size = sample.get_stats()["estimated_bytes"]
    cache = CacheService(max_bytes=entry_size * 2, max_entry_bytes=entry_size)
    for key in ("a", "b", "c"):
        cache.set(key, b"x" * 1024)
        assert cache.get_stats()["estimated_bytes"] <= entry_size * 2
    assert cache.get("a") is None
    assert cache.get_stats()["keys"] == 2
    assert cache.get_stats()["evictions"] == 1
    # A read does not reorder the documented oldest-write eviction policy.
    assert cache.get("b") is not None
    cache.set("c", b"y" * 1024)
    assert cache.get_stats()["estimated_bytes"] == entry_size * 2
    cache.set("d", b"z" * 1024)
    assert cache.get("b") is None
    assert cache.get("c") == b"y" * 1024
    assert cache.get_stats()["evictions"] == 2


def test_oversized_replacement_is_rejected_before_copy_and_preserves_old_entry(monkeypatch):
    cache = CacheService(max_bytes=2048, max_entry_bytes=1024)
    cache.set("key", "old")
    before = cache.get_stats()["estimated_bytes"]

    copied = []

    def record_copy(value):
        copied.append(value)
        return value

    with monkeypatch.context() as local:
        local.setattr(cache, "_copy_data", record_copy)
        cache.set("key", b"x" * 2048)
    assert copied == []
    assert cache.get("key") == "old"
    assert cache.get_stats()["estimated_bytes"] == before
    assert cache.get_stats()["rejections"] == 1


def test_copied_payload_is_remeasured_before_admission(monkeypatch):
    cache = CacheService(max_bytes=2048, max_entry_bytes=1024)
    cache.set("key", "old")
    with monkeypatch.context() as local:
        local.setattr(cache, "_copy_data", lambda value: b"x" * 2048)
        cache.set("key", "small input")
    assert cache.get("key") == "old"
    assert cache.get_stats()["rejections"] == 1


def test_unsupported_payload_is_not_shallow_counted_or_copied():
    copied = []

    class Opaque:
        def __init__(self):
            self.large = b"x" * 4096

        def __deepcopy__(self, memo):
            copied.append(self)
            return self

    cache = CacheService()
    cache.set("opaque", {"nested": Opaque()})
    assert copied == []
    assert cache.get("opaque") is None
    assert cache.get_stats()["rejections"] == 1
    assert cache.get_stats()["estimated_bytes"] == 0


@pytest.mark.parametrize("storage", ["extra", "private"])
def test_pydantic_extra_and_private_payloads_are_included_in_admission(storage):
    class Model(BaseModel):
        model_config = ConfigDict(extra="allow")
        _payload: bytes = PrivateAttr(default=b"")

    model = Model()
    if storage == "extra":
        model.extra_payload = b"x" * 4096
    else:
        model._payload = b"x" * 4096
    cache = CacheService(max_bytes=4096, max_entry_bytes=2048)
    cache.set("hidden", model)
    assert cache.get("hidden") is None
    assert cache.get_stats()["rejections"] == 1


def test_pydantic_custom_copy_cannot_alias_the_retained_payload():
    class UnsafeCopy(BaseModel):
        values: list[str]

        def __deepcopy__(self, memo=None):
            return self

    cache = CacheService()
    cache.set("model", UnsafeCopy(values=["GDP"]))
    assert cache.get("model") is None
    assert cache.get_stats()["rejections"] == 1


def test_copy_failure_is_best_effort_and_keeps_previous_entry(monkeypatch):
    cache = CacheService()
    cache.set("key", "old")

    def fail_copy(value):
        raise MemoryError("isolated copy allocation failure")

    with monkeypatch.context() as local:
        local.setattr(cache, "_copy_data", fail_copy)
        cache.set("key", "new")
    assert cache.get("key") == "old"
    assert cache.get_stats()["rejections"] == 1


@pytest.mark.parametrize("method", ["get", "get_stale"])
def test_read_copy_failure_is_a_miss_and_can_retry_without_exposing_cache(method, monkeypatch):
    cache = CacheService()
    cache.set("key", {"value": ["old"]})

    def fail_copy(value):
        raise MemoryError("isolated read copy allocation failure")

    with monkeypatch.context() as local:
        local.setattr(cache, "_copy_data", fail_copy)
        assert getattr(cache, method)("key") is None
    stats = cache.get_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == (1 if method == "get" else 0)
    assert stats["read_copy_failures"] == 1
    assert stats["rejections"] == 0
    assert getattr(cache, method)("key") == {"value": ["old"]}
    cache.clear()
    assert cache.get_stats()["read_copy_failures"] == 0


def test_replacement_refreshes_write_order_and_shrinks_accounting():
    cache = CacheService(max_entries=2)
    cache.set("a", b"x" * 4096)
    cache.set("b", "second")
    before = cache.get_stats()["estimated_bytes"]
    cache.set("a", "small replacement")
    assert cache.get_stats()["estimated_bytes"] < before
    cache.set("c", "third")
    assert cache.get("a") == "small replacement"
    assert cache.get("b") is None
    assert cache.get("c") == "third"
    assert cache.get_stats()["keys"] == 2


def test_long_cache_key_counts_toward_admission_budget():
    cache = CacheService(max_bytes=4096, max_entry_bytes=2048)
    cache.set("x" * 4096, 1)
    assert cache.get_stats()["keys"] == 0
    assert cache.get_stats()["rejections"] == 1


def test_cycles_and_shared_containers_do_not_hang_or_leak_owned_references():
    cache = CacheService()
    shared = {"codes": ["GDP"]}
    original = [shared, shared]
    original.append(original)
    cache.set("cycle", original)
    copy = cache.get("cycle")
    assert copy[0] is copy[1]
    assert copy[2] is copy
    copy[0]["codes"].append("mutation")
    fresh = cache.get("cycle")
    assert fresh[0]["codes"] == ["GDP"]
    assert fresh[2] is fresh
    assert cache.get_stats()["estimated_bytes"] > 0


def test_all_removal_paths_release_accounting_and_preserve_existing_ttl_behavior(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    cache = CacheService()
    cache.set("expires", ["value"], ttl=1)
    cache.set("delete", ["value"], ttl=1000)
    before = cache.get_stats()["estimated_bytes"]
    clock[0] += 2
    # Existing stale reads do not delete expired entries or trigger cleanup.
    assert cache.get_stale("expires") == ["value"]
    assert cache.get_stats()["estimated_bytes"] == before
    # Existing fresh-read expiry removes that entry, including its accounting.
    assert cache.get("expires") is None
    assert cache.get_stats()["estimated_bytes"] < before
    assert cache.delete("delete") is True
    assert cache.delete("delete") is False
    assert cache.get_stats()["estimated_bytes"] == 0
    cache.set("cleanup", ["value"], ttl=1)
    clock[0] += CacheService.CLEANUP_INTERVAL + 1
    cache.get("missing")
    assert cache.get_stats()["estimated_bytes"] == 0
    cache.set("clear", ["value"])
    cache.clear()
    assert cache.get_stats()["estimated_bytes"] == 0
    assert cache.get_stats()["keys"] == 0
    assert cache.get_stats()["evictions"] == cache.get_stats()["rejections"] == 0


def test_normal_write_does_not_eagerly_remove_available_stale_fallback(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    cache = CacheService()
    cache.set("expired", ["fallback"], ttl=1)
    clock[0] += CacheService.CLEANUP_INTERVAL + 1
    cache.set("unrelated", ["fresh"])
    assert cache.get_stale("expired") == ["fallback"]


def test_concurrent_reads_writes_and_deletes_keep_private_payloads_and_bounds():
    cache = CacheService(max_entries=8, max_bytes=16384, max_entry_bytes=2048)

    def work(worker):
        for number in range(30):
            key = f"{worker}:{number % 4}"
            cache.set(key, {"values": [worker, number]})
            value = cache.get(key)
            if value is not None:
                value["values"].append("reader mutation")
                another = cache.get(key)
                if another is not None:
                    assert "reader mutation" not in another["values"]
            if number % 3 == 0:
                cache.delete(key)
            stats = cache.get_stats()
            assert stats["keys"] <= 8
            assert 0 <= stats["estimated_bytes"] <= 16384

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(4)))
    cache.clear()
    assert cache.get_stats()["estimated_bytes"] == 0


def test_redis_fallback_owns_nested_payload_without_a_second_read_copy():
    with patch("backend.services.redis_cache.get_settings", return_value=SimpleNamespace(redis_url=None)):
        service = RedisCacheService()

    async def no_reconnect():
        return None

    service._maybe_reconnect = no_reconnect

    async def exercise():
        original = {"nested": [series()]}
        await service.set("FRED", "query", original)
        original["nested"][0].data[0].value = -1
        first = await service.get("FRED", "query")
        assert first["nested"][0].data[0].value == 100.0
        first["nested"][0].data[0].value = -2
        assert (await service.get("FRED", "query"))["nested"][0].data[0].value == 100.0

    asyncio.run(exercise())


@pytest.mark.parametrize("name", ["max_entries", "max_bytes", "max_entry_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_cache_limits_are_rejected(name, value):
    with pytest.raises(ValueError):
        CacheService(**{name: value})
