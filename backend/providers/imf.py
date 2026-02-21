from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING
import asyncio
import logging

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client
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

    # Common economic indicators mapped to IMF DataMapper codes
    INDICATOR_MAPPINGS: Dict[str, str] = {
        # GDP and Growth
        "GDP": "NGDP_RPCH",  # Real GDP growth (annual percent change)
        "GDP_GROWTH": "NGDP_RPCH",
        "REAL_GDP_GROWTH": "NGDP_RPCH",

        # Unemployment
        "UNEMPLOYMENT": "LUR",  # Unemployment rate (percent of total labor force)
        "UNEMPLOYMENT_RATE": "LUR",
        "UNEMPLOYMENT_FORECAST": "LUR",  # Note: DataMapper only has historical, no forecasts
        "UNEMPLOYMENT_PROJECTION": "LUR",

        # Inflation
        "INFLATION": "PCPIPCH",  # Inflation, average consumer prices (annual percent change)
        "CPI": "PCPIPCH",
        "INFLATION_FORECAST": "PCPIPCH",  # Note: DataMapper only has historical, no forecasts
        "INFLATION_PROJECTION": "PCPIPCH",
        "FUTURE_INFLATION": "PCPIPCH",

        # Current Account and Trade
        "CURRENT_ACCOUNT": "BCA_NGDPD",  # Current account balance (percent of GDP)
        "BALANCE_OF_PAYMENTS": "BCA_NGDPD",  # Balance of payments (same as current account)
        "BOP": "BCA_NGDPD",  # Balance of payments abbreviation
        "BoP": "BCA_NGDPD",  # Balance of payments alternative

        # Government Debt
        "DEBT": "GGXWDG_NGDP",  # Fixed: was missing, causing data_not_available for "debt" queries
        "GOVT_DEBT": "GGXWDG_NGDP",  # General government gross debt (percent of GDP)
        "GOVERNMENT_DEBT": "GGXWDG_NGDP",
        "GOVERNMENT_DEBT_TO_GDP": "GGXWDG_NGDP",
        "GOV_DEBT": "GGXWDG_NGDP",
        "PUBLIC_DEBT": "GGXWDG_NGDP",
        "PUBLIC_DEBT_TO_GDP": "GGXWDG_NGDP",
        "DEBT_TO_GDP": "GGXWDG_NGDP",
        "DEBT_TO_GDP_RATIO": "GGXWDG_NGDP",
        "DEBT_RATIO": "GGXWDG_NGDP",
        "NATIONAL_DEBT": "GGXWDG_NGDP",
        "SOVEREIGN_DEBT": "GGXWDG_NGDP",
        "GLOBAL_DEBT": "GGXWDG_NGDP",  # Note: For multi-country queries
        "WORLD_DEBT": "GGXWDG_NGDP",

        # Fiscal Balance / Deficit
        "FISCAL_DEFICIT": "GGXCNL_NGDP",  # General government net lending/borrowing
        "BUDGET_DEFICIT": "GGXCNL_NGDP",
        "FISCAL_BALANCE": "GGXCNL_NGDP",

        # Government Revenue and Expenditure
        "GOVERNMENT_REVENUE": "rev",  # Government revenue (percent of GDP)
        "GOV_REVENUE": "rev",
        "FISCAL_REVENUE": "rev",
        "REVENUE": "rev",
        "GOVERNMENT_EXPENDITURE": "exp",  # Government expenditure (percent of GDP)
        "GOV_EXPENDITURE": "exp",
        "FISCAL_EXPENDITURE": "exp",
        "EXPENDITURE": "exp",
        "PRIMARY_EXPENDITURE": "prim_exp",  # Primary expenditure (percent of GDP)
        "PRIMARY_BALANCE": "pb",  # Primary balance (percent of GDP)

        # Household Debt
        "HOUSEHOLD_DEBT": "HH_ALL",  # Household debt, all instruments
        "CONSUMER_DEBT": "HH_ALL",

        # Corporate Debt
        "CORPORATE_DEBT": "NFC_ALL",  # Nonfinancial corporate debt, all instruments
        "BUSINESS_DEBT": "NFC_ALL",
        "NONFINANCIAL_CORPORATE": "NFC_ALL",

        # Exchange Rates
        "REER": "EREER",  # Real Effective Exchange Rates (2010=100)
        "REAL_EFFECTIVE_EXCHANGE_RATE": "EREER",
        "EXCHANGE_RATE_INDEX": "EREER",

        # Note: Savings and Investment indicators are NOT available in IMF DataMapper API
        # These would need to be retrieved via IMF WEO database or SDMX API
        # Queries for savings/investment will trigger metadata search

        # Demographics
        "POPULATION": "LP",  # Population (millions)
    }

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
                response = await client.get(url, timeout=60.0)
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
        """Get IMF indicator code from common indicator name or validate raw code."""
        key = indicator.upper().replace(" ", "_")

        # First, check if it's a mapped indicator name (e.g., "GDP" -> "NGDP_RPCH")
        if key in self.INDICATOR_MAPPINGS:
            return self.INDICATOR_MAPPINGS[key]

        # Second, check if the input is already a valid IMF code (e.g., "NGDP_RPCH")
        # This handles cases where the LLM returns the raw IMF code
        valid_codes = set(self.INDICATOR_MAPPINGS.values())
        if key in valid_codes:
            return key  # It's already a valid IMF code

        return None

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
        """
        from ..routing.country_resolver import CountryResolver

        # Try CountryResolver first (returns ISO3 for IMF compatibility)
        iso3 = CountryResolver.to_iso3(country)
        if iso3:
            return iso3

        # Fallback to local mappings
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

        # Convert all country names to IMF codes
        country_codes = [self._country_code(country) for country in countries]

        # Fetch data with retry logic
        url = f"{self.base_url}/{indicator_code}"

        try:
            response = await self._retry_request(url, max_retries=3, initial_delay=1.0)
            payload = response.json()
        except Exception as e:
            raise RuntimeError(
                f"Failed to fetch IMF indicator {indicator_code} after retries. "
                f"Error: {e}. The IMF API may be temporarily unavailable."
            )

        # Extract data for the indicator
        if "values" not in payload or indicator_code not in payload["values"]:
            raise DataNotAvailableError(f"IMF indicator {indicator_code} not found in response")

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
            return mapped, indicator

        # Step 2: Try cross-provider indicator translator (handles indicator names from other systems)
        translator = get_indicator_translator()
        translated_code, concept_name = translator.translate_indicator(indicator, "IMF")
        if translated_code:
            logger.info(f"IMF: Translated '{indicator}' to '{translated_code}' via concept '{concept_name}'")
            return translated_code, concept_name

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
            key = indicator.upper().replace(" ", "_")
            self.INDICATOR_MAPPINGS[key] = code
            return code, discovery.get("name")

        raise DataNotAvailableError(
            f"IMF indicator '{indicator}' not found. Try refining your query or consult IMF DataMapper for available indicators."
        )
