from __future__ import annotations

from typing import Dict, List, Optional
import logging

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from ..services.rate_limiter import wait_for_provider, record_provider_request
from .base import BaseProvider

logger = logging.getLogger(__name__)


class StatsCanProvider(BaseProvider):
    """Statistics Canada Web Data Service (WDS) provider.

    Supports two methods:
    1. Vector API: For national-level aggregates (fast, simple)
    2. WDS Coordinate API: For provincial/dimensional data (requires metadata discovery)

    No API key required for basic access.
    """

    # Keyword synonyms for better search matching
    # Maps indicator keywords to alternative search terms
    KEYWORD_SYNONYMS: Dict[str, List[str]] = {
        'HOUSING_PRICE': ['housing price index', 'new housing price', 'NHPI', 'house price'],
        'HOUSING_PRICE_INDEX': ['housing price index', 'new housing price', 'NHPI'],
        'BUILDING_PERMITS': ['building permits', 'construction permits', 'building authorization'],
        'RETAIL_SALES': ['retail trade', 'retail sales', 'retail commodity'],
        'MANUFACTURING_SALES': ['manufacturing sales', 'manufacturing shipments'],
        'EMPLOYMENT': ['employment', 'labour force', 'jobs', 'working'],
        'EMPLOYMENT_BY_AGE': ['employment by age', 'labour force by age', 'employment age group'],
        'GDP_BY_INDUSTRY': ['gdp by industry', 'gdp industry', 'economic output by sector', 'sector gdp'],
        'GDP_INDUSTRY': ['gdp by industry', 'industry gdp', 'sector output'],
        'EXPORTS': ['exports', 'international trade', 'merchandise exports'],
        'IMPORTS': ['imports', 'international trade', 'merchandise imports'],
        'IMMIGRATION': ['immigration', 'immigrants', 'permanent residents', 'international migration'],
        'TRADE_BALANCE': ['trade balance', 'merchandise trade', 'international trade', 'trade surplus', 'trade deficit'],
        'AGRICULTURE': ['agriculture', 'farming', 'crop', 'livestock'],
        'FISHING': ['fishing', 'fisheries', 'aquaculture', 'seafood'],
        'FORESTRY': ['forestry', 'logging', 'timber', 'wood'],
        'OIL': ['oil', 'crude', 'petroleum', 'energy'],
        'CONSTRUCTION': ['construction', 'building', 'engineering'],
    }

    # Verified core indicators only (other indicators discovered via metadata search)
    # These vector IDs have been manually verified to be correct (2025-11-21)
    # All other indicators are discovered dynamically to avoid incorrect mappings
    VECTOR_MAPPINGS: Dict[str, int] = {
        # Core economic indicators (manually verified - July 2025)
        "GDP": 65201210,  # GDP at basic prices, all industries, seasonally adjusted annual rate, chained 2017 dollars
        "GDP_ALL_INDUSTRIES": 65201210,  # Alias for total GDP
        "UNEMPLOYMENT": 2062815,  # Unemployment rate, both sexes, 15 years and over
        "UNEMPLOYMENT_RATE": 2062815,
        "INFLATION": 41690973,  # CPI, all-items, year-over-year percentage change
        "INFLATION_RATE": 41690973,  # Alias for INFLATION
        "CPI": 41690914,  # CPI, all-items index (2002=100)
        "CONSUMER_PRICE_INDEX": 41690914,  # Alias for CPI
        "PRICE_INDEX": 41690914,  # Alias for CPI
        # Combined CPI/inflation aliases (LLM may parse as combined term)
        "CPI_INFLATION": 41690973,  # CPI year-over-year percentage change
        "CPI_INFLATION_RATE": 41690973,  # Same as above
        "CPI_RATE": 41690973,  # Alias
        "INFLATION_CPI": 41690973,  # Alias
        "CONSUMER_PRICE_INFLATION": 41690973,  # Alias
        "POPULATION": 1,  # Population estimates, quarterly (verified 2025-11)

        # GDP by Industry breakdowns (verified 2025-11-29)
        # Product 36100434: GDP at basic prices, by industry, monthly
        "GDP_GOODS_PRODUCING": 65201211,  # Goods-producing industries
        "GDP_GOODS": 65201211,  # Alias
        "GDP_GOODS_SECTOR": 65201211,  # Alias
        "GDP_SERVICES_PRODUCING": 65201212,  # Services-producing industries
        "GDP_SERVICES": 65201212,  # Alias
        "GDP_SERVICE_SECTOR": 65201212,  # Alias
        "GDP_BUSINESS_SECTOR": 65201213,  # Business sector industries
        "GDP_NON_BUSINESS_SECTOR": 65201214,  # Non-business sector industries
        "GDP_INDUSTRIAL_PRODUCTION": 65201217,  # Industrial production
        "GDP_MANUFACTURING": 65201219,  # Manufacturing
        "GDP_DURABLE_MANUFACTURING": 65201220,  # Durable manufacturing industries
        "GDP_NON_DURABLE_MANUFACTURING": 65201218,  # Non-durable manufacturing industries
        # GDP by industry aggregate (Product 36100402 - annual provincial GDP by industry)
        "GDP_BY_INDUSTRY": 65201211,  # Default to goods-producing as starting point
        "GDP_INDUSTRY": 65201211,  # Alias
        "GDP_ALL_INDUSTRIES": 2461968,  # All industries vector

        # Housing (subject 34 - Construction)
        "HOUSING_STARTS": 52300157,  # Housing starts (all areas), Canada and provinces, monthly (units: thousands) - CMHC data
        "HOUSING_STARTS_ALL_AREAS": 52300157,  # Housing starts (all areas), monthly
        "HOUSING_STARTS_CENTRES_10K": 52299896,  # Housing starts (centres 10k+), monthly

        # Housing Price Index - USE COORDINATE-BASED QUERY (vector is deprecated)
        # Set to None to trigger coordinate-based fetch via COORDINATE_PRODUCT_MAPPINGS
        "HOUSING_PRICE_INDEX": None,  # Use coordinate query (product 18100205)
        "NEW_HOUSING_PRICE_INDEX": None,  # Use coordinate query
        "NHPI": None,  # Use coordinate query
        "HOUSE_PRICE_INDEX": None,  # Use coordinate query

        # Immigration (Product 17100040 - Components of international migration, quarterly)
        # Use coordinate-based query
        "IMMIGRATION": None,  # Use coordinate query (product 17100040)
        "IMMIGRANTS": None,  # Use coordinate query
        "PERMANENT_RESIDENTS": None,  # Use coordinate query
        "IMMIGRATION_STATISTICS": None,  # Use coordinate query
        "INTERNATIONAL_MIGRATION": None,  # Use coordinate query

        # Retail Trade (Product 2010005602 - Monthly retail trade sales by industry)
        # This is the current active table (replaces deprecated 20100008)
        # For total retail (all industries), need to use coordinate-based query or discover vector
        # Vector 7631665 = Retail trade, Canada, unadjusted (from older table)
        "RETAIL_SALES": 7631665,  # Retail trade sales, Canada, total (may need dynamic discovery)
        "RETAIL_TRADE": 7631665,  # Alias
        "RETAIL_SALES_BY_SECTOR": None,  # Requires coordinate-based query with product 2010005602

        # Employment (Product 1410028702 - Labour force characteristics by age group, monthly)
        # Vector 2062816 = Employment, both sexes, 15 years and over, total
        "EMPLOYMENT": 2062816,  # Employment, both sexes, 15+ years, monthly
        "EMPLOYED": 2062816,  # Alias
        "EMPLOYMENT_BY_AGE": None,  # Requires coordinate-based query with product 1410028702

        # Merchandise Trade (Product 12100011 - International merchandise trade, monthly)
        # NOTE: Vectors 38226270, 38226235, 38226238 may need verification
        # Using dynamic discovery as fallback
        "TRADE_BALANCE": None,  # Use dynamic discovery (product 1210001101)
        "MERCHANDISE_TRADE_BALANCE": None,  # Use dynamic discovery
        "MERCHANDISE_TRADE": None,  # Use dynamic discovery
        "EXPORTS": 38226235,  # Domestic exports, monthly (keep for backward compatibility)
        "IMPORTS": 38226238,  # Imports, monthly (keep for backward compatibility)

        # Interest Rates - StatsCan doesn't publish policy rates (that's Bank of Canada)
        # However, they do have lending rates in Product 10100122
        # For now, mark as requiring dynamic discovery
        # "INTEREST_RATE": Will need to use dynamic discovery or recommend Bank of Canada data

        # Note: Retail by sector, Employment by age/industry, and other dimensional indicators
        # require coordinate-based queries (fetch_categorical_data or fetch_dynamic_data)

        # Labor Productivity (Table 36-10-0480-01: Labour productivity and related measures)
        # NOTE: Vector IDs need verification - using dynamic discovery for now
        # These are added to prevent false positives from metadata search
        "PRODUCTIVITY": None,  # Use dynamic discovery - Labour productivity, business sector
        "LABOR_PRODUCTIVITY": None,  # Use dynamic discovery
        "LABOUR_PRODUCTIVITY": None,  # UK/Canadian spelling
        "OUTPUT_PER_HOUR": None,  # Use dynamic discovery
        "PRODUCTIVITY_GROWTH": None,  # Use dynamic discovery - Labour productivity growth rate
        "LABOR_PRODUCTIVITY_GROWTH": None,  # Use dynamic discovery
        "LABOUR_PRODUCTIVITY_GROWTH": None,  # UK/Canadian spelling
        "MULTIFACTOR_PRODUCTIVITY": None,  # Use dynamic discovery
        "TOTAL_FACTOR_PRODUCTIVITY": None,  # Use dynamic discovery
        "TFP": None,  # Use dynamic discovery
        "UNIT_LABOR_COST": None,  # Use dynamic discovery
        "UNIT_LABOUR_COST": None,  # UK/Canadian spelling
        "ULC": None,  # Use dynamic discovery
    }

    # Coordinate-based product mappings for indicators that don't have vector IDs
    # Format: indicator_name -> (product_id, default_coordinate, description)
    # Coordinates are 10 dimensions separated by dots (e.g., "1.1.0.0.0.0.0.0.0.0")
    # Member ID 1 typically means "Total", "Canada", or "All"
    COORDINATE_PRODUCT_MAPPINGS: Dict[str, tuple] = {
        # Housing Price Index (Product 18100205 - New housing price index, monthly)
        # Dimension 0: Geography (1=Canada), Dimension 1: Index type (1=Total house and land)
        "HOUSING_PRICE_INDEX": ("18100205", "1.1.0.0.0.0.0.0.0.0", "New housing price index"),
        "NEW_HOUSING_PRICE_INDEX": ("18100205", "1.1.0.0.0.0.0.0.0.0", "New housing price index"),
        "NHPI": ("18100205", "1.1.0.0.0.0.0.0.0.0", "New housing price index"),
        "HOUSE_PRICE_INDEX": ("18100205", "1.1.0.0.0.0.0.0.0.0", "New housing price index"),

        # Immigration (Product 17100040 - Components of international migration, quarterly)
        # Dimension 0: Geography (1=Canada), Dimension 1: Component (1=Immigrants)
        "IMMIGRATION": ("17100040", "1.1.0.0.0.0.0.0.0.0", "International migration - Immigrants"),
        "IMMIGRANTS": ("17100040", "1.1.0.0.0.0.0.0.0.0", "International migration - Immigrants"),
        "PERMANENT_RESIDENTS": ("17100040", "1.1.0.0.0.0.0.0.0.0", "International migration - Immigrants"),
        "IMMIGRATION_STATISTICS": ("17100040", "1.1.0.0.0.0.0.0.0.0", "International migration - Immigrants"),
        "INTERNATIONAL_MIGRATION": ("17100040", "1.1.0.0.0.0.0.0.0.0", "International migration"),

        # Employment by age (Product 14100017 - Labour force characteristics by gender and age)
        # Dimension 0: Geography (1=Canada), Dimension 1: Labour force characteristic (3=Employment)
        # Dimension 2: Gender (1=Both sexes), Dimension 3: Age group (1=15+ years or all ages)
        "EMPLOYMENT_BY_AGE": ("14100017", "1.3.1.1.0.0.0.0.0.0", "Employment by age group"),
        "LABOUR_FORCE_BY_AGE": ("14100017", "1.3.1.1.0.0.0.0.0.0", "Labour force by age group"),
        "LABOR_FORCE_BY_AGE": ("14100017", "1.3.1.1.0.0.0.0.0.0", "Labour force by age group"),

        # Merchandise trade balance (Product 12100011 - International merchandise trade)
        # Dimensions: Geography(1=Canada), Trade(1=Import/2=Export/3=Balance), Basis(1=Customs/2=BOP),
        #             Seasonal(1=Unadj/2=Adj), Partners(1=All countries)
        # Note: Balance of Payments (2) with Seasonal adjustment (2) has data
        "TRADE_BALANCE": ("12100011", "1.3.2.2.1.0.0.0.0.0", "Merchandise trade balance"),
        "MERCHANDISE_TRADE_BALANCE": ("12100011", "1.3.2.2.1.0.0.0.0.0", "Merchandise trade balance"),
        "MERCHANDISE_TRADE": ("12100011", "1.2.2.2.1.0.0.0.0.0", "Merchandise exports"),
    }

    # Runtime cache for vector ID -> product ID mappings (populated by metadata search)
    # This cache is built dynamically when indicators are discovered via metadata search
    # Pre-populated with common indicators to avoid slow API resolution (2025-11-21)
    # IMPORTANT: Product IDs must be 10-digit numbers for table viewer URLs
    # Format: Subject(2) + Product type(2) + Specific(4) + Version(2) = 10 digits
    # Example: 14-10-0287-01 → 1410028701
    PRODUCT_ID_CACHE: Dict[int, str] = {
        # Demographics (Subject 17)
        1: "1710000501",  # Population → Population estimates by age and gender (quarterly)

        # GDP (Subject 36 - National accounts)
        65201210: "3610043401",  # GDP at basic prices, all industries
        65201211: "3610043401",  # GDP goods-producing industries
        65201212: "3610043401",  # GDP services-producing industries
        65201213: "3610043401",  # GDP business sector
        65201214: "3610043401",  # GDP non-business sector
        65201217: "3610043401",  # GDP industrial production
        65201218: "3610043401",  # GDP non-durable manufacturing
        65201219: "3610043401",  # GDP manufacturing
        65201220: "3610043401",  # GDP durable manufacturing

        # Labour (Subject 14 - Labour)
        2062815: "1410028701",  # Unemployment rate → Labour force characteristics, monthly (14-10-0287-01)
        14100239: "1410023901",  # Employment by industry
        14100287: "1410028701",  # Labour force characteristics

        # Prices (Subject 18 - Prices and price indexes)
        41690914: "1810000401",  # CPI all-items index → Consumer Price Index, monthly (18-10-0004-01)
        41690973: "1810000401",  # CPI inflation rate → Consumer Price Index (same product, different vector)

        # Housing (Subject 34 - Construction)
        52300157: "3410015801",  # Housing starts (all areas) → CMHC housing starts (34-10-0158-01)
        52299896: "3410015601",  # Housing starts (10k+) → CMHC housing starts in centres 10,000+ (34-10-0156-01)
        111955410: "1810020501",  # Housing price index → New housing price index (18-10-0205-01)

        # Immigration (Subject 17 - Population and demography)
        # NOTE: Vector 484 deprecated - dynamic discovery will use product directly
        # 484: "1710004001",  # Immigration → Components of international migration (17-10-0040-01)

        # Retail Trade (Subject 20 - Retail trade)
        7631665: "2010000801",  # Retail trade → Retail trade sales (20-10-0008-01)

        # Employment (Subject 14 - Labour)
        2062816: "1410028701",  # Employment → Labour force characteristics (14-10-0287-01)

        # Trade (Subject 12 - International trade)
        # NOTE: Trade vectors may be deprecated - dynamic discovery will use product
        38226235: "1210001101",  # Exports → International merchandise trade (12-10-0011-01)
        38226238: "1210001101",  # Imports → International merchandise trade (12-10-0011-01)

        # Labour by age group (Product 1410028702 - Labour force characteristics by age group)
        # Note: This table has dimensional structure requiring coordinate-based queries

        # Note: Provincial GDP queries should use metadata search to discover product 36100402 (Real GDP by province)
    }

    FREQUENCY_MAP: Dict[int, str] = {
        1: "daily",
        3: "weekly",
        6: "monthly",
        9: "quarterly",
        12: "annual",
    }

    SCALAR_FACTOR_MAP: Dict[int, str] = {
        0: "units",        # No multiplier
        1: "tens",         # 10
        2: "hundreds",     # 100
        3: "thousands",    # 1,000
        4: "ten thousands",     # 10,000
        5: "hundred thousands", # 100,000
        6: "millions",     # 1,000,000
        7: "ten millions",      # 10,000,000
        8: "hundred millions",  # 100,000,000
        9: "billions",     # 1,000,000,000
    }

    # Dimension member IDs for WDS coordinate-based queries
    # Based on product 17100005 (Population estimates by age and gender)

    # Dimension 0: Geography (provinces/territories)
    GEOGRAPHY_MEMBER_IDS: Dict[str, int] = {
        "CANADA": 1,
        "NEWFOUNDLAND AND LABRADOR": 2,
        "NEWFOUNDLAND": 2,
        "PRINCE EDWARD ISLAND": 3,
        "PEI": 3,
        "NOVA SCOTIA": 4,
        "NEW BRUNSWICK": 5,
        "QUEBEC": 6,
        "ONTARIO": 7,
        "MANITOBA": 8,
        "SASKATCHEWAN": 9,
        "ALBERTA": 10,
        "BRITISH COLUMBIA": 11,
        "BC": 11,
        "YUKON": 12,
        "NORTHWEST TERRITORIES": 13,
        "NWT": 13,
        "NUNAVUT": 14,
    }

    # Dimension 1: Gender/Sex
    GENDER_MEMBER_IDS: Dict[str, int] = {
        "TOTAL": 1,
        "BOTH": 1,
        "ALL": 1,
        "BOTH SEXES": 1,
        "MEN": 2,
        "MALE": 2,
        "MALES": 2,
        "MEN+": 2,
        "WOMEN": 3,
        "FEMALE": 3,
        "FEMALES": 3,
        "WOMEN+": 3,
    }

    # Dimension 2: Age groups
    # Note: There are 139 age group members. Listing key ones here.
    # For a complete list, query the cube metadata dynamically.
    AGE_GROUP_MEMBER_IDS: Dict[str, int] = {
        "ALL AGES": 1,
        "ALL": 1,
        "0 TO 4 YEARS": 2,
        "0-4": 2,
        "5 TO 9 YEARS": 3,
        "5-9": 3,
        "10 TO 14 YEARS": 4,
        "10-14": 4,
        "15 TO 19 YEARS": 5,
        "15-19": 5,
        "20 TO 24 YEARS": 6,
        "20-24": 6,
        "25 TO 29 YEARS": 7,
        "25-29": 7,
        "30 TO 34 YEARS": 8,
        "30-34": 8,
        "35 TO 39 YEARS": 9,
        "35-39": 9,
        "40 TO 44 YEARS": 10,
        "40-44": 10,
        "45 TO 49 YEARS": 11,
        "45-49": 11,
        "50 TO 54 YEARS": 12,
        "50-54": 12,
        "55 TO 59 YEARS": 13,
        "55-59": 13,
        "60 TO 64 YEARS": 14,
        "60-64": 14,
        "65 TO 69 YEARS": 15,
        "65-69": 15,
        "70 TO 74 YEARS": 16,
        "70-74": 16,
        "75 TO 79 YEARS": 17,
        "75-79": 17,
        "80 TO 84 YEARS": 18,
        "80-84": 18,
        "85 TO 89 YEARS": 19,
        "85-89": 19,
        "90 YEARS AND OVER": 20,
        "90+": 20,
    }

    # Product ID for population data by demographics
    POPULATION_DEMOGRAPHICS_PRODUCT = "17100005"

    # REMOVED: CMA_MAPPING - StatsCan WDS API does not support city-level queries
    # Statistics Canada only provides data at the province/territory level
    # City-level queries should be rejected with a helpful error message

    # Country/region name aliases for better matching
    GEOGRAPHY_ALIASES: Dict[str, str] = {
        "NFLD": "NEWFOUNDLAND AND LABRADOR",
        "NL": "NEWFOUNDLAND AND LABRADOR",
        "PE": "PRINCE EDWARD ISLAND",
        "NS": "NOVA SCOTIA",
        "NB": "NEW BRUNSWICK",
        "QC": "QUEBEC",
        "ON": "ONTARIO",
        "MB": "MANITOBA",
        "SK": "SASKATCHEWAN",
        "AB": "ALBERTA",
        "BC": "BRITISH COLUMBIA",
        "YT": "YUKON",
        "NT": "NORTHWEST TERRITORIES",
        "NU": "NUNAVUT",
        "CANADA": "CANADA",
        "CA": "CANADA",  # ISO 3166-1 alpha-2 code for Canada
        "CAN": "CANADA",  # ISO 3166-1 alpha-3 code for Canada
        "ALL": "CANADA",
        "NATIONAL": "CANADA",
    }

    @property
    def provider_name(self) -> str:
        return "StatsCan"

    def __init__(self, metadata_search_service=None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        settings = get_settings()
        self.base_url = settings.statscan_base_url.rstrip("/")
        self.metadata_search = metadata_search_service  # Optional: for intelligent indicator discovery

    async def _fetch_data(self, **params) -> NormalizedData | List[NormalizedData]:
        """Implement BaseProvider interface by routing to fetch_indicator."""
        return await self.fetch_indicator(
            indicator=params.get("indicator", "GDP"),
            start_year=params.get("start_year"),
            end_year=params.get("end_year"),
        )

    def _get_table_viewer_url(self, product_id: str) -> str:
        """Convert 8-digit WDS product ID to 10-digit table viewer URL.

        Statistics Canada WDS API uses 8-digit product IDs (e.g., "17100005"),
        but the table viewer URLs require 10-digit IDs (e.g., "1710000501").

        The conversion appends "01" to get the default version of the table.

        Args:
            product_id: 8-digit product ID from WDS API

        Returns:
            Full table viewer URL with 10-digit product ID
        """
        # Ensure product_id is a string
        pid_str = str(product_id)

        # If already 10 digits, use as-is
        if len(pid_str) == 10:
            return f"https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid={pid_str}"

        # If 8 digits, append "01" for default version
        if len(pid_str) == 8:
            return f"https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid={pid_str}01"

        # For other lengths, return generic data search page
        return "https://www150.statcan.gc.ca/n1/en/type/data"

    async def _vector_id(self, indicator: Optional[str], vector_id: Optional[int]) -> int:
        """
        Get vector ID from indicator name or direct vector ID.

        If indicator not in hardcoded mappings, uses metadata search service
        to intelligently discover the correct vector ID. Falls back to dynamic
        WDS discovery if LLM-based selection doesn't find a confident match.
        """
        if vector_id:
            return vector_id
        if not indicator:
            raise ValueError("Vector ID or indicator is required")

        # Try hardcoded mappings first (fast path)
        key = indicator.upper().replace(" ", "_")
        mapped = self.VECTOR_MAPPINGS.get(key)
        if mapped:
            logger.info(f"✅ Using hardcoded mapping for '{indicator}': vector {mapped}")
            return mapped

        # Check if indicator is a numeric string (LLM sometimes returns vector ID directly)
        indicator_stripped = indicator.strip()
        if indicator_stripped.isdigit():
            vector_id_int = int(indicator_stripped)
            # Validate it's a known vector ID by checking if it's in our mappings values
            if vector_id_int in self.VECTOR_MAPPINGS.values():
                logger.info(f"✅ Using numeric indicator as vector ID: {vector_id_int}")
                return vector_id_int
            # Even if not in mappings, if it's an 8-digit number it's likely a valid vector ID
            if len(indicator_stripped) >= 7:
                logger.info(f"⚠️ Using unverified numeric vector ID: {vector_id_int}")
                return vector_id_int

        # If not in hardcoded mappings, try intelligent metadata search (SDMX-first)
        if self.metadata_search:
            logger.info(f"🔍 Indicator '{indicator}' not in hardcoded mappings, searching metadata (SDMX-first)...")

            try:
                # Search metadata catalog (SDMX first, then StatsCan API)
                search_results = await self.metadata_search.search_with_sdmx_fallback(
                    provider="StatsCan",
                    indicator=indicator,
                )

                if search_results:
                    # Use LLM to select best match
                    discovery = await self.metadata_search.discover_indicator(
                        provider="StatsCan",
                        indicator_name=indicator,
                        search_results=search_results
                    )

                    # Check if discovery returned ambiguity flag (multiple diverse options)
                    if discovery and discovery.get("ambiguous"):
                        options = discovery.get("options", [])
                        options_text = "\n".join([
                            f"  • {opt['name']}" for opt in options[:5]
                        ])
                        raise DataNotAvailableError(
                            f"Your query '{indicator}' matches multiple datasets. Please be more specific:\n{options_text}\n\n"
                            f"Try specifying the exact metric you need."
                        )

                    if discovery and discovery.get("confidence", 0) > 0.6:
                        discovered_id = discovery["code"]
                        logger.info(
                            f"✅ Discovered indicator code {discovered_id} for '{indicator}' "
                            f"(confidence: {discovery['confidence']})"
                        )

                        # Check if discovered_id is numeric (vector ID) or alphanumeric (SDMX/product ID)
                        if isinstance(discovered_id, str) and not discovered_id.isdigit():
                            # SDMX-style alphanumeric code (e.g., 'TET00003', 'MED_AG50')
                            # Treat as product ID and skip caching as vector ID
                            logger.info(f"ℹ️ Code '{discovered_id}' is alphanumeric (SDMX product ID), not a numeric vector ID")
                            # Raise to trigger WDS dynamic discovery with this product ID
                            raise DataNotAvailableError(
                                f"SDMX code '{discovered_id}' found but needs WDS coordinate-based query. "
                                f"Using dynamic discovery..."
                            )
                        else:
                            # Numeric vector ID - cache and return
                            vector_id_int = int(discovered_id)
                            self.VECTOR_MAPPINGS[key] = vector_id_int
                            return vector_id_int
                    else:
                        logger.warning(
                            f"⚠️ Low confidence match for '{indicator}' (confidence: {discovery.get('confidence', 0) if discovery else 0}). "
                            f"Falling back to WDS dynamic discovery..."
                        )

            except Exception as e:
                logger.warning(f"Error during SDMX metadata search: {e}. Falling back to WDS discovery...")

        # Fall back to dynamic WDS discovery
        logger.info(f"📡 Falling back to WDS dynamic discovery for '{indicator}'")
        raise DataNotAvailableError(
            f"Cannot determine vector ID for '{indicator}' with confidence. "
            f"Please note: Some Statistics Canada indicators like EMPLOYMENT and RETAIL_SALES "
            f"are available but require provincial/temporal context. "
            f"Try: GDP, UNEMPLOYMENT, INFLATION, CPI, HOUSING_STARTS. "
            f"Or try a different provider like WorldBank or IMF."
        )

    def _resolve_geography(self, geography: Optional[str]) -> int:
        """
        Resolve a geography name to its member ID for Canadian provinces/territories only.

        Statistics Canada WDS API only supports province/territory-level data.
        City-level queries and non-Canadian countries are not supported.

        Args:
            geography: Geography name (province/territory abbreviation or full name, or None for Canada)

        Returns:
            Member ID for the geography (1 = Canada by default)

        Raises:
            ValueError: If geography is not a valid Canadian province/territory
        """
        if not geography:
            return 1  # Default to Canada

        geography_upper = geography.upper()

        # List of known city names that should be rejected with helpful message
        CANADIAN_CITIES = [
            "TORONTO", "VANCOUVER", "MONTREAL", "CALGARY", "EDMONTON",
            "WINNIPEG", "OTTAWA", "HALIFAX", "QUEBEC CITY", "VICTORIA",
            "REGINA", "SASKATOON", "MISSISSAUGA", "BRAMPTON", "HAMILTON",
            "KITCHENER", "LONDON", "MARKHAM", "VAUGHAN", "GATINEAU"
        ]

        # List of known non-Canadian country codes
        NON_CANADIAN_COUNTRIES = [
            "US", "USA", "UNITED STATES", "CN", "CHINA", "EU", "EUROPE",
            "UK", "UNITED KINGDOM", "JP", "JAPAN", "DE", "GERMANY",
            "FR", "FRANCE", "IT", "ITALY", "ES", "SPAIN", "MX", "MEXICO",
            "BR", "BRAZIL", "IN", "INDIA", "AU", "AUSTRALIA", "KR", "KOREA"
        ]

        # Reject city-level queries
        if geography_upper in CANADIAN_CITIES or geography_upper.replace("_", " ") in CANADIAN_CITIES:
            raise ValueError(
                f"City-level data not supported: '{geography}'. "
                f"Statistics Canada only provides province/territory-level data. "
                f"Please specify a province (e.g., 'Ontario' instead of 'Toronto', 'BC' instead of 'Vancouver'). "
                f"Available: {', '.join(sorted([k for k in self.GEOGRAPHY_MEMBER_IDS.keys() if k != 'CANADA'])[:8])}..."
            )

        # Reject non-Canadian countries
        if geography_upper in NON_CANADIAN_COUNTRIES:
            raise ValueError(
                f"Non-Canadian geography: '{geography}'. "
                f"Statistics Canada only provides data for Canadian provinces/territories. "
                f"For {geography} data, try providers like FRED (US), WorldBank (global), OECD, or Eurostat (EU)."
            )

        # Check geography aliases first (short forms like "ON", "QC")
        if geography_upper in self.GEOGRAPHY_ALIASES:
            canonical_name = self.GEOGRAPHY_ALIASES[geography_upper]
            geography_upper = canonical_name.upper()

        # Check province/territory mappings
        if geography_upper in self.GEOGRAPHY_MEMBER_IDS:
            return self.GEOGRAPHY_MEMBER_IDS[geography_upper]

        # Unknown geography - provide helpful error
        available_provinces = sorted(
            [k for k in self.GEOGRAPHY_MEMBER_IDS.keys() if k != "CANADA"]
        )
        raise ValueError(
            f"Unknown geography: '{geography}'. "
            f"Statistics Canada only supports Canadian provinces/territories. "
            f"Available: {', '.join(available_provinces)}. "
            f"You can also use 'Canada' for national data."
        )

    def _map_frequency(self, freq_code: int) -> str:
        return self.FREQUENCY_MAP.get(freq_code, "unknown")

    def _map_scalar_factor(self, scalar_code: int) -> str:
        return self.SCALAR_FACTOR_MAP.get(scalar_code, "")

    def _filter_by_date_range(
        self,
        data_points: List[Dict[str, any]],
        start_date: Optional[str],
        end_date: Optional[str]
    ) -> List[Dict[str, any]]:
        """Filter data points by date range.

        Args:
            data_points: List of data points with 'date' field
            start_date: Start date in ISO format (e.g., '2010-01-01') or None
            end_date: End date in ISO format (e.g., '2020-12-31') or None

        Returns:
            Filtered list of data points within the date range
        """
        if not start_date and not end_date:
            return data_points

        filtered = []
        for point in data_points:
            date_str = point.get("date", "")
            if not date_str:
                continue

            # Extract year-month from date (handles both 'YYYY-MM-DD' and 'YYYY-MM' formats)
            try:
                # StatsCan dates are typically 'YYYY-MM-01' or 'YYYY-MM'
                date_parts = date_str.split("-")
                point_year = int(date_parts[0])
                point_month = int(date_parts[1]) if len(date_parts) > 1 else 1
                point_day = int(date_parts[2]) if len(date_parts) > 2 else 1

                # Check start date
                if start_date:
                    start_parts = start_date.split("-")
                    start_year = int(start_parts[0])
                    start_month = int(start_parts[1]) if len(start_parts) > 1 else 1
                    start_day = int(start_parts[2]) if len(start_parts) > 2 else 1

                    if (point_year, point_month, point_day) < (start_year, start_month, start_day):
                        continue

                # Check end date
                if end_date:
                    end_parts = end_date.split("-")
                    end_year = int(end_parts[0])
                    end_month = int(end_parts[1]) if len(end_parts) > 1 else 12
                    end_day = int(end_parts[2]) if len(end_parts) > 2 else 31

                    if (point_year, point_month, point_day) > (end_year, end_month, end_day):
                        continue

                filtered.append(point)
            except (ValueError, IndexError):
                # If date parsing fails, include the point
                filtered.append(point)

        logger.info(
            f"📅 Date filter applied: {len(data_points)} → {len(filtered)} points "
            f"({start_date or 'start'} to {end_date or 'end'})"
        )
        return filtered

    def _get_unit_description(self, indicator_name: str, scalar_code: int) -> str:
        """
        Get a human-readable unit description based on indicator and scalar code.

        Args:
            indicator_name: Name of the indicator
            scalar_code: Scalar factor code from StatsCan

        Returns:
            Unit description string
        """
        scalar_unit = self._map_scalar_factor(scalar_code)

        # Check indicator type to provide better unit context
        indicator_upper = indicator_name.upper() if indicator_name else ""

        if any(x in indicator_upper for x in ["UNEMPLOYMENT", "RATE", "PERCENT"]):
            return "percent"
        elif any(x in indicator_upper for x in ["INDEX", "CPI", "PRICE"]):
            return "index (2007=100)" if "PRICE" in indicator_upper else "index (2002=100)"
        elif any(x in indicator_upper for x in ["POPULATION", "COUNT", "NUMBER"]):
            return scalar_unit or "persons"
        else:
            # For monetary/aggregate values, use scalar description
            return scalar_unit or "units"

    def _normalize_units(self, value: float | None, from_scalar_code: int, to_unit: str = "billions", indicator_name: Optional[str] = None) -> tuple[float | None, str]:
        """Convert values from StatsCan's scalar factor to a target unit.

        StatsCan API returns values with scalarFactorCode indicating the unit:
        - Code 0: units (1)
        - Code 1: tens (10)
        - Code 2: hundreds (100)
        - Code 3: thousands (1,000)
        - Code 4: ten thousands (10,000)
        - Code 5: hundred thousands (100,000)
        - Code 6: millions (1,000,000)
        - Code 7: ten millions (10,000,000)
        - Code 8: hundred millions (100,000,000)
        - Code 9: billions (1,000,000,000)

        For better UX, we convert large monetary values to billions.
        For example, if scalar_code=6 (millions) and value=2287214:
        - Value is 2,287,214 million dollars
        - Convert to billions: 2,287,214 / 1,000 = 2,287.214 billion
        - This makes GDP values human-readable

        Args:
            value: The scaled value from the API (e.g., 2287214)
            from_scalar_code: The scalar factor code from the API (e.g., 6 for millions)
            to_unit: Target unit ("billions", "millions", "thousands", etc.)
            indicator_name: Name of indicator for context-aware unit mapping

        Returns:
            Tuple of (converted_value, unit_label)
        """
        if value is None:
            return None, self._get_unit_description(indicator_name or "", from_scalar_code)

        # Define conversion factors - these match the scalar factor codes
        scale_factors = {
            "units": 1,
            "tens": 10,
            "hundreds": 100,
            "thousands": 1_000,
            "ten thousands": 10_000,
            "hundred thousands": 100_000,
            "millions": 1_000_000,
            "ten millions": 10_000_000,
            "hundred millions": 100_000_000,
            "billions": 1_000_000_000,
        }

        # Get source unit from scalar code
        source_unit = self._map_scalar_factor(from_scalar_code)
        if not source_unit or source_unit not in scale_factors:
            # Unknown scalar code, return as-is
            return value, self._get_unit_description(indicator_name or "", from_scalar_code)

        source_factor = scale_factors[source_unit]
        target_factor = scale_factors.get(to_unit, source_factor)

        # Convert: value * source_factor / target_factor
        # Example: 2,287,214 millions → billions
        #          2,287,214 * 1,000,000 / 1,000,000,000 = 2,287.214
        if source_factor != target_factor:
            converted_value = value * source_factor / target_factor
            return converted_value, to_unit
        else:
            # Source and target are same, use context-aware unit description
            final_unit = self._get_unit_description(indicator_name or "", from_scalar_code)
            return value, final_unit

    def _extract_detailed_metadata(
        self,
        cube_metadata: Dict[str, any],
        coordinate: Optional[str] = None
    ) -> Dict[str, any]:
        """Extract detailed metadata from cube metadata.

        Args:
            cube_metadata: Raw cube metadata from getCubeMetadata API
            coordinate: Optional coordinate string (e.g., "1.1.1.1.0.0.0.0.0.0") to get specific dimension values

        Returns:
            Dictionary with extracted metadata fields:
            - seasonalAdjustment: e.g., "Seasonally adjusted at annual rates"
            - priceType: e.g., "Chained (2017) dollars"
            - description: Full cube title
            - startDate, endDate: Data range
        """
        extracted = {}

        # Get cube title as description
        extracted["description"] = cube_metadata.get("cubeTitleEn", "")

        # Get date range
        extracted["startDate"] = cube_metadata.get("cubeStartDate")
        extracted["endDate"] = cube_metadata.get("cubeEndDate")

        # Parse dimensions to find seasonal adjustment, price type, etc.
        dimensions = cube_metadata.get("dimension", [])
        coord_parts = coordinate.split(".") if coordinate else []

        for dim_idx, dim_info in enumerate(dimensions):
            dim_name = dim_info.get("dimensionNameEn", "").upper()
            members = dim_info.get("member", [])

            # Get the selected member ID from coordinate
            selected_member_id = int(coord_parts[dim_idx]) if dim_idx < len(coord_parts) and coord_parts[dim_idx].isdigit() else 1

            # Find the member name for the selected ID
            selected_member_name = None
            for member in members:
                if member.get("memberId") == selected_member_id:
                    selected_member_name = member.get("memberNameEn", "")
                    break

            # Categorize based on dimension name
            if "SEASONAL" in dim_name or "ADJUSTMENT" in dim_name:
                extracted["seasonalAdjustment"] = selected_member_name or "Not specified"
            elif "PRICE" in dim_name or "DOLLAR" in dim_name or "VALUE" in dim_name:
                extracted["priceType"] = selected_member_name or "Not specified"
            elif "TYPE" in dim_name and "DATA" in dim_name:
                extracted["dataType"] = selected_member_name or "Level"

        # Default data type if not found
        if not extracted.get("dataType"):
            # Infer from title
            title_upper = extracted.get("description", "").upper()
            if "PERCENT CHANGE" in title_upper or "% CHANGE" in title_upper:
                extracted["dataType"] = "Percent change"
            elif "CHANGE" in title_upper:
                extracted["dataType"] = "Change"
            elif "INDEX" in title_upper:
                extracted["dataType"] = "Index"
            else:
                extracted["dataType"] = "Level"

        return extracted

    async def _get_cube_metadata(self, product_id: str) -> Dict[str, any]:
        """Get detailed metadata for a StatsCan cube/product.

        Uses the WDS getCubeMetadata endpoint (POST) to discover:
        - Available dimensions (geography, time period, etc.)
        - Member IDs for filtering
        - Data structure and hierarchy

        Args:
            product_id: Product ID (e.g., "14100287" for labour force data)

        Returns:
            Dictionary with cube metadata including dimensions and members
        """
        try:
            # Use shared HTTP client pool for better performance
            client = get_http_client()
            logger.info(f"📊 Fetching metadata for product {product_id}")
            response = await client.post(
                f"{self.base_url}/getCubeMetadata",
                json=[{"productId": product_id}],
                headers={"Content-Type": "application/json"},
                timeout=30.0
            )
            response.raise_for_status()
            payload = response.json()

            # Response is an array with status and object
            if payload and len(payload) > 0:
                response_obj = payload[0]
                if response_obj.get("status") == "SUCCESS":
                    metadata = response_obj.get("object", {})
                    logger.info(f"✅ Retrieved metadata for product {product_id}")
                    return metadata
                else:
                    raise ValueError(f"API error for product {product_id}: {response_obj.get('status')}")
            else:
                raise ValueError(f"Empty response for product {product_id}")

        except Exception as e:
            logger.error(f"Failed to get metadata for product {product_id}: {e}")
            raise DataNotAvailableError(
                f"Could not retrieve metadata for Statistics Canada product {product_id}: {e}"
            )

    def _find_dimension_member(
        self,
        dimensions: List[Dict[str, any]],
        dimension_keywords: List[str],
        member_search: str
    ) -> tuple:
        """Find a dimension member that matches the search term.

        Searches through cube dimensions for one matching the keywords,
        then finds a member within that dimension matching the search term.

        Args:
            dimensions: List of dimension objects from cube metadata
            dimension_keywords: Keywords to identify the dimension (e.g., ["NAICS", "INDUSTRY"])
            member_search: Term to search for in member names (e.g., "goods-producing")

        Returns:
            Tuple of (dimension_index, member_id, member_name) or (None, None, None) if not found
        """
        search_terms = member_search.lower().replace("-", " ").replace("_", " ").split()

        for dim_idx, dim_info in enumerate(dimensions):
            dim_name = dim_info.get("dimensionNameEn", "").upper()

            # Check if this dimension matches our target
            if not any(kw.upper() in dim_name for kw in dimension_keywords):
                continue

            members = dim_info.get("member", [])
            best_match = None
            best_score = 0

            for member in members:
                member_name = member.get("memberNameEn", "")
                member_name_lower = member_name.lower()
                member_id = member.get("memberId")

                # Score based on how many search terms match
                score = sum(1 for term in search_terms if term in member_name_lower)

                # Exact phrase match gets bonus
                if member_search.lower().replace("-", " ").replace("_", " ") in member_name_lower:
                    score += 5

                if score > best_score:
                    best_score = score
                    best_match = (dim_idx, member_id, member_name)

            if best_match and best_score > 0:
                logger.info(f"✅ Found member match: '{best_match[2]}' (id={best_match[1]}) in dimension {dim_idx}")
                return best_match

        return (None, None, None)

    async def fetch_with_breakdown(
        self,
        params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch data with a specific industry/sector breakdown.

        This is a general method that dynamically finds the appropriate
        dimension member based on the breakdown parameter.

        Args:
            params: Dictionary containing:
                - indicator: Base indicator (e.g., "GDP")
                - breakdown: Industry/sector breakdown (e.g., "goods-producing", "services", "manufacturing")
                - startDate, endDate: Date range
                - periods: Number of periods

        Returns:
            NormalizedData with the breakdown data
        """
        indicator = params.get("indicator", "GDP")
        breakdown = params.get("breakdown") or params.get("industry")
        periods = params.get("periods", 240)
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        if not breakdown:
            # No breakdown specified, use regular fetch
            return await self.fetch_series(params)

        # Get the base indicator's vector ID to find the product
        indicator_upper = indicator.upper().replace(" ", "_")
        base_vector = self.VECTOR_MAPPINGS.get(indicator_upper)

        if not base_vector:
            raise DataNotAvailableError(
                f"Indicator '{indicator}' not found. Available: GDP, UNEMPLOYMENT, CPI, INFLATION, etc."
            )

        # Get product ID for this indicator
        product_id = self.PRODUCT_ID_CACHE.get(base_vector)
        if not product_id:
            try:
                product_id = await self._get_product_id_from_vector(base_vector)
            except Exception:
                raise DataNotAvailableError(
                    f"Could not determine product for indicator '{indicator}'"
                )

        # Normalize product ID to 8 digits (API requirement)
        # 10-digit IDs like 3610043401 need to be converted to 8-digit: 36100434
        product_id_str = str(product_id)
        if len(product_id_str) == 10:
            product_id_str = product_id_str[:8]  # Remove version suffix (last 2 digits)
        elif len(product_id_str) < 8:
            product_id_str = product_id_str.zfill(8)  # Pad with leading zeros

        logger.info(f"🔍 Fetching {indicator} with breakdown: {breakdown} (product: {product_id_str})")

        # Get cube metadata to find the dimension structure
        cube_meta = await self._get_cube_metadata(product_id_str)
        dimensions = cube_meta.get("dimension", [])

        if not dimensions:
            raise DataNotAvailableError(f"No dimensions found for product {product_id}")

        # Find the industry/sector dimension and matching member
        dim_idx, member_id, member_name = self._find_dimension_member(
            dimensions,
            dimension_keywords=["NAICS", "INDUSTRY", "SECTOR", "CLASSIFICATION"],
            member_search=breakdown
        )

        if dim_idx is None:
            # List available breakdowns
            available = []
            for dim in dimensions:
                dim_name = dim.get("dimensionNameEn", "").upper()
                if any(kw in dim_name for kw in ["NAICS", "INDUSTRY", "SECTOR"]):
                    for m in dim.get("member", [])[:10]:
                        available.append(m.get("memberNameEn", ""))

            raise DataNotAvailableError(
                f"Could not find breakdown '{breakdown}' for {indicator}. "
                f"Available breakdowns: {', '.join(available[:8])}..."
            )

        # Build coordinate: default member IDs (1) for all dimensions except the target
        coordinate_parts = []
        for i, dim in enumerate(dimensions):
            if i == dim_idx:
                coordinate_parts.append(str(member_id))
            else:
                coordinate_parts.append("1")  # Default to first member (usually "Total" or "All")

        # Pad to 10 dimensions
        while len(coordinate_parts) < 10:
            coordinate_parts.append("0")

        coordinate = ".".join(coordinate_parts[:10])
        logger.info(f"📊 Using coordinate: {coordinate} for {indicator} - {member_name}")

        # Fetch data using coordinate
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": int(product_id_str),  # Use normalized 8-digit product ID
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )

        response.raise_for_status()
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            error_msg = payload[0].get("object", "Unknown error") if payload else "Empty response"
            raise DataNotAvailableError(
                f"StatsCan query failed for {indicator} ({breakdown}): {error_msg}"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(
                f"No data found for {indicator} - {breakdown}"
            )

        # Build data points
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)
        frequency = self._map_frequency(freq_code)
        unit = self._map_scalar_factor(scalar_code) or "units"

        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date filter
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build indicator name
        indicator_name = f"Canadian {indicator} - {member_name}"

        # Get detailed metadata
        detailed_meta = self._extract_detailed_metadata(cube_meta, coordinate)

        source_url = self._get_table_viewer_url(product_id_str)

        metadata = Metadata(
            source="Statistics Canada",
            indicator=indicator_name,
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id_str}:{coordinate}",
            apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            sourceUrl=source_url,
            seasonalAdjustment=detailed_meta.get("seasonalAdjustment"),
            priceType=detailed_meta.get("priceType"),
            dataType=detailed_meta.get("dataType"),
            description=detailed_meta.get("description"),
            scaleFactor=self._map_scalar_factor(scalar_code),
            startDate=data_points[0]["date"] if data_points else None,
            endDate=data_points[-1]["date"] if data_points else None,
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def _get_product_id_from_vector(self, vector_id: int) -> str:
        """Query StatsCan API to get the product ID for a given vector ID.

        This is needed when metadata search discovers a vector ID but we need
        the product ID for batch API calls. Results are cached for future use.

        Args:
            vector_id: The vector ID to look up

        Returns:
            Product ID string (e.g., "17100005")

        Raises:
            ValueError: If product ID cannot be determined
        """
        # Check cache first
        if vector_id in self.PRODUCT_ID_CACHE:
            product_id = self.PRODUCT_ID_CACHE[vector_id]
            logger.debug(f"✅ Using cached product ID {product_id} for vector {vector_id}")
            return product_id

        # Query StatsCan API for vector metadata
        logger.info(f"🔍 Querying StatsCan API for product ID of vector {vector_id}")
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        try:
            response = await client.post(
                f"{self.base_url}/getSeriesInfoFromVector",
                json=[{"vectorId": vector_id}],
                headers={"Content-Type": "application/json"},
                timeout=30.0
            )
            response.raise_for_status()
            data = response.json()

            if data and len(data) > 0:
                product_id = str(data[0].get("productId", ""))
                if product_id:
                    # Cache for future use
                    self.PRODUCT_ID_CACHE[vector_id] = product_id
                    logger.info(f"✅ Discovered product ID {product_id} for vector {vector_id} (cached)")
                    return product_id

            raise ValueError(f"Product ID not found for vector {vector_id}")

        except Exception as e:
            logger.error(f"Failed to get product ID for vector {vector_id}: {e}")
            raise ValueError(f"Could not determine product ID for vector {vector_id}: {e}")

    async def fetch_by_coordinate(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch data using coordinate-based query for indicators without vector IDs.

        This method uses the COORDINATE_PRODUCT_MAPPINGS to find the correct product
        and coordinate for indicators that require dimensional queries.

        Args:
            params: Dictionary containing:
                - indicator: Indicator name (must be in COORDINATE_PRODUCT_MAPPINGS)
                - periods: Number of recent periods to fetch (default: 240)
                - startDate, endDate: Optional date range filters

        Returns:
            NormalizedData object with metadata and data points
        """
        indicator = params.get("indicator", "")
        indicator_key = indicator.upper().replace(" ", "_")
        periods = params.get("periods", 240)
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        # Look up in coordinate mappings
        mapping = self.COORDINATE_PRODUCT_MAPPINGS.get(indicator_key)
        if not mapping:
            raise DataNotAvailableError(
                f"No coordinate mapping found for '{indicator}'. "
                f"Available: {', '.join(list(self.COORDINATE_PRODUCT_MAPPINGS.keys())[:10])}..."
            )

        product_id, coordinate, description = mapping
        logger.info(f"📊 Using coordinate-based query for {indicator}: product={product_id}, coord={coordinate}")

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )
        response.raise_for_status()
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            raise DataNotAvailableError(
                f"StatsCan coordinate query failed for {indicator} (product={product_id}, coord={coordinate})"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(f"No data found for {indicator} (product={product_id})")

        # Determine frequency and unit from first data point
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)
        frequency = self._map_frequency(freq_code)
        unit = self._get_unit_description(indicator, scalar_code)

        # Convert data points
        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date range filter if specified
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build source URL
        source_url = self._get_table_viewer_url(product_id)

        metadata = Metadata(
            source="Statistics Canada",
            indicator=f"Canadian {description}",
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id}:{coordinate}",
            apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            sourceUrl=source_url,
        )

        logger.info(f"✅ Retrieved {len(data_points)} data points for {indicator} via coordinate query")
        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_series(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch time series data from Statistics Canada.

        Args:
            params: Dictionary containing:
                - indicator: Common indicator name (e.g., "GDP", "UNEMPLOYMENT")
                - vectorId: Direct vector ID (optional, overrides indicator)
                - periods: Number of recent periods to fetch (default: 120 for 10 years monthly)

        Returns:
            NormalizedData object with metadata and data points
        """
        indicator = params.get("indicator")
        indicator_key = indicator.upper().replace(" ", "_") if indicator else None

        # Priority 1: Check if this indicator requires coordinate-based query
        if indicator_key and indicator_key in self.COORDINATE_PRODUCT_MAPPINGS:
            logger.info(f"🔄 Routing {indicator} to coordinate-based query")
            return await self.fetch_by_coordinate(params)

        # Priority 2: Check if indicator has a vector ID (not None)
        if indicator_key and indicator_key in self.VECTOR_MAPPINGS:
            vector_id = self.VECTOR_MAPPINGS.get(indicator_key)
            if vector_id is None:
                # Vector is explicitly None - use dynamic discovery instead
                logger.info(f"🔍 {indicator} has None vector - using dynamic discovery")
                return await self.fetch_dynamic_data(params)

        target_vector = await self._vector_id(
            indicator,
            params.get("vectorId")
        )
        periods = params.get("periods", 240)  # Default to 20 years of monthly data

        # Use extended timeout (300s = 5 minutes) to handle complex multi-province queries
        # StatsCan API can be slow, especially for batch coordinate queries
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        # Fetch data using the vector ID
        response = await client.post(
            f"{self.base_url}/getDataFromVectorsAndLatestNPeriods",
            json=[{"vectorId": target_vector, "latestN": periods}],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )
        response.raise_for_status()
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            raise RuntimeError(f"StatsCan vector {target_vector} not found or error occurred")

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise RuntimeError(f"No data found for vector {target_vector}")

        # Get indicator name from parameters or use vector ID
        indicator_name = params.get("indicator", f"Vector {target_vector}")
        if indicator_name in self.VECTOR_MAPPINGS:
            # Use the mapped name
            series_title = f"Canadian {indicator_name}"
        else:
            series_title = f"Vector {target_vector}"

        # Determine frequency and unit from first data point
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)

        frequency = self._map_frequency(freq_code)

        # Determine if we should normalize units to billions (for monetary values)
        # GDP and similar monetary indicators should be in billions for readability
        should_normalize = indicator_name and any(
            term in indicator_name.upper()
            for term in ["GDP", "REVENUE", "EXPENDITURE", "DEBT", "DEFICIT", "SURPLUS"]
        )

        # Convert values if needed
        if should_normalize:
            # Convert data points to billions
            data_points = []
            target_unit = "billions"
            for point in vector_data:
                converted_value, final_unit = self._normalize_units(
                    point["value"],
                    scalar_code,
                    to_unit=target_unit,
                    indicator_name=indicator_name
                )
                data_points.append({
                    "date": point["refPer"],
                    "value": converted_value,
                })
            unit = final_unit
        else:
            # Keep original units (for percentages, indices, counts, etc.)
            unit = self._get_unit_description(indicator_name, scalar_code)
            data_points = [
                {
                    "date": point["refPer"],
                    "value": point["value"] if point["value"] is not None else None,
                }
                for point in vector_data
            ]

        # Apply date range filter if specified
        start_date = params.get("startDate")
        end_date = params.get("endDate")
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build API URL for reproducibility
        api_url = f"{self.base_url}/getDataFromVectorsAndLatestNPeriods (POST with vectorId={target_vector}, latestN={periods})"

        # Human-readable URL for data verification on Statistics Canada website
        # Try to get product ID from cache to build proper table viewer URL
        cached_product_id = self.PRODUCT_ID_CACHE.get(target_vector)
        if cached_product_id:
            # Use table viewer URL with product ID (auto-converts to 10-digit format)
            source_url = self._get_table_viewer_url(cached_product_id)
        else:
            # Fallback to StatsCan data search page
            source_url = "https://www150.statcan.gc.ca/n1/en/type/data"

        # Try to fetch detailed metadata for enhanced information
        detailed_meta = {}
        if cached_product_id:
            try:
                cube_meta = await self._get_cube_metadata(cached_product_id)
                # Get coordinate from data_object if available
                coordinate = data_object.get("coordinate")
                detailed_meta = self._extract_detailed_metadata(cube_meta, coordinate)
            except Exception as e:
                logger.warning(f"Could not fetch detailed metadata: {e}")

        # Determine scale factor from scalar code
        scale_factor = self._map_scalar_factor(scalar_code) if scalar_code else None

        metadata = Metadata(
            source="Statistics Canada",
            indicator=series_title,
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=str(target_vector),
            apiUrl=api_url,
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=detailed_meta.get("seasonalAdjustment"),
            priceType=detailed_meta.get("priceType"),
            dataType=detailed_meta.get("dataType"),
            description=detailed_meta.get("description"),
            notes=None,  # StatsCan doesn't provide detailed notes easily
            scaleFactor=scale_factor,
            startDate=detailed_meta.get("startDate") if detailed_meta.get("startDate") else (data_points[0]["date"] if data_points else None),
            endDate=detailed_meta.get("endDate") if detailed_meta.get("endDate") else (data_points[-1]["date"] if data_points else None),
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def search_vectors(
        self, keyword: str, limit: int = 10
    ) -> List[Dict[str, any]]:
        """Search for data cubes/vectors by keyword.

        This is a helper method to find vector IDs for indicators.
        Uses the WDS getAllCubesListLite endpoint to dynamically discover
        available tables without relying on hardcoded mappings.

        Supports synonym expansion for better matching (e.g., "HOUSING_PRICE"
        matches "housing price index", "new housing price", "NHPI").

        Args:
            keyword: Search term (e.g., "unemployment", "GDP", "employment")
            limit: Maximum number of results to return

        Returns:
            List of matching cubes with productId and titles
        """
        try:
            # Use shared HTTP client pool for better performance
            client = get_http_client()
            logger.info(f"🔍 Searching StatsCan for: {keyword}")
            response = await client.get(
                f"{self.base_url}/getAllCubesListLite",
                timeout=30.0
            )
            response.raise_for_status()
            cubes = response.json()

            # Build search terms (original keyword + synonyms)
            keyword_upper = keyword.upper().replace(" ", "_")
            search_terms = [keyword.lower()]

            # Add synonyms if available
            if keyword_upper in self.KEYWORD_SYNONYMS:
                synonyms = self.KEYWORD_SYNONYMS[keyword_upper]
                search_terms.extend([s.lower() for s in synonyms])
                logger.info(f"   Expanded search with synonyms: {synonyms}")

            # Match cubes by any search term (case-insensitive substring match)
            matching = []
            for cube in cubes:
                cube_title = cube.get("cubeTitleEn", "").lower()
                # Check if any search term matches the cube title
                if any(term in cube_title for term in search_terms):
                    matching.append({
                        "productId": str(cube["productId"]),
                        "title": cube.get("cubeTitleEn", ""),
                        "startDate": cube.get("cubeStartDate"),
                        "endDate": cube.get("cubeEndDate"),
                        "archived": cube.get("archived", "1"),
                        "frequency": cube.get("frequencyCode"),
                    })

            # Prioritize active (non-archived) cubes with recent data
            def sort_key(cube):
                # Score: (is_active, is_recent, has_long_history)
                is_active = 1 if cube.get("archived") == "2" else 0
                end_year = int(cube.get("endDate", "2000-01-01")[:4])
                is_recent = 1 if end_year >= 2024 else (0.5 if end_year >= 2020 else 0)
                return (is_active, is_recent)

            matching.sort(key=sort_key, reverse=True)
            logger.info(f"✅ Found {len(matching)} StatsCan cubes matching '{keyword}'")
            return matching[:limit]

        except Exception as e:
            logger.error(f"Error searching StatsCan cubes: {e}")
            raise DataNotAvailableError(
                f"Failed to search Statistics Canada for '{keyword}': {e}"
            )

    async def fetch_categorical_data(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch categorical data using WDS coordinate-based queries.

        This method uses the WDS getDataFromCubePidCoordAndLatestNPeriods endpoint
        with 10-dimension coordinates to retrieve data filtered by any categorical
        dimensions (geography, gender, age groups, etc.).

        Args:
            params: Dictionary containing:
                - productId: Product ID to query (e.g., "17100005" for population)
                - indicator: Human-readable indicator name (e.g., "Population")
                - periods: Number of recent periods to fetch (default: 20)
                - dimensions: Dict mapping dimension names to values, e.g.:
                    {
                        "geography": "Ontario",      # Province/territory name
                        "gender": "Men+",            # Gender category
                        "age": "25 to 29 years"      # Age group
                    }
                  Any dimension can be None or omitted to use "all" (member ID 1)

        Returns:
            NormalizedData object with metadata and data points

        Raises:
            ValueError: If dimension value not recognized
            DataNotAvailableError: If data cannot be retrieved

        Examples:
            # Ontario population (all genders, all ages)
            {"productId": "17100005", "indicator": "Population",
             "dimensions": {"geography": "Ontario"}}

            # Canada male population (all ages)
            {"productId": "17100005", "indicator": "Population",
             "dimensions": {"gender": "Men+"}}

            # Ontario male population aged 25-29
            {"productId": "17100005", "indicator": "Population",
             "dimensions": {"geography": "Ontario", "gender": "Men+", "age": "25 to 29 years"}}
        """
        product_id = params.get("productId", self.POPULATION_DEMOGRAPHICS_PRODUCT)
        indicator = params.get("indicator", "Population")
        periods = params.get("periods", 20)
        dimensions = params.get("dimensions", {})

        # Extract dimension values (default to None = "all")
        geography = dimensions.get("geography")
        gender = dimensions.get("gender")
        age = dimensions.get("age")

        # Look up member IDs for each dimension
        # Default to 1 (which typically means "Total" or "All" for most dimensions)
        geography_id = 1  # Default: All Canada
        gender_id = 1     # Default: Total (both genders)
        age_id = 1        # Default: All ages

        # Dimension 0: Geography
        if geography:
            # Use _resolve_geography which validates cities and non-Canadian countries
            geography_id = self._resolve_geography(geography)

        # Dimension 1: Gender
        if gender:
            gender_upper = gender.upper()
            gender_id = self.GENDER_MEMBER_IDS.get(gender_upper)
            if not gender_id:
                available = ", ".join(sorted(set(self.GENDER_MEMBER_IDS.keys())))
                raise ValueError(
                    f"Unknown gender: '{gender}'. "
                    f"Available: {available}"
                )

        # Dimension 2: Age group
        if age:
            age_upper = age.upper()
            age_id = self.AGE_GROUP_MEMBER_IDS.get(age_upper)
            if not age_id:
                available = ", ".join(sorted(list(self.AGE_GROUP_MEMBER_IDS.keys())[:20]))
                raise ValueError(
                    f"Unknown age group: '{age}'. "
                    f"Available (partial): {available}... (see AGE_GROUP_MEMBER_IDS for full list)"
                )

        # Build 10-dimension coordinate
        # For product 17100005 (Population by age and gender):
        #   Dimension 0: Geography (province/territory)
        #   Dimension 1: Gender
        #   Dimension 2: Age group
        #   Dimensions 3-9: Padded with zeros (unused for this product)
        coordinate = f"{geography_id}.{gender_id}.{age_id}.0.0.0.0.0.0.0"

        # Build human-readable description for logging and metadata
        description_parts = []
        if geography:
            description_parts.append(geography)
        if gender and gender.upper() not in ["TOTAL", "BOTH", "ALL", "BOTH SEXES"]:
            description_parts.append(gender)
        if age and age.upper() not in ["ALL AGES", "ALL"]:
            description_parts.append(f"aged {age}")

        description = " ".join(description_parts) if description_parts else "Canada (all categories)"

        logger.info(
            f"Fetching {indicator} for {description} "
            f"using WDS coordinate: {coordinate}"
        )

        # Use extended timeout (300s = 5 minutes) to handle complex multi-province queries
        # StatsCan API can be slow, especially for batch coordinate queries
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        # Fetch data using coordinate-based query
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )

        if response.status_code == 406:
            raise DataNotAvailableError(
                f"Invalid coordinate format for product {product_id}. "
                f"Coordinate: {coordinate}. "
                f"This may indicate the product structure has changed."
            )

        response.raise_for_status()
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            error_msg = payload[0].get("object", "Unknown error") if payload else "Empty response"
            raise DataNotAvailableError(
                f"StatsCan WDS query failed for {description}. "
                f"Error: {error_msg}"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(
                f"No data found for {description} {indicator}"
            )

        # Determine frequency and unit from first data point
        freq_code = vector_data[0].get("frequencyCode", 12)  # Default to annual
        scalar_code = vector_data[0].get("scalarFactorCode", 0)

        frequency = self._map_frequency(freq_code)
        unit = self._map_scalar_factor(scalar_code)

        # Build API URL for reproducibility
        api_url = (
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods "
            f"(POST with productId={product_id}, coordinate={coordinate}, latestN={periods})"
        )

        # Human-readable URL for data verification on Statistics Canada website
        # Use helper method to ensure 10-digit product ID format
        source_url = self._get_table_viewer_url(product_id)

        # Build indicator name
        indicator_name = f"{description} {indicator}" if description != "Canada (all categories)" else indicator

        # Build data points first (needed for startDate/endDate)
        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date range filter if specified
        start_date = params.get("startDate")
        end_date = params.get("endDate")
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Determine dataType from indicator name
        indicator_upper = indicator_name.upper()
        if "RATE" in indicator_upper or "PERCENT" in indicator_upper:
            data_type = "Rate"
        elif "INDEX" in indicator_upper:
            data_type = "Index"
        elif "CHANGE" in indicator_upper:
            data_type = "Percent Change"
        else:
            data_type = "Level"

        metadata = Metadata(
            source="Statistics Canada",
            indicator=indicator_name,
            country="Canada",
            frequency=frequency,
            unit=unit if unit else "persons",
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id}:{coordinate}",
            apiUrl=api_url,
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=None,  # Not available in categorical queries without metadata
            dataType=data_type,
            priceType=None,  # Not typically available for categorical queries
            description=indicator_name,
            notes=None,  # StatsCan doesn't provide detailed notes easily
            scaleFactor=self._map_scalar_factor(scalar_code) if scalar_code else None,
            startDate=data_points[0]["date"] if data_points else None,
            endDate=data_points[-1]["date"] if data_points else None,
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_dynamic_data(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch data dynamically using WDS metadata discovery.

        This is the main method for handling queries that don't match hardcoded
        vector IDs. It:
        1. Uses metadata search to find the right cube
        2. Gets cube metadata to understand dimensions
        3. Routes to appropriate fetch method based on structure

        Args:
            params: Dictionary containing:
                - indicator: Indicator name (e.g., "employment", "retail sales")
                - geography: Optional province/territory
                - period: Optional number of periods (default: 240 for 20 years)

        Returns:
            NormalizedData with the requested data
        """
        indicator = params.get("indicator", "")
        geography = params.get("geography")
        periods = params.get("periods", 240)
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        # Validate geography if provided
        if geography:
            # This will raise ValueError for cities or non-Canadian countries
            self._resolve_geography(geography)

        logger.info(f"📡 Using dynamic metadata discovery for: {indicator} (geography: {geography})")

        # Normalize indicator name: convert "RETAIL_SALES" → "retail sales"
        # This ensures the search works with StatsCan's actual cube titles
        search_term = indicator.lower().replace("_", " ")
        logger.info(f"📊 Search term: '{search_term}' (normalized from '{indicator}')")

        # Step 1: Search for matching cubes
        matching_cubes = await self.search_vectors(search_term, limit=5)

        if not matching_cubes:
            raise DataNotAvailableError(
                f"No Statistics Canada data found for '{indicator}'. "
                f"Try a different indicator or use a different provider."
            )

        # Step 2: Try each matching cube until one works
        for cube in matching_cubes:
            product_id = cube.get("productId")
            cube_title = cube.get("title", "Unknown")

            try:
                logger.info(f"🔄 Trying {product_id}: {cube_title}")

                # Fetch metadata to understand structure
                metadata = await self._get_cube_metadata(product_id)

                # Determine which fetch method to use based on structure
                dimensions = metadata.get("dimension", [])
                has_geography = any(
                    "geogr" in d.get("dimensionNameEn", "").lower()
                    for d in dimensions
                )

                if geography and has_geography:
                    # Has geography dimension - use coordinate method
                    logger.info(f"✅ Using coordinate method for {product_id}")
                    result = await self.fetch_from_product_with_discovery(
                        product_id=product_id,
                        indicator=indicator,
                        metadata=metadata,
                        geography=geography,
                        periods=periods,
                        start_date=start_date,
                        end_date=end_date
                    )
                    return result
                elif geography and not has_geography:
                    # User requested geography but cube doesn't have it
                    logger.warning(f"⚠️ Product {product_id} doesn't have geography dimension")
                    continue
                else:
                    # No geography requested - use vector/simple method
                    logger.info(f"✅ Using vector method for {product_id}")
                    result = await self.fetch_from_product_with_discovery(
                        product_id=product_id,
                        indicator=indicator,
                        metadata=metadata,
                        geography=geography,
                        periods=periods,
                        start_date=start_date,
                        end_date=end_date
                    )
                    return result

            except Exception as e:
                logger.warning(f"⚠️ Product {product_id} failed: {e}")
                continue

        # All cubes failed
        raise DataNotAvailableError(
            f"Unable to retrieve data for '{indicator}' from Statistics Canada. "
            f"All matching datasets failed. Try a different indicator or provider."
        )

    async def fetch_from_product_with_discovery(
        self,
        product_id: str,
        indicator: str,
        metadata: Dict[str, any],
        geography: Optional[str],
        periods: int = 240,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> NormalizedData:
        """Fetch data from a specific product using discovered metadata.

        Args:
            product_id: Product ID to fetch from
            indicator: Indicator name for metadata
            metadata: Cube metadata from getCubeMetadata
            geography: Optional province/territory name
            periods: Number of periods to fetch
            start_date: Optional start date filter (ISO format)
            end_date: Optional end date filter (ISO format)

        Returns:
            NormalizedData with the results
        """
        # Validate geography if provided
        if geography:
            # This will raise ValueError for cities or non-Canadian countries
            self._resolve_geography(geography)

        # Extract available dimensions from metadata
        dimensions = metadata.get("dimension", [])
        indicator_lower = indicator.lower() if indicator else ""

        # Build coordinate by finding member IDs for each dimension
        # Use intelligent dimension matching based on indicator context
        coordinate_parts = []
        for dim_info in dimensions:
            dim_name = dim_info.get("dimensionNameEn", "").upper()
            dim_name_lower = dim_name.lower()
            members = dim_info.get("member", [])

            # Helper: Find best member by matching keywords
            def find_member_by_keywords(keywords: list) -> int | None:
                for kw in keywords:
                    for member in members:
                        member_name = member.get("memberNameEn", "").lower()
                        if kw.lower() in member_name:
                            return member.get("memberId")
                return None

            # 1. Geography dimension - match to specified geography or default to Canada
            if "geogr" in dim_name_lower:
                if geography:
                    geography_upper = geography.upper()
                    found_id = None
                    # Direct lookup
                    for member in members:
                        member_name = member.get("memberNameEn", "").upper()
                        if geography_upper == member_name or member_name.startswith(geography_upper):
                            found_id = member.get("memberId")
                            break
                    # Alias lookup
                    if not found_id:
                        canonical = self.GEOGRAPHY_ALIASES.get(geography_upper)
                        if canonical:
                            for member in members:
                                if canonical.upper() in member.get("memberNameEn", "").upper():
                                    found_id = member.get("memberId")
                                    break
                    coordinate_parts.append(found_id if found_id else 1)
                else:
                    coordinate_parts.append(1)  # Default to Canada

            # 2. Trade dimension - match based on indicator (balance, export, import)
            elif "trade" in dim_name_lower:
                if "balance" in indicator_lower:
                    found = find_member_by_keywords(["balance", "net"])
                elif "export" in indicator_lower:
                    found = find_member_by_keywords(["export", "exports"])
                elif "import" in indicator_lower:
                    found = find_member_by_keywords(["import", "imports"])
                else:
                    found = None
                coordinate_parts.append(found if found else 1)

            # 3. Component dimension (immigration, migration) - match based on indicator
            elif "component" in dim_name_lower:
                if any(x in indicator_lower for x in ["immigra", "immigrant", "permanent"]):
                    found = find_member_by_keywords(["immigrants", "immigration", "permanent"])
                elif "emigra" in indicator_lower:
                    found = find_member_by_keywords(["emigrants", "emigration"])
                else:
                    found = None
                coordinate_parts.append(found if found else 1)

            # 4. Adjustment dimension - prefer seasonally adjusted
            elif any(x in dim_name_lower for x in ["seasonal", "adjustment"]):
                found = find_member_by_keywords(["seasonally adjusted", "adjusted"])
                coordinate_parts.append(found if found else 1)

            # 5. Basis dimension - prefer balance of payments for trade data
            elif "basis" in dim_name_lower:
                found = find_member_by_keywords(["balance of payments", "bop"])
                coordinate_parts.append(found if found else 1)

            # 6. Default to first member (usually "Total" or "All")
            else:
                coordinate_parts.append(1)

        # Build coordinate string
        # WDS coordinates have 10 dimensions separated by dots, e.g., "1.2.3.0.0.0.0.0.0.0"
        coordinate = ".".join(str(p) for p in coordinate_parts[:10])
        # Pad with zeros if fewer than 10 dimensions
        while coordinate.count(".") < 9:
            coordinate += ".0"

        logger.info(f"📊 Fetching {product_id} with coordinate: {coordinate}")

        # Fetch data using coordinate
        # Use extended timeout (300s = 5 minutes) to handle complex multi-province queries
        # StatsCan API can be slow, especially for batch coordinate queries
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )

        response.raise_for_status()
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            error_msg = payload[0].get("object", "Unknown error") if payload else "Empty response"
            raise DataNotAvailableError(
                f"StatsCan query failed for {indicator}: {error_msg}"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(
                f"No data found for {indicator} from Statistics Canada"
            )

        # Extract metadata from response
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)

        frequency = self._map_frequency(freq_code)
        unit = self._map_scalar_factor(scalar_code) or "units"

        # Build data points
        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date range filter if specified
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build full indicator name
        indicator_name = indicator
        if geography:
            indicator_name = f"{geography} {indicator}"

        # Human-readable URL for data verification on Statistics Canada website
        # Use helper method to ensure 10-digit product ID format
        source_url = self._get_table_viewer_url(product_id)

        # Determine dataType from indicator name
        indicator_upper = indicator_name.upper()
        if "RATE" in indicator_upper or "PERCENT" in indicator_upper:
            data_type = "Rate"
        elif "INDEX" in indicator_upper:
            data_type = "Index"
        elif "CHANGE" in indicator_upper:
            data_type = "Percent Change"
        else:
            data_type = "Level"

        metadata_obj = Metadata(
            source="Statistics Canada",
            indicator=indicator_name,
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id}:{coordinate}",
            apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=None,  # Not available in dynamic discovery without metadata
            dataType=data_type,
            priceType=None,  # Not typically available in dynamic discovery
            description=indicator_name,
            notes=None,  # StatsCan doesn't provide detailed notes easily
            scaleFactor=self._map_scalar_factor(scalar_code) if scalar_code else None,
            startDate=data_points[0]["date"] if data_points else None,
            endDate=data_points[-1]["date"] if data_points else None,
        )

        return NormalizedData(metadata=metadata_obj, data=data_points)

    async def fetch_multi_province_data(
        self, params: Dict[str, any]
    ) -> List[NormalizedData]:
        """Fetch data for multiple provinces in a single batch API call.

        This method is optimized for multi-province queries by discovering the product's
        actual dimension structure and building proper coordinates for batch queries.

        Handles different product types (Population, Labour, Housing, etc.) by
        dynamically fetching metadata to determine dimension counts and member IDs.

        Args:
            params: Dictionary containing:
                - productId: Product ID or vector ID to query
                - indicator: Human-readable indicator name (e.g., "Population")
                - periods: Number of recent periods to fetch (default: 20)
                - provinces: List of province names or "all" for all provinces
                - dimensions: Dict for additional dimensions (labour_characteristic, gender, age, etc.)

        Returns:
            List of NormalizedData objects (one per province)

        Raises:
            ValueError: If product structure cannot be determined
            DataNotAvailableError: If no data can be retrieved
        """
        product_id_param = params.get("productId", self.POPULATION_DEMOGRAPHICS_PRODUCT)
        indicator = params.get("indicator", "Population")
        periods = params.get("periods", 20)
        provinces_param = params.get("provinces", "all")
        dimensions = params.get("dimensions", {})
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        # Handle case where productId is actually a vector ID (from metadata search)
        # Vector IDs are integers, product IDs are strings like "17100005"
        if isinstance(product_id_param, int):
            logger.info(f"🔄 Parameter is vector ID {product_id_param}, resolving to product ID...")
            try:
                product_id = await self._get_product_id_from_vector(product_id_param)
                logger.info(f"✅ Resolved vector {product_id_param} → product {product_id}")
            except ValueError as e:
                logger.warning(f"⚠️ Could not resolve product ID from vector {product_id_param}: {e}")
                raise ValueError(f"Cannot use batch method: {e}")
        else:
            product_id = str(product_id_param)

        # IMPORTANT: Discover actual product structure to build correct coordinates
        logger.info(f"📊 Discovering dimension structure for product {product_id}...")
        try:
            metadata = await self._get_cube_metadata(product_id)
        except Exception as e:
            logger.warning(f"⚠️ Could not get metadata for {product_id}: {e}")
            raise ValueError(f"Cannot determine product structure: {e}")

        # Extract dimensions from metadata
        dimensions_list = metadata.get("dimension", [])
        if not dimensions_list:
            raise ValueError(f"Product {product_id} has no dimensions")

        logger.info(f"Product {product_id} has {len(dimensions_list)} dimensions")

        # Build a mapping of dimension names to their indices and member mappings
        dimension_mappings = {}
        for dim_idx, dim_info in enumerate(dimensions_list):
            dim_name = dim_info.get("dimensionNameEn", "").upper()
            dim_members = dim_info.get("member", [])

            # Create member ID mappings by name
            member_map = {}
            for member in dim_members:
                member_name = member.get("memberNameEn", "").upper()
                member_id = member.get("memberId")
                if member_name and member_id:
                    member_map[member_name] = member_id

            dimension_mappings[dim_name] = {
                "index": dim_idx,
                "member_map": member_map,
                "members": dim_members
            }

            logger.debug(f"  Dimension {dim_idx} ({dim_name}): {len(member_map)} members")

        # Determine which provinces to query
        if provinces_param == "all" or provinces_param is None:
            # All provinces except Canada total (ID=1)
            provinces_to_query = [
                name for name, member_id in self.GEOGRAPHY_MEMBER_IDS.items()
                if member_id != 1 and member_id < 15  # Exclude Canada, keep provinces/territories
            ]
        elif isinstance(provinces_param, list):
            provinces_to_query = provinces_param
        else:
            provinces_to_query = [provinces_param]

        # Validate all provinces before building requests
        for province_name in provinces_to_query:
            try:
                # This will raise ValueError for cities or non-Canadian countries
                self._resolve_geography(province_name)
            except ValueError as e:
                # Re-raise with context about multi-province query
                raise ValueError(
                    f"Invalid geography in multi-province query: {str(e)}"
                )

        # Build coordinate requests for each province
        coordinate_requests = []
        province_map = {}  # Map coordinate to province name for response parsing

        for province_name in provinces_to_query:
            province_upper = province_name.upper()

            # Check geography aliases first (short forms like "ON", "QC")
            if province_upper in self.GEOGRAPHY_ALIASES:
                canonical_name = self.GEOGRAPHY_ALIASES[province_upper]
                province_upper = canonical_name.upper()

            geography_id = self.GEOGRAPHY_MEMBER_IDS.get(province_upper)

            if not geography_id:
                logger.warning(f"⚠️ Unknown province '{province_name}', skipping")
                continue

            # Build coordinate by iterating through dimensions
            # Start with geography ID at index 0
            coordinate_parts = [str(geography_id)]

            # Fill in remaining dimensions based on metadata
            for dim_idx in range(1, len(dimensions_list)):
                dim_info = dimensions_list[dim_idx]
                dim_name = dim_info.get("dimensionNameEn", "").upper()

                # Try to find a matching dimension value from params
                member_id = 1  # Default to first member (usually "Total" or "All")

                # Check if user provided a value for this dimension
                if "LABOUR" in dim_name and "CHARACTERISTIC" in dim_name:
                    # Labour force characteristic dimension
                    labour_char = dimensions.get("labour_characteristic") or dimensions.get("characteristic")
                    if labour_char:
                        labour_char_upper = labour_char.upper()
                        # Try to find in member map
                        for member in dim_info.get("member", []):
                            if labour_char_upper in member.get("memberNameEn", "").upper():
                                member_id = member.get("memberId", 1)
                                break
                elif "GENDER" in dim_name or "SEX" in dim_name:
                    gender = dimensions.get("gender")
                    if gender:
                        gender_upper = gender.upper()
                        member_id = self.GENDER_MEMBER_IDS.get(gender_upper, 1)
                elif "AGE" in dim_name:
                    age = dimensions.get("age")
                    if age:
                        age_upper = age.upper()
                        member_id = self.AGE_GROUP_MEMBER_IDS.get(age_upper, 1)
                elif "STATISTIC" in dim_name:
                    # Statistics dimension (Estimate, Standard Error, etc.)
                    member_id = 1  # Default to "Estimate"

                coordinate_parts.append(str(member_id))

            # Pad coordinate to 10 dimensions (StatsCan requirement)
            while len(coordinate_parts) < 10:
                coordinate_parts.append("0")

            coordinate = ".".join(coordinate_parts[:10])
            coordinate_requests.append({
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods
            })
            province_map[coordinate] = province_name

            logger.debug(f"  {province_name}: {coordinate}")

        if not coordinate_requests:
            raise ValueError(f"No valid provinces found in: {provinces_to_query}")

        logger.info(
            f"Fetching {indicator} for {len(coordinate_requests)} provinces "
            f"in batch (single API call)"
        )

        # Make single batch API call with all coordinates
        # Use extended timeout for multi-province queries (300s = 5 minutes)
        # This prevents timeouts when StatsCan API is slow
        try:
            # Wait for rate limiter before making request
            wait_delay = await wait_for_provider("StatsCan")
            if wait_delay > 0:
                logger.info(f"⏳ StatsCan rate limiter applied {wait_delay:.1f}s delay")

            # Use shared HTTP client pool for better performance
            client = get_http_client()
            response = await client.post(
                f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
                json=coordinate_requests,  # Send array of coordinate requests
                headers={"Content-Type": "application/json"},
                timeout=300.0,
            )

            if response.status_code == 406:
                raise DataNotAvailableError(
                    f"Invalid coordinate format for product {product_id}"
                )

            response.raise_for_status()
            payload = response.json()

            # Record this request for rate limiting
            record_provider_request("StatsCan")

        except httpx.TimeoutException:
            raise DataNotAvailableError(
                f"StatsCan API timeout after 300 seconds for {len(coordinate_requests)} provinces. "
                f"The batch query took too long. Try reducing the time period or number of provinces."
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise DataNotAvailableError(
                    f"StatsCan API rate limit exceeded. Please try again in a few moments."
                )
            raise

        # Parse results for each province
        results = []
        failed_provinces = []

        for i, result_obj in enumerate(payload):
            # Extract coordinate from nested object (StatsCan API structure)
            data_object = result_obj.get("object", {})
            coordinate = data_object.get("coordinate", "")
            province_name = province_map.get(coordinate, f"Province_{i+1}")

            if result_obj.get("status") != "SUCCESS":
                status_code = data_object.get("responseStatusCode", result_obj.get("responseStatusCode", "?"))
                error_msg = data_object if isinstance(data_object, str) else "Unknown error"
                logger.warning(
                    f"Province '{province_name}' query failed: Code {status_code} - {error_msg}"
                )
                failed_provinces.append(province_name)
                continue

            vector_data = data_object.get("vectorDataPoint", [])

            if not vector_data:
                logger.warning(f"⚠️ No data returned for {province_name}")
                failed_provinces.append(province_name)
                continue

            # Extract metadata
            frequency_code = vector_data[0].get("frequencyCode", 6)
            frequency = self.FREQUENCY_MAP.get(frequency_code, "unknown")
            scalar_factor = vector_data[0].get("scalarFactorCode", 0)
            unit = self.SCALAR_FACTOR_MAP.get(scalar_factor, "")

            # Human-readable URL for data verification on Statistics Canada website
            # Use helper method to ensure 10-digit product ID format
            source_url = self._get_table_viewer_url(product_id)

            # Build data points first (needed for startDate/endDate)
            data_points = [
                {
                    "date": point["refPer"],
                    "value": point["value"] if point["value"] is not None else None,
                }
                for point in vector_data
            ]

            # Apply date range filter if specified
            if start_date or end_date:
                data_points = self._filter_by_date_range(data_points, start_date, end_date)

            # Determine dataType from indicator name
            indicator_name = f"{province_name} {indicator}"
            indicator_upper = indicator_name.upper()
            if "RATE" in indicator_upper or "PERCENT" in indicator_upper:
                data_type = "Rate"
            elif "INDEX" in indicator_upper:
                data_type = "Index"
            elif "CHANGE" in indicator_upper:
                data_type = "Percent Change"
            else:
                data_type = "Level"

            metadata = Metadata(
                source="Statistics Canada",
                indicator=indicator_name,
                country="Canada",
                frequency=frequency,
                unit=unit if unit else "persons",
                lastUpdated=(vector_data[-1].get("releaseTime") or "") if vector_data else "",
                seriesId=f"{product_id}:{coordinate}",
                apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
                sourceUrl=source_url,
                # Enhanced metadata fields
                seasonalAdjustment=None,  # Not available in multi-province batch queries
                dataType=data_type,
                priceType=None,  # Not typically available in batch queries
                description=indicator_name,
                notes=None,  # StatsCan doesn't provide detailed notes easily
                scaleFactor=self._map_scalar_factor(scalar_factor) if scalar_factor else None,
                startDate=data_points[0]["date"] if data_points else None,
                endDate=data_points[-1]["date"] if data_points else None,
            )

            results.append(NormalizedData(metadata=metadata, data=data_points))

        if not results:
            provinces_str = ", ".join(failed_provinces) if failed_provinces else "all provinces"
            raise DataNotAvailableError(
                f"No data returned for any province. Failed: {provinces_str}. "
                f"The product {product_id} may not support the requested dimension combination. "
                f"Try using a single province or a different indicator."
            )

        if failed_provinces:
            logger.warning(f"⚠️ Data retrieved for {len(results)}/{len(coordinate_requests)} provinces. "
                          f"Failed: {', '.join(failed_provinces[:5])}")
        else:
            logger.info(f"✅ Successfully fetched data for {len(results)} provinces")

        return results
