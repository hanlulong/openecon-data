from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from ._sdmx import period_to_iso_date as _period_to_iso_date
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

    # Lazy-loaded dataflow metadata (id -> {"name": ..., "description": ...})
    _DATAFLOW_METADATA_CACHE: Optional[Dict[str, Dict[str, str]]] = None
    _COUNTRY_DISPLAY_NAME_CACHE: Optional[Dict[str, str]] = None

    @property
    def provider_name(self) -> str:
        return "BIS"

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        settings = get_settings()
        self.base_url = settings.bis_base_url.rstrip("/")
        self.metadata_search = metadata_search_service

    @classmethod
    def _load_dataflow_metadata(cls) -> Dict[str, Dict[str, str]]:
        """Load BIS SDMX dataflow labels/descriptions from local metadata cache."""
        if cls._DATAFLOW_METADATA_CACHE is not None:
            return cls._DATAFLOW_METADATA_CACHE

        metadata: Dict[str, Dict[str, str]] = {}
        try:
            metadata_path = (
                Path(__file__).resolve().parents[1]
                / "data"
                / "metadata"
                / "sdmx"
                / "bis_dataflows.json"
            )
            if metadata_path.exists():
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                for code, payload in raw.items():
                    if not isinstance(payload, dict):
                        continue
                    metadata[str(code).upper()] = {
                        "name": str(payload.get("name") or "").strip(),
                        "description": str(payload.get("description") or "").strip(),
                    }
        except Exception as exc:
            logger.debug("BIS: Failed to load local dataflow metadata: %s", exc)

        cls._DATAFLOW_METADATA_CACHE = metadata
        return metadata

    def _lookup_dataflow_info(self, indicator_code: str) -> tuple[Optional[str], Optional[str]]:
        """Return (name, description) for a BIS dataflow code if known."""
        metadata = self._load_dataflow_metadata()
        info = metadata.get(str(indicator_code or "").upper(), {})
        name = info.get("name") or None
        description = info.get("description") or None
        return name, description

    @staticmethod
    def _is_exact_dataflow_request(indicator: str) -> bool:
        """Return true when the caller supplied a provider-native BIS dataflow code."""
        normalized = str(indicator or "").strip().upper()
        return (
            normalized.startswith("WS_")
            or normalized.startswith("BIS_WS_")
            or normalized.startswith("BIS.WS_")
        )

    @staticmethod
    def _country_dimension_ids(series_dimensions: list) -> set[str]:
        """Identify provider-native dimensions that carry country/area values."""
        country_ids: set[str] = set()
        for dim in series_dimensions or []:
            dim_id = str(dim.get("id") or "").upper()
            dim_name = str(dim.get("name") or "").lower()
            if dim_id in {"REF_AREA", "REP_CTY", "BORROWERS_CTY", "ISSUER_RES", "RESIDENCY", "COUNTRY"}:
                country_ids.add(dim_id)
            elif "country" in dim_name or "area" in dim_name:
                country_ids.add(dim_id)
        return country_ids

    @staticmethod
    def _aggregate_dimension_score(selected_dimensions: Dict[str, Dict[str, str]]) -> int:
        """Score mechanically broad/not-applicable BIS dimension values."""
        score = 0
        for dim in selected_dimensions.values():
            value_id = str(dim.get("id") or "").upper()
            value_name = str(dim.get("name") or "").lower()
            if value_id in {"A", "ALL", "T", "TOT", "_T", "Z", "5J"}:
                score += 3
            if any(marker in value_name for marker in ["all", "total", "not applicable", "aggregate"]):
                score += 2
        return score

    def _select_fallback_series(
        self,
        series_data: dict,
        series_dimensions: list,
        *,
        requested_country_code: Optional[str],
    ) -> tuple[Optional[str], dict, Dict[str, Dict[str, str]]]:
        """Select a BIS series from a frequency-level exact-dataflow response.

        Selection is purely provider-native and mechanical: if the dataflow has
        country dimensions and a country was requested, the selected key must
        carry that exact country code.  Broad/all/not-applicable non-country
        dimensions are preferred, then longer observation history, then stable
        series key order.
        """
        if not series_data:
            return None, {}, {}

        requested_country_code = str(requested_country_code or "").upper() or None
        country_dimension_ids = self._country_dimension_ids(series_dimensions)
        best_key: Optional[str] = None
        best_observations: dict = {}
        best_selected: Dict[str, Dict[str, str]] = {}
        best_score: tuple[int, int, str] | None = None

        for series_key in sorted(series_data):
            series_obj = series_data.get(series_key) or {}
            observations = series_obj.get("observations", {}) or {}
            if not observations:
                continue

            selected = self._selected_series_dimension_values(series_key, series_dimensions)
            if requested_country_code and country_dimension_ids:
                selected_country_values = {
                    str(selected.get(dim_id, {}).get("id") or "").upper()
                    for dim_id in country_dimension_ids
                }
                if requested_country_code not in selected_country_values:
                    continue

            aggregate_score = self._aggregate_dimension_score(
                {
                    dim_id: dim
                    for dim_id, dim in selected.items()
                    if dim_id.upper() not in country_dimension_ids
                }
            )
            score = (aggregate_score, len(observations), str(series_key))
            if best_score is None or score > best_score:
                best_score = score
                best_key = str(series_key)
                best_observations = observations
                best_selected = selected

        return best_key, best_observations, best_selected

    def _frequency_candidates(self, preferred_frequency: str) -> list[str]:
        preferred = str(preferred_frequency or "").upper()[:1] or "M"
        candidates = [preferred, "A", "Q", "M"]
        return list(dict.fromkeys(candidate for candidate in candidates if candidate))

    async def _fetch_exact_dataflow_fallback(
        self,
        *,
        indicator_code: str,
        indicator_label: Optional[str],
        country_code_raw: Optional[str],
        start_year: Optional[int],
        end_year: Optional[int],
        preferred_frequency: str,
    ) -> Optional[NormalizedData]:
        """Fetch exact provider-native BIS dataflows with non-standard key shapes.

        This fallback is intentionally limited to exact ``WS_*`` dataflow codes.
        It does not resolve natural language to codes; it only changes how an
        already-known provider-native dataflow is queried.
        """
        indicator_code = str(indicator_code or "").strip().upper()
        if not indicator_code.startswith("WS_"):
            return None

        requested_country_code = self._country_code(country_code_raw) if country_code_raw else None
        client = get_http_client()
        date_params: dict[str, str] = {}
        if start_year:
            date_params["startPeriod"] = str(start_year)
        if end_year:
            date_params["endPeriod"] = str(end_year)

        for frequency in self._frequency_candidates(preferred_frequency):
            url = f"{self.base_url}/data/{indicator_code}/{frequency}"
            payload = None
            try:
                # raise_on_status=False: BIS branches on the status code and
                # retries WITHOUT date params on a non-200/error payload, so the
                # helper must return the response to inspect (retry/rate-limit/
                # breaker still applied), not raise on 4xx.
                response = await self._get_with_retry(
                    client,
                    url,
                    params=date_params,
                    headers={"Accept": "application/vnd.sdmx.data+json;version=1.0.0"},
                    timeout=30.0,
                    raise_on_status=False,
                )
                if response.status_code == 200 and response.content:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                if payload is None or "errors" in payload:
                    response = await self._get_with_retry(
                        client,
                        url,
                        headers={"Accept": "application/vnd.sdmx.data+json;version=1.0.0"},
                        timeout=30.0,
                        raise_on_status=False,
                    )
                    if response.status_code != 200 or not response.content:
                        continue
                    try:
                        payload = response.json()
                    except Exception:
                        continue
            except Exception:
                continue

            data = (payload or {}).get("data") or {}
            datasets = data.get("dataSets") or []
            if not datasets or "series" not in datasets[0]:
                continue

            structure = data.get("structure") or {}
            dimensions = structure.get("dimensions") or {}
            observation_dimensions = dimensions.get("observation", []) or []
            time_dimension = next(
                (dim for dim in observation_dimensions if dim.get("id") == "TIME_PERIOD"),
                None,
            )
            if not time_dimension:
                continue
            time_values = time_dimension.get("values") or []
            series_dimensions = dimensions.get("series", []) or []
            series_key, observations, selected_dimensions = self._select_fallback_series(
                datasets[0].get("series") or {},
                series_dimensions,
                requested_country_code=requested_country_code,
            )
            if not series_key or not observations:
                continue

            data_points = []
            for time_idx_str in sorted(observations, key=lambda item: int(item) if str(item).isdigit() else -1):
                obs_data = observations.get(time_idx_str)
                try:
                    time_idx = int(time_idx_str)
                except (ValueError, TypeError):
                    continue
                if time_idx < 0 or time_idx >= len(time_values):
                    continue
                time_period = str((time_values[time_idx] or {}).get("id") or "")
                if not time_period:
                    continue
                value = None
                if obs_data:
                    try:
                        value_str = obs_data[0]
                        if value_str is not None and value_str != "":
                            value = float(value_str)
                    except (ValueError, TypeError, IndexError):
                        value = None
                date_str = _period_to_iso_date(time_period)
                year_int = int(str(time_period).split("-")[0])
                if start_year and year_int < start_year:
                    continue
                if end_year and year_int > end_year:
                    continue
                data_points.append({"date": date_str, "value": value})

            if not data_points:
                continue

            freq_label = {"M": "monthly", "Q": "quarterly", "A": "annual"}.get(frequency, frequency)
            dataflow_name, dataflow_description = self._lookup_dataflow_info(indicator_code)
            selected_country = None
            for dim_id in self._country_dimension_ids(series_dimensions):
                value_id = str(selected_dimensions.get(dim_id, {}).get("id") or "").upper()
                if requested_country_code and value_id == requested_country_code:
                    selected_country = value_id
                    break
            display_country = (
                self._display_country_name(selected_country)
                if selected_country
                else ("Global" if not requested_country_code else self._display_country_name(requested_country_code))
            )
            selected_dimension_text = "; ".join(
                f"{dim_id}={dim.get('id') or ''} ({dim.get('name') or ''})"
                for dim_id, dim in selected_dimensions.items()
            )
            metadata = Metadata(
                source="BIS",
                indicator=indicator_label or dataflow_name or indicator_code,
                country=display_country,
                frequency=freq_label,
                unit="",
                lastUpdated="",
                seriesId=f"{indicator_code}/{frequency}/{series_key}",
                apiUrl=url,
                sourceUrl=f"https://data.bis.org/topics/{indicator_code}",
                seasonalAdjustment=None,
                dataType=None,
                priceType=None,
                description=(
                    dataflow_description
                    or f"Exact BIS dataflow {indicator_code}; selected dimensions: {selected_dimension_text}"
                ),
                notes=[f"Selected BIS SDMX key {series_key}: {selected_dimension_text}"],
                startDate=data_points[0]["date"],
                endDate=data_points[-1]["date"],
            )
            return NormalizedData(metadata=metadata, data=data_points)

        return None

    @classmethod
    def _country_display_names(cls) -> Dict[str, str]:
        """Build human-readable country labels for BIS metadata."""
        if cls._COUNTRY_DISPLAY_NAME_CACHE is not None:
            return cls._COUNTRY_DISPLAY_NAME_CACHE

        display_names: Dict[str, str] = {
            "XM": "Euro Area",
        }
        display_scores: Dict[str, tuple[int, int]] = {"XM": (10, len("Euro Area"))}

        for raw_name, code in cls.COUNTRY_MAPPINGS.items():
            normalized_code = str(code or "").upper()
            candidate = str(raw_name or "").replace("_", " ").strip()
            if len(candidate) <= 2:
                continue

            score = (
                2 if " " in candidate else 1,
                len(candidate),
            )
            if score >= display_scores.get(normalized_code, (0, 0)):
                display_names[normalized_code] = candidate.title()
                display_scores[normalized_code] = score

        cls._COUNTRY_DISPLAY_NAME_CACHE = display_names
        return display_names

    @classmethod
    def _display_country_name(cls, country_code: str) -> str:
        """Return a human-readable country name for BIS metadata."""
        normalized_code = str(country_code or "").upper()
        return cls._country_display_names().get(normalized_code, normalized_code)

    @staticmethod
    def _selected_series_dimension_values(
        series_key: Optional[str],
        series_dimensions: list,
    ) -> Dict[str, Dict[str, str]]:
        """Decode selected series dimension values from a BIS series key."""
        if not series_key or not series_dimensions:
            return {}

        try:
            key_parts = [int(part) for part in str(series_key).split(":")]
        except (TypeError, ValueError):
            return {}

        selected: Dict[str, Dict[str, str]] = {}
        for idx, dim in enumerate(series_dimensions):
            dim_id = str(dim.get("id") or "")
            values = dim.get("values", []) or []
            if not dim_id or idx >= len(key_parts):
                continue

            value_idx = key_parts[idx]
            if value_idx < 0 or value_idx >= len(values):
                continue

            value_obj = values[value_idx] if isinstance(values[value_idx], dict) else {}
            selected[dim_id] = {
                "id": str(value_obj.get("id") or ""),
                "name": str(value_obj.get("name") or value_obj.get("id") or "").strip(),
            }
        return selected

    def _build_indicator_display_name(
        self,
        indicator_code: str,
        indicator_label: Optional[str],
        series_key: Optional[str],
        series_dimensions: list,
    ) -> str:
        """
        Build a user-readable indicator name for BIS series.

        Falls back to local SDMX metadata labels when only opaque dataflow codes are available.
        """
        dataflow_name, _ = self._lookup_dataflow_info(indicator_code)

        base_name = str(indicator_label or "").strip()
        if not base_name or base_name.upper() == str(indicator_code or "").upper():
            base_name = dataflow_name or indicator_code

        selected = self._selected_series_dimension_values(series_key, series_dimensions)
        context_parts: List[str] = []

        # Add high-value context for key BIS datasets.
        if indicator_code == "WS_TC":
            borrower = selected.get("TC_BORROWERS", {}).get("name")
            unit_type = selected.get("UNIT_TYPE", {}).get("name")
            if borrower and borrower.lower() not in base_name.lower():
                context_parts.append(borrower)
            if unit_type and unit_type.lower() not in base_name.lower():
                context_parts.append(unit_type)
        elif indicator_code == "WS_DSR":
            borrower = selected.get("DSR_BORROWERS", {}).get("name")
            if borrower and borrower.lower() not in base_name.lower():
                context_parts.append(borrower)
        elif indicator_code in {"WS_SPP", "WS_CPP", "WS_DPP"}:
            value_type = selected.get("VALUE", {}).get("name")
            if value_type and value_type.lower() not in base_name.lower():
                context_parts.append(value_type)

        if context_parts:
            return f"{base_name} ({'; '.join(context_parts[:2])})"
        return base_name

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

        exact_provider_dataflow_request = self._is_exact_dataflow_request(indicator)
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
        if indicator_code in ["WS_CBPOL", "WS_LONG_CPI", "WS_XRU", "WS_EER"]:
            frequency = "M"  # Force monthly for these indicators
            logger.info(f"BIS: Forced monthly frequency for {indicator_code}")
        elif indicator_code in ["WS_TC", "WS_SPP", "WS_CPP", "WS_DPP", "WS_DSR", "WS_GLI", "WS_CREDIT_GAP", "WS_DEBT_SEC2_PUB"]:
            frequency = "Q"  # Force quarterly for these indicators
            logger.info(f"BIS: Forced quarterly frequency for {indicator_code}")

        # Special handling for indicators that don't use country codes in standard way
        # WS_GLI (Global Liquidity Indicators) uses complex multi-dimensional structure
        if indicator_code == "WS_GLI":
            # GLI doesn't filter by single country, get all data
            return await self._fetch_gli_data(start_year, end_year)

        # Fetch all countries in parallel using asyncio.gather with a semaphore
        # to avoid overwhelming the BIS API while being much faster than sequential.
        # Use shared HTTP client pool for better performance
        client = get_http_client()

        # Concurrency limiter: allow up to 5 parallel requests to BIS
        semaphore = asyncio.Semaphore(5)

        async def _fetch_single_country(country_code_raw: str) -> Optional[NormalizedData]:
            """Fetch data for a single country, returning None on failure."""
            async with semaphore:
                country_code = self._country_code(country_code_raw)

                # Build SDMX query key based on indicator type
                date_params: dict = {}
                if start_year:
                    date_params["startPeriod"] = str(start_year)
                if end_year:
                    date_params["endPeriod"] = str(end_year)

                # For Eurozone countries requesting monetary indicators,
                # try Euro area (XM) if the country-specific data is outdated or unavailable
                country_codes_to_try = [country_code]
                if country_code in self.EUROZONE_COUNTRIES and indicator_code in ["WS_CBPOL", "WS_LONG_CPI"]:
                    country_codes_to_try.append("XM")

                for current_country_code in country_codes_to_try:
                    # WS_EER (Effective Exchange Rates) uses a different key structure:
                    # FREQ.EER_TYPE.EER_BASKET.REF_AREA where EER_TYPE=R (real) or N (nominal)
                    if indicator_code == "WS_EER":
                        sdmx_key = f"{frequency}.R.B.{current_country_code}"
                    else:
                        sdmx_key = f"{frequency}.{current_country_code}"
                    url = f"{self.base_url}/data/{indicator_code}/{sdmx_key}"

                    try:
                        # First attempt: with date parameters.
                        # raise_on_status=False: BIS inspects the status and
                        # retries WITHOUT date params when a dataflow rejects
                        # them, so the helper must return the response (retry/
                        # rate-limit/breaker still applied), not raise on 4xx.
                        response = await self._get_with_retry(client, url, params=date_params, headers={
                            "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
                        }, timeout=20.0, raise_on_status=False)

                        payload = None
                        if response.status_code == 200 and response.content:
                            try:
                                payload = response.json()
                            except Exception:
                                payload = None

                        # Some BIS dataflows don't support startPeriod/endPeriod parameters
                        if payload is None or "errors" in payload or response.status_code != 200:
                            response = await self._get_with_retry(client, url, headers={
                                "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
                            }, timeout=20.0, raise_on_status=False)
                            if response.status_code != 200 or not response.content:
                                logger.debug(f"BIS: No data for {current_country_code} (status: {response.status_code})")
                                continue
                            try:
                                payload = response.json()
                            except Exception as json_err:
                                logger.debug(f"BIS: JSON parse error for {current_country_code}: {json_err}")
                                continue

                        if "data" not in payload or "dataSets" not in payload["data"]:
                            continue

                        datasets = payload["data"]["dataSets"]
                        if not datasets or "series" not in datasets[0]:
                            continue

                        structure = payload["data"]["structure"]
                        dimensions = structure["dimensions"]["observation"]
                        time_dimension = next((d for d in dimensions if d["id"] == "TIME_PERIOD"), None)

                        if not time_dimension:
                            continue

                        time_values = time_dimension["values"]
                        series_data = datasets[0]["series"]
                        if not series_data:
                            continue

                        series_dimensions = structure["dimensions"].get("series", [])
                        best_series_key, observations = self._select_best_series(
                            series_data, series_dimensions, indicator_code
                        )

                        if not observations:
                            continue

                        data_points = []
                        for time_idx_str, obs_data in observations.items():
                            try:
                                time_idx = int(time_idx_str)
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid time index '{time_idx_str}' in BIS response, skipping")
                                continue
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

                                date_str = _period_to_iso_date(time_period)
                                year_int = int(str(time_period).split("-")[0])

                                if start_year and year_int < start_year:
                                    continue
                                if end_year and year_int > end_year:
                                    continue

                                data_points.append({
                                    "date": date_str,
                                    "value": value
                                })

                        if not data_points:
                            continue

                        freq_label = {"M": "monthly", "Q": "quarterly", "A": "annual"}.get(frequency, frequency)

                        if indicator_code == "WS_CBPOL":
                            unit = "percent"
                        elif indicator_code in ["WS_LONG_CPI", "WS_CPP"]:
                            unit = "index"
                        elif indicator_code in ["WS_XRU", "WS_EER"]:
                            unit = "index"
                        elif indicator_code == "WS_CREDIT_GAP":
                            unit = "percentage points"
                        elif indicator_code == "WS_TC":
                            unit = "percent of GDP"
                        elif indicator_code == "WS_SPP":
                            unit = "index"
                        else:
                            unit = ""

                        api_url = f"{self.base_url}/data/{indicator_code}/{sdmx_key}"
                        if date_params:
                            param_str = "&".join(f"{k}={v}" for k, v in date_params.items())
                            api_url += f"?{param_str}"

                        indicator_name = self._build_indicator_display_name(
                            indicator_code=indicator_code,
                            indicator_label=indicator_label,
                            series_key=best_series_key,
                            series_dimensions=series_dimensions,
                        )
                        _, dataflow_description = self._lookup_dataflow_info(indicator_code)

                        display_country = self._display_country_name(current_country_code)

                        topic_map = {
                            "WS_CBPOL": "CBPOL",
                            "WS_TC": "TOTAL_CREDIT",
                            "WS_SPP": "RPP",
                            "WS_XRU": "EER",
                            "WS_EER": "EER",
                            "WS_LONG_CPI": "CPI",
                            "WS_GLI": "GLI",
                            "WS_CREDIT_GAP": "TOTAL_CREDIT",
                            "WS_DSR": "DSR",
                            "WS_DEBT_SEC2_PUB": "SEC_PUB",
                        }
                        topic = topic_map.get(indicator_code, indicator_code)
                        source_url = f"https://data.bis.org/topics/{topic}"

                        if indicator_code == "WS_TC":
                            if "percent" in unit.lower() or "gdp" in unit.lower():
                                seasonal_adjustment = None
                            else:
                                seasonal_adjustment = "Seasonally Adjusted"
                        else:
                            seasonal_adjustment = None

                        if indicator_code == "WS_CBPOL":
                            data_type = "Rate"
                        elif indicator_code in ["WS_LONG_CPI", "WS_CPP", "WS_XRU", "WS_EER", "WS_SPP"]:
                            data_type = "Index"
                        elif indicator_code == "WS_CREDIT_GAP":
                            data_type = "Gap"
                        elif indicator_code in ["WS_TC", "WS_DSR", "WS_GLI", "WS_DEBT_SEC2_PUB"]:
                            data_type = "Level"
                        else:
                            data_type = None

                        price_type = "Real (inflation-adjusted)" if indicator_code in ["WS_SPP", "WS_CPP"] and "real" in indicator_name.lower() else None

                        start_date_val = data_points[0]["date"] if data_points else None
                        end_date_val = data_points[-1]["date"] if data_points else None

                        metadata = Metadata(
                            source="BIS",
                            indicator=indicator_name,
                            country=display_country,
                            frequency=freq_label,
                            unit=unit,
                            lastUpdated="",
                            seriesId=indicator_code,
                            apiUrl=api_url,
                            sourceUrl=source_url,
                            seasonalAdjustment=seasonal_adjustment,
                            dataType=data_type,
                            priceType=price_type,
                            description=dataflow_description or indicator_name,
                            notes=None,
                            startDate=start_date_val,
                            endDate=end_date_val,
                        )

                        return NormalizedData(metadata=metadata, data=data_points)

                    except Exception:
                        continue

                return None  # No data found for this country

        # Launch all country fetches in parallel
        logger.info(f"🌍 BIS: Fetching {len(country_list)} countries in parallel for {indicator_code}")
        country_results = await asyncio.gather(
            *[_fetch_single_country(c) for c in country_list],
            return_exceptions=True,
        )

        for cr in country_results:
            if isinstance(cr, NormalizedData):
                results.append(cr)
            elif isinstance(cr, Exception):
                logger.debug(f"BIS: Country fetch failed: {cr}")

        if not results and exact_provider_dataflow_request and indicator_code.startswith("WS_"):
            fallback_results = await asyncio.gather(
                *[
                    self._fetch_exact_dataflow_fallback(
                        indicator_code=indicator_code,
                        indicator_label=indicator_label,
                        country_code_raw=c,
                        start_year=start_year,
                        end_year=end_year,
                        preferred_frequency=frequency,
                    )
                    for c in country_list
                ],
                return_exceptions=True,
            )
            for fr in fallback_results:
                if isinstance(fr, NormalizedData):
                    results.append(fr)
                elif isinstance(fr, Exception):
                    logger.debug("BIS: Exact dataflow fallback failed: %s", fr)

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
            # raise_on_status=False: BIS inspects the status and retries WITHOUT
            # date params on a non-200 (some dataflows reject startPeriod/
            # endPeriod). The helper returns the response to branch on (retry/
            # rate-limit/breaker still applied); the explicit raise_for_status()
            # below preserves the terminal failure path exactly as before.
            response = await self._get_with_retry(client, url, params=params, headers={
                "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
            }, timeout=30.0, raise_on_status=False)

            # Retry without date params if error
            if response.status_code != 200:
                response = await self._get_with_retry(client, url, headers={
                    "Accept": "application/vnd.sdmx.data+json;version=1.0.0"
                }, timeout=30.0, raise_on_status=False)

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

                    # Convert time period (shared SDMX parser, Phase 3.2)
                    date_str = _period_to_iso_date(time_period)
                    year_int = int(str(time_period).split("-")[0])

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

        except DataNotAvailableError:
            raise
        except Exception as exc:
            # Distinguish a genuine empty result from a broken fetch/parse. The
            # legitimate "no data" branches above already `return results` (empty),
            # so reaching here means a transport/HTTP/JSON/parse failure — which
            # must surface (Phase 3.4 convention) so the orchestrator can fall
            # back to another provider instead of treating it as "no data".
            # If we already parsed some series, a later failure shouldn't discard
            # them; return the partial set. Only a total failure raises.
            logger.warning("BIS GLI fetch/parse failed: %s", exc)
            if not results:
                raise DataNotAvailableError(
                    f"BIS Global Liquidity Indicators request failed: {exc}"
                ) from exc

        return results

    async def _resolve_indicator_code(self, indicator: str) -> tuple[str, Optional[str]]:
        """Resolve BIS indicator code through mechanical codes or metadata search."""
        # Step 1: Allow users/upstream selectors to supply raw BIS dataflow
        # codes directly. This is mechanical provider-native passthrough.
        indicator_text = str(indicator or "").strip()
        indicator_upper = indicator_text.upper()
        if indicator_upper.startswith("BIS_WS_"):
            indicator_upper = indicator_upper.removeprefix("BIS_")
        elif indicator_upper.startswith("BIS.WS_"):
            indicator_upper = indicator_upper.split(".", 1)[1]
        if indicator_upper.startswith("WS_"):
            return indicator_upper, None

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
        elif indicator_code == "WS_CREDIT_GAP":
            # Credit-to-GDP gaps: prefer actual-minus-trend gap, not the
            # companion ratio/trend series returned by the same provider flow.
            preferences = {
                "TC_BORROWERS": "P",  # Private non-financial sector
                "TC_LENDERS": "A",    # All sectors
                "CG_DTYPE": "C",      # Credit-to-GDP gaps (actual-trend)
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
