from __future__ import annotations

import logging
from typing import Dict, List, Optional, TYPE_CHECKING

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


class BISProvider(BaseProvider):
    """Bank for International Settlements (BIS) Statistics API provider.

    Uses the BIS SDMX REST API to retrieve banking and financial statistics.
    No API key required for basic access.

    API Documentation: https://stats.bis.org/api/v1/
    """

    # BIS supported countries (ISO 2-letter codes)
    # Infrastructure fix: Explicitly define coverage for early error detection
    # This enables proper fallback to alternative providers for unsupported countries
    BIS_SUPPORTED_COUNTRIES: frozenset = frozenset({
        # Major economies
        "AE", "AR", "AT", "AU", "BE", "BG", "BR", "CA", "CH", "CL", "CN", "CO",
        "CZ", "DE", "DK", "EE", "EG", "ES", "FI", "FR", "GB", "GR", "HK", "HR",
        "HU", "ID", "IE", "IL", "IN", "IT", "JP", "KE", "KR", "LT", "LV", "LU",
        "MT", "MX", "MY", "NL", "NO", "NZ", "PH", "PL", "PT", "RO", "RU", "SA",
        "SE", "SG", "SK", "SI", "TH", "TR", "TW", "US", "VN", "ZA",
        # Special regions
        "XM",  # Euro Area
    })

    # Common indicators mapped to BIS dataflow codes
    # NOTE: Only include VERIFIED WORKING dataflows (tested 2025-11-15)
    # Many BIS metadata catalog indicators don't actually exist or have no data
    INDICATOR_MAPPINGS: Dict[str, str] = {
        # ✅ VERIFIED WORKING:
        "POLICY_RATE": "WS_CBPOL",  # Central bank policy rates (monthly)
        "INTEREST_RATE": "WS_CBPOL",
        "CB_POLICY_RATE": "WS_CBPOL",
        "CENTRAL_BANK_POLICY_RATES": "WS_CBPOL",

        "TOTAL_CREDIT": "WS_TC",  # Total credit to private sector (quarterly)
        "CREDIT": "WS_TC",
        "CREDIT_DATA": "WS_TC",
        "CREDIT_TO_GDP": "WS_TC",  # Credit to GDP ratio
        "CREDIT_GDP_RATIO": "WS_TC",
        "CREDIT_TO_GDP_RATIO": "WS_TC",
        "CREDIT_TO_NON-FINANCIAL_SECTOR": "WS_TC",
        "CREDIT_TO_NON-FINANCIAL_SECTOR_AS_PERCENTAGE_OF_GDP": "WS_TC",
        "CREDIT_TO_PRIVATE_SECTOR": "WS_TC",  # Synonym for credit to non-financial sector
        "PRIVATE_SECTOR_CREDIT": "WS_TC",
        "PRIVATE_CREDIT": "WS_TC",
        # Additional keyword variations for better matching
        "CREDIT_TO_PRIVATE_NON-FINANCIAL_SECTOR": "WS_TC",
        "CREDIT_TO_PRIVATE_NON_FINANCIAL_SECTOR": "WS_TC",
        "PRIVATE_NON-FINANCIAL_SECTOR": "WS_TC",
        "PRIVATE_NON_FINANCIAL_SECTOR": "WS_TC",
        "NON-FINANCIAL_SECTOR_CREDIT": "WS_TC",
        "NON_FINANCIAL_SECTOR_CREDIT": "WS_TC",
        "TOTAL_CREDIT_TO_PRIVATE": "WS_TC",
        "CREDIT_GAP": "WS_TC",  # Map credit gap queries to total credit (closest match)
        # Bank credit growth mappings
        "BANK_CREDIT": "WS_TC",  # Use total credit for bank credit queries
        "BANK_CREDIT_GROWTH": "WS_TC",
        "CREDIT_GROWTH": "WS_TC",
        "BANKING_CREDIT": "WS_TC",

        "PROPERTY_PRICES": "WS_SPP",  # Residential property prices (quarterly)
        "PROPERTY_PRICE": "WS_SPP",
        "RESIDENTIAL_PROPERTY_PRICE": "WS_SPP",
        "RESIDENTIAL_PROPERTY_PRICES": "WS_SPP",
        "HOUSE_PRICES": "WS_SPP",
        "HOUSING_PRICES": "WS_SPP",
        "HOUSING_PRICE": "WS_SPP",
        "REAL_ESTATE_PRICES": "WS_SPP",
        "REAL_ESTATE_PRICE": "WS_SPP",
        "HOUSING_MARKET": "WS_SPP",
        "HOUSING_MARKET_INDEX": "WS_SPP",
        "HOUSE_PRICE_INDEX": "WS_SPP",
        "PROPERTY_PRICE_INDEX": "WS_SPP",

        "EXCHANGE_RATE": "WS_XRU",  # Effective exchange rate indices (monthly)
        "EXCHANGE_RATES": "WS_XRU",
        "EFFECTIVE_EXCHANGE_RATES": "WS_XRU",
        "EXCHANGE_RATE_INDICES": "WS_XRU",

        "CONSUMER_PRICES": "WS_LONG_CPI",  # Long series on consumer prices (monthly)
        "CPI": "WS_LONG_CPI",
        "INFLATION": "WS_LONG_CPI",

        "DEBT_SERVICE_RATIO": "WS_DSR",  # Debt service ratios (quarterly)
        "DSR": "WS_DSR",
        "DEBT_SERVICE": "WS_DSR",
        "DEBT_SERVICE_RATIOS": "WS_DSR",

        "GLOBAL_LIQUIDITY": "WS_GLI",  # Global liquidity indicators (quarterly)
        "LIQUIDITY": "WS_GLI",
        "LIQUIDITY_INDICATORS": "WS_GLI",
        "GLI": "WS_GLI",

        "DEBT_SECURITIES": "WS_DEBT_SEC2_PUB",  # International debt securities (quarterly)
        "INTERNATIONAL_DEBT_SECURITIES": "WS_DEBT_SEC2_PUB",
        "INTERNATIONAL_DEBT": "WS_DEBT_SEC2_PUB",
        "DEBT_SEC": "WS_DEBT_SEC2_PUB",

        # Household and corporate debt - mapped to total credit (WS_TC has this breakdown)
        "HOUSEHOLD_DEBT": "WS_TC",  # TC dataset has household sector breakdown
        "HOUSEHOLD_CREDIT": "WS_TC",
        "CONSUMER_DEBT": "WS_TC",
        "CORPORATE_DEBT": "WS_TC",  # TC dataset has corporate sector breakdown
        "CORPORATE_CREDIT": "WS_TC",
        "BUSINESS_DEBT": "WS_TC",
        "NONFINANCIAL_CORPORATE_DEBT": "WS_TC",
        "NON_FINANCIAL_CORPORATE_DEBT": "WS_TC",
        "DEBT": "WS_TC",  # Generic debt queries
        "DEBT_RATIO": "WS_TC",
        "DEBT_TO_GDP": "WS_TC",

        # ❌ REMOVED (tested and don't work):
        # "WS_CREDIT_GAP" - No data available, use WS_TC instead
        # Other indicators from metadata catalog also don't work
    }

    # Indicators that BIS doesn't have - redirect to other providers
    # These trigger helpful error messages with alternative data sources
    REDIRECT_INDICATORS: Dict[str, str] = {
        "PRODUCTIVITY": "OECD or WorldBank",
        "LABOR_PRODUCTIVITY": "OECD or WorldBank",
        "LABOUR_PRODUCTIVITY": "OECD or WorldBank",
        "OUTPUT_PER_WORKER": "OECD or WorldBank",
        "GDP_PER_WORKER": "OECD or WorldBank",
        "WORKER_PRODUCTIVITY": "OECD or WorldBank",
        "PRODUCTIVITY_GROWTH": "OECD or WorldBank",
        "LABOR_PRODUCTIVITY_GROWTH": "OECD or WorldBank",
        "UNIT_LABOR_COST": "OECD or Eurostat",
        "UNIT_LABOUR_COST": "OECD or Eurostat",
        "ULC": "OECD or Eurostat",
    }

    # Country code mappings (BIS uses ISO 2-letter codes)
    # Comprehensive mappings for common country names to ISO 3166-1 alpha-2 codes
    COUNTRY_MAPPINGS: Dict[str, str] = {
        # North America
        "USA": "US",
        "UNITED STATES": "US",
        "UNITED_STATES": "US",
        "CANADA": "CA",
        "MEXICO": "MX",

        # Europe
        "AUSTRIA": "AT",
        "BELGIUM": "BE",
        "BRITAIN": "GB",
        "UK": "GB",
        "UNITED KINGDOM": "GB",
        "UNITED_KINGDOM": "GB",
        "DENMARK": "DK",
        "FINLAND": "FI",
        "FRANCE": "FR",
        "GERMANY": "DE",
        "GREECE": "GR",
        "IRELAND": "IE",
        "ITALY": "IT",
        "LUXEMBOURG": "LU",
        "NETHERLANDS": "NL",
        "NORWAY": "NO",
        "PORTUGAL": "PT",
        "SPAIN": "ES",
        "SWEDEN": "SE",
        "SWITZERLAND": "CH",
        "CZECH": "CZ",
        "CZECHIA": "CZ",
        "CZECH REPUBLIC": "CZ",
        "HUNGARY": "HU",
        "POLAND": "PL",
        "ROMANIA": "RO",
        "SLOVAKIA": "SK",
        "SLOVENIA": "SI",

        # Asia-Pacific
        "AUSTRALIA": "AU",
        "CHINA": "CN",
        "HONG KONG": "HK",
        "HONGKONG": "HK",
        "INDIA": "IN",
        "INDONESIA": "ID",
        "JAPAN": "JP",
        "KOREA": "KR",
        "SOUTH_KOREA": "KR",
        "SOUTH KOREA": "KR",
        "MALAYSIA": "MY",
        "NEW ZEALAND": "NZ",
        "NEWZEALAND": "NZ",
        "PHILIPPINES": "PH",
        "SINGAPORE": "SG",
        "SOUTH AFRICA": "ZA",
        "SOUTHAFRICA": "ZA",
        "THAILAND": "TH",
        "VIETNAM": "VN",

        # Middle East
        "SAUDI ARABIA": "SA",
        "SAUDIARABIA": "SA",
        "UNITED ARAB EMIRATES": "AE",
        "UAE": "AE",
        "ISRAEL": "IL",
        "TURKEY": "TR",

        # Americas
        "ARGENTINA": "AR",
        "BRAZIL": "BR",
        "CHILE": "CL",
        "COLOMBIA": "CO",
        "PERU": "PE",

        # Africa
        "SOUTH AFRICA": "ZA",
        "SOUTHAFRICA": "ZA",
        "EGYPT": "EG",
        "NIGERIA": "NG",
        "KENYA": "KE",

        # Special
        "RUSSIA": "RU",
        "EURO AREA": "XM",
        "EUROAREA": "XM",
        "EURO_AREA": "XM",
    }

    # Euro area countries that should use "XM" (Euro area) for monetary data after 1999
    EUROZONE_COUNTRIES: set = {
        "AT", "BE", "CY", "EE", "FI", "FR", "DE", "GR", "IE", "IT",
        "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES"
    }

    # Regional groupings for multi-country queries
    # Using ISO Alpha-2 codes (compatible with CountryResolver)
    REGION_MAPPINGS: Dict[str, List[str]] = {
        # Major country groupings
        "G7": ["CA", "FR", "DE", "IT", "JP", "GB", "US"],
        "G7_COUNTRIES": ["CA", "FR", "DE", "IT", "JP", "GB", "US"],
        "G20": ["AR", "AU", "BR", "CA", "CN", "FR", "DE", "IN", "ID", "IT", "JP", "KR", "MX", "RU", "SA", "ZA", "TR", "GB", "US"],
        # BRICS
        "BRICS": ["BR", "RU", "IN", "CN", "ZA"],
        "BRICS_COUNTRIES": ["BR", "RU", "IN", "CN", "ZA"],
        # European groupings
        "EUROPE": ["AT", "BE", "CH", "CZ", "DE", "DK", "ES", "FI", "FR", "GB", "GR", "HU", "IE", "IT", "NL", "NO", "PL", "PT", "RO", "SE", "SK", "SI"],
        "EUROZONE": ["AT", "BE", "CY", "EE", "FI", "FR", "DE", "GR", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES"],
        "EURO_AREA": ["AT", "BE", "CY", "EE", "FI", "FR", "DE", "GR", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES"],
        "EU": ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"],
        # Nordic countries
        "NORDIC": ["DK", "FI", "IS", "NO", "SE"],
        "NORDIC_COUNTRIES": ["DK", "FI", "IS", "NO", "SE"],
        "SCANDINAVIA": ["DK", "NO", "SE"],
        # ASEAN
        "ASEAN": ["BN", "KH", "ID", "LA", "MY", "MM", "PH", "SG", "TH", "VN"],
        "ASEAN_COUNTRIES": ["BN", "KH", "ID", "LA", "MY", "MM", "PH", "SG", "TH", "VN"],
        # Asia-Pacific
        "ASIA_PACIFIC": ["AU", "CN", "HK", "ID", "IN", "JP", "KR", "MY", "NZ", "PH", "SG", "TH"],
        "APAC": ["AU", "CN", "HK", "ID", "IN", "JP", "KR", "MY", "NZ", "PH", "SG", "TH"],
    }

    @property
    def provider_name(self) -> str:
        return "BIS"

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        settings = get_settings()
        self.base_url = settings.bis_base_url.rstrip("/")
        self.metadata_search = metadata_search_service

    async def _fetch_data(self, **params) -> List[NormalizedData]:
        """Implement BaseProvider interface by routing to fetch_indicator."""
        return await self.fetch_indicator(
            indicator=params.get("indicator", "POLICY_RATE"),
            country=params.get("country"),
            countries=params.get("countries"),
            start_year=params.get("start_year"),
            end_year=params.get("end_year"),
            frequency=params.get("frequency", "M"),
        )

    def _indicator_code(self, indicator: str) -> Optional[str]:
        """Get BIS dataflow code from common indicator name."""
        key = indicator.upper().replace(" ", "_")
        return self.INDICATOR_MAPPINGS.get(key)

    def _country_code(self, country: str) -> str:
        """Get BIS country code (ISO 2-letter) from common country name.

        Uses CountryResolver as primary source for individual country normalization.
        """
        # CENTRALIZED: Try CountryResolver first (single source of truth)
        try:
            from ..routing.country_resolver import CountryResolver
            iso_code = CountryResolver.normalize(country)
            if iso_code and len(iso_code) == 2:
                return iso_code
        except Exception:
            pass

        # Fallback to local mappings for BIS-specific cases
        key = country.upper().replace(" ", "_")
        mapped = self.COUNTRY_MAPPINGS.get(key)
        if mapped:
            return mapped
        # If already 2-letter code and not in mappings, return as-is
        if len(country) == 2:
            return country.upper()
        return country.upper()

    def _expand_region(self, region: str) -> List[str]:
        """Expand regional name to list of country codes.

        Uses CountryResolver as the single source of truth for region definitions.
        Falls back to BIS-specific mappings for groups not in CountryResolver.

        Args:
            region: Region name (e.g., "Europe", "EU", "Eurozone")

        Returns:
            List of ISO 2-letter country codes for the region, or [region] if not a known region
        """
        from ..routing.country_resolver import CountryResolver

        key = region.upper().replace(" ", "_")

        # First, try CountryResolver (single source of truth for standard regions)
        expanded = CountryResolver.get_region_expansion(key, format="iso2")
        if expanded:
            logger.info(f"🌍 Expanding region '{region}' via CountryResolver → {len(expanded)} countries")
            return expanded

        # Try variant names
        for variant in [key, key.replace("_COUNTRIES", ""), key.replace("_NATIONS", "")]:
            expanded = CountryResolver.get_region_expansion(variant, format="iso2")
            if expanded:
                logger.info(f"🌍 Matched region '{variant}' via CountryResolver → {len(expanded)} countries")
                return expanded

        # Fall back to BIS-specific region mappings (EUROPE with specific list)
        if key in self.REGION_MAPPINGS:
            countries = self.REGION_MAPPINGS[key]
            logger.info(f"🌍 Expanding region '{region}' via BIS mappings → {len(countries)} countries")
            return countries

        return [region]  # Not a region, return as-is

    async def fetch_indicator(
        self,
        indicator: str,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        frequency: str = "M",  # M=Monthly, Q=Quarterly, A=Annual
    ) -> List[NormalizedData]:
        """Fetch financial indicator data from BIS Statistics API.

        Args:
            indicator: Indicator name (e.g., "POLICY_RATE", "CREDIT_GAP") or BIS dataflow code
            country: Single country name or ISO 2-letter code
            countries: List of country names or ISO 2-letter codes (for multi-country queries)
            start_year: Start year (optional)
            end_year: End year (optional)
            frequency: Data frequency - M (monthly), Q (quarterly), A (annual)

        Returns:
            List of NormalizedData objects (one per country)
        """
        # Normalize frequency to single-letter code (handles both "M" and "monthly")
        freq_map = {
            "monthly": "M", "month": "M", "m": "M",
            "quarterly": "Q", "quarter": "Q", "q": "Q",
            "annual": "A", "yearly": "A", "year": "A", "a": "A",
        }
        frequency = freq_map.get(frequency.lower(), frequency.upper()[0]) if frequency else "M"

        indicator_code, indicator_label = await self._resolve_indicator_code(indicator)

        # Expand regions to country lists
        if countries:
            # Expand each item in countries list (could be regions or countries)
            expanded_countries = []
            for c in countries:
                expanded_countries.extend(self._expand_region(c))
            country_list = expanded_countries
            logger.info(f"🌍 BIS: Expanded {countries} to {len(country_list)} countries")
        elif country:
            # Expand single country parameter (could be a region)
            country_list = self._expand_region(country)
            if len(country_list) > 1:
                logger.info(f"🌍 BIS: Expanded '{country}' to {len(country_list)} countries: {country_list[:5]}...")
        else:
            country_list = ["US"]  # Default to US if no country specified

        # INFRASTRUCTURE FIX: Early coverage check for unsupported countries
        # This enables proper fallback to alternative providers (FRED, World Bank, IMF)
        # instead of silently returning empty results
        normalized_countries = [self._country_code(c) for c in country_list]
        unsupported = [c for c in normalized_countries if c not in self.BIS_SUPPORTED_COUNTRIES]
        if unsupported and len(unsupported) == len(normalized_countries):
            # ALL requested countries are unsupported - raise error for fallback
            unsupported_names = ", ".join(unsupported[:5])
            if len(unsupported) > 5:
                unsupported_names += f", ... ({len(unsupported)} total)"
            raise DataNotAvailableError(
                f"BIS doesn't have data for: {unsupported_names}. "
                f"For US policy rates, try FRED. For global interest rates, try World Bank (deposit/lending rates as proxy)."
            )
        elif unsupported:
            # Some countries unsupported - log warning but continue with supported ones
            logger.warning(f"⚠️ BIS: {len(unsupported)} countries not supported: {unsupported[:5]}")
            country_list = [c for c in country_list if self._country_code(c) in self.BIS_SUPPORTED_COUNTRIES]

        results: List[NormalizedData] = []

        # Auto-detect frequency based on indicator (some only have specific frequencies)
        # BIS indicators have specific data frequencies that MUST be matched:
        # - Monthly only: WS_CBPOL (policy rates), WS_LONG_CPI (consumer prices), WS_XRU (exchange rates)
        # - Quarterly only: WS_TC (credit), WS_SPP (property prices), WS_DSR (debt service), etc.
        if indicator_code in ["WS_CBPOL", "WS_LONG_CPI", "WS_XRU"]:
            frequency = "M"  # Force monthly for these indicators
            logger.info(f"BIS: Forced monthly frequency for {indicator_code}")
        elif indicator_code in ["WS_TC", "WS_SPP", "WS_CPP", "WS_DPP", "WS_DSR", "WS_GLI", "WS_DEBT_SEC2_PUB"]:
            frequency = "Q"  # Force quarterly for these indicators
            logger.info(f"BIS: Forced quarterly frequency for {indicator_code}")

        # Special handling for indicators that don't use country codes in standard way
        # WS_GLI (Global Liquidity Indicators) uses complex multi-dimensional structure
        if indicator_code == "WS_GLI":
            # GLI doesn't filter by single country, get all data
            return await self._fetch_gli_data(start_year, end_year)

        # Loop through each country
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        for country_code_raw in country_list:
            country_code = self._country_code(country_code_raw)

            # Build SDMX query key based on indicator type
            # Format: data/{dataflow}/{freq}.{country}?startPeriod=YYYY&endPeriod=YYYY
            params = {}
            if start_year:
                params["startPeriod"] = str(start_year)
            if end_year:
                params["endPeriod"] = str(end_year)

            # For Eurozone countries requesting monetary indicators,
            # try Euro area (XM) if the country-specific data is outdated or unavailable
            country_codes_to_try = [country_code]
            if country_code in self.EUROZONE_COUNTRIES and indicator_code in ["WS_CBPOL", "WS_LONG_CPI"]:
                # Try country first, then Euro area as fallback
                country_codes_to_try.append("XM")

            result_found = False
            for current_country_code in country_codes_to_try:
                if result_found:
                    break

                # Construct the SDMX data query
                # All BIS dataflows use standard structure: freq.country
                sdmx_key = f"{frequency}.{current_country_code}"

                url = f"{self.base_url}/data/{indicator_code}/{sdmx_key}"

                try:
                    # First attempt: with date parameters
                    response = await client.get(url, params=params, headers={
                        "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
                    }, timeout=30.0)

                    # Check response before parsing JSON
                    payload = None
                    if response.status_code == 200 and response.content:
                        try:
                            payload = response.json()
                        except Exception:
                            payload = None

                    # Some BIS dataflows don't support startPeriod/endPeriod parameters
                    # If we get an error or empty response, retry without date filters
                    if payload is None or "errors" in payload or response.status_code != 200:
                        # Retry without date parameters
                        response = await client.get(url, headers={
                            "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
                        }, timeout=30.0)
                        if response.status_code != 200 or not response.content:
                            logger.debug(f"BIS: No data for {current_country_code} (status: {response.status_code})")
                            continue  # Try next country code
                        try:
                            payload = response.json()
                        except Exception as json_err:
                            logger.debug(f"BIS: JSON parse error for {current_country_code}: {json_err}")
                            continue  # Try next country code

                    # Parse SDMX-JSON format
                    if "data" not in payload or "dataSets" not in payload["data"]:
                        continue  # Try next country code

                    datasets = payload["data"]["dataSets"]
                    if not datasets or "series" not in datasets[0]:
                        continue  # Try next country code

                    # Extract time dimension and observations
                    structure = payload["data"]["structure"]
                    dimensions = structure["dimensions"]["observation"]
                    time_dimension = next((d for d in dimensions if d["id"] == "TIME_PERIOD"), None)

                    if not time_dimension:
                        continue  # Try next country code

                    time_values = time_dimension["values"]

                    # Get all series data
                    series_data = datasets[0]["series"]
                    if not series_data:
                        continue  # Try next country code

                    # BIS often returns multiple series with different dimension combinations
                    # We need to find the most relevant series or use the first one with data
                    # Get series dimensions to interpret keys
                    series_dimensions = structure["dimensions"].get("series", [])

                    # Find the best series (prefer unadjusted, market value, domestic currency)
                    best_series_key, observations = self._select_best_series(
                        series_data, series_dimensions, indicator_code
                    )

                    if not observations:
                        continue  # Try next country code

                    # Build data points
                    data_points = []
                    for time_idx_str, obs_data in observations.items():
                        try:
                            time_idx = int(time_idx_str)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid time index '{time_idx_str}' in BIS response, skipping")
                            continue
                        if time_idx < len(time_values):
                            time_period = time_values[time_idx]["id"]

                            # Extract value from observation data
                            # BIS observation format: [value_str, status1, status2, status3]
                            value = None
                            if obs_data and len(obs_data) > 0:
                                try:
                                    value_str = obs_data[0]
                                    if value_str is not None and value_str != "":
                                        value = float(value_str)
                                except (ValueError, TypeError, IndexError):
                                    value = None

                            # Convert time period to ISO date format
                            # BIS uses formats like "2020-01", "2020-Q1", "2020"
                            if "-" in time_period:
                                if "Q" in time_period:
                                    # Quarterly: "2020-Q1" -> "2020-01-01"
                                    year, quarter = time_period.split("-Q")
                                    month = (int(quarter) - 1) * 3 + 1
                                    date_str = f"{year}-{month:02d}-01"
                                    year_int = int(year)
                                else:
                                    # Monthly: "2020-01" -> "2020-01-01"
                                    date_str = f"{time_period}-01"
                                    year_int = int(time_period.split("-")[0])
                            else:
                                # Annual: "2020" -> "2020-01-01"
                                date_str = f"{time_period}-01-01"
                                year_int = int(time_period)

                            # Filter by date range if specified (when API didn't filter)
                            if start_year and year_int < start_year:
                                continue
                            if end_year and year_int > end_year:
                                continue

                            data_points.append({
                                "date": date_str,
                                "value": value
                            })

                    if not data_points:
                        continue  # Try next country code

                    # Determine frequency label
                    freq_label = {"M": "monthly", "Q": "quarterly", "A": "annual"}.get(frequency, frequency)

                    # Determine unit based on indicator
                    if indicator_code == "WS_CBPOL":
                        unit = "percent"
                    elif indicator_code in ["WS_LONG_CPI", "WS_CPP"]:
                        unit = "index" # Consumer/producer price indices
                    elif indicator_code == "WS_XRU":
                        unit = "index" # Effective exchange rate index
                    elif indicator_code == "WS_TC":
                        unit = "percent of GDP" # Total credit to GDP ratio
                    elif indicator_code == "WS_SPP":
                        unit = "index" # Property price index
                    else:
                        unit = ""

                    # Build API URL for reproducibility
                    api_url = f"{self.base_url}/data/{indicator_code}/{sdmx_key}"
                    if params:
                        param_str = "&".join(f"{k}={v}" for k, v in params.items())
                        api_url += f"?{param_str}"

                    # Determine indicator name
                    indicator_name = indicator_label or indicator_code

                    # Use original country code or Euro area if fallback was used
                    display_country = current_country_code

                    # Human-readable URL for data verification on BIS Data Portal
                    # Map dataflow codes to topic URLs (verified 2025-11)
                    # See: https://data.bis.org/topics for available topics
                    topic_map = {
                        "WS_CBPOL": "CBPOL",           # Central bank policy rates
                        "WS_TC": "TOTAL_CREDIT",       # Total credit to non-financial sector
                        "WS_SPP": "RPP",               # Residential Property Prices (NOT PROPERTY_PRICES)
                        "WS_XRU": "EER",               # Effective exchange rates
                        "WS_LONG_CPI": "CPI",          # Consumer prices
                        "WS_GLI": "GLI",               # Global liquidity indicators
                        "WS_DSR": "DSR",               # Debt service ratios
                        "WS_DEBT_SEC2_PUB": "SEC_PUB", # International debt securities
                    }
                    topic = topic_map.get(indicator_code, indicator_code)
                    source_url = f"https://data.bis.org/topics/{topic}"

                    # Enhanced metadata fields
                    # Only mark as seasonally adjusted for level data, NOT ratio/percentage data
                    if indicator_code == "WS_TC":
                        # Credit-to-GDP ratios (percent) are NOT seasonally adjusted
                        # Only level data in local currency would be seasonally adjusted
                        if "percent" in unit.lower() or "gdp" in unit.lower():
                            seasonal_adjustment = None
                        else:
                            seasonal_adjustment = "Seasonally Adjusted"
                    else:
                        seasonal_adjustment = None

                    # Determine data type based on indicator
                    if indicator_code == "WS_CBPOL":
                        data_type = "Rate"
                    elif indicator_code in ["WS_LONG_CPI", "WS_CPP", "WS_XRU", "WS_SPP"]:
                        data_type = "Index"
                    elif indicator_code in ["WS_TC", "WS_DSR", "WS_GLI", "WS_DEBT_SEC2_PUB"]:
                        data_type = "Level"
                    else:
                        data_type = None

                    # Determine price type for real price indices
                    price_type = "Real (inflation-adjusted)" if indicator_code in ["WS_SPP", "WS_CPP"] and "real" in indicator_name.lower() else None

                    # Extract start and end dates from data points
                    start_date = data_points[0]["date"] if data_points else None
                    end_date = data_points[-1]["date"] if data_points else None

                    metadata = Metadata(
                        source="BIS",
                        indicator=indicator_name,
                        country=display_country,
                        frequency=freq_label,
                        unit=unit,
                        lastUpdated="",  # BIS doesn't provide explicit last updated date
                        apiUrl=api_url,
                        sourceUrl=source_url,
                        seasonalAdjustment=seasonal_adjustment,
                        dataType=data_type,
                        priceType=price_type,
                        description=indicator_name,
                        notes=None,
                        startDate=start_date,
                        endDate=end_date,
                    )

                    results.append(NormalizedData(metadata=metadata, data=data_points))
                    result_found = True  # Successfully got data, don't try other country codes

                except Exception:
                    # Try next country code if any error occurs
                    continue

        # INFRASTRUCTURE FIX: Raise error for empty results to trigger fallback
        # This enables the query orchestrator to try alternative providers
        if not results and country_list:
            country_names = ", ".join(country_list[:3])
            if len(country_list) > 3:
                country_names += f", ... ({len(country_list)} total)"
            raise DataNotAvailableError(
                f"BIS has no {indicator_label or indicator} data for: {country_names}. "
                f"Try World Bank or IMF for broader country coverage."
            )

        return results

    async def _fetch_gli_data(
        self,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> List[NormalizedData]:
        """Fetch Global Liquidity Indicators (WS_GLI).

        GLI has a multi-dimensional structure with currency, borrower country, sectors, etc.
        We'll fetch aggregate measures in USD.
        """
        results: List[NormalizedData] = []
        frequency = "Q"

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        # GLI query - just frequency, no country filter
        url = f"{self.base_url}/data/WS_GLI/{frequency}"
        params = {}
        if start_year:
            params["startPeriod"] = str(start_year)
        if end_year:
            params["endPeriod"] = str(end_year)

        try:
            response = await client.get(url, params=params, headers={
                "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
            }, timeout=30.0)

            # Retry without date params if error
            if response.status_code != 200:
                response = await client.get(url, headers={
                    "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
                }, timeout=30.0)

            response.raise_for_status()
            payload = response.json()

            if "data" not in payload or "dataSets" not in payload["data"]:
                return results

            datasets = payload["data"]["dataSets"]
            if not datasets or "series" not in datasets[0]:
                return results

            structure = payload["data"]["structure"]
            dimensions = structure["dimensions"]["observation"]
            time_dimension = next((d for d in dimensions if d["id"] == "TIME_PERIOD"), None)

            if not time_dimension:
                return results

            time_values = time_dimension["values"]
            series_data = datasets[0]["series"]

            if not series_data:
                return results

            # Select a representative series (total USD denominated)
            # Look for series with USD denomination and aggregate measures
            best_key, observations = self._select_best_series(
                series_data, structure["dimensions"].get("series", []), "WS_GLI"
            )

            if not observations:
                return results

            # Build data points
            data_points = []
            for time_idx_str, obs_data in observations.items():
                time_idx = int(time_idx_str)
                if time_idx < len(time_values):
                    time_period = time_values[time_idx]["id"]

                    value = None
                    if obs_data and len(obs_data) > 0:
                        try:
                            value_str = obs_data[0]
                            if value_str is not None and value_str != "":
                                value = float(value_str)
                        except (ValueError, TypeError, IndexError):
                            value = None

                    # Convert time period
                    if "Q" in time_period:
                        year, quarter = time_period.split("-Q")
                        month = (int(quarter) - 1) * 3 + 1
                        date_str = f"{year}-{month:02d}-01"
                        year_int = int(year)
                    else:
                        date_str = f"{time_period}-01-01"
                        year_int = int(time_period.split("-")[0])

                    if start_year and year_int < start_year:
                        continue
                    if end_year and year_int > end_year:
                        continue

                    data_points.append({
                        "date": date_str,
                        "value": value
                    })

            if data_points:
                # Human-readable URL for data verification on BIS Data Portal
                source_url = "https://data.bis.org/topics/GLI"

                # Enhanced metadata fields
                start_date = data_points[0]["date"] if data_points else None
                end_date = data_points[-1]["date"] if data_points else None

                metadata = Metadata(
                    source="BIS",
                    indicator="Global Liquidity Indicators",
                    country="Global",
                    frequency="quarterly",
                    unit="USD billions",
                    lastUpdated="",
                    apiUrl=f"{self.base_url}/data/WS_GLI/{frequency}",
                    sourceUrl=source_url,
                    seasonalAdjustment=None,
                    dataType="Level",
                    priceType=None,
                    description="Global Liquidity Indicators",
                    notes=None,
                    startDate=start_date,
                    endDate=end_date,
                )
                results.append(NormalizedData(metadata=metadata, data=data_points))

        except Exception:
            # Return empty list if error
            pass

        return results

    def _extract_indicator_keywords(self, indicator: str) -> str:
        """
        Extract core indicator keywords from phrases containing institution/country names.

        Examples:
            "Reserve Bank of Australia cash rate" → "cash rate"
            "European Central Bank deposit facility rate" → "deposit facility rate"
            "Bank of Japan policy rate" → "policy rate"
            "Federal Reserve interest rate" → "interest rate"
        """
        if not indicator:
            return indicator

        indicator_lower = indicator.lower()

        # Common central bank/institution patterns to remove
        institution_patterns = [
            "reserve bank of australia", "rba",
            "european central bank", "ecb",
            "bank of japan", "boj",
            "bank of england", "boe",
            "federal reserve", "fed",
            "bank of canada", "boc",
            "swiss national bank", "snb",
            "reserve bank of india", "rbi",
            "people's bank of china", "pboc",
            "bank of korea", "bok",
            "central bank of",
            "bank of",
        ]

        # Country patterns that often precede rate names
        country_patterns = [
            "australia", "australian",
            "european", "europe",
            "japan", "japanese",
            "uk", "united kingdom", "british",
            "us", "usa", "united states", "american",
            "canada", "canadian",
            "switzerland", "swiss",
            "india", "indian",
            "china", "chinese",
            "korea", "korean",
            "germany", "german",
            "france", "french",
        ]

        result = indicator_lower

        # Remove institution patterns
        for pattern in institution_patterns:
            if pattern in result:
                result = result.replace(pattern, "").strip()

        # Remove country patterns (but be careful not to remove from indicator names)
        for pattern in country_patterns:
            # Only remove if it's at the start or followed by space
            if result.startswith(pattern + " "):
                result = result[len(pattern):].strip()
            elif result.startswith(pattern + "'s "):
                result = result[len(pattern) + 3:].strip()

        # Clean up any leftover artifacts
        result = " ".join(result.split())  # Normalize whitespace

        # If we stripped too much, return original
        if len(result) < 3:
            return indicator

        return result

    async def _resolve_indicator_code(self, indicator: str) -> tuple[str, Optional[str]]:
        """Resolve BIS indicator code through hardcoded mappings, translator, or metadata search."""
        # Step 0: Check if indicator should be redirected to another provider
        indicator_key = indicator.upper().replace(" ", "_")
        if indicator_key in self.REDIRECT_INDICATORS:
            suggested_provider = self.REDIRECT_INDICATORS[indicator_key]
            raise DataNotAvailableError(
                f"BIS doesn't have {indicator} data. "
                f"For productivity and labor cost data, try: "
                f"• OECD (best for OECD countries): Has comprehensive productivity databases "
                f"• WorldBank (global coverage): Use indicator SL.GDP.PCAP.EM.KD (GDP per person employed) "
                f"• FRED (US only): Use series OPHNFB (Nonfarm Business Sector Labor Productivity)"
            )

        # Step 1: Try direct mapping
        mapped = self._indicator_code(indicator)
        if mapped:
            return mapped, indicator

        # Step 2: Allow users to supply raw BIS dataflow codes directly (uppercase)
        if indicator and indicator.upper() == indicator:
            return indicator, None

        # Step 3: Try cross-provider indicator translator (handles IMF codes, common names, etc.)
        translator = get_indicator_translator()

        # First try with original indicator
        translated_code, concept_name = translator.translate_indicator(indicator, "BIS")
        if translated_code:
            logger.info(f"BIS: Translated '{indicator}' to '{translated_code}' via concept '{concept_name}'")
            return translated_code, concept_name

        # Step 3b: Extract keywords from long phrases (e.g., "Reserve Bank of Australia cash rate" → "cash rate")
        extracted_indicator = self._extract_indicator_keywords(indicator)
        if extracted_indicator != indicator.lower():
            logger.info(f"BIS: Extracted '{extracted_indicator}' from '{indicator}'")
            translated_code, concept_name = translator.translate_indicator(extracted_indicator, "BIS")
            if translated_code:
                logger.info(f"BIS: Translated extracted '{extracted_indicator}' to '{translated_code}' via concept '{concept_name}'")
                return translated_code, concept_name

        if not self.metadata_search:
            raise DataNotAvailableError(
                f"BIS indicator '{indicator}' not recognized. Provide the BIS dataflow code (e.g., WS_CBPOL) or enable metadata discovery."
            )

        # Use hierarchical search: SDMX first, then BIS REST API
        search_results = await self.metadata_search.search_with_sdmx_fallback(
            provider="BIS",
            indicator=indicator,
        )
        if not search_results:
            raise DataNotAvailableError(
                f"BIS indicator '{indicator}' not found. Try a different description (e.g., 'policy rate')."
            )

        discovery = await self.metadata_search.discover_indicator(
            provider="BIS",
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
            f"BIS indicator '{indicator}' not found. Try refining your query or consult BIS Statistics for available datasets."
        )

    def _select_best_series(
        self,
        series_data: dict,
        series_dimensions: list,
        indicator_code: str
    ) -> tuple[str, dict]:
        """Select the most relevant series from multiple series.

        BIS returns multiple series with different dimension combinations.
        We select based on preferences:
        - For total credit (WS_TC): Private non-financial sector (P), percentage of GDP, adjusted
        - For property prices: Real prices, adjusted
        - For other indicators: First series with substantial data

        Returns:
            Tuple of (series_key, observations_dict)
        """
        if not series_data:
            return None, {}

        # Build dimension index for easier lookup
        dim_map = {}
        for i, dim in enumerate(series_dimensions):
            dim_id = dim.get("id")
            values = {v.get("id"): j for j, v in enumerate(dim.get("values", []))}
            dim_map[dim_id] = {"index": i, "values": values}

        # Define preferences for common indicators
        preferences = {}

        if indicator_code == "WS_TC":
            # Total credit: prefer private non-financial sector, % of GDP, adjusted
            preferences = {
                "TC_BORROWERS": "P",  # Private non-financial sector
                "UNIT_TYPE": "770",   # Percentage of GDP
                "TC_ADJUST": "A",     # Adjusted for breaks
                "VALUATION": "M",     # Market value
            }
        elif indicator_code in ["WS_SPP", "WS_CPP", "WS_DPP"]:
            # Property prices: prefer real, adjusted
            preferences = {
                "PP_VALUATION": "R",  # Real
                "UNIT_MEASURE": "628",  # Index
            }
        elif indicator_code == "WS_DSR":
            # Debt service ratio
            preferences = {
                "DSR_BORROWERS": "P",  # Private non-financial
                "DSR_ADJUST": "A",     # Adjusted
            }
        elif indicator_code == "WS_GLI":
            # Global liquidity indicators: prefer USD denomination, total
            preferences = {
                "CURR_DENOM": "USD",  # USD denomination
                "BORROWERS_CTY": "3P",  # All countries
                "BORROWERS_SECTOR": "A",  # All sectors
                "LENDERS_SECTOR": "A",  # All lenders
            }
        elif indicator_code == "WS_DEBT_SEC2_PUB":
            # International debt securities: prefer all issuers, USD
            preferences = {
                "ISSUER_RES": "5J",  # All countries
                "UNIT_MEASURE": "USD",  # USD denomination
            }

        # Score each series based on preferences
        best_score = -1
        best_key = None
        best_observations = {}

        for series_key, series_obj in series_data.items():
            observations = series_obj.get("observations", {})

            # Skip series with no data
            if not observations:
                continue

            # Parse series key (e.g., "0:0:0:1:0:1:1" -> [0, 0, 0, 1, 0, 1, 1])
            try:
                key_parts = [int(x) for x in series_key.split(":")]
            except (ValueError, TypeError):
                logger.warning(f"Invalid series key format '{series_key}' in BIS response, skipping")
                continue

            # Calculate score based on preferences
            score = len(observations)  # Base score on data availability

            for dim_id, preferred_value in preferences.items():
                if dim_id in dim_map:
                    dim_info = dim_map[dim_id]
                    dim_index = dim_info["index"]
                    value_map = dim_info["values"]

                    # Check if series matches preference
                    if dim_index < len(key_parts):
                        actual_value_index = key_parts[dim_index]
                        # Find actual value ID
                        for val_id, val_index in value_map.items():
                            if val_index == actual_value_index:
                                if val_id == preferred_value:
                                    score += 1000  # Strong preference match
                                break

            # Update best if this series scores higher
            if score > best_score:
                best_score = score
                best_key = series_key
                best_observations = observations

        # Fallback to first series if no scoring worked
        if best_key is None:
            best_key = next(iter(series_data))
            best_observations = series_data[best_key].get("observations", {})

        return best_key, best_observations
