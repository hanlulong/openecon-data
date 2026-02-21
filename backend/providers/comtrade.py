from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional
import logging
import asyncio

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import Metadata, NormalizedData
from .comtrade_metadata import (
    COUNTRY_CODE_MAPPINGS,
    HS_CODE_MAPPINGS,
    EU27_COUNTRY_CODES,
    REGION_EXPANSIONS,
    G7_COUNTRY_CODES,
    BRICS_COUNTRY_CODES,
    ASEAN_COUNTRY_CODES,
    NORDIC_COUNTRY_CODES,
)
from .base import BaseProvider

logger = logging.getLogger(__name__)

# Retry configuration for rate limiting
MAX_RETRIES = 5  # Increased from 3 to handle Comtrade API instability
RETRY_DELAY_BASE = 2.0  # Increased from 1.0 for more conservative backoff
RATE_LIMIT_STATUS = 429


class ComtradeProvider(BaseProvider):
    COMMODITY_MAPPINGS: Dict[str, str] = {
        "ALL": "TOTAL",
        "TOTAL": "TOTAL",
        # Oil and Petroleum Products (HS Chapter 27)
        "OIL": "27",
        "OILS": "27",
        "PETROLEUM": "27",
        "PETROLEUM_OIL": "27",
        "PETROLEUM_OILS": "27",
        "MINERAL_FUEL": "27",
        "MINERAL_FUELS": "27",
        "CRUDE": "2709",
        "CRUDE_OIL": "2709",
        "CRUDE_PETROLEUM": "2709",
        "NATURAL_GAS": "2711",
        "COAL": "2701",
        "PHARMACEUTICALS": "30",
        "MEDICINES": "30",
        "DRUGS": "30",
        "CLOTHING": "62",
        "APPAREL": "62",
        "GARMENTS": "62",
        "KNIT_CLOTHING": "61",
        "SHIRTS": "6205",
        "T-SHIRTS": "6109",
        "DRESSES": "6204",
        "TROUSERS": "6203",
        "PANTS": "6203",
        "FOOTWEAR": "64",
        "SHOES": "64",
        "BOOTS": "6401",
        "SNEAKERS": "6404",
        "ATHLETIC_SHOES": "640411",
        "LEATHER_SHOES": "640340",
        "MACHINERY": "84",
        "COMPUTERS": "8471",
        "LAPTOPS": "847130",
        "PRINTERS": "8443",
        "AIR_CONDITIONERS": "8415",
        "REFRIGERATORS": "8418",
        "ELECTRONICS": "85",
        "ELECTRICAL_EQUIPMENT": "85",
        "SMARTPHONES": "851712",
        "PHONES": "8517",
        "TELEVISIONS": "8528",
        "TVS": "8528",
        "SEMICONDUCTORS": "8542",
        "SEMICONDUCTOR": "8542",
        "CHIPS": "8542",
        "BATTERIES": "8506",
        "VEHICLES": "87",
        "CARS": "8703",
        "AUTOMOBILES": "8703",
        "TRUCKS": "8704",
        "MOTORCYCLES": "8711",
        "BICYCLES": "8712",
        "ELECTRIC_VEHICLES": "870380",
        "AIRCRAFT": "88",
        "AIRPLANES": "8802",
        "HELICOPTERS": "8802",
        "MEDICAL_INSTRUMENTS": "90",
        "OPTICAL_INSTRUMENTS": "90",
        "FURNITURE": "94",
        "CHAIRS": "9401",
        "TABLES": "9403",
        "TOYS": "95",
        "GAMES": "95",
        "FOOD": "AG2",
        "AGRICULTURE": "AG2",
        "WHEAT": "1001",
        "RICE": "1006",
        "CORN": "1005",
        "SOYBEANS": "1201",
        "COFFEE": "0901",
        "TEA": "0902",
        "MEAT": "02",
        "FISH": "03",
        "DAIRY": "04",
        "FRUIT": "08",
        "FRUITS": "08",
        "VEGETABLE": "07",
        "VEGETABLES": "07",
        "TEXTILE": "50",
        "TEXTILES": "50",
        "COTTON": "52",
        "FABRIC": "54",
        "IRON": "72",
        "STEEL": "72",
        "ALUMINUM": "76",
        "COPPER": "74",
        "GOLD": "7108",
        "SILVER": "7106",
        "RARE_EARTH": "2805",
        "RARE_EARTH_ELEMENTS": "2805",
        "RARE_EARTH_METALS": "2805",
        "RARE_EARTHS": "2805",
        "PLASTIC": "39",
        "PLASTICS": "39",
        "RUBBER": "40",
        "WOOD": "44",
        "PAPER": "48",
        "CHEMICAL": "28",
        "CHEMICALS": "28",
        "ORGANIC_CHEMICAL": "29",
        "ORGANIC_CHEMICALS": "29",
        # Beverages
        "WINE": "2204",
        "WINES": "2204",
        "BEER": "2203",
        "SPIRITS": "2208",
        "LIQUOR": "2208",
        "BEVERAGES": "22",
        # Flowers and plants
        "FLOWERS": "0603",
        "FLOWER": "0603",
        "CUT_FLOWERS": "0603",
        "PLANTS": "06",
        "LIVE_PLANTS": "06",
        # Minerals
        "IRON_ORE": "2601",
        "ORES": "26",
        "MINERALS": "25",
        # Fashion and apparel (expanded)
        "FASHION": "62",
        "FASHION_TEXTILES": "62",
        "LEATHER": "41",
        "LEATHER_GOODS": "42",
        "BAGS": "4202",
        "HANDBAGS": "420221",
        "JEWELRY": "7113",
        "WATCHES": "9101",
        "SUNGLASSES": "900410",
        "PERFUME": "3303",
        "COSMETICS": "33",
        # Auto parts
        "AUTO_PARTS": "8708",
        "CAR_PARTS": "8708",
        "VEHICLE_PARTS": "8708",
    }

    # Country mappings imported from comtrade_metadata module

    FLOW_MAPPINGS: Dict[str, str] = {
        "EXPORT": "X",
        "EXPORTS": "X",
        "IMPORT": "M",
        "IMPORTS": "M",
        "BOTH": "M,X",
    }

    @property
    def provider_name(self) -> str:
        return "Comtrade"

    def __init__(self, api_key: Optional[str], timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        settings = get_settings()
        self.base_url = settings.comtrade_base_url.rstrip("/")
        self.api_key = api_key

    async def _fetch_data(self, **params) -> List[NormalizedData]:
        """Implement BaseProvider interface by routing to fetch_trade_data."""
        return await self.fetch_trade_data(
            reporter=params.get("reporter"),
            reporters=params.get("reporters"),
            partner=params.get("partner"),
            commodity=params.get("commodity"),
            flow=params.get("flow"),
            start_year=params.get("start_year"),
            end_year=params.get("end_year"),
            frequency=params.get("frequency", "annual"),
        )

    @staticmethod
    def _commodity_code(commodity: Optional[str]) -> str:
        """Convert commodity name/HS code to Comtrade commodity code.

        Handles multiple input formats:
        - Numeric codes: "8703", "2709"
        - HS prefixed codes: "HS 8703", "HS8703", "HS 30", "HS2709"
        - Chapter references: "HS chapter 30", "chapter 84"
        - Text names: "automobiles", "wheat", "machinery"
        """
        if not commodity:
            return "TOTAL"
        commodity = commodity.strip()

        # Tier 1: Direct numeric code (highest priority)
        if commodity.isdigit() and 2 <= len(commodity) <= 6:
            return commodity

        # Tier 2: HS-prefixed codes - strip "HS" prefix and extract numeric part
        # Handles: "HS 8703", "HS8703", "HS 30", "HS2709", "HS chapter 30"
        upper_commodity = commodity.upper()
        if upper_commodity.startswith("HS"):
            # Remove "HS" prefix
            rest = commodity[2:].strip()
            # Handle "HS chapter 30" format
            if rest.upper().startswith("CHAPTER"):
                rest = rest[7:].strip()  # Remove "chapter"
            # Extract numeric part
            numeric_part = ''.join(c for c in rest if c.isdigit())
            if numeric_part and 2 <= len(numeric_part) <= 6:
                return numeric_part

        # Tier 3: "Chapter XX" without HS prefix
        if upper_commodity.startswith("CHAPTER"):
            rest = commodity[7:].strip()
            numeric_part = ''.join(c for c in rest if c.isdigit())
            if numeric_part and 2 <= len(numeric_part) <= 4:
                return numeric_part

        key = upper_commodity.replace(" ", "_")
        # Tier 4: Check local COMMODITY_MAPPINGS (custom/specific mappings)
        code = ComtradeProvider.COMMODITY_MAPPINGS.get(key)
        if code:
            return code

        # Tier 5: Fallback to comprehensive HS code mappings
        code = HS_CODE_MAPPINGS.get(key)
        if code:
            return code

        # Tier 6: Partial match - find commodity containing this term
        for mapping_key, mapping_code in ComtradeProvider.COMMODITY_MAPPINGS.items():
            if key in mapping_key or mapping_key in key:
                return mapping_code

        # Default to TOTAL if no mapping found
        return "TOTAL"

    @staticmethod
    def _country_code(country: str) -> str:
        """Convert country name/code to UN Comtrade numeric code.

        Returns None for invalid regional codes that cannot be resolved.
        Valid regions (like "EU") are converted to their Comtrade codes.
        Invalid regions (like "Middle East", "Asia", "Africa") return None
        to signal that queries with these partners should be decomposed.

        Special handling:
        - "EU27_2020" returns list of individual EU member countries
        - Taiwan can use code 158 (standard) or 490 (alternative for some contexts)
        """
        key = country.upper().replace(" ", "_")
        code = COUNTRY_CODE_MAPPINGS.get(key, None)

        # If not found, return None instead of the original input
        # This prevents invalid codes like "Middle+East" or "AS" from being sent to API
        if code is None:
            return None

        return code

    @staticmethod
    def _flow_code(flow: Optional[str]) -> str:
        if not flow:
            return "M,X"
        key = flow.upper()
        return ComtradeProvider.FLOW_MAPPINGS.get(key, "M,X")

    @staticmethod
    def _generate_periods(start_year: int, end_year: int, frequency: str) -> str:
        """Generate period parameter based on frequency.

        Args:
            start_year: Start year
            end_year: End year (inclusive)
            frequency: "annual", "monthly", "quarterly"

        Returns:
            Comma-separated period string (e.g., "2015,2016,2017" for annual)
        """
        if frequency.lower() in ["annual", "yearly", "a", "y"]:
            # Annual: "2015,2016,2017,..."
            return ",".join(str(year) for year in range(start_year, end_year + 1))

        elif frequency.lower() in ["monthly", "month", "m"]:
            # Monthly: "202001,202002,...,202012,202101,..."
            periods = []
            for year in range(start_year, end_year + 1):
                for month in range(1, 13):
                    periods.append(f"{year}{month:02d}")
            return ",".join(periods)

        elif frequency.lower() in ["quarterly", "quarter", "q"]:
            # Quarterly: "20201,20202,20203,20204,20211,..." (YYYYQ format)
            periods = []
            for year in range(start_year, end_year + 1):
                for quarter in range(1, 5):
                    periods.append(f"{year}{quarter}")
            return ",".join(periods)

        else:
            # Default to annual
            return ",".join(str(year) for year in range(start_year, end_year + 1))

    async def _fetch_single_reporter_data(
        self,
        client: httpx.AsyncClient,
        reporter_raw: str,
        partner_code: Optional[str],
        commodity_code: str,
        flow_code: str,
        period_param: str,
        freq_code: str,
    ) -> List[NormalizedData]:
        """Fetch trade data for a single reporter country with retry logic.

        Helper method to enable parallel fetching of multiple reporters.
        Implements exponential backoff for rate limiting (HTTP 429).

        Args:
            client: httpx AsyncClient instance
            reporter_raw: Reporter country name or code
            partner_code: Partner country code (can be None for world total)
            commodity_code: Commodity code
            flow_code: Trade flow code
            period_param: Comma-separated period string
            freq_code: Frequency code (A/M/Q)

        Returns:
            List of NormalizedData objects (one per flow type)
        """
        reporter_code = self._country_code(reporter_raw)

        # Check if reporter is a known non-reporting territory
        # Taiwan (158) does not report to UN Comtrade due to political status
        # Data about Taiwan trade must be obtained from partner perspective
        # (e.g., China's exports TO Taiwan, Japan's imports FROM Taiwan)
        NON_REPORTING_TERRITORIES = {
            "158": "Taiwan",  # Taiwan - use code 490 in partner queries
            "490": "Taiwan",  # Alternative Taiwan code
        }

        if reporter_code in NON_REPORTING_TERRITORIES:
            territory_name = NON_REPORTING_TERRITORIES[reporter_code]
            logger.warning(
                f"{territory_name} does not report trade data to UN Comtrade. "
                f"To get {territory_name} trade data, use partner perspective: "
                f"For {territory_name} exports: query partner imports FROM {territory_name} (partner code 490). "
                f"For {territory_name} imports: query partner exports TO {territory_name} (partner code 490). "
                f"Returning empty result - consider querying major trading partners (China, Japan, USA)."
            )
            return []

        # Handle invalid regional codes that cannot be resolved
        # (e.g., "Middle East", "Asia", "Africa" without specific country codes)
        if partner_code is None:
            # Only happens for regional codes that aren't mapped
            # These should be decomposed by the query service
            logger.warning(f"Skipping fetch for {reporter_raw}: partner code is None (invalid region?)")
            return []

        params = {
            "typeCode": "C",
            "freqCode": freq_code,
            "clCode": "HS",
            "reporterCode": reporter_code,
            "period": period_param,
            "partnerCode": partner_code,
            "cmdCode": commodity_code,
            "flowCode": flow_code,
            "format": "json",
        }

        if self.api_key:
            params["subscription-key"] = self.api_key

        # Use correct URL path based on frequency
        url_path = f"{self.base_url}/C/{freq_code}/HS"

        # Implement exponential backoff retry for rate limiting
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(url_path, params=params, timeout=60.0)
                response.raise_for_status()
                payload = response.json()
                break  # Success, exit retry loop
            except httpx.HTTPStatusError as e:
                if e.response.status_code == RATE_LIMIT_STATUS and attempt < MAX_RETRIES - 1:
                    # Rate limited: wait and retry with exponential backoff
                    delay = RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(
                        f"Rate limited (429) for {reporter_raw}, attempt {attempt + 1}/{MAX_RETRIES}. "
                        f"Waiting {delay}s before retry..."
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    # Not a rate limit error, or final retry exhausted
                    logger.error(
                        f"Comtrade API error for reporter {reporter_raw}: "
                        f"HTTP {e.response.status_code}: {str(e)}"
                    )
                    return []
            except Exception as e:
                # Other errors (network, JSON parsing, etc.)
                logger.error(
                    f"Comtrade API error for reporter {reporter_raw}: {type(e).__name__}: {str(e)}"
                )
                return []

        records = payload.get("data") or []
        if not records:
            return []  # Return empty if no data

        # IMPROVED DEDUPLICATION: Include cmdCode in key to prevent non-total values
        # from overwriting total values when querying for TOTAL trade
        dedup_map: Dict[tuple, dict] = {}
        for record in records:
            # Use (period, flowDesc, cmdCode) as composite key
            # This prevents a specific HS chapter value from overwriting TOTAL
            key = (
                record.get("period"),
                record.get("flowDesc", "Trade"),
                record.get("cmdCode", "TOTAL")
            )
            # Keep maximum value for same key (handles data revisions)
            if key in dedup_map:
                existing_val = dedup_map[key].get("primaryValue") or 0
                new_val = record.get("primaryValue") or 0
                if new_val > existing_val:
                    dedup_map[key] = record
            else:
                dedup_map[key] = record

        # When querying for TOTAL, filter to only include TOTAL records
        # This prevents component HS chapter values from being mixed in
        if commodity_code == "TOTAL":
            total_records = [r for r in dedup_map.values()
                            if r.get("cmdCode", "").upper() in ("TOTAL", "AG2", "")]
            # If no TOTAL records found, fall back to all records
            if total_records:
                dedup_map = {
                    (r.get("period"), r.get("flowDesc", "Trade"), r.get("cmdCode", "TOTAL")): r
                    for r in total_records
                }
            else:
                logger.warning(f"No TOTAL records found, using all {len(dedup_map)} records")

        # Group deduplicated records by flow
        grouped: Dict[str, List[dict]] = defaultdict(list)
        for record in dedup_map.values():
            grouped[record.get("flowDesc", "Trade")].append(record)

        # Build API URL string safely
        try:
            url_with_params = response.request.url.copy_with(params=params)
            api_url = str(url_with_params)
        except Exception:
            api_url = str(response.request.url)

        if "subscription-key" in params:
            api_url = api_url.replace(params["subscription-key"], "YOUR_KEY")

        # Build results for this reporter
        results = []
        for flow_desc, flow_records in grouped.items():
            flow_records.sort(key=lambda x: x["period"])
            first = flow_records[0]

            flow_name = flow_desc or ("Exports" if "X" in flow_code else "Imports")
            commodity_name = first.get("cmdDesc") or ("Total Trade" if commodity_code == "TOTAL" else commodity_code)
            reporter_name = first.get("reporterDesc") or reporter_raw

            # Create data points and deduplicate by date
            # When multiple records exist for same period, keep the maximum value (assumes it's the total)
            data_points_map = {}
            for item in flow_records:
                date_str = f"{item['period']}-01-01"
                new_value = item.get("primaryValue") or 0

                # If date already exists, keep the maximum value
                if date_str in data_points_map:
                    existing_value = data_points_map[date_str]["value"] or 0
                    if new_value > existing_value:
                        data_points_map[date_str] = {"date": date_str, "value": new_value}
                else:
                    data_points_map[date_str] = {"date": date_str, "value": new_value}

            # Convert to list and sort by date
            data_points = sorted(data_points_map.values(), key=lambda x: x["date"])

            # DATA VALIDATION: Detect suspiciously low values for major trade flows
            # Major economies (US, China, Germany, Japan, UK, France, etc.) typically have
            # trade flows in billions of dollars, not thousands
            major_traders = {"CHN", "USA", "DEU", "JPN", "GBR", "FRA", "ITA", "NLD", "KOR", "CAN", "MEX"}
            reporter_is_major = reporter_code in major_traders or reporter_raw.upper() in [
                "CHINA", "UNITED STATES", "GERMANY", "JAPAN", "UNITED KINGDOM", "UK",
                "FRANCE", "ITALY", "NETHERLANDS", "SOUTH KOREA", "CANADA", "MEXICO"
            ]

            if reporter_is_major and commodity_code == "TOTAL":
                values = [p["value"] for p in data_points if p["value"] is not None and p["value"] > 0]
                if values:
                    max_val = max(values)
                    min_val = min(values)

                    # For major traders, total trade should be at least $1 billion
                    if max_val < 1e9:
                        logger.warning(
                            f"⚠️ COMTRADE DATA QUALITY: All values for {reporter_name} total {flow_name} "
                            f"are below $1B (max=${max_val:,.0f}). Data may be incomplete or in wrong units."
                        )

                    # Check for suspicious outliers (value >1000x smaller than max)
                    if min_val > 0 and max_val / min_val > 1000:
                        logger.warning(
                            f"⚠️ COMTRADE DATA QUALITY: Large value range for {reporter_name} total {flow_name}. "
                            f"Min=${min_val:,.0f}, Max=${max_val:,.0f}. Some data points may be incorrect."
                        )

            # Map frequency code to readable string
            freq_name = "monthly" if freq_code == "M" else "quarterly" if freq_code == "Q" else "annual"

            # Human-readable URL for data verification on UN Comtrade website
            source_url = "https://comtradeplus.un.org/TradeFlow"

            # Extract start and end dates from data points
            start_date = data_points[0]["date"] if data_points else None
            end_date = data_points[-1]["date"] if data_points else None

            results.append(
                NormalizedData(
                    metadata=Metadata(
                        source="UN Comtrade",
                        indicator=f"{flow_name} - {commodity_name}",
                        country=reporter_name,
                        frequency=freq_name,
                        unit="US Dollars",
                        lastUpdated=datetime.now(timezone.utc).isoformat(),
                        apiUrl=api_url,
                        sourceUrl=source_url,
                        seasonalAdjustment=None,
                        dataType="Level",
                        priceType="Nominal (current prices)",
                        description=f"{flow_name} - {commodity_name}",
                        notes=None,
                        startDate=start_date,
                        endDate=end_date,
                    ),
                    data=data_points,
                )
            )

        return results

    async def fetch_trade_data(
        self,
        reporter: Optional[str] = None,
        reporters: Optional[List[str]] = None,
        partner: Optional[str] = None,
        commodity: Optional[str] = None,
        flow: Optional[str] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        frequency: str = "annual",
    ) -> List[NormalizedData]:
        """Fetch trade data from UN Comtrade API.

        Args:
            reporter: Single reporter country (for backwards compatibility)
            reporters: List of reporter countries (for multi-country queries)
            partner: Partner country code or name (None for world total)
            commodity: Commodity code or name
            flow: Trade flow (IMPORT, EXPORT, BOTH)
            start_year: Start year for data
            end_year: End year for data
            frequency: Data frequency (annual, monthly, quarterly)

        Returns:
            List of NormalizedData objects (one per reporter if multiple reporters)

        Raises:
            DataNotAvailableError: If partner is an invalid regional code that cannot be resolved
        """
        from ..utils.retry import DataNotAvailableError

        # Support both single reporter and multiple reporters
        reporter_list = reporters or [reporter or "US"]

        # Expand region names to individual countries using CountryResolver
        from ..routing.country_resolver import CountryResolver

        expanded_reporters = []
        for r in reporter_list:
            r_upper = r.upper().replace(" ", "_").replace("-", "_")

            # First, try CountryResolver (single source of truth for standard regions)
            expanded = CountryResolver.get_region_expansion(r_upper, format="un_numeric")
            if expanded:
                # Convert int codes to string format for Comtrade API
                region_codes = [str(code) for code in expanded]
                logger.info(f"🌍 Expanding Comtrade region '{r}' via CountryResolver → {len(region_codes)} countries")
                expanded_reporters.extend(region_codes)
            # Try variant names
            elif "_COUNTRIES" in r_upper or "_NATIONS" in r_upper:
                variant = r_upper.replace("_COUNTRIES", "").replace("_NATIONS", "")
                expanded = CountryResolver.get_region_expansion(variant, format="un_numeric")
                if expanded:
                    region_codes = [str(code) for code in expanded]
                    logger.info(f"🌍 Matched region '{variant}' via CountryResolver → {len(region_codes)} countries")
                    expanded_reporters.extend(region_codes)
                else:
                    expanded_reporters.append(r)
            # Fall back to Comtrade-specific REGION_EXPANSIONS (EU27_2020, etc.)
            elif r_upper in REGION_EXPANSIONS:
                region_codes = REGION_EXPANSIONS[r_upper]
                logger.info(f"🌍 Expanding Comtrade region '{r}' via Comtrade mappings → {len(region_codes)} countries")
                expanded_reporters.extend(region_codes)
            else:
                expanded_reporters.append(r)
        reporter_list = expanded_reporters if expanded_reporters else reporter_list

        # Taiwan Special Handling: Taiwan (490) is a non-reporting territory
        # If Taiwan is the reporter, we need to flip to partner perspective
        # Taiwan exports = partner imports FROM Taiwan (490)
        # Taiwan imports = partner exports TO Taiwan (490)
        taiwan_query = False
        if len(reporter_list) == 1:
            reporter_code_check = self._country_code(reporter_list[0])
            if reporter_code_check in ["158", "490"]:
                taiwan_query = True
                logger.info(
                    "Detected Taiwan as reporter - will use partner perspective. "
                    "Taiwan exports → query major partners' imports FROM Taiwan (490). "
                    "Taiwan imports → query major partners' exports TO Taiwan (490)."
                )

                # If no partner specified, use major Taiwan trading partners
                if not partner:
                    # Major Taiwan trading partners: China, USA, Japan, South Korea, Hong Kong
                    logger.info(
                        "No partner specified for Taiwan query - querying major trading partners: "
                        "China, USA, Japan, South Korea, Hong Kong, Singapore"
                    )
                    partner_list_for_taiwan = ["China", "USA", "Japan", "South Korea", "Hong Kong", "Singapore"]
                    # Flip: Taiwan as reporter → partners as reporters, Taiwan (490) as partner
                    reporter_list = partner_list_for_taiwan
                    partner = "Taiwan"  # Will resolve to 490
                    # Flip flow direction
                    if flow:
                        flow_upper = flow.upper()
                        if "EXPORT" in flow_upper:
                            flow = "IMPORT"  # Taiwan exports = partner imports from Taiwan
                            logger.info("Flipped flow: Taiwan exports → partner imports FROM Taiwan")
                        elif "IMPORT" in flow_upper:
                            flow = "EXPORT"  # Taiwan imports = partner exports to Taiwan
                            logger.info("Flipped flow: Taiwan imports → partner exports TO Taiwan")
                else:
                    # Partner specified - flip reporter and partner
                    original_partner = partner
                    partner = "Taiwan"  # Taiwan becomes partner (490)
                    reporter_list = [original_partner]  # Partner becomes reporter
                    # Flip flow direction
                    if flow:
                        flow_upper = flow.upper()
                        if "EXPORT" in flow_upper:
                            flow = "IMPORT"
                            logger.info(f"Flipped: Taiwan exports to {original_partner} → {original_partner} imports FROM Taiwan")
                        elif "IMPORT" in flow_upper:
                            flow = "EXPORT"
                            logger.info(f"Flipped: Taiwan imports from {original_partner} → {original_partner} exports TO Taiwan")

        # Handle partner country code resolution
        if partner:
            partner_code = self._country_code(partner)

            # Special handling for EU27_2020 - query individual EU countries
            if partner_code == "EU27_2020":
                logger.info(
                    f"Expanding EU partner query to {len(EU27_COUNTRY_CODES)} individual EU member countries"
                )
                # For UK imports from EU, query UK as reporter with each EU country as partner
                # This will be handled by making multiple parallel requests below
                # For now, we'll use a special marker that we process later
                partner_is_eu27 = True
                partner_code = None  # Will expand later
            elif partner_code is None:
                # Partner is an unrecognized region that cannot be resolved
                raise DataNotAvailableError(
                    f"'{partner}' is not a valid country or recognized region in UN Comtrade. "
                    f"Please specify individual countries. "
                    f"For regions like 'Middle East', please specify individual countries: "
                    f"UAE, Saudi Arabia, Qatar, Kuwait, Oman, Iraq, Iran, Israel, etc."
                )
            else:
                partner_is_eu27 = False
        else:
            partner_code = "0"  # World total
            partner_is_eu27 = False
        commodity_code = self._commodity_code(commodity)
        flow_code = self._flow_code(flow)

        now = datetime.now(timezone.utc)
        start = start_year or now.year - 5
        end = end_year or now.year - 1

        # Determine frequency code for API
        freq_lower = frequency.lower()
        if freq_lower in ["monthly", "month", "m"]:
            freq_code = "M"
        elif freq_lower in ["quarterly", "quarter", "q"]:
            freq_code = "Q"
        else:
            freq_code = "A"  # Annual is default

        # Generate period parameter based on frequency
        period_param = self._generate_periods(start, end, frequency)

        # Use shared HTTP client pool for better performance (timeout passed per-request)
        client = get_http_client()
        # If partner is EU27, expand to individual EU countries
        if partner_is_eu27:
            logger.info(
                f"Querying {len(reporter_list)} reporter(s) against {len(EU27_COUNTRY_CODES)} EU countries"
            )
            # Create tasks for each reporter x each EU country combination
            tasks = []
            for reporter_raw in reporter_list:
                for eu_country_code in EU27_COUNTRY_CODES:
                    tasks.append(
                        self._fetch_single_reporter_data(
                            client,
                            reporter_raw,
                            eu_country_code,
                            commodity_code,
                            flow_code,
                            period_param,
                            freq_code,
                        )
                    )

            # Wait for all EU country requests to complete
            results_list = await asyncio.gather(*tasks)

            # Flatten and aggregate results
            all_results = []
            for result in results_list:
                all_results.extend(result)

            # TODO: Consider aggregating data across EU countries for cleaner output
            # For now, return all individual country results
            logger.info(f"Retrieved {len(all_results)} data series from EU countries")

        else:
            # Standard query - fetch data for all reporters in parallel
            tasks = [
                self._fetch_single_reporter_data(
                    client,
                    reporter_raw,
                    partner_code,
                    commodity_code,
                    flow_code,
                    period_param,
                    freq_code,
                )
                for reporter_raw in reporter_list
            ]

            # Wait for all requests to complete
            results_list = await asyncio.gather(*tasks)

            # Flatten results (each task returns a list)
            all_results = []
            for result in results_list:
                all_results.extend(result)

        return all_results

    async def fetch_trade_balance(
        self,
        reporter: str,
        partner: Optional[str] = None,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        frequency: str = "annual",
    ) -> NormalizedData:
        """Fetch trade balance by getting imports and exports separately, then calculating balance.

        Trade Balance = Exports - Imports

        Makes sequential requests with delay to avoid rate limiting.

        Args:
            reporter: Reporting country code or name
            partner: Partner country code or name (None for world total)
            start_year: Start year for data
            end_year: End year for data
            frequency: Data frequency ("annual", "monthly", "quarterly")

        Returns:
            NormalizedData with trade balance time series
        """
        # Import here to avoid circular dependency
        from ..utils.retry import DataNotAvailableError

        # Fetch exports and imports separately for robustness
        # Add delay between requests to avoid rate limiting
        try:
            exports_data = await self.fetch_trade_data(
                reporter=reporter,
                partner=partner,
                commodity=None,
                flow="EXPORTS",
                start_year=start_year,
                end_year=end_year,
                frequency=frequency,
            )
        except Exception as exc:
            raise DataNotAvailableError(
                f"Failed to fetch export data for trade balance calculation: {str(exc)}"
            ) from exc

        # Add 0.5s delay between exports and imports requests to reduce rate limit risk
        await asyncio.sleep(0.5)

        try:
            imports_data = await self.fetch_trade_data(
                reporter=reporter,
                partner=partner,
                commodity=None,
                flow="IMPORTS",
                start_year=start_year,
                end_year=end_year,
                frequency=frequency,
            )
        except Exception as exc:
            raise DataNotAvailableError(
                f"Failed to fetch import data for trade balance calculation: {str(exc)}"
            ) from exc

        # Check that we got data
        if not exports_data or not imports_data:
            raise DataNotAvailableError(
                f"No trade data available for {reporter}" + (f" with {partner}" if partner else " (world)")
            )

        # Extract the first series from each result (should only be one per flow)
        exports = exports_data[0] if exports_data else None
        imports = imports_data[0] if imports_data else None

        if not exports or not imports:
            raise DataNotAvailableError(
                "Missing import or export data for trade balance calculation"
            )

        # Check that we have actual data points
        if not exports.data or not imports.data:
            raise DataNotAvailableError(
                f"No data points available for {reporter}" + (f" with {partner}" if partner else " (world)")
            )

        # Create maps for easier lookup
        import_map = {point.date: point.value or 0 for point in imports.data}
        export_map = {point.date: point.value or 0 for point in exports.data}

        # Get all unique dates from both series
        all_dates = sorted(set(list(import_map.keys()) + list(export_map.keys())))

        # Calculate trade balance for all dates
        balance_points = []
        for date in all_dates:
            export_value = export_map.get(date, 0)
            import_value = import_map.get(date, 0)
            balance = export_value - import_value
            balance_points.append({"date": date, "value": balance})

        # Build partner description for metadata
        partner_desc = f" with {partner}" if partner else " (World)"

        # Extract start and end dates from balance data
        start_date = balance_points[0]["date"] if balance_points else None
        end_date = balance_points[-1]["date"] if balance_points else None

        return NormalizedData(
            metadata=Metadata(
                source="UN Comtrade",
                indicator=f"Trade Balance{partner_desc}",
                country=exports.metadata.country,
                frequency=exports.metadata.frequency,
                unit="US Dollars",
                lastUpdated=datetime.now(timezone.utc).isoformat(),
                apiUrl=exports.metadata.apiUrl,
                seasonalAdjustment=None,
                dataType="Level",
                priceType="Nominal (current prices)",
                description=f"Trade Balance{partner_desc}",
                notes=None,
                startDate=start_date,
                endDate=end_date,
            ),
            data=balance_points,
        )
