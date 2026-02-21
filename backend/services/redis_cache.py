"""
Redis caching service for econ-data-mcp.

Provides distributed caching with TTL support, automatic serialization,
and fallback to in-memory cache when Redis is unavailable.
"""

from __future__ import annotations

import json
import logging
import hashlib
from typing import Any, Optional, Dict
from datetime import timedelta
import asyncio

try:
    import redis.asyncio as redis
    from redis.asyncio.retry import Retry
    from redis.backoff import ExponentialBackoff
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

from ..config import get_settings
from .cache import CacheService  # Fallback to in-memory cache

logger = logging.getLogger(__name__)


class RedisCacheService:
    """
    Redis-based caching service with automatic fallback to in-memory cache.

    Features:
    - Distributed caching across multiple backend instances
    - TTL support with configurable expiration
    - Automatic JSON serialization/deserialization
    - Connection pooling and retry logic
    - Graceful fallback to in-memory cache when Redis is unavailable
    - Provider-specific cache namespaces
    """

    def __init__(self):
        self.settings = get_settings()
        self.redis_client: Optional[redis.Redis] = None
        self.fallback_cache = CacheService()  # In-memory fallback
        self._connected = False
        self._lock = asyncio.Lock()

        # Cache TTL configuration per provider (in seconds)
        self.ttl_config = {
            "FRED": 3600,          # 1 hour
            "WORLDBANK": 7200,     # 2 hours (less frequent updates)
            "COMTRADE": 3600,      # 1 hour
            "STATSCAN": 3600,      # 1 hour
            "IMF": 7200,           # 2 hours
            "OECD": 1800,          # 30 minutes (due to rate limits)
            "BIS": 7200,           # 2 hours
            "EUROSTAT": 3600,      # 1 hour
            "EXCHANGERATE": 300,   # 5 minutes (more volatile)
            "COINGECKO": 60,       # 1 minute (highly volatile)
            "default": 3600        # 1 hour default
        }

    async def connect(self) -> bool:
        """
        Connect to Redis server with retry logic.

        Returns:
            True if connected successfully, False otherwise
        """
        if not REDIS_AVAILABLE:
            logger.warning("Redis library not installed. Using in-memory cache.")
            return False

        if self._connected:
            return True

        async with self._lock:
            if self._connected:
                return True

            try:
                # Get Redis configuration from environment
                redis_url = getattr(self.settings, 'REDIS_URL', None) or 'redis://localhost:6379/0'

                # Create connection with retry logic
                retry = Retry(ExponentialBackoff(), 3)
                self.redis_client = await redis.from_url(
                    redis_url,
                    encoding='utf-8',
                    decode_responses=True,
                    retry=retry,
                    retry_on_timeout=True,
                    socket_keepalive=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )

                # Test connection
                await self.redis_client.ping()
                self._connected = True
                logger.info(f"✅ Connected to Redis at {redis_url}")
                return True

            except Exception as e:
                logger.warning(f"Failed to connect to Redis: {e}. Using in-memory cache.")
                self.redis_client = None
                self._connected = False
                return False

    async def disconnect(self):
        """Disconnect from Redis gracefully."""
        if self.redis_client:
            try:
                await self.redis_client.close()
                logger.info("Disconnected from Redis")
            except Exception as e:
                logger.error(f"Error disconnecting from Redis: {e}")
            finally:
                self.redis_client = None
                self._connected = False

    def _generate_key(self, provider: str, query: str, params: Optional[Dict] = None) -> str:
        """
        Generate a unique cache key for a query.

        Args:
            provider: Data provider name
            query: Query string
            params: Additional parameters

        Returns:
            Unique cache key
        """
        # Create a consistent hash of the query and parameters
        key_parts = [provider.upper(), query]
        if params:
            # Sort parameters for consistent hashing
            sorted_params = json.dumps(params, sort_keys=True)
            key_parts.append(sorted_params)

        key_string = ":".join(key_parts)
        # Use MD5 for shorter keys (Redis key limit is 512MB but shorter is better)
        key_hash = hashlib.md5(key_string.encode()).hexdigest()

        return f"openecon:{provider.lower()}:{key_hash}"

    async def get(self, provider: str, query: str, params: Optional[Dict] = None) -> Optional[Any]:
        """
        Get cached data for a query.

        Args:
            provider: Data provider name
            query: Query string
            params: Additional parameters

        Returns:
            Cached data if available, None otherwise
        """
        key = self._generate_key(provider, query, params)

        # Try Redis first
        if self._connected and self.redis_client:
            try:
                data = await self.redis_client.get(key)
                if data:
                    logger.debug(f"🎯 Redis cache hit for {provider}: {query[:50]}...")
                    return json.loads(data)
            except Exception as e:
                logger.warning(f"Redis get error: {e}. Falling back to in-memory cache.")
                self._connected = False  # Mark as disconnected for reconnection attempt

        # Fallback to in-memory cache
        data = self.fallback_cache.get(key)
        if data:
            logger.debug(f"💾 In-memory cache hit for {provider}: {query[:50]}...")
        return data

    async def set(self, provider: str, query: str, data: Any, params: Optional[Dict] = None, ttl: Optional[int] = None) -> bool:
        """
        Cache data for a query.

        Args:
            provider: Data provider name
            query: Query string
            data: Data to cache
            params: Additional parameters
            ttl: Time to live in seconds (optional, uses provider default if not specified)

        Returns:
            True if cached successfully
        """
        key = self._generate_key(provider, query, params)

        # Determine TTL
        if ttl is None:
            ttl = self.ttl_config.get(provider.upper(), self.ttl_config["default"])

        # Try Redis first
        success = False
        if self._connected and self.redis_client:
            try:
                serialized = json.dumps(data)
                await self.redis_client.setex(key, ttl, serialized)
                logger.debug(f"✅ Cached to Redis: {provider} query (TTL: {ttl}s)")
                success = True
            except Exception as e:
                logger.warning(f"Redis set error: {e}. Using in-memory cache.")
                self._connected = False

        # Always cache to in-memory as well for redundancy
        self.fallback_cache.set(key, data, ttl)

        return success

    async def delete(self, provider: str, query: str, params: Optional[Dict] = None) -> bool:
        """
        Delete cached data for a query.

        Args:
            provider: Data provider name
            query: Query string
            params: Additional parameters

        Returns:
            True if deleted successfully
        """
        key = self._generate_key(provider, query, params)

        success = False
        # Try Redis first
        if self._connected and self.redis_client:
            try:
                result = await self.redis_client.delete(key)
                success = result > 0
                if success:
                    logger.debug(f"🗑️ Deleted from Redis: {provider} query")
            except Exception as e:
                logger.warning(f"Redis delete error: {e}")

        # Also delete from in-memory cache
        self.fallback_cache.delete(key)

        return success

    async def clear_provider(self, provider: str) -> int:
        """
        Clear all cached data for a specific provider.

        Args:
            provider: Data provider name

        Returns:
            Number of keys deleted
        """
        pattern = f"openecon:{provider.lower()}:*"
        deleted = 0

        if self._connected and self.redis_client:
            try:
                # Use SCAN to avoid blocking Redis
                cursor = 0
                while True:
                    cursor, keys = await self.redis_client.scan(cursor, match=pattern, count=100)
                    if keys:
                        deleted += await self.redis_client.delete(*keys)
                    if cursor == 0:
                        break
                logger.info(f"🧹 Cleared {deleted} cache entries for {provider}")
            except Exception as e:
                logger.error(f"Error clearing Redis cache for {provider}: {e}")

        # Clear in-memory cache (approximate, as we don't track by provider)
        self.fallback_cache.clear()

        return deleted

    async def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        stats = {
            "redis_connected": self._connected,
            "in_memory_stats": self.fallback_cache.stats()
        }

        if self._connected and self.redis_client:
            try:
                info = await self.redis_client.info()
                stats["redis_stats"] = {
                    "used_memory": info.get("used_memory_human", "N/A"),
                    "connected_clients": info.get("connected_clients", 0),
                    "total_commands": info.get("total_commands_processed", 0),
                    "keyspace_hits": info.get("keyspace_hits", 0),
                    "keyspace_misses": info.get("keyspace_misses", 0),
                    "hit_rate": (
                        info.get("keyspace_hits", 0) /
                        (info.get("keyspace_hits", 0) + info.get("keyspace_misses", 1))
                    ) if info.get("keyspace_hits", 0) > 0 else 0
                }

                # Count econ-data-mcp keys
                openecon_keys = 0
                cursor = 0
                while True:
                    cursor, keys = await self.redis_client.scan(cursor, match="openecon:*", count=100)
                    openecon_keys += len(keys)
                    if cursor == 0:
                        break
                stats["redis_stats"]["openecon_keys"] = openecon_keys

            except Exception as e:
                logger.error(f"Error getting Redis stats: {e}")
                stats["redis_stats"] = {"error": str(e)}

        return stats


# Global instance
_redis_cache: Optional[RedisCacheService] = None


async def get_redis_cache() -> RedisCacheService:
    """
    Get the global Redis cache instance.

    Returns:
        RedisCacheService instance
    """
    global _redis_cache
    if _redis_cache is None:
        _redis_cache = RedisCacheService()
        await _redis_cache.connect()
    return _redis_cache


async def cleanup_redis_cache():
    """Cleanup Redis cache on shutdown."""
    global _redis_cache
    if _redis_cache:
        await _redis_cache.disconnect()
        _redis_cache = None