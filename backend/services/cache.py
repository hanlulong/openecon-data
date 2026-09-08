from __future__ import annotations

import threading
import time
import hashlib
import json
import copy
import sys
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pydantic import BaseModel

from ..models import NormalizedData

_MODEL_STORAGE = (
    "__dict__", "__pydantic_fields_set__", "__pydantic_extra__", "__pydantic_private__",
)


@dataclass
class CacheEntry:
    value: Any
    expires_at: float
    estimated_bytes: int


def _estimate_payload_bytes(value: Any, limit: int) -> int:
    """Estimate a supported object graph without serializing or copying it.

    Shared references and cycles are counted once. Unknown objects are refused
    because their shallow size can hide arbitrarily large retained state.
    """
    seen: set[int] = set()
    checked_model_types: set[type] = set()
    total = 0

    def visit(item: Any) -> None:
        nonlocal total
        identity = id(item)
        if identity in seen:
            return
        seen.add(identity)
        kind = type(item)
        scalar = kind in (type(None), bool, int, float, complex, str, bytes, bytearray)
        container = kind in (dict, list, tuple, set, frozenset)
        model = isinstance(item, BaseModel)
        if not (scalar or container or model):
            raise ValueError("Unsupported cache payload")
        total += sys.getsizeof(item)
        if total > limit:
            raise ValueError("Cache payload exceeds admission limit")
        if kind is dict:
            for key, child in item.items():
                visit(key)
                visit(child)
        elif container:
            for child in item:
                visit(child)
        elif model:
            # Include Pydantic's instance dictionaries and bookkeeping sets;
            # do not create a second graph with model_dump(). Custom slots
            # or custom copies could hide storage or return the original
            # mutable object, so do not admit those models.
            if kind not in checked_model_types:
                if kind.__deepcopy__ is not BaseModel.__deepcopy__:
                    raise ValueError("Unsupported cache model copy behavior")
                for cls in kind.__mro__:
                    declared = cls.__dict__.get("__slots__", ())
                    if isinstance(declared, str):
                        declared = (declared,)
                    if any(name not in _MODEL_STORAGE for name in declared):
                        raise ValueError("Unsupported cache model storage")
                checked_model_types.add(kind)
            for name in _MODEL_STORAGE:
                visit(getattr(item, name, None))

    visit(value)
    return total


class CacheService:
    """
    In-memory cache with TTL tracking and bounded, privately owned payloads.

    Capacity eviction removes the oldest write, not the least-recently read
    entry. Byte accounting estimates keys and supported payload graphs; it
    excludes cache bookkeeping, temporary copies and allocator/OS overhead.
    Each cache instance has its own budget, not a process-wide RSS limit.
    """

    DEFAULT_TTL = 3600  # 1 hour
    DAILY_DATA_TTL = 3600  # 1 hour
    MONTHLY_DATA_TTL = 43200  # 12 hours
    QUARTERLY_DATA_TTL = 86400  # 24 hours
    MAX_CACHE_ENTRIES = 10000  # Prevent unbounded growth
    MAX_CACHE_BYTES = 128 * 1024 * 1024
    MAX_ENTRY_BYTES = 16 * 1024 * 1024
    CLEANUP_INTERVAL = 300  # Clean expired entries every 5 minutes

    def __init__(
        self,
        *,
        max_entries: int | None = None,
        max_bytes: int | None = None,
        max_entry_bytes: int | None = None,
    ) -> None:
        self.max_entries = self.MAX_CACHE_ENTRIES if max_entries is None else max_entries
        self.max_bytes = self.MAX_CACHE_BYTES if max_bytes is None else max_bytes
        self.max_entry_bytes = self.MAX_ENTRY_BYTES if max_entry_bytes is None else max_entry_bytes
        for value in (self.max_entries, self.max_bytes, self.max_entry_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Cache limits must be positive integers")
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._estimated_bytes = 0
        self.evictions = 0
        self.rejections = 0
        self.read_copy_failures = 0
        self.hits = 0
        self.misses = 0
        self._last_cleanup = time.time()

    @staticmethod
    def _normalize_params(params: Dict[str, Any]) -> str:
        """
        Normalize parameters to canonical form for cache key.

        Sorts parameters alphabetically and handles nested structures
        to ensure consistent hashing regardless of parameter order.

        Performance: O(n log n) where n = number of params (typically < 10)
        """
        try:
            # Convert to JSON to handle nested structures uniformly
            # Sort keys to ensure consistent ordering
            json_str = json.dumps(params, sort_keys=True, separators=(',', ':'), default=str)
            # Use MD5 hash for compact representation
            return hashlib.md5(json_str.encode()).hexdigest()
        except (TypeError, ValueError):
            # Fallback for non-serializable objects
            sorted_items = sorted(params.items())
            return hashlib.md5(str(sorted_items).encode()).hexdigest()

    def _key(self, prefix: str, params: Dict[str, Any]) -> str:
        return f"{prefix}:{self._normalize_params(params)}"

    def _ttl_for_frequency(self, frequency: str) -> int:
        freq = frequency.lower()
        if freq == "daily":
            return self.DAILY_DATA_TTL
        if freq == "monthly":
            return self.MONTHLY_DATA_TTL
        if freq in {"quarterly", "annual"}:
            return self.QUARTERLY_DATA_TTL
        return self.DEFAULT_TTL

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        # Reject before allocating an expensive copy, then measure the owned
        # graph too: copying or concurrent caller mutation can change its size.
        try:
            if type(key) is not str:
                raise ValueError("Cache keys must be strings")
            key_bytes = sys.getsizeof(key)
            limit = min(self.max_entry_bytes, self.max_bytes) - key_bytes
            _estimate_payload_bytes(value, limit)
            value = self._copy_data(value)
            estimated_bytes = key_bytes + _estimate_payload_bytes(value, limit)
        except Exception:
            # Caching is best effort. Preserve any previous entry and never
            # turn an unsupported/oversized result or copy failure into a
            # provider/query failure. Do not log potentially sensitive values.
            with self._lock:
                self.rejections += 1
            return
        expiry = time.time() + (ttl or self.DEFAULT_TTL)
        with self._lock:
            self._remove(key)
            while self._cache and (
                len(self._cache) >= self.max_entries
                or self._estimated_bytes + estimated_bytes > self.max_bytes
            ):
                self._remove(next(iter(self._cache)))
                self.evictions += 1
            self._cache[key] = CacheEntry(value, expiry, estimated_bytes)
            self._estimated_bytes += estimated_bytes

    def get(self, key: str) -> Any | None:
        with self._lock:
            # Trigger cleanup if interval elapsed
            self._maybe_cleanup()

            entry = self._cache.get(key)
            if not entry:
                self.misses += 1
                return None
            if entry.expires_at < time.time():
                self._remove(key)
                self.misses += 1
                return None
            value = entry.value
        # Values are never mutated while owned by this cache, so expensive
        # copies can happen outside the lock even if the entry is evicted.
        return self._copy_for_read(value, count_request=True)

    def get_stale(self, key: str) -> Any | None:
        """Return cached value even if expired (stale-while-revalidate).

        Used as a fallback when the primary data provider is down.
        A 1-hour-old GDP dataset is infinitely more useful than
        'No Data Available' during a transient API outage.
        """
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            value = entry.value
        return self._copy_for_read(value, count_request=False)

    def _copy_for_read(self, value: Any, *, count_request: bool) -> Any | None:
        try:
            result = self._copy_data(value)
        except Exception:
            with self._lock:
                self.read_copy_failures += 1
                if count_request:
                    self.misses += 1
            return None
        if count_request:
            with self._lock:
                self.hits += 1
        return result

    def get_data_stale(self, provider: str, params: dict) -> Any | None:
        """Get cached data even if expired, for provider failure fallback."""
        key = self._key(provider, params)
        return self.get_stale(key)

    def _remove(self, key: str) -> bool:
        """Remove one entry and its accounting (must hold the lock)."""
        entry = self._cache.pop(key, None)
        if entry is None:
            return False
        self._estimated_bytes -= entry.estimated_bytes
        return True

    def delete(self, key: str) -> bool:
        """Delete a raw cache key. Returns True when key existed."""
        with self._lock:
            return self._remove(key)

    def _maybe_cleanup(self) -> None:
        """Cleanup expired entries if interval elapsed (must hold lock)."""
        now = time.time()
        if now - self._last_cleanup < self.CLEANUP_INTERVAL:
            return

        # Remove expired entries
        expired_keys = [
            k for k, v in self._cache.items()
            if v.expires_at < now
        ]
        for key in expired_keys:
            self._remove(key)

        self._last_cleanup = now

    def cache_data(
        self,
        provider: str,
        params: Dict[str, Any],
        data: NormalizedData | list[NormalizedData],
        ttl: Optional[int] = None,
    ) -> None:
        key = self._key(provider, params)

        # An explicit TTL wins: the query-service write path computes ONE
        # coherent TTL for both the Redis and in-memory copies (previously the
        # memory backup used a frequency-keyed TTL up to 12x longer than the
        # provider-keyed Redis TTL, so after Redis expiry the read path fell
        # through to a much staler memory copy).
        if ttl is None:
            # Calculate TTL based on data frequency
            ttl = self.DEFAULT_TTL
            if isinstance(data, list) and data:
                ttl = self._ttl_for_frequency(data[0].metadata.frequency)
            elif isinstance(data, NormalizedData):
                ttl = self._ttl_for_frequency(data.metadata.frequency)

        # Set with calculated TTL (atomic operation within set method)
        self.set(key, data, ttl)

    @staticmethod
    def _copy_data(value: Any) -> Any:
        """Own both sides of the cache boundary, including raw metadata.

        Copy the whole graph once so nested models, shared references and
        cycles retain their shape without exposing the cache's mutable state.
        """
        return copy.deepcopy(value)

    def get_data(self, provider: str, params: Dict[str, Any]) -> NormalizedData | list[NormalizedData] | None:
        key = self._key(provider, params)
        return self.get(key)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._estimated_bytes = 0
            self.evictions = 0
            self.rejections = 0
            self.read_copy_failures = 0
            self.hits = 0
            self.misses = 0
            self._last_cleanup = time.time()

    def get_stats(self) -> Dict[str, int | float]:
        with self._lock:
            total_requests = self.hits + self.misses
            hit_rate = (self.hits / total_requests * 100) if total_requests > 0 else 0
            return {
                "keys": len(self._cache),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(hit_rate, 2),
                "ksize": len(self._cache),
                "vsize": len(self._cache),
                "max_entries": self.max_entries,
                "estimated_bytes": self._estimated_bytes,
                "max_bytes": self.max_bytes,
                "max_entry_bytes": self.max_entry_bytes,
                "evictions": self.evictions,
                "rejections": self.rejections,
                "read_copy_failures": self.read_copy_failures,
            }

    def stats(self) -> Dict[str, int | float]:
        """Backward-compatible alias used by RedisCacheService."""
        return self.get_stats()


cache_service = CacheService()
