"""Base provider class with common HTTP retry and error handling logic."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import httpx

from ..models import NormalizedData
from ..utils.retry import DataNotAvailableError

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """Base class for all data providers.

    Provides common functionality:
    - HTTP client management with timeout
    - Retry logic for transient failures
    - Common error handling and logging
    - Rate limiting awareness
    - Standardized provider identification

    All providers should inherit from this class and implement:
    - provider_name property (required)
    - _fetch_data method (abstract)
    """

    # Default timeout (seconds)
    DEFAULT_TIMEOUT = 30.0

    # Retry configuration
    MAX_RETRIES = 3
    RETRY_BACKOFF_FACTOR = 1.0

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        """Initialize base provider.

        Args:
            timeout: Request timeout in seconds
        """
        self.timeout = timeout
        self.last_request_time = None
        self.rate_limit_reset = None

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the canonical provider name (e.g., 'FRED', 'WorldBank', 'IMF').

        This is used for logging, routing, and metadata.
        """
        pass

    @abstractmethod
    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Fetch data from provider API. Must be implemented by subclasses.

        Args:
            **params: Provider-specific parameters

        Returns:
            Normalized data or list of normalized data
        """
        pass

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """Get request with automatic retry on transient failures.

        Args:
            client: httpx AsyncClient
            url: Request URL
            **kwargs: Additional httpx parameters

        Returns:
            HTTP response

        Raises:
            DataNotAvailableError: If all retries fail
        """
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:
                response = await client.get(url, **kwargs, timeout=self.timeout)

                # Check for rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    self.rate_limit_reset = datetime.now() + timedelta(seconds=retry_after)
                    logger.warning(f"Rate limited. Retry after {retry_after}s")

                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code

                if status == 429:
                    # Rate limited - wait and retry
                    continue
                elif status in (404, 403):
                    # Not found or forbidden - don't retry
                    raise DataNotAvailableError(
                        f"API returned {status}: {e.response.text[:200]}"
                    )
                elif status >= 500:
                    # Server error - retry
                    if attempt < self.MAX_RETRIES - 1:
                        logger.warning(f"Server error {status}, retrying...")
                        continue
                    raise DataNotAvailableError(f"Server error {status} after {self.MAX_RETRIES} retries")
                else:
                    # Other client error
                    raise DataNotAvailableError(str(e))

            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout) as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(f"Connection error, retrying... (attempt {attempt + 1})")
                    continue
                raise DataNotAvailableError(f"Connection failed after {self.MAX_RETRIES} retries: {str(e)}")

            except Exception as e:
                last_error = e
                raise DataNotAvailableError(f"Request failed: {str(e)}")

        # All retries exhausted
        raise DataNotAvailableError(f"Failed after {self.MAX_RETRIES} retries: {str(last_error)}")

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """Post request with automatic retry on transient failures.

        Args:
            client: httpx AsyncClient
            url: Request URL
            **kwargs: Additional httpx parameters

        Returns:
            HTTP response

        Raises:
            DataNotAvailableError: If all retries fail
        """
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:
                response = await client.post(url, **kwargs, timeout=self.timeout)
                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code

                if status in (404, 403):
                    raise DataNotAvailableError(f"API returned {status}")
                elif status >= 500 and attempt < self.MAX_RETRIES - 1:
                    logger.warning(f"Server error {status}, retrying...")
                    continue
                elif status >= 500:
                    raise DataNotAvailableError(f"Server error {status} after retries")
                else:
                    raise DataNotAvailableError(str(e))

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(f"Connection error, retrying...")
                    continue
                raise DataNotAvailableError(f"Connection failed: {str(e)}")

        raise DataNotAvailableError(f"Failed after {self.MAX_RETRIES} retries: {str(last_error)}")

    @staticmethod
    def _normalize_country_code(country: str, mappings: Dict[str, str]) -> str:
        """Normalize country code using provided mappings.

        Args:
            country: Country name or code
            mappings: Dictionary mapping various formats to standard code

        Returns:
            Normalized country code
        """
        key = country.upper().replace(" ", "_")
        return mappings.get(key, country.upper())

    @staticmethod
    def _normalize_indicator(indicator: str, mappings: Dict[str, str]) -> Optional[str]:
        """Normalize indicator using provided mappings.

        Args:
            indicator: Indicator name or code
            mappings: Dictionary mapping indicator names to codes

        Returns:
            Normalized indicator code or None if not found
        """
        if not indicator:
            return None
        key = indicator.upper().replace(" ", "_")
        return mappings.get(key)

    @staticmethod
    def _is_rate_limited() -> bool:
        """Check if provider is currently rate limited."""
        # Can be overridden by subclasses
        return False

    @staticmethod
    def _parse_json_safe(response: httpx.Response) -> Dict[str, Any]:
        """Safely parse JSON response with error handling.

        Args:
            response: HTTP response

        Returns:
            Parsed JSON dictionary

        Raises:
            DataNotAvailableError: If JSON parsing fails
        """
        try:
            return response.json()
        except Exception as e:
            raise DataNotAvailableError(f"Failed to parse response: {str(e)}")
