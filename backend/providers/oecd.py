from __future__ import annotations

import asyncio
import logging
import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from pathlib import Path

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError, retry_async
from ._sdmx import period_to_iso_date as _period_to_iso_date
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
        if not re.fullmatch(r"DSD_[A-Za-z0-9_]+@[A-Za-z0-9_]+", code):
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

    @classmethod
    def _lookup_dataflow_registry_external_bases(cls, dataflow_code: str) -> List[str]:
        """Return provider-native REST bases advertised by the local OECD registry."""
        code = cls._canonical_dataflow_code(dataflow_code)
        if not re.fullmatch(r"DSD_[A-Za-z0-9_]+@[A-Za-z0-9_]+", code):
            return []

        try:
            from ..services.indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
            row = lookup.get("OECD", code)
        except Exception as exc:
            logger.debug("OECD registry external-base lookup skipped for %s: %s", code, exc)
            row = None

        if not row or str(row.get("code") or "").strip().upper() != code.upper():
            return []

        raw_metadata = row.get("raw_metadata")
        metadata: dict = {}
        if isinstance(raw_metadata, str) and raw_metadata.strip():
            try:
                parsed = json.loads(raw_metadata)
                if isinstance(parsed, dict):
                    metadata = parsed
            except json.JSONDecodeError:
                return []
        elif isinstance(raw_metadata, dict):
            metadata = raw_metadata

        bases: List[str] = []
        links = metadata.get("links")
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                rest_base = cls._oecd_rest_base_from_link(str(link.get("href") or ""))
                if rest_base and rest_base not in bases:
                    bases.append(rest_base)
        return bases

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

    @staticmethod
    def _parse_oecd_default_annotations(annotations: Any) -> Dict[str, str]:
        """Parse OECD provider-native DEFAULT annotations into dimension defaults."""
        if not isinstance(annotations, list):
            return {}

        defaults: Dict[str, str] = {}
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            if str(annotation.get("type") or "").upper() != "DEFAULT":
                continue
            title = str(annotation.get("title") or "").strip()
            if not title:
                continue
            for part in title.split(","):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                dim_id = key.strip()
                dim_value = value.strip()
                if not dim_id or not dim_value:
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_]+", dim_id):
                    continue
                defaults.setdefault(dim_id, dim_value)
        return defaults

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
        default_values = cls._parse_oecd_default_annotations(dataflow_info.get("annotations"))

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
            "default_values": default_values,
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
        for registry_base in self._lookup_dataflow_registry_external_bases(dataflow):
            if registry_base not in external_bases:
                external_bases.append(registry_base)
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
        country_code: Optional[str],
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
                if country_code:
                    key_parts[position] = str(country_code)
                    filled = True
                elif dim_id in defaults and defaults[dim_id]:
                    key_parts[position] = str(defaults[dim_id])
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
    def _oecd_ref_area_code_for_structure(
        structure_metadata: Optional[Dict[str, Any]],
        country_code: Optional[str],
    ) -> Optional[str]:
        """Return the provider-native REF_AREA code for a specific OECD dataflow.

        Most OECD dataflows use ISO alpha-3 REF_AREA values, but some migrated
        SDMX surfaces still advertise ISO alpha-2 values in their content
        constraints (for example national-accounts IDC tables use ``DE`` rather
        than ``DEU``).  Use the dataflow's provider-native constraint metadata
        to choose the exact request code instead of assuming one global country
        code convention.
        """
        requested = str(country_code or "").strip().upper()
        if not requested or not structure_metadata:
            return country_code

        valid_ref_areas = {
            str(value or "").strip().upper()
            for value in (structure_metadata.get("valid_values_by_dimension") or {}).get("REF_AREA") or []
            if str(value or "").strip()
        }
        if not valid_ref_areas or requested in valid_ref_areas:
            return requested

        try:
            from ..routing.country_resolver import CountryResolver

            iso2 = CountryResolver.to_iso2(requested)
            if iso2 and iso2.upper() in valid_ref_areas:
                return iso2.upper()
        except Exception:
            pass

        return requested

    @staticmethod
    def _build_oecd_relaxed_key_from_structure(
        structure_metadata: Dict[str, Any],
        country_code: Optional[str],
        frequency: Optional[str] = None,
    ) -> Optional[str]:
        """Build a low-specificity provider-native key for availability retry.

        This is used only after OECD's advertised DEFAULT annotations produce
        no records.  It keeps mechanical request constraints (REF_AREA and, when
        known, FREQ) but relaxes optional default dimensions so the response can
        reveal which provider-native combinations actually have observations.
        """
        dimensions = structure_metadata.get("dimensions") or []
        if not dimensions:
            return None

        key_parts = [""] * len(dimensions)
        filled = False
        country_dims = {"REF_AREA", "geo", "COUNTRY"}
        frequency_value = str(frequency or "").strip()

        for array_idx, dim in enumerate(dimensions):
            if not isinstance(dim, dict):
                continue
            dim_id = str(dim.get("id") or "")
            position = dim.get("position", array_idx)
            if not isinstance(position, int) or position < 0 or position >= len(key_parts):
                position = array_idx
            if dim_id in country_dims and country_code:
                key_parts[position] = str(country_code)
                filled = True
            elif dim_id == "FREQ" and frequency_value:
                key_parts[position] = frequency_value
                filled = True

        if not filled:
            return None
        return ".".join(key_parts)

    @staticmethod
    def _oecd_expected_frequency_for_structure(
        structure_metadata: Optional[Dict[str, Any]],
        expected_freq: Optional[str],
    ) -> Optional[str]:
        """Reconcile heuristic cadence hints with provider-native FREQ values.

        Some OECD dataflow IDs contain text fragments such as ``UNEMP`` even
        when the public structure is annual-only.  A heuristic monthly hint must
        not override the dataflow's own content constraints; otherwise the
        request key asks for a non-existent FREQ and returns false no-data.
        """
        candidate = str(expected_freq or "").strip().upper()
        if not structure_metadata:
            return candidate or None
        valid_freqs = {
            str(value or "").strip().upper()
            for value in (structure_metadata.get("valid_values_by_dimension") or {}).get("FREQ") or []
            if str(value or "").strip()
        }
        if not valid_freqs:
            return candidate or None
        if candidate and candidate in valid_freqs:
            return candidate
        if len(valid_freqs) == 1:
            return next(iter(valid_freqs))
        return None

    @staticmethod
    def _oecd_country_constraint_error(
        structure_metadata: Optional[Dict[str, Any]],
        country_code: str,
        dataflow: str,
    ) -> Optional[str]:
        """Return a diagnostic when OECD constraints exclude the requested country."""
        if not structure_metadata:
            return None
        dimension_ids = {
            str(dim_id or "").strip()
            for dim_id in structure_metadata.get("dimension_ids") or []
            if str(dim_id or "").strip()
        }
        if "REF_AREA" not in dimension_ids:
            return None

        valid_ref_areas = {
            str(value or "").strip().upper()
            for value in (structure_metadata.get("valid_values_by_dimension") or {}).get("REF_AREA") or []
            if str(value or "").strip()
        }
        if not valid_ref_areas:
            return None

        requested = str(country_code or "").strip().upper()
        if not requested or requested in valid_ref_areas:
            return None

        compatible_prefixes = set()
        try:
            from ..routing.country_resolver import CountryResolver

            iso2 = CountryResolver.to_iso2(requested)
            if iso2:
                compatible_prefixes.add(iso2.upper())
        except Exception:
            compatible_prefixes = set()
        if requested == "GBR":
            # OECD Education subnational regional area codes use UK* rather than GB*.
            compatible_prefixes.add("UK")

        if compatible_prefixes and any(
            value.startswith(prefix)
            for value in valid_ref_areas
            for prefix in compatible_prefixes
        ):
            return None

        sample = ", ".join(sorted(valid_ref_areas)[:12])
        return (
            f"OECD dataflow {dataflow} does not advertise REF_AREA={requested} "
            f"in its content constraints. Available REF_AREA sample: {sample}."
        )

    @staticmethod
    def _year_from_oecd_period(period: Any) -> Optional[int]:
        match = re.search(r"(\d{4})", str(period or ""))
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _period_code_from_oecd_period(period: Any, frequency: Optional[str] = None) -> Optional[str]:
        """Return an OECD API period code while preserving provider cadence.

        OECD structure constraints often publish full ISO timestamps even for
        monthly or quarterly data.  The data endpoint expects SDMX period codes
        such as ``2026-02`` or ``2025-Q4``; collapsing everything to the year
        can create a false empty response for high-frequency dataflows.
        """
        text = str(period or "").strip()
        if not text:
            return None
        if re.search(r"\d{4}-Q[1-4]", text, flags=re.IGNORECASE):
            match = re.search(r"(\d{4})-Q([1-4])", text, flags=re.IGNORECASE)
            return f"{match.group(1)}-Q{match.group(2)}" if match else None
        match = re.search(r"(\d{4})(?:-(\d{2}))?", text)
        if not match:
            return None
        year = match.group(1)
        month_text = match.group(2)
        freq = str(frequency or "").strip().upper()
        if freq == "M" and month_text:
            return f"{year}-{month_text}"
        if freq == "Q" and month_text:
            try:
                quarter = ((int(month_text) - 1) // 3) + 1
            except ValueError:
                quarter = 1
            quarter = min(max(quarter, 1), 4)
            return f"{year}-Q{quarter}"
        return year

    @classmethod
    def _period_range_from_structure(
        cls,
        structure_metadata: Dict[str, Any],
        frequency: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """Return provider-native start/end period codes from time constraints."""
        for entry in structure_metadata.get("time_ranges") or []:
            if not isinstance(entry, dict):
                continue
            time_range = entry.get("timeRange")
            if not isinstance(time_range, dict):
                continue
            start_info = time_range.get("startPeriod", {})
            end_info = time_range.get("endPeriod", {})
            start_value = start_info.get("period") if isinstance(start_info, dict) else start_info
            end_value = end_info.get("period") if isinstance(end_info, dict) else end_info
            start_period = cls._period_code_from_oecd_period(start_value, frequency)
            end_period = cls._period_code_from_oecd_period(end_value, frequency)
            if start_period or end_period:
                return start_period, end_period
        return None, None

    @classmethod
    def _provider_advertised_time_params_from_structure(
        cls,
        structure_metadata: Optional[Dict[str, Any]],
        base_params: Dict[str, str],
    ) -> Optional[Dict[str, str]]:
        """Return OECD provider-advertised default/valid time span params.

        This is a mechanical fallback for provider-generated default windows:
        when a latest-period request is empty for a sparse dataflow/country,
        retrying the provider's own advertised default time span preserves the
        same REF_AREA/key constraints without guessing a query-specific range.
        """
        if not structure_metadata:
            return None

        defaults = dict(structure_metadata.get("default_values") or {})
        period_frequency = cls._time_param_frequency_from_structure(structure_metadata)
        start_period = cls._period_code_from_oecd_period(
            defaults.get("TIME_PERIOD_START"),
            period_frequency,
        )
        end_period = cls._period_code_from_oecd_period(
            defaults.get("TIME_PERIOD_END"),
            period_frequency,
        )
        if not start_period and not end_period:
            start_period, end_period = cls._period_range_from_structure(
                structure_metadata,
                period_frequency,
            )
        if not start_period and not end_period:
            return None

        params = dict(base_params or {})
        if start_period:
            params["startPeriod"] = str(start_period)
        elif end_period:
            params["startPeriod"] = str(end_period)
        if end_period:
            params["endPeriod"] = str(end_period)
        elif start_period:
            params["endPeriod"] = str(start_period)
        return params

    @staticmethod
    def _time_param_frequency_from_structure(structure_metadata: Dict[str, Any]) -> Optional[str]:
        defaults = dict(structure_metadata.get("default_values") or {})
        candidate = str(defaults.get("FREQ") or defaults.get("frequency") or "").strip().upper()
        if candidate:
            return candidate
        valid_freqs = {
            str(value or "").strip().upper()
            for value in (structure_metadata.get("valid_values_by_dimension") or {}).get("FREQ") or []
            if str(value or "").strip()
        }
        if len(valid_freqs) == 1:
            return next(iter(valid_freqs))
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
        period_frequency = cls._time_param_frequency_from_structure(structure_metadata)
        valid_start_period, valid_end_period = cls._period_range_from_structure(
            structure_metadata,
            period_frequency,
        )

        request_start = cls._year_from_oecd_period(params.get("startPeriod"))
        request_end = cls._year_from_oecd_period(params.get("endPeriod"))

        if request_start is None and request_end is None:
            if valid_start is not None:
                params["startPeriod"] = str(valid_start_period or valid_start)
            if valid_end is not None:
                params["endPeriod"] = str(valid_end_period or valid_end)
            return

        no_overlap = (
            (request_end is not None and valid_start is not None and request_end < valid_start)
            or (request_start is not None and valid_end is not None and request_start > valid_end)
        )
        if no_overlap:
            if valid_end is not None:
                end_period = str(valid_end_period or valid_end)
                params["startPeriod"] = end_period
                params["endPeriod"] = end_period
            elif valid_start is not None:
                params["startPeriod"] = str(valid_start_period or valid_start)
            return

        if request_start is not None and valid_start is not None and request_start < valid_start:
            params["startPeriod"] = str(valid_start_period or valid_start)
        if request_end is not None and valid_end is not None and request_end > valid_end:
            params["endPeriod"] = str(valid_end_period or valid_end)

        if (
            str(params.get("startPeriod") or "") == str(params.get("endPeriod") or "")
            and str(params.get("startPeriod") or "").strip()
        ):
            # OECD's API can reject broad high-dimensional downloads even when a
            # dataflow's provider-default selection is narrow.  For generated
            # default windows, prefer the latest advertised observation year
            # over a multi-year pull; explicit user ranges bypass this method.
            return

        default_end = str((structure_metadata.get("default_values") or {}).get("TIME_PERIOD_END") or "").strip()
        default_end_period = cls._period_code_from_oecd_period(default_end, period_frequency)
        if default_end_period is not None:
            params["startPeriod"] = str(default_end_period)
            params["endPeriod"] = str(default_end_period)
            return

        if valid_end is not None:
            end_period = str(valid_end_period or valid_end)
            params["startPeriod"] = end_period
            params["endPeriod"] = end_period

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
        canonical_explicit_dataflow = self._canonical_dataflow_code(raw_indicator).upper()

        exact_parts = [part.strip() for part in raw_indicator.split(",")]
        if len(exact_parts) in (2, 3):
            exact_agency = exact_parts[0]
            exact_dataflow = self._canonical_dataflow_code(exact_parts[1])
            exact_version = exact_parts[2] if len(exact_parts) == 3 else ""
            if (
                re.fullmatch(r"[A-Za-z0-9_.-]+", exact_agency or "")
                and re.fullmatch(r"DSD_[A-Za-z0-9_]+@[A-Za-z0-9_]+", exact_dataflow or "")
                and (not exact_version or re.fullmatch(r"[0-9][A-Za-z0-9_.-]*", exact_version))
            ):
                _, registry_version = self._lookup_dataflow_registry_metadata(exact_dataflow)
                result = (exact_agency, exact_dataflow, exact_version or registry_version or "1.0")
                logger.info("🔒 Treating exact OECD agency/dataflow tuple as resolved: %s", result)
                cache_service.set(f"oecd_indicator:{explicit_dataflow}", result, ttl=86400)
                return result

        explicit_prefix = canonical_explicit_dataflow
        if explicit_prefix.startswith("DSD_") and "@" in explicit_prefix:
            catalog = self._load_dataflows_catalog()
            prefix_matches = [
                flow_id
                for flow_id in catalog
                if flow_id.upper().startswith(explicit_prefix)
            ]
            exact_matches = [
                flow_id
                for flow_id in prefix_matches
                if flow_id.upper() == explicit_prefix
            ]
            if prefix_matches and not exact_matches:
                # Some user-visible OECD catalog fragments are valid prefixes
                # rather than full dataflow IDs. Keep expanding those through
                # the provider catalog instead of treating the fragment as a
                # final exact ID.
                flow_id = min(prefix_matches, key=len)
                logger.info(
                    "🔒 Resolved OECD dataflow prefix '%s' -> %s via local catalog",
                    explicit_dataflow,
                    flow_id,
                )
                structure = flow_id.split("@")[0]
                result = (
                    self._extract_agency_from_structure(structure, flow_id),
                    flow_id,
                    "1.0",
                )
                cache_service.set(f"oecd_indicator:{explicit_dataflow}", result, ttl=86400)
                return result

        if re.fullmatch(r"DSD_[A-Z0-9_]+@[A-Z0-9_]+", canonical_explicit_dataflow):
            logger.info("🔒 Treating explicit OECD dataflow as resolved: %s", canonical_explicit_dataflow)
            result = self._build_result_from_discovery(canonical_explicit_dataflow, {})
            cache_service.set(f"oecd_indicator:{explicit_dataflow}", result, ttl=86400)
            return result

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
        Build mechanical lookup variants for OECD indicator discovery.

        This intentionally avoids hardcoded short-code-to-concept expansion:
        natural-language authority must come from metadata search plus LLM
        adjudication, while exact provider-native dataflows are handled before
        this method.
        """
        raw = str(indicator or "").strip()
        if not raw:
            return []

        terms: List[str] = [raw]
        compact = raw.replace("_", " ").strip()
        if compact and compact.lower() != raw.lower():
            terms.append(compact)

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
        if "DSD_EARNINGS" in structure_upper:
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
        country_code = self._country_code(country) if country else None

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
        provider_default_time_params = dict(params) if used_default_time_range else None

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
        structure_defaults_for_selection: Dict[str, str] = {}
        provider_advertised_time_params: Optional[Dict[str, str]] = None
        relaxed_default_retry = False
        if structure_metadata and structure_metadata.get("dimensions"):
            adjusted_expected_freq = self._oecd_expected_frequency_for_structure(
                structure_metadata,
                expected_freq,
            )
            if adjusted_expected_freq != expected_freq:
                logger.info(
                    "OECD adjusted expected frequency for %s from %s to %s using structure metadata",
                    dataflow,
                    expected_freq,
                    adjusted_expected_freq,
                )
                expected_freq = adjusted_expected_freq
            country_code = self._oecd_ref_area_code_for_structure(
                structure_metadata,
                country_code,
            )
            if country_code:
                country_constraint_error = self._oecd_country_constraint_error(
                    structure_metadata,
                    country_code,
                    dataflow,
                )
                if country_constraint_error:
                    raise DataNotAvailableError(country_constraint_error)
            if used_default_time_range:
                self._clamp_default_time_params_to_oecd_constraints(params, structure_metadata)
                provider_advertised_time_params = (
                    self._provider_advertised_time_params_from_structure(
                        structure_metadata,
                        params,
                    )
                )
            data_base_url = str(structure_metadata.get("base_url") or self.base_url).rstrip("/")
            defaults = dict(structure_metadata.get("default_values") or {})
            if expected_freq and "FREQ" not in defaults:
                defaults["frequency"] = expected_freq
            structure_defaults_for_selection = dict(defaults)
            defaults = defaults or None
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
                user_params={"country": country_code} if country_code else {},
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
            filter_key = f".{country_code}.........." if country_code else "all"
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
        primary_filter_key = filter_key
        primary_url = url
        default_time_params_were_clamped = (
            provider_default_time_params is not None
            and params != provider_default_time_params
        )

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

        async def _request_oecd_data(
            request_url: str,
            request_params: Optional[Dict[str, str]] = None,
        ) -> Dict[str, Any]:
            async def fetch_with_retry():
                try:
                    active_params = request_params if request_params is not None else params
                    # Use 50s timeout - OECD SDMX API can be very slow for complex queries
                    # Research shows OECD has 60 requests/hour rate limit, so we need patience
                    response = await http_client.get(
                        request_url,
                        params=active_params,
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

            return await retry_async(
                fetch_with_retry,
                max_attempts=3,  # More attempts for slow OECD API
                initial_delay=3.0,  # Start with 3s delay
                backoff_factor=2.0,  # Exponential backoff
                jitter=2.0,  # Add 0-2s random jitter
            )

        async def _try_provider_advertised_time_window(
            request_url: str,
            request_key: str,
        ) -> Optional[Dict[str, Any]]:
            if not provider_advertised_time_params or provider_advertised_time_params == params:
                return None
            logger.info(
                "OECD generated latest/default window returned no records for %s/%s; "
                "retrying provider-advertised default time span %s-%s with key %s",
                country_code,
                dataflow,
                provider_advertised_time_params.get("startPeriod"),
                provider_advertised_time_params.get("endPeriod"),
                request_key,
            )
            try:
                return await _request_oecd_data(
                    request_url,
                    request_params=provider_advertised_time_params,
                )
            except DataNotAvailableError as exc:
                logger.info(
                    "OECD provider-advertised default time span had no data for %s/%s "
                    "with key %s; continuing existing provider-native fallbacks: %s",
                    country_code,
                    dataflow,
                    request_key,
                    exc,
                )
                return None

        # Use retry_async with exponential backoff and jitter for OECD:
        # - 3 attempts (original + 2 retries)
        # - Exponential backoff: 3s → 6s → 12s
        # - Jitter: 0-2s random added to avoid thundering herd
        # Total worst case: 50s + 5s + 50s + 8s + 50s = ~163s (but rare)
        try:
            data = await _request_oecd_data(url)
        except DataNotAvailableError as primary_exc:
            advertised_data = await _try_provider_advertised_time_window(url, filter_key)
            if advertised_data is not None:
                data = advertised_data
                params = dict(provider_advertised_time_params or params)
            else:
                relaxed_frequency = (
                    structure_defaults_for_selection.get("FREQ")
                    or structure_defaults_for_selection.get("frequency")
                    or expected_freq
                )
                relaxed_key = (
                    self._build_oecd_relaxed_key_from_structure(
                        structure_metadata,
                        country_code,
                        relaxed_frequency,
                    )
                    if structure_metadata
                    else None
                )
                if not relaxed_key or relaxed_key == filter_key:
                    raise primary_exc
                relaxed_url = f"{data_base_url}/data/{agency},{dataflow},{version}/{relaxed_key}"
                logger.info(
                    "OECD default key returned no records for %s/%s; retrying relaxed provider-native key %s",
                    country_code,
                    dataflow,
                    relaxed_key,
                )
                try:
                    data = await _request_oecd_data(relaxed_url)
                except DataNotAvailableError as relaxed_exc:
                    advertised_data = await _try_provider_advertised_time_window(
                        relaxed_url,
                        relaxed_key,
                    )
                    if advertised_data is None:
                        raise relaxed_exc
                    data = advertised_data
                    params = dict(provider_advertised_time_params or params)
                filter_key = relaxed_key
                url = relaxed_url
                relaxed_default_retry = True

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
        if (
            not observations
            and provider_advertised_time_params is not None
            and provider_advertised_time_params != params
        ):
            logger.info(
                "OECD latest-period default window returned no observations for %s/%s; "
                "retrying provider-advertised default time span %s-%s",
                country_code,
                dataflow,
                provider_advertised_time_params.get("startPeriod"),
                provider_advertised_time_params.get("endPeriod"),
            )
            try:
                advertised_window_data = await _request_oecd_data(
                    url,
                    request_params=provider_advertised_time_params,
                )
            except DataNotAvailableError as exc:
                logger.info(
                    "OECD provider-advertised default time span had no observations for %s/%s; "
                    "continuing existing provider-native fallbacks: %s",
                    country_code,
                    dataflow,
                    exc,
                )
            else:
                advertised_datasets = advertised_window_data.get("data", {}).get("dataSets", [])
                advertised_dataset = advertised_datasets[0] if advertised_datasets else None
                advertised_observations = (
                    advertised_dataset.get("observations", {})
                    if isinstance(advertised_dataset, dict)
                    else {}
                )
                if advertised_observations:
                    data = advertised_window_data
                    datasets = advertised_datasets
                    dataset = advertised_dataset
                    observations = advertised_observations
                    params = dict(provider_advertised_time_params)

        if not observations and not relaxed_default_retry:
            relaxed_frequency = (
                structure_defaults_for_selection.get("FREQ")
                or structure_defaults_for_selection.get("frequency")
                or expected_freq
            )
            relaxed_key = (
                self._build_oecd_relaxed_key_from_structure(
                    structure_metadata,
                    country_code,
                    relaxed_frequency,
                )
                if structure_metadata
                else None
            )
            if relaxed_key and relaxed_key != filter_key:
                relaxed_url = f"{data_base_url}/data/{agency},{dataflow},{version}/{relaxed_key}"
                logger.info(
                    "OECD default key returned an empty dataset for %s/%s; "
                    "retrying relaxed provider-native key %s",
                    country_code,
                    dataflow,
                    relaxed_key,
                )
                data = await _request_oecd_data(relaxed_url)
                filter_key = relaxed_key
                url = relaxed_url
                relaxed_default_retry = True
                datasets = data.get("data", {}).get("dataSets", [])
                dataset = datasets[0] if datasets else None
                if dataset is not None:
                    observations = dataset.get("observations", {})

        if (
            not observations
            and provider_default_time_params is not None
            and default_time_params_were_clamped
        ):
            # OECD content constraints can advertise a latest period that has
            # no observations for a specific country/default dimension
            # combination.  If the single-period request is empty, replay the
            # same provider-native key sequence with the original generated
            # default window before declaring no data.  The provider-advertised
            # default/valid span has already been tried above when available.
            # Explicit user dates do not enter this path.
            logger.info(
                "OECD latest-period default window returned no observations for %s/%s; "
                "retrying original generated default window",
                country_code,
                dataflow,
            )
            default_window_data = await _request_oecd_data(
                primary_url,
                request_params=provider_default_time_params,
            )
            default_datasets = default_window_data.get("data", {}).get("dataSets", [])
            default_dataset = default_datasets[0] if default_datasets else None
            default_observations = (
                default_dataset.get("observations", {})
                if isinstance(default_dataset, dict)
                else {}
            )

            if not default_observations and structure_metadata:
                relaxed_frequency = (
                    structure_defaults_for_selection.get("FREQ")
                    or structure_defaults_for_selection.get("frequency")
                    or expected_freq
                )
                default_relaxed_key = self._build_oecd_relaxed_key_from_structure(
                    structure_metadata,
                    country_code,
                    relaxed_frequency,
                )
                if default_relaxed_key and default_relaxed_key != primary_filter_key:
                    default_relaxed_url = (
                        f"{data_base_url}/data/{agency},{dataflow},{version}/{default_relaxed_key}"
                    )
                    logger.info(
                        "OECD generated default window returned an empty dataset for %s/%s; "
                        "retrying relaxed provider-native key %s with the same window",
                        country_code,
                        dataflow,
                        default_relaxed_key,
                    )
                    default_window_data = await _request_oecd_data(
                        default_relaxed_url,
                        request_params=provider_default_time_params,
                    )
                    default_datasets = default_window_data.get("data", {}).get("dataSets", [])
                    default_dataset = default_datasets[0] if default_datasets else None
                    default_observations = (
                        default_dataset.get("observations", {})
                        if isinstance(default_dataset, dict)
                        else {}
                    )
                    if default_observations:
                        filter_key = default_relaxed_key
                        url = default_relaxed_url
                        relaxed_default_retry = True

            if default_observations:
                data = default_window_data
                datasets = default_datasets
                dataset = default_dataset
                observations = default_observations
                params = dict(provider_default_time_params)

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
        obs_status_attr_index = None
        obs_status_values = []
        for attr_idx, attr in enumerate(observation_attributes):
            if not isinstance(attr, dict):
                continue
            attr_id = attr.get("id")
            if attr_id == "REF_PERIOD":
                ref_period_attr_index = attr_idx
                ref_period_values = attr.get("values", []) or []
            elif attr_id == "OBS_STATUS":
                obs_status_attr_index = attr_idx
                obs_status_values = attr.get("values", []) or []

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

        response_country_code = country_code
        if not response_country_code and country_dim_index is not None:
            country_values = dimensions[country_dim_index].get("values", []) or []
            if len(country_values) == 1 and isinstance(country_values[0], dict):
                response_country_code = str(country_values[0].get("id") or "").strip() or None

        # Parse observations with enhanced filtering
        logger.info(f"📈 Total observations in API response: {len(observations)}")
        data_points = []
        observations_checked = 0
        observations_filtered_out = 0
        selected_observations_with_period = 0
        selected_null_value_observations = 0
        null_value_status_counts: Dict[str, int] = defaultdict(int)
        selection_defaults = dict(structure_defaults_for_selection or {})
        if selection_defaults.get("frequency") and "FREQ" not in selection_defaults:
            selection_defaults["FREQ"] = selection_defaults["frequency"]
        ignored_selection_defaults = {
            "LASTNPERIODS",
            "LASTNOBSERVATIONS",
            "TIME_PERIOD_START",
            "TIME_PERIOD_END",
            "frequency",
        }

        def _default_value_matches(dim_id: str, value_id: str) -> bool:
            default_value = str(selection_defaults.get(dim_id) or "").strip()
            if not default_value or dim_id in ignored_selection_defaults:
                return False
            allowed_values = {
                part.strip().upper()
                for part in default_value.split("+")
                if part.strip()
            }
            return bool(allowed_values) and value_id.upper() in allowed_values

        def _ref_area_from_series_key(series_key: Any) -> Optional[str]:
            if not isinstance(series_key, (list, tuple)):
                return None
            for dim_id, value_id in series_key:
                if str(dim_id or "") == "REF_AREA" and str(value_id or "").strip():
                    return str(value_id).strip()
            return None

        def _observation_status_label(observation_value: Any) -> Optional[str]:
            if (
                obs_status_attr_index is None
                or not isinstance(observation_value, list)
                or len(observation_value) <= 1 + obs_status_attr_index
            ):
                return None
            status_index = observation_value[1 + obs_status_attr_index]
            if status_index is None:
                return None
            status_info: Any = None
            if isinstance(status_index, int) and 0 <= status_index < len(obs_status_values):
                status_info = obs_status_values[status_index]
            elif isinstance(status_index, str):
                status_info = {"id": status_index}
            if isinstance(status_info, dict):
                status_id = str(status_info.get("id") or status_info.get("value") or "").strip()
                status_name = str(status_info.get("name") or "").strip()
                if status_id and status_name and status_name != status_id:
                    return f"{status_id} ({status_name})"
                return status_id or status_name or None
            if status_info is not None:
                status_label = str(status_info).strip()
                return status_label or None
            return None

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
            selected_observations_with_period += 1

            if value is None:
                selected_null_value_observations += 1
                status_label = _observation_status_label(obs_value)
                if status_label:
                    null_value_status_counts[status_label] += 1
                continue

            series_key_parts = []
            default_match_score = 0
            for dim_idx, dim in enumerate(dimensions):
                if dim_idx == time_dim_index:
                    continue
                dim_id = str(dim.get("id") or "")
                value_id = ""
                if dim_idx < len(indices) and indices[dim_idx] is not None:
                    dim_values = dim.get("values", []) or []
                    if indices[dim_idx] < len(dim_values):
                        dim_value = dim_values[indices[dim_idx]]
                        if isinstance(dim_value, dict):
                            value_id = str(dim_value.get("id") or dim_value.get("value") or "")
                if dim_id:
                    series_key_parts.append((dim_id, value_id))
                    if value_id and _default_value_matches(dim_id, value_id):
                        default_match_score += 1

            # Convert time period to ISO date via the shared SDMX parser
            # (Phase 3.2). OECD returns "2020", "2020-Q1", "2020-01". This
            # fixes a prior divergence where quarters were anchored to the
            # END of the quarter's start-month (Q1 -> "2020-03-01", two
            # months late) instead of the start-of-quarter convention every
            # other SDMX provider uses (Q1 -> "2020-01-01").
            time_period = str(time_period)
            date_str = _period_to_iso_date(time_period)

            data_points.append(
                {
                    "date": date_str,
                    "value": float(value),
                    "_series_key": tuple(series_key_parts),
                    "_default_match_score": default_match_score,
                }
            )

        logger.info(f"📊 Filtering results:")
        logger.info(f"   Observations checked: {observations_checked}")
        logger.info(f"   Observations filtered out: {observations_filtered_out}")
        logger.info(f"   Selected observations with period: {selected_observations_with_period}")
        logger.info(f"   Selected null-valued observations: {selected_null_value_observations}")
        logger.info(f"   Data points extracted: {len(data_points)}")

        if not data_points:
            if (
                selected_observations_with_period > 0
                and selected_null_value_observations == selected_observations_with_period
            ):
                reported_country = country_code or response_country_code or "requested selection"
                status_summary = ""
                if null_value_status_counts:
                    ranked_statuses = sorted(
                        null_value_status_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                    status_summary = "; OBS_STATUS=" + ", ".join(
                        f"{label} ({count})" for label, count in ranked_statuses[:5]
                    )
                raise DataNotAvailableError(
                    "oecd_missing_valued_observations: "
                    f"OECD returned {selected_observations_with_period} observations for "
                    f"{reported_country} {dataflow}, but all observation values were null/missing"
                    f"{status_summary}. The provider response contains no collected numeric values "
                    "for the requested country and time window."
                )

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

        if data_points and selection_defaults:
            points_by_series: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
            for point in data_points:
                points_by_series[point.get("_series_key")].append(point)
            if len(points_by_series) > 1:
                scored_series = []
                for series_key, points in points_by_series.items():
                    score = max(
                        int(point.get("_default_match_score") or 0)
                        for point in points
                    )
                    scored_series.append(
                        (
                            score,
                            len(points),
                            str(series_key),
                            series_key,
                        )
                    )
                best_score, best_count, _best_sort_key, best_series_key = max(scored_series)
                if best_score > 0:
                    response_country_code = response_country_code or _ref_area_from_series_key(best_series_key)
                    logger.info(
                        "OECD selected provider-default-conforming series after %srequest: "
                        "score=%s observations=%s series=%s",
                        "relaxed " if relaxed_default_retry else "",
                        best_score,
                        best_count,
                        best_series_key,
                    )
                    data_points = points_by_series[best_series_key]

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

        for point in data_points:
            point.pop("_series_key", None)
            point.pop("_default_match_score", None)

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
            country=(
                self._country_label(response_country_code)
                if response_country_code
                else "OECD provider default"
            ),
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
