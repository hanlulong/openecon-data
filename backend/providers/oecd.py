from __future__ import annotations

import asyncio
import logging
import json
import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from pathlib import Path

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError, retry_async
from .base import BaseProvider
from ..services.dsd_cache import get_dimension_key_builder
from ..services.cache import cache_service
from ..services.rate_limiter import (
    ProviderRateLimitWaitExceeded,
    wait_for_provider,
    record_provider_request,
    record_provider_rate_limit_error,
    record_provider_success,
    is_provider_circuit_open,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.metadata_search import MetadataSearchService


class OECDProvider(BaseProvider):
    """OECD Statistics API provider for international economic data.

    Uses SDMX-JSON format. No API key required.
    Documentation: https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html

    Inherits from BaseProvider for circuit breaker protection and
    standardized provider interface. Keeps custom SDMX-specific HTTP/retry
    logic (rate limiter, DSD cache) since OECD's API has unique requirements.

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

    CANONICAL_DATAFLOW_ALIASES: Dict[str, tuple[str, str, str]] = {
        "gdp": ("OECD.SDD.NAD", "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE", "1.0"),
        "gross domestic product": ("OECD.SDD.NAD", "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE", "1.0"),
        "nominal gdp": ("OECD.SDD.NAD", "DSD_NAMAIN10@DF_TABLE1_EXPENDITURE", "1.0"),
    }

    # Cached dataflows catalog (loaded once per process)
    _DATAFLOWS_CATALOG: Optional[Dict] = None
    _DATAFLOW_STRUCTURE_CACHE: Dict[str, Dict[str, Any]] = {}

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

    # Region expansions removed - all region definitions now consolidated in
    # CountryResolver (backend/routing/country_resolver.py) as the single source of truth.
    # The expand_countries() method below uses CountryResolver.get_region_expansion().

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None) -> None:
        super().__init__(timeout=50.0)  # OECD SDMX API is slow
        settings = get_settings()
        self.base_url = settings.oecd_base_url.rstrip("/")
        self.metadata_search = metadata_search_service

    @property
    def provider_name(self) -> str:
        return "OECD"

    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Route to fetch_indicator for BaseProvider interface compliance."""
        return await self.fetch_indicator(
            indicator=params.get("indicator", "GDP"),
            country=params.get("country", "USA"),
            start_year=params.get("start_year"),
            end_year=params.get("end_year"),
        )

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

    @staticmethod
    def _canonical_dataflow_code(dataflow_code: str) -> str:
        """Return the OECD SDMX dataflow ID without local catalog prefixes."""
        code = str(dataflow_code or "").strip()
        if code.upper().startswith("OECD_"):
            return code[5:]
        return code

    @classmethod
    def _lookup_dataflow_registry_metadata(cls, dataflow_code: str) -> tuple[Optional[str], Optional[str]]:
        """Look up agency/version for an exact OECD dataflow from the local registry.

        OECD's public SDMX API requires the owning agency and active DSD
        version in the URL.  The dataflow ID alone is not enough: education,
        labour, and tax-benefit dataflows commonly live outside the heuristic
        ``OECD.SDD.*`` agencies, and some active structures are not version
        ``1.0``.  The indicator registry stores the provider-native
        ``agencyID`` and ``version`` captured from OECD metadata; use it before
        falling back to heuristics.
        """
        code = cls._canonical_dataflow_code(dataflow_code)
        if not re.fullmatch(r"DSD_[A-Za-z0-9_]+@DF_[A-Za-z0-9_]+", code):
            return None, None

        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
            row = lookup.get("OECD", code)
        except Exception as exc:
            logger.debug("OECD registry lookup skipped for %s: %s", code, exc)
            row = None

        if not row:
            return None, None

        row_code = str(row.get("code") or "").strip()
        if row_code.upper() != code.upper():
            return None, None

        raw_metadata = row.get("raw_metadata")
        metadata: dict = {}
        if isinstance(raw_metadata, str) and raw_metadata.strip():
            try:
                parsed = json.loads(raw_metadata)
                if isinstance(parsed, dict):
                    metadata = parsed
            except json.JSONDecodeError:
                logger.debug("OECD registry raw metadata is not JSON for %s", code)
        elif isinstance(raw_metadata, dict):
            metadata = raw_metadata

        agency = str(metadata.get("agencyID") or metadata.get("agency") or "").strip() or None
        version = str(metadata.get("version") or "").strip() or None

        structure = str(metadata.get("structure") or "").strip()
        if structure:
            structure_match = re.search(
                r"DataStructure=([^:()]+):[^()]+(?:\(([^()]+)\))?",
                structure,
            )
            if structure_match:
                agency = agency or structure_match.group(1)
                version = version or structure_match.group(2)

        if agency and not re.fullmatch(r"[A-Za-z0-9_.-]+", agency):
            agency = None
        if version and not re.fullmatch(r"[0-9][A-Za-z0-9_.-]*", version):
            version = None

        return agency, version

    @staticmethod
    def _oecd_structure_cache_key(base_url: str, agency: str, dataflow: str, version: str) -> str:
        """Return a stable cache key for OECD dataflow structure metadata."""
        return "|".join(
            [
                str(base_url or "").rstrip("/"),
                str(agency or ""),
                str(dataflow or ""),
                str(version or ""),
            ]
        )

    @staticmethod
    def _oecd_rest_base_from_link(href: str) -> Optional[str]:
        """Extract an OECD SDMX REST base URL from a dataflow external-reference link."""
        link = str(href or "").strip()
        if "/rest/" not in link:
            return None
        base = link.split("/rest/", 1)[0] + "/rest"
        if not re.fullmatch(r"https://sdmx\.oecd\.org/[A-Za-z0-9_-]+/rest", base):
            return None
        return base.rstrip("/")

    @staticmethod
    def _annotation_value(annotations: Any, annotation_id: str) -> Optional[str]:
        if not isinstance(annotations, list):
            return None
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            if str(annotation.get("id") or "") == annotation_id:
                value = annotation.get("title")
                return str(value) if value is not None else None
        return None

    @classmethod
    def _parse_oecd_dataflow_structure(
        cls,
        payload: Dict[str, Any],
        base_url: str,
    ) -> Dict[str, Any]:
        """Parse OECD structure/dataflow metadata into provider-friendly fields.

        OECD's `structure/dataflow` response is the most reliable source for
        the dataflow's DSD, dimension order, content constraints, observation
        count, and external dissemination-space links.
        """
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}

        dataflows = data.get("dataflows") if isinstance(data.get("dataflows"), list) else []
        dataflow_info = dataflows[0] if dataflows and isinstance(dataflows[0], dict) else {}

        data_structures = (
            data.get("dataStructures")
            if isinstance(data.get("dataStructures"), list)
            else []
        )
        dsd = data_structures[0] if data_structures and isinstance(data_structures[0], dict) else {}
        components = dsd.get("dataStructureComponents", {}) if isinstance(dsd, dict) else {}
        dim_list = components.get("dimensionList", {}) if isinstance(components, dict) else {}
        raw_dimensions = dim_list.get("dimensions", []) if isinstance(dim_list, dict) else []

        dimensions: List[Dict[str, Any]] = []
        for idx, dim in enumerate(raw_dimensions):
            if not isinstance(dim, dict):
                continue
            position = dim.get("position", idx)
            if not isinstance(position, int):
                try:
                    position = int(position)
                except (TypeError, ValueError):
                    position = idx
            local_representation = dim.get("localRepresentation", {})
            if not isinstance(local_representation, dict):
                local_representation = {}
            dimensions.append(
                {
                    "id": dim.get("id"),
                    "position": position,
                    "name": dim.get("name", dim.get("id")),
                    "codelist": local_representation.get("enumeration"),
                }
            )
        dimensions.sort(key=lambda item: item.get("position", 0))

        valid_values_by_dimension: Dict[str, set[str]] = {}
        time_ranges: List[Dict[str, Any]] = []
        obs_count: Optional[int] = None
        content_constraints = (
            data.get("contentConstraints")
            if isinstance(data.get("contentConstraints"), list)
            else []
        )
        for constraint in content_constraints:
            if not isinstance(constraint, dict):
                continue
            obs_count_text = cls._annotation_value(constraint.get("annotations"), "obs_count")
            if obs_count_text and obs_count is None:
                try:
                    obs_count = int(float(obs_count_text))
                except (TypeError, ValueError):
                    obs_count = None

            for cube_region in constraint.get("cubeRegions", []) or []:
                if not isinstance(cube_region, dict) or cube_region.get("isIncluded") is False:
                    continue
                for key_value in cube_region.get("keyValues", []) or []:
                    if not isinstance(key_value, dict):
                        continue
                    dim_id = str(key_value.get("id") or "").strip()
                    if not dim_id:
                        continue
                    values = key_value.get("values")
                    if isinstance(values, list):
                        valid_values_by_dimension.setdefault(dim_id, set()).update(
                            str(value) for value in values if value is not None
                        )
                    time_range = key_value.get("timeRange")
                    if isinstance(time_range, dict):
                        time_ranges.append({"dimension": dim_id, "timeRange": time_range})

        links = dataflow_info.get("links", []) if isinstance(dataflow_info, dict) else []
        external_base_urls: List[str] = []
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                rest_base = cls._oecd_rest_base_from_link(str(link.get("href") or ""))
                if rest_base and rest_base not in external_base_urls:
                    external_base_urls.append(rest_base)

        return {
            "base_url": str(base_url or "").rstrip("/"),
            "agency": dataflow_info.get("agencyID"),
            "dataflow": dataflow_info.get("id"),
            "version": dataflow_info.get("version"),
            "name": dataflow_info.get("name"),
            "is_external_reference": bool(dataflow_info.get("isExternalReference")),
            "external_base_urls": external_base_urls,
            "dsd_id": dsd.get("id") if isinstance(dsd, dict) else None,
            "dsd_agency": dsd.get("agencyID") if isinstance(dsd, dict) else None,
            "dsd_version": dsd.get("version") if isinstance(dsd, dict) else None,
            "dimensions": dimensions,
            "dimension_ids": [dim.get("id") for dim in dimensions],
            "valid_values_by_dimension": {
                dim_id: sorted(values) for dim_id, values in valid_values_by_dimension.items()
            },
            "time_ranges": time_ranges,
            "obs_count": obs_count,
        }

    async def _fetch_oecd_dataflow_structure_at_base(
        self,
        base_url: str,
        agency: str,
        dataflow: str,
        version: str,
    ) -> Dict[str, Any]:
        cache_key = self._oecd_structure_cache_key(base_url, agency, dataflow, version)
        cached = self._DATAFLOW_STRUCTURE_CACHE.get(cache_key)
        if cached:
            return cached

        url = (
            f"{base_url.rstrip('/')}/v2/structure/dataflow/"
            f"{agency}/{dataflow}/{version}"
        )
        params = {"references": "all", "detail": "referencepartial"}
        headers = {
            "Accept": "application/vnd.sdmx.structure+json;version=1.0",
            "Accept-Encoding": "gzip, deflate, br",
        }
        http_client = get_http_client()
        response = await http_client.get(url, params=params, headers=headers, timeout=60.0)
        response.raise_for_status()
        metadata = self._parse_oecd_dataflow_structure(response.json(), base_url.rstrip("/"))
        metadata["structureUrl"] = str(response.request.url)
        self._DATAFLOW_STRUCTURE_CACHE[cache_key] = metadata
        return metadata

    async def _get_oecd_dataflow_structure(
        self,
        agency: str,
        dataflow: str,
        version: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch OECD dataflow structure metadata, following external space links.

        The main `/public` space may return only an external-reference stub for
        large dedicated-space dataflows (for example TiVA under `sti-public`).
        When that happens, follow the advertised link and re-run the same
        structure query against that REST base.
        """
        try:
            metadata = await self._fetch_oecd_dataflow_structure_at_base(
                self.base_url,
                agency,
                dataflow,
                version,
            )
        except Exception as exc:
            logger.info(
                "OECD structure/dataflow lookup failed for %s,%s,%s at %s: %s",
                agency,
                dataflow,
                version,
                self.base_url,
                exc,
            )
            metadata = None

        if metadata and metadata.get("dimensions"):
            return metadata

        external_bases = list(metadata.get("external_base_urls", [])) if metadata else []
        for external_base in external_bases:
            if external_base.rstrip("/") == self.base_url.rstrip("/"):
                continue
            try:
                external_metadata = await self._fetch_oecd_dataflow_structure_at_base(
                    external_base,
                    agency,
                    dataflow,
                    version,
                )
            except Exception as exc:
                logger.info(
                    "OECD external structure lookup failed for %s,%s,%s at %s: %s",
                    agency,
                    dataflow,
                    version,
                    external_base,
                    exc,
                )
                continue
            if external_metadata.get("dimensions"):
                logger.info(
                    "Using OECD dedicated dissemination space for %s: %s",
                    dataflow,
                    external_metadata.get("base_url"),
                )
                return external_metadata

        return metadata

    @staticmethod
    def _build_oecd_key_from_structure(
        structure_metadata: Dict[str, Any],
        country_code: str,
        custom_defaults: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Build an SDMX v1 positional key from OECD structure metadata."""
        dimensions = structure_metadata.get("dimensions") or []
        if not dimensions:
            return None

        key_parts = [""] * len(dimensions)
        defaults = custom_defaults or {}
        country_dims = {"REF_AREA", "geo", "COUNTRY"}
        filled = False

        for array_idx, dim in enumerate(dimensions):
            if not isinstance(dim, dict):
                continue
            dim_id = str(dim.get("id") or "")
            position = dim.get("position", array_idx)
            if not isinstance(position, int) or position < 0 or position >= len(key_parts):
                position = array_idx

            if dim_id in country_dims:
                key_parts[position] = str(country_code)
                filled = True
            elif dim_id in defaults and defaults[dim_id]:
                key_parts[position] = str(defaults[dim_id])
                filled = True
            elif dim_id == "FREQ" and defaults.get("frequency"):
                key_parts[position] = str(defaults["frequency"])
                filled = True

        if not filled:
            return "all"
        return ".".join(key_parts)

    @staticmethod
    def _year_from_oecd_period(period: Any) -> Optional[int]:
        match = re.search(r"(\d{4})", str(period or ""))
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @classmethod
    def _valid_year_range_from_structure(
        cls,
        structure_metadata: Dict[str, Any],
    ) -> tuple[Optional[int], Optional[int]]:
        """Return the valid data year range advertised by OECD constraints."""
        for entry in structure_metadata.get("time_ranges") or []:
            if not isinstance(entry, dict):
                continue
            time_range = entry.get("timeRange")
            if not isinstance(time_range, dict):
                continue
            start_info = time_range.get("startPeriod", {})
            end_info = time_range.get("endPeriod", {})
            start_year = cls._year_from_oecd_period(
                start_info.get("period") if isinstance(start_info, dict) else start_info
            )
            end_year = cls._year_from_oecd_period(
                end_info.get("period") if isinstance(end_info, dict) else end_info
            )
            if start_year or end_year:
                return start_year, end_year
        return None, None

    @classmethod
    def _clamp_default_time_params_to_oecd_constraints(
        cls,
        params: Dict[str, str],
        structure_metadata: Optional[Dict[str, Any]],
    ) -> None:
        """Clamp provider-generated default dates to OECD's valid time range.

        This is intentionally used only for provider-generated default windows,
        not explicit user-requested dates, so a true user request for an
        unavailable year still surfaces as unavailable rather than silently
        returning a different year.
        """
        if not structure_metadata:
            return
        valid_start, valid_end = cls._valid_year_range_from_structure(structure_metadata)
        if valid_start is None and valid_end is None:
            return

        request_start = cls._year_from_oecd_period(params.get("startPeriod"))
        request_end = cls._year_from_oecd_period(params.get("endPeriod"))

        if request_start is None and request_end is None:
            if valid_start is not None:
                params["startPeriod"] = str(valid_start)
            if valid_end is not None:
                params["endPeriod"] = str(valid_end)
            return

        no_overlap = (
            (request_end is not None and valid_start is not None and request_end < valid_start)
            or (request_start is not None and valid_end is not None and request_start > valid_end)
        )
        if no_overlap:
            if valid_start is not None:
                params["startPeriod"] = str(valid_start)
            if valid_end is not None:
                params["endPeriod"] = str(valid_end)
            return

        if request_start is not None and valid_start is not None and request_start < valid_start:
            params["startPeriod"] = str(valid_start)
        if request_end is not None and valid_end is not None and request_end > valid_end:
            params["endPeriod"] = str(valid_end)

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

    def _country_label(self, country_code: str) -> str:
        """Convert OECD country codes back to a human-readable country label."""
        try:
            from ..routing.country_resolver import CountryResolver

            iso2 = CountryResolver.to_iso2(country_code.upper()) or CountryResolver.normalize(country_code)
            if iso2:
                preferred = None
                for alias, code in CountryResolver.COUNTRY_ALIASES.items():
                    if code != iso2.upper():
                        continue
                    alias_text = str(alias).strip()
                    if len(alias_text) <= 2:
                        continue
                    if preferred is None or len(alias_text) > len(preferred):
                        preferred = alias_text
                if preferred:
                    return preferred.title()
        except Exception:
            pass
        return country_code

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
        raw_indicator = str(indicator or "").strip()
        explicit_dataflow = raw_indicator.upper()

        exact_parts = [part.strip() for part in raw_indicator.split(",")]
        if len(exact_parts) in (2, 3):
            exact_agency = exact_parts[0]
            exact_dataflow = self._canonical_dataflow_code(exact_parts[1])
            exact_version = exact_parts[2] if len(exact_parts) == 3 else ""
            if (
                re.fullmatch(r"[A-Za-z0-9_.-]+", exact_agency or "")
                and re.fullmatch(r"DSD_[A-Za-z0-9_]+@DF_[A-Za-z0-9_]+", exact_dataflow or "")
                and (not exact_version or re.fullmatch(r"[0-9][A-Za-z0-9_.-]*", exact_version))
            ):
                _, registry_version = self._lookup_dataflow_registry_metadata(exact_dataflow)
                result = (exact_agency, exact_dataflow, exact_version or registry_version or "1.0")
                logger.info("🔒 Treating exact OECD agency/dataflow tuple as resolved: %s", result)
                cache_service.set(f"oecd_indicator:{explicit_dataflow}", result, ttl=86400)
                return result

        if re.fullmatch(r"DSD_[A-Z0-9_]+@DF_[A-Z0-9_]+", explicit_dataflow):
            logger.info("🔒 Treating explicit OECD dataflow as resolved: %s", explicit_dataflow)
            result = self._build_result_from_discovery(explicit_dataflow, {})
            cache_service.set(f"oecd_indicator:{explicit_dataflow}", result, ttl=86400)
            return result

        explicit_prefix = explicit_dataflow
        if explicit_prefix.startswith("OECD_"):
            explicit_prefix = explicit_prefix[len("OECD_"):]
        if explicit_prefix.startswith("DSD_") and "@DF_" in explicit_prefix:
            catalog = self._load_dataflows_catalog()
            prefix_matches = [
                flow_id
                for flow_id in catalog
                if flow_id.upper().startswith(explicit_prefix)
            ]
            if prefix_matches:
                # Prefer the shortest matching catalog key to avoid drifting to
                # longer, more specialized derivatives when the fragment already
                # identifies a common parent dataflow.
                exact_matches = [
                    flow_id
                    for flow_id in prefix_matches
                    if flow_id.upper() == explicit_prefix
                ]
                flow_id = exact_matches[0] if exact_matches else min(prefix_matches, key=len)
                logger.info(
                    "🔒 Resolved OECD dataflow prefix '%s' -> %s via local catalog",
                    explicit_dataflow,
                    flow_id,
                )
                if exact_matches:
                    result = self._build_result_from_discovery(flow_id, {})
                else:
                    structure = flow_id.split("@")[0]
                    result = (
                        self._extract_agency_from_structure(structure, flow_id),
                        self._canonical_dataflow_code(flow_id),
                        "1.0",
                    )
                cache_service.set(f"oecd_indicator:{explicit_dataflow}", result, ttl=86400)
                return result

        normalized_indicator = re.sub(r"\s+", " ", str(indicator or "").replace("_", " ").strip().lower())
        canonical_dataflow = self.CANONICAL_DATAFLOW_ALIASES.get(normalized_indicator)
        if canonical_dataflow:
            logger.info(
                "📌 Using canonical OECD dataflow alias for '%s' -> %s",
                indicator,
                canonical_dataflow[1],
            )
            cache_service.set(f"oecd_indicator:{str(indicator or '').upper()}", canonical_dataflow, ttl=86400)
            return canonical_dataflow

        lookup_terms = self._build_indicator_lookup_terms(indicator)
        if not lookup_terms:
            raise DataNotAvailableError("OECD indicator is empty")

        # STEP 1: Check cache first (for all lookup aliases)
        cache_keys = [f"oecd_indicator:{term.upper()}" for term in lookup_terms]
        for cache_key in cache_keys:
            cached = cache_service.get(cache_key)
            if cached:
                logger.info(f"🔄 Cache hit for OECD indicator lookup key: {cache_key}")
                return cached

        logger.info(
            "🔍 Resolving OECD indicator '%s' with lookup terms: %s",
            indicator,
            lookup_terms[:4],
        )

        # STEP 2: Use metadata search if available (PRIMARY method)
        if self.metadata_search:
            try:
                ambiguity_error: Optional[DataNotAvailableError] = None
                for idx, lookup_term in enumerate(lookup_terms):
                    logger.info(f"📚 Searching OECD metadata catalog for indicator: {lookup_term}")
                    search_results = await self.metadata_search.search_with_sdmx_fallback(
                        provider="OECD",
                        indicator=lookup_term,
                    )

                    if not search_results:
                        logger.warning(
                            f"⚠️ No SDMX metadata found for '{lookup_term}'. "
                            f"Trying next lookup alias."
                        )
                        continue

                    logger.info(
                        f"✅ Found {len(search_results)} matching OECD dataflows for '{lookup_term}'"
                    )

                    # Use LLM to intelligently select the best match
                    logger.info(f"🤖 Using LLM to select best matching dataflow for '{lookup_term}'")
                    discovery = await self.metadata_search.discover_indicator(
                        provider="OECD",
                        indicator_name=lookup_term,
                        search_results=search_results,
                    )

                    # Check if discovery returned ambiguity flag (multiple diverse options)
                    if discovery and discovery.get("ambiguous"):
                        options = discovery.get("options", [])
                        options_text = "\n".join([
                            f"  • {opt['name']}" for opt in options[:5]
                        ])
                        ambiguity_error = DataNotAvailableError(
                            f"Your query '{lookup_term}' matches multiple datasets. Please be more specific:\n{options_text}\n\n"
                            f"Try specifying the exact metric you need."
                        )
                        if idx < len(lookup_terms) - 1:
                            continue
                        raise ambiguity_error

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
                            for cache_key in cache_keys:
                                cache_service.set(cache_key, result, ttl=86400)  # Cache 24h
                            logger.info(
                                "✅ Resolved OECD indicator '%s' via lookup term '%s' → %s",
                                indicator,
                                lookup_term,
                                result,
                            )
                            return result

                        logger.warning(
                            f"⚠️ LLM confidence too low for '{lookup_term}' "
                            f"(confidence: {confidence} < 0.6). Trying next lookup alias."
                        )
                    else:
                        logger.warning(
                            f"⚠️ LLM could not select a dataflow for '{lookup_term}'. "
                            f"Trying next lookup alias."
                        )

                if ambiguity_error:
                    raise ambiguity_error

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

            # Use first human-readable lookup term for local lexical scoring.
            # Metadata search already iterates aliases; catalog scoring works best
            # with one focused phrase.
            catalog_lookup_term = next(
                (
                    term for term in lookup_terms
                    if not re.fullmatch(r"[A-Z][A-Z0-9_.-]{2,24}", term.upper())
                ),
                lookup_terms[0],
            )

            candidates = []
            indicator_lower = catalog_lookup_term.lower()
            indicator_words = set(indicator_lower.replace("_", " ").split())
            is_gdp_query = any(
                token in indicator_lower
                for token in (
                    "gdp",
                    "gross domestic product",
                    "national accounts",
                    "economic output",
                )
            )
            is_labor_query = any(
                token in indicator_lower
                for token in (
                    "unemployment",
                    "employment",
                    "labor",
                    "labour",
                    "participation",
                    "hours worked",
                )
            )
            is_price_query = any(
                token in indicator_lower
                for token in (
                    "inflation",
                    "price",
                    "cpi",
                    "ppi",
                    "hicp",
                    "deflator",
                )
            )
            is_producer_price_query = any(
                token in indicator_lower
                for token in (
                    "producer price",
                    "ppi",
                    "wholesale price",
                )
            )

            for flow_id, flow_info in catalog.items():
                name = flow_info.get("name", "").lower()
                desc = flow_info.get("description", "").lower()
                structure = flow_info.get("structure", "")
                flow_id_lower = str(flow_id).lower()
                combined_text = f"{flow_id_lower} {name} {desc}"

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

                # Main statistical aggregates (GDP) - only boost for GDP-like intent.
                if (
                    "NAMAIN" in structure
                    and is_gdp_query
                    and "tax" not in indicator_lower
                    and "revenue" not in indicator_lower
                    and not is_working_hours_query
                ):
                    priority_bonus += 1000  # National Accounts Main Aggregates
                elif is_gdp_query and ("QNA" in flow_id or "QNA" in name):
                    priority_bonus += 800  # Quarterly National Accounts
                elif is_labor_query and ("LFS" in structure or "IALFS" in flow_id):
                    priority_bonus += 800  # Labour Force Survey (for unemployment)
                    # Extra boost for "rates" dataflows when user asks for "rate"
                    # Check for "rate" word or "_rt" abbreviation (e.g., UNE_RT = unemployment rate)
                    is_rate_query = "rate" in indicator_lower or "_rt" in indicator_lower or indicator_lower.endswith("rt")
                    if is_rate_query and "rate" in name:
                        priority_bonus += 500  # Prefer rates dataflow for rate queries
                        logger.debug(f"Rate match boost for {flow_id}: +500")

                # High priority: Standard OECD datasets
                elif is_price_query and "OECD" in flow_id and "PRICES" in name:
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

                if is_producer_price_query:
                    has_producer_signal = any(
                        token in combined_text
                        for token in ("producer", "ppi", "wholesale")
                    )
                    if has_producer_signal:
                        priority_bonus += 900
                    else:
                        # Prevent generic national-accounts drift for producer-price intent.
                        priority_bonus -= 1400
                        if "NAMAIN" in structure:
                            priority_bonus -= 600

                if score > 0:
                    candidates.append((score + priority_bonus, flow_id, flow_info, structure))

            if candidates:
                # Sort by score (descending) and select best match
                candidates.sort(key=lambda x: x[0], reverse=True)
                best_score, flow_id, flow_info, structure = candidates[0]

                result = self._build_result_from_discovery(flow_id, {})
                agency, dataflow, version = result
                for cache_key in cache_keys:
                    cache_service.set(cache_key, result, ttl=86400)  # Cache 24h
                logger.info(
                    f"✅ Found OECD indicator '{indicator}' in local catalog → {dataflow} "
                    f"(priority-adjusted score: {best_score}, structure: {structure}, agency: {agency}, version: {version})"
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

    def _build_indicator_lookup_terms(self, indicator: str) -> List[str]:
        """
        Build ordered lookup aliases for OECD indicator discovery.

        This improves code-like inputs (e.g., IRLT) by expanding them into
        human-readable concept labels before metadata search.
        """
        raw = str(indicator or "").strip()
        if not raw:
            return []

        terms: List[str] = [raw]
        compact = raw.replace("_", " ").strip()
        if compact and compact.lower() != raw.lower():
            terms.append(compact)

        upper = raw.upper()
        code_like = bool(re.fullmatch(r"[A-Z][A-Z0-9_.-]{2,24}", upper))
        is_dataflow_code = "@" in upper or upper.startswith("DSD_") or upper.startswith("DF_")
        is_short_code_alias = False

        semantic_terms: List[str] = []
        short_code_aliases = {
            "PPI": "producer price index",
            "IRLT": "long-term interest rates",
            "REER": "real effective exchange rate",
            "CPI": "consumer price index",
            "HICP": "harmonised index of consumer prices",
            "B6BLTT": "current account balance",
        }
        if code_like and not is_dataflow_code and " " not in raw:
            is_short_code_alias = upper in short_code_aliases
            try:
                from ..services.catalog_service import (
                    find_concepts_by_code,
                    get_all_synonyms,
                    get_provider_info,
                )

                concepts = find_concepts_by_code("OECD", upper)
                for concept in concepts:
                    provider_info = get_provider_info(concept, "OECD") or {}
                    primary = provider_info.get("primary", {})
                    if isinstance(primary, dict):
                        primary_name = str(primary.get("name") or "").strip()
                        if primary_name:
                            semantic_terms.append(primary_name)

                    # Add a few synonyms as backup search terms.
                    for synonym in get_all_synonyms(concept)[:3]:
                        synonym_text = str(synonym or "").strip()
                        if synonym_text:
                            semantic_terms.append(synonym_text)
            except Exception as exc:
                logger.debug("OECD indicator alias expansion skipped for '%s': %s", indicator, exc)

            # Generic short-code aliases to reduce ambiguous metadata matches.
            alias = short_code_aliases.get(upper)
            if alias:
                semantic_terms.append(alias)

        if semantic_terms:
            if is_short_code_alias:
                # Short aliases (PPI/CPI/REER/...) are often globally ambiguous.
                # Prefer semantic expansions first, while still keeping raw code as
                # a backup lookup term.
                terms = semantic_terms + terms
            else:
                terms = semantic_terms + terms

        deduped: List[str] = []
        seen: set[str] = set()
        for term in terms:
            normalized = str(term or "").strip()
            key = normalized.lower()
            if not normalized or key in seen:
                continue
            seen.add(key)
            deduped.append(normalized)
        return deduped[:6]

    def _build_result_from_discovery(self, dataflow_code: str, discovery: dict) -> tuple[str, str, str]:
        """Build the final result tuple from LLM discovery output.

        Args:
            dataflow_code: The selected dataflow code (e.g., "DSD_NAMAIN1@DF_QNA")
            discovery: LLM discovery result with code, name, description, confidence, optional agency

        Returns:
            Tuple of (agency, dataflow, version)
        """
        registry_agency, registry_version = self._lookup_dataflow_registry_metadata(dataflow_code)
        discovery_agency = str(discovery.get("agency") or "").strip()
        agency = registry_agency or discovery_agency

        if registry_agency:
            logger.info("Using agency from OECD registry metadata: %s", agency)
        elif discovery_agency:
            logger.info(f"Using agency from discovery: {agency}")
        else:
            # Extract agency from structure
            structure = dataflow_code.split("@")[0] if "@" in dataflow_code else dataflow_code
            agency = self._extract_agency_from_structure(structure, dataflow_code)
            logger.info(f"Extracted agency from structure: {agency}")

        # Keep full format (DSD_XXX@DF_XXX) for later extraction in fetch_indicator
        dataflow = self._canonical_dataflow_code(dataflow_code)
        version = str(discovery.get("version") or registry_version or "1.0")

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
            return "OECD.EDU.IMEP"

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

        # Public governance indicators (Government at a Glance, public finance dashboards)
        if "DSD_GOV" in structure_upper or "GOV_" in dataflow_upper:
            return "OECD.GOV.GIP"

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
        if is_provider_circuit_open("OECD"):
            raise DataNotAvailableError(
                "OECD is temporarily unavailable due to rate limiting. Please try again later."
            )

        # Resolve indicator to (agency, dataflow, version) tuple using metadata search if needed
        agency, dataflow, version = await self._resolve_indicator(indicator)
        country_code = self._country_code(country)

        # Build time parameters with intelligent defaults
        from datetime import datetime
        current_year = datetime.now().year

        params = {"dimensionAtObservation": "AllDimensions"}
        used_default_time_range = (not start_year and not end_year) or (
            start_year == current_year - 5 and end_year == current_year
        )

        # Default to last 5 years if no time range specified
        if used_default_time_range:
            params["startPeriod"] = str(current_year - 5)
            params["endPeriod"] = str(current_year)
        else:
            if start_year:
                params["startPeriod"] = str(start_year)
            if end_year:
                params["endPeriod"] = str(end_year)

        # Determine expected frequency based on indicator type and dataflow.
        # This is used both for optional key defaults and later metadata labels.
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

        # Build SDMX filter key using dynamic DSD lookup (general solution)
        # Extract DSD ID from dataflow string (format: DSD_XXX@DF_YYY)
        dsd_id = dataflow.split("@")[0] if "@" in dataflow else dataflow
        data_base_url = self.base_url
        structure_metadata = await self._get_oecd_dataflow_structure(agency, dataflow, version)
        filter_key = None
        if structure_metadata and structure_metadata.get("dimensions"):
            if used_default_time_range:
                self._clamp_default_time_params_to_oecd_constraints(params, structure_metadata)
            data_base_url = str(structure_metadata.get("base_url") or self.base_url).rstrip("/")
            defaults = {"frequency": expected_freq} if expected_freq else None
            filter_key = self._build_oecd_key_from_structure(
                structure_metadata,
                country_code,
                custom_defaults=defaults,
            )
            if filter_key and "REF_AREA" not in set(structure_metadata.get("dimension_ids") or []):
                logger.info(
                    "OECD dataflow %s has no REF_AREA dimension; using structure-derived key %s without country filter",
                    dataflow,
                    filter_key,
                )

        # Build proper dimension key to avoid downloading ALL data (causes rate limiting)
        # Prefer OECD's structure/dataflow metadata; fall back to the legacy DSD
        # key builder only when that metadata is unavailable.
        if not filter_key:
            key_builder = get_dimension_key_builder()
            filter_key = await key_builder.build_key(
                provider="OECD",
                agency=agency,
                dsd_id=dsd_id,
                version=version,
                base_url=data_base_url,
                user_params={"country": country_code},
                custom_defaults={"frequency": expected_freq} if expected_freq else None,
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

        # Determine expected measure/transformation for specific indicators
        expected_measure = None
        expected_transform = None

        if "GROWTH" in indicator_upper:
            expected_transform = "GRW"  # Growth rate
        elif "RATE" in indicator_upper or indicator_upper in ["UNEMPLOYMENT", "INFLATION"]:
            expected_measure = "PC"  # Percentage

        # Construct URL
        # OECD SDMX API requires the FULL dataflow ID including DSD_XXX@DF_XXX format
        url = f"{data_base_url}/data/{agency},{dataflow},{version}/{filter_key}"

        # STEP 1: Wait for rate limiter before making request
        # This prevents hitting rate limits in the first place by enforcing delays
        try:
            wait_delay = await wait_for_provider("OECD", max_wait_seconds=5.0)
        except ProviderRateLimitWaitExceeded as exc:
            record_provider_rate_limit_error("OECD")
            raise DataNotAvailableError(
                "OECD is temporarily rate-limited right now. Please try again later or use a different provider."
            ) from exc
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
        time_dim_index = None
        time_dim = None
        for array_idx, dim in enumerate(dimensions):
            if dim.get("id") == "TIME_PERIOD":
                time_dim_index = array_idx
                time_dim = dim
                break

        time_values = time_dim.get("values", []) if time_dim else []
        observation_attributes = (
            (structure.get("attributes") or {}).get("observation", [])
            if isinstance(structure.get("attributes"), dict)
            else []
        )
        ref_period_attr_index = None
        ref_period_values = []
        for attr_idx, attr in enumerate(observation_attributes):
            if isinstance(attr, dict) and attr.get("id") == "REF_PERIOD":
                ref_period_attr_index = attr_idx
                ref_period_values = attr.get("values", []) or []
                break

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
                if country_dim_index >= len(indices) or indices[country_dim_index] != country_value_index:
                    skip_observation = True

            # Filter by frequency if specified
            if freq_dim_index is not None and freq_value_indices:
                if freq_dim_index >= len(indices) or indices[freq_dim_index] not in freq_value_indices:
                    skip_observation = True

            # Filter by measure if specified
            if measure_dim_index is not None and measure_value_indices:
                if measure_dim_index >= len(indices) or indices[measure_dim_index] not in measure_value_indices:
                    skip_observation = True

            # Filter by transformation if specified
            if transform_dim_index is not None and transform_value_indices:
                if transform_dim_index >= len(indices) or indices[transform_dim_index] not in transform_value_indices:
                    skip_observation = True

            if skip_observation:
                observations_filtered_out += 1
                continue

            time_period = None
            if (
                time_dim_index is not None
                and time_dim_index < len(indices)
                and indices[time_dim_index] is not None
                and indices[time_dim_index] < len(time_values)
            ):
                time_info = time_values[indices[time_dim_index]]
                if time_info:
                    time_period = time_info.get("id") or time_info.get("value")
            elif (
                ref_period_attr_index is not None
                and isinstance(obs_value, list)
                and len(obs_value) > 1 + ref_period_attr_index
            ):
                ref_period_index = obs_value[1 + ref_period_attr_index]
                if (
                    ref_period_index is not None
                    and isinstance(ref_period_index, int)
                    and ref_period_index < len(ref_period_values)
                ):
                    ref_period_info = ref_period_values[ref_period_index]
                    if isinstance(ref_period_info, dict):
                        time_period = (
                            ref_period_info.get("id")
                            or ref_period_info.get("value")
                            or ref_period_info.get("name")
                        )

            if not time_period:
                continue

            # obs_value is an array where first element is the value
            value = obs_value[0] if isinstance(obs_value, list) and obs_value else obs_value

            if value is not None:
                # Convert time period to ISO date
                # OECD returns formats like "2020", "2020-Q1", "2020-01"
                time_period = str(time_period)
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
            country=self._country_label(country_code),
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
        requested_aggregate = False
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
                    requested_aggregate = True
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

        # Step 2: Try OECD aggregate only when no explicit country list was provided
        # or when user explicitly requested OECD aggregate. For explicit country
        # comparisons, fetch individual countries directly.
        should_try_aggregate = (
            requested_aggregate
            or (target_countries is None and not countries)
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
        elif len(target_countries) > 8:
            raise DataNotAvailableError(
                "OECD multi-country comparisons over more than 8 countries are temporarily unavailable due to API rate limits. "
                "Try a smaller country set or choose a different provider."
            )
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
