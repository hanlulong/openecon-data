from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING
import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client, get_http1_client, effective_timeout
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from .base import BaseProvider

if TYPE_CHECKING:
    from ..services.metadata_search import MetadataSearchService

logger = logging.getLogger(__name__)


class IMFProvider(BaseProvider):
    """International Monetary Fund (IMF) DataMapper API provider.

    Uses the IMF DataMapper API to retrieve economic indicators for countries worldwide.
    No API key required for basic access.

    PHASE D: Now inherits from BaseProvider for:
    - Unified provider_name property
    - Standardized HTTP retry logic
    - Common error handling patterns

    API Documentation: https://www.imf.org/external/datamapper/api/help
    """

    SDMX_DATA_BASE_URL = "https://api.imf.org/external/sdmx/2.1/data/IMF.STA"
    SDMX_STRUCTURE_BASE_URL = "https://api.imf.org/external/sdmx/2.1/dataflow/IMF.STA"
    _DATAFLOW_STRUCTURE_CACHE: Dict[str, Dict[str, Any]] = {}

    # FALLBACK Regional/group mappings (map region name to list of country codes)
    # NOTE: CountryResolver (backend/routing/country_resolver.py) is the PRIMARY source.
    # This dict is only used as fallback for IMF-specific regions not in CountryResolver.
    # Common regions like EUROZONE, ASIA, OECD, G7, G20, BRICS are handled by CountryResolver.
    # The regions below are IMF-specific classifications (DEVELOPED_ECONOMIES, EMERGING_MARKETS, etc.)
    REGION_MAPPINGS: Dict[str, List[str]] = {
        # NOTE: EUROZONE/ASIA/OECD etc. are handled by CountryResolver first in _resolve_countries()
        # These entries are kept as fallback but should not normally be reached.

        # Developed economies (OECD + high-income countries) - IMF WEO classification
        "DEVELOPED_ECONOMIES": ["USA", "CAN", "GBR", "DEU", "FRA", "ITA", "ESP", "JPN", "KOR", "AUS",
                                 "NZL", "NLD", "BEL", "AUT", "CHE", "NOR", "SWE", "DNK", "FIN", "IRL", "ISL"],
        "DEVELOPED_COUNTRIES": ["USA", "CAN", "GBR", "DEU", "FRA", "ITA", "ESP", "JPN", "KOR", "AUS",
                                 "NZL", "NLD", "BEL", "AUT", "CHE", "NOR", "SWE", "DNK", "FIN", "IRL", "ISL"],
        "ADVANCED_ECONOMIES": ["USA", "CAN", "GBR", "DEU", "FRA", "ITA", "ESP", "JPN", "KOR", "AUS",
                                "NZL", "NLD", "BEL", "AUT", "CHE", "NOR", "SWE", "DNK", "FIN", "IRL", "ISL"],

        # Emerging markets and developing economies (EMDE)
        # Comprehensive list covering all major emerging and developing regions
        "EMERGING_MARKETS": ["CHN", "IND", "BRA", "RUS", "ZAF", "MEX", "IDN", "TUR", "SAU", "ARG",
                             "THA", "MYS", "POL", "PHL", "EGY", "PAK", "VNM", "CHL", "COL", "PER"],
        "EMERGING_MARKET_ECONOMIES": ["CHN", "IND", "BRA", "RUS", "ZAF", "MEX", "IDN", "TUR", "SAU", "ARG",
                                       "THA", "MYS", "POL", "PHL", "EGY", "PAK", "VNM", "CHL", "COL", "PER"],
        "EMERGING_ECONOMIES": ["CHN", "IND", "BRA", "RUS", "ZAF", "MEX", "IDN", "TUR", "SAU", "ARG",
                               "THA", "MYS", "POL", "PHL", "EGY", "PAK", "VNM", "CHL", "COL", "PER"],

        # Developing economies (EMDE - combines emerging markets + developing countries)
        # Based on IMF WEO classification of emerging market and developing economies
        "DEVELOPING_ECONOMIES": [
            # Emerging and Developing Asia
            "CHN", "IND", "IDN", "THA", "MYS", "PHL", "VNM", "BGD", "PAK", "MMR", "KHM", "LAO",
            # Emerging and Developing Europe
            "RUS", "TUR", "POL", "UKR", "ROU", "HUN", "CZE", "BGR", "HRV", "SRB",
            # Latin America and the Caribbean
            "BRA", "MEX", "ARG", "COL", "CHL", "PER", "VEN", "ECU", "GTM", "CUB", "URY", "PRY", "BOL",
            # Middle East and Central Asia
            "SAU", "IRN", "ARE", "IRQ", "QAT", "KWT", "OMN", "JOR", "LBN", "KAZ", "UZB", "AZE",
            # Sub-Saharan Africa
            "ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "TZA", "UGA", "DZA", "MAR", "AGO", "SDN",
        ],
        "DEVELOPING_COUNTRIES": [
            # Same as DEVELOPING_ECONOMIES
            "CHN", "IND", "IDN", "THA", "MYS", "PHL", "VNM", "BGD", "PAK", "MMR", "KHM", "LAO",
            "RUS", "TUR", "POL", "UKR", "ROU", "HUN", "CZE", "BGR", "HRV", "SRB",
            "BRA", "MEX", "ARG", "COL", "CHL", "PER", "VEN", "ECU", "GTM", "CUB", "URY", "PRY", "BOL",
            "SAU", "IRN", "ARE", "IRQ", "QAT", "KWT", "OMN", "JOR", "LBN", "KAZ", "UZB", "AZE",
            "ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "TZA", "UGA", "DZA", "MAR", "AGO", "SDN",
        ],
        "EMDE": [
            # Emerging Market and Developing Economies (IMF official classification)
            "CHN", "IND", "IDN", "THA", "MYS", "PHL", "VNM", "BGD", "PAK", "MMR", "KHM", "LAO",
            "RUS", "TUR", "POL", "UKR", "ROU", "HUN", "CZE", "BGR", "HRV", "SRB",
            "BRA", "MEX", "ARG", "COL", "CHL", "PER", "VEN", "ECU", "GTM", "CUB", "URY", "PRY", "BOL",
            "SAU", "IRN", "ARE", "IRQ", "QAT", "KWT", "OMN", "JOR", "LBN", "KAZ", "UZB", "AZE",
            "ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "TZA", "UGA", "DZA", "MAR", "AGO", "SDN",
        ],

        # G7
        "G7": ["USA", "JPN", "DEU", "GBR", "FRA", "ITA", "CAN"],
        "G_7": ["USA", "JPN", "DEU", "GBR", "FRA", "ITA", "CAN"],
        "GROUP_OF_7": ["USA", "JPN", "DEU", "GBR", "FRA", "ITA", "CAN"],

        # G20 (major economies)
        "G20": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "BRA", "ITA", "CAN",
                "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],
        "G_20": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "BRA", "ITA", "CAN",
                 "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],
        "GROUP_OF_20": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "BRA", "ITA", "CAN",
                        "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],

        # BRICS
        "BRICS": ["BRA", "RUS", "IND", "CHN", "ZAF"],
        "BRICS_COUNTRIES": ["BRA", "RUS", "IND", "CHN", "ZAF"],

        # BRICS+ (2024 expansion - includes Egypt, Ethiopia, Iran, UAE)
        "BRICS_PLUS": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],
        "BRICS+": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],

        # OECD (38 members as of 2024)
        # Comprehensive list of all OECD member countries
        "OECD": ["AUS", "AUT", "BEL", "CAN", "CHL", "COL", "CRI", "CZE",
                 "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "ISL",
                 "IRL", "ISR", "ITA", "JPN", "KOR", "LVA", "LTU", "LUX",
                 "MEX", "NLD", "NZL", "NOR", "POL", "PRT", "SVK", "SVN",
                 "ESP", "SWE", "CHE", "TUR", "GBR", "USA"],
        "OECD_COUNTRIES": ["AUS", "AUT", "BEL", "CAN", "CHL", "COL", "CRI", "CZE",
                           "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "ISL",
                           "IRL", "ISR", "ITA", "JPN", "KOR", "LVA", "LTU", "LUX",
                           "MEX", "NLD", "NZL", "NOR", "POL", "PRT", "SVK", "SVN",
                           "ESP", "SWE", "CHE", "TUR", "GBR", "USA"],
        "ALL_OECD": ["AUS", "AUT", "BEL", "CAN", "CHL", "COL", "CRI", "CZE",
                     "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "ISL",
                     "IRL", "ISR", "ITA", "JPN", "KOR", "LVA", "LTU", "LUX",
                     "MEX", "NLD", "NZL", "NOR", "POL", "PRT", "SVK", "SVN",
                     "ESP", "SWE", "CHE", "TUR", "GBR", "USA"],
        "ALL_OECD_COUNTRIES": ["AUS", "AUT", "BEL", "CAN", "CHL", "COL", "CRI", "CZE",
                               "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "ISL",
                               "IRL", "ISR", "ITA", "JPN", "KOR", "LVA", "LTU", "LUX",
                               "MEX", "NLD", "NZL", "NOR", "POL", "PRT", "SVK", "SVN",
                               "ESP", "SWE", "CHE", "TUR", "GBR", "USA"],
        "OECD_MEMBER": ["AUS", "AUT", "BEL", "CAN", "CHL", "COL", "CRI", "CZE",
                        "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "ISL",
                        "IRL", "ISR", "ITA", "JPN", "KOR", "LVA", "LTU", "LUX",
                        "MEX", "NLD", "NZL", "NOR", "POL", "PRT", "SVK", "SVN",
                        "ESP", "SWE", "CHE", "TUR", "GBR", "USA"],
        "OECD_MEMBERS": ["AUS", "AUT", "BEL", "CAN", "CHL", "COL", "CRI", "CZE",
                         "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "ISL",
                         "IRL", "ISR", "ITA", "JPN", "KOR", "LVA", "LTU", "LUX",
                         "MEX", "NLD", "NZL", "NOR", "POL", "PRT", "SVK", "SVN",
                         "ESP", "SWE", "CHE", "TUR", "GBR", "USA"],

        # EU (European Union) - 27 members
        "EU": ["DEU", "FRA", "ITA", "ESP", "POL", "ROU", "NLD", "BEL", "GRC", "CZE", "PRT",
               "SWE", "HUN", "AUT", "BGR", "DNK", "FIN", "SVK", "IRL", "HRV", "LTU", "SVN",
               "LVA", "EST", "CYP", "LUX", "MLT"],
        "EUROPEAN_UNION": ["DEU", "FRA", "ITA", "ESP", "POL", "ROU", "NLD", "BEL", "GRC", "CZE", "PRT",
                           "SWE", "HUN", "AUT", "BGR", "DNK", "FIN", "SVK", "IRL", "HRV", "LTU", "SVN",
                           "LVA", "EST", "CYP", "LUX", "MLT"],

        # Nordic countries
        "NORDIC": ["NOR", "SWE", "DNK", "FIN", "ISL"],
        "NORDIC_COUNTRIES": ["NOR", "SWE", "DNK", "FIN", "ISL"],

        # Latin America (major economies)
        "LATIN_AMERICA": ["BRA", "MEX", "ARG", "COL", "CHL", "PER", "VEN", "ECU", "GTM", "CUB"],
        "SOUTH_AMERICA": ["BRA", "ARG", "COL", "CHL", "PER", "VEN", "ECU", "URY", "PRY", "BOL", "GUY", "SUR"],

        # Middle East (major economies)
        "MIDDLE_EAST": ["SAU", "ARE", "ISR", "TUR", "IRN", "IRQ", "QAT", "KWT", "OMN", "JOR", "LBN"],

        # Africa (major economies)
        "AFRICAN_COUNTRIES": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "TZA", "UGA", "DZA", "MAR"],
        "AFRICA": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "TZA", "UGA", "DZA", "MAR"],

        # ASEAN
        "ASEAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],

        # Top economies (by GDP)
        "TOP_10_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP_20_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                             "RUS", "KOR", "AUS", "ESP", "MEX", "IDN", "NLD", "SAU", "TUR", "CHE"],
        "TOP_20_COUNTRIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                             "RUS", "KOR", "AUS", "ESP", "MEX", "IDN", "NLD", "SAU", "TUR", "CHE"],
        "MAJOR_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "MAJOR_COUNTRIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],

        # Global/worldwide (use top economies as proxy)
        "GLOBALLY": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                     "RUS", "KOR", "AUS", "ESP", "MEX", "IDN", "NLD", "SAU", "TUR", "CHE"],
        "WORLDWIDE": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                      "RUS", "KOR", "AUS", "ESP", "MEX", "IDN", "NLD", "SAU", "TUR", "CHE"],
        "WORLD": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                  "RUS", "KOR", "AUS", "ESP", "MEX", "IDN", "NLD", "SAU", "TUR", "CHE"],
        "GLOBAL": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                   "RUS", "KOR", "AUS", "ESP", "MEX", "IDN", "NLD", "SAU", "TUR", "CHE"],

        # Major currency areas
        "MAJOR_CURRENCIES": ["USA", "JPN", "GBR", "CHE", "CAN", "AUS", "NZL", "NOR", "SWE"],  # USD, EUR (covered by Eurozone), JPY, GBP, CHF, CAD, AUD, NZD, NOK, SEK

        # Oil exporting countries (OPEC+ major members)
        "OIL_EXPORTING": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OIL_EXPORTING_COUNTRIES": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OIL_EXPORTERS": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OPEC": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
        "OPEC_COUNTRIES": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
    }

    COUNTRY_MAPPINGS: Dict[str, str] = {
        # Common abbreviations
        "US": "USA",
        "USA": "USA",
        "UK": "GBR",
        "GB": "GBR",

        # European countries (ISO 3166-1 alpha-3 codes)
        "GERMANY": "DEU",
        "DE": "DEU",
        "FRANCE": "FRA",
        "FR": "FRA",
        "ITALY": "ITA",
        "IT": "ITA",
        "SPAIN": "ESP",
        "ES": "ESP",
        "PORTUGAL": "PRT",
        "PT": "PRT",
        "GREECE": "GRC",  # Fixed: was missing, causing "GREECE" instead of "GRC"
        "GR": "GRC",
        "NETHERLANDS": "NLD",
        "NL": "NLD",
        "BELGIUM": "BEL",
        "BE": "BEL",
        "AUSTRIA": "AUT",
        "AT": "AUT",
        "IRELAND": "IRL",
        "IE": "IRL",
        "FINLAND": "FIN",
        "FI": "FIN",
        "SWEDEN": "SWE",
        "SE": "SWE",
        "DENMARK": "DNK",
        "DK": "DNK",
        "POLAND": "POL",
        "PL": "POL",
        "CZECH_REPUBLIC": "CZE",
        "CZECHIA": "CZE",
        "CZ": "CZE",
        "HUNGARY": "HUN",
        "HU": "HUN",
        "ROMANIA": "ROU",
        "RO": "ROU",
        "BULGARIA": "BGR",
        "BG": "BGR",
        "CROATIA": "HRV",
        "HR": "HRV",
        "SLOVAKIA": "SVK",
        "SK": "SVK",
        "SLOVENIA": "SVN",
        "SI": "SVN",
        "LITHUANIA": "LTU",
        "LT": "LTU",
        "LATVIA": "LVA",
        "LV": "LVA",
        "ESTONIA": "EST",
        "EE": "EST",
        "SWITZERLAND": "CHE",
        "CH": "CHE",
        "NORWAY": "NOR",
        "NO": "NOR",
        "ICELAND": "ISL",
        "IS": "ISL",
        "LUXEMBOURG": "LUX",
        "LU": "LUX",
        "MALTA": "MLT",
        "MT": "MLT",
        "CYPRUS": "CYP",
        "CY": "CYP",

        # Other major countries
        "JAPAN": "JPN",
        "JP": "JPN",
        "CHINA": "CHN",
        "CN": "CHN",
        "INDIA": "IND",
        "IN": "IND",
        "CANADA": "CAN",
        "CA": "CAN",
        "AUSTRALIA": "AUS",
        "AU": "AUS",
        "BRAZIL": "BRA",
        "BR": "BRA",
        "RUSSIA": "RUS",
        "RU": "RUS",
        "MEXICO": "MEX",
        "MX": "MEX",
        "SOUTH_KOREA": "KOR",
        "KOREA": "KOR",
        "KR": "KOR",
        "INDONESIA": "IDN",
        "ID": "IDN",
        "TURKEY": "TUR",
        "TR": "TUR",
        "SAUDI_ARABIA": "SAU",
        "SA": "SAU",
        "ARGENTINA": "ARG",
        "AR": "ARG",
        "SOUTH_AFRICA": "ZAF",
        "ZA": "ZAF",
        "THAILAND": "THA",
        "TH": "THA",
        "MALAYSIA": "MYS",
        "MY": "MYS",
        "SINGAPORE": "SGP",
        "SG": "SGP",
        "PHILIPPINES": "PHL",
        "PH": "PHL",
        "VIETNAM": "VNM",
        "VN": "VNM",
        "PAKISTAN": "PAK",
        "PK": "PAK",
        "BANGLADESH": "BGD",
        "BD": "BGD",
        "EGYPT": "EGY",
        "EG": "EGY",
        "NIGERIA": "NGA",
        "NG": "NGA",
        "CHILE": "CHL",
        "CL": "CHL",
        "COLOMBIA": "COL",
        "CO": "COL",
        "PERU": "PER",
        "PE": "PER",
        "NEW_ZEALAND": "NZL",
        "NZ": "NZL",
        "ISRAEL": "ISR",
        "IL": "ISR",
        "UAE": "ARE",
        "UNITED_ARAB_EMIRATES": "ARE",
        "AE": "ARE",
    }

    # Reverse mapping: ISO 3166-1 alpha-3 codes to display names
    CODE_TO_COUNTRY_NAME: Dict[str, str] = {
        "USA": "United States",
        "GBR": "United Kingdom",
        "DEU": "Germany",
        "FRA": "France",
        "ITA": "Italy",
        "ESP": "Spain",
        "PRT": "Portugal",
        "GRC": "Greece",
        "NLD": "Netherlands",
        "BEL": "Belgium",
        "AUT": "Austria",
        "IRL": "Ireland",
        "FIN": "Finland",
        "SWE": "Sweden",
        "DNK": "Denmark",
        "POL": "Poland",
        "CZE": "Czech Republic",
        "HUN": "Hungary",
        "ROU": "Romania",
        "BGR": "Bulgaria",
        "HRV": "Croatia",
        "SVK": "Slovakia",
        "SVN": "Slovenia",
        "LTU": "Lithuania",
        "LVA": "Latvia",
        "EST": "Estonia",
        "CHE": "Switzerland",
        "NOR": "Norway",
        "ISL": "Iceland",
        "LUX": "Luxembourg",
        "MLT": "Malta",
        "CYP": "Cyprus",
        "JPN": "Japan",
        "CHN": "China",
        "IND": "India",
        "CAN": "Canada",
        "AUS": "Australia",
        "BRA": "Brazil",
        "RUS": "Russia",
        "MEX": "Mexico",
        "KOR": "South Korea",
        "IDN": "Indonesia",
        "TUR": "Turkey",
        "SAU": "Saudi Arabia",
        "ARG": "Argentina",
        "ZAF": "South Africa",
        "THA": "Thailand",
        "MYS": "Malaysia",
        "SGP": "Singapore",
        "PHL": "Philippines",
        "VNM": "Vietnam",
        "PAK": "Pakistan",
        "BGD": "Bangladesh",
        "EGY": "Egypt",
        "NGA": "Nigeria",
        "CHL": "Chile",
        "COL": "Colombia",
        "PER": "Peru",
        "NZL": "New Zealand",
        "ISR": "Israel",
        "ARE": "United Arab Emirates",
    }

    @property
    def provider_name(self) -> str:
        """Return canonical provider name for logging and routing."""
        return "IMF"

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)  # Initialize BaseProvider
        settings = get_settings()
        self.base_url = settings.imf_base_url.rstrip("/")
        self.engine_base_url = "https://data.imf.org"
        self.metadata_search = metadata_search_service

    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Implementation of BaseProvider's abstract method.

        Routes to fetch_indicator with appropriate parameters.
        """
        indicator = params.get("indicator", "GDP")
        country = params.get("country") or params.get("region", "US")
        start_year = params.get("start_year") or params.get("startDate", "").split("-")[0] if params.get("startDate") else None
        end_year = params.get("end_year") or params.get("endDate", "").split("-")[0] if params.get("endDate") else None

        return await self.fetch_indicator(
            indicator=indicator,
            country=country,
            start_year=int(start_year) if start_year else None,
            end_year=int(end_year) if end_year else None,
        )

    async def _retry_request(self, url: str, max_retries: int = 3, initial_delay: float = 1.0):
        """Execute HTTP request with exponential backoff retry logic.

        Args:
            url: URL to request
            max_retries: Maximum number of retry attempts
            initial_delay: Initial delay in seconds (doubles on each retry)

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPError: If all retries fail
        """
        last_error = None

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        for attempt in range(max_retries):
            try:
                logger.info(f"IMF API request (attempt {attempt + 1}/{max_retries}): {url}")
                response = await client.get(url, timeout=effective_timeout(60.0))
                response.raise_for_status()
                return response

            except (httpx.HTTPError, httpx.TimeoutException) as e:
                last_error = e

                # Log the error
                if attempt < max_retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    logger.warning(
                        f"IMF API request failed (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"IMF API request failed after {max_retries} attempts: {e}")

        # All retries exhausted
        raise last_error

    @staticmethod
    def _looks_like_imf_code(indicator: str) -> bool:
        """Heuristic check for IMF code-like indicator strings."""
        token = str(indicator or "").strip().upper()
        return bool(token and re.fullmatch(r"[A-Z][A-Z0-9_]{2,32}", token))

    def _friendly_indicator_label(self, requested_indicator: str, indicator_code: str) -> str:
        """
        Resolve a human-readable indicator label when the request is code-like.
        """
        requested = str(requested_indicator or "").strip()
        if requested and not self._looks_like_imf_code(requested):
            return requested

        try:
            from ..services.catalog_service import find_concepts_by_code, get_provider_info

            concepts = find_concepts_by_code("IMF", indicator_code)
            for concept in concepts:
                provider_info = get_provider_info(concept, "IMF") or {}
                primary = provider_info.get("primary", {})
                if isinstance(primary, dict):
                    label = str(primary.get("name") or "").strip()
                    if label:
                        return label
        except Exception as exc:
            logger.debug(
                "Could not resolve friendly IMF label for %s (%s): %s",
                indicator_code,
                requested_indicator,
                exc,
            )

        return indicator_code

    def _resolve_countries(self, country_or_region: str) -> List[str]:
        """Resolve country/region to list of IMF country codes.

        Uses CountryResolver as the single source of truth for region definitions.
        Falls back to IMF-specific mappings for specialized regions.

        Handles:
        - Single countries: "USA", "Germany" -> ["USA"], ["DEU"]
        - Regional groups: "Eurozone", "Asian countries" -> ["DEU", "FRA", ...], ["CHN", "JPN", ...]

        Returns:
            List of IMF country codes (ISO 3166-1 alpha-3)
        """
        from ..routing.country_resolver import CountryResolver

        key = country_or_region.upper().replace(" ", "_")

        # First, try CountryResolver (single source of truth for standard regions)
        expanded = CountryResolver.get_region_expansion(key, format="iso3")
        if expanded:
            logger.info(f"🌍 Resolved region '{country_or_region}' via CountryResolver → {len(expanded)} countries")
            return expanded

        # Try variant names
        for variant in [key, key.replace("_COUNTRIES", ""), key.replace("_NATIONS", "")]:
            expanded = CountryResolver.get_region_expansion(variant, format="iso3")
            if expanded:
                logger.info(f"🌍 Matched region '{variant}' via CountryResolver → {len(expanded)} countries")
                return expanded

        # Fall back to IMF-specific regional groups (DEVELOPED_ECONOMIES, EMERGING_MARKETS, etc.)
        if key in self.REGION_MAPPINGS:
            countries = self.REGION_MAPPINGS[key]
            logger.info(f"🌍 Resolved region '{country_or_region}' via IMF mappings → {len(countries)} countries")
            return countries

        # Otherwise treat as single country
        return [self._country_code(country_or_region)]

    def _country_code(self, country: str) -> str:
        """Get IMF country code from common country name.

        CENTRALIZED: Uses CountryResolver as primary source, with fallback
        to IMF-specific COUNTRY_MAPPINGS for edge cases.

        Resolution order:
        1. Normalize country name → ISO2 via CountryResolver.normalize()
        2. Convert ISO2 → ISO3 via CountryResolver.to_iso3()
        3. Try direct ISO3 lookup (input may already be ISO3)
        4. Fallback to local COUNTRY_MAPPINGS
        """
        from ..routing.country_resolver import CountryResolver

        # Step 1: Normalize country name/alias to ISO2, then convert to ISO3
        iso2 = CountryResolver.normalize(country)
        if iso2:
            iso3 = CountryResolver.to_iso3(iso2)
            if iso3:
                return iso3

        # Step 2: Input might already be an ISO2 code (e.g. "JM")
        iso3 = CountryResolver.to_iso3(country)
        if iso3:
            return iso3

        # Step 3: Input might already be a valid ISO3 code (e.g. "JAM")
        iso2_check = CountryResolver.to_iso2(country)
        if iso2_check:
            return country.upper()

        # Step 4: Fallback to local mappings for edge cases
        key = country.upper().replace(" ", "_")
        return self.COUNTRY_MAPPINGS.get(key, country.upper())

    def _country_name(self, code: str) -> str:
        """Get display-friendly country name from ISO 3166-1 alpha-3 code."""
        value = str(code or "").strip().upper()
        if value in self.CODE_TO_COUNTRY_NAME:
            return self.CODE_TO_COUNTRY_NAME[value]
        try:
            from ..routing.country_resolver import CountryResolver

            iso2 = CountryResolver.to_iso2(value)
            if iso2:
                for name, candidate_iso2 in CountryResolver.COUNTRY_ALIASES.items():
                    if candidate_iso2 == iso2 and not re.fullmatch(r"[a-z]{2,3}", name):
                        return name.title()
        except Exception:
            pass
        return code

    async def fetch_indicator(
        self,
        indicator: str,
        country: str = "USA",
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> NormalizedData:
        """Fetch economic indicator data from IMF DataMapper API.

        Args:
            indicator: Indicator name (e.g., "GDP", "UNEMPLOYMENT") or IMF code
            country: Country name or ISO3 code
            start_year: Start year (optional, defaults to all available)
            end_year: End year (optional, defaults to all available)

        Returns:
            NormalizedData object with metadata and data points
        """
        # Use batch method to fetch single country
        results = await self.fetch_batch_indicator(
            indicator=indicator,
            countries=[country],
            start_year=start_year,
            end_year=end_year,
        )

        if not results:
            raise DataNotAvailableError(f"No data returned for {country} {indicator}")

        return results[0]

    async def fetch_batch_indicator(
        self,
        indicator: str,
        countries: list[str],
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> list[NormalizedData]:
        """Fetch economic indicator data for multiple countries from IMF DataMapper API.

        This method is optimized for multi-country queries - it makes a single API call
        that returns data for ALL countries, then filters to the requested countries.

        Args:
            indicator: Indicator name (e.g., "GDP", "UNEMPLOYMENT") or IMF code
            countries: List of country names or ISO3 codes
            start_year: Start year (optional, defaults to all available)
            end_year: End year (optional, defaults to all available)

        Returns:
            List of NormalizedData objects (one per country)
        """
        indicator_code, indicator_label = await self._resolve_indicator_code(indicator)
        execution_family = self._classify_execution_family(indicator_code)
        if execution_family == "NON_DATAMAPPER_INDICATOR":
            sdmx_candidates = self._build_sdmx_series_candidates(
                indicator_code=indicator_code,
                indicator_label=indicator_label,
                countries=countries,
            )
            if sdmx_candidates:
                return await self._fetch_sdmx_exact_indicator_family(
                    indicator_code=indicator_code,
                    indicator_label=indicator_label,
                    candidates=sdmx_candidates,
                    start_year=start_year,
                    end_year=end_year,
                )
            if self._is_disabled_bop_exact_code_shape(indicator_code):
                raise DataNotAvailableError(
                    f"IMF BOP exact-code execution is disabled under the no-rule authority policy for {indicator_code}. "
                    "The currently available BOP bridge requires label-to-codelist candidate matching, so this family "
                    "remains fail-closed until an exact provider-native dimension contract is implemented."
                )
            self._raise_for_unsupported_execution_family(indicator_code, indicator_label)

        # Convert all country names to IMF codes
        country_codes = [self._country_code(country) for country in countries]

        # Fetch data with retry logic
        url = f"{self.base_url}/{indicator_code}"

        try:
            payload = None
            json_error = None
            for parse_attempt in range(5):
                response = await self._retry_request(url, max_retries=4, initial_delay=1.0)
                try:
                    payload = response.json()
                    json_error = None
                    break
                except (ValueError, json.JSONDecodeError) as exc:
                    json_error = exc
                    if parse_attempt < 4:
                        logger.warning(
                            "IMF API returned invalid JSON for %s (attempt %s/5): %s. Retrying...",
                            indicator_code,
                            parse_attempt + 1,
                            exc,
                        )
                        await asyncio.sleep(0.5 * (parse_attempt + 1))
                    else:
                        raise
            if payload is None and json_error is not None:
                raise json_error
        except Exception as e:
            raise DataNotAvailableError(
                f"Failed to fetch IMF indicator {indicator_code} after retries. "
                f"Error: {e}. The IMF API may be temporarily unavailable."
            ) from e

        # Extract data for the already-resolved indicator. Do not swap to
        # another code here from the original natural-language query; a
        # different executable code must come from exact input or upstream
        # adjudication before fetch.
        if "values" not in payload or indicator_code not in payload["values"]:
            raise DataNotAvailableError(
                f"IMF indicator {indicator_code} not found in response"
            )

        all_country_data = payload["values"][indicator_code]

        # Determine indicator name
        indicator_name = indicator_label or indicator_code

        # Process each requested country
        results = []
        missing_countries = []  # Track countries with no data

        for country_code in country_codes:
            country_data = all_country_data.get(country_code)
            if not country_data:
                # Track missing country for better error message
                missing_countries.append(country_code)
                logger.warning(
                    f"No data found for country '{country_code}' in IMF indicator {indicator_code}. "
                    f"The country may not have data available for this indicator."
                )
                continue

            # Filter by year range if specified
            filtered_data = {}
            for year_str, value in country_data.items():
                try:
                    year = int(year_str)
                    if start_year and year < start_year:
                        continue
                    if end_year and year > end_year:
                        continue
                    filtered_data[year_str] = value
                except (ValueError, TypeError):
                    # Skip non-numeric years
                    continue

            if not filtered_data:
                logger.warning(
                    f"No data found for {country_code} {indicator_code} in specified year range "
                    f"({start_year or 'all'} to {end_year or 'all'})"
                )
                continue

            # Determine unit based on indicator code
            percent_indicators = [
                "NGDP_RPCH", "LUR", "PCPIPCH", "BCA_NGDPD", "GGXWDG_NGDP",
                "GGXCNL_NGDP", "rev", "exp", "prim_exp", "pb"
            ]
            unit = "percent" if indicator_code in percent_indicators else ""

            # Convert to data points (IMF uses year strings, convert to ISO date format)
            data_points = [
                {
                    "date": f"{year}-01-01",
                    "value": value if value is not None else None,
                }
                for year, value in sorted(filtered_data.items(), key=lambda x: int(x[0]))
            ]

            # Normalize percentage values (IMF sometimes stores as decimals)
            if unit == "percent":
                data_points = self._normalize_percentage_values(data_points, indicator_name)

            # Human-readable URL for data verification on IMF DataMapper website
            # Format: https://www.imf.org/external/datamapper/{INDICATOR_CODE}@WEO/{COUNTRY}
            source_url = f"https://www.imf.org/external/datamapper/{indicator_code}@WEO/{country_code}"

            # Build country-specific API URL for reproducibility
            # Format: https://www.imf.org/external/datamapper/api/v1/{INDICATOR_CODE}/{COUNTRY}
            api_url = f"{self.base_url}/{indicator_code}/{country_code}"

            # Determine dataType based on indicator code
            growth_indicators = ["NGDP_RPCH", "PCPIPCH"]  # Growth rates
            rate_indicators = ["LUR", "BCA_NGDPD", "GGXWDG_NGDP", "GGXCNL_NGDP", "rev", "exp", "prim_exp", "pb"]
            if indicator_code in growth_indicators:
                data_type = "Percent Change"
            elif indicator_code in rate_indicators:
                data_type = "Rate"
            else:
                data_type = "Level"

            # Extract start/end dates from data_points
            start_date = data_points[0]["date"] if data_points else None
            end_date = data_points[-1]["date"] if data_points else None

            metadata = Metadata(
                source="IMF",
                indicator=indicator_name,
                country=self._country_name(country_code),
                frequency="annual",
                unit=unit,
                lastUpdated="",  # IMF doesn't provide last updated date in DataMapper
                seriesId=indicator_code,
                apiUrl=api_url,
                sourceUrl=source_url,
                seasonalAdjustment=None,  # IMF DataMapper data is typically not seasonally adjusted
                dataType=data_type,
                priceType=None,  # IMF doesn't specify this clearly
                description=indicator_name,
                notes=None,
                startDate=start_date,
                endDate=end_date,
            )

            results.append(NormalizedData(metadata=metadata, data=data_points))

        if not results:
            # Provide detailed error message distinguishing different failure modes
            available_countries = sorted(all_country_data.keys())

            # Build detailed error message
            error_parts = []

            if missing_countries:
                error_parts.append(
                    f"IMF DataMapper API does not have '{indicator_name}' data for: {', '.join(missing_countries)}."
                )

            # Check if it's a country code issue (e.g., "GREECE" instead of "GRC")
            wrong_codes = [c for c in missing_countries if c not in available_countries and len(c) > 3]
            if wrong_codes:
                error_parts.append(
                    f"Potential country code mapping issue: {', '.join(wrong_codes)} "
                    f"(expected ISO 3166-1 alpha-3 codes like 'GRC', 'ESP', 'ITA')."
                )

            # Provide sample of available countries
            sample_countries = ', '.join(available_countries[:20])
            error_parts.append(
                f"Data is available for {len(available_countries)} countries including: {sample_countries}..."
            )

            # Check if requested countries exist in ANY IMF data
            if all(c in available_countries for c in missing_countries):
                error_parts.append(
                    f"Note: Requested countries exist in IMF database but don't have data for indicator '{indicator_code}'."
                )

            raise DataNotAvailableError(" ".join(error_parts))

        return results
    def _normalize_percentage_values(self, data: list[dict], indicator_name: str) -> list[dict]:
        """Delegate to the shared SDMX percentage-normalizer (Phase 3.1)."""
        from ._sdmx import normalize_percentage_values as _shared
        return _shared(data, label=indicator_name)

    @staticmethod
    def _indicator_catalog_lookup_keys(indicator_code: str) -> List[str]:
        """Return exact IMF catalog lookup keys, preserving provider-native case.

        IMF DataMapper has several executable non-WEO codes whose public API
        identifiers are mixed/lowercase (for example ``PrivInexDIGDP`` and
        ``prim_exp``).  The local catalog is case-sensitive for those rows, so
        exact-code resolution must try the provider-native spelling before the
        all-uppercase normalization used by SDMX exact-code families.
        """
        raw = str(indicator_code or "").strip()
        if not raw:
            return []
        return list(dict.fromkeys([raw, raw.upper()]))

    @staticmethod
    def _is_executable_datamapper_catalog_category(category: str) -> bool:
        """Return whether a catalog category can use legacy DataMapper v1.

        This is a provider-surface check for exact catalog codes only.  It does
        not infer codes from prose, and deliberately excludes ``INDICATOR``
        public-SDMX rows and ``DATAFLOW`` descriptors.
        """
        value = str(category or "").strip().upper()
        return bool(value and value not in {"INDICATOR", "DATAFLOW"})

    def _indicator_catalog_entry(self, indicator_code: str) -> Optional[Dict[str, Any]]:
        """Return the local IMF indicator catalog entry for a code when available."""
        lookup_keys = self._indicator_catalog_lookup_keys(indicator_code)
        if not lookup_keys:
            return None
        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
            for code in lookup_keys:
                entry = lookup.get("IMF", code)
                if entry:
                    return entry
        except Exception as exc:
            logger.debug("IMF indicator catalog lookup skipped for '%s': %s", indicator_code, exc)
        return None

    def _classify_execution_family(self, indicator_code: str) -> str:
        """Classify whether a resolved IMF code is executable on the DataMapper path."""
        entry = self._indicator_catalog_entry(indicator_code) or {}
        category = str(entry.get("category") or "").strip().upper()
        if category == "INDICATOR":
            return "NON_DATAMAPPER_INDICATOR"
        if category:
            return f"DATAMAPPER_{category}"
        return "DATAMAPPER_UNKNOWN"

    def _likely_dataset_family_hint(
        self,
        indicator_code: str,
        indicator_label: Optional[str],
    ) -> Optional[str]:
        """Diagnostic-only hint for offline triage of non-DataMapper series."""
        code = str(indicator_code or "").strip().upper()
        label = str(indicator_label or "").strip().lower()
        if not code and not label:
            return None

        if (
            "balance of payments" in label
            or "_BP6_" in code
            or code.startswith(("BX", "BM", "BS"))
        ):
            return "IMF.STA:BOP"

        if (
            re.search(r"\b(?:labou?r markets?|labor force|labour force)\b", label)
            or re.match(r"^(?:[A-Z]{3}_)?L(?:E|ER|EW|UR|UE|FE|MI|LF|LFPR|PR)(?:_|[A-Z0-9])", code)
        ):
            return "IMF.STA:LS"

        if (
            label.startswith("national accounts")
            or code.startswith(("NGDPVA_", "NGDP_", "NPGDP"))
        ):
            return "IMF.STA:NA_MAIN"

        entry = self._indicator_catalog_entry(code) or {}
        keywords = str(entry.get("keywords") or "").lower()
        if "balance of payments" in keywords:
            return "IMF.STA:BOP"
        if re.search(r"\b(?:labou?r markets?|labor force|labour force)\b", keywords) or "employment rate" in keywords:
            return "IMF.STA:LS"
        if "national accounts" in keywords or "gross value added" in keywords:
            return "IMF.STA:NA_MAIN"

        return None

    def _is_disabled_bop_exact_code_shape(self, indicator_code: str) -> bool:
        """Return whether an exact code targets the disabled no-rule BOP bridge."""
        code = self._strip_country_prefix(indicator_code)
        if code.startswith("BOP_"):
            return True
        return bool(re.search(r"(?:^|_)BP6(?:_|$)", code))

    @staticmethod
    def _local_xml_name(tag: str) -> str:
        """Return the namespace-stripped XML tag name."""
        return str(tag or "").rsplit("}", 1)[-1]

    @staticmethod
    def _period_to_date(period: str) -> str:
        """Normalize SDMX period strings to the API's date shape.

        Delegates to the shared SDMX period parser (Phase 3.2); this method
        is the correctness reference that parser reproduces.
        """
        from ._sdmx import period_to_iso_date as _shared
        return _shared(period)

    @staticmethod
    def _frequency_label(code: str) -> str:
        """Delegate to the shared SDMX frequency-label map (Phase 3.2)."""
        from ._sdmx import frequency_label as _shared
        return _shared(code)

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _country_prefix_from_indicator_code(self, indicator_code: str) -> Optional[str]:
        """Return a leading ISO3 country prefix when the IMF catalog code encodes one."""
        code = str(indicator_code or "").strip().upper()
        match = re.match(r"^([A-Z]{3})_", code)
        if not match:
            return None
        candidate = match.group(1)
        try:
            from ..routing.country_resolver import CountryResolver

            if CountryResolver.to_iso2(candidate):
                return candidate
        except Exception:
            if candidate in self.CODE_TO_COUNTRY_NAME:
                return candidate
        return None

    def _strip_country_prefix(self, indicator_code: str) -> str:
        code = str(indicator_code or "").strip().upper()
        prefix = self._country_prefix_from_indicator_code(code)
        if prefix and code.startswith(f"{prefix}_"):
            return code[len(prefix) + 1 :]
        return code

    def _sdmx_country_codes(self, indicator_code: str, countries: List[str]) -> List[str]:
        prefix = self._country_prefix_from_indicator_code(indicator_code)
        if prefix:
            return [prefix]

        country_codes: List[str] = []
        for country in countries or ["USA"]:
            code = self._country_code(country)
            if code and code not in country_codes:
                country_codes.append(code)
        return country_codes or ["USA"]

    @staticmethod
    def _is_aggregate_trade_code(bare_code: str) -> bool:
        code = str(bare_code or "").strip().upper()
        if any(fragment in code for fragment in ("_H5_", "_HS", "_SITC", "_CPC", "_BEC")):
            return False
        return bool(
            re.fullmatch(r"(?:T?[XM]G?|[XM]G)_(?:FOB|CIF)_(?:USD|XDC)", code)
            or re.fullmatch(r"(?:T?[XM]G?|[XM]G)_(?:FOB|CIF)_(?:USD|XDC)_IX", code)
        )

    @staticmethod
    def _trade_indicator_from_code(bare_code: str) -> Optional[str]:
        code = str(bare_code or "").strip().upper()
        if re.match(r"^(?:T?XG?|XG)_", code):
            return "XG"
        if re.match(r"^(?:T?MG?|MG)_", code):
            return "MG"
        return None

    @staticmethod
    def _trade_transformation_from_code(bare_code: str) -> Optional[str]:
        code = str(bare_code or "").strip().upper()
        basis = "FOB" if re.search(r"(?:^|_)FOB(?:_|$)", code) else "CIF" if re.search(r"(?:^|_)CIF(?:_|$)", code) else None
        currency = "XDC" if re.search(r"(?:^|_)XDC(?:_|$)", code) else "USD" if re.search(r"(?:^|_)USD(?:_|$)", code) else None
        if basis and currency:
            suffix = "_IX" if code.endswith("_IX") else ""
            return f"{basis}_{currency}{suffix}"
        return None

    @staticmethod
    def _coicop_from_cpi_code(bare_code: str) -> str:
        code = str(bare_code or "").upper()
        if code == "PCPI_IX":
            return "_T"
        match = re.search(r"(?:^|_)CP_?(\d{2})(?:_|$)", code)
        if match:
            return f"CP{match.group(1)}"
        return "_T"

    @staticmethod
    def _is_cpi_candidate(bare_code: str, label: str) -> bool:
        code = str(bare_code or "").upper()
        text = str(label or "").lower()
        if "weight" in text:
            return False
        if code == "PCPI_IX":
            return True
        if re.fullmatch(r"PCPI_CP_?\d{2}(?:_BY\d{4}|_BY\d{4}M\d{2})?_IX", code):
            return True
        return False

    @staticmethod
    def _is_ppi_candidate(bare_code: str, label: str) -> bool:
        code = str(bare_code or "").upper()
        text = str(label or "").lower()
        if any(fragment in code for fragment in ("ISIC", "NACE")):
            return False
        if any(term in text for term in ["by activity", "manufacture of", "mining of", "commodities by activity"]):
            return False
        return code in {"PPPI_IX", "PPI_IX", "WPI_IX", "PPPIA_IX"}

    @staticmethod
    def _ppi_indicator_from_code(bare_code: str) -> Optional[str]:
        code = str(bare_code or "").strip().upper()
        if code.startswith("WPI"):
            return "WPI"
        if code in {"PPPI_IX", "PPI_IX", "PPPIA_IX"}:
            return "PPI"
        return None

    def _build_sdmx_series_candidates(
        self,
        *,
        indicator_code: str,
        indicator_label: Optional[str],
        countries: List[str],
    ) -> List[Dict[str, str]]:
        """Build exact-code SDMX 2.1 candidates for public IMF.STA families.

        The legacy DataMapper API does not serve many catalog-native IMF
        ``INDICATOR`` rows.  This mapper is intentionally narrow: it only
        emits candidates when the provider code can be mapped mechanically to
        a documented public IMF.STA SDMX 2.1 flow/key.
        """
        code = str(indicator_code or "").strip().upper()
        label = str(indicator_label or "").strip()
        bare_code = self._strip_country_prefix(code)
        countries_to_try = self._sdmx_country_codes(code, countries)
        candidates: List[Dict[str, str]] = []

        if self._is_aggregate_trade_code(bare_code):
            indicator = self._trade_indicator_from_code(bare_code)
            transformation = self._trade_transformation_from_code(bare_code)
            if indicator and transformation:
                for country in countries_to_try:
                    candidates.append(
                        {
                            "flow": "ITG",
                            "key": f"{country}.{indicator}.{transformation}.A",
                            "country": country,
                            "frequency": "A",
                            "unit": transformation,
                            "data_type": "Level",
                        }
                    )
            return candidates

        if self._is_cpi_candidate(bare_code, label):
            coicop = self._coicop_from_cpi_code(bare_code)
            transformation = "IX"
            frequencies = ["A"] if coicop == "_T" else ["A", "M", "Q"]
            for country in countries_to_try:
                for frequency in frequencies:
                    candidates.append(
                        {
                            "flow": "CPI",
                            "key": f"{country}.CPI.{coicop}.{transformation}.{frequency}",
                            "country": country,
                            "frequency": frequency,
                            "unit": "index",
                            "data_type": "Index",
                        }
                    )
            return candidates

        if self._is_ppi_candidate(bare_code, label):
            indicator = self._ppi_indicator_from_code(bare_code)
            if indicator:
                for country in countries_to_try:
                    candidates.append(
                        {
                            "flow": "PPI",
                            "key": f"{country}.{indicator}.IX.A",
                            "country": country,
                            "frequency": "A",
                            "unit": "index",
                            "data_type": "Index",
                        }
                    )
            return candidates

        return []

    def _sdmx_data_url(
        self,
        *,
        flow: str,
        key: str,
        start_year: Optional[int],
        end_year: Optional[int],
    ) -> str:
        url = f"{self.SDMX_DATA_BASE_URL},{flow}/{key}"
        params: Dict[str, str] = {}
        if start_year is not None:
            params["startPeriod"] = str(start_year)
        if end_year is not None:
            params["endPeriod"] = str(end_year)
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    @staticmethod
    def _sdmx_codelist_id(reference: Any) -> Optional[str]:
        value = str(reference or "").strip()
        if not value:
            return None
        match = re.search(r"Codelist=[^:]+:([A-Za-z0-9_]+)\(", value)
        if match:
            return match.group(1)
        return value if re.fullmatch(r"[A-Za-z0-9_]+", value) else None

    @staticmethod
    def _sdmx_codelist_aliases_by_dimension() -> Dict[str, tuple[str, ...]]:
        """Known IMF.STA codelist ids for dimensions that omit localRepresentation.

        IMF's public SDMX structures often include the relevant codelists in
        ``references=all`` responses but omit per-dimension enumeration refs.
        These aliases are mechanical metadata wiring only: they expose official
        codelist values for diagnostics/contract checks and do not select a
        semantic series.
        """
        return {
            "COUNTRY": ("CL_COUNTRY",),
            "BOP_ACCOUNTING_ENTRY": ("CL_BOP_ACCOUNTING_ENTRY",),
            "INDICATOR": (
                "CL_BOP_INDICATOR",
                "CL_ITG_INDICATOR",
                "CL_PPI_INDICATOR",
                "CL_LS_INDICATOR",
                "CL_INDICATOR",
            ),
            "TYPE_OF_TRANSFORMATION": (
                "CL_ITG_TYPE_OF_TRANSFORMATION",
                "CL_CPI_TYPE_OF_TRANSFORMATION",
                "CL_PPI_TYPE_OF_TRANSFORMATION",
                "CL_LS_TYPE_OF_TRANSFORMAtION",
                "CL_TYPE_OF_TRANSFORMATION",
                "CL_TRANSFORMATION",
            ),
            "UNIT": ("CL_UNIT",),
            "FREQ": ("CL_FREQ", "CL_FREQUENCY"),
            "FREQUENCY": ("CL_FREQ", "CL_FREQUENCY"),
            "INDEX_TYPE": ("CL_INDEX_TYPE",),
            "COICOP_1999": ("CL_COICOP_1999",),
        }

    @classmethod
    def _first_sdmx_name(cls, element: ET.Element) -> Optional[str]:
        fallback: Optional[str] = None
        for child in element:
            if cls._local_xml_name(child.tag) != "Name":
                continue
            text = str(child.text or "").strip()
            if not text:
                continue
            if child.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") == "en":
                return text
            if fallback is None:
                fallback = text
        return fallback

    @classmethod
    def _parse_imf_dataflow_structure(
        cls,
        payload: Any,
        *,
        source_url: str = "",
    ) -> Dict[str, Any]:
        """Parse IMF Data Portal SDMX structure metadata.

        Metadata is diagnostic only; supportability remains fail-closed until
        exact family routing and observation retrieval are implemented.
        """
        if isinstance(payload, str):
            try:
                root = ET.fromstring(str(payload or "").strip())
            except ET.ParseError as exc:
                raise DataNotAvailableError(f"IMF SDMX structure response was not parseable XML: {exc}") from exc

            dataflow_info: Dict[str, Any] = {}
            for element in root.iter():
                if cls._local_xml_name(element.tag) != "Dataflow":
                    continue
                structure_ref: Dict[str, str] = {}
                for child in element.iter():
                    if cls._local_xml_name(child.tag) == "Ref":
                        structure_ref = {str(key): str(value) for key, value in child.attrib.items()}
                        break
                dataflow_info = {
                    "agency": element.attrib.get("agencyID"),
                    "dataflow": element.attrib.get("id"),
                    "version": element.attrib.get("version"),
                    "name": cls._first_sdmx_name(element),
                    "data_structure_ref": structure_ref,
                }
                break

            data_structure_info: Dict[str, Any] = {}
            dimensions: List[Dict[str, Any]] = []
            time_dimensions: List[Dict[str, Any]] = []
            for element in root.iter():
                if cls._local_xml_name(element.tag) != "DataStructure":
                    continue
                data_structure_info = {
                    "agency": element.attrib.get("agencyID"),
                    "id": element.attrib.get("id"),
                    "version": element.attrib.get("version"),
                    "name": cls._first_sdmx_name(element),
                }
                for child in element.iter():
                    local = cls._local_xml_name(child.tag)
                    if local not in {"Dimension", "TimeDimension"}:
                        continue
                    dim_id = str(child.attrib.get("id") or "").strip()
                    if not dim_id:
                        continue
                    default_position = len(dimensions) + len(time_dimensions)
                    try:
                        position = int(child.attrib.get("position", default_position))
                    except (TypeError, ValueError):
                        position = default_position
                    concept_ref: Dict[str, str] = {}
                    for candidate in child.iter():
                        if cls._local_xml_name(candidate.tag) == "Ref":
                            concept_ref = {str(key): str(value) for key, value in candidate.attrib.items()}
                            break
                    parsed_dimension = {
                        "id": dim_id,
                        "position": position,
                        "type": local,
                        "concept_ref": concept_ref,
                    }
                    if local == "TimeDimension":
                        time_dimensions.append(parsed_dimension)
                    else:
                        dimensions.append(parsed_dimension)
                break
            dimensions.sort(key=lambda item: item.get("position", 0))
            time_dimensions.sort(key=lambda item: item.get("position", 0))

            codelists: List[Dict[str, Any]] = []
            codelist_values: Dict[str, set[str]] = {}
            codelist_entries: Dict[str, List[Dict[str, str]]] = {}
            for element in root.iter():
                if cls._local_xml_name(element.tag) != "Codelist":
                    continue
                sample_codes: List[Dict[str, str]] = []
                entries: List[Dict[str, str]] = []
                values: set[str] = set()
                code_count = 0
                for code in element:
                    if cls._local_xml_name(code.tag) != "Code":
                        continue
                    code_id = str(code.attrib.get("id") or "")
                    code_name = cls._first_sdmx_name(code) or ""
                    code_count += 1
                    if code_id:
                        values.add(code_id.upper())
                        entries.append({"id": code_id, "name": code_name})
                    if len(sample_codes) < 20:
                        sample_codes.append(
                            {
                                "id": code_id,
                                "name": code_name,
                            }
                        )
                codelist_id = str(element.attrib.get("id") or "")
                if codelist_id and values:
                    codelist_values[codelist_id] = values
                    codelist_entries[codelist_id] = entries
                codelists.append(
                    {
                        "agency": element.attrib.get("agencyID"),
                        "id": codelist_id,
                        "version": element.attrib.get("version"),
                        "name": cls._first_sdmx_name(element),
                        "code_count": code_count,
                        "sample_codes": sample_codes,
                    }
                )

            codelist_aliases_by_dimension = cls._sdmx_codelist_aliases_by_dimension()
            allowed_values_by_dimension: Dict[str, set[str]] = {}
            codelist_entries_by_dimension: Dict[str, List[Dict[str, str]]] = {}
            for dimension in dimensions:
                if dimension.get("is_time"):
                    continue
                dim_id = str(dimension.get("id") or "")
                concept_id = str((dimension.get("concept_ref") or {}).get("id") or "")
                candidate_codelists = (
                    codelist_aliases_by_dimension.get(dim_id)
                    or codelist_aliases_by_dimension.get(concept_id)
                    or (f"CL_{dim_id}",)
                )
                for codelist_id in candidate_codelists:
                    if codelist_id in codelist_values:
                        allowed_values_by_dimension[dim_id] = codelist_values[codelist_id]
                        codelist_entries_by_dimension[dim_id] = codelist_entries.get(codelist_id, [])
                        break

            return {
                "source_url": source_url,
                "agency": dataflow_info.get("agency") or data_structure_info.get("agency"),
                "dataflow": dataflow_info.get("dataflow"),
                "version": dataflow_info.get("version") or data_structure_info.get("version"),
                "name": dataflow_info.get("name"),
                "dsd_id": data_structure_info.get("id"),
                "dsd_agency": data_structure_info.get("agency"),
                "dsd_version": data_structure_info.get("version"),
                "dimensions": dimensions,
                "dimension_ids": [dimension.get("id") for dimension in dimensions],
                "time_dimensions": time_dimensions,
                "time_dimension_ids": [dimension.get("id") for dimension in time_dimensions],
                "codelists": codelists,
                "codelist_sizes": {
                    str(codelist.get("id") or ""): int(codelist.get("code_count") or 0)
                    for codelist in codelists
                    if str(codelist.get("id") or "")
                },
                "allowed_values_by_dimension": allowed_values_by_dimension,
                "codelist_entries_by_dimension": codelist_entries_by_dimension,
            }

        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        data = data if isinstance(data, dict) else {}
        dataflows = data.get("dataflows") if isinstance(data.get("dataflows"), list) else []
        dataflow_info = dataflows[0] if dataflows and isinstance(dataflows[0], dict) else {}
        structures = data.get("dataStructures") if isinstance(data.get("dataStructures"), list) else []
        dsd = structures[0] if structures and isinstance(structures[0], dict) else {}
        components = dsd.get("dataStructureComponents", {}) if isinstance(dsd, dict) else {}
        dimension_list = components.get("dimensionList", {}) if isinstance(components, dict) else {}

        codelist_sizes: Dict[str, int] = {}
        codelist_values: Dict[str, set[str]] = {}
        codelist_entries: Dict[str, List[Dict[str, str]]] = {}
        codelists = data.get("codelists") if isinstance(data.get("codelists"), list) else []
        for codelist in codelists:
            if not isinstance(codelist, dict):
                continue
            codelist_id = str(codelist.get("id") or "").strip()
            codes = codelist.get("codes") or codelist.get("items")
            if codelist_id and isinstance(codes, list):
                codelist_sizes[codelist_id] = len(codes)
                codelist_values[codelist_id] = {
                    str(code.get("id") or "").strip().upper()
                    for code in codes
                    if isinstance(code, dict) and str(code.get("id") or "").strip()
                }
                codelist_entries[codelist_id] = [
                    {
                        "id": str(code.get("id") or "").strip(),
                        "name": str(code.get("name") or code.get("label") or "").strip(),
                    }
                    for code in codes
                    if isinstance(code, dict) and str(code.get("id") or "").strip()
                ]

        def parse_dimensions(values: Any) -> List[Dict[str, Any]]:
            dimensions: List[Dict[str, Any]] = []
            for index, dim in enumerate(values if isinstance(values, list) else []):
                if not isinstance(dim, dict):
                    continue
                position = dim.get("position", index)
                if not isinstance(position, int):
                    try:
                        position = int(position)
                    except (TypeError, ValueError):
                        position = index
                representation = dim.get("localRepresentation", {})
                representation = representation if isinstance(representation, dict) else {}
                codelist = cls._sdmx_codelist_id(representation.get("enumeration"))
                parsed: Dict[str, Any] = {
                    "id": dim.get("id"),
                    "position": position,
                    "type": dim.get("type"),
                    "name": dim.get("name", dim.get("id")),
                    "codelist": codelist,
                }
                if codelist and codelist in codelist_sizes:
                    parsed["value_count"] = codelist_sizes[codelist]
                dimensions.append(parsed)
            dimensions.sort(key=lambda item: item.get("position", 0))
            return dimensions

        dimensions = parse_dimensions(dimension_list.get("dimensions") if isinstance(dimension_list, dict) else [])
        time_dimensions = parse_dimensions(dimension_list.get("timeDimensions") if isinstance(dimension_list, dict) else [])
        codelist_aliases_by_dimension = cls._sdmx_codelist_aliases_by_dimension()
        allowed_values_by_dimension: Dict[str, set[str]] = {}
        codelist_entries_by_dimension: Dict[str, List[Dict[str, str]]] = {}
        for dim in dimensions:
            dim_id = str(dim.get("id") or "")
            codelist = str(dim.get("codelist") or "")
            candidate_codelists = (
                (codelist,) if codelist else ()
            ) + tuple(codelist_aliases_by_dimension.get(dim_id) or (f"CL_{dim_id}",))
            for codelist_id in candidate_codelists:
                if codelist_id in codelist_values:
                    allowed_values_by_dimension[dim_id] = codelist_values[codelist_id]
                    codelist_entries_by_dimension[dim_id] = codelist_entries.get(codelist_id, [])
                    break

        return {
            "agency": dataflow_info.get("agencyID"),
            "dataflow": dataflow_info.get("id"),
            "version": dataflow_info.get("version"),
            "name": dataflow_info.get("name"),
            "dsd_id": dsd.get("id") if isinstance(dsd, dict) else None,
            "dsd_agency": dsd.get("agencyID") if isinstance(dsd, dict) else None,
            "dsd_version": dsd.get("version") if isinstance(dsd, dict) else None,
            "dimensions": dimensions,
            "dimension_ids": [dim.get("id") for dim in dimensions],
            "time_dimensions": time_dimensions,
            "time_dimension_ids": [dim.get("id") for dim in time_dimensions],
            "codelist_sizes": codelist_sizes,
            "allowed_values_by_dimension": allowed_values_by_dimension,
            "codelist_entries_by_dimension": codelist_entries_by_dimension,
        }

    async def _get_imf_dataflow_structure(self, dataflow: str) -> Optional[Dict[str, Any]]:
        """Fetch official IMF.STA dataflow structure metadata, without unblocking supportability."""
        flow = str(dataflow or "").strip().upper()
        if not flow:
            return None
        cache_key = f"IMF.STA:{flow}:latest"
        cached = self._DATAFLOW_STRUCTURE_CACHE.get(cache_key)
        if cached:
            return cached
        url = f"{self.SDMX_STRUCTURE_BASE_URL}/{flow}/latest"
        client = get_http1_client()
        try:
            response = await self._get_with_retry(
                client,
                url,
                params={"references": "all"},
                headers={"Accept": "application/vnd.sdmx.structure+json;version=1.0.0"},
                timeout=effective_timeout(30.0),
            )
            response_text = str(getattr(response, "text", "") or "").strip()
            if response_text.startswith("<"):
                metadata = self._parse_imf_dataflow_structure(response_text)
            else:
                metadata = self._parse_imf_dataflow_structure(response.json())
                if not metadata.get("dimension_ids") and response_text:
                    metadata = self._parse_imf_dataflow_structure(response_text)
        except Exception as exc:
            logger.info("IMF SDMX dataflow structure lookup failed for %s: %s", flow, exc)
            return None
        metadata["structureUrl"] = str(getattr(getattr(response, "request", None), "url", url))
        self._DATAFLOW_STRUCTURE_CACHE[cache_key] = metadata
        return metadata

    def _parse_sdmx_structure_specific_xml(
        self,
        response_text: str,
    ) -> List[tuple[Dict[str, str], List[Dict[str, str]]]]:
        """Parse IMF SDMX 2.1 structure-specific XML into series/observation rows."""
        try:
            root = ET.fromstring(str(response_text or "").strip())
        except ET.ParseError as exc:
            raise DataNotAvailableError(f"IMF SDMX response was not parseable XML: {exc}") from exc

        parsed: List[tuple[Dict[str, str], List[Dict[str, str]]]] = []
        for element in root.iter():
            if self._local_xml_name(element.tag) != "Series":
                continue
            series_attrs = {str(key): str(value) for key, value in element.attrib.items()}
            observations: List[Dict[str, str]] = []
            for child in element:
                if self._local_xml_name(child.tag) != "Obs":
                    continue
                attrs = {str(key): str(value) for key, value in child.attrib.items()}
                if not attrs.get("TIME_PERIOD") or "OBS_VALUE" not in attrs:
                    continue
                observations.append(attrs)
            if observations:
                parsed.append((series_attrs, observations))
        return parsed

    def _parse_sdmx_csv(
        self,
        response_text: str,
    ) -> List[tuple[Dict[str, str], List[Dict[str, str]]]]:
        """Parse IMF SDMX CSV into the same series/observation shape as XML."""
        rows = [
            {str(key): str(value) for key, value in row.items() if key is not None and value is not None}
            for row in csv.DictReader(io.StringIO(str(response_text or "")))
        ]
        grouped: Dict[tuple[tuple[str, str], ...], List[Dict[str, str]]] = {}
        series_dimensions_by_key: Dict[tuple[tuple[str, str], ...], Dict[str, str]] = {}
        observation_columns = {
            "TIME_PERIOD",
            "OBS_VALUE",
            "OBS_STATUS",
            "OBS_CONF",
            "UNIT_MULT",
            "DECIMALS",
        }

        for row in rows:
            if not row.get("TIME_PERIOD") or "OBS_VALUE" not in row:
                continue
            dimensions = {
                key: value
                for key, value in row.items()
                if key not in observation_columns
                and not key.startswith("OBS_")
                and key
                and value != ""
            }
            key = tuple(sorted(dimensions.items()))
            series_dimensions_by_key.setdefault(key, dimensions)
            grouped.setdefault(key, []).append(row)

        return [
            (series_dimensions_by_key[key], observations)
            for key, observations in grouped.items()
            if observations
        ]

    async def _fetch_sdmx_exact_indicator_family(
        self,
        *,
        indicator_code: str,
        indicator_label: Optional[str],
        candidates: List[Dict[str, str]],
        start_year: Optional[int],
        end_year: Optional[int],
    ) -> List[NormalizedData]:
        """Fetch a non-DataMapper IMF exact code through public IMF.STA SDMX 2.1."""
        client = get_http1_client()
        attempted: List[str] = []
        last_error: Optional[Exception] = None
        indicator_name = indicator_label or indicator_code

        for candidate in candidates:
            flow = candidate["flow"]
            key = candidate["key"]
            url = self._sdmx_data_url(
                flow=flow,
                key=key,
                start_year=start_year,
                end_year=end_year,
            )
            attempted.append(f"{flow}/{key}")
            try:
                # raise_on_status=False: this candidate ladder inspects the
                # status code to decide whether to fall through to the next
                # flow/key candidate, so the helper must hand back the response
                # rather than raise. It still applies retry/rate-limit/breaker.
                response = await self._get_with_retry(
                    client,
                    url,
                    headers={"Accept": "text/csv, application/vnd.sdmx.data+csv;version=2.0.0"},
                    timeout=effective_timeout(30.0),
                    raise_on_status=False,
                )
                if response.status_code >= 500:
                    last_error = DataNotAvailableError(f"HTTP {response.status_code}")
                    continue
                if response.status_code >= 400:
                    last_error = DataNotAvailableError(f"HTTP {response.status_code}")
                    continue
                response_text = getattr(response, "text", "")
                content_type = str(getattr(response, "headers", {}).get("content-type", "")).lower()
                if "csv" in content_type or str(response_text).lstrip().startswith("DATAFLOW,"):
                    series_payloads = self._parse_sdmx_csv(response_text)
                else:
                    series_payloads = self._parse_sdmx_structure_specific_xml(response_text)
            except Exception as exc:
                last_error = exc
                continue

            results: List[NormalizedData] = []
            for series_attrs, observations in series_payloads:
                data_points = [
                    {
                        "date": self._period_to_date(obs.get("TIME_PERIOD", "")),
                        "value": self._float_or_none(obs.get("OBS_VALUE")),
                    }
                    for obs in observations
                ]
                data_points = [
                    point for point in data_points
                    if point["date"] and point["value"] is not None
                ]
                data_points.sort(key=lambda point: point["date"])
                if not data_points:
                    continue

                country_code = (
                    series_attrs.get("COUNTRY")
                    or candidate.get("country")
                    or key.split(".", 1)[0]
                )
                frequency_code = (
                    series_attrs.get("FREQUENCY")
                    or series_attrs.get("FREQ")
                    or candidate.get("frequency")
                    or "A"
                )
                metadata = Metadata(
                    source="IMF",
                    indicator=indicator_name,
                    country=self._country_name(country_code),
                    frequency=self._frequency_label(frequency_code),
                    unit=candidate.get("unit", ""),
                    lastUpdated="",
                    seriesId=indicator_code,
                    apiUrl=url,
                    sourceUrl=f"https://data.imf.org/en/datasets/IMF.STA:{flow}",
                    seasonalAdjustment=None,
                    dataType=candidate.get("data_type", "Level"),
                    priceType=None,
                    description=f"{indicator_name} (IMF.STA {flow} key {key})",
                    notes=[
                        "Fetched from the official IMF SDMX 2.1 public API because this catalog code is not served by legacy DataMapper v1."
                    ],
                    startDate=data_points[0]["date"],
                    endDate=data_points[-1]["date"],
                )
                results.append(NormalizedData(metadata=metadata, data=data_points))

            if results:
                return results

        diagnostic = f" Attempted SDMX keys: {', '.join(attempted)}." if attempted else ""
        error_suffix = f" Last error: {last_error}." if last_error else ""
        raise DataNotAvailableError(
            f"IMF SDMX 2.1 returned no observations for {indicator_code}.{diagnostic}{error_suffix}"
        )

    def _split_bop_series_code(self, indicator_code: str) -> Dict[str, str]:
        """Split a BOP-style IMF code into dimension components."""
        code = str(indicator_code or "").strip().upper()
        if len(code) < 3:
            return {}

        accounting_entry = code[:2]
        remainder = code[2:]
        if "_" in remainder:
            indicator_part, unit = remainder.rsplit("_", 1)
        else:
            indicator_part, unit = remainder, ""

        return {
            "BOP_ACCOUNTING_ENTRY": accounting_entry,
            "INDICATOR": indicator_part,
            "UNIT": unit,
        }

    @staticmethod
    def _normalize_bop_match_text(value: Any) -> str:
        """Normalize BOP labels for conservative codelist-name matching."""
        text = str(value or "").lower()
        text = text.replace("non-profit", "nonprofit").replace("nonfinancial", "non financial")
        text = re.sub(r"[^a-z0-9]+", " ", text)
        stopwords = {
            "balance",
            "payments",
            "bop",
            "bpm6",
            "definition",
            "current",
            "account",
            "financial",
            "capital",
            "supplementary",
            "items",
            "national",
            "currency",
            "dollars",
            "dollar",
            "euros",
            "debit",
            "credit",
            "net",
            "from",
            "and",
            "the",
            "of",
            "in",
            "on",
            "for",
            "with",
            "other",
        }
        return " ".join(token for token in text.split() if token and token not in stopwords)

    @classmethod
    def _bop_match_tokens(cls, value: Any) -> set[str]:
        return {
            token
            for token in cls._normalize_bop_match_text(value).split()
            if len(token) >= 3
        }

    def _bop_country_codes(
        self,
        indicator_code: str,
        countries: List[str],
        structure: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        structure_countries = set()
        if structure:
            structure_countries = {
                str(value or "").strip().upper()
                for value in (structure.get("allowed_values_by_dimension") or {}).get("COUNTRY") or []
                if str(value or "").strip()
            }
        raw_prefix_match = re.match(r"^([A-Z]{3})_", str(indicator_code or "").strip().upper())
        if raw_prefix_match and raw_prefix_match.group(1) in structure_countries:
            return [raw_prefix_match.group(1)]

        prefix = self._country_prefix_from_indicator_code(indicator_code)
        if prefix:
            return [prefix]
        return [self._country_code(country) for country in countries or ["USA"]]

    def _bop_accounting_entries(self, indicator_code: str, label: str) -> List[str]:
        bare_code = self._strip_country_prefix(indicator_code)
        text = f"{indicator_code} {label}".lower()
        entries: List[str] = []
        if bare_code.startswith("BX") or re.search(r"\bcredit\b|\breceipts?\b", text):
            entries.append("CD_T")
        if bare_code.startswith("BM") or re.search(r"\bdebit\b|\bpayments?\b", text):
            entries.append("DB_T")
        if bare_code.startswith("BN") or "credits less debits" in text:
            entries.append("NETCD_T")
        if "net acquisition of financial assets" in text:
            entries.append("A_NFA_T")
        if "net incurrence of liabilities" in text:
            entries.append("L_NIL_T")
        if "assets" in text and "net acquisition" not in text:
            entries.append("A_T")
        if "liabilities" in text and "net incurrence" not in text:
            entries.append("L_T")
        return list(dict.fromkeys(entries or ["CD_T"]))

    @staticmethod
    def _bop_units(indicator_code: str, label: str) -> List[str]:
        text = f"{indicator_code} {label}".upper()
        units: List[str] = []
        if "USD" in text or "US DOLLAR" in text:
            units.append("USD")
        if "EUR" in text or "EURO" in text:
            units.append("EUR")
        if "XDR" in text or "SDR" in text:
            units.append("XDR")
        if "XDC" in text or "NATIONAL CURRENCY" in text:
            units.append("XDC")
        return list(dict.fromkeys(units or ["USD", "XDC"]))

    def _bop_indicator_candidates(
        self,
        structure: Optional[Dict[str, Any]],
        label: str,
        *,
        limit: int = 5,
    ) -> List[str]:
        """Choose BOP indicator codes by matching labels to official codelist names."""
        entries = []
        if structure:
            entries = list((structure.get("codelist_entries_by_dimension") or {}).get("INDICATOR") or [])
        if not entries:
            return []

        label_norm = self._normalize_bop_match_text(label)
        label_tokens = self._bop_match_tokens(label)
        scored: List[tuple[int, str]] = []
        for entry in entries:
            code = str(entry.get("id") or "").strip().upper()
            name = str(entry.get("name") or "").strip()
            if not code or not name:
                continue
            name_norm = self._normalize_bop_match_text(name)
            name_tokens = self._bop_match_tokens(name)
            if not name_tokens:
                continue
            overlap = len(label_tokens & name_tokens)
            if not overlap:
                continue
            score = overlap * 5
            if name_norm and name_norm in label_norm:
                score += 40
            if label_norm and label_norm in name_norm:
                score += 20
            score -= max(0, len(name_tokens - label_tokens) - 2)
            scored.append((score, code))

        scored.sort(reverse=True)
        return [code for score, code in scored if score >= 12][:limit]

    def _build_bop_sdmx_series_candidates(
        self,
        *,
        indicator_code: str,
        indicator_label: Optional[str],
        countries: List[str],
        structure: Optional[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        """Build exact IMF.STA BOP REST candidates from a legacy BOP label."""
        label = str(indicator_label or indicator_code or "")
        indicator_candidates = self._bop_indicator_candidates(structure, label)
        if not indicator_candidates:
            return []

        candidates: List[Dict[str, str]] = []
        for country in self._bop_country_codes(indicator_code, countries, structure):
            for accounting_entry in self._bop_accounting_entries(indicator_code, label):
                for bop_indicator in indicator_candidates:
                    for unit in self._bop_units(indicator_code, label):
                        candidates.append(
                            {
                                "flow": "BOP",
                                "key": f"{country}.{accounting_entry}.{bop_indicator}.{unit}.A",
                                "country": country,
                                "frequency": "A",
                                "unit": unit,
                                "data_type": "Level",
                            }
                        )
                        if len(candidates) >= 20:
                            return candidates
        return candidates

    def _validate_bop_structure_candidate(
        self,
        metadata: Optional[Dict[str, Any]],
        indicator_code: str,
        countries: List[str],
    ) -> Optional[str]:
        """Return a fail-closed diagnostic when a BOP candidate is structurally invalid."""
        if not metadata:
            return "BOP structure metadata unavailable"

        required_dimensions = {"COUNTRY", "BOP_ACCOUNTING_ENTRY", "INDICATOR", "UNIT", "FREQUENCY"}
        dimension_ids = {str(dim or "").strip().upper() for dim in metadata.get("dimension_ids") or []}
        missing_dimensions = sorted(required_dimensions - dimension_ids)
        if missing_dimensions:
            return f"missing BOP dimensions: {', '.join(missing_dimensions)}"

        split_code = self._split_bop_series_code(indicator_code)
        requested_by_dimension = {
            "COUNTRY": [self._country_code(country) for country in countries],
            "BOP_ACCOUNTING_ENTRY": [split_code.get("BOP_ACCOUNTING_ENTRY", "")],
            "INDICATOR": [split_code.get("INDICATOR", "")],
            "UNIT": [split_code.get("UNIT", "")],
            "FREQUENCY": ["A"],
        }
        allowed_by_dimension = dict(metadata.get("allowed_values_by_dimension") or {})
        for dimension, requested_values in requested_by_dimension.items():
            allowed_values = {
                str(value or "").strip().upper()
                for value in allowed_by_dimension.get(dimension) or []
                if str(value or "").strip()
            }
            if not allowed_values:
                continue
            missing_values = [
                str(value or "").strip().upper()
                for value in requested_values
                if str(value or "").strip() and str(value or "").strip().upper() not in allowed_values
            ]
            if missing_values:
                return f"{dimension} value(s) not present in BOP structure: {', '.join(missing_values)}"
        return None

    def _build_bop_query_payload(
        self,
        indicator_code: str,
        countries: List[str],
        start_year: Optional[int],
        end_year: Optional[int],
        *,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Build the first bounded BOP-family SDMX engine query payload."""
        country_codes = [self._country_code(country) for country in countries]
        split_code = self._split_bop_series_code(indicator_code)
        filters: List[Dict[str, Any]] = []
        if country_codes:
            filters.append({"dimensionId": "COUNTRY", "values": country_codes})
        for dim in ("BOP_ACCOUNTING_ENTRY", "INDICATOR", "UNIT"):
            value = split_code.get(dim)
            if value:
                filters.append({"dimensionId": dim, "values": [value]})
        filters.append({"dimensionId": "FREQUENCY", "values": ["A"]})
        if start_year or end_year:
            filters.append(
                {
                    "dimensionId": "TIME_PERIOD",
                    "values": [
                        str(start_year) if start_year is not None else "",
                        str(end_year) if end_year is not None else "",
                    ],
                }
            )

        return {
            "agencyID": "IMF.STA",
            "resourceID": "BOP",
            "version": "21.0.0",
            "filters": filters,
            "detail": "full",
            "includeHistory": "false",
            "messageVersion": "2.0.0",
            "limit": limit,
            "attributes": "none",
            "_type": "SdmxDataQueryV3",
            "dimensionAtObservation": "AllDimensions",
            "firstNObservations": 0,
        }

    async def _submit_engine_query(self, payload: Dict[str, Any]) -> str:
        """Submit a SDMX engine query and return the OTT token."""
        client = get_http_client()
        response = await self._post_with_retry(
            client,
            f"{self.engine_base_url}/platform/rest/v2/engine/data/sync/submit",
            json=payload,
            timeout=effective_timeout(60.0),
        )
        token = str(getattr(response, "text", "") or "").strip()
        if not token:
            raise DataNotAvailableError("IMF engine query returned no OTT token")
        return token

    async def _retrieve_engine_ott(self, ott_token: str) -> httpx.Response:
        """Retrieve the result of a previously submitted SDMX engine query."""
        client = get_http_client()
        # raise_on_status=False: the caller parses embedded engine errors out of
        # the raw response body (including non-2xx), so the helper must return
        # the response untouched (still with retry/rate-limit/breaker applied).
        response = await self._get_with_retry(
            client,
            f"{self.engine_base_url}/api/platform/v2/engine/data/sync/ott/{ott_token}",
            timeout=effective_timeout(60.0),
            raise_on_status=False,
        )
        return response

    def _extract_embedded_engine_error(self, response_text: str) -> Optional[Dict[str, Any]]:
        """Extract a trailing embedded error object from an OTT response body."""
        text = str(response_text or "").strip()
        marker = '{"status":'
        marker_idx = text.find(marker)
        if marker_idx <= 0:
            return None
        try:
            candidate = json.loads(text[marker_idx:])
        except Exception:
            return None
        if isinstance(candidate, dict) and "status" in candidate and "message" in candidate:
            return candidate
        return None

    def _decode_engine_ott_parts(self, response_text: str) -> List[Any]:
        """Decode concatenated JSON parts from an OTT response body."""
        text = str(response_text or "").strip()
        if not text:
            return []

        decoder = json.JSONDecoder()
        parts: List[Any] = []
        idx = 0
        while idx < len(text):
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx >= len(text):
                break
            obj, end = decoder.raw_decode(text, idx)
            parts.append(obj)
            idx = end
        return parts

    def _classify_bop_ott_response(self, response_text: str) -> Dict[str, Any]:
        """Classify the current OTT response body into a structured diagnostic."""
        parts = self._decode_engine_ott_parts(response_text)
        structure_part = None
        embedded_error = None

        for part in parts:
            if (
                isinstance(part, dict)
                and isinstance(part.get("data"), dict)
                and isinstance(part["data"].get("structures"), list)
            ):
                structure_part = part
            elif isinstance(part, dict) and "status" in part and "message" in part:
                embedded_error = part

        structure_summary = None
        if structure_part:
            structures = structure_part.get("data", {}).get("structures", [])
            first_structure = structures[0] if structures else {}
            series_dimensions = first_structure.get("dimensions", {}).get("series", [])
            structure_summary = {
                "series_dimensions": [d.get("id") for d in series_dimensions],
                "dimension_value_sizes": {
                    str(d.get("id") or ""): len(d.get("values", []))
                    for d in series_dimensions
                },
            }

        if embedded_error:
            return {
                "kind": "embedded_error",
                "parts": len(parts),
                "error": embedded_error,
                "structure_summary": structure_summary,
            }
        if structure_part:
            return {
                "kind": "structure_only",
                "parts": len(parts),
                "structure_summary": structure_summary,
            }
        return {
            "kind": "unclassified",
            "parts": len(parts),
            "structure_summary": structure_summary,
        }

    def _payload_fingerprint(self, payload: Dict[str, Any]) -> str:
        """Build a short stable fingerprint for an engine payload."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    def _payload_observability_suffix(self, payload: Dict[str, Any]) -> str:
        """Return a compact observability suffix for BOP engine errors."""
        fingerprint = self._payload_fingerprint(payload)
        filter_ids = [
            str(item.get("dimensionId") or "").strip()
            for item in payload.get("filters", [])
            if isinstance(item, dict) and str(item.get("dimensionId") or "").strip()
        ]
        return (
            f" payload_fingerprint={fingerprint}; "
            f"filter_dimensions={','.join(filter_ids) or 'none'}"
        )

    async def _fetch_bop_family(
        self,
        *,
        indicator_code: str,
        indicator_label: Optional[str],
        countries: List[str],
        start_year: Optional[int],
        end_year: Optional[int],
    ) -> List[NormalizedData]:
        """Prototype BOP-first non-WEO execution lane.

        This is intentionally bounded: it proves routing and engine reachability
        first, while remaining fail-closed until end-to-end payload semantics
        and result normalization are proven stable.
        """
        structure = await self._get_imf_dataflow_structure("BOP")
        sdmx_candidates = self._build_bop_sdmx_series_candidates(
            indicator_code=indicator_code,
            indicator_label=indicator_label,
            countries=countries,
            structure=structure,
        )
        if sdmx_candidates:
            try:
                return await self._fetch_sdmx_exact_indicator_family(
                    indicator_code=indicator_code,
                    indicator_label=indicator_label,
                    candidates=sdmx_candidates,
                    start_year=start_year,
                    end_year=end_year,
                )
            except DataNotAvailableError as exc:
                raise DataNotAvailableError(
                    f"IMF BOP public SDMX returned no observations for exact candidates derived from {indicator_code}: {exc}. "
                    "The non-WEO BOP family remains fail-closed until exact public SDMX observations are available."
                ) from exc

        structure_error = self._validate_bop_structure_candidate(
            structure,
            indicator_code=indicator_code,
            countries=countries,
        )
        if structure_error:
            raise DataNotAvailableError(
                f"IMF BOP execution lane cannot structurally validate {indicator_code}: {structure_error}. "
                "The non-WEO BOP family remains fail-closed until exact public SDMX dimensions are proven."
            )

        payload = self._build_bop_query_payload(
            indicator_code=indicator_code,
            countries=countries,
            start_year=start_year,
            end_year=end_year,
        )
        try:
            ott_token = await self._submit_engine_query(payload)
        except Exception as exc:
            raise DataNotAvailableError(
                f"IMF BOP execution lane could not submit an SDMX engine query for {indicator_code}: {exc}."
                f"{self._payload_observability_suffix(payload)}"
            ) from exc

        response = await self._retrieve_engine_ott(ott_token)
        if response.status_code >= 500:
            raise DataNotAvailableError(
                f"IMF BOP execution lane reached the SDMX engine submit step for {indicator_code}, "
                f"but OTT retrieval is currently unavailable (HTTP {response.status_code})."
                f"{self._payload_observability_suffix(payload)}"
            )
        if response.status_code >= 400:
            raise DataNotAvailableError(
                f"IMF BOP execution lane returned HTTP {response.status_code} during OTT retrieval for {indicator_code}."
                f"{self._payload_observability_suffix(payload)}"
            )

        ott_classification = self._classify_bop_ott_response(getattr(response, "text", ""))
        if ott_classification.get("kind") == "embedded_error":
            embedded_error = ott_classification.get("error") or {}
            structure_summary = ott_classification.get("structure_summary") or {}
            structure_suffix = ""
            if structure_summary:
                structure_suffix = (
                    f" ott_parts={ott_classification.get('parts')}; "
                    f"series_dimensions={','.join(structure_summary.get('series_dimensions') or []) or 'none'}"
                )
            raise DataNotAvailableError(
                f"IMF BOP execution lane reached OTT retrieval for {indicator_code}, but the engine returned "
                f"an embedded error {embedded_error.get('status')}: {embedded_error.get('message')}."
                f"{structure_suffix}"
                f"{self._payload_observability_suffix(payload)}"
            )

        raise DataNotAvailableError(
            f"IMF BOP execution lane obtained an OTT result for {indicator_code}, but result parsing is not implemented yet."
            f"{self._payload_observability_suffix(payload)}"
        )

    def _raise_for_unsupported_execution_family(
        self,
        indicator_code: str,
        indicator_label: Optional[str],
    ) -> None:
        """Fail closed when a resolved IMF code is outside the current fetch surface."""
        family = self._classify_execution_family(indicator_code)
        if family != "NON_DATAMAPPER_INDICATOR":
            return

        label = str(indicator_label or indicator_code).strip() or indicator_code
        raise DataNotAvailableError(
            f"IMF indicator '{label}' ({indicator_code}) resolved to a non-DataMapper IMF family. "
            f"The current runtime can resolve this series from the local IMF catalog, but execution still "
            f"requires IMF dataset-family routing beyond the legacy DataMapper v1 path."
        )

    async def _resolve_indicator_code(self, indicator: str) -> tuple[str, Optional[str]]:
        """Resolve IMF indicator code through mechanical codes or metadata search."""
        # Step 1: If the caller already supplied an exact IMF code that exists
        # in the local indicator catalog, trust it directly. This preserves
        # explicit provider-code queries without re-running metadata discovery,
        # while still failing closed for fake codes because they will miss the
        # local exact lookup and continue down the normal validation path.
        exact_code_raw = str(indicator or "").strip()
        exact_code_normalized = exact_code_raw.upper()
        exact_code_like = self._looks_like_imf_code(exact_code_normalized) or bool(
            re.fullmatch(r"[A-Z0-9][A-Z0-9_\.]{1,}", exact_code_normalized)
            and (
                "_" in exact_code_normalized
                or "." in exact_code_normalized
                or any(ch.isdigit() for ch in exact_code_normalized)
            )
            or re.fullmatch(r"[A-Z]{2}", exact_code_normalized)
        )
        exact_code_has_namespace = (
            "_" in exact_code_normalized
            or "." in exact_code_normalized
            or any(ch.isdigit() for ch in exact_code_normalized)
        )
        exact_code_candidate = exact_code_normalized
        exact_meta = None
        if exact_code_raw:
            try:
                from ..services.indicator_database import get_indicator_lookup

                lookup = get_indicator_lookup()
                for lookup_key in self._indicator_catalog_lookup_keys(exact_code_raw):
                    exact_meta = lookup.get("IMF", lookup_key)
                    if exact_meta:
                        exact_code_candidate = lookup_key
                        break
            except Exception as exc:
                logger.debug("IMF exact-code lookup skipped for '%s': %s", indicator, exc)

        exact_category = str(exact_meta.get("category") or "").strip().upper() if exact_meta else ""
        short_plain_uppercase_code = bool(re.fullmatch(r"[A-Z]{2,10}", exact_code_normalized))
        exact_name_upper = str(exact_meta.get("name") or "").upper() if exact_meta else ""
        plain_code_shadows_title_token = bool(
            short_plain_uppercase_code
            and re.search(rf"(?<![A-Z0-9]){re.escape(exact_code_normalized)}(?![A-Z0-9])", exact_name_upper)
        )
        if exact_meta and exact_category != "DATAFLOW" and (
            exact_code_has_namespace
            or exact_category == "WEO"
            or exact_category.endswith("REO")
            or (
                self._is_executable_datamapper_catalog_category(exact_category)
                and not plain_code_shadows_title_token
            )
        ):
            label_hint = str(exact_meta.get("name") or indicator)
            logger.info("IMF: Using exact local indicator code '%s' from catalog lookup", exact_code_candidate)
            return exact_code_candidate, self._friendly_indicator_label(label_hint, exact_code_candidate)

        # Step 2: Resolve natural-language indicators through provider metadata search only.
        if self.metadata_search:
            if not hasattr(self.metadata_search, "search_with_sdmx_fallback"):
                raise DataNotAvailableError(
                    f"IMF indicator '{indicator}' not found. Try refining your query or consult IMF DataMapper for available indicators."
                )

            # Use hierarchical search: SDMX first, then IMF DataMapper API.
            search_results = await self.metadata_search.search_with_sdmx_fallback(
                provider="IMF",
                indicator=indicator,
            )
            if search_results:
                discovery = await self.metadata_search.discover_indicator(
                    provider="IMF",
                    indicator_name=indicator,
                    search_results=search_results,
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

                if discovery and discovery.get("code"):
                    code = discovery["code"]
                    return code, discovery.get("name")

        # Note: We used to allow raw IMF codes without validation (if uppercase + underscore),
        # but this led to errors when LLMs generated fake codes like "CORPORATE_DEBT".
        # Now we ALWAYS validate through metadata search to ensure codes exist.

        if not self.metadata_search:
            raise DataNotAvailableError(
                f"IMF indicator '{indicator}' not recognized. Provide the official IMF code (e.g., NGDP_RPCH) or enable metadata discovery."
            )

        raise DataNotAvailableError(
            f"IMF indicator '{indicator}' not found. Try refining your query or consult IMF DataMapper for available indicators."
        )
