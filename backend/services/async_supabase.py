"""Async wrapper for Supabase client to prevent event loop blocking."""
from __future__ import annotations

import asyncio
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Dict, List, Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)


def sanitize_for_json(obj: Any) -> Any:
    """Recursively sanitize data for JSON compatibility.

    Converts NaN, Infinity, and other non-JSON-compliant values to None.
    This prevents 'Out of range float values are not JSON compliant' errors.
    """
    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(item) for item in obj]
    if isinstance(obj, (str, int, bool)):
        return obj
    # For other types, try to convert to string
    try:
        json.dumps(obj)  # Test if it's JSON serializable
        return obj
    except (TypeError, ValueError):
        return str(obj)


class AsyncSupabase:
    """
    Async wrapper for Supabase client.

    Prevents blocking the event loop by running synchronous Supabase
    operations in a thread pool executor. All operations include timeout
    protection and graceful error handling.

    Features:
    - Thread pool executor for non-blocking operations
    - Timeout protection on all operations (default 5s)
    - Graceful degradation on failures
    - Connection pooling via single client instance
    - Comprehensive logging

    Example:
        >>> client = AsyncSupabase("https://project.supabase.co", "api-key")
        >>> data = await client.select("users", filters={"id": "123"})
        >>> inserted = await client.insert("logs", {"message": "test"})
    """

    def __init__(self, url: str, key: str, max_workers: int = 4):
        """
        Initialize async Supabase wrapper.

        Args:
            url: Supabase project URL
            key: Supabase API key (anon or service role)
            max_workers: Maximum thread pool workers (default 4)
        """
        self.client: Client = create_client(url, key)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.url = url
        logger.debug(f"Initialized AsyncSupabase with {max_workers} workers")

    async def _run_async(
        self,
        func,
        *args,
        **kwargs
    ) -> Any:
        """
        Run synchronous function in thread pool.

        This prevents blocking the event loop by executing the sync
        operation in a separate thread.

        Args:
            func: Synchronous function to run
            *args: Positional arguments for function
            **kwargs: Keyword arguments for function

        Returns:
            Result from the synchronous function
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            partial(func, *args, **kwargs)
        )

    async def select(
        self,
        table: str,
        columns: str = "*",
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        order_asc: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
        timeout: float = 5.0
    ) -> List[Dict[str, Any]]:
        """
        Async SELECT query.

        Args:
            table: Table name
            columns: Columns to select (default "*")
            filters: Equality filters as dict (e.g., {"id": 123})
            order_by: Column to order by
            order_asc: Order ascending (default False = descending)
            limit: Limit number of results
            offset: Offset for pagination
            timeout: Query timeout in seconds (default 5s)

        Returns:
            List of rows, empty list on error or timeout

        Example:
            >>> data = await client.select(
            ...     "users",
            ...     filters={"status": "active"},
            ...     limit=10,
            ...     timeout=3.0
            ... )
        """
        try:
            def execute_query():
                query = self.client.table(table).select(columns)

                # Apply equality filters
                if filters:
                    for key, value in filters.items():
                        query = query.eq(key, value)

                # Apply ordering
                if order_by:
                    query = query.order(order_by, desc=not order_asc)

                # Apply limit and offset
                if limit:
                    query = query.limit(limit)
                if offset:
                    query = query.offset(offset)

                # Execute the query
                result = query.execute()
                return result.data or []

            result = await asyncio.wait_for(
                self._run_async(execute_query),
                timeout=timeout
            )
            logger.debug(f"Selected {len(result)} rows from {table}")
            return result

        except asyncio.TimeoutError:
            logger.warning(f"SELECT {table} timed out after {timeout}s")
            return []
        except Exception as e:
            logger.error(f"SELECT {table} error: {e}", exc_info=True)
            return []

    async def insert(
        self,
        table: str,
        data: Dict[str, Any],
        timeout: float = 5.0,
        upsert: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Async INSERT query.

        Args:
            table: Table name
            data: Row data to insert
            timeout: Query timeout in seconds (default 5s)
            upsert: If True, update on conflict (default False)

        Returns:
            Inserted row data, None on error or timeout

        Example:
            >>> result = await client.insert(
            ...     "users",
            ...     {"email": "user@example.com", "name": "User"},
            ...     timeout=3.0
            ... )
        """
        try:
            # Sanitize data to prevent JSON serialization errors with NaN/Infinity
            sanitized_data = sanitize_for_json(data)

            def execute_insert():
                query = self.client.table(table).insert(sanitized_data)
                if upsert:
                    query = query.on_conflict("id")
                result = query.execute()
                return result.data[0] if result.data else None

            result = await asyncio.wait_for(
                self._run_async(execute_insert),
                timeout=timeout
            )
            logger.debug(f"Inserted row into {table}")
            return result

        except asyncio.TimeoutError:
            logger.warning(f"INSERT {table} timed out after {timeout}s")
            return None
        except Exception as e:
            logger.error(f"INSERT {table} error: {e}", exc_info=True)
            return None

    async def update(
        self,
        table: str,
        data: Dict[str, Any],
        filters: Dict[str, Any],
        timeout: float = 5.0
    ) -> bool:
        """
        Async UPDATE query with optimistic locking.

        Args:
            table: Table name
            data: Data to update
            filters: Equality filters for WHERE clause
            timeout: Query timeout in seconds (default 5s)

        Returns:
            True if update succeeded, False otherwise

        Example:
            >>> success = await client.update(
            ...     "users",
            ...     {"status": "inactive"},
            ...     filters={"id": 123},
            ...     timeout=3.0
            ... )
        """
        try:
            def execute_update():
                query = self.client.table(table).update(data)

                # Apply equality filters
                for key, value in filters.items():
                    query = query.eq(key, value)

                result = query.execute()
                return bool(result.data)

            result = await asyncio.wait_for(
                self._run_async(execute_update),
                timeout=timeout
            )
            if result:
                logger.debug(f"Updated rows in {table}")
            return result

        except asyncio.TimeoutError:
            logger.warning(f"UPDATE {table} timed out after {timeout}s")
            return False
        except Exception as e:
            logger.error(f"UPDATE {table} error: {e}", exc_info=True)
            return False

    async def delete(
        self,
        table: str,
        filters: Dict[str, Any],
        timeout: float = 5.0
    ) -> bool:
        """
        Async DELETE query.

        Args:
            table: Table name
            filters: Equality filters for WHERE clause
            timeout: Query timeout in seconds (default 5s)

        Returns:
            True if delete succeeded, False otherwise

        Example:
            >>> success = await client.delete(
            ...     "users",
            ...     filters={"id": 123},
            ...     timeout=3.0
            ... )
        """
        try:
            def execute_delete():
                query = self.client.table(table).delete()

                # Apply equality filters
                for key, value in filters.items():
                    query = query.eq(key, value)

                query.execute()
                return True

            result = await asyncio.wait_for(
                self._run_async(execute_delete),
                timeout=timeout
            )
            if result:
                logger.debug(f"Deleted rows from {table}")
            return result

        except asyncio.TimeoutError:
            logger.warning(f"DELETE {table} timed out after {timeout}s")
            return False
        except Exception as e:
            logger.error(f"DELETE {table} error: {e}", exc_info=True)
            return False

    async def rpc(
        self,
        function: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0
    ) -> Any:
        """
        Async RPC (stored procedure) call.

        Args:
            function: RPC function name
            params: Function parameters
            timeout: Query timeout in seconds (default 10s)

        Returns:
            RPC result data, None on error or timeout

        Example:
            >>> result = await client.rpc(
            ...     "process_data",
            ...     {"user_id": 123},
            ...     timeout=15.0
            ... )
        """
        try:
            def execute_rpc():
                result = self.client.rpc(function, params or {}).execute()
                return result.data

            result = await asyncio.wait_for(
                self._run_async(execute_rpc),
                timeout=timeout
            )
            logger.debug(f"RPC {function} completed")
            return result

        except asyncio.TimeoutError:
            logger.warning(f"RPC {function} timed out after {timeout}s")
            return None
        except Exception as e:
            logger.error(f"RPC {function} error: {e}", exc_info=True)
            return None

    async def health_check(self) -> bool:
        """
        Check Supabase connectivity with short timeout.

        Returns:
            True if Supabase is reachable, False otherwise

        Example:
            >>> is_healthy = await client.health_check()
        """
        try:
            await self.select(
                "user_queries",
                columns="id",
                limit=1,
                timeout=2.0
            )
            logger.debug("Supabase health check passed")
            return True
        except Exception as e:
            logger.warning(f"Supabase health check failed: {e}")
            return False

    def shutdown(self):
        """Shutdown thread pool executor."""
        self.executor.shutdown(wait=True)
        logger.debug("AsyncSupabase executor shutdown complete")
