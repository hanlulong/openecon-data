from __future__ import annotations

import logging
from typing import Dict, Optional

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from .base import BaseProvider

logger = logging.getLogger(__name__)


class ExchangeRateProvider(BaseProvider):
    """ExchangeRate-API provider for currency exchange rates.

    Supports both free open access and API key access.
    Free tier: ~1,500 requests/month, daily updates.
    Documentation: https://www.exchangerate-api.com/docs
    """

    CURRENCY_MAPPINGS: Dict[str, str] = {
        "DOLLAR": "USD",
        "EURO": "EUR",
        "POUND": "GBP",
        "YEN": "JPY",
        "YUAN": "CNY",
        "FRANC": "CHF",
        "RUPEE": "INR",
        "WON": "KRW",
        "REAL": "BRL",
        "RUBLE": "RUB",
        "PESO": "MXN",
        "RAND": "ZAR",
        "LIRA": "TRY",
    }

    @property
    def provider_name(self) -> str:
        return "ExchangeRate"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        settings = get_settings()
        self.api_key = api_key
        # Use authenticated endpoint if API key provided, otherwise open access
        if api_key:
            self.base_url = f"https://v6.exchangerate-api.com/v6/{api_key}"
        else:
            self.base_url = settings.exchangerate_base_url.rstrip("/")

    async def _fetch_data(self, **params) -> NormalizedData:
        """Implement BaseProvider interface by routing to fetch_exchange_rate."""
        return await self.fetch_exchange_rate(
            base_currency=params.get("base_currency", "USD"),
            target_currency=params.get("target_currency"),
            target_currencies=params.get("target_currencies"),
        )

    def _currency_code(self, currency: str) -> str:
        """Get currency code from common currency name."""
        key = currency.upper().replace(" ", "_")
        return self.CURRENCY_MAPPINGS.get(key, currency.upper())

    async def fetch_exchange_rate(
        self,
        base_currency: str = "USD",
        target_currency: Optional[str] = None,
        target_currencies: Optional[list[str]] = None,
    ) -> NormalizedData:
        """Fetch exchange rates from ExchangeRate-API.

        Args:
            base_currency: Base currency code (e.g., "USD", "EUR")
            target_currency: Single target currency code (optional)
            target_currencies: List of target currency codes (optional)

        Returns:
            NormalizedData object with exchange rates
        """
        base_code = self._currency_code(base_currency)

        logger.info(f"🔍 ExchangeRate: Fetching rates for {base_code}")
        logger.info(f"   - target_currency: {target_currency}")
        logger.info(f"   - target_currencies: {target_currencies}")
        logger.info(f"   - API URL: {self.base_url}/latest/{base_code}")

        try:
            # Use shared HTTP client pool for better performance
            client = get_http_client()
            full_url = f"{self.base_url}/latest/{base_code}"
            logger.info(f"📡 Requesting: {full_url}")
            response = await client.get(full_url, timeout=15.0)
            logger.info(f"📊 Response status: {response.status_code}")
            response.raise_for_status()
            data = response.json()
            logger.info(f"✅ Response received. Result: {data.get('result')}, Rates count: {len(data.get('rates', {}))}")

            if data.get("result") != "success":
                error_msg = data.get('error-type', 'Unknown error')
                logger.error(f"ExchangeRate-API error: {error_msg}")
                raise DataNotAvailableError(f"ExchangeRate API returned error: {error_msg}")

        except httpx.HTTPStatusError as e:
            logger.error(f"ExchangeRate-API HTTP error {e.response.status_code}: {e.response.text}")
            if e.response.status_code == 429:
                raise DataNotAvailableError("ExchangeRate API rate limit exceeded. Please try again later.")
            elif e.response.status_code == 401:
                raise DataNotAvailableError("Invalid ExchangeRate API key configuration.")
            raise DataNotAvailableError(f"ExchangeRate API error: HTTP {e.response.status_code}")
        except httpx.TimeoutException:
            logger.error("ExchangeRate-API timeout")
            raise DataNotAvailableError("ExchangeRate API request timed out. Please try again.")
        except DataNotAvailableError:
            raise  # Re-raise our custom exceptions
        except Exception as e:
            logger.error(f"Unexpected error fetching exchange rates: {str(e)}")
            raise DataNotAvailableError(f"Failed to fetch exchange rates: {str(e)}")

        rates = data.get("rates", {})
        time_last_update = data.get("time_last_update_utc", "")

        logger.info(f"📈 Processing {len(rates)} exchange rates")
        logger.info(f"   - Last update: {time_last_update}")

        # If specific target currencies list provided
        if target_currencies:
            logger.info(f"🎯 Target currencies mode: {target_currencies}")
            target_codes = [self._currency_code(curr) for curr in target_currencies]
            data_points = []

            for currency in target_codes:
                if currency in rates:
                    logger.info(f"   ✅ Found rate for {currency}: {rates[currency]}")
                    data_points.append({
                        "date": f"{currency}",  # Use currency code as "date" for categorical display
                        "value": rates[currency]
                    })
                else:
                    logger.warning(f"   ❌ Missing rate for {currency}")

            if not data_points:
                logger.error(f"❌ None of the target currencies found in rates")
                raise DataNotAvailableError(f"None of the target currencies found in rates")

            logger.info(f"✅ Returning {len(data_points)} currency rates")
            indicator_name = f"{base_code} exchange rates"
            frequency = "categorical"  # Multi-currency data is categorical, not time series

        # If single target currency specified, filter to just that rate
        elif target_currency:
            logger.info(f"🎯 Single target currency mode: {target_currency}")
            target_code = self._currency_code(target_currency)
            if target_code not in rates:
                logger.error(f"❌ Target currency {target_code} not found in rates")
                logger.info(f"   Available currencies: {list(rates.keys())[:20]}...")
                raise DataNotAvailableError(f"Target currency {target_code} not found in rates")

            logger.info(f"✅ Found rate: {base_code} -> {target_code} = {rates[target_code]}")

            # Return single exchange rate as a time series with one point
            if time_last_update:
                # Convert from "Sun, 19 Oct 2025 00:02:31 +0000" to ISO 8601
                from datetime import datetime
                try:
                    dt = datetime.strptime(time_last_update, "%a, %d %b %Y %H:%M:%S %z")
                    date_str = dt.strftime("%Y-%m-%d")
                except (ValueError, AttributeError):
                    # Fallback if parsing fails
                    date_str = "2025-01-01"
            else:
                date_str = "2025-01-01"

            data_points = [{
                "date": date_str,
                "value": rates[target_code]
            }]

            logger.info(f"📅 Date: {date_str}, Value: {rates[target_code]}")
            indicator_name = f"{base_code} to {target_code}"
            frequency = "daily"  # Single currency is time series data
        else:
            logger.info(f"🌍 All currencies mode (major currencies)")
            # Return all exchange rates
            # Convert to data points format (showing top 20 major currencies)
            major_currencies = ["EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD",
                               "NZD", "SEK", "NOK", "DKK", "INR", "BRL", "MXN",
                               "ZAR", "KRW", "SGD", "HKD", "RUB", "TRY"]

            data_points = [
                {
                    "date": f"{currency}",  # Use currency code as "date" for categorical display
                    "value": rates[currency]
                }
                for currency in major_currencies if currency in rates
            ]

            logger.info(f"✅ Returning {len(data_points)} major currency rates")
            indicator_name = f"{base_code} exchange rates"
            frequency = "categorical"  # Multi-currency data is categorical, not time series

        metadata = Metadata(
            source="ExchangeRate-API",
            indicator=indicator_name,
            country="Global",
            frequency=frequency,
            unit="exchange rate",
            lastUpdated=time_last_update,
            apiUrl=f"{self.base_url}/latest/{base_code}",
        )

        logger.info(f"🎉 ExchangeRate: Returning {len(data_points)} data points")
        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_historical_rate(
        self,
        base_currency: str,
        target_currency: str,
        year: int,
        month: int,
        day: int,
    ) -> NormalizedData:
        """Fetch historical exchange rate for a specific date.

        Note: Historical data requires a paid API key.
        """
        if not self.api_key:
            raise DataNotAvailableError("Historical data requires an API key (paid plan)")

        base_code = self._currency_code(base_currency)
        target_code = self._currency_code(target_currency)
        date_str = f"{year}/{month:02d}/{day:02d}"

        try:
            # Use shared HTTP client pool for better performance
            client = get_http_client()
            response = await client.get(
                f"https://v6.exchangerate-api.com/v6/{self.api_key}/history/{base_code}/{year}/{month}/{day}",
                timeout=15.0
            )
            response.raise_for_status()
            data = response.json()

            if data.get("result") != "success":
                error_msg = data.get('error-type', 'Unknown error')
                logger.error(f"ExchangeRate-API error: {error_msg}")
                raise DataNotAvailableError(f"ExchangeRate API returned error: {error_msg}")

            rates = data.get("conversion_rates", {})

            if target_code not in rates:
                raise DataNotAvailableError(f"Target currency {target_code} not found for date {date_str}")

        except httpx.HTTPStatusError as e:
            logger.error(f"ExchangeRate-API HTTP error {e.response.status_code}: {e.response.text}")
            if e.response.status_code == 429:
                raise DataNotAvailableError("ExchangeRate API rate limit exceeded. Please try again later.")
            elif e.response.status_code == 401:
                raise DataNotAvailableError("Invalid ExchangeRate API key.")
            raise DataNotAvailableError(f"ExchangeRate API error: HTTP {e.response.status_code}")
        except DataNotAvailableError:
            raise  # Re-raise our custom exceptions
        except Exception as e:
            logger.error(f"Unexpected error fetching historical exchange rate: {str(e)}")
            raise DataNotAvailableError(f"Failed to fetch historical exchange rate: {str(e)}")

        metadata = Metadata(
            source="ExchangeRate-API",
            indicator=f"{base_code} to {target_code}",
            country="Global",
            frequency="daily",
            unit="exchange rate",
            lastUpdated=date_str,
            apiUrl=f"https://v6.exchangerate-api.com/v6/[API_KEY]/history/{base_code}/{year}/{month}/{day}",
        )

        data_points = [{
            "date": date_str,
            "value": rates[target_code]
        }]

        return NormalizedData(metadata=metadata, data=data_points)
