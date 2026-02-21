from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, TYPE_CHECKING, Any

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


class EurostatProvider(BaseProvider):
    """Eurostat Statistics API provider for EU economic data using SDMX 3.0 endpoints."""

    # Indicator name -> dataset code mappings (for quick lookup)
    # NOTE: IMF-style codes (NGDP, LUR, etc.) are handled by IndicatorTranslator
    # This mapping only contains Eurostat-native indicator names
    DATASET_MAPPINGS: Dict[str, str] = {
        # National Accounts
        "GDP": "nama_10_gdp",
        "GDP_GROWTH": "nama_10_gdp",
        "GDP GROWTH": "nama_10_gdp",
        "GDP_GROWTH_RATE": "nama_10_gdp",
        "GDP_PER_CAPITA": "nama_10_pc",
        "GDP PER CAPITA": "nama_10_pc",
        "GROSS_DOMESTIC_PRODUCT": "nama_10_gdp",
        "GROSS DOMESTIC PRODUCT": "nama_10_gdp",
        "REAL_GDP": "nama_10_gdp",
        "REAL GDP": "nama_10_gdp",
        "NOMINAL_GDP": "nama_10_gdp",
        "NOMINAL GDP": "nama_10_gdp",

        # Labor Market
        "UNEMPLOYMENT": "une_rt_a",
        "UNEMPLOYMENT_RATE": "une_rt_a",
        "UNEMPLOYMENT RATE": "une_rt_a",
        "JOBLESS_RATE": "une_rt_a",
        "JOBLESS RATE": "une_rt_a",
        # Note: tipslm80 is "percentage point change" - NOT the actual rate!
        # Use une_rt_a (standard unemployment rate) with AGE=Y_LT25 for youth unemployment RATE
        "YOUTH_UNEMPLOYMENT": "une_rt_a",
        "YOUTH UNEMPLOYMENT": "une_rt_a",
        "YOUTH_UNEMPLOYMENT_RATE": "une_rt_a",
        "YOUTH UNEMPLOYMENT RATE": "une_rt_a",
        "YOUTH_UNEMPLOYMENT_CHANGE": "tipslm80",  # Percentage point change version
        "EMPLOYMENT": "lfsi_emp_a",
        "EMPLOYMENT_RATE": "lfsi_emp_a",
        "EMPLOYMENT RATE": "lfsi_emp_a",

        # Prices - Headline Inflation (all items including food and energy)
        "INFLATION": "prc_hicp_aind",
        "CPI": "prc_hicp_aind",
        "HICP": "prc_hicp_aind",
        "HEADLINE_INFLATION": "prc_hicp_aind",
        "HEADLINE INFLATION": "prc_hicp_aind",
        "HEADLINE_CPI": "prc_hicp_aind",
        "CONSUMER_PRICES": "prc_hicp_manr",
        "CONSUMER PRICES": "prc_hicp_manr",
        "PRICE_INDEX": "prc_hicp_midx",

        # Prices - Core Inflation (EXCLUDING food and energy)
        # CRITICAL: Core inflation uses CP00XEF COICOP code, not CP00
        "CORE_INFLATION": "prc_hicp_aind_core",  # Special marker for core
        "CORE INFLATION": "prc_hicp_aind_core",
        "CORE_CPI": "prc_hicp_aind_core",
        "CORE CPI": "prc_hicp_aind_core",
        "UNDERLYING_INFLATION": "prc_hicp_aind_core",
        "UNDERLYING INFLATION": "prc_hicp_aind_core",
        "CPI_EX_FOOD_ENERGY": "prc_hicp_aind_core",
        "HICP_EXCLUDING_FOOD": "prc_hicp_aind_core",
        "HICP EXCLUDING FOOD": "prc_hicp_aind_core",

        # House Prices
        "HOUSE_PRICES": "prc_hpi_a",
        "HOUSE PRICES": "prc_hpi_a",
        "HOUSING_PRICES": "prc_hpi_a",
        "PROPERTY_PRICES": "prc_hpi_a",

        # Trade
        "TRADE": "ext_lt_maineu",
        "TRADE_BALANCE": "ext_lt_maineu",  # Use ext_lt_maineu with indic_et=MIO_BAL_VAL
        "TRADE BALANCE": "ext_lt_maineu",  # Use ext_lt_maineu with indic_et=MIO_BAL_VAL
        "EXPORTS": "ext_lt_maineu",
        "IMPORTS": "ext_lt_maineu",

        # Population
        "POPULATION": "demo_pjan",

        # Government Finance
        "GOVERNMENT_DEFICIT": "gov_10dd_edpt1",
        "GOVERNMENT DEFICIT": "gov_10dd_edpt1",
        "DEFICIT": "gov_10dd_edpt1",
        "GOVERNMENT_DEBT": "gov_10q_ggdebt",
        "GOVERNMENT DEBT": "gov_10q_ggdebt",
        "DEBT": "gov_10q_ggdebt",

        # Industry
        "INDUSTRIAL_PRODUCTION": "sts_inpr_a",
        "INDUSTRIAL PRODUCTION": "sts_inpr_a",
        "PRODUCTION": "sts_inpr_a",

        # Retail
        "RETAIL_TRADE": "sts_trtu_a",
        "RETAIL TRADE": "sts_trtu_a",
        "RETAIL": "sts_trtu_a",
        "RETAIL_TRADE_VOLUME": "sts_trtu_a",

        # Labor Costs
        "LABOR_COST": "lc_lci_r2_a",
        "LABOR COST": "lc_lci_r2_a",
        "LABOR_COST_INDEX": "lc_lci_r2_a",
        "LABOUR_COST": "lc_lci_r2_a",

        # Energy
        "ENERGY_CONSUMPTION": "nrg_bal_c",
        "ENERGY CONSUMPTION": "nrg_bal_c",
        "ENERGY_BALANCE": "nrg_bal_c",
        "ENERGY BALANCE": "nrg_bal_c",
        "FINAL_ENERGY_CONSUMPTION": "nrg_bal_c",
        "PRIMARY_ENERGY_CONSUMPTION": "nrg_bal_c",

        # Labor Productivity (nama_10_lp_ulc dataset)
        # Critical mappings to prevent false positives from metadata search
        "PRODUCTIVITY": "nama_10_lp_ulc",  # Labour productivity and unit labour costs
        "LABOR_PRODUCTIVITY": "nama_10_lp_ulc",
        "LABOUR_PRODUCTIVITY": "nama_10_lp_ulc",
        "LABOR PRODUCTIVITY": "nama_10_lp_ulc",
        "LABOUR PRODUCTIVITY": "nama_10_lp_ulc",
        "GDP_PER_HOUR": "nama_10_lp_ulc",
        "GDP PER HOUR": "nama_10_lp_ulc",
        "GDP_PER_WORKER": "nama_10_lp_ulc",
        "GDP PER WORKER": "nama_10_lp_ulc",
        "PRODUCTIVITY_GROWTH": "nama_10_lp_ulc",
        "PRODUCTIVITY GROWTH": "nama_10_lp_ulc",
        "OUTPUT_PER_HOUR": "nama_10_lp_ulc",
        "OUTPUT PER HOUR": "nama_10_lp_ulc",
        "WORKER_PRODUCTIVITY": "nama_10_lp_ulc",
        "WORKER PRODUCTIVITY": "nama_10_lp_ulc",

        # Unit Labor Cost
        "UNIT_LABOR_COST": "nama_10_lp_ulc",
        "UNIT LABOR COST": "nama_10_lp_ulc",
        "UNIT_LABOUR_COST": "nama_10_lp_ulc",
        "UNIT LABOUR COST": "nama_10_lp_ulc",
        "ULC": "nama_10_lp_ulc",

        # INFRASTRUCTURE FIX: Interest Rates - EI_MFIR_M dataset
        # Contains: day-to-day rates, 3-month rates, government bond yields (1y, 5y, 10y)
        "INTEREST_RATE": "EI_MFIR_M",
        "INTEREST RATE": "EI_MFIR_M",
        "INTEREST_RATES": "EI_MFIR_M",
        "INTEREST RATES": "EI_MFIR_M",
        "REAL_INTEREST_RATE": "EI_MFIR_M",
        "REAL INTEREST RATE": "EI_MFIR_M",
        "NOMINAL_INTEREST_RATE": "EI_MFIR_M",
        "NOMINAL INTEREST RATE": "EI_MFIR_M",
        "LONG_TERM_INTEREST_RATE": "EI_MFIR_M",
        "LONG TERM INTEREST RATE": "EI_MFIR_M",
        "GOVERNMENT_BOND_YIELD": "EI_MFIR_M",
        "GOVERNMENT BOND YIELD": "EI_MFIR_M",
        "BOND_YIELD": "EI_MFIR_M",
        "BOND YIELD": "EI_MFIR_M",
        "10_YEAR_BOND": "EI_MFIR_M",
        "10 YEAR BOND": "EI_MFIR_M",
        "MONEY_MARKET_RATE": "EI_MFIR_M",
        "MONEY MARKET RATE": "EI_MFIR_M",
    }

    COUNTRY_MAPPINGS: Dict[str, str] = {
        # All 27 EU Member States (as of 2020)
        "GERMANY": "DE",
        "FRANCE": "FR",
        "ITALY": "IT",
        "SPAIN": "ES",
        "NETHERLANDS": "NL",
        "POLAND": "PL",
        "BELGIUM": "BE",
        "SWEDEN": "SE",
        "AUSTRIA": "AT",
        "DENMARK": "DK",
        "FINLAND": "FI",
        "PORTUGAL": "PT",
        "GREECE": "GR",
        "CZECH REPUBLIC": "CZ",
        "CZECHIA": "CZ",
        "ROMANIA": "RO",
        "HUNGARY": "HU",
        "IRELAND": "IE",
        "SLOVAKIA": "SK",
        "BULGARIA": "BG",
        "CROATIA": "HR",
        "LITHUANIA": "LT",
        "SLOVENIA": "SI",
        "LATVIA": "LV",
        "ESTONIA": "EE",
        "CYPRUS": "CY",
        "LUXEMBOURG": "LU",
        "MALTA": "MT",
        # EU aggregates
        "EU": "EU27_2020",
        "EUROPEAN UNION": "EU27_2020",
        "EURO AREA": "EA20",  # Updated: EA19→EA20 (20 countries from 2023)
        "EUROZONE": "EA20",   # Updated: EA19→EA20 (20 countries from 2023)
        # CRITICAL FIX: Common alternative terms for EU/Europe that LLM might use
        "EUROPE": "EU27_2020",  # Generic "Europe" should map to EU27
        "EUROPEAN": "EU27_2020",
        "EU_27": "EU27_2020",
        "EU27": "EU27_2020",
        "EA": "EA20",  # Euro Area shorthand
        "EA_20": "EA20",
        "EA19": "EA20",  # Old code still sometimes used
    }

    # Comprehensive dimension mappings for top Eurostat datasets
    # Format: dataset_code -> {dimension: value}
    DATASET_DEFAULT_FILTERS: Dict[str, Dict[str, str]] = {
        # === National Accounts ===
        "nama_10_gdp": {"na_item": "B1GQ", "unit": "CP_MEUR"},  # GDP
        "nama_10_pc": {"na_item": "B1GQ", "unit": "CP_EUR_HAB"},  # GDP per capita

        # === Labor Market ===
        "une_rt_a": {"age": "Y15-74", "sex": "T"},  # Unemployment rate
        "une_rt_m": {"age": "Y15-74", "sex": "T"},  # Unemployment rate (monthly)
        "lfsa_urgan": {"age": "Y15-24", "sex": "T"},  # Youth unemployment rate (ages 15-24)
        "lfsi_emp_a": {"age": "Y15-64", "sex": "T", "unit": "PC_POP"},  # Employment rate (percentage of population)
        "lfsq_egan": {"age": "Y15-64", "sex": "T", "wstatus": "EMP"},  # Employment by age

        # === Prices and Inflation ===
        "prc_hicp_aind": {"coicop": "CP00"},  # HICP inflation - HEADLINE (all items)
        "prc_hicp_aind_core": {"coicop": "TOT_X_NRG_FOOD"},  # HICP inflation - CORE (excluding energy, food)
        "prc_hicp_manr": {"coicop": "CP00"},  # HICP monthly - headline
        "prc_hicp_midx": {"coicop": "CP00"},  # HICP index - headline
        "prc_ppp_ind": {"na_item": "B1GQ"},  # Price level indices
        "prc_hpi_a": {"purchase": "TOTAL"},  # House price index

        # === International Trade ===
        # Note: partner="EXT_EU27_2020" for extra-EU trade aggregate
        # indic_et: MIO_BAL_VAL=balance, MIO_EXP_VAL=exports, MIO_IMP_VAL=imports
        "ext_lt_intratrd": {"sitc06": "TOTAL"},  # Intra-EU trade
        "ext_lt_maineu": {"sitc06": "TOTAL", "partner": "EXT_EU27_2020", "indic_et": "MIO_BAL_VAL"},  # Extra-EU trade balance

        # === Population ===
        "demo_pjan": {"age": "TOTAL", "sex": "T"},  # Population by age/sex
        "demo_gind": {},  # Population indicators

        # === Government Finance ===
        "gov_10dd_edpt1": {"na_item": "B9", "sector": "S13"},  # Government deficit
        "gov_10q_ggdebt": {"na_item": "GD", "sector": "S13"},  # Government debt
        "gov_10q_ggnfa": {"na_item": "B9", "sector": "S13"},  # Net lending/borrowing

        # === Industry and Production ===
        # Note: indic_bt="PRD" (not "PROD") for production index, s_adj default NSA, unit I21
        "sts_inpr_a": {"indic_bt": "PRD", "nace_r2": "B-D", "s_adj": "CA", "unit": "I21"},  # Industrial production
        "sts_inpr_m": {"indic_bt": "PRD", "nace_r2": "B-D", "s_adj": "CA", "unit": "I21"},  # Industrial production (monthly)

        # === Retail and Services ===
        "sts_trtu_a": {"indic_bt": "TOVV", "nace_r2": "G47"},  # Retail trade
        "sts_trtu_m": {"indic_bt": "TOVV", "nace_r2": "G47"},  # Retail trade (monthly)

        # === Labor Costs ===
        "lc_lci_r2_a": {"lcstruct": "D1_D4_MD5", "nace_r2": "B-S_X_O"},  # Labor cost index
        "lc_lci_r2_q": {"lcstruct": "D1_D4_MD5", "nace_r2": "B-S_X_O"},  # Labor cost index (quarterly)

        # === Energy ===
        "nrg_bal_c": {"nrg_bal": "GIC", "siec": "TOTAL", "unit": "KTOE"},  # Gross inland consumption (kilotonnes of oil equivalent)

        # === Interest Rates (INFRASTRUCTURE FIX) ===
        # EI_MFIR_M: Interest rates - monthly data
        # indic options: MF-DDI-RT (day-to-day), MF-3MI-RT (3-month), MF-LTGBY-RT (long-term govt bond yield)
        "EI_MFIR_M": {"indic": "MF-LTGBY-RT"},  # Default: Long-term government bond yields (10-year equivalent)
    }

    @property
    def provider_name(self) -> str:
        return "Eurostat"

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.metadata_search = metadata_search_service
        self._dataset_labels: Dict[str, str] = {}

    async def _fetch_data(self, **params) -> NormalizedData:
        """Implement BaseProvider interface by routing to fetch_indicator."""
        return await self.fetch_indicator(
            indicator=params.get("indicator", "GDP"),
            country=params.get("country", "EU"),
            start_year=params.get("start_year"),
            end_year=params.get("end_year"),
        )

    def _country_code(self, country: str) -> str:
        """Resolve country to Eurostat code, using CountryResolver as primary source.

        Priority:
        1. Eurostat-specific aggregates (EU27_2020, EA20, etc.) - keep in local mapping
        2. CountryResolver for individual countries - unified ISO alpha-2 codes
        3. Fallback to uppercase original
        """
        key = country.upper().replace(" ", "_")

        # Check Eurostat-specific aggregates first (EU, Euro Area, etc.)
        if key in self.COUNTRY_MAPPINGS:
            return self.COUNTRY_MAPPINGS[key]

        # PHASE C: Use CountryResolver for individual country normalization
        try:
            from ..routing.country_resolver import CountryResolver
            iso_code = CountryResolver.normalize(country)
            if iso_code and len(iso_code) == 2:  # Valid ISO alpha-2
                return iso_code
        except Exception:
            pass  # Fall through to original logic

        return country.upper()

    async def fetch_indicator(
        self,
        indicator: str,
        country: str = "EU",
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> NormalizedData:
        """Fetch economic indicator data from Eurostat JSON-stat API (statistics/1.0).

        Note: The SDMX 2.1 API has compatibility issues (406 errors).
        We use the JSON-stat API which is more reliable.

        If start_year and end_year are not specified, defaults to last 5 years of data.
        """
        dataset_code, dataset_label = await self._resolve_dataset_code(indicator)
        country_code = self._country_code(country)

        # Handle special marker codes (e.g., prc_hicp_aind_core for core inflation)
        # These use the same underlying dataset but with different filter parameters
        # The actual API dataset code is the base name without the marker suffix
        original_dataset_code = dataset_code  # Keep for COICOP lookup
        if dataset_code.endswith("_core"):
            # Core inflation: strip _core suffix for API call, but COICOP filter will be applied from DATASET_DEFAULT_FILTERS
            dataset_code = dataset_code.replace("_core", "")
            logger.info(f"🔧 Core inflation detected: using {dataset_code} with core COICOP filter")

        # Use JSON-stat API endpoint (statistics/1.0) instead of SDMX
        # This is more reliable and doesn't have the 406 Not Acceptable errors
        data_url = f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset_code}"

        # Determine frequency based on dataset code
        # Quarterly datasets: gov_10q_*, lc_lci_r2_q
        # Monthly datasets: *_m suffix, EI_MFIR_M (interest rates), some sts_* (e.g., sts_inpr_m)
        # Annual datasets: default, ext_lt_* (main trade datasets are annual)
        dataset_code_lower = dataset_code.lower()
        if "_10q_" in dataset_code_lower or dataset_code_lower.endswith("_q"):
            freq = "Q"
        elif dataset_code_lower.endswith("_m"):  # Case-insensitive check for monthly datasets
            freq = "M"
        else:
            freq = "A"  # Annual by default (includes ext_lt_maineu)

        # Build query parameters
        query_params: Dict[str, str] = {
            "geo": country_code,
            "freq": freq,
        }

        # Add time range (JSON-stat uses sinceTimePeriod, not startPeriod)
        # Default to last 5 years if not specified
        current_year = datetime.now(timezone.utc).year
        query_params["sinceTimePeriod"] = str(start_year or (current_year - 5))

        # Add static defaults for this dataset (e.g., age, sex, indicator codes)
        # Use original_dataset_code to get correct filters for special marker codes (e.g., _core)
        static_defaults = self.DATASET_DEFAULT_FILTERS.get(original_dataset_code, {})
        if not static_defaults:
            # Fallback to base dataset code if marker code not found
            static_defaults = self.DATASET_DEFAULT_FILTERS.get(dataset_code, {})
        for key, value in static_defaults.items():
            query_params[key] = value

        # CRITICAL: Indicator-specific filter overrides
        # Youth unemployment queries need AGE=Y15-24 (15 to 24 years) instead of Y15-74
        indicator_upper = indicator.upper()
        if "YOUTH" in indicator_upper and dataset_code in ["une_rt_a", "une_rt_m"]:
            query_params["age"] = "Y15-24"  # 15 to 24 years old (youth unemployment)
            logger.info(f"🔧 Applied youth unemployment filter: AGE=Y15-24")

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        try:
            response = await client.get(data_url, params=query_params, timeout=30.0)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise DataNotAvailableError(
                    f"Eurostat dataset '{dataset_code}' not found for country {country_code}"
                )
            raise

        data_points, frequency = self._parse_dataset(payload, dataset_code)
        if not data_points:
            raise DataNotAvailableError(f"No data found for {country_code} in dataset {dataset_code}")

        # Apply year-over-year rate calculation if requested
        # Check if indicator name suggests rate/growth/change calculation is needed
        if self._should_calculate_rate(indicator):
            logger.info(f"Calculating year-over-year rate for indicator: {indicator}")
            data_points = self._calculate_year_over_year_change(data_points)
            # Update unit to reflect percentage change
            unit = "percent"
        else:
            # Extract unit from API response (preferred) or fallback to hardcoded mapping
            unit = self._extract_unit_from_payload(payload, dataset_code)

        # Normalize percentage values (Eurostat sometimes stores as decimals)
        if unit == "percent" or "percent" in unit.lower():
            data_points = self._normalize_percentage_values(data_points, dataset_code)

        api_url = self._compose_url(data_url, query_params)

        # Human-readable URL for data verification on Eurostat Data Browser
        source_url = f"https://ec.europa.eu/eurostat/databrowser/view/{dataset_code}/default/table?lang=en"

        # Determine seasonal adjustment from dataset code
        seasonal_adj = None
        if "_sa" in dataset_code or "sa_" in dataset_code:
            seasonal_adj = "Seasonally adjusted"
        elif "_nsa" in dataset_code or "nsa_" in dataset_code:
            seasonal_adj = "Not seasonally adjusted"

        # Determine data type from indicator name
        data_type = None
        indicator_lower = indicator.lower()
        if "rate" in indicator_lower or "percent" in indicator_lower:
            data_type = "Rate"
        elif "index" in indicator_lower or dataset_code.startswith("prc_"):
            data_type = "Index"
        elif "change" in indicator_lower or "growth" in indicator_lower:
            data_type = "Percent Change"
        else:
            data_type = "Level"

        # Determine price type
        price_type = None
        if "cp_" in dataset_code.lower() or "current" in indicator_lower:
            price_type = "Current prices"
        elif "clv" in dataset_code.lower() or "constant" in indicator_lower or "real" in indicator_lower:
            price_type = "Constant prices"

        # Extract start and end dates from data points
        start_date = data_points[0]["date"] if data_points else None
        end_date = data_points[-1]["date"] if data_points else None

        metadata = Metadata(
            source="Eurostat",
            indicator=dataset_label or payload.get("label", indicator),
            country=country_code,
            frequency=frequency,
            unit=unit,
            lastUpdated=payload.get("updated", ""),
            seriesId=dataset_code,
            apiUrl=api_url,
            sourceUrl=source_url,
            seasonalAdjustment=seasonal_adj,
            dataType=data_type,
            priceType=price_type,
            description=dataset_label or payload.get("label", indicator),
            notes=None,
            startDate=start_date,
            endDate=end_date,
        )

        return NormalizedData(metadata=metadata, data=data_points)

    def _dataset_code(self, indicator: str) -> Optional[str]:
        return self.DATASET_MAPPINGS.get(indicator.upper())

    async def _resolve_dataset_code(self, indicator: str) -> tuple[str, Optional[str]]:
        """Resolve Eurostat dataset code through hardcoded mappings, translator, or metadata search."""
        # Step 1: Try direct mapping
        mapped = self._dataset_code(indicator)
        if mapped:
            return mapped, self._dataset_labels.get(mapped)

        # Step 2: Allow users to supply raw Eurostat dataset codes directly (lowercase with underscores)
        if indicator and indicator.lower() == indicator and "_" in indicator:
            return indicator, self._dataset_labels.get(indicator)

        # Step 3: Try cross-provider indicator translator (handles IMF codes like NGDP, LUR, etc.)
        translator = get_indicator_translator()
        translated_code, concept_name = translator.translate_indicator(indicator, "EUROSTAT")
        if translated_code:
            logger.info(f"Eurostat: Translated '{indicator}' to '{translated_code}' via concept '{concept_name}'")
            return translated_code, concept_name

        if not self.metadata_search:
            raise DataNotAvailableError(
                f"Eurostat dataset for '{indicator}' not recognized. Provide the dataset code (e.g., nama_10_gdp) or enable metadata discovery."
            )

        # Use hierarchical search: SDMX first, then Eurostat REST API
        search_results = await self.metadata_search.search_with_sdmx_fallback(
            provider="Eurostat",
            indicator=indicator,
        )
        if not search_results:
            raise DataNotAvailableError(
                f"Eurostat dataset for '{indicator}' not found. Try a different description or provide the dataset code directly."
            )

        discovery = await self.metadata_search.discover_indicator(
            provider="Eurostat",
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
                f"Try specifying the exact metric you need (e.g., 'EU non-financial corporation debt' or 'EU non-financial corporation investment')."
            )

        if discovery and discovery.get("code"):
            code = discovery["code"]
            label = discovery.get("name")
            self.DATASET_MAPPINGS[indicator.upper()] = code
            if label:
                self._dataset_labels[code] = label
            return code, label

        raise DataNotAvailableError(
            f"Eurostat dataset for '{indicator}' not found. Try refining your query or consult the Eurostat dataset catalog."
        )


    def _extract_dataset_label(self, structure: Dict[str, Any]) -> Optional[str]:
        dataset = structure.get("dataset")
        if isinstance(dataset, list) and dataset:
            dataset = dataset[0]
        if isinstance(dataset, dict):
            for key in ("label", "title", "name"):
                value = dataset.get(key)
                text = self._extract_text(value)
                if text:
                    return text
        return None

    def _get_dimension_order(self, structure: Optional[Dict[str, Any]], dataset_code: str) -> list[str]:
        """Extract the correct dimension order from dataset structure.

        Returns a list of dimension IDs in the order they should appear in the SDMX key.
        Falls back to common patterns if structure is unavailable.
        """
        if structure:
            dimensions = self._iter_dimensions(structure)
            dimension_ids = []
            for dimension in dimensions:
                dim_id = dimension.get("id") or dimension.get("name") or dimension.get("code")
                if dim_id:
                    dim_key = str(dim_id).strip().lower()
                    # Skip time dimensions (they're query params, not key dimensions)
                    if dim_key not in {"time", "time_period", "timeperiod"}:
                        dimension_ids.append(dim_key)
            if dimension_ids:
                return dimension_ids

        # Fallback to common Eurostat SDMX 2.1 patterns by dataset
        # Based on official Eurostat API documentation
        if dataset_code == "nama_10_gdp":
            # National Accounts: [FREQ].[UNIT].[NA_ITEM].[GEO]
            return ["freq", "unit", "na_item", "geo"]
        elif dataset_code == "une_rt_a":
            # Unemployment rate: [FREQ].[AGE].[SEX].[GEO]
            return ["freq", "age", "sex", "geo"]
        elif dataset_code == "prc_hicp_aind":
            # HICP inflation: [FREQ].[COICOP].[GEO]
            return ["freq", "coicop", "geo"]
        else:
            # Generic fallback: frequency, geography
            return ["freq", "geo"]

    def _extract_dimension_defaults(self, structure: Dict[str, Any]) -> Dict[str, str]:
        defaults: Dict[str, str] = {}
        for dimension in self._iter_dimensions(structure):
            dim_id = dimension.get("id") or dimension.get("name") or dimension.get("code")
            if not dim_id:
                continue
            dim_key = str(dim_id).strip()
            if not dim_key:
                continue
            dim_key_lower = dim_key.lower()
            if dim_key_lower in {"time", "time_period", "timeperiod"}:
                continue

            value_id = self._extract_default_value(dimension) or self._extract_first_value(dimension)
            if value_id:
                defaults[dim_key_lower] = value_id

            if dim_key_lower == "unit":
                unit_label = self._extract_value_label(dimension, value_id)
                if unit_label:
                    defaults["_unit_label"] = unit_label
        return defaults

    def _iter_dimensions(self, structure: Dict[str, Any]) -> list[Dict[str, Any]]:
        dataset = structure.get("dataset")
        candidates = []
        if isinstance(dataset, dict):
            candidates = self._normalize_dimensions(dataset.get("dimensions"))
        if not candidates and isinstance(structure, dict):
            candidates = self._normalize_dimensions(structure.get("dimensions"))
        return candidates

    def _normalize_dimensions(self, value: Any) -> list[Dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for key in ("dimension", "dimensions", "observation"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            items = []
            for key, nested in value.items():
                if isinstance(nested, dict):
                    candidate = dict(nested)
                    candidate.setdefault("id", key)
                    items.append(candidate)
            return items
        return []

    def _extract_default_value(self, dimension: Dict[str, Any]) -> Optional[str]:
        candidates = [
            dimension.get("default"),
            dimension.get("defaultId"),
            dimension.get("defaultMember"),
            dimension.get("defaultValue"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str):
                return candidate
            if isinstance(candidate, dict):
                for key in ("id", "value", "code"):
                    value = candidate.get(key)
                    if isinstance(value, str):
                        return value
        return None

    def _extract_first_value(self, dimension: Dict[str, Any]) -> Optional[str]:
        values = dimension.get("values")
        if isinstance(values, list):
            for entry in values:
                if isinstance(entry, dict):
                    value_id = entry.get("id") or entry.get("value") or entry.get("code")
                    if isinstance(value_id, str):
                        return value_id
        if isinstance(values, dict):
            # Some payloads map value id -> label
            for key, entry in values.items():
                if isinstance(entry, dict):
                    value_id = entry.get("id") or entry.get("value") or entry.get("code")
                    if isinstance(value_id, str):
                        return value_id
                if isinstance(key, str):
                    return key
        return None

    def _extract_value_label(self, dimension: Dict[str, Any], value_id: Optional[str]) -> Optional[str]:
        if not value_id:
            return None
        values = dimension.get("values")
        if isinstance(values, list):
            for entry in values:
                if not isinstance(entry, dict):
                    continue
                entry_id = entry.get("id") or entry.get("value") or entry.get("code")
                if entry_id == value_id:
                    return self._extract_text(entry.get("label") or entry.get("name"))
        if isinstance(values, dict):
            entry = values.get(value_id)
            if isinstance(entry, dict):
                return self._extract_text(entry.get("label") or entry.get("name"))
        return None

    def _extract_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("en", "EN", "value", "text", "label", "name"):
                text = value.get(key)
                if isinstance(text, str):
                    return text
                if isinstance(text, list) and text:
                    extracted = self._extract_text(text[0])
                    if extracted:
                        return extracted
            return ""
        if isinstance(value, list):
            for item in value:
                extracted = self._extract_text(item)
                if extracted:
                    return extracted
        return ""

    def _build_time_period(self, start_year: Optional[int], end_year: Optional[int]) -> str:
        current_year = datetime.now(timezone.utc).year
        default_start = current_year - 9
        start = start_year or default_start
        end = end_year or current_year
        if start > end:
            start, end = end, start
        if start == end:
            return str(start)
        return f"{start}:{end}"

    def _compose_url(self, base_url: str, params: Dict[str, Any]) -> str:
        if not params:
            return base_url
        from urllib.parse import urlencode

        return f"{base_url}?{urlencode(params)}"

    def _parse_dataset(self, payload: Dict[str, Any], dataset_code: str) -> tuple[list[Dict[str, Any]], str]:
        """Parse JSON-stat 2.0 format from Eurostat API.

        The JSON-stat format has a flat structure with:
        - value: dict/array of data values indexed by position
        - dimension: metadata about dimensions including time
        """
        # Check for JSON-stat format (statistics/1.0 API)
        if "value" in payload and "dimension" in payload:
            data_points = self._parse_json_stat(payload, dataset_code)
            frequency = self._infer_frequency(payload.get("dimension", {}).get("time", {}), dataset_code)
            return data_points, frequency

        # Fallback: try SDMX-JSON format (data/sdmx API) - legacy support
        data_section = payload.get("data", {})
        datasets = data_section.get("dataset") or data_section.get("datasets") or []
        if isinstance(datasets, dict):
            datasets = [datasets]
        if not datasets:
            return [], "annual"  # Return empty instead of raising error

        dataset = datasets[0]
        values = dataset.get("value", {})
        if isinstance(values, list):
            values = {str(idx): val for idx, val in enumerate(values)}

        if not values:
            return [], "annual"  # Return empty if no values

        dimensions = dataset.get("dimension", {})
        time_dim = dimensions.get("time") or {}
        category = time_dim.get("category") or {}
        indexes = category.get("index") or {}

        if not indexes:
            return [], "annual"  # Return empty if no time dimension

        ordered = sorted(indexes.items(), key=lambda item: item[1])

        data_points: list[Dict[str, Any]] = []
        for label, idx in ordered:
            value = values.get(str(idx))
            if value is None:
                continue
            data_points.append(
                {
                    "date": self._normalize_time_label(label),
                    "value": value,
                }
            )

        frequency = self._infer_frequency(time_dim, dataset_code)
        return data_points, frequency

    def _parse_json_stat(self, payload: Dict[str, Any], dataset_code: str) -> list[Dict[str, Any]]:
        """Parse JSON-stat 2.0 format from Eurostat API with proper unit selection."""
        values = payload.get("value", {})
        dimensions = payload.get("dimension", {})
        time_dim = dimensions.get("time", {})
        indexes = time_dim.get("category", {}).get("index", {})
        ordered = sorted(indexes.items(), key=lambda item: item[1])

        # Get dimension sizes to calculate positions
        sizes = payload.get("size", [])
        id_list = payload.get("id", [])

        # For unemployment rate (une_rt_a), we need PC_ACT (percentage of active population)
        # For other datasets, take the first/default unit
        unit_index = 0
        if dataset_code == "une_rt_a" and "unit" in dimensions:
            unit_dim = dimensions.get("unit", {})
            unit_indexes = unit_dim.get("category", {}).get("index", {})
            # Prefer PC_ACT for unemployment rate
            if "PC_ACT" in unit_indexes:
                unit_index = unit_indexes["PC_ACT"]
            elif "PC" in unit_indexes:
                unit_index = unit_indexes["PC"]

        data_points: list[Dict[str, Any]] = []

        # Calculate the correct value index based on dimensions
        for label, idx in ordered:
            # Build the position in the flattened array
            if len(sizes) == len(id_list) and "unit" in id_list:
                # Find positions of unit and time in the dimension list
                unit_pos = id_list.index("unit")
                time_pos = id_list.index("time") if "time" in id_list else -1

                # Calculate the flattened index
                position = 0
                multiplier = 1

                # Work backwards through dimensions to calculate position
                for i in range(len(id_list) - 1, -1, -1):
                    if i == time_pos:
                        position += idx * multiplier
                    elif i == unit_pos:
                        position += unit_index * multiplier
                    # Other dimensions default to 0 (first value)

                    if i > 0:
                        multiplier *= sizes[i]

                value = values.get(str(position))
            else:
                # Fallback to simple time-based indexing
                value = values.get(str(idx))

            if value is None:
                continue

            data_points.append(
                {
                    "date": self._normalize_time_label(label),
                    "value": value,
                }
            )
        return data_points

    def _normalize_time_label(self, label: str) -> str:
        if label and "-" in label:
            if "Q" in label:
                year, quarter = label.split("-Q")
                month = (int(quarter) - 1) * 3 + 1
                return f"{year}-{month:02d}-01"
            if "M" in label:
                year, month = label.split("-")
                return f"{year}-{month}-01"
        return f"{label}-01-01"

    def _should_calculate_rate(self, indicator: str, query: str = "") -> bool:
        """Determine if we should calculate year-over-year rate from index data.

        IMPORTANT: Only apply to INDEX data that needs conversion to growth rates.
        Do NOT apply to data that is ALREADY a rate (like unemployment rate, inflation rate).
        """
        indicator_lower = indicator.lower()
        query_lower = query.lower()

        # CRITICAL: Do NOT calculate rate for data that is ALREADY a rate/percentage
        # These indicators are already expressed as percentages - no conversion needed
        already_rate_indicators = [
            "unemployment",  # Unemployment rate is already a percentage
            "inflation",     # Inflation rate is already a percentage
            "interest rate", # Interest rate is already a percentage
            "employment rate",  # Employment rate is already a percentage
        ]
        for rate_indicator in already_rate_indicators:
            if rate_indicator in indicator_lower or rate_indicator in query_lower:
                return False

        # Only apply to growth/change queries for INDEX data
        growth_keywords = ["growth", "change", "yoy", "year-over-year"]
        return any(keyword in indicator_lower or keyword in query_lower for keyword in growth_keywords)

    def _calculate_year_over_year_change(self, data: list[dict]) -> list[dict]:
        """Calculate year-over-year percentage change from index values."""
        if not data or len(data) < 2:
            return data

        result: list[dict] = []
        for i in range(1, len(data)):
            prev_value = data[i-1].get('value')
            curr_value = data[i].get('value')

            # Skip if either value is None or prev_value is 0 (avoid division by zero)
            if prev_value is None or curr_value is None or prev_value == 0:
                continue

            yoy_change = ((curr_value - prev_value) / prev_value) * 100
            result.append({
                'date': data[i]['date'],
                'value': round(yoy_change, 2)
            })

        return result

    def _infer_frequency(self, time_dimension: Dict[str, Any], dataset_code: str) -> str:
        category = time_dimension.get("category", {})
        labels = list((category.get("index") or {}).keys())
        if not labels:
            labels = list((category.get("label") or {}).keys())

        if labels:
            sample = labels[0]
            if "-Q" in sample:
                return "quarterly"
            if "-" in sample and len(sample.split("-")[-1]) == 2:
                return "monthly"
        if dataset_code in {"nama_10_gdp"}:
            return "annual"
        return "annual"

    def _extract_unit_from_payload(self, payload: Dict[str, Any], dataset_code: str) -> str:
        """
        Extract the unit label from the JSON-stat payload.

        This method extracts the actual unit from the API response instead of
        relying on hardcoded mappings.

        Args:
            payload: JSON-stat response from Eurostat API
            dataset_code: Dataset code for fallback logic

        Returns:
            Human-readable unit label (e.g., "Million euro", "Percentage")
        """
        try:
            dimensions = payload.get("dimension", {})
            unit_dim = dimensions.get("unit", {})

            if unit_dim:
                category = unit_dim.get("category", {})
                labels = category.get("label", {})
                indexes = category.get("index", {})

                # Get the first (or default) unit label
                if labels:
                    # If there's only one unit, use it
                    if len(labels) == 1:
                        return next(iter(labels.values()))

                    # For unemployment rate, prefer PC_ACT
                    if dataset_code == "une_rt_a":
                        for code, label in labels.items():
                            if code in ["PC_ACT", "PC"]:
                                return label

                    # Return the first label
                    return next(iter(labels.values()))

            # Check for unit in the dataset label
            label = payload.get("label", "")
            if label:
                # Try to extract unit from label (e.g., "GDP - Million EUR")
                if " - " in label:
                    parts = label.split(" - ")
                    if len(parts) > 1:
                        potential_unit = parts[-1].strip()
                        if any(u in potential_unit.lower() for u in ["euro", "percent", "million", "thousand", "index"]):
                            return potential_unit

        except Exception as e:
            logger.debug(f"Failed to extract unit from payload: {e}")

        # Fall back to hardcoded mappings
        return self._infer_unit_fallback(dataset_code)

    def _infer_unit_fallback(self, dataset_code: str) -> str:
        """Fallback unit inference for datasets without proper unit metadata."""
        # National accounts datasets
        if "nama_" in dataset_code:
            if "gdp" in dataset_code.lower() or "B1G" in dataset_code:
                return "million EUR"
            return "million EUR"

        # Price indices
        if "prc_" in dataset_code or "hicp" in dataset_code.lower():
            return "index (2015=100)"

        # Unemployment datasets
        if "une_" in dataset_code or "lfsa_" in dataset_code:
            return "percent"

        # Government finance
        if "gov_" in dataset_code:
            if "_gdp" in dataset_code:
                return "percent of GDP"
            return "million EUR"

        # Non-financial corporations / sectoral accounts
        if "nasa_" in dataset_code or "_nf_" in dataset_code:
            return "million EUR"

        # Trade data
        if "ext_" in dataset_code or "comext" in dataset_code:
            return "million EUR"

        # Employment data
        if "lfst_" in dataset_code or "employ" in dataset_code.lower():
            return "thousand persons"

        return ""

    def _infer_unit(self, dataset_code: str) -> str:
        """Legacy method for backward compatibility."""
        return self._infer_unit_fallback(dataset_code)

    def _normalize_percentage_values(self, data: list[dict], dataset_code: str) -> list[dict]:
        """
        Normalize percentage values that are stored as decimals.
        If values are < 1.5 in absolute value, multiply by 100.

        Args:
            data: List of data points with 'date' and 'value' keys
            dataset_code: Dataset code for detection logic

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
        # Exception: Negative values (e.g., GDP contraction) can be < -1, so we use absolute values
        if max_value < 1.5:
            logger.info(f"Normalizing percentage values for dataset: {dataset_code} (max value: {max_value})")
            return [
                {'date': d['date'], 'value': d['value'] * 100 if d['value'] is not None else None}
                for d in data
            ]

        return data
