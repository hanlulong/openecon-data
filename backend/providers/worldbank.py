from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, TYPE_CHECKING
import logging
import re

import httpx

from ..config import get_settings
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from ..services.http_pool import get_http1_client, effective_timeout
from .base import BaseProvider

if TYPE_CHECKING:
    from ..services.metadata_search import MetadataSearchService

logger = logging.getLogger(__name__)

# ── WorldBank API health status cache ──────────────────────────────
# Tracks recent 502/timeout failures. When the API is down, skip it
# and go straight to fallback providers (IMF, Eurostat, OECD).
import time as _time_mod

_WB_HEALTH: dict = {"failures": 0, "last_failure": 0.0, "circuit_open": False}
_WB_CIRCUIT_THRESHOLD = 3      # consecutive failures to open circuit
_WB_CIRCUIT_COOLDOWN_S = 300   # 5 minutes before retrying


def _wb_record_failure():
    _WB_HEALTH["failures"] += 1
    _WB_HEALTH["last_failure"] = _time_mod.time()
    if _WB_HEALTH["failures"] >= _WB_CIRCUIT_THRESHOLD:
        _WB_HEALTH["circuit_open"] = True
        logger.warning("⚡ WorldBank circuit breaker OPEN — skipping WB for %ds", _WB_CIRCUIT_COOLDOWN_S)


def _wb_record_success():
    _WB_HEALTH["failures"] = 0
    _WB_HEALTH["circuit_open"] = False


def _wb_is_available() -> bool:
    if not _WB_HEALTH["circuit_open"]:
        return True
    elapsed = _time_mod.time() - _WB_HEALTH["last_failure"]
    if elapsed >= _WB_CIRCUIT_COOLDOWN_S:
        logger.info("⚡ WorldBank circuit breaker HALF-OPEN — retrying after %ds cooldown", int(elapsed))
        _WB_HEALTH["circuit_open"] = False
        _WB_HEALTH["failures"] = 0
        return True
    return False


class WorldBankProvider(BaseProvider):
    """World Bank data provider.

    PHASE D: Now inherits from BaseProvider for:
    - Unified provider_name property
    - Standardized HTTP retry logic
    - Common error handling patterns
    """
    # WorldBank region and aggregate codes (from https://api.worldbank.org/v2/region)
    # These can be used in place of country codes to query aggregate data
    # NOTE: These codes are VALID and work correctly with the WorldBank API
    # Previous testing showed SSA returning 400 errors, but this was due to external issues,
    # not invalid codes. Direct API tests confirm all these codes work.
    VALID_REGIONS = {
        # Major regions
        "AFE", "AFR", "AFW",  # Africa regions
        "EAS", "ECS", "LCN",  # Asia, Europe, Latin America
        "MEA", "NAC", "SAS",  # Middle East, North America, South Asia
        "SSA", "SSF",         # Sub-Saharan Africa (with/without high income)
        "WLD",                # World
        # Income levels (from https://api.worldbank.org/v2/incomelevel)
        "HIC", "LIC", "LMC", "LMY", "MIC", "UMC", "INX",
    }

    # Fallback mappings for income-based aggregates that often lack data
    # When LMY/MIC/LIC fail, we fetch multiple geographic regions that overlap
    # This provides comprehensive coverage for "developing countries" queries
    INCOME_AGGREGATE_FALLBACKS = {
        # Low & Middle Income (LMY) → fetch major developing region aggregates
        "LMY": ["SAS", "SSF", "EAS", "LCN", "MEA"],  # South Asia, Sub-Saharan Africa, East Asia, Latin America, Middle East
        # Middle Income (MIC) → similar regions
        "MIC": ["EAS", "LCN", "MEA", "ECS"],  # East Asia, Latin America, Middle East, Europe (includes some MIC)
        # Low Income (LIC) → focus on poorest regions
        "LIC": ["SSF", "SAS"],  # Sub-Saharan Africa, South Asia (most LIC countries)
    }

    # Regional term mappings for natural language queries
    # Maps common regional terms to WorldBank region codes
    # This prevents system from decomposing regional queries into individual country queries
    REGIONAL_TERM_MAPPINGS = {
        # Geographic regions
        "SOUTH ASIA": "SAS",
        "SOUTH ASIAN": "SAS",
        "EAST ASIA": "EAS",
        "EAST ASIAN": "EAS",
        "MIDDLE EAST": "MEA",
        "LATIN AMERICA": "LCN",
        "LATIN AMERICAN": "LCN",
        "NORTH AMERICA": "NAC",
        "NORTH AMERICAN": "NAC",
        "SUB-SAHARAN AFRICA": "SSF",  # Use SSF (excl. high income) - has data for poverty indicators
        "SUB SAHARAN AFRICA": "SSF",
        "AFRICA": "AFR",
        "AFRICAN": "AFR",
        "AFRICAN COUNTRIES": "AFR",
        "EUROPEAN": "ECS",
        "EUROPE": "ECS",
        "EUROPEAN UNION": "ECS",
        "EU": "ECS",
        "WORLD": "WLD",
        "GLOBAL": "WLD",
        "GLOBALLY": "WLD",

        # Regional groups
        # Note: ASEAN expanded to individual countries via COUNTRY_GROUP_EXPANSIONS
        "SOUTH AMERICA": "LCN",  # Latin America & Caribbean includes South America
        "SOUTH AMERICAN": "LCN",
        "SOUTH AMERICAN COUNTRIES": "LCN",

        # Income/development levels
        "DEVELOPING COUNTRIES": "LMY",  # Low & middle income
        "DEVELOPING NATIONS": "LMY",
        "DEVELOPING ECONOMIES": "LMY",  # Added for "developing economies inflation" queries
        "DEVELOPED COUNTRIES": "HIC",  # High income
        "DEVELOPED NATIONS": "HIC",
        "DEVELOPED ECONOMIES": "HIC",  # Added for consistency
        "EMERGING MARKETS": "LMY",
        "EMERGING ECONOMIES": "LMY",
        "LOW-INCOME COUNTRIES": "LIC",
        "LOW INCOME COUNTRIES": "LIC",
        "MIDDLE-INCOME COUNTRIES": "MIC",
        "MIDDLE INCOME COUNTRIES": "MIC",
        "HIGH-INCOME COUNTRIES": "HIC",
        "HIGH INCOME COUNTRIES": "HIC",

        # Special groupings
        "LEAST DEVELOPED COUNTRIES": "LIC",
        "LEAST DEVELOPED NATIONS": "LIC",
    }

    # Country group expansions - maps group names to lists of country codes
    # This enables queries like "G7 countries", "Nordic countries", etc.
    COUNTRY_GROUP_EXPANSIONS: Dict[str, List[str]] = {
        # G7 (7 major advanced economies)
        "G7": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "G7_COUNTRIES": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "G7 COUNTRIES": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "GROUP_OF_SEVEN": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "GROUP OF SEVEN": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],

        # Nordic countries
        "NORDIC": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "NORDIC_COUNTRIES": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "NORDIC COUNTRIES": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "SCANDINAVIA": ["SWE", "NOR", "DNK"],
        "SCANDINAVIAN_COUNTRIES": ["SWE", "NOR", "DNK"],
        "SCANDINAVIAN COUNTRIES": ["SWE", "NOR", "DNK"],

        # African countries (major economies)
        "AFRICAN": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "MAR", "TZA", "DZA", "AGO"],
        "AFRICAN_COUNTRIES": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "MAR", "TZA", "DZA", "AGO"],
        "AFRICAN COUNTRIES": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "MAR", "TZA", "DZA", "AGO"],

        # East Asian economies
        "EAST_ASIAN": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST_ASIAN_ECONOMIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST ASIAN ECONOMIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST_ASIAN_COUNTRIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST ASIAN COUNTRIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST_ASIA": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],

        # BRICS
        "BRICS": ["BRA", "RUS", "IND", "CHN", "ZAF"],
        "BRICS_COUNTRIES": ["BRA", "RUS", "IND", "CHN", "ZAF"],
        "BRICS COUNTRIES": ["BRA", "RUS", "IND", "CHN", "ZAF"],

        # BRICS+ (2024 expansion - includes Egypt, Ethiopia, Iran, UAE)
        "BRICS_PLUS": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],
        "BRICS+": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],
        "BRICS PLUS": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],

        # ASEAN (10 member countries)
        "ASEAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "ASEAN_COUNTRIES": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "ASEAN COUNTRIES": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "SOUTHEAST_ASIAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "SOUTHEAST ASIAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],

        # Top 10 CO2 emitters (approximate, based on recent data)
        "TOP_10_EMITTERS": ["CHN", "USA", "IND", "RUS", "JPN", "DEU", "IRN", "KOR", "SAU", "IDN"],
        "TOP_EMITTERS": ["CHN", "USA", "IND", "RUS", "JPN", "DEU", "IRN", "KOR", "SAU", "IDN"],
        "TOP 10 EMITTERS": ["CHN", "USA", "IND", "RUS", "JPN", "DEU", "IRN", "KOR", "SAU", "IDN"],

        # European Union (major members)
        "EU": ["DEU", "FRA", "ITA", "ESP", "NLD", "POL", "BEL", "SWE", "AUT", "GRC",
               "PRT", "CZE", "ROU", "HUN", "DNK", "FIN", "IRL"],
        "EUROPEAN_UNION": ["DEU", "FRA", "ITA", "ESP", "NLD", "POL", "BEL", "SWE", "AUT", "GRC",
                          "PRT", "CZE", "ROU", "HUN", "DNK", "FIN", "IRL"],
        "EUROPEAN UNION": ["DEU", "FRA", "ITA", "ESP", "NLD", "POL", "BEL", "SWE", "AUT", "GRC",
                          "PRT", "CZE", "ROU", "HUN", "DNK", "FIN", "IRL"],
        "EUROPEAN_COUNTRIES": ["DEU", "FRA", "ITA", "ESP", "GBR", "NLD", "POL", "BEL", "SWE", "AUT",
                               "GRC", "PRT", "CHE", "NOR", "DNK", "FIN", "IRL"],  # Includes non-EU GBR, CHE, NOR
        "EUROPEAN COUNTRIES": ["DEU", "FRA", "ITA", "ESP", "GBR", "NLD", "POL", "BEL", "SWE", "AUT",
                               "GRC", "PRT", "CHE", "NOR", "DNK", "FIN", "IRL"],

        # G20 (major economies)
        "G20": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],
        "G20_COUNTRIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                         "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],
        "G20 COUNTRIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                         "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],

        # Major/Top economies (by GDP)
        "MAJOR_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "MAJOR ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP_10_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP 10 ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "LARGEST_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "LARGEST ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],

        # Baltic states
        "BALTIC": ["EST", "LVA", "LTU"],
        "BALTIC_STATES": ["EST", "LVA", "LTU"],
        "BALTIC STATES": ["EST", "LVA", "LTU"],

        # OECD countries (major subset - 38 members total, showing main ones)
        "OECD": ["USA", "CAN", "MEX", "GBR", "DEU", "FRA", "ITA", "ESP", "NLD", "BEL",
                 "AUT", "CHE", "SWE", "NOR", "DNK", "FIN", "ISL", "IRL", "PRT", "GRC",
                 "POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU",
                 "JPN", "KOR", "AUS", "NZL", "TUR", "ISR", "CHL", "CRI", "COL"],
        "OECD_COUNTRIES": ["USA", "CAN", "MEX", "GBR", "DEU", "FRA", "ITA", "ESP", "NLD", "BEL",
                          "AUT", "CHE", "SWE", "NOR", "DNK", "FIN", "ISL", "IRL", "PRT", "GRC",
                          "POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU",
                          "JPN", "KOR", "AUS", "NZL", "TUR", "ISR", "CHL", "CRI", "COL"],
        "OECD COUNTRIES": ["USA", "CAN", "MEX", "GBR", "DEU", "FRA", "ITA", "ESP", "NLD", "BEL",
                          "AUT", "CHE", "SWE", "NOR", "DNK", "FIN", "ISL", "IRL", "PRT", "GRC",
                          "POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU",
                          "JPN", "KOR", "AUS", "NZL", "TUR", "ISR", "CHL", "CRI", "COL"],

        # Oil exporting countries (OPEC+ major members)
        "OIL_EXPORTING": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OIL_EXPORTING_COUNTRIES": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OIL EXPORTING COUNTRIES": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OPEC": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
        "OPEC_COUNTRIES": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
        "OPEC COUNTRIES": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
    }

    COUNTRY_MAPPINGS: Dict[str, str] = {
        # Common abbreviations
        "US": "USA",
        "USA": "USA",
        "UK": "GBR",
        "GB": "GBR",
        "UAE": "ARE",

        # ISO2 codes (World Bank API accepts both ISO2 and ISO3)
        "DE": "DE", "FR": "FR", "JP": "JP", "CN": "CN", "IN": "IN",
        "CA": "CA", "BR": "BR", "RU": "RU", "AU": "AU", "ES": "ES",
        "PT": "PT", "SE": "SE", "HR": "HR", "ID": "ID", "MX": "MX",
        "ZA": "ZA", "VN": "VN", "PH": "PH", "TR": "TR", "PL": "PL",
        "EG": "EG", "BD": "BD", "KR": "KR", "NO": "NO", "DK": "DK",
        "FI": "FI", "IE": "IE", "NG": "NG", "TH": "TH", "AR": "AR",
        "IT": "IT", "NL": "NL", "BE": "BE", "AT": "AT", "GR": "GR",
        "CH": "CH", "SG": "SG", "MY": "MY", "PK": "PK", "CL": "CL",
        "CO": "CO", "PE": "PE", "VE": "VE", "CZ": "CZ", "HU": "HU",
        "RO": "RO", "UA": "UA", "IL": "IL", "SA": "SA", "NZ": "NZ",

        # Comprehensive country names to ISO2 codes
        # Americas
        "UNITED_STATES": "USA", "AMERICA": "USA", "UNITED_STATES_OF_AMERICA": "USA",
        "CANADA": "CA", "MEXICO": "MX", "BRAZIL": "BR", "ARGENTINA": "AR",
        "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE", "VENEZUELA": "VE",
        "ECUADOR": "EC", "BOLIVIA": "BO", "PARAGUAY": "PY", "URUGUAY": "UY",
        "COSTA_RICA": "CR", "PANAMA": "PA", "CUBA": "CU", "DOMINICAN_REPUBLIC": "DO",
        "PUERTO_RICO": "PR", "GUATEMALA": "GT", "HONDURAS": "HN", "EL_SALVADOR": "SV",
        "NICARAGUA": "NI", "JAMAICA": "JM", "TRINIDAD_AND_TOBAGO": "TT", "HAITI": "HT",

        # Europe
        "GERMANY": "DE", "FRANCE": "FR", "UNITED_KINGDOM": "GBR", "BRITAIN": "GBR",
        "ITALY": "IT", "SPAIN": "ES", "NETHERLANDS": "NL", "HOLLAND": "NL",
        "BELGIUM": "BE", "AUSTRIA": "AT", "SWITZERLAND": "CH", "SWEDEN": "SE",
        "NORWAY": "NO", "DENMARK": "DK", "FINLAND": "FI", "IRELAND": "IE",
        "PORTUGAL": "PT", "GREECE": "GR", "POLAND": "PL", "CZECH_REPUBLIC": "CZ",
        "CZECHIA": "CZ", "HUNGARY": "HU", "ROMANIA": "RO", "UKRAINE": "UA",
        "CROATIA": "HR", "SLOVAKIA": "SK", "SLOVENIA": "SI", "BULGARIA": "BG",
        "SERBIA": "RS", "BOSNIA": "BA", "ALBANIA": "AL", "NORTH_MACEDONIA": "MK",
        "MACEDONIA": "MK", "MONTENEGRO": "ME", "KOSOVO": "XK", "LATVIA": "LV",
        "LITHUANIA": "LT", "ESTONIA": "EE", "ICELAND": "IS", "LUXEMBOURG": "LU",
        "MALTA": "MT", "CYPRUS": "CY", "MOLDOVA": "MD", "BELARUS": "BY",

        # Asia
        "CHINA": "CN", "JAPAN": "JP", "SOUTH_KOREA": "KR", "KOREA": "KR",
        "NORTH_KOREA": "KP", "INDIA": "IN", "INDONESIA": "ID", "PAKISTAN": "PK",
        "BANGLADESH": "BD", "VIETNAM": "VN", "THAILAND": "TH", "PHILIPPINES": "PH",
        "MALAYSIA": "MY", "SINGAPORE": "SG", "MYANMAR": "MM", "BURMA": "MM",
        "CAMBODIA": "KH", "LAOS": "LA", "SRI_LANKA": "LK", "NEPAL": "NP",
        "TAIWAN": "TW", "HONG_KONG": "HK", "MONGOLIA": "MN", "BRUNEI": "BN",
        "TIMOR_LESTE": "TL", "MALDIVES": "MV", "BHUTAN": "BT", "AFGHANISTAN": "AF",

        # Middle East
        "TURKEY": "TR", "TURKIYE": "TR", "IRAN": "IR", "IRAQ": "IQ",
        "SAUDI_ARABIA": "SA", "ISRAEL": "IL", "UNITED_ARAB_EMIRATES": "ARE",
        "QATAR": "QA", "KUWAIT": "KW", "OMAN": "OM", "BAHRAIN": "BH",
        "JORDAN": "JO", "LEBANON": "LB", "SYRIA": "SY", "YEMEN": "YE",
        "PALESTINE": "PS",

        # Africa
        "NIGERIA": "NG", "SOUTH_AFRICA": "ZA", "EGYPT": "EG", "KENYA": "KE",
        "ETHIOPIA": "ET", "GHANA": "GH", "TANZANIA": "TZ", "MOROCCO": "MA",
        "ALGERIA": "DZ", "TUNISIA": "TN", "LIBYA": "LY", "SUDAN": "SD",
        "UGANDA": "UG", "ANGOLA": "AO", "MOZAMBIQUE": "MZ", "ZIMBABWE": "ZW",
        "ZAMBIA": "ZM", "BOTSWANA": "BW", "NAMIBIA": "NA", "SENEGAL": "SN",
        "IVORY_COAST": "CI", "COTE_D_IVOIRE": "CI", "CAMEROON": "CM",
        "DEMOCRATIC_REPUBLIC_OF_CONGO": "CD", "DRC": "CD", "CONGO": "CG",
        "RWANDA": "RW", "MAURITIUS": "MU", "MADAGASCAR": "MG",

        # Oceania
        "AUSTRALIA": "AU", "NEW_ZEALAND": "NZ", "PAPUA_NEW_GUINEA": "PG",
        "FIJI": "FJ",

        # Russia/Central Asia
        "RUSSIA": "RU", "RUSSIAN_FEDERATION": "RU", "KAZAKHSTAN": "KZ",
        "UZBEKISTAN": "UZ", "TURKMENISTAN": "TM", "TAJIKISTAN": "TJ",
        "KYRGYZSTAN": "KG", "AZERBAIJAN": "AZ", "GEORGIA": "GE", "ARMENIA": "AM",
    }

    @property
    def provider_name(self) -> str:
        """Return canonical provider name for logging and routing."""
        return "WorldBank"

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)  # Initialize BaseProvider
        settings = get_settings()
        self.base_url = settings.worldbank_base_url.rstrip("/")
        self.metadata_search = metadata_search_service

    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Implementation of BaseProvider's abstract method.

        Routes to fetch_indicator with appropriate parameters.
        """
        indicator = params.get("indicator", "GDP")
        country = params.get("country") or params.get("region")
        countries = params.get("countries")
        start_date = params.get("start_date") or params.get("startDate")
        end_date = params.get("end_date") or params.get("endDate")

        return await self.fetch_indicator(
            indicator=indicator,
            country=country,
            countries=countries,
            start_date=start_date,
            end_date=end_date,
        )

    def _map_regional_term(self, term: str) -> Optional[str]:
        """
        Map regional terms to WorldBank region codes.

        Args:
            term: Regional term (e.g., "South Asia", "developing countries")

        Returns:
            WorldBank region code if term is regional, None otherwise
        """
        term_upper = term.upper().strip()

        # CRITICAL: First check if this is a known country name or country CODE
        # Countries like "South Africa", "South Korea" should NOT be treated as regions
        # Also ISO codes like "DEU" should not match "EU" partial term
        term_key = term_upper.replace(" ", "_")
        if term_key in self.COUNTRY_MAPPINGS:
            logger.debug(f"'{term}' is a country (code: {self.COUNTRY_MAPPINGS[term_key]}), not a region")
            return None

        # Check if it's an ISO country code (2 or 3 letters)
        # ISO codes should NEVER be treated as regional terms
        if len(term_upper) <= 3 and term_upper.isalpha():
            # This looks like a country code (e.g., "DEU", "USA", "GB")
            # Don't try to match partial regional terms within it
            logger.debug(f"'{term}' looks like an ISO country code, not treating as region")
            return None

        # Direct lookup for exact regional term matches
        if term_upper in self.REGIONAL_TERM_MAPPINGS:
            region_code = self.REGIONAL_TERM_MAPPINGS[term_upper]
            logger.info(f"🌍 Mapped regional term '{term}' → WorldBank region code '{region_code}'")
            return region_code

        # Partial match (e.g., "countries in South Asia" → "SAS")
        # But only if the term is clearly about a region, not a country
        # AND the term is longer than a typical country code (> 3 chars)
        if len(term_upper) > 3:
            for regional_term, region_code in self.REGIONAL_TERM_MAPPINGS.items():
                if regional_term in term_upper:
                    # Additional safety: don't match partial region names within country names
                    # e.g., don't match "AFRICA" in "SOUTH AFRICA" or "ASIA" in "SOUTH KOREA"
                    # Check if the term starts with a known country prefix
                    known_country_prefixes = ["SOUTH AFRICA", "SOUTH KOREA", "NORTH KOREA",
                                              "CENTRAL AFRICAN", "WEST BANK"]
                    is_country = any(term_upper.startswith(prefix) or term_upper == prefix
                                    for prefix in known_country_prefixes)
                    if not is_country:
                        logger.info(f"🌍 Matched regional term '{regional_term}' in '{term}' → WorldBank region code '{region_code}'")
                        return region_code
                    else:
                        logger.debug(f"Skipping regional match for country: '{term}'")

        return None

    def _expand_country_group(self, country: str) -> Optional[List[str]]:
        """
        Check if the country string represents a country group and expand it.

        Uses CountryResolver as the single source of truth for region definitions.
        Falls back to WorldBank-specific mappings only for groups not in CountryResolver.

        Args:
            country: Country string (e.g., "G7", "Nordic countries")

        Returns:
            List of ISO3 country codes if it's a group, None otherwise
        """
        from ..routing.country_resolver import CountryResolver

        key = country.upper().replace(" ", "_")

        # Guardrail: if this already resolves to a concrete country code (e.g., US/USA),
        # do NOT attempt fuzzy group expansion.
        if CountryResolver.normalize(country):
            return None

        # First, try CountryResolver (single source of truth)
        expanded = CountryResolver.get_region_expansion(key, format="iso3")
        if expanded:
            logger.info(f"🌍 Expanded country group '{country}' via CountryResolver → {len(expanded)} countries: {', '.join(expanded)}")
            return expanded

        # Try partial match variants
        for variant in [key, key.replace("_COUNTRIES", ""), key.replace("_NATIONS", "")]:
            expanded = CountryResolver.get_region_expansion(variant, format="iso3")
            if expanded:
                logger.info(f"🌍 Matched country group '{variant}' via CountryResolver → {len(expanded)} countries: {', '.join(expanded)}")
                return expanded

        # Fall back to WorldBank-specific group expansions (for non-standard groups)
        if key in self.COUNTRY_GROUP_EXPANSIONS:
            countries = self.COUNTRY_GROUP_EXPANSIONS[key]
            logger.info(f"🌍 Expanded country group '{country}' via WorldBank mappings → {len(countries)} countries: {', '.join(countries)}")
            return countries

        # Check for partial matches in WorldBank-specific groups.
        # Only allow this for longer tokens to avoid false positives
        # like "US" matching "BRICS_PLUS".
        if len(key) < 4:
            return None

        for group_key, countries in self.COUNTRY_GROUP_EXPANSIONS.items():
            if group_key in key or key in group_key:
                logger.info(f"🌍 Matched country group '{group_key}' in '{country}' → {len(countries)} countries: {', '.join(countries)}")
                return countries

        return None

    def _country_code(self, country: str) -> str:
        """
        Convert country name/code to WorldBank API format.

        CENTRALIZED COUNTRY HANDLING: Uses CountryResolver as primary source,
        with fallback to WorldBank-specific regional/aggregate codes.

        Accepts:
        - ISO2/ISO3 country codes (e.g., "US", "USA", "CN")
        - Region codes (e.g., "SSA", "EAS", "WLD")
        - Income level codes (e.g., "HIC", "LMC")
        - Country names (e.g., "United States", "Germany")
        - Regional terms (e.g., "South Asia", "developing countries")

        Returns:
        - Uppercase country/region/aggregate code for API
        """
        # First, try to map regional terms (WorldBank-specific aggregates)
        region_code = self._map_regional_term(country)
        if region_code:
            return region_code

        country_upper = country.upper()

        # WorldBank's "all countries" endpoint is case-sensitive in practice:
        # /country/all works while /country/ALL can hang or return no body.
        # Keep the provider's no-country default on the intended all-country
        # surface instead of accidentally uppercasing it into a broken code.
        if country_upper == "ALL":
            return "all"

        # Check if it's a valid WorldBank region/aggregate code
        if country_upper in self.VALID_REGIONS:
            logger.debug(f"Using WorldBank region/aggregate code: {country_upper}")
            return country_upper

        # CENTRALIZED: Use CountryResolver for individual country normalization
        try:
            from ..routing.country_resolver import CountryResolver
            iso_code = CountryResolver.normalize(country)
            if iso_code:
                logger.debug(f"CountryResolver: '{country}' → '{iso_code}'")
                return iso_code
        except Exception as e:
            logger.debug(f"CountryResolver failed: {e}")

        # Fallback to local mappings
        key = country_upper.replace(" ", "_")
        mapped = self.COUNTRY_MAPPINGS.get(key)
        if mapped:
            return mapped

        # Default: return uppercase (might be ISO2/ISO3 code)
        logger.debug(f"Using country code as-is: {country_upper}")
        return country_upper

    async def _get_alternative_indicators(
        self, indicator: str, primary_code: str, limit: int = 5
    ) -> List[str]:
        """
        Get alternative indicator codes from the database for fallback.

        INFRASTRUCTURE FIX: When an indicator is archived or unavailable,
        this provides alternatives to try. This is a GENERAL mechanism that
        helps ALL queries hitting unavailable indicators.
        """
        try:
            from ..services.indicator_database import (
                get_indicator_database,
                get_indicator_lookup,
            )
            lookup = get_indicator_lookup()
            db = get_indicator_database()

            primary_row = lookup.get("WorldBank", primary_code)
            primary_title = str((primary_row or {}).get("name") or "").strip()
            primary_title_norm = self._normalize_indicator_title(primary_title)
            requested_title_norm = self._normalize_indicator_title(indicator)

            candidates: list[dict[str, Any]] = []
            seen_codes: set[str] = set()
            order = 0

            def add_rows(rows: list[dict[str, Any]] | None) -> None:
                nonlocal order
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    code = str(row.get("code") or "").strip()
                    if not code or code == primary_code or code in seen_codes:
                        continue
                    candidate = dict(row)
                    candidate["_alternative_order"] = order
                    order += 1
                    seen_codes.add(code)
                    candidates.append(candidate)

            # Keep the existing ranked lookup path, then supplement it with
            # provider-native exact-title and raw FTS rows.  The raw DB path is
            # intentionally not semantically authoritative; it only broadens the
            # retry candidates after the selected provider-native code returned
            # no data.
            broad_limit = max(limit * 4, 20)
            add_rows(lookup.search(indicator, provider="WorldBank", limit=broad_limit))

            if primary_title:
                add_rows(
                    lookup.exact_name_matches(
                        [primary_title],
                        provider="WorldBank",
                        limit=broad_limit,
                    )
                )

            raw_limit = max(limit * 20, 80)
            add_rows(db.search(indicator, provider="WorldBank", limit=raw_limit))
            if primary_title and primary_title != indicator:
                add_rows(db.search(primary_title, provider="WorldBank", limit=raw_limit))

            def candidate_rank(row: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
                code = str(row.get("code") or "")
                name_norm = self._normalize_indicator_title(str(row.get("name") or ""))
                source_id, source_name = self._indicator_row_source(row)
                source_name_lower = source_name.lower()

                exact_primary_title = int(bool(primary_title_norm and name_norm == primary_title_norm))
                exact_requested_title = int(bool(requested_title_norm and name_norm == requested_title_norm))
                generic_wdi_source = int(
                    source_id == "2" or source_name_lower == "world development indicators"
                )
                non_archive_source = int("archive" not in source_name_lower)
                dotted_provider_code = int("." in code)
                # Negative order preserves upstream rank when provider-native
                # evidence is otherwise tied.
                return (
                    exact_primary_title,
                    exact_requested_title,
                    generic_wdi_source,
                    non_archive_source,
                    dotted_provider_code,
                    -int(row.get("_alternative_order") or 0),
                )

            candidates.sort(key=candidate_rank, reverse=True)
            return [str(row.get("code")) for row in candidates[:limit]]
        except Exception as e:
            logger.debug(f"Could not get alternative indicators: {e}")
            return []

    @staticmethod
    def _normalize_indicator_title(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @staticmethod
    def _indicator_row_source(row: dict[str, Any]) -> tuple[str, str]:
        raw = row.get("raw_metadata")
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if isinstance(raw, dict):
            source = raw.get("source")
            if isinstance(source, dict):
                return (
                    str(source.get("id") or "").strip(),
                    str(source.get("value") or source.get("name") or "").strip(),
                )
        return "", ""

    @staticmethod
    def _looks_like_worldbank_indicator_code(indicator: str) -> bool:
        """Return true for exact WorldBank indicator-code shapes.

        WorldBank public REST indicator IDs are not limited to dotted WDI
        forms; public sources such as G20 FII and DDH also use lower-case,
        underscore, and digit-bearing codes (for example `fin14q2` and
        `al_prim_some_dfcl_all`).  These should be treated as exact provider
        requests, not natural-language text that should try alternatives.
        """
        text = str(indicator or "").strip()
        if not text or re.search(r"\s", text) or not text[0].isalpha():
            return False
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,127}", text):
            return False
        return "." in text or "_" in text or any(ch.isdigit() for ch in text)

    def _indicator_source_id(self, indicator_code: str) -> Optional[str]:
        """Return the provider-native WorldBank source id for an exact code.

        This is metadata plumbing: the local catalog stores the source object
        returned by the WorldBank indicator metadata API.  It lets exact codes
        from non-WDI sources use their documented source-specific data endpoint
        instead of being mislabeled as deleted by the generic WDI data path.
        """
        code = str(indicator_code or "").strip()
        if not code:
            return None
        try:
            from ..services.indicator_database import get_indicator_lookup

            row = get_indicator_lookup().get("WorldBank", code)
        except Exception as exc:
            logger.debug("WorldBank source-id lookup skipped for %s: %s", code, exc)
            return None
        if not row:
            return None

        raw = row.get("raw_metadata")
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if isinstance(raw, dict):
            source = raw.get("source")
            if isinstance(source, dict):
                source_id = str(source.get("id") or "").strip()
                if source_id:
                    return source_id
        return None

    @staticmethod
    def _source_variable(record: dict[str, Any], concept: str) -> dict[str, Any]:
        for item in record.get("variable") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("concept") or "").strip().lower() == concept.lower():
                return item
        return {}

    @staticmethod
    def _source_endpoint_country_code(country_code: str) -> str:
        """Normalize countries for WorldBank source-specific endpoints.

        The standard `/country/{code}/indicator/{indicator}` endpoint accepts
        ISO2 country codes such as `US`, but the documented
        `/sources/{source}/country/{code}/series/{indicator}` endpoint expects
        ISO3 codes for individual countries (`USA`).  Keep aggregate/all
        surfaces intact and only convert concrete countries.
        """
        code = str(country_code or "").strip()
        if not code:
            return code
        if code.lower() == "all":
            return "all"
        try:
            from ..routing.country_resolver import CountryResolver

            iso3 = CountryResolver.to_iso3(code)
            if iso3:
                return iso3
            iso2 = CountryResolver.normalize(code)
            if iso2:
                iso3 = CountryResolver.to_iso3(iso2)
                if iso3:
                    return iso3
        except Exception:
            return code
        return code

    @staticmethod
    def _source_time_point(
        time_var: dict[str, Any],
        last_updated: str,
    ) -> tuple[str, Optional[int], str, bool]:
        """Normalize a WorldBank source Time variable.

        Source-specific endpoints use the documented advanced-data shape where
        Time is another provider-native concept variable.  When using MRNEV,
        some sources return a frequency label such as ``Monthly`` or ``Annual``
        instead of the exact observation period.  Keep those provider-native
        records usable without inventing a semantic year by dating the single
        latest-value point to the source ``lastupdated`` stamp and separating
        frequency labels into distinct series.
        """
        raw_time = str(time_var.get("id") or time_var.get("value") or "").strip()
        time_value = str(time_var.get("value") or time_var.get("id") or "").strip()
        combined = " ".join(part for part in (raw_time, time_value) if part).strip()

        match = re.search(r"YR(?P<year>19\d{2}|20\d{2})-M(?P<month>\d{1,2})", combined, re.IGNORECASE)
        if match:
            year = int(match.group("year"))
            month = max(1, min(12, int(match.group("month"))))
            return f"{year:04d}-{month:02d}-01", year, time_value or raw_time, True

        match = re.search(r"(?P<year>19\d{2}|20\d{2})M(?P<month>\d{1,2})", combined, re.IGNORECASE)
        if match:
            year = int(match.group("year"))
            month = max(1, min(12, int(match.group("month"))))
            return f"{year:04d}-{month:02d}-01", year, time_value or raw_time, True

        match = re.search(r"(?P<year>19\d{2}|20\d{2})Q(?P<quarter>[1-4])", combined, re.IGNORECASE)
        if match:
            year = int(match.group("year"))
            month = (int(match.group("quarter")) - 1) * 3 + 1
            return f"{year:04d}-{month:02d}-01", year, time_value or raw_time, True

        match = re.search(r"\b(19\d{2}|20\d{2})\b", combined)
        if match:
            year = int(match.group(1))
            return f"{year:04d}-01-01", year, time_value or raw_time, True

        fallback = ""
        last_updated_text = str(last_updated or "").strip()
        last_updated_match = re.search(r"\b(19\d{2}|20\d{2})-\d{2}-\d{2}\b", last_updated_text)
        if last_updated_match:
            fallback = last_updated_match.group(0)
        else:
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", last_updated_text)
            fallback = f"{year_match.group(1)}-01-01" if year_match else "latest"

        return fallback, None, time_value or raw_time or "latest", False

    @staticmethod
    def _source_frequency(time_label: str, parsed_date: str) -> str:
        label = str(time_label or "").strip().lower()
        if "monthly" in label or re.search(
            r"\d{4}\s*M\d{1,2}|YR\d{4}-M\d{1,2}",
            str(time_label),
            re.IGNORECASE,
        ):
            return "monthly"
        if "quarter" in label or re.search(r"\d{4}Q[1-4]", str(time_label), re.IGNORECASE):
            return "quarterly"
        if "annual" in label or re.fullmatch(r"\d{4}-01-01", str(parsed_date or "")):
            return "annual"
        return "annual"

    async def _fetch_source_series_endpoint(
        self,
        *,
        source_id: str,
        indicator: str,
        country_codes: list[str],
        start_date: Optional[str],
        end_date: Optional[str],
        headers: dict[str, str],
        client: Any,
    ) -> List[NormalizedData]:
        """Fetch exact non-WDI WorldBank source-series data.

        The generic `/country/{country}/indicator/{code}` endpoint can report
        source-specific indicators as deleted even when their metadata and data
        are public under `/sources/{source}/country/{country}/series/{code}`.
        Use that documented source endpoint only for exact catalog-backed codes.
        """
        source_id = str(source_id or "").strip()
        indicator_code = str(indicator or "").strip()
        if not source_id or not indicator_code:
            return []

        batch_codes = ";".join(
            self._source_endpoint_country_code(country_code)
            for country_code in (country_codes or ["all"])
        )
        url = f"{self.base_url}/sources/{source_id}/country/{batch_codes}/series/{indicator_code}"
        params = {"format": "json", "per_page": 1000}
        if start_date and end_date:
            params["date"] = f"{start_date[:4]}:{end_date[:4]}"
        elif not start_date and not end_date:
            # Source-specific endpoints often sort all-country records by
            # far-future placeholder years with null values.  WorldBank's
            # documented MRNEV parameter returns the most recent non-empty
            # provider-native values, avoiding false data_not_available
            # outcomes for exact no-date catalog-code requests.
            params["MRNEV"] = 5
        logger.info("WorldBank source endpoint call: %s | params=%s", url, params)

        response = await self._get_with_retry(
            client,
            url,
            params=params,
            headers=headers,
            timeout=effective_timeout(25.0),
        )
        try:
            payload = response.json()
        except ValueError:
            logger.warning(
                "WorldBank source endpoint returned non-JSON response for source=%s indicator=%s",
                source_id,
                indicator_code,
            )
            return []
        if not isinstance(payload, dict):
            return []
        source = payload.get("source") or {}
        if not isinstance(source, dict):
            return []
        records = [record for record in (source.get("data") or []) if isinstance(record, dict)]
        total_pages = int(payload.get("pages") or 1)
        if 1 < total_pages <= 10:
            for page_num in range(2, total_pages + 1):
                page_params = {**params, "page": page_num}
                page_response = await self._get_with_retry(
                    client,
                    url,
                    params=page_params,
                    headers=headers,
                    timeout=effective_timeout(25.0),
                )
                try:
                    page_payload = page_response.json()
                except ValueError:
                    logger.warning(
                        "WorldBank source endpoint page %d returned non-JSON response for source=%s indicator=%s",
                        page_num,
                        source_id,
                        indicator_code,
                    )
                    continue
                page_source = page_payload.get("source") if isinstance(page_payload, dict) else None
                if isinstance(page_source, dict):
                    records.extend(
                        record
                        for record in (page_source.get("data") or [])
                        if isinstance(record, dict)
                    )
        if not records:
            return []
        total_records = int(payload.get("total") or len(records) or 0)

        source_name = str(source.get("name") or f"WorldBank source {source_id}").strip()
        last_updated = str(payload.get("lastupdated") or response.headers.get("Date", "") or "")
        api_url = self._compose_source_url(url, params)
        source_url = f"https://api.worldbank.org/v2/sources/{source_id}/country/{batch_codes}/series/{indicator_code}?format=json"

        start_year = int(start_date[:4]) if start_date else None
        end_year = int(end_date[:4]) if end_date else None

        grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for record in records:
            value = record.get("value")
            if value is None:
                continue

            country_var = self._source_variable(record, "Country")
            time_var = self._source_variable(record, "Time")
            series_var = self._source_variable(record, "Series")

            point_date, year, time_label, has_observation_period = self._source_time_point(
                time_var,
                last_updated,
            )
            if (start_year or end_year) and year is None:
                continue
            if start_year and year < start_year:
                continue
            if end_year and year > end_year:
                continue

            country_id = str(country_var.get("id") or "").strip() or "all"
            country_name = str(country_var.get("value") or country_id).strip()
            indicator_name = str(series_var.get("value") or indicator_code).strip()

            dimension_parts: list[tuple[str, str, str]] = []
            for item in record.get("variable") or []:
                if not isinstance(item, dict):
                    continue
                concept = str(item.get("concept") or "").strip()
                if concept.lower() in {"country", "series", "time"}:
                    continue
                dim_id = str(item.get("id") or "").strip()
                dim_value = str(item.get("value") or dim_id).strip()
                if dim_id or dim_value:
                    dimension_parts.append((concept, dim_id, dim_value))
            if time_label and not has_observation_period:
                dimension_parts.append(("Time", str(time_var.get("id") or time_label).strip(), time_label))

            series_key = (
                country_id,
                tuple((concept, dim_id) for concept, dim_id, _ in dimension_parts),
            )
            bucket = grouped.setdefault(
                series_key,
                {
                    "country_id": country_id,
                    "country_name": country_name,
                    "indicator_name": indicator_name,
                    "dimension_parts": dimension_parts,
                    "time_label": time_label,
                    "frequency": self._source_frequency(time_label, point_date),
                    "uses_lastupdated_date": not has_observation_period,
                    "points": [],
                },
            )
            bucket["points"].append({"date": point_date, "value": value})

        results: List[NormalizedData] = []
        max_source_series = 500
        for bucket in grouped.values():
            if len(results) >= max_source_series:
                break
            points = sorted(bucket["points"], key=lambda item: item["date"])
            if not points:
                continue
            indicator_label = str(bucket.get("indicator_name") or indicator_code).strip()
            dimension_parts = list(bucket.get("dimension_parts") or [])
            if dimension_parts:
                label_suffix = " — ".join(
                    dim_value or dim_id
                    for _, dim_id, dim_value in dimension_parts
                    if dim_value or dim_id
                )
                if label_suffix:
                    indicator_label = f"{indicator_label} — {label_suffix}"
            if not dimension_parts:
                series_id = indicator_code
            elif len(dimension_parts) == 1 and dimension_parts[0][0].lower() == "sector":
                series_id = f"{indicator_code}:{dimension_parts[0][1]}"
            else:
                dimension_suffix = "|".join(
                    f"{concept}={dim_id or dim_value}"
                    for concept, dim_id, dim_value in dimension_parts
                    if concept and (dim_id or dim_value)
                )
                series_id = f"{indicator_code}:{dimension_suffix}" if dimension_suffix else indicator_code
            values = [point["value"] for point in points if point.get("value") is not None]
            unit = "USD" if "$" in indicator_label or "us$" in indicator_label.lower() or "dollars" in indicator_label.lower() else ""
            notes = [
                f"WorldBank source-specific endpoint source={source_id}",
                (
                    "dimensions="
                    + "; ".join(
                        f"{concept}={dim_value or dim_id}"
                        for concept, dim_id, dim_value in dimension_parts
                    )
                ) if dimension_parts else "dimensions=not specified",
                (
                    f"source endpoint returned {total_records} records; "
                    f"response limited to first {max_source_series} series"
                ) if total_records > max_source_series else "source endpoint returned complete first page",
            ]
            if bucket.get("uses_lastupdated_date"):
                notes.append(
                    "WorldBank MRNEV source response omitted an observation period; "
                    "data point date uses source lastupdated"
                )

            results.append(
                NormalizedData(
                    metadata=Metadata(
                        source="World Bank",
                        indicator=indicator_label,
                        country=str(bucket.get("country_name") or bucket.get("country_id") or ""),
                        frequency=str(bucket.get("frequency") or "annual"),
                        unit=unit,
                        lastUpdated=last_updated,
                        seriesId=series_id,
                        apiUrl=api_url,
                        sourceUrl=source_url,
                        seasonalAdjustment=None,
                        dataType="Level",
                        priceType="Nominal (current prices)" if unit == "USD" else None,
                        description=f"{source_name}: {indicator_label}",
                        notes=notes,
                        startDate=points[0]["date"],
                        endDate=points[-1]["date"],
                    ),
                    data=points,
                )
            )

        if results:
            logger.info(
                "WorldBank source endpoint returned %d series for source=%s indicator=%s",
                len(results),
                source_id,
                indicator_code,
            )
        return results

    @staticmethod
    def _compose_source_url(base_url: str, params: Dict[str, Any]) -> str:
        if not params:
            return base_url
        from urllib.parse import urlencode

        return f"{base_url}?{urlencode(params)}"

    # Reverse mapping: sets of ISO2 country codes that correspond to
    # WorldBank aggregate region codes.  When the query service expands
    # a region like "Sub-Saharan Africa" into individual countries, we
    # can detect this and use the aggregate code instead — one API call
    # returning an aggregate statistic instead of 20+ individual calls.
    _REGION_COUNTRY_SETS: Dict[str, frozenset] = {
        "SSF": frozenset({
            "AO", "BW", "CM", "CI", "CD", "ET", "GH", "KE", "MG", "MW",
            "ML", "MZ", "NA", "NE", "NG", "RW", "SN", "ZA", "TZ", "UG", "ZM", "ZW",
        }),
    }

    async def fetch_indicator(
        self,
        indicator: str,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        _skip_alternatives: bool = False,  # Internal flag to prevent recursion
        _allow_semantic_alternatives: bool = False,
        _defaulted_all_country: bool = False,
    ) -> List[NormalizedData]:
        # Circuit breaker: skip WB entirely when API is confirmed down
        if not _wb_is_available():
            raise DataNotAvailableError(
                f"WorldBank API is temporarily unavailable (circuit breaker open). "
                f"Try again in {_WB_CIRCUIT_COOLDOWN_S // 60} minutes."
            )
        catalog_source_id = self._indicator_source_id(indicator)
        resolved_from_semantic_adjudication = bool(_allow_semantic_alternatives)
        exact_indicator_request = bool(
            (self._looks_like_worldbank_indicator_code(indicator) or catalog_source_id)
            and not resolved_from_semantic_adjudication
        )
        indic = await self._resolve_indicator_code(indicator)
        semantic_resolved_request = bool(
            _allow_semantic_alternatives
            and catalog_source_id
            and str(indic).strip() == str(indicator or "").strip()
        )
        if semantic_resolved_request:
            # A catalog-backed code that came from upstream LLM/metadata
            # adjudication is provider-native, but it is not literal user input.
            # If that exact code returns no data, allow the normal executable
            # alternative path to try same-title public WDI rows instead of
            # fail-closing on a non-executable source-specific code.
            exact_indicator_request = False
        if not catalog_source_id or str(indic).strip() != str(indicator or "").strip():
            catalog_source_id = self._indicator_source_id(indic)
        defaulted_no_country_all = bool(_defaulted_all_country or (not country and not countries))
        country_list = countries or ([country] if country else ["all"])

        # Detect when the country list represents a known WB aggregate region.
        # When the query service has pre-expanded "Sub-Saharan Africa" into
        # individual ISO2 codes, use the WB region code instead for efficiency
        # and to get the proper aggregate statistic.
        if len(country_list) >= 5:
            country_set = frozenset(c.upper() for c in country_list)
            for region_code, region_members in self._REGION_COUNTRY_SETS.items():
                # Check if the country set is a subset of the region members
                # (allows partial matches when query service uses a subset)
                if country_set <= region_members or region_members <= country_set:
                    overlap = len(country_set & region_members)
                    if overlap >= min(len(region_members), len(country_set)) * 0.7:
                        logger.info(
                            "🌍 Detected region aggregate: %d/%d countries match %s — using region code",
                            overlap, len(country_set), region_code,
                        )
                        country_list = [region_code]
                        break

        # Expand country groups (e.g., "G7" → ["USA", "GBR", "FRA", ...])
        # ALWAYS expand groups to individual countries - region codes often fail
        expanded_countries: List[str] = []
        for country_item in country_list:
            # First, try explicit country group expansion
            group_expansion = self._expand_country_group(country_item)
            if group_expansion:
                expanded_countries.extend(group_expansion)
                continue

            # Check if this is a regional term that should map to a region code
            region_code = self._map_regional_term(country_item)
            if region_code:
                # Region codes often fail for many indicators (e.g., AFR doesn't work for population)
                # So we try region code first, but fall back to expanding it if possible
                # For now, use the region code - individual queries will handle failures
                expanded_countries.append(region_code)
                continue

            # Otherwise, use country as-is (might be ISO code or country name)
            expanded_countries.append(country_item)

        country_list = expanded_countries
        results: List[NormalizedData] = []

        # Add proper headers to avoid rate limiting and blocking
        headers = {
            "User-Agent": "openecon-data/1.0 (https://openecon.ai; economic-data-aggregator)",
            "Accept": "application/json",
        }

        # Use a shared HTTP/1.1 client. WorldBank's API has repeatedly hung on
        # otherwise-fast indicator endpoints over httpx HTTP/2 while the same
        # endpoints return quickly with HTTP/1.1 (curl --http1.1 and httpx
        # http2=False). Keep this provider off the shared HTTP/2 pool.
        client = get_http1_client()

        # Batch multi-country requests using WorldBank's semicolon-separated
        # country codes: /country/USA;GBR;FRA/indicator/X — single API call
        # instead of N sequential calls.  Dramatically faster for G7/BRICS/etc.
        resolved_codes = {}
        for raw in country_list:
            code = self._country_code(raw)
            resolved_codes[code] = raw  # Map resolved → original for metadata

        batch_codes = ";".join(resolved_codes.keys())
        url = f"{self.base_url}/country/{batch_codes}/indicator/{indic}"
        prefer_parallel_small_group = 1 < len(country_list) <= 3

        date_param = None
        if start_date and end_date:
            date_param = f"{start_date[:4]}:{end_date[:4]}"

        # Scale per_page based on number of countries to avoid pagination
        # cutting off countries (e.g., G20 × 65 years = 1,235 records > 1,000).
        # WorldBank allows up to 32,500 per page.
        per_page = 10000 if "all" in resolved_codes else max(1000, len(country_list) * 100)
        params = {"format": "json", "per_page": min(per_page, 10000)}
        if date_param:
            params["date"] = date_param
        elif exact_indicator_request and "all" in resolved_codes:
            # Official WorldBank API v2 supports MRNEV ("most recent non-empty
            # values").  For exact no-country/no-date requests, fetching every
            # historical all-country observation can require multi-page 17k+
            # record responses and time out before any answer is returned.  A
            # real user asking an exact indicator with no date/country needs a
            # current answerable slice, not exhaustive archival replay.
            params["MRNEV"] = 1

        # Track total fetch time to enforce a time budget for the entire operation.
        # This prevents cascading timeouts (batch + fallback + alternatives)
        # from pushing the total past 60s.  The budget must be generous enough
        # that the primary batch request (25s) can complete — the WB API is
        # notoriously slow, especially over HTTP/2.
        import time as _time
        _fetch_start = _time.perf_counter()
        _FETCH_BUDGET_S = effective_timeout(30.0)  # Total time budget for all WB API calls

        # Single batched request for all countries (with 502 retry — WB API is intermittent)
        logger.info(f"WorldBank API call: {url} | params={params} | countries={len(country_list)}")
        payload = None
        batch_response = None  # Track response for metadata (e.g. Date header)
        api_error_detail = None
        transport_failure_seen = False
        if prefer_parallel_small_group:
            logger.info(
                "WorldBank: skipping batched multi-country request for small group (%d countries); "
                "using per-country parallel fetch for better reliability",
                len(country_list),
            )
        else:
            try:
                for _attempt in range(3):
                    batch_response = await client.get(url, params=params, headers=headers, timeout=effective_timeout(25.0))
                    logger.info(f"WorldBank API response: status={batch_response.status_code} (attempt {_attempt+1})")
                    if batch_response.status_code != 502:
                        break
                    transport_failure_seen = True
                    logger.warning(f"WorldBank 502 Bad Gateway (attempt {_attempt+1}/3), retrying...")
                    await asyncio.sleep(1.0)
                batch_response.raise_for_status()
                payload = batch_response.json()

                if isinstance(payload, list) and len(payload) > 0:
                    if isinstance(payload[0], dict) and "message" in payload[0]:
                        error_msg = payload[0]["message"]
                        if isinstance(error_msg, list) and len(error_msg) > 0:
                            error_detail = error_msg[0].get("value", "Unknown error")
                            api_error_detail = str(error_detail)
                            logger.warning(f"World Bank API error: {error_detail}")
                            payload = None

                if payload and (len(payload) < 2 or not payload[1]):
                    logger.debug(f"No data for {batch_codes} indicator {indic}")
                    payload = None

                # Detect pagination truncation.  Multi-country and all-country
                # WorldBank responses can exceed a single page.  Pull the
                # remaining pages when bounded; otherwise fall back to
                # per-country fetches for explicit country groups.
                if payload and isinstance(payload[0], dict):
                    total_pages = int(payload[0].get("pages", 1) or 1)
                    if total_pages > 1:
                        total_records = int(payload[0].get("total", 0) or 0)
                        returned = len(payload[1]) if len(payload) > 1 and payload[1] else 0
                        if total_pages <= 10:
                            logger.info(
                                "WorldBank paginated response: got %d/%d records "
                                "(page 1/%d). Fetching remaining pages.",
                                returned,
                                total_records,
                                total_pages,
                            )
                            all_records = list(payload[1] or [])
                            for page_num in range(2, total_pages + 1):
                                page_params = {**params, "page": page_num}
                                page_response = await self._get_with_retry(
                                    client,
                                    url,
                                    params=page_params,
                                    headers=headers,
                                    timeout=effective_timeout(25.0),
                                )
                                page_payload = page_response.json()
                                if (
                                    isinstance(page_payload, list)
                                    and len(page_payload) >= 2
                                    and page_payload[1]
                                ):
                                    all_records.extend(page_payload[1])
                            if total_records and len(all_records) < int(total_records):
                                logger.warning(
                                    "WorldBank pagination incomplete: got %d/%d records",
                                    len(all_records),
                                    total_records,
                                )
                                payload = None
                            else:
                                payload = [payload[0], all_records]
                        else:
                            logger.warning(
                                f"WorldBank pagination truncation: got {returned}/{total_records} records "
                                f"(page 1/{total_pages}). Falling back to sequential fetch."
                            )
                            payload = None  # Force fallback to per-country sequential fetch
            except httpx.HTTPError as e:
                logger.warning(f"HTTP error fetching batched data for {batch_codes}: {e}")
                transport_failure_seen = True
                payload = None
            except Exception as e:
                logger.warning(f"Error fetching batched data: {e}")
                payload = None

        if api_error_detail and exact_indicator_request:
            source_id = catalog_source_id or self._indicator_source_id(indic)
            if source_id and source_id != "2":
                source_results = await self._fetch_source_series_endpoint(
                    source_id=source_id,
                    indicator=indic,
                    country_codes=list(resolved_codes.keys()),
                    start_date=start_date,
                    end_date=end_date,
                    headers=headers,
                    client=client,
                )
                if source_results:
                    _wb_record_success()
                    return source_results
            raise DataNotAvailableError(
                f"WorldBank exact indicator code '{indic}' is not available from the public data endpoint: "
                f"{api_error_detail}"
            )

        # If batch request failed, fall back to parallel per-country fetch.
        # Accumulate ALL country records into a single payload so the
        # batch processing loop below handles all countries together.
        # Skip fallback if time budget already exceeded (avoids cascading timeouts).
        _elapsed_so_far = _time.perf_counter() - _fetch_start
        skip_duplicate_country_fallback = (
            len(country_list) == 1
            and self._country_code(str(country_list[0])) == "all"
        )
        if (
            len(country_list) == 1
            and not exact_indicator_request
            and not prefer_parallel_small_group
        ):
            # For a natural-label single-country request, the "batch" URL and
            # per-country fallback URL are identical.  Do not spend the whole
            # budget retrying the same unavailable primary indicator before
            # the executable-alternative path can run.
            skip_duplicate_country_fallback = True

        if not payload and not skip_duplicate_country_fallback and _elapsed_so_far < _FETCH_BUDGET_S:
            remaining_budget = _FETCH_BUDGET_S - _elapsed_so_far
            accumulated_records = []
            fallback_meta = None
            wb_sem = asyncio.Semaphore(5)

            async def _fetch_single_country(country_code_raw: str):
                async with wb_sem:
                    try:
                        country_code = self._country_code(country_code_raw)
                        single_url = f"{self.base_url}/country/{country_code}/indicator/{indic}"
                        response = await self._get_with_retry(client, single_url, params=params, headers=headers, timeout=effective_timeout(15.0))
                        single_payload = response.json()
                        if isinstance(single_payload, list) and len(single_payload) >= 2 and single_payload[1]:
                            return single_payload
                    except Exception as e:
                        logger.warning(f"Error fetching {country_code_raw}: {e}. Skipping.")
                    return None

            try:
                fallback_results = await asyncio.wait_for(
                    asyncio.gather(
                        *[_fetch_single_country(c) for c in country_list],
                        return_exceptions=True,
                    ),
                    timeout=remaining_budget,
                )
                for fr in fallback_results:
                    if isinstance(fr, list) and len(fr) >= 2 and fr[1]:
                        accumulated_records.extend(fr[1])
                        if not fallback_meta:
                            fallback_meta = fr[0]
                if accumulated_records and fallback_meta:
                    payload = [fallback_meta, accumulated_records]
            except asyncio.TimeoutError:
                logger.warning(
                    "WorldBank per-country fallback timed out after %.1fs",
                    _time.perf_counter() - _fetch_start,
                )
        elif not payload and skip_duplicate_country_fallback:
            logger.info("WorldBank skipping duplicate per-country fallback: primary request already attempted")
        elif not payload:
            logger.info(
                "WorldBank skipping per-country fallback: time budget exceeded (%.1fs)",
                _elapsed_so_far,
            )

        # Provider-native exact-title/code requests with no user country first
        # use WorldBank's documented all-country surface.  Some WDI indicators
        # expose no non-null data (or intermittently fail) on /country/all
        # while the provider-native World aggregate does return a valid current
        # observation.  Retry that aggregate only for defaulted no-country exact
        # requests; explicit country/all-country requests remain strict.
        if (
            not payload
            and exact_indicator_request
            and defaulted_no_country_all
            and (not catalog_source_id or catalog_source_id == "2")
            and len(country_list) == 1
            and self._country_code(str(country_list[0])) == "all"
            and (_time.perf_counter() - _fetch_start) < _FETCH_BUDGET_S
        ):
            world_code = "WLD"
            world_url = f"{self.base_url}/country/{world_code}/indicator/{indic}"
            world_params = dict(params)
            if not date_param:
                world_params["MRNEV"] = 1
            try:
                logger.info(
                    "WorldBank exact no-country all surface returned no data; "
                    "trying World aggregate for indicator %s",
                    indic,
                )
                world_response = await self._get_with_retry(
                    client,
                    world_url,
                    params=world_params,
                    headers=headers,
                    timeout=effective_timeout(15.0),
                )
                world_payload = world_response.json()
                if (
                    isinstance(world_payload, list)
                    and len(world_payload) >= 2
                    and world_payload[1]
                ):
                    payload = world_payload
                    batch_response = world_response
            except Exception as exc:
                logger.debug(
                    "WorldBank World aggregate retry failed for exact no-country %s: %s",
                    indic,
                    exc,
                )

        # Process batched payload — group records by country
        # NOTE: Do NOT return early here — fall through to alternative indicator
        # fallback logic below when payload is empty/missing. Early return would
        # bypass the infrastructure that tries alternative indicators.
        all_records = []
        if payload and len(payload) >= 2 and payload[1]:
            all_records = payload[1]

        # Group records by country code
        from collections import defaultdict
        by_country: dict[str, list] = defaultdict(list)
        for record in all_records:
            if isinstance(record, dict):
                cc = record.get("countryiso3code") or record.get("country", {}).get("id", "")
                by_country[cc].append(record)

        for country_code_key, records in by_country.items():
            if not records:
                continue
            first_record = records[0]
            if not first_record or not isinstance(first_record, dict):
                continue
            indicator_name = first_record.get("indicator", {}).get("value", indic)
            country_name = first_record.get("country", {}).get("value", country_code_key)
            country_code = country_code_key

            api_url = f"{self.base_url}/country/{country_code}/indicator/{indic}?format=json&per_page=1000"
            if date_param:
                api_url += f"&date={date_param}"

            # Extract unit from indicator name (e.g., "GDP per capita, PPP (current international $)" → "current international $")
            unit = ""
            if "(" in indicator_name and ")" in indicator_name:
                unit = indicator_name[indicator_name.rfind("(")+1:indicator_name.rfind(")")]
            # Fallback: if no parentheses, check for common unit patterns
            elif "%" in indicator_name or "percent" in indicator_name.lower():
                unit = "%"
            elif "$" in indicator_name or "dollars" in indicator_name.lower():
                unit = "USD"

            # Human-readable URL for data verification on World Bank website
            source_url = f"https://data.worldbank.org/indicator/{indic}?locations={country_code}"

            # Determine data type from indicator name
            data_type = None
            indicator_lower = indicator_name.lower()
            if "growth" in indicator_lower or "% change" in indicator_lower:
                data_type = "Percent Change"
            elif "%" in indicator_name or "percent" in indicator_lower or "ratio" in indicator_lower:
                data_type = "Rate"
            elif "index" in indicator_lower:
                data_type = "Index"
            else:
                data_type = "Level"

            # Determine price type from indicator name
            price_type = None
            if "constant" in indicator_lower or "real" in indicator_lower:
                price_type = "Real (constant prices)"
            elif "current" in indicator_lower or "nominal" in indicator_lower:
                price_type = "Nominal (current prices)"
            elif "ppp" in indicator_lower:
                price_type = "PPP (purchasing power parity)"

            # Extract data range from records (safe access pattern)
            data_list = [
                {"date": f"{entry.get('date', 'unknown')}-01-01", "value": entry.get("value")}
                for entry in reversed(records)
                if isinstance(entry, dict) and entry.get("value") is not None and entry.get("date")
            ]

            # Skip countries/regions with no actual data values
            if not data_list:
                logger.debug(f"No data values for {country_code_key} ({country_code}) indicator {indic} - all values null")
                continue

            # These are safe now due to the guard clause above
            start_date_val = data_list[0]["date"]
            end_date_val = data_list[-1]["date"]

            normalized = NormalizedData(
                metadata=Metadata(
                    source="World Bank",
                    indicator=indicator_name,
                    country=country_name,
                    frequency="annual",
                    unit=unit,
                    lastUpdated=batch_response.headers.get("Date", "") if batch_response else "",
                    seriesId=indic,  # Add seriesId with indicator code
                    apiUrl=api_url,
                    sourceUrl=source_url,
                    # Enhanced metadata fields
                    seasonalAdjustment=None,  # World Bank data is typically not seasonally adjusted (annual)
                    dataType=data_type,
                    priceType=price_type,
                    description=indicator_name,
                    notes=None,
                    startDate=start_date_val,
                    endDate=end_date_val,
                ),
                data=data_list,
            )
            results.append(normalized)

        # If we got results, record success and return.
        if results:
            _wb_record_success()
            return results

        # No results — try fallbacks in order of priority.
        _results_elapsed = _time.perf_counter() - _fetch_start

        # 0. Exact catalog-backed non-WDI source endpoint.  Some public
        # WorldBank sources return empty/deleted from the generic
        # /country/{country}/indicator/{code} endpoint but serve the same
        # provider-native code through /sources/{source}/country/{country}/series/{code}.
        # This uses only source id metadata already stored with the exact
        # catalog code; it does not infer a semantic replacement indicator.
        if exact_indicator_request:
            source_id = catalog_source_id or self._indicator_source_id(indic)
            if source_id and source_id != "2":
                try:
                    source_results = await self._fetch_source_series_endpoint(
                        source_id=source_id,
                        indicator=indic,
                        country_codes=list(resolved_codes.keys()),
                        start_date=start_date,
                        end_date=end_date,
                        headers=headers,
                        client=client,
                    )
                except Exception as exc:
                    logger.warning(
                        "WorldBank source endpoint fallback failed for source=%s indicator=%s: %s",
                        source_id,
                        indic,
                        exc,
                    )
                    source_results = []
                if source_results:
                    _wb_record_success()
                    return source_results

        # 1. Income aggregate fallback (only if time budget allows)
        if _results_elapsed < _FETCH_BUDGET_S:
            income_aggregates_tried = [c for c in country_list if c in self.INCOME_AGGREGATE_FALLBACKS]

            if income_aggregates_tried:
                logger.info(f"⚠️ Income aggregate(s) {income_aggregates_tried} returned no data for {indic}. Trying geographic region fallbacks...")

                fallback_regions = set()
                for agg in income_aggregates_tried:
                    fallback_regions.update(self.INCOME_AGGREGATE_FALLBACKS[agg])

                fallback_results = []
                for region in fallback_regions:
                    try:
                        region_data = await self.fetch_indicator(
                            indicator=indic,
                            country=region,
                            start_date=start_date,
                            end_date=end_date,
                        )
                        fallback_results.extend(region_data)
                    except DataNotAvailableError:
                        logger.debug(f"Fallback region {region} also has no data for {indic}")
                        continue
                    except Exception as e:
                        logger.debug(f"Error fetching fallback region {region}: {e}")
                        continue

                if fallback_results:
                    logger.info(f"✅ Income aggregate fallback succeeded: got data from {len(fallback_results)} geographic regions")
                    return fallback_results

        # 2. Alternative indicators (only if time budget allows and not already tried)
        _alt_elapsed = _time.perf_counter() - _fetch_start
        if not _skip_alternatives and not exact_indicator_request and _alt_elapsed < _FETCH_BUDGET_S:
            alternatives = await self._get_alternative_indicators(indicator, indic, limit=3)
            if alternatives:
                logger.info(f"⚠️ Primary indicator {indic} failed. Trying {len(alternatives)} alternatives: {alternatives}")
                for alt_code in alternatives:
                    try:
                        alt_results = await self.fetch_indicator(
                            indicator=alt_code,
                            countries=country_list,
                            start_date=start_date,
                            end_date=end_date,
                            _skip_alternatives=True,
                        )
                        if alt_results:
                            logger.info(f"✅ Alternative indicator succeeded: {alt_code}")
                            return alt_results
                    except DataNotAvailableError:
                        logger.debug(f"Alternative indicator {alt_code} also has no data")
                        continue
                    except Exception as e:
                        logger.debug(f"Error with alternative indicator {alt_code}: {e}")
                        continue

        # All paths exhausted — ALWAYS raise so the query service knows WB
        # failed and can attempt cross-provider fallback (IMF, Eurostat, etc.).
        # Previously, when the time budget was exceeded, this returned an empty
        # list which the query service treated as "no data" rather than an error,
        # silently skipping the WB provider without triggering fallback chains.
        _total_elapsed = _time.perf_counter() - _fetch_start
        if transport_failure_seen:
            _wb_record_failure()
        logger.warning(
            "WorldBank fetch failed for indicator %s after %.1fs (budget=%.0fs)",
            indic, _total_elapsed, _FETCH_BUDGET_S,
        )
        raise DataNotAvailableError(
            f"No data found for any of the requested countries for indicator {indic}. "
            f"The data may not be available for the specified countries or indicator."
        )

    async def _resolve_indicator_code(self, indicator: str) -> str:
        """Resolve WorldBank indicator code without legacy semantic shortcuts.

        Resolution priority:
        1. Pre-resolved codes (contain dots, e.g., NY.GDP.MKTP.CD) -- instant
        2. Exact provider-native title match from local metadata -- mechanical
        3. Metadata search (SDMX + WB REST API + LLM) -- bounded network I/O

        Retired universal semantic shortcut modules are not used as final
        semantic authority here.
        """
        # Short-circuit: if indicator is already a valid WorldBank code
        # (dotted WDI forms and public REST codes with underscores/digits),
        # return it directly without re-running semantic selection.
        # This prevents double-resolution where an already-correct code
        # gets re-resolved to a different (wrong) indicator.
        if self._looks_like_worldbank_indicator_code(indicator):
            logger.info(f"🔒 WorldBank: Using pre-resolved indicator code: {indicator}")
            return str(indicator).strip()

        # Some WorldBank public sources use short exact catalog codes (for
        # example GEM `TOT`) that do not contain dots, underscores, or digits.
        # If the local catalog has an exact code record, keep it mechanically
        # instead of sending the token through semantic metadata search.
        if self._indicator_source_id(indicator):
            logger.info("🔒 WorldBank: Using exact catalog-backed indicator code: %s", indicator)
            return str(indicator).strip()

        exact_title_text = str(indicator or "").strip()
        if exact_title_text:
            try:
                from ..services.indicator_resolution import find_exact_provider_title_match

                exact_match = find_exact_provider_title_match(exact_title_text, "WorldBank")
            except Exception as exc:
                logger.debug("WorldBank exact-title lookup skipped for '%s': %s", indicator, exc)
                exact_match = None

            if exact_match and exact_match.get("code"):
                code = str(exact_match["code"]).strip()
                logger.info("🔒 WorldBank: Using exact local indicator code '%s' from provider-title match", code)
                return code

        # Allow users to supply raw WorldBank indicator codes directly
        if indicator and "." in indicator:
            return indicator

        if not self.metadata_search:
            raise DataNotAvailableError(
                f"WorldBank indicator '{indicator}' not recognized. Provide the official indicator code (e.g., NY.GDP.MKTP.CD) or enable metadata discovery."
            )

        # Use hierarchical search: SDMX first, then WorldBank REST API.
        # Cap total metadata search time to 15s to prevent 60s+ hangs when
        # the WB indicator API is slow. The upstream pipeline
        # (resolve_indicator_for_fetch) should have already resolved most
        # indicators via exact passthrough or IndicatorSelector.
        import time as _time
        _meta_start = _time.perf_counter()
        try:
            search_results = await asyncio.wait_for(
                self.metadata_search.search_with_sdmx_fallback(
                    provider="WorldBank",
                    indicator=indicator,
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            _meta_elapsed = _time.perf_counter() - _meta_start
            logger.warning(
                "WorldBank metadata search timed out after %.1fs for '%s'",
                _meta_elapsed, indicator,
            )
            raise DataNotAvailableError(
                f"WorldBank indicator '{indicator}' search timed out. "
                f"Try providing the official indicator code (e.g., NY.GDP.MKTP.CD)."
            )

        _meta_elapsed = _time.perf_counter() - _meta_start
        logger.info(
            "WorldBank metadata search completed in %.1fs for '%s' (%d results)",
            _meta_elapsed, indicator, len(search_results) if search_results else 0,
        )

        if not search_results:
            raise DataNotAvailableError(
                f"WorldBank indicator '{indicator}' not found. Try another description or provide the official indicator code."
            )

        discovery = await self.metadata_search.discover_indicator(
            provider="WorldBank",
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
            return code

        raise DataNotAvailableError(
            f"WorldBank indicator '{indicator}' not found. Try refining your query or use a known indicator name like GDP or Unemployment."
        )
