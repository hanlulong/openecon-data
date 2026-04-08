from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, TYPE_CHECKING
import logging

import httpx

from ..config import get_settings
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from ..services.http_pool import get_http_client, effective_timeout
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
            from ..services.indicator_lookup import get_indicator_lookup
            lookup = get_indicator_lookup()
            results = lookup.search(indicator, provider='WorldBank', limit=limit + 1)

            # Return alternative codes, excluding the primary one we already tried
            alternatives = []
            for r in results:
                code = r.get('code')
                if code and code != primary_code and code not in alternatives:
                    alternatives.append(code)
                    if len(alternatives) >= limit:
                        break
            return alternatives
        except Exception as e:
            logger.debug(f"Could not get alternative indicators: {e}")
            return []

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
    ) -> List[NormalizedData]:
        # Circuit breaker: skip WB entirely when API is confirmed down
        if not _wb_is_available():
            raise DataNotAvailableError(
                f"WorldBank API is temporarily unavailable (circuit breaker open). "
                f"Try again in {_WB_CIRCUIT_COOLDOWN_S // 60} minutes."
            )
        indic = await self._resolve_indicator_code(indicator)
        country_list = countries or [country or "USA"]

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

        # Use shared HTTP client pool for better performance
        client = get_http_client()

        # Batch multi-country requests using WorldBank's semicolon-separated
        # country codes: /country/USA;GBR;FRA/indicator/X — single API call
        # instead of N sequential calls.  Dramatically faster for G7/BRICS/etc.
        resolved_codes = {}
        for raw in country_list:
            code = self._country_code(raw)
            resolved_codes[code] = raw  # Map resolved → original for metadata

        batch_codes = ";".join(resolved_codes.keys())
        url = f"{self.base_url}/country/{batch_codes}/indicator/{indic}"

        date_param = None
        if start_date and end_date:
            date_param = f"{start_date[:4]}:{end_date[:4]}"

        # Scale per_page based on number of countries to avoid pagination
        # cutting off countries (e.g., G20 × 65 years = 1,235 records > 1,000).
        # WorldBank allows up to 32,500 per page.
        per_page = max(1000, len(country_list) * 100)
        params = {"format": "json", "per_page": min(per_page, 10000)}
        if date_param:
            params["date"] = date_param

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
        try:
            for _attempt in range(3):
                batch_response = await client.get(url, params=params, headers=headers, timeout=effective_timeout(25.0))
                logger.info(f"WorldBank API response: status={batch_response.status_code} (attempt {_attempt+1})")
                if batch_response.status_code != 502:
                    break
                logger.warning(f"WorldBank 502 Bad Gateway (attempt {_attempt+1}/3), retrying...")
                await asyncio.sleep(1.0)
            batch_response.raise_for_status()
            payload = batch_response.json()

            if isinstance(payload, list) and len(payload) > 0:
                if isinstance(payload[0], dict) and "message" in payload[0]:
                    error_msg = payload[0]["message"]
                    if isinstance(error_msg, list) and len(error_msg) > 0:
                        error_detail = error_msg[0].get("value", "Unknown error")
                        logger.warning(f"World Bank API error: {error_detail}")
                        payload = None

            if payload and (len(payload) < 2 or not payload[1]):
                logger.debug(f"No data for {batch_codes} indicator {indic}")
                payload = None

            # Detect pagination truncation: if API says there are more pages,
            # the batch response is incomplete — fall back to sequential fetch.
            if payload and isinstance(payload[0], dict):
                total_pages = payload[0].get("pages", 1)
                if total_pages > 1:
                    total_records = payload[0].get("total", 0)
                    returned = len(payload[1]) if len(payload) > 1 and payload[1] else 0
                    logger.warning(
                        f"WorldBank pagination truncation: got {returned}/{total_records} records "
                        f"(page 1/{total_pages}). Falling back to sequential fetch."
                    )
                    payload = None  # Force fallback to per-country sequential fetch
        except httpx.HTTPError as e:
            logger.warning(f"HTTP error fetching batched data for {batch_codes}: {e}")
            payload = None
        except Exception as e:
            logger.warning(f"Error fetching batched data: {e}")
            payload = None

        # If batch request failed, fall back to parallel per-country fetch.
        # Accumulate ALL country records into a single payload so the
        # batch processing loop below handles all countries together.
        # Skip fallback if time budget already exceeded (avoids cascading timeouts).
        _elapsed_so_far = _time.perf_counter() - _fetch_start
        if not payload and _elapsed_so_far < _FETCH_BUDGET_S:
            remaining_budget = _FETCH_BUDGET_S - _elapsed_so_far
            accumulated_records = []
            fallback_meta = None
            wb_sem = asyncio.Semaphore(5)

            async def _fetch_single_country(country_code_raw: str):
                async with wb_sem:
                    try:
                        country_code = self._country_code(country_code_raw)
                        single_url = f"{self.base_url}/country/{country_code}/indicator/{indic}"
                        response = await client.get(single_url, params=params, headers=headers, timeout=effective_timeout(15.0))
                        response.raise_for_status()
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
        elif not payload:
            logger.info(
                "WorldBank skipping per-country fallback: time budget exceeded (%.1fs)",
                _elapsed_so_far,
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
        if not _skip_alternatives and _alt_elapsed < _FETCH_BUDGET_S:
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
        """Resolve WorldBank indicator code through IndicatorResolver (unified) or metadata search.

        Resolution priority:
        1. Pre-resolved codes (contain dots, e.g., NY.GDP.MKTP.CD) -- instant
        2. IndicatorResolver (FTS5 + catalog + translator) -- fast, local
        3. Metadata search (SDMX + WB REST API + LLM) -- slow, network I/O
           Only used as last resort with a 15s timeout cap.
        """
        # Short-circuit: if indicator is already a valid WorldBank code
        # (contains dots like "NY.GDP.MKTP.CD" or "NV.IND.TOTL.KD.ZG"),
        # return it directly without re-resolving through the resolver.
        # This prevents double-resolution where an already-correct code
        # gets re-resolved to a different (wrong) indicator.
        if indicator and "." in indicator and indicator[0].isalpha():
            logger.info(f"🔒 WorldBank: Using pre-resolved indicator code: {indicator}")
            return indicator

        # Use IndicatorResolver as the unified first attempt
        # This consolidates FTS5 search, translator, and catalog into one service
        try:
            from ..services.indicator_resolver import get_indicator_resolver
            resolver = get_indicator_resolver()
            resolved = resolver.resolve(indicator, provider="WorldBank")
            if resolved and resolved.confidence >= 0.7:
                logger.info(f"🔍 IndicatorResolver: WorldBank '{indicator}' → '{resolved.code}' (confidence: {resolved.confidence:.2f}, source: {resolved.source})")
                return resolved.code
        except Exception as e:
            logger.debug(f"IndicatorResolver failed, falling back: {e}")

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
        # indicators via the catalog or IndicatorSelector.
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
