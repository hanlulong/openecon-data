"""Shared default time-range helpers for provider routing/fetching."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict

logger = logging.getLogger(__name__)


def apply_default_time_range(provider: str, routing: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply smart default time ranges when user does not provide explicit dates.

    Rules:
    - Respect explicit user dates.
    - Comtrade uses last 10 years for absent or explicitly framework-default dates.
    - CoinGecko defaults to last 30 days.
    - ExchangeRate keeps no date default (current rate behavior).
    """
    provider_upper = (provider or "").upper()
    today = datetime.now()

    start_date = routing.get("startDate")
    end_date = routing.get("endDate")

    if provider_upper == "COMTRADE":
        should_apply_default = (
            (not start_date and not end_date)
            or routing.get("__time_scope_authority") == "default"
        )

        if should_apply_default:
            start_year_int = today.year - 10
            end_year_int = today.year
            routing["startDate"] = f"{start_year_int}-01-01"
            routing["endDate"] = f"{end_year_int}-12-31"
            routing["start_year"] = start_year_int
            routing["end_year"] = end_year_int
            logger.info(
                "Applied default 10-year range for Comtrade: %s to %s",
                start_year_int,
                end_year_int,
            )
            return routing

        return routing

    if start_date or end_date:
        return routing

    if provider_upper == "COINGECKO":
        start = today - timedelta(days=30)
        routing["startDate"] = start.strftime("%Y-%m-%d")
        routing["endDate"] = today.strftime("%Y-%m-%d")
        routing["__default_time_range_applied"] = "coingecko_30d"
        logger.info(
            "Applied default 30-day range for CoinGecko: %s to %s",
            routing["startDate"],
            routing["endDate"],
        )

    # ExchangeRate intentionally has no default date range.
    return routing
