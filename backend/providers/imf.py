from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING
import asyncio
import hashlib
import json
import logging
import re

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client, effective_timeout
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from ..services.indicator_translator import get_indicator_translator
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

    # Indicators NOT available in DataMapper API
    # These will trigger clarification responses
    UNSUPPORTED_INDICATORS = {
        "PRODUCTIVITY_GROWTH",
        "PRODUCTIVITY",
        "PENSION_SUSTAINABILITY",
        "PENSION",
        "RETIREMENT",
        "SOCIAL_SECURITY",
        # Trade volume indicators (not in DataMapper, available in WEO database)
        "TRADE_VOLUME",
        "TRADE_VOLUME_GROWTH",
        "EXPORT_VOLUME",
        "EXPORT_VOLUME_GROWTH",
        "IMPORT_VOLUME",
        "IMPORT_VOLUME_GROWTH",
        "WORLD_TRADE_VOLUME",
        "WORLD_TRADE_GROWTH",
        # Commodity price indicators (not in DataMapper, available in PCPS database)
        "COMMODITY_PRICE",
        "COMMODITY_PRICE_INDEX",
        "COMMODITY_INDEX",
        "COMMODITY_PRICES",
        "PRIMARY_COMMODITY_PRICE",
        "PRIMARY_COMMODITY_PRICE_INDEX",
        "GLOBAL_COMMODITY_PRICE_INDEX",
        "GLOBAL_COMMODITY_INDEX",
        # Foreign exchange reserves (in IFS database, not DataMapper API)
        "FX_RESERVES",
        "FOREIGN_EXCHANGE_RESERVES",
        "RESERVES",
        "TOTAL_RESERVES",
        "CURRENCY_RESERVES",
        "INTERNATIONAL_RESERVES",
        "FOREX_RESERVES",
    }

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

    def _indicator_code(self, indicator: str) -> Optional[str]:
        """Validate raw IMF code via indicator translator/database lookup.

        Static INDICATOR_MAPPINGS have been removed in favour of the indicator
        database (330K+ entries) and the cross-provider IndicatorTranslator.
        This method now only returns a code when the translator can confirm it.
        """
        # Delegate entirely to the translator — it knows every valid IMF code
        # via the universal concept table and the indicators.db FTS5 index.
        return None

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
        return self.CODE_TO_COUNTRY_NAME.get(code, code)

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
            dataset_hint = self._likely_dataset_family_hint(indicator_code, indicator_label)
            if dataset_hint == "IMF.STA:BOP":
                return await self._fetch_bop_family(
                    indicator_code=indicator_code,
                    indicator_label=indicator_label,
                    countries=countries,
                    start_year=start_year,
                    end_year=end_year,
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

        # Extract data for the indicator
        if "values" not in payload or indicator_code not in payload["values"]:
            alternative_codes = self._get_alternative_indicator_codes(
                indicator=indicator,
                primary_code=indicator_code,
                requested_country_codes=country_codes,
            )
            for alternative_code in alternative_codes:
                alt_url = f"{self.base_url}/{alternative_code}"
                try:
                    alt_response = await self._retry_request(alt_url, max_retries=2, initial_delay=0.6)
                    alt_payload = alt_response.json()
                except Exception:
                    continue

                if "values" in alt_payload and alternative_code in alt_payload["values"]:
                    logger.info(
                        "IMF indicator fallback resolved %s -> %s for query '%s'",
                        indicator_code,
                        alternative_code,
                        indicator,
                    )
                    indicator_code = alternative_code
                    indicator_label = self._friendly_indicator_label(indicator, alternative_code)
                    payload = alt_payload
                    break
            else:
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
        """
        Normalize percentage values that are stored as decimals.
        If indicator mentions 'percent', 'rate', 'ratio' and values are < 1, multiply by 100.

        Args:
            data: List of data points with 'date' and 'value' keys
            indicator_name: Name of the indicator for detection logic

        Returns:
            Normalized data points with percentage values (e.g., 60 instead of 0.60)
        """
        if not data:
            return data

        # Check if values look like decimals (all non-null absolute values < 1.5)
        # We use 1.5 as threshold because some rates can exceed 1% (e.g., 1.2% inflation)
        # but values like 60% (debt/GDP) would never be stored as 60.0
        non_null_values = [abs(d['value']) for d in data if d['value'] is not None]
        if not non_null_values:
            return data

        max_value = max(non_null_values)

        # If max value < 1.5, likely stored as decimals (0.012 = 1.2%)
        # Exception: Negative values (deficits) can be < -1, so we use absolute values
        if max_value < 1.5:
            logger.info(f"Normalizing percentage values for indicator: {indicator_name} (max value: {max_value})")
            return [
                {'date': d['date'], 'value': d['value'] * 100 if d['value'] is not None else None}
                for d in data
            ]

        return data

    def _get_alternative_indicator_codes(
        self,
        indicator: str,
        primary_code: str,
        requested_country_codes: Optional[List[str]] = None,
        limit: int = 8,
    ) -> List[str]:
        """
        Find alternative IMF indicator codes when the primary candidate is unavailable.

        This is a general framework fallback:
        - prefers provider-native codes from indicator lookup search
        - de-prioritizes country-prefixed series for multi-country queries
        - keeps producer-price queries in the producer-price family
        """
        requested_country_codes = [str(code or "").upper() for code in (requested_country_codes or []) if code]
        primary_upper = str(primary_code or "").upper().strip()

        seed_codes: List[str] = []
        if primary_upper:
            seed_codes.append(primary_upper)
            if ":" in primary_upper:
                seed_codes.append(primary_upper.split(":", 1)[1])

        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
            search_results = lookup.search(indicator, provider="IMF", limit=20)
        except Exception as exc:
            logger.debug("IMF alternative indicator lookup failed: %s", exc)
            search_results = []

        producer_price_query = any(
            token in str(indicator or "").lower()
            for token in ("producer", "ppi", "wholesale")
        )

        preferred: List[str] = []
        secondary: List[str] = []
        seen: set[str] = set()

        def _record(code_value: Optional[str]) -> None:
            code = str(code_value or "").upper().strip()
            if not code or code in seen or code in seed_codes:
                return

            country_prefix = re.match(r"^([A-Z]{3})_", code)
            if country_prefix and requested_country_codes:
                if country_prefix.group(1) not in requested_country_codes:
                    return

            seen.add(code)
            if producer_price_query and not any(token in code for token in ("PPI", "PPPI", "PWPI")):
                secondary.append(code)
                return

            preferred.append(code)

        for seed in seed_codes[1:]:
            _record(seed)
        for candidate in search_results:
            _record(candidate.get("code"))

        if producer_price_query:
            # For producer-price requests, fail closed rather than silently
            # drifting to consumer-price substitutes.
            return preferred[:limit]

        return (preferred + secondary)[:limit]

    def _search_local_indicator_catalog(
        self,
        indicator: str,
        *,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        """Search the local IMF indicator catalog using normalized query variants.

        This is a bounded recovery path for long-tail IMF titles that the
        DataMapper metadata endpoint may not surface well. It searches the
        repo-local indicator database with country/provider wrappers stripped
        and preserves ranked, deduplicated candidates for downstream selection.
        """
        try:
            from ..services.indicator_database import get_indicator_lookup
            from ..services.indicator_resolution import exact_title_search_inputs

            lookup = get_indicator_lookup()
            search_queries = exact_title_search_inputs(indicator, "IMF")
        except Exception as exc:
            logger.debug("IMF local catalog search unavailable for '%s': %s", indicator, exc)
            return []

        seen: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for query_text in search_queries:
            try:
                results = lookup.search(query_text, provider="IMF", limit=limit)
            except Exception as exc:
                logger.debug("IMF local catalog lookup failed for '%s': %s", query_text, exc)
                continue

            for result in results:
                code = str(result.get("code") or "").strip().upper()
                name = str(result.get("name") or "").strip()
                if not code or not name or code in seen:
                    continue
                seen.add(code)
                candidates.append(
                    {
                        "code": code,
                        "id": code,
                        "name": name,
                        "description": str(result.get("description") or name),
                        "source": "LOCAL_IMF_CATALOG",
                    }
                )
                if len(candidates) >= limit:
                    return candidates

        return candidates

    def _indicator_catalog_entry(self, indicator_code: str) -> Optional[Dict[str, Any]]:
        """Return the local IMF indicator catalog entry for a code when available."""
        code = str(indicator_code or "").strip().upper()
        if not code:
            return None
        try:
            from ..services.indicator_database import get_indicator_lookup

            return get_indicator_lookup().get("IMF", code)
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
        """Infer the most likely IMF dataset family for a non-DataMapper series."""
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
            label.startswith("labor markets")
            or label.startswith("labour markets")
            or code.startswith(("LER_", "LUR_", "LUE_", "LFE_"))
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
        if "labor markets" in keywords or "employment rate" in keywords:
            return "IMF.STA:LS"
        if "national accounts" in keywords or "gross value added" in keywords:
            return "IMF.STA:NA_MAIN"

        return None

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
        response = await client.post(
            f"{self.engine_base_url}/platform/rest/v2/engine/data/sync/submit",
            json=payload,
            timeout=effective_timeout(60.0),
        )
        response.raise_for_status()
        token = str(getattr(response, "text", "") or "").strip()
        if not token:
            raise DataNotAvailableError("IMF engine query returned no OTT token")
        return token

    async def _retrieve_engine_ott(self, ott_token: str) -> httpx.Response:
        """Retrieve the result of a previously submitted SDMX engine query."""
        client = get_http_client()
        response = await client.get(
            f"{self.engine_base_url}/api/platform/v2/engine/data/sync/ott/{ott_token}",
            timeout=effective_timeout(60.0),
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
        dataset_hint = self._likely_dataset_family_hint(indicator_code, indicator_label)
        dataset_suffix = f" Likely next dataset family: {dataset_hint}." if dataset_hint else ""
        raise DataNotAvailableError(
            f"IMF indicator '{label}' ({indicator_code}) resolved to a non-DataMapper IMF family. "
            f"The current runtime can resolve this series from the local IMF catalog, but execution still "
            f"requires IMF dataset-family routing beyond the legacy DataMapper v1 path."
            f"{dataset_suffix}"
        )

    async def _resolve_from_local_catalog(
        self,
        indicator: str,
    ) -> Optional[tuple[str, Optional[str]]]:
        """Resolve an IMF indicator via the local indicator catalog."""
        local_catalog_results = self._search_local_indicator_catalog(indicator)
        if not local_catalog_results:
            return None

        logger.info(
            "IMF: local catalog fallback found %d candidates for '%s'",
            len(local_catalog_results),
            indicator,
        )
        if self.metadata_search:
            discovery = await self.metadata_search.discover_indicator(
                provider="IMF",
                indicator_name=indicator,
                search_results=local_catalog_results,
            )
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
                code = str(discovery["code"]).strip()
                return code, discovery.get("name")

        indicator_lower = str(indicator or "").lower()
        query_country_codes: set[str] = set()
        try:
            from ..routing.country_resolver import CountryResolver

            for alias in sorted(CountryResolver.COUNTRY_ALIASES.keys(), key=len, reverse=True):
                alias_text = str(alias).strip()
                if not alias_text:
                    continue
                if re.search(
                    rf"(?<![a-z0-9]){re.escape(alias_text)}(?![a-z0-9])",
                    indicator_lower,
                    flags=re.IGNORECASE,
                ):
                    iso2 = CountryResolver.normalize(alias_text)
                    iso3 = CountryResolver.to_iso3(iso2) if iso2 else None
                    if iso3:
                        query_country_codes.add(iso3)
        except Exception as exc:
            logger.debug("IMF local catalog country extraction skipped for '%s': %s", indicator, exc)

        def _local_rank(index: int, candidate: dict[str, Any]) -> tuple[int, int]:
            code = str(candidate.get("code") or "").upper()
            name = str(candidate.get("name") or "").lower()
            score = 0
            country_prefix = re.match(r"^([A-Z]{3})_", code)
            if country_prefix and query_country_codes:
                try:
                    from ..routing.country_resolver import CountryResolver

                    prefix = country_prefix.group(1)
                    if CountryResolver.to_iso2(prefix):
                        if prefix in query_country_codes:
                            score += 2
                        else:
                            score -= 3
                except Exception:
                    pass
            if " real " in f" {indicator_lower} ":
                if " real " in f" {name} " or "_R_" in code:
                    score += 3
                elif " nominal " in f" {name} ":
                    score -= 1
            if " nominal " in f" {indicator_lower} ":
                if " nominal " in f" {name} ":
                    score += 3
                if " real " in f" {name} " or "_R_" in code:
                    score -= 1
            return score, -index

        top_local = max(
            enumerate(local_catalog_results),
            key=lambda item: _local_rank(item[0], item[1]),
        )[1]
        return top_local["code"], top_local.get("name")

    async def _resolve_indicator_code(self, indicator: str) -> tuple[str, Optional[str]]:
        """Resolve IMF indicator code through hardcoded mappings, translator, or metadata search."""
        # Step 0: Check if indicator is explicitly unsupported
        indicator_key = indicator.upper().replace(" ", "_")
        if indicator_key in self.UNSUPPORTED_INDICATORS:
            # Provide helpful error message based on indicator type
            if any(kw in indicator_key for kw in ["TRADE_VOLUME", "TRADE_GROWTH", "EXPORT_VOLUME", "IMPORT_VOLUME"]):
                raise DataNotAvailableError(
                    f"Trade volume indicators are not available in the IMF DataMapper API. "
                    f"These indicators are published in the IMF World Economic Outlook (WEO) database, "
                    f"which is not accessible via the DataMapper API. "
                    f"Try using alternative data sources like OECD, World Bank, or UN Comtrade for trade volume data."
                )
            elif any(kw in indicator_key for kw in ["COMMODITY_PRICE", "COMMODITY_INDEX"]):
                raise DataNotAvailableError(
                    f"Commodity spot prices (gold, silver, oil, etc.) are not available through our current data providers. "
                    f"The IMF PCPS database has commodity prices but uses an SDMX API that is not currently accessible. "
                    f"For commodity price indices (not spot prices), try: "
                    f"• FRED: 'Producer Price Index All Commodities' (PPIACO) "
                    f"• For real-time gold/silver prices, consider dedicated services like kitco.com or goldprice.org"
                )
            elif any(kw in indicator_key for kw in ["PRODUCTIVITY", "OUTPUT_PER_WORKER", "GDP_PER_WORKER"]):
                raise DataNotAvailableError(
                    f"Labor productivity data is not available in the IMF DataMapper API. "
                    f"For productivity data, use: "
                    f"• OECD (best for OECD countries): Has comprehensive productivity databases "
                    f"• WorldBank (global coverage): Use indicator SL.GDP.PCAP.EM.KD (GDP per person employed) "
                    f"• FRED (US only): Use series OPHNFB (Nonfarm Business Sector Labor Productivity)"
                )
            elif any(kw in indicator_key for kw in ["RESERVES", "FX_RESERVES", "FOREX"]):
                raise DataNotAvailableError(
                    f"Foreign exchange reserves data is not available in the IMF DataMapper API. "
                    f"This data is in the IMF International Financial Statistics (IFS) database. "
                    f"For reserves data, use WorldBank with indicator FI.RES.TOTL.CD (Total reserves including gold)."
                )
            else:
                raise DataNotAvailableError(
                    f"IMF indicator '{indicator}' is not available in the DataMapper API. "
                    f"This data may be available through other IMF databases (WEO, PCPS, BOP) "
                    f"or alternative providers."
                )

        # Step 1: Try direct mapping
        mapped = self._indicator_code(indicator)
        if mapped:
            return mapped, self._friendly_indicator_label(indicator, mapped)

        # Step 1.5: If the caller already supplied an exact IMF code that exists
        # in the local indicator catalog, trust it directly. This preserves
        # explicit provider-code queries without re-running metadata discovery,
        # while still failing closed for fake codes because they will miss the
        # local exact lookup and continue down the normal validation path.
        exact_code_candidate = str(indicator or "").upper().strip()
        if re.fullmatch(r"[A-Z0-9][A-Z0-9_\.]{1,}", exact_code_candidate):
            try:
                from ..services.indicator_database import get_indicator_lookup

                lookup = get_indicator_lookup()
                exact_meta = lookup.get("IMF", exact_code_candidate)
            except Exception as exc:
                logger.debug("IMF exact-code lookup skipped for '%s': %s", indicator, exc)
                exact_meta = None

            if exact_meta:
                label_hint = str(exact_meta.get("name") or indicator)
                logger.info("IMF: Using exact local indicator code '%s' from catalog lookup", exact_code_candidate)
                return exact_code_candidate, self._friendly_indicator_label(label_hint, exact_code_candidate)

        indicator_text = str(indicator or "").strip()
        prioritize_local_catalog = (
            not self._looks_like_imf_code(indicator_text)
            and len(indicator_text.split()) >= 5
        )
        if prioritize_local_catalog:
            local_resolution = await self._resolve_from_local_catalog(indicator)
            if local_resolution:
                return local_resolution

        # Step 2: Try cross-provider indicator translator (handles indicator names from other systems)
        translator = get_indicator_translator()
        translated_code, concept_name = translator.translate_indicator(indicator, "IMF")
        if translated_code:
            logger.info(f"IMF: Translated '{indicator}' to '{translated_code}' via concept '{concept_name}'")
            label_hint = concept_name or indicator
            return translated_code, self._friendly_indicator_label(label_hint, translated_code)

        # Step 3: Try the local indicator catalog with normalized query variants.
        # This is especially important for long-tail IMF component titles where
        # DataMapper metadata search often returns zero exact keyword matches,
        # but the local catalog still has provider-native series entries.
        local_resolution = await self._resolve_from_local_catalog(indicator)
        if local_resolution:
            return local_resolution

        # Note: We used to allow raw IMF codes without validation (if uppercase + underscore),
        # but this led to errors when LLMs generated fake codes like "CORPORATE_DEBT".
        # Now we ALWAYS validate through metadata search to ensure codes exist.

        if not self.metadata_search:
            raise DataNotAvailableError(
                f"IMF indicator '{indicator}' not recognized. Provide the official IMF code (e.g., NGDP_RPCH) or enable metadata discovery."
            )

        # Use hierarchical search: SDMX first, then IMF DataMapper API
        search_results = await self.metadata_search.search_with_sdmx_fallback(
            provider="IMF",
            indicator=indicator,
        )
        if not search_results:
            raise DataNotAvailableError(
                f"IMF indicator '{indicator}' not found. Try a different description or provide the IMF indicator code."
            )

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

        raise DataNotAvailableError(
            f"IMF indicator '{indicator}' not found. Try refining your query or consult IMF DataMapper for available indicators."
        )
