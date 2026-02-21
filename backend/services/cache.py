from __future__ import annotations

import threading
import time
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..models import NormalizedData


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class CacheService:
    """
    In-memory cache with TTL tracking and hit/miss statistics.

    Optimizations:
    - Normalized cache keys for consistent hashing
    - O(1) lookup performance
    - Automatic expiration cleanup
    - Thread-safe operations
    """

    DEFAULT_TTL = 3600  # 1 hour
    DAILY_DATA_TTL = 3600  # 1 hour
    MONTHLY_DATA_TTL = 43200  # 12 hours
    QUARTERLY_DATA_TTL = 86400  # 24 hours
    MAX_CACHE_ENTRIES = 10000  # Prevent unbounded growth
    CLEANUP_INTERVAL = 300  # Clean expired entries every 5 minutes

    def __init__(self) -> None:
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
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
        expiry = time.time() + (ttl or self.DEFAULT_TTL)
        with self._lock:
            self._cache[key] = CacheEntry(value=value, expires_at=expiry)

    def get(self, key: str) -> Any | None:
        with self._lock:
            # Trigger cleanup if interval elapsed
            self._maybe_cleanup()

            entry = self._cache.get(key)
            if not entry:
                self.misses += 1
                return None
            if entry.expires_at < time.time():
                self._cache.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return entry.value

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
            self._cache.pop(key, None)

        # If cache exceeds limit, remove oldest entries (simple LRU)
        if len(self._cache) > self.MAX_CACHE_ENTRIES:
            # Remove oldest 10% of entries by expiration time
            to_remove = max(10, len(self._cache) // 10)
            sorted_by_expiry = sorted(
                self._cache.items(),
                key=lambda x: x[1].expires_at
            )
            for key, _ in sorted_by_expiry[:to_remove]:
                self._cache.pop(key, None)

        self._last_cleanup = now

    def cache_data(self, provider: str, params: Dict[str, Any], data: NormalizedData | list[NormalizedData]) -> None:
        key = self._key(provider, params)

        # Calculate TTL based on data frequency
        ttl = self.DEFAULT_TTL
        if isinstance(data, list) and data:
            ttl = self._ttl_for_frequency(data[0].metadata.frequency)
        elif isinstance(data, NormalizedData):
            ttl = self._ttl_for_frequency(data.metadata.frequency)

        # Set with calculated TTL (atomic operation within set method)
        self.set(key, data, ttl)

    def get_data(self, provider: str, params: Dict[str, Any]) -> NormalizedData | list[NormalizedData] | None:
        key = self._key(provider, params)
        return self.get(key)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
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
                "max_entries": self.MAX_CACHE_ENTRIES,
            }


cache_service = CacheService()

