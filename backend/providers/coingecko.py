"""
CoinGecko API Provider for cryptocurrency and blockchain data.

Supports:
- Real-time cryptocurrency prices
- Historical market data
- On-chain token data
- NFT collections
- Market analysis across 200+ blockchains
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import asyncio
import httpx

from ..models import NormalizedData, DataPoint, Metadata
from ..services.http_pool import get_http_client
from ..utils.retry import DataNotAvailableError
from .base import BaseProvider

logger = logging.getLogger(__name__)


class CoinGeckoProvider(BaseProvider):
    """Provider for CoinGecko cryptocurrency and blockchain data."""

    BASE_URL_FREE = "https://api.coingecko.com/api/v3"
    BASE_URL_PRO = "https://pro-api.coingecko.com/api/v3"

    @property
    def provider_name(self) -> str:
        return "CoinGecko"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        """
        Initialize CoinGecko provider.

        Args:
            api_key: CoinGecko API key (optional for free tier, Demo or Pro for authenticated access)
            timeout: Request timeout in seconds
        """
        super().__init__(timeout=timeout)
        self.api_key = api_key
        # CoinGecko API key types:
        # - Demo keys: start with "CG-" and use x_cg_demo_api_key parameter
        # - Pro keys: longer format and use x_cg_pro_api_key parameter with Pro URL
        # - Free tier: no key needed, but lower rate limits
        self.is_demo = bool(api_key and api_key.startswith("CG-"))
        self.is_pro = bool(api_key and not api_key.startswith("CG-") and len(api_key) > 30)

        # Demo keys use free URL, Pro keys use Pro URL
        self.base_url = self.BASE_URL_PRO if self.is_pro else self.BASE_URL_FREE

    async def _fetch_data(self, **params) -> List[NormalizedData]:
        """Implement BaseProvider interface by routing to appropriate method."""
        coin_ids = params.get("coin_ids", ["bitcoin"])
        vs_currency = params.get("vs_currency", "usd")
        days = params.get("days")

        if days:
            # Historical data
            return await self.get_historical_data(
                coin_id=coin_ids[0] if isinstance(coin_ids, list) else coin_ids,
                vs_currency=vs_currency,
                days=days,
            )
        else:
            # Current price
            return await self.get_simple_price(
                coin_ids=coin_ids if isinstance(coin_ids, list) else [coin_ids],
                vs_currency=vs_currency,
            )

    def _build_url(self, endpoint: str, params: Dict[str, Any]) -> str:
        """Build URL with API key if available."""
        # Add appropriate API key parameter based on key type
        if self.api_key:
            if self.is_demo:
                params["x_cg_demo_api_key"] = self.api_key
            elif self.is_pro:
                params["x_cg_pro_api_key"] = self.api_key
        return f"{self.base_url}/{endpoint}"

    async def _make_request_with_retry(
        self,
        endpoint: str,
        params: Dict[str, Any],
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Make a request to CoinGecko API with exponential backoff retry logic.
        Handles rate limiting (429) gracefully.
        """
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        for attempt in range(max_retries):
            # Build URL with current API key state (may change if key is invalid)
            request_params = params.copy()
            url = self._build_url(endpoint, request_params)
            try:
                response = await client.get(url, params=request_params, timeout=30.0)

                # Handle rate limiting
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        # Exponential backoff: wait 1, 2, 4 seconds
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"⚠️  CoinGecko rate limit hit. Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise DataNotAvailableError(
                            "CoinGecko rate limit exceeded. Please try again in a few minutes."
                        )

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if status_code == 429:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(f"⚠️  CoinGecko rate limit hit (429). Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise DataNotAvailableError("CoinGecko rate limit exceeded. Please try again in a few minutes.")
                elif status_code == 401:
                    # API key might be invalid/expired - try without key
                    if self.api_key and attempt == 0:
                        logger.warning(f"⚠️ CoinGecko API key invalid, retrying without key")
                        self.api_key = None  # Disable key for subsequent requests
                        continue
                    logger.error(f"❌ CoinGecko API error: Invalid API key")
                    raise DataNotAvailableError("CoinGecko API authentication failed")
                elif status_code >= 500:
                    # Server errors might be temporary, retry once
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(f"⚠️  CoinGecko server error ({status_code}). Retrying in {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                logger.error(f"❌ CoinGecko API error: {status_code} - {e.response.text}")
                raise DataNotAvailableError(f"CoinGecko API error: HTTP {status_code}")
            except httpx.TimeoutException:
                logger.error(f"❌ CoinGecko request timed out (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"⚠️  Retrying in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                raise DataNotAvailableError("CoinGecko request timed out after multiple retries")
            except DataNotAvailableError:
                raise  # Re-raise our custom exceptions
            except Exception as e:
                logger.error(f"❌ CoinGecko request failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"⚠️  Retrying in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                raise DataNotAvailableError(f"CoinGecko request failed: {str(e)}")

        raise DataNotAvailableError("CoinGecko request failed after retries")

    async def get_simple_price(
        self,
        coin_ids: List[str],
        vs_currency: str = "usd",
        include_24h_change: bool = True,
        include_market_cap: bool = True,
        include_volume: bool = True,
        metric: str = "price",  # "price", "volume", "market_cap", "24h_change"
    ) -> List[NormalizedData]:
        """
        Get current prices/volume/market cap for cryptocurrencies.

        Args:
            coin_ids: List of coin IDs (e.g., ['bitcoin', 'ethereum'])
            vs_currency: Target currency (default: 'usd')
            include_24h_change: Include 24h price change percentage
            include_market_cap: Include market cap
            include_volume: Include 24h volume
            metric: Which metric to extract ("price", "volume", "market_cap", "24h_change")

        Returns:
            List of normalized data for the requested metric
        """
        params = {
            "ids": ",".join(coin_ids),
            "vs_currencies": vs_currency,
            "include_24hr_change": str(include_24h_change).lower(),
            "include_market_cap": str(include_market_cap).lower(),
            "include_24hr_vol": str(include_volume).lower(),
        }

        data = await self._make_request_with_retry("simple/price", params)

        logger.info(f"✅ CoinGecko: Retrieved data for {len(data)} coins (metric: {metric})")

        result = []
        current_time = datetime.now().isoformat()

        # Map metric names to API response keys
        metric_mapping = {
            "price": (vs_currency, "Price", vs_currency.upper()),
            "volume": (f"{vs_currency}_24h_vol", "24h Trading Volume", vs_currency.upper()),
            "market_cap": (f"{vs_currency}_market_cap", "Market Cap", vs_currency.upper()),
            "24h_change": (f"{vs_currency}_24h_change", "24h Price Change", "percent"),
        }

        metric_lower = metric.lower()
        if metric_lower not in metric_mapping:
            logger.warning(f"⚠️ Unknown metric '{metric}', defaulting to 'price'")
            metric_lower = "price"

        api_key, indicator_suffix, unit = metric_mapping[metric_lower]

        for coin_id, coin_data in data.items():
            if api_key not in coin_data:
                logger.warning(f"⚠️ Metric '{api_key}' not found for {coin_id}")
                continue

            data_points = [
                DataPoint(date=current_time, value=float(coin_data[api_key]))
            ]

            # Build coin-specific API URL
            api_url = f"{self.base_url}/simple/price?ids={coin_id}&vs_currencies={vs_currency}"

            metadata = Metadata(
                source="CoinGecko",
                indicator=f"{coin_id.title()} {indicator_suffix}",
                country=None,
                frequency="real-time",
                unit=unit,
                lastUpdated=current_time,
                seriesId=coin_id,
                apiUrl=api_url,
            )

            result.append(
                NormalizedData(metadata=metadata, data=data_points)
            )

        return result

    async def get_historical_data(
        self,
        coin_id: str,
        vs_currency: str = "usd",
        days: int = 30,
        interval: Optional[str] = None,
        metric: str = "price",  # "price", "market_cap", "volume"
    ) -> List[NormalizedData]:
        """
        Get historical market data for a cryptocurrency.

        Args:
            coin_id: Coin ID (e.g., 'bitcoin')
            vs_currency: Target currency (default: 'usd')
            days: Number of days of data (1, 7, 14, 30, 90, 180, 365, or 'max')
                  Note: Free tier is limited to 365 days. Pro API key required for more.
            interval: Data interval ('daily' or 'hourly', auto-determined if None)
            metric: Which metric to extract ("price", "market_cap", "volume")

        Returns:
            List of normalized historical data
        """
        logger.info(f"🔍 CoinGecko: Fetching historical data for {coin_id}")
        logger.info(f"   - vs_currency: {vs_currency}")
        logger.info(f"   - days: {days}")
        logger.info(f"   - interval: {interval}")
        logger.info(f"   - metric: {metric}")
        logger.info(f"   - key_type: {'demo' if self.is_demo else 'pro' if self.is_pro else 'free'}")

        # CoinGecko free/demo tier has a 365-day limit for historical data
        # Pro tier doesn't have this limit
        if not self.is_pro and days > 365:
            logger.warning(f"⚠️ CoinGecko: Free tier limited to 365 days, requested {days} days. Capping at 365.")
            days = 365
        params = {
            "vs_currency": vs_currency,
            "days": str(days),
        }

        if interval:
            params["interval"] = interval

        endpoint = f"coins/{coin_id}/market_chart"
        logger.info(f"📡 Requesting: {self.base_url}/{endpoint} with params: {params}")

        data = await self._make_request_with_retry(endpoint, params)

        # Select the appropriate data based on metric
        metric_key_mapping = {
            "price": "prices",
            "market_cap": "market_caps",
            "volume": "total_volumes",
        }
        metric_key = metric_key_mapping.get(metric.lower(), "prices")

        logger.info(f"✅ CoinGecko: Retrieved {len(data.get(metric_key, []))} historical {metric} points for {coin_id}")

        # Build API URL with query parameters for reproducibility
        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        api_url = f"{self.base_url}/{endpoint}?{param_str}"

        # Convert timestamps to ISO format and create data points
        data_points = []
        for timestamp, value in data.get(metric_key, []):
            date_str = datetime.fromtimestamp(timestamp / 1000).isoformat()
            data_points.append(DataPoint(date=date_str, value=float(value)))

        logger.info(f"📊 Created {len(data_points)} data points for {metric}")

        # Determine frequency
        if days == 1:
            frequency = "5-minute"
        elif days <= 7:
            frequency = "hourly"
        else:
            frequency = "daily"

        # Determine indicator name and unit based on metric
        metric_display_mapping = {
            "price": (f"{coin_id.title()} Price", vs_currency.upper()),
            "market_cap": (f"{coin_id.title()} Market Cap", vs_currency.upper()),
            "volume": (f"{coin_id.title()} 24h Volume", vs_currency.upper()),
        }
        indicator_name, unit = metric_display_mapping.get(metric.lower(), (f"{coin_id.title()} Price", vs_currency.upper()))

        metadata = Metadata(
            source="CoinGecko",
            indicator=indicator_name,
            country=None,
            frequency=frequency,
            unit=unit,
            lastUpdated=datetime.now().isoformat(),
            seriesId=coin_id,
            apiUrl=api_url,
        )

        return [NormalizedData(metadata=metadata, data=data_points)]

    async def get_historical_data_range(
        self,
        coin_id: str,
        vs_currency: str,
        from_date: str,
        to_date: str,
        metric: str = "price",  # "price", "market_cap", "volume"
    ) -> List[NormalizedData]:
        """
        Get historical market data for a specific date range.

        Args:
            coin_id: Coin ID (e.g., 'bitcoin')
            vs_currency: Target currency
            from_date: Start date (YYYY-MM-DD or Unix timestamp)
            to_date: End date (YYYY-MM-DD or Unix timestamp)
            metric: Which metric to extract ("price", "market_cap", "volume")

        Returns:
            List of normalized historical data
        """
        logger.info(f"🔍 CoinGecko: Fetching historical data range for {coin_id} ({from_date} to {to_date})")
        logger.info(f"   - metric: {metric}")

        # Convert dates to Unix timestamps if needed
        if "-" in from_date:
            from_timestamp = int(datetime.fromisoformat(from_date).timestamp())
        else:
            from_timestamp = int(from_date)

        if "-" in to_date:
            to_timestamp = int(datetime.fromisoformat(to_date).timestamp())
        else:
            to_timestamp = int(to_date)

        # CoinGecko free/demo tier has a 365-day limit for historical data
        # Pro tier doesn't have this limit
        now_timestamp = int(datetime.now().timestamp())
        max_days_ago = 365 * 24 * 60 * 60  # 365 days in seconds
        min_allowed_timestamp = now_timestamp - max_days_ago

        if not self.is_pro and from_timestamp < min_allowed_timestamp:
            logger.warning(f"⚠️ CoinGecko: Free tier limited to 365 days. Adjusting from_date.")
            from_timestamp = min_allowed_timestamp
            logger.info(f"   - Adjusted from_timestamp: {datetime.fromtimestamp(from_timestamp).isoformat()}")

        params = {
            "vs_currency": vs_currency,
            "from": str(from_timestamp),
            "to": str(to_timestamp),
        }

        endpoint = f"coins/{coin_id}/market_chart/range"
        data = await self._make_request_with_retry(endpoint, params)

        # Select the appropriate data based on metric
        metric_key_mapping = {
            "price": "prices",
            "market_cap": "market_caps",
            "volume": "total_volumes",
        }
        metric_key = metric_key_mapping.get(metric.lower(), "prices")

        logger.info(f"✅ CoinGecko: Retrieved {len(data.get(metric_key, []))} {metric} data points for {coin_id} ({from_date} to {to_date})")

        # Build API URL with query parameters for reproducibility
        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        api_url = f"{self.base_url}/{endpoint}?{param_str}"

        # Convert timestamps to ISO format and create data points
        data_points = []
        for timestamp, value in data.get(metric_key, []):
            date_str = datetime.fromtimestamp(timestamp / 1000).isoformat()
            data_points.append(DataPoint(date=date_str, value=float(value)))

        # Determine frequency based on date range
        days_diff = (to_timestamp - from_timestamp) / 86400
        if days_diff <= 1:
            frequency = "5-minute"
        elif days_diff <= 7:
            frequency = "hourly"
        else:
            frequency = "daily"

        # Determine indicator name and unit based on metric
        metric_display_mapping = {
            "price": (f"{coin_id.title()} Price", vs_currency.upper()),
            "market_cap": (f"{coin_id.title()} Market Cap", vs_currency.upper()),
            "volume": (f"{coin_id.title()} 24h Volume", vs_currency.upper()),
        }
        indicator_name, unit = metric_display_mapping.get(metric.lower(), (f"{coin_id.title()} Price", vs_currency.upper()))

        metadata = Metadata(
            source="CoinGecko",
            indicator=indicator_name,
            country=None,
            frequency=frequency,
            unit=unit,
            lastUpdated=datetime.now().isoformat(),
            seriesId=coin_id,
            apiUrl=api_url,
        )

        return [NormalizedData(metadata=metadata, data=data_points)]

    async def get_market_data(
        self,
        vs_currency: str = "usd",
        coin_ids: Optional[List[str]] = None,
        order: str = "market_cap_desc",
        per_page: int = 100,
        page: int = 1,
        category: Optional[str] = None,
    ) -> List[NormalizedData]:
        """
        Get market data for multiple cryptocurrencies.

        Args:
            vs_currency: Target currency
            coin_ids: Specific coin IDs to include (None for top coins)
            order: Sort order (market_cap_desc, volume_desc, etc.)
            per_page: Results per page (max 250)
            page: Page number
            category: Filter by category (e.g., 'decentralized-finance-defi')

        Returns:
            List of normalized market data
        """
        params = {
            "vs_currency": vs_currency,
            "order": order,
            "per_page": str(per_page),
            "page": str(page),
            "sparkline": "false",
        }

        if coin_ids:
            params["ids"] = ",".join(coin_ids)

        if category:
            params["category"] = category

        data = await self._make_request_with_retry("coins/markets", params)

        logger.info(f"✅ CoinGecko: Retrieved market data for {len(data)} coins")

        result = []
        current_time = datetime.now().isoformat()
        display_url = f"{self.base_url}/coins/markets"

        for coin in data:
            # Create data point for current price
            data_points = [
                DataPoint(date=current_time, value=float(coin.get("current_price", 0)))
            ]

            metadata = Metadata(
                source="CoinGecko",
                indicator=f"{coin['name']} ({coin['symbol'].upper()}) Market Price",
                country=None,
                frequency="real-time",
                unit=vs_currency.upper(),
                lastUpdated=current_time,
                seriesId=coin["id"],
                apiUrl=display_url,
            )

            result.append(
                NormalizedData(metadata=metadata, data=data_points)
            )

        return result
