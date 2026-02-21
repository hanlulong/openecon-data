from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode


class MockURL:
    def __init__(self, url: str):
        self._url = url

    def copy_with(self, params: Optional[Dict[str, Any]] = None) -> "MockURL":
        if not params:
            return MockURL(self._url)
        base, _, _ = self._url.partition("?")
        return MockURL(f"{base}?{urlencode(params)}")

    def __str__(self) -> str:
        return self._url


class MockRequest:
    def __init__(self, url: str):
        self.url = MockURL(url)


class MockAsyncResponse:
    def __init__(
        self,
        json_data: Dict[str, Any],
        *,
        headers: Optional[Dict[str, str]] = None,
        request_url: Optional[str] = None,
        status_code: int = 200,
    ) -> None:
        self._json = json_data
        self.headers = headers or {}
        self.request = MockRequest(request_url or "https://example.com/mock")
        self.status_code = status_code
        # content is used to check if response has data
        self.content = json_data is not None

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return self._json


class MockAsyncClient:
    def __init__(self, responses: Iterable[MockAsyncResponse]) -> None:
        self._responses: List[MockAsyncResponse] = list(responses)

    async def __aenter__(self) -> "MockAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def get(self, url: str, *, params: Optional[Dict[str, Any]] = None, **_kwargs) -> MockAsyncResponse:
        if not self._responses:
            raise AssertionError("No more mock responses available")
        response = self._responses.pop(0)
        base_url = url if isinstance(url, str) else str(url)
        response.request = MockRequest(base_url)
        return response


def run(coro):
    """Helper to run async functions in synchronous tests."""
    return asyncio.run(coro)
