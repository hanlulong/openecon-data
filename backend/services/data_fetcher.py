"""Data fetching logic: provider dispatch, multi-indicator, and specialty fetchers.

Extracted from query.py to reduce file size and isolate the data retrieval
layer into a focused module.

This module provides:
- fetch_from_provider_dispatch: Route to the correct provider and call its API
- fetch_data: Full fetch pipeline (resolve, cache, dispatch, validate)
- fetch_multi_indicator_data: Parallel fetch for multi-indicator queries
- fetch_from_coingecko: CoinGecko cryptocurrency data fetching
- fetch_exchange_rate_with_historical_fallback: ExchangeRate + FRED fallback
- fetch_historical_exchange_from_fred: FRED historical exchange rates
- extract_exchange_rate_params: Currency pair extraction from query text
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..models import Metadata, NormalizedData, ParsedIntent
from ..utils.retry import retry_async, DataNotAvailableError
from ..services.time_range_defaults import apply_default_time_range
from ..utils.processing_steps import get_processing_tracker

if TYPE_CHECKING:
    from ..providers.fred import FREDProvider
    from ..providers.worldbank import WorldBankProvider
    from ..providers.comtrade import ComtradeProvider
    from ..providers.statscan import StatsCanProvider
    from ..providers.imf import IMFProvider
    from ..providers.exchangerate import ExchangeRateProvider
    from ..providers.bis import BISProvider
    from ..providers.eurostat import EurostatProvider
    from ..providers.oecd import OECDProvider
    from ..providers.coingecko import CoinGeckoProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pre-compiled regex patterns
# ---------------------------------------------------------------------------

_CURRENCY_TO_RE = re.compile(r"\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b")
_CURRENCY_SLASH_RE = re.compile(r"\b([A-Z]{3})[/\-]([A-Z]{3})\b")
_CURRENCY_VS_RE = re.compile(r"\b([A-Z]{3})\s+VS\.?\s+([A-Z]{3})\b")
_CURRENCY_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
_TOP_N_RE = re.compile(r"\btop\s+(\d{1,3})\b")


# ---------------------------------------------------------------------------
# Helper — import normalize_provider_name lazily to avoid circular imports
# ---------------------------------------------------------------------------

def _normalize_provider_name(provider: str) -> str:
    from .query import normalize_provider_name
    return normalize_provider_name(provider)


# ---------------------------------------------------------------------------
# CoinGecko fetcher
# ---------------------------------------------------------------------------

async def fetch_from_coingecko(
    coingecko_provider: Any,
    intent: ParsedIntent,
    params: dict,
) -> list:
    """Fetch cryptocurrency data from CoinGecko.

    Handles:
    - Coin ID mapping (ticker symbols -> CoinGecko IDs)
    - Time period extraction from query text
    - Historical data (date range or days)
    - Current price/market_cap/volume/24h_change
    - Top N rankings by market cap

    Returns list of NormalizedData.
    """
    logger.info("CoinGecko Query Parameters:")
    logger.info(f"   - Indicators: {intent.indicators}")

    query_lower = intent.originalQuery.lower() if intent.originalQuery else ""

    # Extract time periods from query text
    time_patterns = ["last", "past", "previous", "recent", "historical",
                     "days", "weeks", "months", "year", "history"]
    mentions_time = any(p in query_lower for p in time_patterns)

    days_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+days?', query_lower)
    weeks_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+weeks?', query_lower)
    months_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+months?', query_lower)
    year_match = re.search(r'(?:last|past|previous)\s+(\d+)\s+years?', query_lower)

    if not params.get("days"):
        extracted_days = None
        if days_match:
            extracted_days = int(days_match.group(1))
        elif weeks_match:
            extracted_days = int(weeks_match.group(1)) * 7
        elif months_match:
            extracted_days = int(months_match.group(1)) * 30
        elif year_match:
            extracted_days = int(year_match.group(1)) * 365
        elif mentions_time:
            extracted_days = 30
        if extracted_days:
            params["days"] = extracted_days
            params.pop("startDate", None)
            params.pop("endDate", None)

    # Parse coin IDs from params
    raw_coin_ids = params.get("coinIds")
    if isinstance(raw_coin_ids, list):
        coin_ids = [str(cid).strip() for cid in raw_coin_ids if str(cid).strip()]
    elif isinstance(raw_coin_ids, str):
        coin_ids = [p.strip() for p in raw_coin_ids.split(",") if p.strip()]
    else:
        coin_ids = []

    # Sanitize vs_currency
    raw_vs = str(params.get("vsCurrency") or "usd").strip().lower()
    invalid_tokens = {"right", "now", "today", "current", "recent", "latest",
                      "trend", "performance", "history", "historical"}
    vs_currency = raw_vs if raw_vs not in invalid_tokens and re.fullmatch(r"[a-z]{3,10}", raw_vs) else "usd"
    params["vsCurrency"] = vs_currency

    # Coin name -> CoinGecko ID mapping
    coin_map = {
        "bitcoin": "bitcoin", "btc": "bitcoin",
        "ethereum": "ethereum", "eth": "ethereum",
        "solana": "solana", "sol": "solana",
        "cardano": "cardano", "ada": "cardano",
        "polkadot": "polkadot", "dot": "polkadot",
        "avalanche": "avalanche-2", "avax": "avalanche-2",
        "polygon": "matic-network", "matic": "matic-network",
        "chainlink": "chainlink", "link": "chainlink",
        "uniswap": "uniswap", "uni": "uniswap",
        "dogecoin": "dogecoin", "doge": "dogecoin",
        "shiba": "shiba-inu", "shib": "shiba-inu",
        "ripple": "ripple", "xrp": "ripple",
        "binance": "binancecoin", "bnb": "binancecoin",
        "litecoin": "litecoin", "ltc": "litecoin",
        "tron": "tron", "trx": "tron",
        "stellar": "stellar", "xlm": "stellar",
        "cosmos": "cosmos", "atom": "cosmos",
        "near": "near", "nearprotocol": "near",
        "algorand": "algorand", "algo": "algorand",
    }

    # Map provided coin IDs
    if coin_ids:
        coin_ids = [coin_map.get(c.lower(), c) for c in coin_ids]
    else:
        # Auto-detect from indicators
        for indicator in (intent.indicators or []):
            ind_lower = indicator.lower().replace(" ", "")
            for name, cid in coin_map.items():
                if name in ind_lower:
                    coin_ids.append(cid)
                    break

        # Fallback: check query text
        if not coin_ids:
            for name, cid in coin_map.items():
                if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", query_lower):
                    if cid not in coin_ids:
                        coin_ids.append(cid)
            if not coin_ids:
                coin_ids = ["bitcoin"]

    logger.info(f"   - Resolved coins: {coin_ids}, vs={vs_currency}")

    indicator_lower = " ".join(intent.indicators).lower() if intent.indicators else ""
    metric_text = f"{indicator_lower} {query_lower}".strip()

    # Historical data request
    if params.get("startDate") or params.get("endDate") or params.get("days"):
        hist_metric = "price"
        if any(t in metric_text for t in ["market cap", "market capitalization", "marketcap"]):
            hist_metric = "market_cap"
        elif any(t in metric_text for t in ["volume", "trading volume", "24h volume"]):
            hist_metric = "volume"

        if params.get("startDate") and params.get("endDate"):
            series_list = []
            for coin_id in coin_ids:
                data = await coingecko_provider.get_historical_data_range(
                    coin_id=coin_id, vs_currency=vs_currency,
                    from_date=params["startDate"], to_date=params["endDate"],
                    metric=hist_metric,
                )
                series_list.extend(data)
            return series_list
        else:
            days = params.get("days", 30)
            series_list = []
            for coin_id in coin_ids:
                data = await coingecko_provider.get_historical_data(
                    coin_id=coin_id, vs_currency=vs_currency,
                    metric=hist_metric, days=days,
                )
                series_list.extend(data)
            return series_list

    # Current data
    ranking_keywords = ["top", "top 10", "top 5", "top 20", "ranking", "rankings", "largest", "biggest"]
    is_ranking = any(t in metric_text for t in ranking_keywords)

    if is_ranking and "market cap" in metric_text:
        top_n_match = _TOP_N_RE.search(query_lower)
        per_page = int(top_n_match.group(1)) if top_n_match else 10
        per_page = max(1, min(250, per_page))
        return await coingecko_provider.get_market_data(
            vs_currency=vs_currency, order="market_cap_desc", per_page=per_page,
        )

    metric = "price"
    if any(t in metric_text for t in ["volume", "trading volume", "24h volume", "24-hour volume"]):
        metric = "volume"
    elif any(t in metric_text for t in ["market cap", "market capitalization", "marketcap"]):
        metric = "market_cap"
    elif any(t in metric_text for t in ["24h change", "24 hour change", "price change", "change"]):
        metric = "24h_change"

    return await coingecko_provider.get_simple_price(
        coin_ids=coin_ids, vs_currency=vs_currency, metric=metric,
    )


# ---------------------------------------------------------------------------
# Exchange rate fetchers
# ---------------------------------------------------------------------------

async def fetch_exchange_rate_with_historical_fallback(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
) -> list:
    """Fetch exchange rate data, falling back to FRED for historical requests.

    The ExchangeRate-API free tier only supports current rates.
    For historical data, we fall back to FRED which has daily exchange
    rate series for 21 major currency pairs.

    Returns list of NormalizedData.
    Raises DataNotAvailableError if neither source can serve the request.
    """
    from datetime import datetime, timedelta

    logger.info("ExchangeRate Query Parameters:")
    logger.info(f"   - baseCurrency: {params.get('baseCurrency', 'USD')}")
    logger.info(f"   - targetCurrency: {params.get('targetCurrency')}")
    logger.info(f"   - startDate: {params.get('startDate')}")

    # Detect if user is requesting historical data.
    # Rely on LLM-provided startDate/endDate params instead of regex
    # pattern matching on query text -- the LLM handles semantic intent.
    has_historical = False
    start_date = params.get("startDate")
    end_date = params.get("endDate")
    if start_date or end_date:
        try:
            today = datetime.now().date()
            week_ago = today - timedelta(days=7)
            if start_date:
                start_dt = datetime.fromisoformat(start_date[:10]).date()
                if start_dt < week_ago:
                    has_historical = True
            if end_date and not has_historical:
                end_dt = datetime.fromisoformat(end_date[:10]).date()
                if end_dt < today - timedelta(days=1):
                    has_historical = True
        except (ValueError, AttributeError):
            pass

    if has_historical:
        logger.warning("ExchangeRate: Historical data requested -- falling back to FRED")
        result = await fetch_historical_exchange_from_fred(svc, intent, params)
        if result:
            return result
        raise DataNotAvailableError(
            "Historical exchange rate data is not available with the free ExchangeRate API tier. "
            "\n\n**Alternatives:**\n"
            "1. For **current rates**: Rephrase without time references\n"
            "2. For **historical rates**: Use a paid ExchangeRate API key\n"
            "3. For **Real Effective Exchange Rate** (REER): Ask for 'REER' (uses IMF data)\n\n"
            "Note: Some bilateral exchange rates are available via FRED for major currency pairs."
        )

    # Current rate -- use ExchangeRate-API
    series = await svc.exchangerate_provider.fetch_exchange_rate(
        base_currency=params.get("baseCurrency", "USD"),
        target_currency=params.get("targetCurrency"),
        target_currencies=params.get("targetCurrencies"),
    )
    return [series]


async def fetch_historical_exchange_from_fred(
    svc: Any,
    intent: ParsedIntent,
    params: dict,
) -> Optional[list]:
    """Attempt to fetch historical exchange rate from FRED.

    FRED has daily exchange rate series for 21 major currency pairs.
    Returns list of NormalizedData on success, None if currency not supported.
    """
    base_currency = params.get("baseCurrency", "USD")
    target_currency = params.get("targetCurrency")

    if not target_currency:
        query_upper = (intent.originalQuery or "").upper()
        to_match = _CURRENCY_TO_RE.search(query_upper)
        slash_match = re.search(r'\b([A-Z]{3})[/\s](?:VS\s)?([A-Z]{3})\b', query_upper)
        if to_match:
            base_currency, target_currency = to_match.group(1), to_match.group(2)
        elif slash_match:
            base_currency, target_currency = slash_match.group(1), slash_match.group(2)

    if not target_currency:
        return None

    # FRED USD-based exchange rate series
    fred_fx = {
        "EUR": "DEXUSEU", "GBP": "DEXUSUK", "JPY": "DEXJPUS",
        "CAD": "DEXCAUS", "CHF": "DEXSZUS", "AUD": "DEXUSAL",
        "CNY": "DEXCHUS", "MXN": "DEXMXUS", "INR": "DEXINUS",
        "BRL": "DEXBZUS", "KRW": "DEXKOUS", "SEK": "DEXSDUS",
        "NOK": "DEXNOUS", "DKK": "DEXDNUS", "SGD": "DEXSIUS",
        "HKD": "DEXHKUS", "NZD": "DEXUSNZ", "ZAR": "DEXSFUS",
        "THB": "DEXTHUS", "MYR": "DEXMAUS", "TWD": "DEXTAUS",
    }

    target_upper = target_currency.upper()
    base_upper = base_currency.upper()

    fred_series_id = None
    if target_upper in fred_fx and target_upper != "USD":
        fred_series_id = fred_fx[target_upper]
    elif base_upper in fred_fx and target_upper == "USD":
        fred_series_id = fred_fx[base_upper]
    elif base_upper != "USD" and target_upper != "USD":
        fred_series_id = fred_fx.get(base_upper) or fred_fx.get(target_upper)

    if not fred_series_id:
        return None

    try:
        series = await svc.fred_provider.fetch_series({
            "seriesId": fred_series_id,
            "startDate": params.get("startDate"),
            "endDate": params.get("endDate"),
        })
        series.metadata.indicator = f"{base_upper} to {target_upper} Exchange Rate"
        series.metadata.source = "FRED (Federal Reserve)"
        return [series]
    except Exception as e:
        logger.warning(f"FRED exchange rate fallback failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Exchange rate parameter extraction
# ---------------------------------------------------------------------------

def extract_exchange_rate_params(params: dict, intent: ParsedIntent) -> dict:
    """Extract currency pair information from query and populate params.

    CRITICAL: This must be called BEFORE cache lookup to ensure each unique
    currency pair has its own cache entry. Without this, different currency
    queries could share the same incorrect cached data.

    Args:
        params: Current query parameters
        intent: Parsed intent with originalQuery

    Returns:
        Updated params with baseCurrency and targetCurrency populated
    """
    # If params already has both currencies, use them
    if params.get("baseCurrency") and params.get("targetCurrency"):
        logger.info(f"Currency params already set: {params.get('baseCurrency')} -> {params.get('targetCurrency')}")
        return params

    params = {**params}  # Create a copy to avoid mutation

    # Currency code mapping for common names/symbols
    currency_name_map = {
        "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD",
        "euro": "EUR", "euros": "EUR", "eur": "EUR",
        "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "sterling": "GBP", "british pound": "GBP",
        "yen": "JPY", "jpy": "JPY", "japanese yen": "JPY",
        "yuan": "CNY", "cny": "CNY", "renminbi": "CNY", "rmb": "CNY", "chinese yuan": "CNY",
        "franc": "CHF", "chf": "CHF", "swiss franc": "CHF",
        "rupee": "INR", "inr": "INR", "indian rupee": "INR",
        "won": "KRW", "krw": "KRW", "korean won": "KRW",
        "real": "BRL", "brl": "BRL", "brazilian real": "BRL",
        "ruble": "RUB", "rub": "RUB", "russian ruble": "RUB",
        "peso": "MXN", "mxn": "MXN", "mexican peso": "MXN",
        "rand": "ZAR", "zar": "ZAR", "south african rand": "ZAR",
        "lira": "TRY", "try": "TRY", "turkish lira": "TRY",
        "canadian dollar": "CAD", "cad": "CAD", "loonie": "CAD",
        "australian dollar": "AUD", "aud": "AUD", "aussie dollar": "AUD",
        "singapore dollar": "SGD", "sgd": "SGD",
        "hong kong dollar": "HKD", "hkd": "HKD",
        "new zealand dollar": "NZD", "nzd": "NZD", "kiwi dollar": "NZD",
    }

    query_text = (intent.originalQuery or "").upper()

    # Extract currency codes using various patterns
    base_currency = params.get("baseCurrency")
    target_currency = params.get("targetCurrency")

    # Pattern 1: "X to Y" (e.g., "USD to EUR", "JPY to USD")
    to_match = _CURRENCY_TO_RE.search(query_text)
    if to_match:
        base_currency = to_match.group(1)
        target_currency = to_match.group(2)
        logger.info(f"Extracted from 'X to Y' pattern: {base_currency} -> {target_currency}")

    # Pattern 2: "X/Y" or "X-Y" (e.g., "USD/EUR", "EUR-GBP")
    if not base_currency or not target_currency:
        slash_match = _CURRENCY_SLASH_RE.search(query_text)
        if slash_match:
            base_currency = slash_match.group(1)
            target_currency = slash_match.group(2)
            logger.info(f"Extracted from 'X/Y' pattern: {base_currency} -> {target_currency}")

    # Pattern 3: "X vs Y" (e.g., "USD vs EUR")
    if not base_currency or not target_currency:
        vs_match = _CURRENCY_VS_RE.search(query_text)
        if vs_match:
            base_currency = vs_match.group(1)
            target_currency = vs_match.group(2)
            logger.info(f"Extracted from 'X vs Y' pattern: {base_currency} -> {target_currency}")

    # Pattern 4: Try to find any currency codes in the query
    if not base_currency or not target_currency:
        # Look for 3-letter currency codes
        all_codes = _CURRENCY_CODE_RE.findall(query_text)
        # Filter to known currency codes
        valid_codes = {"USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD",
                      "INR", "KRW", "BRL", "MXN", "ZAR", "TRY", "SGD", "HKD",
                      "NZD", "SEK", "NOK", "DKK", "THB", "MYR", "TWD", "RUB"}
        found_codes = [c for c in all_codes if c in valid_codes]
        if len(found_codes) >= 2 and not base_currency:
            base_currency = found_codes[0]
            target_currency = found_codes[1]
            logger.info(f"Extracted from code search: {base_currency} -> {target_currency}")
        elif len(found_codes) == 1:
            # Single currency found - treat as "X to USD" or "USD to X"
            code = found_codes[0]
            if code == "USD":
                # Query is about USD, but we need a target
                # Default to EUR as most common pair
                base_currency = "USD"
                target_currency = params.get("targetCurrency") or "EUR"
            else:
                # Other currency to USD
                base_currency = code
                target_currency = "USD"
            logger.info(f"Single code found: {base_currency} -> {target_currency}")

    # Pattern 5: Try common currency names in lowercase query
    if not base_currency or not target_currency:
        query_lower = (intent.originalQuery or "").lower()
        found_currencies: List[tuple] = []
        for name, code in currency_name_map.items():
            if name in query_lower:
                if code not in [c[1] for c in found_currencies]:
                    # Find position for ordering
                    pos = query_lower.find(name)
                    found_currencies.append((pos, code))
        # Sort by position in query
        found_currencies.sort(key=lambda x: x[0])
        if len(found_currencies) >= 2:
            base_currency = found_currencies[0][1]
            target_currency = found_currencies[1][1]
            logger.info(f"Extracted from currency names: {base_currency} -> {target_currency}")
        elif len(found_currencies) == 1:
            code = found_currencies[0][1]
            if code == "USD":
                base_currency = "USD"
                target_currency = params.get("targetCurrency") or "EUR"
            else:
                base_currency = code
                target_currency = "USD"
            logger.info(f"Single currency name found: {base_currency} -> {target_currency}")

    # Apply defaults if still not found
    if not base_currency:
        base_currency = "USD"
        logger.info("Defaulting baseCurrency to USD")
    if not target_currency:
        # Default to EUR if base is USD, otherwise to USD
        target_currency = "EUR" if base_currency == "USD" else "USD"
        logger.info(f"Defaulting targetCurrency to {target_currency}")

    params["baseCurrency"] = base_currency
    params["targetCurrency"] = target_currency

    return params


# ---------------------------------------------------------------------------
# Provider dispatch — the large switch that routes to individual providers
# ---------------------------------------------------------------------------

async def fetch_from_provider_dispatch(
    svc: Any,
    provider: str,
    intent: ParsedIntent,
    params: dict,
) -> List[NormalizedData]:
    """Route to the correct provider and call its API.

    This is the core provider-dispatch switch extracted from the nested
    ``fetch_from_provider()`` closure inside ``_fetch_data``.

    Args:
        svc: QueryService instance (for accessing provider instances)
        provider: Normalized provider name
        intent: Parsed intent
        params: Query parameters (may be mutated for some providers)

    Returns:
        List of NormalizedData from the chosen provider.
    """
    if provider == "FRED":
        # Ensure params has indicator set
        if not params.get("indicator") and intent.indicators:
            params = {**params, "indicator": intent.indicators[0]}

        # Handle multiple indicators for FRED
        if len(intent.indicators) > 1:
            all_series = []
            for indicator in intent.indicators:
                indicator_params = {**params, "indicator": indicator}
                series = await svc.fred_provider.fetch_series(indicator_params)
                all_series.append(series)
            return all_series
        else:
            series = await svc.fred_provider.fetch_series(params)
            return [series]

    if provider in {"WORLDBANK", "WORLD BANK"}:
        resolved_indicator = params.get("indicator")
        logger.info(f"WorldBank dispatch: indicator={resolved_indicator}, country={params.get('country')}, countries={params.get('countries')}, startDate={params.get('startDate')}")
        # Handle multiple indicators for World Bank
        if len(intent.indicators) > 1:
            all_data: List[NormalizedData] = []
            indicators_to_fetch = intent.indicators
            if resolved_indicator and len(intent.indicators) > 1:
                indicators_to_fetch = [str(resolved_indicator)]

            for indicator in indicators_to_fetch:
                data = await svc.world_bank_provider.fetch_indicator(
                    indicator=indicator,
                    country=params.get("country"),
                    countries=params.get("countries"),
                    start_date=params.get("startDate"),
                    end_date=params.get("endDate"),
                )
                all_data.extend(data if isinstance(data, list) else [data])
            return all_data
        else:
            indicator = str(resolved_indicator or (intent.indicators[0] if intent.indicators else ""))
            wb_result = await svc.world_bank_provider.fetch_indicator(
                indicator=indicator,
                country=params.get("country"),
                countries=params.get("countries"),
                start_date=params.get("startDate"),
                end_date=params.get("endDate"),
            )
            if isinstance(wb_result, list):
                logger.info(f"WorldBank returned: {len(wb_result)} series, data_pts={[len(r.data) for r in wb_result if r]}")
            else:
                logger.info(f"WorldBank returned: type={type(wb_result)}, data_pts={len(wb_result.data) if wb_result and wb_result.data else 0}")
            return wb_result

    if provider == "COMTRADE":
        indicators = [indicator.lower() for indicator in intent.indicators]
        if any("balance" in indicator for indicator in indicators):
            series = await svc.comtrade_provider.fetch_trade_balance(
                reporter=params.get("reporter") or params.get("country") or "US",
                partner=params.get("partner"),
                start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                frequency=params.get("frequency", "annual"),
            )
            return [series]
        reporter_value = params.get("reporter") or params.get("country")
        reporters_value = params.get("reporters") or params.get("countries")
        # If an explicit reporter is present (common for bilateral queries),
        # ignore broad countries[] context to avoid duplicate/misaligned fan-out.
        if reporter_value:
            reporters_value = None
        return await svc.comtrade_provider.fetch_trade_data(
            reporter=reporter_value,
            reporters=reporters_value,
            partner=params.get("partner"),
            commodity=params.get("commodity"),
            flow=params.get("flow"),
            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            frequency=params.get("frequency", "annual"),
        )

    if provider in {"STATSCAN", "STATISTICS CANADA"}:
        return await _fetch_from_statscan(svc, intent, params)

    if provider == "IMF":
        return await _fetch_from_imf(svc, intent, params)

    if provider in {"EXCHANGERATE", "EXCHANGE_RATE", "FX"}:
        return await fetch_exchange_rate_with_historical_fallback(svc, intent, params)

    if provider == "BIS":
        indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else "POLICY_RATE"))
        params["indicator"] = indicator
        return await svc.bis_provider.fetch_indicator(
            indicator=indicator,
            country=params.get("country"),
            countries=params.get("countries"),
            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            frequency=params.get("frequency", "M"),
        )

    if provider == "EUROSTAT":
        return await _fetch_from_eurostat(svc, intent, params)

    if provider == "OECD":
        return await _fetch_from_oecd(svc, intent, params)

    if provider in {"COINGECKO", "COIN GECKO"}:
        return await fetch_from_coingecko(svc.coingecko_provider, intent, params)

    raise DataNotAvailableError(
        f"Provider {intent.apiProvider} is not yet implemented. Available providers: FRED, World Bank, Comtrade, StatsCan, IMF, ExchangeRate, BIS, Eurostat, OECD, CoinGecko"
    )


# ---------------------------------------------------------------------------
# Provider-specific helpers (kept private to this module)
# ---------------------------------------------------------------------------

async def _fetch_from_statscan(svc: Any, intent: ParsedIntent, params: dict) -> List[NormalizedData]:
    """Statistics Canada provider dispatch."""
    dimensions = params.get("dimensions", {})

    # Phase 3: pick up __dimensions from materialized ConversationState
    # These are dimension modifiers detected by the context-aware DeltaExtractor
    # and carried through materialize_intent().
    if not dimensions and params.get("__dimensions"):
        dimensions = params["__dimensions"]
        logger.info(f"StatsCan: using __dimensions from conversation state: {dimensions}")

    entity = params.get("entity")
    indicator = params.get("indicator", intent.indicators[0] if intent.indicators else None)

    # --- Framework fix: resolve numeric table/product IDs back to known vectors ---
    resolved_indicator = indicator
    indicator_key = indicator.upper().replace(" ", "_") if indicator else None
    _sc_vectors = svc.statscan_provider.VECTOR_MAPPINGS
    _sc_coords = svc.statscan_provider.COORDINATE_PRODUCT_MAPPINGS

    if indicator_key and indicator_key not in _sc_vectors and indicator_key not in _sc_coords:
        for hr_indicator in (intent.indicators or []):
            hr_key = hr_indicator.upper().replace(" ", "_")
            if hr_key in _sc_vectors or hr_key in _sc_coords:
                logger.info(
                    f"StatsCan: LLM indicator '{indicator}' not in mappings; "
                    f"resolved from intent.indicators '{hr_indicator}' -> key '{hr_key}'"
                )
                resolved_indicator = hr_indicator
                indicator_key = hr_key
                params = {**params, "indicator": hr_indicator}
                break
        else:
            for hr_indicator in (intent.indicators or []):
                normalised_key = hr_indicator.upper().replace(" ", "_").replace("-", "_")
                if normalised_key in _sc_vectors or normalised_key in _sc_coords:
                    logger.info(
                        f"StatsCan: resolved via normalised intent indicator "
                        f"'{hr_indicator}' -> '{normalised_key}'"
                    )
                    resolved_indicator = normalised_key
                    indicator_key = normalised_key
                    params = {**params, "indicator": normalised_key}
                    break

    indicator = resolved_indicator

    # Check for industry/breakdown parameter
    industry = params.get("industry") or params.get("breakdown")
    if industry:
        industry_lower = industry.lower()
        if any(demo in industry_lower for demo in ["age", "gender", "sex", "demographic"]):
            logger.info(f"Demographic breakdown detected: {industry}")
            combined_indicator = f"{indicator or 'EMPLOYMENT'}_BY_AGE"
            demo_params = {
                "indicator": combined_indicator,
                "startDate": params.get("startDate"),
                "endDate": params.get("endDate"),
                "periods": params.get("periods", 240),
            }
            series = await svc.statscan_provider.fetch_series(demo_params)
            return [series]
        else:
            logger.info(f"Industry breakdown detected: {industry}")
            breakdown_params = {
                "indicator": indicator or "GDP",
                "breakdown": industry,
                "startDate": params.get("startDate"),
                "endDate": params.get("endDate"),
                "periods": params.get("periods", 240),
            }
            series = await svc.statscan_provider.fetch_with_breakdown(breakdown_params)
            return [series]

    # If entity is present (from decomposition), convert to dimension
    if entity and not dimensions:
        dimensions = {"geography": entity}

    # --- Dimension modifier detection (GENERAL, metadata-driven) ---
    # Before falling through to standard vector/dynamic fetch, check if the
    # query text contains dimension modifiers (province names, gender terms,
    # age terms, product categories, etc.) that should narrow the data.
    # This uses the table's actual metadata, NOT hardcoded modifier lists.
    if indicator and not dimensions:
        _indicator_key = indicator.upper().replace(" ", "_").replace("-", "_")
        _is_known = (
            _indicator_key in svc.statscan_provider.VECTOR_MAPPINGS
            or _indicator_key in svc.statscan_provider.COORDINATE_PRODUCT_MAPPINGS
        )
        if _is_known:
            query_text = intent.originalQuery or ""
            try:
                # Resolve the product ID for this indicator
                _product_id = None
                _vec = svc.statscan_provider.VECTOR_MAPPINGS.get(_indicator_key)
                _coord = svc.statscan_provider.COORDINATE_PRODUCT_MAPPINGS.get(_indicator_key)
                if _coord:
                    _product_id = svc.statscan_provider._normalize_metadata_product_id(_coord[0])
                elif _vec is not None:
                    _cached = svc.statscan_provider.PRODUCT_ID_CACHE.get(_vec)
                    if _cached:
                        _product_id = svc.statscan_provider._normalize_metadata_product_id(_cached)

                if _product_id:
                    _cube_meta = await svc.statscan_provider._get_cube_metadata(_product_id)
                    _modifiers = svc.statscan_provider.extract_dimension_modifiers(
                        query_text, _indicator_key, _product_id, _cube_meta,
                    )
                    if _modifiers:
                        logger.info(
                            f"StatsCan dimension modifiers detected: {_modifiers} "
                            f"for indicator={_indicator_key}"
                        )
                        start_year = int(params["startDate"][:4]) if params.get("startDate") else None
                        end_year = int(params["endDate"][:4]) if params.get("endDate") else None
                        series = await svc.statscan_provider.fetch_with_dimensions(
                            base_indicator=_indicator_key,
                            modifiers=_modifiers,
                            start_year=start_year,
                            end_year=end_year,
                            periods=params.get("periods", 240),
                        )
                        return [series]
            except Exception as e:
                logger.warning(
                    f"Dimension modifier extraction/fetch failed for {_indicator_key}: {e}. "
                    f"Falling through to standard fetch."
                )

    # Use dimension-aware fetch when dimensions are specified
    if dimensions:
        # Phase 3: If dimensions came from conversation state (__dimensions),
        # use fetch_with_dimensions which is the general mechanism that
        # discovers table metadata and builds coordinates dynamically.
        # fetch_categorical_data is only for basic product ID / dimensions combos.
        _indicator_key_for_dim = (indicator or "").upper().replace(" ", "_").replace("-", "_")
        _is_known_dim = (
            _indicator_key_for_dim in svc.statscan_provider.VECTOR_MAPPINGS
            or _indicator_key_for_dim in svc.statscan_provider.COORDINATE_PRODUCT_MAPPINGS
        )
        if _is_known_dim and params.get("__dimensions"):
            try:
                start_year = int(params["startDate"][:4]) if params.get("startDate") else None
                end_year = int(params["endDate"][:4]) if params.get("endDate") else None
                series = await svc.statscan_provider.fetch_with_dimensions(
                    base_indicator=_indicator_key_for_dim,
                    modifiers=dimensions,
                    start_year=start_year,
                    end_year=end_year,
                    periods=params.get("periods", 240),
                )
                return [series]
            except Exception as e:
                logger.warning(
                    f"fetch_with_dimensions failed for {_indicator_key_for_dim} "
                    f"with __dimensions={dimensions}: {e}. Falling through."
                )

        categorical_params = {
            "productId": params.get("productId", "17100005"),
            "indicator": indicator or "Population",
            "periods": params.get("periods", 20),
            "dimensions": dimensions,
        }
        series = await svc.statscan_provider.fetch_categorical_data(categorical_params)
        return [series]
    else:
        if indicator and indicator.upper().replace(" ", "_") in svc.statscan_provider.VECTOR_MAPPINGS:
            series = await svc.statscan_provider.fetch_series(params)
            return [series]
        elif indicator:
            logger.info(f"Using dynamic discovery for StatsCan indicator: {indicator}")
            dynamic_params = {
                "indicator": indicator,
                "indicatorLabel": str(intent.indicators[0] if intent.indicators else indicator),
                "geography": params.get("geography"),
                "periods": params.get("periods", 240),
            }
            try:
                result = await svc.statscan_provider.fetch_dynamic_data(dynamic_params)
                return [result]
            except DataNotAvailableError:
                logger.warning(f"Dynamic discovery failed for {indicator}, trying vector fetch")
                series = await svc.statscan_provider.fetch_series(params)
                return [series]
        else:
            raise DataNotAvailableError("No indicator specified for Statistics Canada query")


async def _fetch_from_imf(svc: Any, intent: ParsedIntent, params: dict) -> List[NormalizedData]:
    """IMF provider dispatch."""
    countries_param = params.get("countries") or params.get("country")
    resolved_indicator = str(params.get("indicator") or "").strip()

    # Resolve countries/regions to list of country codes
    resolved_countries: List[str] = []
    if isinstance(countries_param, list):
        for item in countries_param:
            resolved_countries.extend(svc.imf_provider._resolve_countries(item))
    elif isinstance(countries_param, str):
        resolved_countries = svc.imf_provider._resolve_countries(countries_param)
    else:
        resolved_countries = ["USA"]

    # Remove duplicates while preserving order
    resolved_countries = list(dict.fromkeys(resolved_countries))

    logger.info(
        "IMF query resolved to %d countries: %s (from params: %s)",
        len(resolved_countries),
        resolved_countries[:10] if len(resolved_countries) > 10 else resolved_countries,
        countries_param,
    )

    if len(resolved_countries) > 1:
        logger.info("Using IMF batch method for %d countries", len(resolved_countries))
        all_data: List[NormalizedData] = []
        indicators_to_fetch = list(intent.indicators or [])
        if resolved_indicator:
            indicators_to_fetch = [resolved_indicator]
        if not indicators_to_fetch:
            indicators_to_fetch = [resolved_indicator] if resolved_indicator else []

        for indicator in indicators_to_fetch:
            series_list = await svc.imf_provider.fetch_batch_indicator(
                indicator=indicator,
                countries=resolved_countries,
                start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            )
            all_data.extend(series_list)
        return all_data
    else:
        country = resolved_countries[0]
        if len(intent.indicators) > 1:
            all_data = []
            indicators_to_fetch = list(intent.indicators or [])
            if resolved_indicator:
                indicators_to_fetch = [resolved_indicator]
            for indicator in indicators_to_fetch:
                series = await svc.imf_provider.fetch_indicator(
                    indicator=indicator,
                    country=country,
                    start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                    end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                )
                all_data.append(series)
            return all_data
        else:
            indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else ""))
            series = await svc.imf_provider.fetch_indicator(
                indicator=indicator,
                country=country,
                start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            )
            return [series]


async def _fetch_from_eurostat(svc: Any, intent: ParsedIntent, params: dict) -> List[NormalizedData]:
    """Eurostat provider dispatch."""
    indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else "GDP"))
    params["indicator"] = indicator

    country_param = params.get("country")
    countries_param = params.get("countries", [])

    # EU aggregate codes that should NOT expand
    EU_AGGREGATES = {"EU", "EU27", "EU27_2020", "EU28", "EA", "EA19", "EA20", "EUROZONE", "EURO_AREA"}

    is_multi_country = isinstance(countries_param, list) and len(countries_param) > 1

    if not is_multi_country and isinstance(country_param, str):
        upper_country = country_param.upper().replace(" ", "_")
        if upper_country not in EU_AGGREGATES:
            from ..routing.country_resolver import CountryResolver

            expanded = CountryResolver.expand_region(country_param)
            if expanded:
                countries_param = expanded
                is_multi_country = True
                logger.info(f"Expanded Eurostat region '{country_param}' to {len(expanded)} countries via CountryResolver")
            else:
                SUB_REGION_MAPPINGS = {
                    "BENELUX": ["BE", "NL", "LU"],
                    "BALTIC": ["EE", "LV", "LT"],
                    "DACH": ["DE", "AT", "CH"],
                    "IBERIAN": ["ES", "PT"],
                    "VISEGRAD": ["PL", "CZ", "SK", "HU"],
                    "V4": ["PL", "CZ", "SK", "HU"],
                }
                if upper_country in SUB_REGION_MAPPINGS:
                    countries_param = SUB_REGION_MAPPINGS[upper_country]
                    is_multi_country = True
                    logger.info(f"Expanded Eurostat sub-region '{country_param}' to: {countries_param}")

    if is_multi_country:
        logger.info(f"Multi-country Eurostat query detected: {countries_param}")
        eurostat_sem = asyncio.Semaphore(5)

        async def _fetch_eurostat_country(country_code: str) -> Optional[NormalizedData]:
            async with eurostat_sem:
                try:
                    return await svc.eurostat_provider.fetch_indicator(
                        indicator=indicator,
                        country=country_code,
                        start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                        end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
                    )
                except Exception as e:
                    logger.warning(f"Failed to fetch {indicator} for {country_code}: {e}")
                    return None

        eurostat_results = await asyncio.gather(
            *[_fetch_eurostat_country(c) for c in countries_param],
            return_exceptions=True,
        )
        series_list = [
            r for r in eurostat_results
            if isinstance(r, NormalizedData)
        ]

        if not series_list:
            raise DataNotAvailableError(f"No Eurostat data available for {indicator} in any requested countries")

        return series_list

    # Single country query (default to EU aggregate if not specified)
    single_country = country_param if country_param else "EU27_2020"
    series = await svc.eurostat_provider.fetch_indicator(
        indicator=indicator,
        country=single_country,
        start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
        end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
    )
    return [series]


async def _fetch_from_oecd(svc: Any, intent: ParsedIntent, params: dict) -> List[NormalizedData]:
    """OECD provider dispatch."""
    indicator = str(params.get("indicator") or (intent.indicators[0] if intent.indicators else "GDP"))
    params["indicator"] = indicator

    country_param = params.get("country")
    countries_param = params.get("countries", [])

    # Handle LLM parsing "OECD unemployment" as countries=["ALL_OECD"]
    if countries_param and len(countries_param) == 1:
        c = countries_param[0].upper().replace(" ", "_")
        if c in ("OECD", "ALL_OECD", "ALL_OECD_COUNTRIES", "OECD_COUNTRIES"):
            logger.info(f"Converting countries=['{countries_param[0]}'] to OECD aggregate query")
            country_param = "OECD"
            countries_param = []

    # If no country specified, use OECD aggregate
    if not country_param and not countries_param:
        logger.info("No country specified for OECD query, using OECD aggregate")
        country_param = "OECD"

    # Detect multi-country requests including region names
    expanded_countries: List[str] = []
    if isinstance(country_param, str):
        if country_param.upper() in ("OECD", "OECD_AVERAGE"):
            expanded_countries = ["OECD"]
        else:
            expanded_countries = svc.oecd_provider.expand_countries(country_param)

    is_multi_country = (
        isinstance(countries_param, list) and len(countries_param) > 1
    ) or (
        len(expanded_countries) > 1
    )

    if is_multi_country:
        logger.info("Multi-country OECD query detected")
        try:
            countries = countries_param if countries_param else expanded_countries
            series_list = await svc.oecd_provider.fetch_multi_country(
                indicator=indicator,
                countries=countries,
                start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
                end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
            )
            return series_list
        except Exception as exc:
            error_msg = str(exc).lower()
            temporarily_unavailable = any(
                token in error_msg
                for token in ("rate limit", "429", "circuit", "timeout", "timed out", "temporarily unavailable")
            )
            if temporarily_unavailable:
                logger.warning("OECD multi-country temporarily unavailable: %s", exc)
                raise DataNotAvailableError(
                    f"OECD temporarily unavailable for multi-country request: {exc}"
                ) from exc
            raise

    try:
        series = await svc.oecd_provider.fetch_indicator(
            indicator=indicator,
            country=country_param,
            start_year=int(params["startDate"][:4]) if params.get("startDate") else None,
            end_year=int(params["endDate"][:4]) if params.get("endDate") else None,
        )
        return [series]
    except Exception as exc:
        error_msg = str(exc).lower()
        temporarily_unavailable = any(
            token in error_msg
            for token in ("rate limit", "429", "circuit", "timeout", "timed out", "temporarily unavailable")
        )
        if temporarily_unavailable:
            logger.warning("OECD temporarily unavailable for %s: %s", country_param, exc)
            raise DataNotAvailableError(
                f"OECD temporarily unavailable for {country_param or 'OECD'}: {exc}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Main fetch orchestration
# ---------------------------------------------------------------------------

async def fetch_data(svc: Any, intent: ParsedIntent) -> List[NormalizedData]:
    """Full data fetch pipeline: resolve indicator, check cache, dispatch to provider, validate.

    This is the primary entry point extracted from ``QueryService._fetch_data``.

    Args:
        svc: QueryService instance
        intent: Parsed intent to fetch data for

    Returns:
        List of NormalizedData from the selected provider.
    """
    logger.info(f"_fetch_data called: provider={intent.apiProvider}, indicators={intent.indicators}")

    provider = _normalize_provider_name(intent.apiProvider)

    # Early exit: concept is not available from any provider
    if provider == "NOT_AVAILABLE":
        indicator_text = intent.indicators[0] if intent.indicators else "this indicator"
        logger.info(f"Not available: {indicator_text} -- no provider carries this data")
        return [NormalizedData(
            metadata=Metadata(
                source="Catalog",
                indicator=f"{indicator_text} -- Not Available",
                frequency="N/A",
                unit="N/A",
                lastUpdated="",
                description=(
                    f"'{indicator_text}' is not currently available through any of our "
                    f"data providers. This may be because the data has been discontinued, "
                    f"archived, or is only available through specialized sources."
                ),
            ),
            data=[],
        )]
    params = intent.parameters or {}

    # Pre-flight geographic split
    if not params.get("__geo_split_child"):
        split_result = await svc._preflight_geographic_split(intent)
        if split_result is not None:
            return split_result
    # Strip the recursion guard
    if "__geo_split_child" in params:
        params = {k: v for k, v in params.items() if k != "__geo_split_child"}
        intent.parameters = params

    fallback_excluded_providers = {
        _normalize_provider_name(str(candidate))
        for candidate in (params.get("__fallback_excluded_providers") or [])
        if candidate
    }
    fallback_excluded_providers.discard("")
    tracker = get_processing_tracker()

    ranking_scope_query = str(intent.originalQuery or "").strip()
    if not ranking_scope_query and intent.indicators:
        ranking_scope_query = " ".join(str(indicator) for indicator in intent.indicators if indicator)
    params = svc._maybe_expand_ranking_country_scope(ranking_scope_query, provider, params)
    intent.parameters = params

    provider, params = svc._apply_concept_provider_override(provider, intent, params)
    intent.parameters = params

    # PHASE B: Resolve indicator code via unified resolution pipeline
    params = await svc._resolve_indicator_for_fetch(provider, intent, params)

    # Check catalog availability and re-route if needed
    provider, params = svc._apply_catalog_availability_override(
        provider, intent, params, fallback_excluded_providers
    )

    # Preserve catalog-resolved flag as a transient attribute on the intent
    if params.get("__catalog_resolved"):
        object.__setattr__(intent, "_catalog_resolved", True)

    internal_param_keys = {"__fallback_excluded_providers", "__catalog_resolved", "__catalog_concept", "__qualifier_checked", "__geo_split_child"}
    if any(key in params for key in internal_param_keys):
        params = {k: v for k, v in params.items() if k not in internal_param_keys}
        intent.parameters = params

    # Apply smart default time ranges based on provider
    logger.info(f"Before defaults - provider={provider}, startDate={params.get('startDate')}, endDate={params.get('endDate')}")
    params = apply_default_time_range(provider, params)
    logger.info(f"After defaults - startDate={params.get('startDate')}, start_year={params.get('start_year')}")
    intent.parameters = params

    # For ExchangeRate queries, extract currency pairs BEFORE cache lookup
    if provider == "EXCHANGERATE":
        params = extract_exchange_rate_params(params, intent)
        intent.parameters = params
        logger.info(f"ExchangeRate: Cache params after currency extraction: baseCurrency={params.get('baseCurrency')}, targetCurrency={params.get('targetCurrency')}")

    # Include originalQuery in params for cache key differentiation.
    # This ensures "CPI shelter Canada" and "CPI energy Canada" get
    # separate cache entries even though both resolve to indicator=CPI.
    if intent.originalQuery and "__original_query" not in params:
        params["__original_query"] = intent.originalQuery

    cached = await svc._get_from_cache(provider, params)
    if cached:
        logger.info("Cache hit for %s", provider)
        result_list = cached if isinstance(cached, list) else [cached]
        svc._normalize_bis_metadata_labels(result_list)
        if tracker:
            with tracker.track(
                "cache_hit",
                "Served instantly from cache",
                {
                    "provider": provider,
                    "indicator_count": len(intent.indicators),
                },
            ) as update_cache_metadata:
                update_cache_metadata({
                    "series_count": len(result_list),
                    "cached": True,
                })
                return result_list
        return result_list

    logger.info("Cache miss for %s, fetching from API", provider)

    if tracker:
        provider_names = {
            "FRED": "Federal Reserve",
            "WORLDBANK": "World Bank",
            "COMTRADE": "UN Comtrade",
            "STATSCAN": "Statistics Canada",
            "BIS": "Bank for International Settlements",
            "EUROSTAT": "Eurostat",
            "OECD": "OECD",
            "COINGECKO": "CoinGecko",
        }
        provider_display = provider_names.get(provider, provider)
        fetch_message = f"Retrieving data from {provider_display}..."

        with tracker.track(
            "fetching_data",
            fetch_message,
            {
                "provider": provider,
                "indicator_count": len(intent.indicators),
            },
        ) as update_fetch_metadata:
            provider_start = time.perf_counter()
            result = await fetch_from_provider_dispatch(svc, provider, intent, params)
            provider_elapsed = time.perf_counter() - provider_start
            logger.info(f"Provider {provider} fetch: {provider_elapsed:.2f}s")
            update_fetch_metadata({
                "series_count": len(result),
                "cached": False,
                "fetch_time_ms": round(provider_elapsed * 1000, 1),
            })
    else:
        provider_start = time.perf_counter()
        result = await fetch_from_provider_dispatch(svc, provider, intent, params)
        provider_elapsed = time.perf_counter() - provider_start
        logger.info(f"Provider {provider} fetch: {provider_elapsed:.2f}s")

    if not result or (len(result) == 1 and not result[0].data):
        raise DataNotAvailableError(
            f"No data available from {provider} for the requested parameters. "
            f"The data may not exist or may not be available for the specified time period or location."
        )

    svc._normalize_bis_metadata_labels(result)

    # Validate data before returning
    from backend.services.data_validator import get_data_validator
    validator = get_data_validator()
    for data_series in result:
        validation_result = validator.validate(data_series)
        validator.log_validation_results(data_series, validation_result)
        if not validation_result.valid or validation_result.confidence < 0.5:
            logger.warning(
                f"Data quality concern for {data_series.metadata.indicator if data_series.metadata else 'UNKNOWN'}: "
                f"confidence={validation_result.confidence:.2f}, issues={len(validation_result.issues)}"
            )

    await svc._save_to_cache(provider, params, result if len(result) > 1 else result[0])
    return result


async def fetch_multi_indicator_data(svc: Any, intent: ParsedIntent) -> List[NormalizedData]:
    """Fetch data for multiple indicators by making separate API calls for each.

    Args:
        svc: QueryService instance
        intent: Parsed intent with multiple indicators

    Returns:
        Combined list of NormalizedData from all indicator fetches.
    """
    from ..services.parameter_validator import ParameterValidator

    all_data: List[NormalizedData] = []
    explicit_provider = svc._normalize_provider_alias(
        svc._detect_explicit_provider(intent.originalQuery or "")
    )

    # Ensure default time periods are applied to base intent first
    if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
        logger.info("Applying default time periods to multi-indicator query...")
        ParameterValidator.apply_default_time_periods(intent)

    # Create separate intents for each indicator
    fetch_tasks = []
    for indicator in intent.indicators:
        params = dict(intent.parameters) if intent.parameters else {}

        params["indicator"] = indicator
        params.pop("__catalog_resolved", None)
        params.pop("__catalog_concept", None)

        single_provider = _normalize_provider_name(intent.apiProvider)
        if explicit_provider:
            single_provider = explicit_provider
        else:
            try:
                routing_intent = ParsedIntent(
                    apiProvider=single_provider,
                    indicators=[indicator],
                    parameters=dict(params),
                    clarificationNeeded=False,
                    originalQuery=intent.originalQuery,
                )
                routed_provider = await svc._select_routed_provider(
                    routing_intent,
                    f"{indicator} {intent.originalQuery or ''}".strip(),
                )
                if routed_provider:
                    single_provider = routed_provider
            except Exception as exc:
                logger.debug(
                    "Multi-indicator provider routing failed for '%s': %s",
                    indicator,
                    exc,
                )

        countries_text = ""
        if params.get("countries"):
            countries_text = f" for {', '.join(str(c) for c in params['countries'][:3])}"
        narrowed_query = f"{indicator}{countries_text}"

        single_intent = ParsedIntent(
            apiProvider=single_provider,
            indicators=[indicator],
            parameters=params,
            clarificationNeeded=False,
            confidence=intent.confidence,
            recommendedChartType=intent.recommendedChartType,
            originalQuery=narrowed_query,
        )

        task = retry_async(
            lambda i=single_intent: fetch_data(svc, i),
            max_attempts=2,
            initial_delay=0.5,
        )
        fetch_tasks.append(task)

    # Fetch all indicators in parallel with a total timeout
    num_countries = len(
        intent.parameters.get("countries", []) if intent.parameters else []
    )
    total_timeout = min(90, 45 + max(0, num_countries - 3) * 5)
    logger.info(
        "Fetching %s indicators in parallel (timeout=%ds, countries=%d)...",
        len(fetch_tasks), total_timeout, num_countries,
    )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*fetch_tasks, return_exceptions=True),
            timeout=total_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Multi-indicator fetch timed out after %ds -- returning partial results",
            total_timeout,
        )
        results = []

    # Collect successful results
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            indicator_name = intent.indicators[i] if i < len(intent.indicators) else "unknown"
            logger.warning("Failed to fetch indicator %s: %s", indicator_name, result)
            continue
        if isinstance(result, list):
            all_data.extend(result)
        else:
            all_data.append(result)

    if not all_data:
        raise DataNotAvailableError(
            f"Could not fetch any of the requested indicators: {', '.join(intent.indicators)}"
        )

    logger.info("Successfully fetched %s datasets for %s indicators", len(all_data), len(intent.indicators))
    return all_data
