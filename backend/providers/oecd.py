from __future__ import annotations

import asyncio
import logging
import json
from typing import Dict, List, Optional, TYPE_CHECKING
from pathlib import Path

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError, retry_async
from ..services.dsd_cache import get_dimension_key_builder
from ..services.cache import cache_service
from ..services.rate_limiter import (
    wait_for_provider,
    record_provider_request,
    record_provider_rate_limit_error,
    record_provider_success,
    is_provider_circuit_open,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.metadata_search import MetadataSearchService


class OECDProvider:
    """OECD Statistics API provider for international economic data.

    Uses SDMX-JSON format. No API key required.
    Documentation: https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html

    Dynamic metadata discovery:
    - Loads OECD dataflows catalog from disk
    - Maps indicators to correct dataflows via metadata search
    - No hardcoded mappings for country/indicator combinations
    - Supports ALL OECD member countries dynamically
    """

    # Core economic indicators with known dataflows (for performance optimization)
    # When metadata search returns these, we know the correct agency/structure
    KNOWN_INDICATORS: Dict[str, Dict[str, str]] = {
        "GDP": {"keywords": ["GDP", "gross domestic product"]},
        "UNEMPLOYMENT": {"keywords": ["unemployment", "jobless rate", "LFS"]},
        "INFLATION": {"keywords": ["inflation", "CPI", "consumer price index", "price"]},
        "EXPORTS": {"keywords": ["exports", "goods exported"]},
        "IMPORTS": {"keywords": ["imports", "goods imported"]},
        # Tax and revenue indicators - HIGH PRIORITY
        "TAX_REVENUE": {"keywords": ["tax revenue", "tax receipts", "taxation", "revenue statistics"]},
        "GINI": {"keywords": ["gini", "income inequality", "inequality index"]},
        "EDUCATION": {"keywords": ["education", "educational attainment", "education spending"]},
        "HEALTH": {"keywords": ["health expenditure", "health spending", "healthcare"]},
        "R&D": {"keywords": ["r&d", "research and development", "R&D expenditure"]},
        "PRODUCTIVITY": {"keywords": ["productivity", "labor productivity", "productivity growth"]},
        "PENSION": {"keywords": ["pension", "pension spending", "retirement"]},
        # Labor market indicators - HIGH PRIORITY (DSD_HW dataflow)
        "WORKING_HOURS": {"keywords": ["working hours", "hours worked", "annual hours", "weekly hours", "work hours"]},
        "PART_TIME": {"keywords": ["part-time", "part time", "part-time employment"]},
        "FULL_TIME": {"keywords": ["full-time", "full time", "full-time employment"]},
        "LABOR_FORCE": {"keywords": ["labor force", "labour force", "workforce", "employed population"]},
    }

    # Cached dataflows catalog (loaded once per process)
    _DATAFLOWS_CATALOG: Optional[Dict] = None

    # OECD member countries (38 members as of 2024)
    # Ordered list for "all OECD countries" queries
    OECD_MEMBER_COUNTRIES: List[str] = [
        "USA", "DEU", "FRA", "GBR", "JPN", "ITA", "CAN", "ESP", "AUS", "KOR",
        "MEX", "NLD", "BEL", "AUT", "SWE", "NOR", "DNK", "FIN", "CHE", "POL",
        "PRT", "GRC", "CZE", "HUN", "NZL", "TUR", "CHL", "ISR", "ISL", "IRL",
        "LUX", "SVN", "SVK", "EST", "LVA", "LTU", "COL", "CRI"
    ]

    # Country code mapping (ISO 3166-1 alpha-3)
    COUNTRY_MAPPINGS: Dict[str, str] = {
        # Major economies
        "UNITED_STATES": "USA",
        "US": "USA",
        "GERMANY": "DEU",
        "DE": "DEU",
        "FRANCE": "FRA",
        "FR": "FRA",
        "UNITED_KINGDOM": "GBR",
        "UK": "GBR",
        "GB": "GBR",
        "JAPAN": "JPN",
        "JP": "JPN",
        "ITALY": "ITA",
        "IT": "ITA",
        "CANADA": "CAN",
        "CA": "CAN",
        "SPAIN": "ESP",
        "ES": "ESP",
        "AUSTRALIA": "AUS",
        "AU": "AUS",
        "SOUTH_KOREA": "KOR",
        "KOREA": "KOR",
        "KR": "KOR",
        "MEXICO": "MEX",
        "MX": "MEX",

        # Additional OECD members
        "NETHERLANDS": "NLD",
        "NL": "NLD",
        "BELGIUM": "BEL",
        "BE": "BEL",
        "AUSTRIA": "AUT",
        "AT": "AUT",
        "SWEDEN": "SWE",
        "SE": "SWE",
        "NORWAY": "NOR",
        "NO": "NOR",
        "DENMARK": "DNK",
        "DK": "DNK",
        "FINLAND": "FIN",
        "FI": "FIN",
        "SWITZERLAND": "CHE",
        "CH": "CHE",
        "POLAND": "POL",
        "PL": "POL",
        "PORTUGAL": "PRT",
        "PT": "PRT",
        "GREECE": "GRC",
        "GR": "GRC",
        "CZECH_REPUBLIC": "CZE",
        "CZ": "CZE",
        "HUNGARY": "HUN",
        "HU": "HUN",
        "NEW_ZEALAND": "NZL",
        "NZ": "NZL",
        "TURKEY": "TUR",
        "TR": "TUR",
        "CHILE": "CHL",
        "CL": "CHL",
        "ISRAEL": "ISR",
        "IL": "ISR",
        "ICELAND": "ISL",
        "IS": "ISL",
        "IRELAND": "IRL",
        "IE": "IRL",
        "LUXEMBOURG": "LUX",
        "MALTA": "MLT",
        "CYPRUS": "CYP",
        "CY": "CYP",
        "SLOVENIA": "SVN",
        "SI": "SVN",
        "SLOVAK REPUBLIC": "SVK",
        "SLOVAKIA": "SVK",
        "SK": "SVK",
        "ROMANIA": "ROU",
        "RO": "ROU",
        "BULGARIA": "BGR",
        "BG": "BGR",
        "CROATIA": "HRV",
        "HR": "HRV",
        "ESTONIA": "EST",
        "EE": "EST",
        "LATVIA": "LVA",
        "LV": "LVA",
        "LITHUANIA": "LTU",
        "LT": "LTU",
        "COLOMBIA": "COL",
        "CO": "COL",
        "COSTA RICA": "CRI",
        "CR": "CRI",

        # Country groups
        "OECD": "OECD",
        "OECD_AVERAGE": "OECD",
        "OECD AVERAGE": "OECD",
        "ALL_OECD": "ALL_OECD",  # Special marker for multi-country queries
        "ALL OECD": "ALL_OECD",
        "ALL_OECD_COUNTRIES": "ALL_OECD",
        "ALL OECD COUNTRIES": "ALL_OECD",
        "G7": "G7",
        "G20": "G20",
        "EA": "EA19",  # Euro Area
        "EURO_AREA": "EA19",
        "EURO AREA": "EA19",
        "EU": "EU27_2020",
        "EUROPEAN_UNION": "EU27_2020",
        "EUROPEAN UNION": "EU27_2020",
    }

    # Region expansions - maps region names to lists of country codes
    # This enables "Nordic countries", "G7", etc. in queries
    REGION_EXPANSIONS: Dict[str, List[str]] = {
        # Nordic countries
        "NORDIC": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "NORDIC_COUNTRIES": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "NORDIC COUNTRIES": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "SCANDINAVIA": ["SWE", "NOR", "DNK"],
        "SCANDINAVIAN_COUNTRIES": ["SWE", "NOR", "DNK"],

        # G7 (7 major economies)
        "G7": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "G7_COUNTRIES": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],

        # G20 (OECD members only - excludes non-OECD like China, Russia, etc.)
        "G20": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN", "KOR", "AUS", "MEX",
                "TUR", "AUT", "BEL", "NLD", "ESP"],

        # BRICS (only those in OECD - limited overlap)
        # Note: China, Russia, India are NOT OECD members

        # European Union (OECD EU members)
        "EU": ["AUT", "BEL", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN",
               "IRL", "ITA", "LVA", "LTU", "LUX", "NLD", "POL", "PRT", "SVK", "SVN", "ESP", "SWE"],
        "EUROPEAN_UNION": ["AUT", "BEL", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN",
                          "IRL", "ITA", "LVA", "LTU", "LUX", "NLD", "POL", "PRT", "SVK", "SVN", "ESP", "SWE"],

        # Eurozone (countries using Euro)
        "EUROZONE": ["AUT", "BEL", "EST", "FIN", "FRA", "DEU", "GRC", "IRL", "ITA",
                     "LVA", "LTU", "LUX", "NLD", "PRT", "SVK", "SVN", "ESP"],
        "EURO_AREA": ["AUT", "BEL", "EST", "FIN", "FRA", "DEU", "GRC", "IRL", "ITA",
                      "LVA", "LTU", "LUX", "NLD", "PRT", "SVK", "SVN", "ESP"],

        # Asia-Pacific OECD members
        "ASIA_PACIFIC": ["JPN", "KOR", "AUS", "NZL"],
        "ASIA PACIFIC": ["JPN", "KOR", "AUS", "NZL"],
        "APAC": ["JPN", "KOR", "AUS", "NZL"],

        # Southern Europe
        "SOUTHERN_EUROPE": ["ESP", "ITA", "GRC", "PRT"],
        "SOUTHERN EUROPE": ["ESP", "ITA", "GRC", "PRT"],
        "MEDITERRANEAN": ["ESP", "ITA", "GRC", "PRT"],

        # Eastern Europe (OECD members)
        "EASTERN_EUROPE": ["POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU"],
        "EASTERN EUROPE": ["POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU"],

        # English-speaking countries
        "ANGLOSPHERE": ["USA", "GBR", "CAN", "AUS", "NZL", "IRL"],
        "ENGLISH_SPEAKING": ["USA", "GBR", "CAN", "AUS", "NZL", "IRL"],

        # ASEAN (for completeness - only OECD-adjacent countries have good data)
        # Note: Not all ASEAN countries are OECD members, but we include for multi-country queries
        "ASEAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "ASEAN_COUNTRIES": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],

        # BRICS (limited OECD coverage - only Chile/Colombia overlap, data may be incomplete)
        # Note: China, Russia, India, Brazil, South Africa are NOT OECD members
        # Including for query compatibility - will use best-effort data
        "BRICS": ["BRA", "RUS", "IND", "CHN", "ZAF"],
        "BRICS_COUNTRIES": ["BRA", "RUS", "IND", "CHN", "ZAF"],

        # BRICS+ (2024 expansion)
        "BRICS_PLUS": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],
    }

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None) -> None:
        settings = get_settings()
        self.base_url = settings.oecd_base_url.rstrip("/")
        self.metadata_search = metadata_search_service

    @classmethod
    def _load_dataflows_catalog(cls) -> Dict:
        """Load OECD dataflows catalog from disk (lazy loading with caching).

        Returns:
            Dictionary mapping dataflow IDs to their metadata
        """
        if cls._DATAFLOWS_CATALOG is not None:
            return cls._DATAFLOWS_CATALOG

        catalog_path = Path(__file__).parent.parent / "data" / "metadata" / "sdmx" / "oecd_dataflows.json"

        if not catalog_path.exists():
            logger.warning(f"OECD dataflows catalog not found at {catalog_path}")
            cls._DATAFLOWS_CATALOG = {}
            return cls._DATAFLOWS_CATALOG

        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                cls._DATAFLOWS_CATALOG = json.load(f)
            logger.info(f"Loaded OECD dataflows catalog with {len(cls._DATAFLOWS_CATALOG)} dataflows")
        except Exception as e:
            logger.error(f"Failed to load OECD dataflows catalog: {e}")
            cls._DATAFLOWS_CATALOG = {}

        return cls._DATAFLOWS_CATALOG

    def _country_code(self, country: str) -> str:
        """Normalize country code to OECD format (ISO alpha-3).

        PHASE C: Uses CountryResolver as primary source, with fallback to local mappings.
        OECD API uses ISO alpha-3 codes (USA, DEU, FRA), so we convert from alpha-2 if needed.

        Supports various input formats:
        - Full names: "United States", "Costa Rica"
        - Short codes: "US", "CA"
        - With spaces or underscores: handled transparently
        """
        # Normalize input: convert to uppercase
        country_upper = country.upper()

        # Try direct match in local mappings first (has OECD alpha-3 codes)
        if country_upper in self.COUNTRY_MAPPINGS:
            return self.COUNTRY_MAPPINGS[country_upper]

        # PHASE C: Try CountryResolver and convert to alpha-3
        try:
            from ..routing.country_resolver import CountryResolver
            iso_alpha2 = CountryResolver.normalize(country)
            if iso_alpha2:
                # Convert alpha-2 to alpha-3 using our mappings
                if iso_alpha2 in self.COUNTRY_MAPPINGS:
                    return self.COUNTRY_MAPPINGS[iso_alpha2]
                # Common alpha-2 to alpha-3 conversions for OECD
                alpha2_to_alpha3 = {
                    "US": "USA", "DE": "DEU", "FR": "FRA", "GB": "GBR", "JP": "JPN",
                    "IT": "ITA", "CA": "CAN", "ES": "ESP", "AU": "AUS", "KR": "KOR",
                    "MX": "MEX", "NL": "NLD", "BE": "BEL", "AT": "AUT", "SE": "SWE",
                    "NO": "NOR", "DK": "DNK", "FI": "FIN", "CH": "CHE", "PL": "POL",
                    "PT": "PRT", "GR": "GRC", "IE": "IRL", "NZ": "NZL", "CZ": "CZE",
                    "HU": "HUN", "SK": "SVK", "SI": "SVN", "LU": "LUX", "IS": "ISL",
                    "EE": "EST", "LV": "LVA", "LT": "LTU", "TR": "TUR", "IL": "ISR",
                    "CL": "CHL", "CO": "COL", "CR": "CRI"
                }
                if iso_alpha2 in alpha2_to_alpha3:
                    return alpha2_to_alpha3[iso_alpha2]
        except Exception:
            pass

        # Try with underscores replaced with spaces
        country_spaces = country_upper.replace("_", " ").replace("-", " ")
        if country_spaces in self.COUNTRY_MAPPINGS:
            return self.COUNTRY_MAPPINGS[country_spaces]

        # Try with spaces replaced with underscores
        country_underscores = country_upper.replace(" ", "_").replace("-", "_")
        if country_underscores in self.COUNTRY_MAPPINGS:
            return self.COUNTRY_MAPPINGS[country_underscores]

        # Try fuzzy match: compare without spaces/underscores/dashes
        normalized_input = country_upper.replace(" ", "").replace("_", "").replace("-", "")
        for map_key, code in self.COUNTRY_MAPPINGS.items():
            normalized_key = map_key.replace("_", "").replace(" ", "").replace("-", "")
            if normalized_key == normalized_input:
                return code

        # Default: return uppercase country code
        return country_upper

    def expand_countries(self, country_or_region: str) -> List[str]:
        """Expand a country or region name to a list of country codes.

        Uses CountryResolver as the single source of truth for region definitions.
        Falls back to OECD-specific mappings for groups not in CountryResolver.

        This method handles:
        - Single countries: "USA" → ["USA"]
        - Regional groups: "Nordic" → ["SWE", "NOR", "DNK", "FIN", "ISL"]
        - "ALL_OECD" → all OECD member countries

        Args:
            country_or_region: Country name/code or region identifier

        Returns:
            List of ISO 3166-1 alpha-3 country codes
        """
        from ..routing.country_resolver import CountryResolver

        # Normalize input
        key = country_or_region.upper().replace("-", "_")

        # Check for ALL_OECD special case
        if key in ("ALL_OECD", "ALL OECD", "ALL_OECD_COUNTRIES", "ALL OECD COUNTRIES", "OECD_COUNTRIES"):
            return self.OECD_MEMBER_COUNTRIES

        # First, try CountryResolver (single source of truth for standard regions)
        expanded = CountryResolver.get_region_expansion(key, format="iso3")
        if expanded:
            logger.info(f"🌍 Expanding region '{country_or_region}' via CountryResolver → {len(expanded)} countries")
            return expanded

        # Try variant names
        for variant in [key, key.replace("_COUNTRIES", ""), key.replace("_NATIONS", "")]:
            expanded = CountryResolver.get_region_expansion(variant, format="iso3")
            if expanded:
                logger.info(f"🌍 Matched region '{variant}' via CountryResolver → {len(expanded)} countries")
                return expanded

        # Fall back to OECD-specific region expansions (ANGLOSPHERE, SOUTHERN_EUROPE, etc.)
        for region_key in [key, key.replace("_", " "), key.replace(" ", "_")]:
            if region_key in self.REGION_EXPANSIONS:
                logger.info(f"🌍 Expanding region '{country_or_region}' via OECD mappings → {len(self.REGION_EXPANSIONS[region_key])} countries")
                return self.REGION_EXPANSIONS[region_key]

        # Single country - normalize and return as list
        return [self._country_code(country_or_region)]

    async def _resolve_indicator(self, indicator: str) -> tuple[str, str, str]:
        """Resolve OECD dataflow through dynamic metadata discovery.

        This method implements a multi-layer fallback strategy:
        1. Check cache (fastest, for frequently-accessed indicators)
        2. Query metadata search service using SDMX catalogs (primary discovery)
        3. Use LLM to select best matching dataflow (intelligent selection)
        4. Extract agency and structure information from SDMX metadata
        5. Fall back to local catalog lookup if metadata search unavailable

        Returns:
            Tuple of (agency, dataflow, version)

        Raises:
            DataNotAvailableError if no suitable dataflow found after all fallback attempts
        """
        # STEP 1: Check cache first
        cache_key = f"oecd_indicator:{indicator.upper()}"
        cached = cache_service.get(cache_key)
        if cached:
            logger.info(f"🔄 Cache hit for OECD indicator: {indicator}")
            return cached

        logger.info(f"🔍 Resolving OECD indicator: {indicator} (cache miss)")

        # STEP 2: Use metadata search if available (PRIMARY method)
        if self.metadata_search:
            try:
                logger.info(f"📚 Searching OECD metadata catalog for indicator: {indicator}")
                search_results = await self.metadata_search.search_with_sdmx_fallback(
                    provider="OECD",
                    indicator=indicator,
                )

                if search_results:
                    logger.info(f"✅ Found {len(search_results)} matching OECD dataflows for '{indicator}'")

                    # Use LLM to intelligently select the best match
                    logger.info(f"🤖 Using LLM to select best matching dataflow for '{indicator}'")
                    discovery = await self.metadata_search.discover_indicator(
                        provider="OECD",
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
                        confidence = discovery.get('confidence', 0)
                        dataflow_code = discovery["code"]

                        # Only use LLM result if confidence is high enough (>0.6)
                        if confidence > 0.6:
                            logger.info(
                                f"✅ LLM selected dataflow: {dataflow_code} "
                                f"(confidence: {confidence})"
                            )

                            # Extract agency from structure/dataflow info
                            result = self._build_result_from_discovery(dataflow_code, discovery)
                            cache_service.set(cache_key, result, ttl=86400)  # Cache 24h
                            logger.info(f"✅ Resolved OECD indicator '{indicator}' → {result}")
                            return result
                        else:
                            logger.warning(
                                f"⚠️ LLM confidence too low for '{indicator}' "
                                f"(confidence: {confidence} < 0.6). Falling back to catalog lookup."
                            )
                    else:
                        logger.warning(
                            f"⚠️ LLM could not select a dataflow for '{indicator}'. "
                            f"Falling back to catalog lookup."
                        )
                else:
                    logger.warning(
                        f"⚠️ No SDMX metadata found for '{indicator}'. "
                        f"Falling back to local catalog lookup."
                    )

            except Exception as e:
                logger.warning(
                    f"⚠️ Metadata search failed for '{indicator}': {type(e).__name__}: {str(e)}. "
                    f"Falling back to local catalog lookup."
                )

        # STEP 3: Fall back to local catalog lookup (FALLBACK method)
        logger.info(f"📂 Attempting direct catalog lookup for '{indicator}'")
        try:
            catalog = self._load_dataflows_catalog()

            if not catalog:
                raise DataNotAvailableError(
                    f"OECD metadata catalog not loaded. Cannot resolve '{indicator}'. "
                    f"Please check that backend/data/metadata/sdmx/oecd_dataflows.json exists."
                )

            # Collect all matching candidates
            candidates = []
            indicator_lower = indicator.lower()
            indicator_words = set(indicator_lower.replace("_", " ").split())

            for flow_id, flow_info in catalog.items():
                name = flow_info.get("name", "").lower()
                desc = flow_info.get("description", "").lower()
                structure = flow_info.get("structure", "")

                # Calculate match score
                score = 0
                if indicator_lower in name:
                    score += 100  # Exact match in name
                elif indicator_lower in desc:
                    score += 50   # Exact match in description
                else:
                    # Partial word matching
                    for word in indicator_words:
                        if len(word) > 2:  # Ignore very short words
                            if word in name:
                                score += 20
                            elif word in desc:
                                score += 5

                # Calculate priority bonus for main OECD statistical dataflows
                # Prioritize main statistical series over specialized/derivative datasets
                priority_bonus = 0

                # CRITICAL: Tax/revenue dataflows should have HIGHEST priority when user asks for tax revenue
                # This check must come FIRST before GDP checks, otherwise GDP always wins
                if "tax" in indicator_lower or "revenue" in indicator_lower:
                    # Highest priority: OECD comparative tax revenue statistics
                    if "REV_COMP_OECD" in flow_id or "DF_RSOECD" in flow_id:
                        # This is THE authoritative tax revenue comparison dataflow
                        priority_bonus += 3000
                        logger.debug(f"OECD Comparative Tax Revenue boost for {flow_id}: +3000")
                    elif "REV_OECD" in flow_id and "COMP" not in flow_id:
                        # Country-specific tax revenue dataflows (second priority)
                        priority_bonus += 2500
                        logger.debug(f"OECD Country Tax Revenue boost for {flow_id}: +2500")
                    elif "REV" in flow_id and ("OECD" in flow_id or "TAX" in flow_id):
                        # Other tax revenue statistics dataflows
                        priority_bonus += 2000
                        logger.debug(f"Tax/Revenue boost for {flow_id}: +2000")
                    elif any(x in name.lower() for x in ["tax revenue", "revenue statistics"]):
                        priority_bonus += 1500
                        logger.debug(f"Tax name match for {flow_id}: +1500")

                    # PENALTY: Subnational/dashboard dataflows are less relevant for general queries
                    if "DASHBOARD" in flow_id or "subnational" in name.lower():
                        priority_bonus -= 1000
                        logger.debug(f"Subnational penalty for {flow_id}: -1000")

                # CRITICAL: Working hours/labor market dataflows should have HIGHEST priority
                # when user asks for working hours, hours worked, annual hours, etc.
                is_working_hours_query = any(x in indicator_lower for x in [
                    "working hours", "hours worked", "annual hours", "weekly hours",
                    "work hours", "hours per worker", "hours per year"
                ])
                if is_working_hours_query:
                    # Highest priority: DSD_HW dataflows (Hours Worked statistics)
                    if "DSD_HW@" in flow_id:
                        # Prefer DF_AVG_ANN_HRS_WKD (Average annual hours actually worked per worker)
                        if "AVG_ANN_HRS_WKD" in flow_id:
                            priority_bonus += 4000  # HIGHEST priority for annual hours
                            logger.debug(f"Annual hours worked boost for {flow_id}: +4000")
                        elif "AVG_USL_WK_WKD" in flow_id:
                            priority_bonus += 3500  # Usual weekly hours (second choice)
                            logger.debug(f"Weekly hours worked boost for {flow_id}: +3500")
                        else:
                            priority_bonus += 3000  # Other DSD_HW dataflows
                            logger.debug(f"DSD_HW boost for {flow_id}: +3000")
                    # PENALTY: Dataflows about tax/poverty/escape poverty are NOT what user wants
                    elif "TAXBEN" in flow_id or "poverty" in name.lower() or "escape" in name.lower():
                        priority_bonus -= 2000
                        logger.debug(f"Tax/poverty penalty for {flow_id}: -2000")
                    # PENALTY: GDP/National Accounts dataflows are NOT working hours
                    elif "NAMAIN" in structure or "GDP" in flow_id:
                        priority_bonus -= 3000
                        logger.debug(f"GDP penalty for working hours query {flow_id}: -3000")

                # CRITICAL: Productivity dataflows should have HIGHEST priority when user asks for productivity
                # IMPORTANT: Distinguish between "productivity growth" (rates) vs "productivity" (levels)
                is_productivity_query = any(x in indicator_lower for x in [
                    "productivity", "labor productivity", "labour productivity"
                ])
                # Check if user specifically wants GROWTH rates (not absolute levels)
                is_growth_query = any(x in indicator_lower for x in [
                    "growth", "change", "rate of change", "growth rate"
                ])
                if is_productivity_query:
                    # Highest priority: DSD_PDB productivity dataflows
                    # NOTE: DF_PDB (main) returns 404, use DF_PDB_LV (levels) or DF_PDB_GR (growth)
                    if "DF_PDB_GR" in flow_id:
                        # Productivity growth rates
                        if is_growth_query:
                            # User wants growth - DF_PDB_GR gets HIGHEST priority
                            priority_bonus += 7000
                            logger.debug(f"Productivity GROWTH rates boost (user wants growth) for {flow_id}: +7000")
                        else:
                            # User wants productivity (ambiguous) - growth is secondary
                            priority_bonus += 5000
                            logger.debug(f"Productivity growth rates boost for {flow_id}: +5000")
                    elif "DF_PDB_LV" in flow_id:
                        # Productivity levels (absolute values)
                        if is_growth_query:
                            # User wants growth - PENALIZE levels dataflow
                            priority_bonus += 3000
                            logger.debug(f"Productivity levels PENALIZED (user wants growth) for {flow_id}: +3000")
                        else:
                            # User wants productivity (ambiguous) - levels is primary
                            priority_bonus += 6000
                            logger.debug(f"Productivity levels boost for {flow_id}: +6000")
                    elif "DSD_PDB@DF_PDB" in flow_id and "LV" not in flow_id and "GR" not in flow_id:
                        # Main DF_PDB often returns 404, give it lower priority
                        priority_bonus += 4000
                        logger.debug(f"Main productivity database boost for {flow_id}: +4000 (may have limited data)")
                    elif "DSD_PDB@" in flow_id or "DSD_PDB" in structure:
                        priority_bonus += 5000  # General productivity database
                        logger.debug(f"Productivity database boost for {flow_id}: +5000")
                    # PENALTY: Regional productivity dataflows are less relevant for national queries
                    elif "REG" in structure or "FUA" in flow_id or "REGION" in name:
                        priority_bonus -= 1000
                        logger.debug(f"Regional productivity penalty for {flow_id}: -1000")

                # CRITICAL: Education, healthcare, and R&D spending queries
                # These can be expressed as: absolute values, % of GDP, or per capita
                # We need to detect what the user wants and prioritize the correct dataflow
                is_spending_query = any(x in indicator_lower for x in [
                    "spending", "expenditure", "cost"
                ])
                is_percent_gdp_query = any(x in indicator_lower for x in [
                    "% of gdp", "percent of gdp", "as percent", "share of gdp", "gdp share",
                    "as a share", "percentage"
                ])
                is_per_capita_query = any(x in indicator_lower for x in [
                    "per capita", "per person", "per head"
                ])

                # Education spending queries
                is_education_query = any(x in indicator_lower for x in [
                    "education", "school", "university", "educational"
                ])
                if is_education_query and is_spending_query:
                    # OECD Education at a Glance (EAG) dataflows
                    if "EAG" in flow_id or "EAG" in structure:
                        if is_per_capita_query and "PER_STUD" in flow_id:
                            # Per student spending - HIGHEST for per capita queries
                            priority_bonus += 7000
                            logger.debug(f"Education per student spending boost for {flow_id}: +7000")
                        elif is_percent_gdp_query and ("GDP" in flow_id or "GDP" in name.upper()):
                            # % of GDP spending - HIGHEST for % of GDP queries
                            priority_bonus += 7000
                            logger.debug(f"Education % of GDP spending boost for {flow_id}: +7000")
                        elif "GDP" in flow_id or "GDP" in name.upper():
                            # Default: % of GDP is most common request
                            priority_bonus += 6000
                            logger.debug(f"Education spending (% GDP default) boost for {flow_id}: +6000")
                        else:
                            priority_bonus += 5000
                            logger.debug(f"Education spending boost for {flow_id}: +5000")
                    # Penalize absolute spending dataflows when user likely wants % of GDP
                    if not is_per_capita_query and not is_percent_gdp_query:
                        # User just said "education spending" - prefer % of GDP
                        if any(x in name.lower() for x in ["million", "billion", "usd", "national currency"]):
                            priority_bonus -= 1000
                            logger.debug(f"Absolute education spending penalty for {flow_id}: -1000")

                # Healthcare spending queries
                is_health_query = any(x in indicator_lower for x in [
                    "health", "healthcare", "medical"
                ])
                if is_health_query and is_spending_query:
                    # OECD Health Statistics (SHA, HEALTH) dataflows
                    if any(x in flow_id or x in structure for x in ["SHA", "HEALTH", "HLTH"]):
                        if is_per_capita_query:
                            # Per capita spending - HIGHEST for per capita queries
                            if "CAP" in flow_id or "capita" in name.lower():
                                priority_bonus += 7000
                                logger.debug(f"Healthcare per capita spending boost for {flow_id}: +7000")
                            else:
                                priority_bonus += 5500
                        elif is_percent_gdp_query:
                            if "GDP" in flow_id or "gdp" in name.lower():
                                priority_bonus += 7000
                                logger.debug(f"Healthcare % of GDP spending boost for {flow_id}: +7000")
                            else:
                                priority_bonus += 5500
                        else:
                            # Default based on query context
                            priority_bonus += 5000
                            logger.debug(f"Healthcare spending boost for {flow_id}: +5000")

                # R&D expenditure queries
                is_rd_query = any(x in indicator_lower for x in [
                    "r&d", "r & d", "research and development", "research & development"
                ])
                if is_rd_query:
                    # OECD Main Science and Technology Indicators (MSTI) dataflows
                    if any(x in flow_id or x in structure for x in ["MSTI", "STI", "RD"]):
                        if is_percent_gdp_query or "gdp" in name.lower():
                            # % of GDP is most common for R&D
                            priority_bonus += 6000
                            logger.debug(f"R&D % of GDP boost for {flow_id}: +6000")
                        else:
                            # Default to % of GDP for R&D queries
                            priority_bonus += 5500
                            logger.debug(f"R&D expenditure boost for {flow_id}: +5500")

                # Main statistical aggregates (GDP) - only boost if NOT asking for tax/revenue or working hours
                if "NAMAIN" in structure and "tax" not in indicator_lower and "revenue" not in indicator_lower and not is_working_hours_query:
                    priority_bonus += 1000  # National Accounts Main Aggregates
                elif "QNA" in flow_id or "QNA" in name:
                    priority_bonus += 800  # Quarterly National Accounts
                elif "LFS" in structure or "IALFS" in flow_id:
                    priority_bonus += 800  # Labour Force Survey (for unemployment)
                    # Extra boost for "rates" dataflows when user asks for "rate"
                    # Check for "rate" word or "_rt" abbreviation (e.g., UNE_RT = unemployment rate)
                    is_rate_query = "rate" in indicator_lower or "_rt" in indicator_lower or indicator_lower.endswith("rt")
                    if is_rate_query and "rate" in name:
                        priority_bonus += 500  # Prefer rates dataflow for rate queries
                        logger.debug(f"Rate match boost for {flow_id}: +500")

                # High priority: Standard OECD datasets
                elif "OECD" in flow_id and "PRICES" in name:
                    priority_bonus += 600  # Main price indexes

                # Negative priority: Specialized/derivative datasets
                # These are valuable but shouldn't be the default choice
                elif any(x in flow_id or x in name.upper() for x in ["CONTRIB", "CONTRIBUTION"]):
                    priority_bonus -= 800  # Contribution/decomposition datasets
                elif "AFDD" in flow_id or "FUA" in flow_id or "REG" in structure:
                    priority_bonus -= 500  # Regional/urban area datasets
                # NOTE: Removed "REV_COMP" penalty - was incorrectly penalizing tax revenue queries
                # The REV_COMP penalty was meant for "revenue component" breakdowns, but it also
                # penalized DSD_REV_COMP_OECD which is the comparative tax revenue dataset we want
                # Tax revenue is a major OECD indicator and should have HIGH priority

                # High priority: Tax and revenue statistics (when explicitly queried)
                if "tax" in indicator_lower or "revenue" in indicator_lower:
                    if "tax" in name.lower() or "revenue" in name.lower():
                        priority_bonus += 500  # Boost tax/revenue dataflows when user requests them

                if score > 0:
                    candidates.append((score + priority_bonus, flow_id, flow_info, structure))

            if candidates:
                # Sort by score (descending) and select best match
                candidates.sort(key=lambda x: x[0], reverse=True)
                best_score, flow_id, flow_info, structure = candidates[0]

                agency = self._extract_agency_from_structure(structure, flow_id)

                # Keep full catalog entry format (DSD_XXX@DF_XXX) for later extraction
                # fetch_indicator will extract both DSD ID and dataflow ID as needed
                dataflow = flow_id
                version = "1.0"

                result = (agency, dataflow, version)
                cache_service.set(cache_key, result, ttl=86400)  # Cache 24h
                logger.info(
                    f"✅ Found OECD indicator '{indicator}' in local catalog → {dataflow} "
                    f"(priority-adjusted score: {best_score}, structure: {structure}, agency: {agency})"
                )
                return result

        except Exception as e:
            logger.error(
                f"❌ Local catalog lookup failed for '{indicator}': {type(e).__name__}: {str(e)}"
            )

        # STEP 4: All methods exhausted - raise error with helpful message
        raise DataNotAvailableError(
            f"OECD indicator '{indicator}' not found in metadata catalog. "
            f"Try refining your query or use a known indicator like: "
            f"GDP, GDP Growth, Unemployment Rate, Inflation, CPI, "
            f"Exports, Imports, Government Debt, Productivity, "
            f"Education Spending, Health Expenditure"
        )

    def _build_result_from_discovery(self, dataflow_code: str, discovery: dict) -> tuple[str, str, str]:
        """Build the final result tuple from LLM discovery output.

        Args:
            dataflow_code: The selected dataflow code (e.g., "DSD_NAMAIN1@DF_QNA")
            discovery: LLM discovery result with code, name, description, confidence, optional agency

        Returns:
            Tuple of (agency, dataflow, version)
        """
        # Check if agency is already in discovery result (from metadata search)
        if discovery.get("agency"):
            agency = discovery["agency"]
            logger.info(f"Using agency from discovery: {agency}")
        else:
            # Extract agency from structure
            structure = dataflow_code.split("@")[0] if "@" in dataflow_code else dataflow_code
            agency = self._extract_agency_from_structure(structure, dataflow_code)
            logger.info(f"Extracted agency from structure: {agency}")

        # Keep full format (DSD_XXX@DF_XXX) for later extraction in fetch_indicator
        dataflow = dataflow_code
        version = "1.0"

        return (agency, dataflow, version)

    def _extract_agency_from_structure(self, structure: str, dataflow_code: str) -> str:
        """Extract OECD agency code from dataflow structure.

        OECD uses several agencies:
        - OECD.SDD.NAD - National Accounts Division (GDP, QNA, NAMAIN, etc.)
        - OECD.SDD.TPS - Labour and Social Statistics (Employment, Unemployment, LFS, etc.)
        - OECD.ECO.MAD - Economic Outlook (Inflation, Prices, CPI, etc.)
        - OECD.CFE.EDS - Centre for Entrepreneurship, SMEs and Regions (Regional stats)
        - OECD.STI.PIE - Science, Technology and Industry (Patents, Innovation)
        - Others...

        Args:
            structure: DSD structure ID (e.g., "SEEAAIR", "DSD_NAMAIN1", "DSD_LFS")
            dataflow_code: Full dataflow code (e.g., "DSD_NAMAIN1@DF_QNA")

        Returns:
            Agency code for SDMX URL
        """
        # Map common structure prefixes to agencies
        structure_upper = structure.upper()
        dataflow_upper = dataflow_code.upper()

        # National accounts (GDP, QNA, National Accounts)
        if any(x in structure_upper for x in ["NAMAIN", "TABLE1", "ANA_MAIN", "NPS"]):
            return "OECD.SDD.NAD"
        if "QNA" in dataflow_upper:
            return "OECD.SDD.NAD"

        # Education Statistics (EAG = Education at a Glance) - check BEFORE labor market
        if "EAG" in structure_upper:
            return "OECD.SDD.EDSTAT"

        # Hours Worked statistics (DSD_HW) - use OECD.ELS.SAE (Employment, Labour and Social Affairs)
        # This is DIFFERENT from other labor force statistics (LFS, IALFS) which use OECD.SDD.TPS
        # DSD_HW dataflows: DF_AVG_ANN_HRS_WKD, DF_AVG_USL_WK_WKD, DF_EMP_USL_WK_HRS, etc.
        if "DSD_HW" in structure_upper or "DSD_HW" in dataflow_upper:
            return "OECD.ELS.SAE"
        if any(x in dataflow_upper for x in ["AVG_ANN_HRS", "AVG_USL_WK", "HRS_WKD"]):
            return "OECD.ELS.SAE"

        # Labor force statistics (Unemployment, Employment, LSO)
        # Note: LSO = Labour Force Survey, IALFS = International Active Labour Force Statistics
        if any(x in structure_upper for x in ["LFS", "LABOUR", "LAB", "LSO"]):
            return "OECD.SDD.TPS"
        if any(x in dataflow_upper for x in ["IALFS", "UNEMP"]):
            return "OECD.SDD.TPS"

        # Consumer prices and inflation statistics (CPI, PRICES)
        # IMPORTANT: PRICES and CPI dataflows use OECD.SDD.TPS, not ECO.MAD
        if any(x in structure_upper for x in ["PRICES", "CPI"]):
            return "OECD.SDD.TPS"
        if any(x in dataflow_upper for x in ["PRICES", "CPI"]):
            return "OECD.SDD.TPS"

        # Economic outlook (EO) forecasts
        if "EO" in dataflow_upper:
            return "OECD.ECO.MAD"

        # Regional statistics (TL2, TL3, FUA, Metro, Regional)
        if any(x in structure_upper for x in ["REG_", "FUA", "METRO", "TL2", "TL3"]):
            return "OECD.CFE.EDS"

        # Patents and Innovation
        if "PATENT" in structure_upper:
            return "OECD.STI.PIE"

        # Environment and Sustainable Development
        if any(x in structure_upper for x in ["SEEA", "ENVIR", "ENV"]):
            return "OECD.ENV"

        # Trade and Competitiveness
        if any(x in structure_upper for x in ["TRADE", "EXPORT", "IMPORT", "TRAD"]):
            return "OECD.TAD"

        # Tax Policy and Statistics (Revenue Statistics, Tax Revenues)
        # IMPORTANT: Tax revenue dataflows use OECD.CTP.TPS agency
        if any(x in structure_upper for x in ["REV", "TAX"]) and "OECD" in dataflow_upper:
            return "OECD.CTP.TPS"
        if any(x in structure_upper for x in ["DSD_REV", "DSD_TAX"]):
            return "OECD.CTP.TPS"
        if "DASHBOARD" in structure_upper and ("TAX" in dataflow_upper or "REV" in dataflow_upper):
            return "OECD.CTP.TPS"

        # Productivity Statistics (Productivity Database)
        # DSD_PDB is the main productivity database - uses OECD.SDD.TPS agency
        # (not OECD.SDD.SSIS which returns 404)
        if "DSD_PDB" in structure_upper or "PRODUCTIVITY" in dataflow_upper:
            return "OECD.SDD.TPS"

        # Default fallback (most common is SDD.NAD)
        logger.info(
            f"Unmapped OECD structure '{structure}' (dataflow: {dataflow_code}), "
            f"using default OECD.SDD.NAD"
        )
        return "OECD.SDD.NAD"

    async def fetch_indicator(
        self,
        indicator: str,
        country: str = "USA",
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> NormalizedData:
        """Fetch economic indicator data from OECD.

        Args:
            indicator: Indicator type (GDP, UNEMPLOYMENT, INFLATION)
            country: Country code (ISO 3166-1 alpha-3 or common names)
            start_year: Start year for data range
            end_year: End year for data range

        Returns:
            NormalizedData with observations

        Raises:
            DataNotAvailableError: If circuit breaker is open or data not available
        """
        # NOTE: Circuit breaker check removed - it was too aggressive and blocked valid queries
        # The circuit breaker will still protect us by opening AFTER we hit actual 429 errors
        # (handled in the retry logic below at lines 974-977)

        # Resolve indicator to (agency, dataflow, version) tuple using metadata search if needed
        agency, dataflow, version = await self._resolve_indicator(indicator)
        country_code = self._country_code(country)

        # Build time parameters with intelligent defaults
        from datetime import datetime
        current_year = datetime.now().year

        params = {"dimensionAtObservation": "AllDimensions"}

        # Default to last 5 years if no time range specified
        if not start_year and not end_year:
            params["startPeriod"] = str(current_year - 5)
            params["endPeriod"] = str(current_year)
        else:
            if start_year:
                params["startPeriod"] = str(start_year)
            if end_year:
                params["endPeriod"] = str(end_year)

        # Build SDMX filter key using dynamic DSD lookup (general solution)
        # Extract DSD ID from dataflow string (format: DSD_XXX@DF_YYY)
        dsd_id = dataflow.split("@")[0] if "@" in dataflow else dataflow

        # Build proper dimension key to avoid downloading ALL data (causes rate limiting)
        # Use DSD cache service to dynamically discover dimension structure
        from ..services.dsd_cache import get_dimension_key_builder

        key_builder = get_dimension_key_builder()
        filter_key = await key_builder.build_key(
            provider="OECD",
            agency=agency,
            dsd_id=dsd_id,
            version=version,
            base_url=self.base_url,
            user_params={"country": country_code},
            custom_defaults=None,
        )

        # Fallback with smart defaults if dimension key building fails
        if not filter_key:
            logger.warning(
                f"Failed to build dimension key for {dsd_id} (DSD may not exist). "
                f"Using smart defaults based on common OECD data structures."
            )
            # Instead of "all", use common OECD dimension pattern:
            # Most OECD dataflows follow: REF_AREA.INDICATOR.MEASURE.FREQ...
            # Build a minimal key with just country to reduce data volume
            filter_key = f".{country_code}.........."  # Country in 2nd position (common pattern)
            logger.info(f"Using fallback dimension key: {filter_key}")
        else:
            logger.info(f"Built OECD dimension key: {filter_key}")

        # Determine expected frequency based on indicator type and dataflow
        indicator_upper = indicator.upper()
        expected_freq = None

        if "QNA" in dataflow or "QUARTERLY" in indicator_upper:
            expected_freq = "Q"  # Quarterly
        elif indicator_upper in ["GDP", "GDP_GROWTH", "GDP_PER_CAPITA", "TRADE",
                                  "EXPORTS", "IMPORTS", "GOVERNMENT_DEBT", "GOVERNMENT_DEFICIT",
                                  "TAX_REVENUE", "PRODUCTIVITY", "EDUCATION_SPENDING",
                                  "EDUCATION_EXPENDITURE", "HEALTH_EXPENDITURE", "HEALTH_SPENDING"]:
            expected_freq = "A"  # Annual
        elif "MONTHLY" in indicator_upper or "UNE" in dataflow:
            expected_freq = "M"  # Monthly

        # Determine expected measure/transformation for specific indicators
        expected_measure = None
        expected_transform = None

        if "GROWTH" in indicator_upper:
            expected_transform = "GRW"  # Growth rate
        elif "RATE" in indicator_upper or indicator_upper in ["UNEMPLOYMENT", "INFLATION"]:
            expected_measure = "PC"  # Percentage

        # Construct URL
        # OECD SDMX API requires the FULL dataflow ID including DSD_XXX@DF_XXX format
        url = f"{self.base_url}/data/{agency},{dataflow},{version}/{filter_key}"

        # STEP 1: Wait for rate limiter before making request
        # This prevents hitting rate limits in the first place by enforcing delays
        wait_delay = await wait_for_provider("OECD")
        if wait_delay > 0:
            logger.info(f"⏳ OECD rate limiter applied {wait_delay:.1f}s delay before request")

        # Wrap HTTP call with enhanced retry logic for OECD rate limiting
        # OECD has strict per-IP rate limits - we need aggressive retries
        # Use shared HTTP client pool for better performance
        http_client = get_http_client()

        async def fetch_with_retry():
            try:
                # Use 50s timeout - OECD SDMX API can be very slow for complex queries
                # Research shows OECD has 60 requests/hour rate limit, so we need patience
                response = await http_client.get(
                    url,
                    params=params,
                    headers={"Accept": "application/vnd.sdmx.data+json; version=2.0.0"},
                    timeout=50.0,
                )

                # Check for rate limiting BEFORE raise_for_status
                if response.status_code == 429:
                    # Record rate limit error for circuit breaker
                    record_provider_rate_limit_error("OECD")
                    response.raise_for_status()  # This will trigger retry logic

                response.raise_for_status()

                # Success! Record it to reset circuit breaker
                record_provider_success("OECD")
                return response.json()
            finally:
                # Record this request for rate limiting purposes
                record_provider_request("OECD")

        # Use retry_async with exponential backoff and jitter for OECD:
        # - 3 attempts (original + 2 retries)
        # - Exponential backoff: 3s → 6s → 12s
        # - Jitter: 0-2s random added to avoid thundering herd
        # Total worst case: 50s + 5s + 50s + 8s + 50s = ~163s (but rare)
        data = await retry_async(
            fetch_with_retry,
            max_attempts=3,  # More attempts for slow OECD API
            initial_delay=3.0,  # Start with 3s delay
            backoff_factor=2.0,  # Exponential backoff
            jitter=2.0,  # Add 0-2s random jitter
        )

        # Parse SDMX-JSON 2.0 format
        # Check if data is None before accessing
        if data is None:
            raise DataNotAvailableError(f"No response data received for {country_code} {indicator}")

        datasets = data.get("data", {}).get("dataSets", [])
        if not datasets:
            raise DataNotAvailableError(f"No data found for {country_code} {indicator}")

        dataset = datasets[0]
        # Check if dataset is None before accessing
        if dataset is None:
            raise DataNotAvailableError(f"Empty dataset received for {country_code} {indicator}")

        observations = dataset.get("observations", {})
        if not observations:
            raise DataNotAvailableError(f"No observations found for {country_code} {indicator}")

        # Get structure information
        structures = data.get("data", {}).get("structures", [])
        if not structures:
            raise RuntimeError("No structure information in response")

        structure = structures[0]
        # Check if structure is None before accessing
        if structure is None:
            raise RuntimeError(f"Empty structure received for {country_code} {indicator}")

        # Check if dimensions is None before accessing
        dimensions_dict = structure.get("dimensions")
        if dimensions_dict is None:
            raise RuntimeError(f"No dimensions information in structure for {country_code} {indicator}")
        dimensions = dimensions_dict.get("observation", [])

        # Find TIME_PERIOD dimension
        time_dim = next((d for d in dimensions if d.get("id") == "TIME_PERIOD"), None)
        if not time_dim:
            raise RuntimeError("No TIME_PERIOD dimension found")

        time_values = time_dim.get("values", [])

        # Find dimensions for filtering
        # CRITICAL: OECD doesn't populate position field, so use array index instead
        country_dim_index = None
        country_value_index = None
        freq_dim_index = None
        freq_value_indices = []
        measure_dim_index = None
        measure_value_indices = []
        transform_dim_index = None
        transform_value_indices = []

        for array_idx, dim in enumerate(dimensions):
            dim_id = dim.get("id")

            # Country dimension
            if dim_id in ["REF_AREA", "geo", "COUNTRY"]:
                country_dim_index = array_idx
                country_values = dim.get("values", [])

                logger.info(f"🔍 Looking for country code: {country_code}")
                logger.info(f"📊 REF_AREA at index {array_idx}, has {len(country_values)} countries")

                # Find the index of our requested country code in the dimension values
                for val_idx, val in enumerate(country_values):
                    if val.get("id") == country_code:
                        country_value_index = val_idx
                        logger.info(f"✅ Found {country_code} at value index {val_idx}")
                        break

                if country_value_index is None:
                    logger.warning(f"⚠️ Country code {country_code} not found in dimension values!")

            # Frequency dimension
            elif dim_id == "FREQ" and expected_freq:
                freq_dim_index = array_idx
                freq_values = dim.get("values", [])
                for val_idx, val in enumerate(freq_values):
                    if val.get("id") == expected_freq:
                        freq_value_indices.append(val_idx)
                        logger.info(f"✅ Found frequency {expected_freq} at index {val_idx}")

            # Measure dimension
            elif dim_id in ["MEASURE", "UNIT_MEASURE"] and expected_measure:
                measure_dim_index = array_idx
                measure_values = dim.get("values", [])
                for val_idx, val in enumerate(measure_values):
                    val_id = val.get("id", "")
                    if expected_measure in val_id or val_id.startswith("PC"):
                        measure_value_indices.append(val_idx)

            # Transformation dimension
            elif dim_id == "TRANSFORMATION" and expected_transform:
                transform_dim_index = array_idx
                transform_values = dim.get("values", [])
                for val_idx, val in enumerate(transform_values):
                    val_id = val.get("id", "")
                    if expected_transform in val_id or "GRW" in val_id or "GROWTH" in val_id:
                        transform_value_indices.append(val_idx)

        # Parse observations with enhanced filtering
        logger.info(f"📈 Total observations in API response: {len(observations)}")
        data_points = []
        observations_checked = 0
        observations_filtered_out = 0

        for obs_key, obs_value in observations.items():
            # obs_key is like "0:0:0:0:0:0" representing dimension indices
            indices = [int(i) if i != "~" else None for i in obs_key.split(":")]
            observations_checked += 1

            # Apply dimension filters
            skip_observation = False

            # Filter by country if we found the country dimension
            if country_dim_index is not None and country_value_index is not None:
                if indices[country_dim_index] != country_value_index:
                    skip_observation = True

            # Filter by frequency if specified
            if freq_dim_index is not None and freq_value_indices:
                if indices[freq_dim_index] not in freq_value_indices:
                    skip_observation = True

            # Filter by measure if specified
            if measure_dim_index is not None and measure_value_indices:
                if indices[measure_dim_index] not in measure_value_indices:
                    skip_observation = True

            # Filter by transformation if specified
            if transform_dim_index is not None and transform_value_indices:
                if indices[transform_dim_index] not in transform_value_indices:
                    skip_observation = True

            if skip_observation:
                observations_filtered_out += 1
                continue

            # The last dimension is typically TIME_PERIOD
            time_index = indices[-1]
            if time_index is not None and time_index < len(time_values):
                time_info = time_values[time_index]
                # Check if time_info is None before accessing
                if time_info is None:
                    continue
                time_period = time_info.get("id")
                # Skip if time_period is None
                if time_period is None:
                    continue

                # obs_value is an array where first element is the value
                value = obs_value[0] if isinstance(obs_value, list) and obs_value else obs_value

                if value is not None:
                    # Convert time period to ISO date
                    # OECD returns formats like "2020", "2020-Q1", "2020-01"
                    if "-Q" in time_period:
                        # Quarterly: convert 2020-Q1 to 2020-03-31
                        year, quarter = time_period.split("-Q")
                        month = int(quarter) * 3
                        date_str = f"{year}-{month:02d}-01"
                    elif "-" in time_period and len(time_period.split("-")) == 2:
                        # Monthly: 2020-01
                        date_str = f"{time_period}-01"
                    else:
                        # Annual: 2020
                        date_str = f"{time_period}-01-01"

                    data_points.append({"date": date_str, "value": float(value)})

        logger.info(f"📊 Filtering results:")
        logger.info(f"   Observations checked: {observations_checked}")
        logger.info(f"   Observations filtered out: {observations_filtered_out}")
        logger.info(f"   Data points extracted: {len(data_points)}")

        if not data_points:
            # Provide helpful error message based on what filters were applied
            error_parts = [f"No valid data points found for {country_code} {indicator}"]

            if country_value_index is None and country_dim_index is not None:
                error_parts.append(f"Country code '{country_code}' may not be available in this dataset.")

            if expected_freq and not freq_value_indices:
                error_parts.append(f"Frequency '{expected_freq}' may not be available.")

            if expected_measure and not measure_value_indices:
                error_parts.append(f"Measure type '{expected_measure}' may not be available.")

            error_parts.append("Try a different time period or country.")

            raise DataNotAvailableError(" ".join(error_parts))

        # Sort by date
        data_points.sort(key=lambda x: x["date"])

        # CRITICAL: Deduplicate data points when dimension filtering fails
        # This handles the case where OECD returns multiple countries/measures
        # and our filtering didn't work properly (common with complex dataflows)
        if len(data_points) > 0:
            # Group by date
            date_values: Dict[str, List[float]] = {}
            for point in data_points:
                date = point["date"]
                value = point["value"]
                if date not in date_values:
                    date_values[date] = []
                date_values[date].append(value)

            # Check if we have duplicates (multiple values per date)
            has_duplicates = any(len(v) > 1 for v in date_values.values())

            if has_duplicates:
                logger.warning(
                    f"⚠️ Found duplicate values per date ({len(data_points)} points for "
                    f"{len(date_values)} dates). Applying intelligent deduplication."
                )

                # Detect if this is a growth/rate indicator (values should be small percentages)
                is_growth_indicator = any(x in indicator.upper() for x in [
                    "GROWTH", "RATE", "CHANGE", "PERCENT"
                ])

                deduplicated = []
                for date, values in sorted(date_values.items()):
                    if len(values) == 1:
                        deduplicated.append({"date": date, "value": values[0]})
                    else:
                        # Multiple values for same date - need to pick the best one
                        if is_growth_indicator:
                            # For growth indicators, prefer values that look like percentages
                            # Filter out index values (near 100) and very large values
                            percentage_values = [v for v in values if -50 <= v <= 50]

                            if percentage_values:
                                # Take median to avoid outliers
                                percentage_values.sort()
                                mid = len(percentage_values) // 2
                                best_value = percentage_values[mid]
                            else:
                                # No percentage-like values, take smallest absolute value
                                best_value = min(values, key=lambda x: abs(x))
                        else:
                            # For level indicators, take the median
                            values.sort()
                            mid = len(values) // 2
                            best_value = values[mid]

                        deduplicated.append({"date": date, "value": best_value})

                logger.info(
                    f"✅ Deduplication: {len(data_points)} → {len(deduplicated)} data points"
                )
                data_points = deduplicated

        # Determine unit and frequency from data or indicator type
        unit = ""
        frequency = "annual"

        if expected_freq == "M":
            frequency = "monthly"
        elif expected_freq == "Q":
            frequency = "quarterly"
        elif expected_freq == "A":
            frequency = "annual"

        # Infer unit from indicator type
        indicator_upper = indicator.upper()
        if "RATE" in indicator_upper or indicator_upper in ["UNEMPLOYMENT", "INFLATION", "CPI"]:
            unit = "percent"
        elif "GDP" in indicator_upper:
            if "GROWTH" in indicator_upper:
                unit = "percent change"
            else:
                unit = "millions of national currency"
        elif "PRICE" in indicator_upper or "INDEX" in indicator_upper:
            unit = "index"
        else:
            unit = "value"

        # Extract last updated date (defensive check for None)
        meta_info = data.get("meta", {}) if data else {}
        last_updated = meta_info.get("prepared", "") if meta_info else ""

        # Human-readable URL for data verification on OECD Data Explorer
        source_url = f"https://data-explorer.oecd.org/vis?lc=en&df[ds]=dsDisseminateFinalDMZ&df[id]={dataflow}&df[ag]={agency}"

        # Determine seasonal adjustment status from dimension values if available
        seasonal_adjustment = None
        for dim in dimensions:
            dim_id = dim.get("id", "")
            if dim_id in ["SEASONAL_ADJUSTMENT", "ADJUSTMENT", "ADJ"]:
                # Check if any dimension value indicates seasonal adjustment
                dim_values = dim.get("values", [])
                if dim_values:
                    # Look for SA (seasonally adjusted) or NSA (not seasonally adjusted)
                    for val in dim_values:
                        val_id = val.get("id", "")
                        if val_id in ["SA", "SEASONALLY_ADJUSTED"]:
                            seasonal_adjustment = "Seasonally adjusted"
                            break
                        elif val_id in ["NSA", "NOT_SEASONALLY_ADJUSTED"]:
                            seasonal_adjustment = "Not seasonally adjusted"
                            break

        # Determine data type from indicator name and transformation
        data_type = None
        if expected_transform and "GRW" in expected_transform:
            data_type = "Percent Change"
        elif "INDEX" in indicator_upper or "PRICE" in indicator_upper:
            data_type = "Index"
        elif "RATE" in indicator_upper or indicator_upper in ["UNEMPLOYMENT", "INFLATION"]:
            data_type = "Rate"
        else:
            data_type = "Level"

        # Determine price type from indicator name
        price_type = None
        indicator_name_lower = (structure.get("name", indicator) if structure else indicator).lower()
        if "constant" in indicator_name_lower or "real" in indicator_name_lower or "chained" in indicator_name_lower:
            price_type = "Constant prices"
        elif "current" in indicator_name_lower or "nominal" in indicator_name_lower:
            price_type = "Current prices"

        # Use indicator name as description
        description = structure.get("name", indicator) if structure else indicator

        # Extract start and end dates from data points
        start_date = data_points[0]["date"] if data_points else None
        end_date = data_points[-1]["date"] if data_points else None

        metadata = Metadata(
            source="OECD",
            indicator=structure.get("name", indicator) if structure else indicator,
            country=country_code,
            frequency=frequency,
            unit=unit,
            lastUpdated=last_updated,
            apiUrl=url,
            sourceUrl=source_url,
            seasonalAdjustment=seasonal_adjustment,
            dataType=data_type,
            priceType=price_type,
            description=description,
            notes=None,
            startDate=start_date,
            endDate=end_date,
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_multi_country(
        self,
        indicator: str,
        countries: Optional[List[str]] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> List[NormalizedData]:
        """Fetch indicator data for multiple OECD countries in parallel.

        Args:
            indicator: Indicator type (GDP, UNEMPLOYMENT, INFLATION)
            countries: List of country codes or names. If None, tries OECD aggregate first,
                       then falls back to major economies. Use ["ALL_OECD"] to fetch all members.
            start_year: Start year for data range
            end_year: End year for data range

        Returns:
            List of NormalizedData objects, one per country
        """
        # IMPORTANT: Fetching all 38 OECD countries individually causes severe rate limiting
        # (~5+ minutes due to rate limits: 8 requests/minute with 5s min delay)
        #
        # Strategy:
        # 1. If no countries specified, try OECD aggregate code first (most dataflows support this)
        # 2. If OECD aggregate fails, fall back to G7 countries (7 major economies)
        # 3. Only fetch all 38 countries when explicitly requested via "ALL_OECD"

        # Major OECD economies for fallback (G7 + major EU + Asia-Pacific)
        MAJOR_OECD_ECONOMIES = ["USA", "DEU", "JPN", "GBR", "FRA", "ITA", "CAN", "KOR", "AUS"]

        # CRITICAL FIX: ALWAYS try OECD aggregate first to avoid rate limiting
        # Many OECD dataflows support aggregate data with country code "OECD"
        # This prevents hitting rate limits from fetching 38+ individual countries

        # Step 1: Determine the target countries
        if not countries:
            # No countries specified - will try aggregate then major economies
            target_countries = None
        else:
            # Countries specified - expand them
            if len(countries) == 1:
                country_upper = countries[0].upper().replace(" ", "_")
                # Check if it's a special marker for OECD-wide data
                if country_upper in ("OECD", "ALL_OECD", "ALL_OECD_COUNTRIES", "OECD_COUNTRIES", "OECD_AVERAGE", "OECD AVERAGE"):
                    target_countries = None  # Will use aggregate
                else:
                    # Single country/region - expand it
                    target_countries = self.expand_countries(countries[0])
            else:
                # Multiple countries - expand each
                expanded_codes: List[str] = []
                for country in countries:
                    codes = self.expand_countries(country)
                    for code in codes:
                        if code not in expanded_codes:
                            expanded_codes.append(code)
                target_countries = expanded_codes

        # Step 2: ALWAYS try OECD aggregate first (unless single specific country requested)
        # This is the most efficient approach and avoids rate limiting
        should_try_aggregate = (
            target_countries is None or  # No countries specified
            len(target_countries) > 1 or  # Multiple countries (likely want comparison)
            "ALL" in str(target_countries).upper()  # Requesting all countries
        )

        if should_try_aggregate:
            logger.info(f"🌍 Trying OECD aggregate first for {indicator} (to avoid rate limiting)")
            try:
                result = await self.fetch_indicator(
                    indicator=indicator,
                    country="OECD",
                    start_year=start_year,
                    end_year=end_year,
                )
                logger.info(f"✅ OECD aggregate data retrieved for {indicator}")
                return [result]
            except Exception as e:
                logger.warning(
                    f"⚠️ OECD aggregate not available for {indicator}: {type(e).__name__}: {str(e)[:100]}. "
                    f"Falling back to individual country queries."
                )

        # Step 3: Determine country codes to fetch
        if target_countries is None:
            # Aggregate failed and no countries specified - use major economies
            country_codes = MAJOR_OECD_ECONOMIES
            logger.info(f"📊 Fetching {indicator} for {len(country_codes)} major OECD economies")
        elif len(target_countries) > 20:
            # Too many countries would hit rate limit - use major economies instead
            logger.warning(
                f"⚠️ {len(target_countries)} countries requested, but this would hit rate limits. "
                f"Using {len(MAJOR_OECD_ECONOMIES)} major OECD economies instead."
            )
            country_codes = MAJOR_OECD_ECONOMIES
        else:
            country_codes = target_countries
            logger.info(f"📊 Fetching {indicator} for {len(country_codes)} countries: {country_codes}")

        # Create fetch tasks for each country
        async def fetch_country_data(country_code: str) -> Optional[NormalizedData]:
            """Fetch data for a single country with error handling"""
            try:
                return await self.fetch_indicator(
                    indicator=indicator,
                    country=country_code,
                    start_year=start_year,
                    end_year=end_year,
                )
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch {indicator} for {country_code}: {e}")
                return None

        # Fetch all countries in parallel with rate limiting
        # Use semaphore to limit concurrent requests (OECD has strict rate limits)
        semaphore = asyncio.Semaphore(5)  # Max 5 concurrent requests

        async def fetch_with_semaphore(country_code: str):
            async with semaphore:
                return await fetch_country_data(country_code)

        tasks = [fetch_with_semaphore(country_code) for country_code in country_codes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out None results and exceptions
        successful_results = []
        failed_count = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"⚠️ Exception for {country_codes[i]}: {result}")
                failed_count += 1
            elif result is not None:
                successful_results.append(result)
            else:
                failed_count += 1

        if not successful_results:
            raise DataNotAvailableError(
                f"Failed to retrieve {indicator} data for any OECD country. "
                f"All {len(country_codes)} requests failed. "
                f"This may be due to rate limiting or data availability issues."
            )

        if failed_count > 0:
            logger.warning(
                f"⚠️ Retrieved data for {len(successful_results)}/{len(country_codes)} countries. "
                f"{failed_count} failed."
            )
        else:
            logger.info(f"✅ Successfully fetched {indicator} for {len(successful_results)} countries")

        return successful_results
