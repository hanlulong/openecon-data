from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import get_settings
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from ..services.http_pool import get_http_client
from .base import BaseProvider

logger = logging.getLogger(__name__)


class FREDProvider(BaseProvider):
    """FRED (Federal Reserve Economic Data) provider.

    PHASE D: Now inherits from BaseProvider for:
    - Unified provider_name property
    - Standardized HTTP retry logic
    - Common error handling patterns
    """
    SERIES_MAPPINGS: Dict[str, str] = {
        # GDP and Growth
        "GDP": "GDP",
        "GDP_GROWTH": "A191RL1Q225SBEA",  # Real GDP growth rate (percentage)
        "GDP_GROWTH_RATE": "A191RL1Q225SBEA",  # Real GDP growth rate (percentage)
        "REAL_GDP_GROWTH": "A191RL1Q225SBEA",  # Real GDP growth rate (percentage)
        "REAL_GDP_GROWTH_RATE": "A191RL1Q225SBEA",  # Real GDP growth rate (percentage)
        "GDP_PER_CAPITA": "A939RX0Q048SBEA",  # Real GDP per capita
        "REAL_GDP_PER_CAPITA": "A939RX0Q048SBEA",  # Real GDP per capita
        "REAL_GDP": "GDPC1",  # Real Gross Domestic Product
        "NOMINAL_GDP": "GDP",  # Nominal GDP

        # Labor Market
        "UNEMPLOYMENT": "UNRATE",
        "UNEMPLOYMENT_RATE": "UNRATE",
        "TOTAL_EMPLOYMENT": "PAYEMS",  # All employees, total nonfarm
        "EMPLOYMENT": "PAYEMS",
        "NONFARM_PAYROLL": "PAYEMS",
        "NONFARM_PAYROLLS": "PAYEMS",
        "TOTAL_NONFARM_PAYROLLS": "PAYEMS",
        "TOTAL_NONFARM_PAYROLL": "PAYEMS",
        "US_NONFARM_PAYROLLS": "PAYEMS",
        "US_EMPLOYMENT": "PAYEMS",
        "PAYROLLS": "PAYEMS",
        "JOBS": "PAYEMS",
        "LABOR_FORCE_PARTICIPATION": "CIVPART",
        "LABOR_FORCE_PARTICIPATION_RATE": "CIVPART",
        "LABOR_FORCE_PARTICIPATION_MEN": "LNS11300001",  # Men, 20 years and over
        "LABOR_FORCE_PARTICIPATION_MALE": "LNS11300001",
        "LABOR_FORCE_PARTICIPATION_WOMEN": "LNS11300002",  # Women, 20 years and over
        "LABOR_FORCE_PARTICIPATION_FEMALE": "LNS11300002",
        "LABOR_FORCE_PARTICIPATION_BY_GENDER": "LNS11300001",  # Default to men, suggest comparison
        "LABOR_FORCE": "CLF16OV",
        "JOBLESS_CLAIMS": "ICSA",  # Initial Claims, Seasonally Adjusted
        "INITIAL_CLAIMS": "ICSA",  # Unemployment insurance initial claims
        "INITIAL_JOBLESS_CLAIMS": "ICSA",  # Initial jobless claims (alternative phrasing)
        "UNEMPLOYMENT_CLAIMS": "ICSA",  # Alternative phrasing for initial claims
        "WEEKLY_JOBLESS_CLAIMS": "ICSA",  # Weekly initial jobless claims
        "WEEKLY_INITIAL_CLAIMS": "ICSA",  # Weekly initial claims

        # Prices and Inflation
        # Note: INFLATION uses CPIAUCSL with pc1 transformation (percent change from year ago)
        "INFLATION": "CPIAUCSL:pc1",  # Use pc1 transformation for inflation rate
        "INFLATION_RATE": "CPIAUCSL:pc1",  # Use pc1 transformation for inflation rate
        "CPI": "CPIAUCSL",  # Raw CPI index
        "CPI_ALL_ITEMS": "CPIAUCSL",
        "CONSUMER_PRICE_INDEX": "CPIAUCSL",
        "CPI_INFLATION": "CPIAUCSL:pc1",  # CPI-based inflation rate
        "CORE_CPI": "CPILFESL",  # CPI for All Urban Consumers: All Items Less Food and Energy
        "CPI_CORE": "CPILFESL",
        "CPI_EXCLUDING_FOOD_AND_ENERGY": "CPILFESL",
        "PPI": "PPIACO",  # Producer Price Index: All Commodities
        "PRODUCER_PRICE_INDEX": "PPIACO",
        # Commodity Price Indices - NOTE: FRED does NOT have gold/silver SPOT prices
        # These are producer price indices, not spot commodity prices
        "COMMODITY_PRICE_INDEX": "PPIACO",  # PPI All Commodities
        "COMMODITY_PRICES": "PPIACO",
        "COMMODITY_INDEX": "PPIACO",
        "ALL_COMMODITIES": "PPIACO",
        "PPI_ALL_COMMODITIES": "PPIACO",
        "PPI_COMMODITIES": "PPIACO",
        "PCE_INFLATION": "PCEPI",  # Personal Consumption Expenditures: Chain-type Price Index
        "PCE_PRICE_INDEX": "PCEPI",
        "CORE_PCE": "PCEPILFE",  # Personal Consumption Expenditures Excluding Food and Energy
        "CORE_PCE_INFLATION": "PCEPILFE",
        "CORE_PCE_INFLATION_RATE": "PCEPILFE",
        "PCE_CORE": "PCEPILFE",
        "PCE_CORE_INFLATION": "PCEPILFE",
        "PCE_EXCLUDING_FOOD_AND_ENERGY": "PCEPILFE",

        # Interest Rates
        "INTEREST_RATE": "FEDFUNDS",
        "FED_FUNDS": "FEDFUNDS",
        "FEDERAL_FUNDS_RATE": "FEDFUNDS",
        "MORTGAGE_RATE": "MORTGAGE30US",  # 30-Year Fixed Rate Mortgage Average
        "MORTGAGE30": "MORTGAGE30US",
        "PRIME_RATE": "DPRIME",  # Bank Prime Loan Rate
        "PRIME_BANK_LOAN_RATE": "DPRIME",
        "PRIME_LENDING_RATE": "DPRIME",
        "TREASURY_YIELD_2_YEAR": "DGS2",  # 2-Year Treasury Constant Maturity Rate
        "2_YEAR_TREASURY": "DGS2",
        "2_YEAR_TREASURY_YIELD": "DGS2",  # Alternative format
        "2YR_TREASURY": "DGS2",
        "2YR_TREASURY_YIELD": "DGS2",
        "TREASURY_YIELD_10_YEAR": "DGS10",  # 10-Year Treasury Constant Maturity Rate
        "10_YEAR_TREASURY": "DGS10",
        "10_YEAR_TREASURY_YIELD": "DGS10",  # Alternative format
        "10YR_TREASURY": "DGS10",
        "10YR_TREASURY_YIELD": "DGS10",
        "10-YEAR_TREASURY_YIELD": "DGS10",
        "TREASURY_YIELD": "DGS10",  # Default to 10-year
        "TREASURY_YIELDS": "DGS10",
        "T-BOND_YIELD": "DGS10",
        "T_BOND_YIELD": "DGS10",
        "TREASURY_YIELD_30_YEAR": "DGS30",  # 30-Year Treasury Constant Maturity Rate
        "30_YEAR_TREASURY": "DGS30",
        "30_YEAR_TREASURY_YIELD": "DGS30",  # Alternative format
        "30YR_TREASURY": "DGS30",
        "30YR_TREASURY_YIELD": "DGS30",
        "CORPORATE_BOND_YIELDS": "BAMLC0A4CBBB",  # BBB Corporate Bond Yield
        "CORPORATE_BOND_YIELDS_BBB": "BAMLC0A4CBBB",
        "BBB_RATED_CORPORATE_BONDS": "BAMLC0A4CBBB",
        "BBB_CORPORATE_BONDS": "BAMLC0A4CBBB",
        "BBB_CORPORATE_BOND_YIELD": "BAMLC0A4CBBB",
        "BBB_BOND_YIELD": "BAMLC0A4CBBB",
        "CORPORATE_BONDS": "BAMLC0A4CBBB",
        "CORPORATE_BOND_YIELD": "BAMLC0A4CBBB",
        "BBB_RATED": "BAMLC0A4CBBB",

        # Housing
        "HOUSING_STARTS": "HOUST",
        "HOUSING_PRICES": "CSUSHPINSA",  # S&P/Case-Shiller U.S. National Home Price Index
        "HOUSING_PRICE_INDEX": "CSUSHPINSA",  # Alternative phrasing for housing prices
        "HOME_PRICES": "CSUSHPINSA",
        "HOME_PRICE_INDEX": "CSUSHPINSA",
        "HOUSE_PRICE_INDEX": "CSUSHPINSA",
        "CASE_SHILLER": "CSUSHPINSA",  # Case-Shiller Home Price Index
        "CASE-SHILLER": "CSUSHPINSA",  # Case-Shiller (alternative formatting)
        "BUILDING_PERMITS": "PERMIT",
        "MEDIAN_HOME_SALES_PRICE": "MSPUS",  # Median Sales Price of Houses Sold for the United States
        "MEDIAN_HOME_PRICE": "MSPUS",
        "MEDIAN_SALES_PRICE": "MSPUS",
        "HOME_SALES": "HSN1F",  # New One Family Houses Sold: United States
        "EXISTING_HOME_SALES": "EXHOSLUSM495S",  # Existing Home Sales

        # Consumption and Retail
        "RETAIL_SALES": "RSXFS",  # Advance Retail Sales: Retail Trade and Food Services
        "RETAIL_SALES_GROWTH": "RSXFS",  # Same series, growth can be calculated from data
        "CONSUMER_SPENDING": "PCE",  # Personal Consumption Expenditures
        "CONSUMER_EXPENDITURES": "PCE",
        "PERSONAL_CONSUMPTION": "PCE",
        "PERSONAL_CONSUMPTION_EXPENDITURES": "PCE",

        # Consumer Sentiment
        "CONSUMER_CONFIDENCE": "UMCSENT",  # University of Michigan: Consumer Sentiment
        "CONSUMER_SENTIMENT": "UMCSENT",
        "CONSUMER_CONFIDENCE_INDEX": "UMCSENT",

        # Savings and Income
        "SAVINGS_RATE": "PSAVERT",  # Personal Saving Rate
        "PERSONAL_SAVINGS_RATE": "PSAVERT",
        "DISPOSABLE_INCOME": "DPI",  # Disposable Personal Income
        "DISPOSABLE_PERSONAL_INCOME": "DPI",
        "REAL_DISPOSABLE_PERSONAL_INCOME": "DSPIC96",  # Real Disposable Personal Income
        "REAL_DISPOSABLE_INCOME": "DSPIC96",
        "REAL_DISPOSABLE_PERSONAL_INCOME_PER_CAPITA": "A229RX0",  # Real DPI per capita
        "REAL_DISPOSABLE_INCOME_PER_CAPITA": "A229RX0",

        # Industrial Production
        "INDUSTRIAL_PRODUCTION": "INDPRO",
        "CAPACITY_UTILIZATION": "TCU",  # Capacity Utilization: Total Industry
        "CAPACITY_UTILIZATION_RATE": "TCU",

        # Trade
        "IMPORTS": "IMPGS",  # Imports of Goods and Services
        "EXPORTS": "EXPGS",  # Exports of Goods and Services
        "TRADE_DEFICIT": "BOPGSTB",  # Trade Balance: Goods and Services
        "TRADE_BALANCE": "BOPGSTB",  # Trade Balance: Goods and Services (alias)
        "TRADE_BALANCE_DEFICIT": "BOPGSTB",  # Trade Balance (alias)

        # Corporate and Business
        "CORPORATE_PROFITS": "CP",
        "BUSINESS_INVENTORIES": "BUSINV",  # Total Business Inventories
        "MANUFACTURING_OUTPUT": "IPB50001N",  # Industrial Production: Manufacturing

        # Construction
        "CONSTRUCTION_SPENDING": "TTLCONS",  # Total Construction Spending
        "CONSTRUCTION_SPENDING_TOTAL": "TTLCONS",

        # Productivity and Wages
        "PRODUCTIVITY": "OPHNFB",  # Nonfarm Business Sector: Labor Productivity
        "LABOR_PRODUCTIVITY": "OPHNFB",
        "LABOUR_PRODUCTIVITY": "OPHNFB",  # UK spelling
        "OUTPUT_PER_HOUR": "OPHNFB",
        "GDP_PER_HOUR": "OPHNFB",
        "WORKER_PRODUCTIVITY": "OPHNFB",
        "NONFARM_PRODUCTIVITY": "OPHNFB",
        "NONFARM_LABOR_PRODUCTIVITY": "OPHNFB",
        "PRODUCTIVITY_GROWTH": "OPHNFB",  # Growth can be derived from the data
        "LABOR_PRODUCTIVITY_GROWTH": "OPHNFB",
        "MANUFACTURING_PRODUCTIVITY": "MPU4900063",  # Manufacturing Sector: Labor Productivity
        "MANUFACTURING_OUTPUT_PER_HOUR": "MPU4900063",
        "UNIT_LABOR_COST": "ULCNFB",  # Nonfarm Business Sector: Unit Labor Cost
        "UNIT_LABOUR_COST": "ULCNFB",  # UK spelling
        "ULC": "ULCNFB",
        "WAGES": "CES0500000003",  # Average Hourly Earnings of All Employees, Total Private
        "AVERAGE_HOURLY_EARNINGS": "CES0500000003",
        "WAGE_GROWTH": "CES0500000003",

        # Money Supply
        "M1": "M1SL",  # M1 Money Stock
        "M2": "M2SL",  # M2 Money Stock
        "M2_MONEY_SUPPLY": "M2SL",
        "M2_GROWTH": "M2SL",  # Growth rate can be calculated from M2 data
        "M2_MONEY_SUPPLY_GROWTH": "M2SL",
        "M2_MONEY_SUPPLY_GROWTH_RATE": "M2SL",

        # Debt
        "HOUSEHOLD_DEBT": "HDTGPDUSQ163N",  # Household Debt to GDP

        # INFRASTRUCTURE FIX: Consumer credit is different from household debt
        # Consumer credit = unsecured credit (credit cards, personal loans, auto loans)
        # Household debt = all household liabilities (includes mortgages)
        "CONSUMER_CREDIT": "TOTALSL",  # Total Consumer Credit Owned and Securitized
        "CONSUMER_CREDIT_OUTSTANDING": "TOTALSL",
        "TOTAL_CONSUMER_CREDIT": "TOTALSL",
        "CONSUMER_DEBT": "TOTALSL",  # Changed from HDTGPDUSQ163N
        "CONSUMER_LOANS": "TOTALSL",
        "REVOLVING_CREDIT": "REVOLSL",  # Revolving Consumer Credit (credit cards)
        "CREDIT_CARD_DEBT": "REVOLSL",
        "NON_REVOLVING_CREDIT": "NONREVSL",  # Nonrevolving Consumer Credit (auto, student loans)

        # Economic Indicators
        "RECESSION": "USREC",
        "NEW_ORDERS": "NEWORDER",  # Manufacturers' New Orders: Durable Goods

        # Exchange Rates (daily)
        "EXCHANGE_RATE_CNY": "DEXCHUS",  # China/US Exchange Rate
        "EXCHANGE_RATE_CHINA": "DEXCHUS",
        "CNY_USD": "DEXCHUS",
        "YUAN_USD": "DEXCHUS",
        "CHINESE_YUAN": "DEXCHUS",
        "YUAN_TO_USD": "DEXCHUS",
        "USD_CNY": "DEXCHUS",
        "EXCHANGE_RATE_EUR": "DEXUSEU",  # US/Euro Exchange Rate
        "EUR_USD": "DEXUSEU",
        "USD_EUR": "DEXUSEU",
        "EXCHANGE_RATE_GBP": "DEXUSUK",  # US/UK Exchange Rate
        "GBP_USD": "DEXUSUK",
        "USD_GBP": "DEXUSUK",
        "EXCHANGE_RATE_JPY": "DEXJPUS",  # Japan/US Exchange Rate
        "JPY_USD": "DEXJPUS",
        "USD_JPY": "DEXJPUS",
        "YEN_USD": "DEXJPUS",
        "EXCHANGE_RATE_CAD": "DEXCAUS",  # Canada/US Exchange Rate
        "CAD_USD": "DEXCAUS",
        "USD_CAD": "DEXCAUS",

        # Commodities
        # NOTE: FRED does NOT have gold or silver spot prices. Precious metal prices
        # should be routed to commodity providers (CoinGecko, etc.).
        # Dynamic search fallback will gracefully fail for gold/silver, allowing
        # the system to try alternative providers.
        "GOLD_ORE": "WPU10210501",  # Gold Ores Producer Price Index (for mining data)
        "GOLD_ORE_PPI": "WPU10210501",
        "SILVER_ORE": "WPU10210601",  # Silver Ores Producer Price Index (for mining data)
        "SILVER_ORE_PPI": "WPU10210601",
        "OIL_PRICE": "DCOILWTICO",  # Crude Oil Prices: West Texas Intermediate (WTI)
        "CRUDE_OIL": "DCOILWTICO",
        "WTI": "DCOILWTICO",
        "OIL_WTI": "DCOILWTICO",
        "NATURAL_GAS": "DHHNGSP",  # Henry Hub Natural Gas Spot Price
        "NATURAL_GAS_PRICE": "DHHNGSP",
        "COPPER": "PCOPPUSDM",  # Global price of Copper
        "COPPER_PRICE": "PCOPPUSDM",

        # Stock Market Indices
        "SP500": "SP500",  # S&P 500 Index
        "S&P500": "SP500",
        "S&P_500": "SP500",
        "SPX": "SP500",
        "SP_500": "SP500",
        "S_AND_P_500": "SP500",
        "STOCK_MARKET": "SP500",  # Default to S&P 500
        "DOW_JONES": "DJIA",  # Dow Jones Industrial Average
        "DJIA": "DJIA",
        "DOW": "DJIA",
        "NASDAQ": "NASDAQCOM",  # NASDAQ Composite Index
        "NASDAQ_COMPOSITE": "NASDAQCOM",
        "VIX": "VIXCLS",  # CBOE Volatility Index
        "VOLATILITY_INDEX": "VIXCLS",
        "VOLATILITY": "VIXCLS",
        "WILSHIRE_5000": "WILL5000INDFC",  # Wilshire 5000 Total Market Full Cap Index
        "WILSHIRE": "WILL5000INDFC",
    }

    FREQUENCY_MAP: Dict[str, str] = {
        "Daily": "daily",
        "Weekly": "weekly",
        "Monthly": "monthly",
        "Quarterly": "quarterly",
        "Annual": "annual",
        "Semiannual": "semiannual",
    }

    @property
    def provider_name(self) -> str:
        """Return canonical provider name for logging and routing."""
        return "FRED"

    def __init__(self, api_key: Optional[str], metadata_search_service=None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)  # Initialize BaseProvider
        self.api_key = api_key
        if not self.api_key:
            # We mirror the JS behavior (warn instead of failing).
            print("⚠️  FRED API key not provided. Some features may be limited.")
        settings = get_settings()
        self.base_url = settings.fred_base_url.rstrip("/")
        self.metadata_search = metadata_search_service  # Optional: for future integration
        # Cache for dynamic series search results to avoid redundant API calls
        self._search_cache: Dict[str, str] = {}

    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Implementation of BaseProvider's abstract method.

        Routes to fetch_series with appropriate parameters.
        """
        return await self.fetch_series(params)

    async def _search_series_dynamic(self, search_text: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search FRED series using the series/search API endpoint.

        This is the GENERAL solution that allows dynamic discovery of ANY FRED series,
        not just those in the hardcoded SERIES_MAPPINGS. This enables the system to
        find series for new indicators without code changes.

        Args:
            search_text: Natural language search terms (e.g., "gold price", "S&P 500")
            limit: Maximum number of results to return

        Returns:
            List of series metadata dicts with id, title, frequency, units, popularity
        """
        if not self.api_key:
            return []

        try:
            client = get_http_client()
            response = await client.get(
                f"{self.base_url}/series/search",
                params={
                    "search_text": search_text,
                    "api_key": self.api_key,
                    "file_type": "json",
                    "limit": limit,
                    "order_by": "popularity",  # Prefer popular/authoritative series
                    "sort_order": "desc",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()

            series_list = data.get("seriess", [])
            logger.info(f"FRED dynamic search for '{search_text}' found {len(series_list)} series")

            return series_list

        except Exception as e:
            logger.warning(f"FRED series search failed for '{search_text}': {e}")
            return []

    def _rank_series_relevance(self, series: Dict[str, Any], search_terms: List[str]) -> float:
        """
        Rank a FRED series by relevance to search terms.

        This implements intelligent ranking to pick the BEST series match, not just
        the first match. Considers title match, popularity, and series characteristics.

        Args:
            series: FRED series metadata
            search_terms: List of normalized search terms

        Returns:
            Relevance score (higher is better)
        """
        score = 0.0
        title_lower = series.get("title", "").lower()
        series_id_lower = series.get("id", "").lower()

        # Exact title match bonus
        for term in search_terms:
            if term in title_lower:
                score += 10.0
            if term in series_id_lower:
                score += 5.0

        # Popularity bonus (FRED provides this)
        popularity = series.get("popularity", 0)
        if popularity > 0:
            score += min(popularity / 10, 5.0)  # Cap at 5 points

        # Prefer actual price/value series over indices or producer prices
        if "price" in title_lower and "producer" not in title_lower:
            score += 3.0
        if "spot" in title_lower or "fixing" in title_lower:
            score += 5.0  # Spot prices are usually what users want
        if "index" in title_lower and "price index" not in title_lower:
            score -= 2.0  # Slight penalty for generic indices
        if "ppi" in title_lower or "producer price" in title_lower:
            score -= 3.0  # Users asking for "gold price" don't want PPI

        # Prefer daily/monthly over annual for commodity prices
        frequency = series.get("frequency_short", "").lower()
        if frequency in ["d", "w", "m"]:
            score += 1.0

        # Penalize discontinued series
        if series.get("observation_end"):
            import datetime
            try:
                end_date = datetime.datetime.strptime(series["observation_end"], "%Y-%m-%d")
                days_old = (datetime.datetime.now() - end_date).days
                if days_old > 365:  # Discontinued over a year ago
                    score -= 5.0
            except:
                pass

        return score

    async def _find_best_series(self, indicator: str) -> Optional[str]:
        """
        Find the best FRED series ID for a natural language indicator using dynamic search.

        This is the GENERAL fallback when hardcoded mappings don't exist. Resolution order:
        1. Check in-memory cache
        2. Search local indicator database (FTS5, 138K+ FRED series) - FAST
        3. Fall back to FRED API search - SLOWER, network call

        Args:
            indicator: Natural language indicator (e.g., "gold price", "S&P 500 index")

        Returns:
            Best matching FRED series ID, or None if no good match found
        """
        # Check cache first
        cache_key = indicator.upper().strip()
        if cache_key in self._search_cache:
            logger.info(f"Using cached FRED series for '{indicator}': {self._search_cache[cache_key]}")
            return self._search_cache[cache_key]

        # STEP 1: Search local indicator database (FTS5, instant)
        try:
            from ..services.indicator_lookup import get_indicator_lookup
            lookup = get_indicator_lookup()
            db_results = lookup.search(indicator, provider="FRED", limit=10)
            if db_results:
                # Rank results and pick best match
                best = db_results[0]  # Already ranked by relevance
                series_id = best.get("code")
                if series_id:
                    logger.info(f"Indicator database match for '{indicator}': {series_id} ({best.get('name', 'N/A')})")
                    self._search_cache[cache_key] = series_id
                    return series_id
        except Exception as e:
            logger.warning(f"Indicator database search failed: {e}")

        # STEP 2: Fall back to FRED API search (slower, network call)
        series_list = await self._search_series_dynamic(indicator, limit=20)

        if not series_list:
            return None

        # Normalize search terms for relevance ranking
        search_terms = [t.lower().strip() for t in re.split(r'[\s_-]+', indicator) if t]

        # Rank all series by relevance
        ranked = [(s, self._rank_series_relevance(s, search_terms)) for s in series_list]
        ranked.sort(key=lambda x: x[1], reverse=True)

        # Log top matches for debugging
        if ranked:
            top_3 = ranked[:3]
            logger.info(f"Top FRED series matches for '{indicator}':")
            for s, score in top_3:
                logger.info(f"  [{score:.1f}] {s['id']}: {s['title']}")

        # Return best match if score is reasonable
        if ranked and ranked[0][1] >= 5.0:
            best_series = ranked[0][0]["id"]
            # Cache the result
            self._search_cache[cache_key] = best_series
            logger.info(f"Dynamic FRED series discovery: '{indicator}' -> '{best_series}'")
            return best_series

        logger.warning(f"No good FRED series match for '{indicator}' (best score: {ranked[0][1] if ranked else 0:.1f})")
        return None

    def _series_id_with_transform(self, indicator: Optional[str], series_id: Optional[str]) -> tuple[str, Optional[str]]:
        """
        Map an indicator name to a FRED series ID and optional transformation.

        Args:
            indicator: Natural language indicator name (e.g., "GDP growth", "unemployment rate")
            series_id: Explicit FRED series ID (e.g., "GDP", "UNRATE")

        Returns:
            Tuple of (series_id, transformation) where transformation can be None or 'pc1', 'pch', etc.

        Raises:
            ValueError: If no valid series ID can be determined
        """
        # If explicit series ID provided, use it (check for transformation suffix)
        if series_id:
            if ":" in series_id:
                parts = series_id.split(":", 1)
                return parts[0], parts[1]
            return series_id, None

        if not indicator:
            raise ValueError("Series ID or indicator is required")

        # Normalize indicator name: uppercase, replace spaces with underscores
        # Also strip common words that don't affect meaning
        normalized = indicator.upper().strip()

        # Remove common filler words
        normalized = (normalized
            .replace(" RATE", "_RATE")  # "unemployment rate" -> "unemployment_rate"
            .replace(" INDEX", "_INDEX")
            .replace(" GROWTH", "_GROWTH")
            .replace(" PRICE", "_PRICE")
            .replace(" PRICES", "_PRICES")
            .replace(" ", "_"))

        # Try exact match first
        if normalized in self.SERIES_MAPPINGS:
            mapping = self.SERIES_MAPPINGS[normalized]
            # Check if mapping includes transformation (e.g., "CPIAUCSL:pc1")
            if ":" in mapping:
                parts = mapping.split(":", 1)
                return parts[0], parts[1]
            return mapping, None

        # Try without trailing modifiers (e.g., "GDP_GROWTH" if "GDP_GROWTH_RATE" fails)
        if normalized.endswith("_RATE"):
            base = normalized[:-5]  # Remove "_RATE"
            if base in self.SERIES_MAPPINGS:
                mapping = self.SERIES_MAPPINGS[base]
                if ":" in mapping:
                    parts = mapping.split(":", 1)
                    return parts[0], parts[1]
                return mapping, None

        # Handle institutional prefixes (e.g., "FEDERAL_RESERVE_INTEREST_RATE" -> "INTEREST_RATE")
        # This is a GENERAL solution for any institutional/central bank prefix
        institutional_prefixes = [
            "FEDERAL_RESERVE_", "FED_", "BANK_OF_", "ECB_", "BOJ_", "BOE_",
            "CENTRAL_BANK_", "US_", "USA_", "UNITED_STATES_"
        ]
        core_term = normalized
        for prefix in institutional_prefixes:
            if normalized.startswith(prefix):
                core_term = normalized[len(prefix):]
                # Try the core term
                if core_term in self.SERIES_MAPPINGS:
                    mapping = self.SERIES_MAPPINGS[core_term]
                    if ":" in mapping:
                        parts = mapping.split(":", 1)
                        return parts[0], parts[1]
                    return mapping, None
                break  # Only strip one prefix

        # Try common variations
        variations = [
            normalized.replace("_GROWTH", ""),  # "GDP_GROWTH" -> "GDP"
            normalized.replace("_INDEX", ""),   # "CONSUMER_CONFIDENCE_INDEX" -> "CONSUMER_CONFIDENCE"
            normalized + "_RATE",               # "UNEMPLOYMENT" -> "UNEMPLOYMENT_RATE"
            core_term,                          # The core term after prefix stripping
            core_term.replace("_RATE", ""),     # Core term without _RATE suffix
        ]

        for variation in variations:
            if variation in self.SERIES_MAPPINGS:
                mapping = self.SERIES_MAPPINGS[variation]
                if ":" in mapping:
                    parts = mapping.split(":", 1)
                    return parts[0], parts[1]
                return mapping, None

        # No static mapping found - return None to signal dynamic search should be tried
        # This allows the async caller to attempt FRED series/search API
        # Note: We no longer assume alphanumeric strings are valid series IDs.
        # Dynamic search will find the correct series, or provide a helpful error.
        return None, None

    async def _resolve_series_id_async(
        self, indicator: Optional[str], series_id: Optional[str]
    ) -> Tuple[str, Optional[str]]:
        """
        Async version of series ID resolution with dynamic search fallback.

        This method implements the GENERAL solution for FRED indicator resolution:
        1. First tries static SERIES_MAPPINGS (fast, known mappings)
        2. Falls back to FRED series/search API for dynamic discovery
        3. Raises DataNotAvailableError only if both approaches fail

        This ensures ANY valid FRED series can be discovered without code changes.

        Args:
            indicator: Natural language indicator name
            series_id: Explicit FRED series ID (takes precedence)

        Returns:
            Tuple of (series_id, transformation)

        Raises:
            DataNotAvailableError: If no matching series can be found
        """
        # Try static mappings first (synchronous, fast)
        result_series, transform = self._series_id_with_transform(indicator, series_id)

        if result_series is not None:
            return result_series, transform

        # PHASE B: Use IndicatorResolver as unified resolution (before dynamic search)
        # This leverages the 330K+ indicator database with FTS5 search
        if indicator:
            try:
                from ..services.indicator_resolver import get_indicator_resolver
                resolver = get_indicator_resolver()
                resolved = resolver.resolve(indicator, provider="FRED")
                if resolved and resolved.confidence >= 0.7:
                    logger.info(f"🔍 IndicatorResolver: FRED '{indicator}' → '{resolved.code}' (confidence: {resolved.confidence:.2f}, source: {resolved.source})")
                    return resolved.code, None
            except Exception as e:
                logger.debug(f"IndicatorResolver failed, continuing to dynamic search: {e}")

        # Static mapping and IndicatorResolver failed - try dynamic search (async, FRED API call)
        if indicator:
            logger.info(f"No static mapping for '{indicator}', attempting dynamic FRED series search...")
            dynamic_series = await self._find_best_series(indicator)
            if dynamic_series:
                logger.info(f"Dynamic discovery successful: '{indicator}' -> '{dynamic_series}'")
                return dynamic_series, None

        # Both approaches failed - provide helpful error
        indicator_lower = indicator.lower() if indicator else ""

        # Check if this is a precious metals/commodity spot price query
        precious_metals = ["gold", "silver", "platinum", "palladium"]
        if any(metal in indicator_lower for metal in precious_metals):
            raise DataNotAvailableError(
                f"FRED does not have spot prices for precious metals like gold or silver. "
                f"For commodity price INDICES (not spot prices), try: "
                f"'Producer Price Index' or 'PPI commodities' which maps to PPIACO. "
                f"For real-time gold/silver spot prices, use dedicated services like kitco.com or goldprice.org."
            )

        raise DataNotAvailableError(
            f"Unknown FRED indicator: '{indicator}'. "
            f"Dynamic search did not find a good match. "
            f"Please use a known indicator name (e.g., 'GDP', 'unemployment', 'inflation', 'housing starts') "
            f"or provide an explicit FRED series ID via the 'seriesId' parameter. "
            f"See https://fred.stlouisfed.org for available series."
        )

    def _series_id(self, indicator: Optional[str], series_id: Optional[str]) -> str:
        """Legacy method for backward compatibility - returns just the series ID.

        NOTE: This synchronous method does NOT support dynamic FRED search fallback.
        For full functionality with dynamic discovery, use fetch_series() instead.
        """
        series, _ = self._series_id_with_transform(indicator, series_id)
        if series is None:
            raise DataNotAvailableError(
                f"Unknown FRED indicator: '{indicator}'. "
                f"Use fetch_series() for dynamic search support, or provide an explicit FRED series ID."
            )
        return series

    def _map_frequency(self, fred_frequency: str) -> str:
        return self.FREQUENCY_MAP.get(fred_frequency, fred_frequency.lower())

    def _normalize_percentage_values(self, data: list[dict], series_id: str, unit: str) -> list[dict]:
        """
        Normalize percentage values that are stored as decimals.
        If values are < 1.5 in absolute value, multiply by 100.

        Args:
            data: List of data points with 'date' and 'value' keys
            series_id: FRED series ID for detection logic
            unit: Unit string from FRED metadata

        Returns:
            Normalized data points with percentage values (e.g., 2.5 instead of 0.025)
        """
        if not data:
            return data

        # Check if values look like decimals (all non-null absolute values < 1.5)
        non_null_values = [abs(d['value']) for d in data if d['value'] is not None]
        if not non_null_values:
            return data

        max_value = max(non_null_values)

        # If max value < 1.5, likely stored as decimals (0.025 = 2.5%)
        # Exception: Negative values can be < -1, so we use absolute values
        if max_value < 1.5:
            logger.info(f"Normalizing percentage values for series: {series_id} (max value: {max_value})")
            return [
                {'date': d['date'], 'value': d['value'] * 100 if d['value'] is not None else None}
                for d in data
            ]

        return data

    async def fetch_series(
        self, params: Dict[str, Any]
    ) -> NormalizedData:
        # Use async resolver with dynamic search fallback (GENERAL solution)
        target_series, transformation = await self._resolve_series_id_async(
            params.get("indicator"), params.get("seriesId")
        )

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        info_response = await client.get(
            f"{self.base_url}/series",
            params={
                "series_id": target_series,
                "api_key": self.api_key,
                "file_type": "json",
            },
            timeout=15.0,
        )
        info_response.raise_for_status()
        info_payload = info_response.json()
        if not info_payload.get("seriess"):
            raise DataNotAvailableError(f"FRED series '{target_series}' not found. Please check the series ID or try a different indicator.")
        info = info_payload["seriess"][0]

        obs_params = {
            "series_id": target_series,
            "api_key": self.api_key,
            "file_type": "json",
        }
        if params.get("startDate"):
            obs_params["observation_start"] = params["startDate"]
        if params.get("endDate"):
            obs_params["observation_end"] = params["endDate"]

        # Add transformation if specified (e.g., 'pc1' for percent change from year ago)
        if transformation:
            obs_params["units"] = transformation

        obs_response = await client.get(
            f"{self.base_url}/series/observations", params=obs_params, timeout=15.0
        )
        obs_response.raise_for_status()
        observations = obs_response.json().get("observations", [])

        # Build API URL for metadata (without exposing actual API key)
        api_url_params = {
            "series_id": target_series,
            "file_type": "json",
        }
        if params.get("startDate"):
            api_url_params["observation_start"] = params["startDate"]
        if params.get("endDate"):
            api_url_params["observation_end"] = params["endDate"]

        query_string = "&".join(f"{key}={value}" for key, value in api_url_params.items())
        api_url = f"{self.base_url}/series/observations?{query_string}&api_key=***"

        unit = info.get("units", "")
        indicator_title = info["title"]

        # Override unit and title if transformation was applied
        if transformation == "pc1":
            unit = "Percent Change from Year Ago"
            indicator_title = f"{info['title']} (YoY % Change)"
        elif transformation == "pch":
            unit = "Percent Change"
            indicator_title = f"{info['title']} (% Change)"
        elif transformation == "log":
            unit = "Natural Log"
            indicator_title = f"{info['title']} (Log)"

        # Human-readable URL for data verification on FRED website
        source_url = f"https://fred.stlouisfed.org/series/{target_series}"

        # Extract enhanced metadata from FRED API response
        seasonal_adjustment = info.get("seasonal_adjustment", None)
        seasonal_adj_short = info.get("seasonal_adjustment_short", None)
        notes = info.get("notes", None)

        # Determine data type from series characteristics
        data_type = None
        title_lower = info["title"].lower()
        unit_lower = unit.lower()
        if "percent change" in title_lower or "growth rate" in title_lower:
            data_type = "Percent Change"
        elif "change" in title_lower:
            data_type = "Change"
        elif "index" in title_lower or "index" in unit_lower:
            data_type = "Index"
        elif "rate" in title_lower and "percent" in unit_lower:
            data_type = "Rate"
        else:
            data_type = "Level"

        # Determine price type (real vs nominal)
        price_type = None
        if "real" in title_lower or "chained" in title_lower or "constant" in title_lower:
            price_type = "Real (inflation-adjusted)"
        elif "nominal" in title_lower or "current" in title_lower:
            price_type = "Nominal (current prices)"

        # Parse notes into list (split by periods/semicolons for readability)
        notes_list = None
        if notes:
            # Truncate very long notes and split into sentences
            notes_text = notes[:500] if len(notes) > 500 else notes
            notes_list = [n.strip() for n in notes_text.split('.') if n.strip()][:3]

        metadata = Metadata(
            source="FRED",
            indicator=info["title"],
            country="US",
            frequency=self._map_frequency(info["frequency"]),
            unit=unit,
            lastUpdated=info.get("last_updated", ""),
            seriesId=target_series,
            apiUrl=api_url,
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=seasonal_adjustment or seasonal_adj_short,
            dataType=data_type,
            priceType=price_type,
            description=info.get("notes", "")[:200] if info.get("notes") else None,
            notes=notes_list,
            startDate=info.get("observation_start"),
            endDate=info.get("observation_end"),
        )

        data_points = [
            {
                "date": obs.get("date", ""),
                "value": None if obs.get("value") in (None, ".", "") else float(obs["value"]),
            }
            for obs in observations
            if obs.get("date")  # Skip observations without dates
        ]

        # Normalize percentage values (FRED sometimes stores as decimals)
        if "percent" in unit.lower() or "rate" in unit.lower():
            data_points = self._normalize_percentage_values(data_points, target_series, unit)

        return NormalizedData(metadata=metadata, data=data_points)

