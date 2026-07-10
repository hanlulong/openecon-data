from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, Optional, TYPE_CHECKING, Any

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client, effective_timeout
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from .base import BaseProvider

if TYPE_CHECKING:
    from ..services.metadata_search import MetadataSearchService


logger = logging.getLogger(__name__)


def _eurostat_dataset_unavailable_message(
    dataset_code: str,
    country_code: Optional[str],
    response_text: str = "",
) -> str:
    """Return a fail-closed supportability reason for non-disseminated datasets."""

    evidence = re.sub(r"\s+", " ", str(response_text or "")).strip()
    if len(evidence) > 300:
        evidence = evidence[:300].rstrip() + "..."
    suffix = f"; provider_evidence={evidence}" if evidence else ""
    return (
        "fail-closed supportability block: "
        "reason=eurostat_dataset_not_disseminated; "
        f"dataset={str(dataset_code or '').strip().lower()}; "
        f"country={country_code or 'ALL_AVAILABLE'}"
        f"{suffix}"
    )


# Genuine growth/change-RATE intent. A bare "change" substring also matches
# "exCHANGE rate" and "CHANGES in inventories" (real LEVEL series), so requiring
# word-boundaried, qualified phrasing prevents silently replacing their published
# values with a year-over-year % transform.
_EUROSTAT_RATE_INTENT_RE = re.compile(
    r"\b(?:growth|yoy|year[-\s]over[-\s]year)\b"
    r"|(?:\b(?:percent|percentage)|%|\brate\s+of)\s+changes?\b"
    r"|\bgrowth\s+rate\b",
    re.IGNORECASE,
)


class EurostatProvider(BaseProvider):
    """Eurostat Statistics API provider for EU economic data using SDMX 3.0 endpoints."""

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
        "GREECE": "EL",  # Eurostat uses "EL" for Greece (not ISO standard "GR")
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
        "prc_hicp_aind": {"coicop": "CP00", "unit": "RCH_A_AVG"},  # HICP inflation - HEADLINE (all items, annual avg rate of change %)
        "prc_hicp_manr": {"coicop": "CP00"},  # HICP monthly - headline
        "prc_hicp_midx": {"coicop": "CP00"},  # HICP index - headline
        # PPP price-level datasets expose many analytical categories and forcing a
        # GDP-only na_item filter can zero out otherwise valid series (for
        # example the direct-cert PPP price-level query for Germany).
        "prc_ppp_ind": {},  # Price level indices
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

        # === Regional and city statistics ===
        # TGS00007 is a NUTS-2 regional table; country aggregates such as FR/ES
        # are not valid geo members for the selected slice.
        "tgs00007": {"unit": "PC", "sex": "T", "age": "Y15-64"},
        # TGS00107 is likewise NUTS-2 only; Eurostat's default presentation uses
        # percentage of population, and country-level geo codes have no rows.
        "tgs00107": {"unit": "PC_POP"},
        # City rents require a dwelling type and currency; fetch_indicator()
        # picks a representative capital-city geo when callers pass a country.
        "prc_colc_rents": {"building": "FLAT2", "currency": "EUR"},

        # === Interest Rates (INFRASTRUCTURE FIX) ===
        # EI_MFIR_M: Interest rates - monthly data
        # indic options: MF-DDI-RT (day-to-day), MF-3MI-RT (3-month), MF-LTGBY-RT (long-term govt bond yield)
        "EI_MFIR_M": {"indic": "MF-LTGBY-RT"},  # Default: Long-term government bond yields (10-year equivalent)
    }

    NUTS2_REPRESENTATIVE_GEO_BY_COUNTRY: Dict[str, str] = {
        "DE": "DE30",  # Berlin
        "ES": "ES30",  # Comunidad de Madrid
        "FR": "FR10",  # Ile-de-France
        "IT": "ITI4",  # Lazio
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
        country: Optional[str] = "EU",
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> NormalizedData:
        """Fetch economic indicator data from Eurostat JSON-stat API (statistics/1.0).

        Note: The SDMX 2.1 API has compatibility issues (406 errors).
        We use the JSON-stat API which is more reliable.

        If start_year and end_year are not specified, defaults to last 5 years of data.
        """
        dataset_code, dataset_label = await self._resolve_dataset(indicator)
        raw_country = str(country or "").strip()
        no_geo_filter = raw_country.upper() in {"__ALL__", "ALL_AVAILABLE", "ALL"}
        country_code = None if no_geo_filter else self._country_code(raw_country or "EU")
        if country_code:
            country_code = self._default_geo_for_dataset(dataset_code, country_code)

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
        query_params: Dict[str, str] = {"freq": freq}
        if country_code:
            query_params["geo"] = country_code

        # Add time range (JSON-stat uses sinceTimePeriod, not startPeriod)
        # Default to last 5 years if not specified
        current_year = datetime.now(timezone.utc).year
        used_default_time_range = start_year is None and end_year is None
        query_params["sinceTimePeriod"] = str(start_year or (current_year - 5))
        # Best-effort upper bound. The SDMX dissemination endpoint doesn't
        # reliably honor either bound for all datasets, so the returned points
        # are ALSO trimmed to [start_year, end_year] in code after parsing.
        if end_year is not None:
            query_params["untilTimePeriod"] = str(end_year)

        # Add mechanical defaults for an already-selected provider-native dataset.
        static_defaults = self.DATASET_DEFAULT_FILTERS.get(dataset_code, {})
        for key, value in static_defaults.items():
            query_params[key] = value
        # Members we asked Eurostat to filter each dimension to. When the API
        # honors a filter the dimension collapses to one member; when it IGNORES
        # one (unknown code) the response stays multi-valued and the parser must
        # decide which member to slice. requested_members lets the parser honor
        # the intended member instead of blind index 0. Caller `filters` are
        # STRICT (fail closed if the member is unavailable — never silently pick
        # something else over an explicit request); the dataset's mechanical
        # static defaults are honored-if-present but fall back to the
        # TOTAL/aggregate member rather than erroring.
        _excluded_dims = {"country", "countries", "geo", "freq", "time", "time_period", "indicator"}
        requested_members: Dict[str, str] = {}
        strict_member_dims: set[str] = set()
        for key, value in static_defaults.items():
            dim_key = str(key or "").strip().lower()
            if not dim_key or dim_key in _excluded_dims or dim_key.startswith("__"):
                continue
            if value is None or value == "":
                continue
            requested_members[dim_key] = str(value)
        for key, value in (filters or {}).items():
            dim_key = str(key or "").strip().lower()
            if not dim_key or dim_key in _excluded_dims or dim_key.startswith("__"):
                continue
            if value is None or value == "":
                continue
            query_params[dim_key] = str(value)
            requested_members[dim_key] = str(value)
            strict_member_dims.add(dim_key)

        def latest_default_time_params() -> Dict[str, str]:
            bounded_params = dict(query_params)
            # Some exact Eurostat datasets are quarterly/monthly or mixed-frequency.
            # If no geography/time was requested, use Eurostat's provider-native
            # latest-period filter and avoid imposing our inferred annual freq.
            if no_geo_filter:
                bounded_params.pop("freq", None)
            bounded_params.pop("sinceTimePeriod", None)
            bounded_params["lastTimePeriod"] = "1"
            return bounded_params

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        effective_query_params = dict(query_params)

        async def fetch_payload(params: Dict[str, str]) -> Dict[str, Any]:
            # Route through the shared base helper so the primary Eurostat path
            # gets rate-limit gating, 429/Retry-After handling, transient 5xx
            # retry, and the per-provider circuit breaker — the fallback/probe
            # paths used to be the only beneficiaries.
            #
            # raise_on_status=False makes the helper RETURN the non-2xx response
            # (for 4xx like 404/413) instead of raising, so we can inspect the
            # status and re-raise the SAME httpx.HTTPStatusError the callers
            # already branch on. This keeps every 404/413 recovery ladder and the
            # _requested_geo_unavailable_supportability_reason callback contract
            # byte-for-byte intact. (5xx is retried then surfaced by the helper
            # as DataNotAvailableError, since a persistent server error is not a
            # recoverable status the ladder can narrow around.)
            response = await self._get_with_retry(
                client,
                data_url,
                params=params,
                raise_on_status=False,
                timeout=effective_timeout(30.0),
            )
            # The helper has already retried/handled 429 and transient 5xx, so a
            # non-2xx here is a terminal client error (404/413/...). Surface it as
            # the same httpx.HTTPStatusError the recovery ladders below branch on
            # — raise_for_status() carries the real response (status_code + text),
            # exactly as the previous direct client.get() path did.
            response.raise_for_status()
            return response.json()

        try:
            payload = await fetch_payload(effective_query_params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in {404, 500} and no_geo_filter and used_default_time_range:
                effective_query_params = latest_default_time_params()
                try:
                    payload = await fetch_payload(effective_query_params)
                except httpx.HTTPStatusError as retry_error:
                    if retry_error.response.status_code not in {404, 500}:
                        raise
                    raise DataNotAvailableError(
                        _eurostat_dataset_unavailable_message(
                            dataset_code,
                            country_code,
                            retry_error.response.text,
                        )
                    ) from retry_error
                except DataNotAvailableError as retry_error:
                    # The narrowed query persistently 5xx'd (the base helper
                    # already exhausted its transient retries). Same conclusion
                    # the old 500 branch reached: the dataset is not disseminated.
                    raise DataNotAvailableError(
                        _eurostat_dataset_unavailable_message(
                            dataset_code,
                            country_code,
                            str(retry_error),
                        )
                    ) from retry_error
            elif e.response.status_code == 413 and no_geo_filter and used_default_time_range:
                effective_query_params = latest_default_time_params()
                try:
                    payload = await fetch_payload(effective_query_params)
                except httpx.HTTPStatusError as retry_error:
                    if retry_error.response.status_code != 413:
                        raise
                    raise DataNotAvailableError(
                        "fail-closed supportability block: "
                        "reason=eurostat_response_too_large; "
                        f"dataset={dataset_code}; "
                        f"country={country_code or 'ALL_AVAILABLE'}"
                    ) from retry_error
            else:
                if e.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"Eurostat dataset '{dataset_code}' not found for country {country_code or 'ALL_AVAILABLE'}"
                    )
                if e.response.status_code == 413:
                    raise DataNotAvailableError(
                        "fail-closed supportability block: "
                        "reason=eurostat_response_too_large; "
                        f"dataset={dataset_code}; "
                        f"country={country_code or 'ALL_AVAILABLE'}"
                    )
                raise

        data_points, frequency, slice_notes = self._parse_dataset(
            payload, dataset_code, requested_members, strict_member_dims
        )
        if not data_points and "freq" in effective_query_params:
            # The freq filter above is inferred from the dataset-code suffix,
            # which is only a naming convention — e.g. prc_hicp_manr is a
            # monthly dataset despite no "_m" suffix, so an imposed freq=A
            # returns an empty cube for EVERY country. Retry once without a
            # frequency filter and accept the dataset's native frequency
            # (dataset-agnostic: the provider tells us what exists, no
            # per-dataset mapping).
            no_freq_params = {
                k: v for k, v in effective_query_params.items() if k != "freq"
            }
            try:
                retry_payload = await fetch_payload(no_freq_params)
                retry_points, retry_frequency, retry_notes = self._parse_dataset(
                    retry_payload, dataset_code, requested_members, strict_member_dims
                )
                if retry_points:
                    logger.info(
                        "Eurostat %s: inferred freq=%s returned no data; "
                        "native-frequency retry succeeded (%s points)",
                        dataset_code,
                        effective_query_params.get("freq"),
                        len(retry_points),
                    )
                    payload = retry_payload
                    data_points = retry_points
                    frequency = retry_frequency
                    slice_notes = retry_notes
                    effective_query_params = no_freq_params
            except (httpx.HTTPStatusError, DataNotAvailableError) as freq_retry_error:
                logger.debug(
                    "Eurostat no-freq retry failed for %s: %s",
                    dataset_code,
                    freq_retry_error,
                )
        if not data_points and used_default_time_range:
            retry_params = latest_default_time_params()
            if retry_params != effective_query_params:
                try:
                    retry_payload = await fetch_payload(retry_params)
                except httpx.HTTPStatusError as retry_error:
                    if retry_error.response.status_code == 413:
                        raise DataNotAvailableError(
                            "fail-closed supportability block: "
                            "reason=eurostat_response_too_large; "
                            f"dataset={dataset_code}; "
                            f"country={country_code or 'ALL_AVAILABLE'}"
                        ) from retry_error
                    raise
                retry_points, retry_frequency, retry_notes = self._parse_dataset(
                    retry_payload, dataset_code, requested_members, strict_member_dims
                )
                if retry_points:
                    payload = retry_payload
                    data_points = retry_points
                    frequency = retry_frequency
                    slice_notes = retry_notes
                    effective_query_params = retry_params

        if not data_points:
            supportability_reason = await self._requested_geo_unavailable_supportability_reason(
                fetch_payload=fetch_payload,
                query_params=query_params,
                dataset_code=dataset_code,
                country_code=country_code,
                no_geo_filter=no_geo_filter,
            )
            if supportability_reason:
                raise DataNotAvailableError(supportability_reason)
            raise DataNotAvailableError(f"No data found for {country_code or 'ALL_AVAILABLE'} in dataset {dataset_code}")

        # Apply year-over-year rate calculation if requested
        # Check if indicator name suggests rate/growth/change calculation is needed
        if self._should_calculate_rate(indicator):
            logger.info(
                f"Calculating year-over-year rate for indicator: {indicator} (frequency={frequency})"
            )
            data_points = self._calculate_year_over_year_change(data_points, frequency)
            # Update unit to reflect percentage change
            unit = "percent"
        else:
            # Extract unit from API response (preferred) or fallback to hardcoded mapping
            unit = self._extract_unit_from_payload(payload, dataset_code)

        # Enforce the requested window in code. The dissemination endpoint does
        # not reliably honor since/untilTimePeriod (both are ignored for several
        # datasets), so "France GDP 2010-2015" otherwise returned 2010→latest.
        # Runs AFTER the YoY calc so its year lag still had the prior periods.
        if start_year is not None or end_year is not None:
            data_points = self._trim_points_to_year_range(data_points, start_year, end_year)

        # NOTE: values are returned exactly as Eurostat publishes them — see
        # the FRED provider note on the removed value-sniffing percent
        # normalizer (it corrupted legitimately small series).

        api_url = self._compose_url(data_url, effective_query_params)

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
        elif re.search(r"\bchanges?\b", indicator_lower) or "growth" in indicator_lower:
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
            country=country_code or "ALL_AVAILABLE",
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
            notes=(slice_notes or None),
            startDate=start_date,
            endDate=end_date,
        )

        return NormalizedData(metadata=metadata, data=data_points)

    def _default_geo_for_dataset(self, dataset_code: str, country_code: str) -> str:
        """Return a dataset-valid default geo for dimensionful regional tables."""
        code = str(dataset_code or "").strip().lower()
        geo = str(country_code or "").strip().upper()
        if code == "tgs00007" and geo in self.NUTS2_REPRESENTATIVE_GEO_BY_COUNTRY:
            return self.NUTS2_REPRESENTATIVE_GEO_BY_COUNTRY[geo]
        if code == "tgs00107" and geo in self.NUTS2_REPRESENTATIVE_GEO_BY_COUNTRY:
            return self.NUTS2_REPRESENTATIVE_GEO_BY_COUNTRY[geo]
        if code == "prc_colc_rents" and re.fullmatch(r"[A-Z]{2}", geo):
            return f"{geo}_CAP"
        return country_code

    async def _requested_geo_unavailable_supportability_reason(
        self,
        *,
        fetch_payload: Callable[[Dict[str, str]], Awaitable[Dict[str, Any]]],
        query_params: Dict[str, str],
        dataset_code: str,
        country_code: Optional[str],
        no_geo_filter: bool,
    ) -> Optional[str]:
        """Classify fail-closed country misses when Eurostat exposes only aggregate geo.

        This is provider-surface supportability, not a semantic shortcut: it only
        runs after the exact provider-native dataset returned no observations for
        the requested geography, and it probes the same Eurostat dataset without a
        geo filter to inspect the public JSON-stat geo dimension.  If the only
        available geographies are aggregates, silently returning that aggregate
        for an individual country request would answer a different user question.
        """
        requested_geo = str(country_code or "").strip().upper()
        if no_geo_filter or not requested_geo:
            return None
        if self._is_aggregate_geo_category(requested_geo, ""):
            return None

        probe_params = dict(query_params)
        probe_params.pop("geo", None)
        probe_params.pop("sinceTimePeriod", None)
        probe_params.pop("time", None)
        probe_params["lastTimePeriod"] = "1"

        try:
            probe_payload = await fetch_payload(probe_params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 413:
                # Existing response-size supportability handling owns this case.
                return None
            raise

        available_geo = self._extract_geo_categories(probe_payload)
        if not available_geo or requested_geo in available_geo:
            return None
        if not all(
            self._is_aggregate_geo_category(code, label)
            for code, label in available_geo.items()
        ):
            return None

        available_codes = ",".join(sorted(available_geo))
        return (
            "fail-closed supportability block: "
            "reason=eurostat_requested_geo_unavailable; "
            f"dataset={dataset_code}; "
            f"country={requested_geo}; "
            f"available_geo={available_codes}"
        )

    @staticmethod
    def _extract_geo_categories(payload: Dict[str, Any]) -> Dict[str, str]:
        """Return provider-native geo category codes and labels from JSON-stat."""
        geo_dimension = (payload.get("dimension") or {}).get("geo") or {}
        category = geo_dimension.get("category") or {}
        labels = category.get("label") or {}
        indexes = category.get("index") or {}

        categories: Dict[str, str] = {}
        if isinstance(labels, dict):
            categories.update({str(code).upper(): str(label or "") for code, label in labels.items()})
        if isinstance(indexes, dict):
            for code in indexes:
                categories.setdefault(str(code).upper(), "")
        elif isinstance(indexes, list):
            for code in indexes:
                categories.setdefault(str(code).upper(), "")
        return categories

    @staticmethod
    def _is_aggregate_geo_category(code: str, label: str) -> bool:
        """Identify Eurostat aggregate geography codes/labels mechanically."""
        normalized_code = str(code or "").strip().upper()
        normalized_label = str(label or "").strip().lower()
        if not normalized_code and not normalized_label:
            return False
        if normalized_code in {"EU", "EU_V", "EU27_2020", "EU28", "EU15", "EA", "EA20", "EA19"}:
            return True
        if re.fullmatch(r"(EU|EA)\d{0,2}(_\d{4})?", normalized_code):
            return True
        if normalized_code.startswith(("EU_", "EA_")):
            return True
        return any(
            token in normalized_label
            for token in ("european union", "euro area", "aggregate")
        )

    async def _resolve_dataset(self, indicator: str) -> tuple[str, Optional[str]]:
        """Resolve Eurostat dataset ID through exact codes or metadata search."""
        # Step 1: Allow users/upstream selectors to supply raw Eurostat dataset/table
        # codes directly.  This is mechanical provider-native passthrough, not
        # natural-language semantic authority.
        normalized_indicator = str(indicator or "").strip()
        if normalized_indicator and " " not in normalized_indicator:
            candidate_code = normalized_indicator.lower()
            if re.fullmatch(r"[a-z0-9_]{4,}", candidate_code):
                looks_like_dataset_id = (
                    "_" in candidate_code
                    or any(ch.isdigit() for ch in normalized_indicator)
                    or normalized_indicator.isupper()
                )
                if looks_like_dataset_id and not candidate_code.endswith("_core"):
                    return candidate_code, self._dataset_labels.get(candidate_code)

        if not self.metadata_search:
            raise DataNotAvailableError(
                f"Eurostat dataset for '{indicator}' not recognized. Provide the dataset code (e.g., nama_10_gdp) or enable metadata discovery."
            )

        # Use hierarchical search: SDMX first, then Eurostat REST API.
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
            code = str(discovery["code"]).strip()
            label = discovery.get("name")
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

    def _parse_dataset(
        self,
        payload: Dict[str, Any],
        dataset_code: str,
        requested_members: Optional[Dict[str, str]] = None,
        strict_member_dims: Optional[set] = None,
    ) -> tuple[list[Dict[str, Any]], str, list]:
        """Parse JSON-stat 2.0 format from Eurostat API.

        The JSON-stat format has a flat structure with:
        - value: dict/array of data values indexed by position
        - dimension: metadata about dimensions including time

        Returns (data_points, frequency, slice_notes) where slice_notes records
        any defaulted/verified dimension member selections for transparency.
        """
        # Check for JSON-stat format (statistics/1.0 API)
        if "value" in payload and "dimension" in payload:
            slice_notes: list = []
            data_points = self._parse_json_stat(
                payload,
                dataset_code,
                requested_members=requested_members,
                strict_member_dims=strict_member_dims,
                notes_sink=slice_notes,
            )
            frequency = self._infer_frequency(payload.get("dimension", {}).get("time", {}), dataset_code)
            return data_points, frequency, slice_notes

        # Fallback: try SDMX-JSON format (data/sdmx API) - legacy support
        # Phase 3.4 error-handling convention: distinguish "API success with
        # zero rows" (return [], "annual") from "malformed payload"
        # (raise DataNotAvailableError). Conflating them defeats the
        # verification harness — silent empties hide parser bugs as data
        # gaps and the accuracy suite can't tell the difference.
        data_section = payload.get("data", {})
        datasets = data_section.get("dataset") or data_section.get("datasets") or []
        if isinstance(datasets, dict):
            datasets = [datasets]
        if not datasets:
            # Empty dataset list is legitimate "no data" from the API.
            return [], "annual", []

        dataset = datasets[0]
        values = dataset.get("value", {})
        if isinstance(values, list):
            values = {str(idx): val for idx, val in enumerate(values)}

        if not values:
            # API returned a dataset envelope but no observations — treat
            # as legitimate "no data" rather than parse failure.
            return [], "annual", []

        dimensions = dataset.get("dimension", {})
        if not dimensions:
            # Values without dimension metadata is structurally invalid
            # SDMX-JSON; bubble up as a parser failure so callers don't
            # silently absorb it as "no data".
            raise DataNotAvailableError(
                f"Eurostat dataset {dataset_code!r} returned values without dimension metadata"
            )
        time_dim = dimensions.get("time") or {}
        category = time_dim.get("category") or {}
        indexes = category.get("index") or {}

        if not indexes:
            # Time dimension is the index axis for any temporal Eurostat
            # series. Missing time index with values present is structurally
            # invalid — raise so the issue surfaces in monitoring.
            raise DataNotAvailableError(
                f"Eurostat dataset {dataset_code!r} returned values but no time-dimension index"
            )

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
        return data_points, frequency, []

    _UNIT_AFFINITY_STOPWORDS = frozenset(
        {"of", "the", "in", "and", "to", "a", "an", "by", "for", "from", "with", "at", "t"}
    )

    @classmethod
    def _label_affinity_tokens(cls, text: str) -> set:
        """Tokenize a metadata label for unit-affinity comparison.

        Purely lexical: lowercases, expands the '%' glyph so it can match
        'percentage', strips numbers/punctuation and stopwords. Used to align
        Eurostat's own dataset label with its own unit labels — no
        domain-specific vocabulary is encoded here.
        """
        text = str(text or "").lower().replace("%", " percentage ")
        return {
            token
            for token in re.findall(r"[a-z]+", text)
            if token not in cls._UNIT_AFFINITY_STOPWORDS
        }

    def _select_unit_choice(
        self, payload: Dict[str, Any], dataset_code: str
    ) -> tuple[int, Optional[str]]:
        """Pick which category of a multi-unit JSON-stat 'unit' dimension is
        the dataset's headline measure. Returns (unit_index, unit_label).

        Many Eurostat datasets publish several units in one cube (e.g.
        tipslm80 carries both 'Percentage point change (t-(t-3))' and
        'Percentage of population in the labour force'). Blindly taking
        index 0 returns derived/change series under a level-style name —
        wrong data with plausible metadata. The dataset label is authored by
        Eurostat to describe the headline series, so when no unit was pinned
        by query params we pick the unit whose label shares the most tokens
        with the dataset label, falling back to the first unit when there is
        no lexical signal at all.
        """
        dimensions = payload.get("dimension", {})
        unit_dim = dimensions.get("unit") or {}
        category = unit_dim.get("category") or {}
        unit_indexes = category.get("index") or {}
        unit_labels = category.get("label") or {}

        if not unit_indexes:
            return 0, None
        if len(unit_indexes) == 1:
            only_code = next(iter(unit_indexes))
            return unit_indexes[only_code], unit_labels.get(only_code)

        # Legacy special case predating the affinity rule; une_rt_a's dataset
        # label carries no unit hint, so the lexical rule cannot decide it.
        if dataset_code == "une_rt_a":
            for code in ("PC_ACT", "PC"):
                if code in unit_indexes:
                    return unit_indexes[code], unit_labels.get(code)

        dataset_tokens = self._label_affinity_tokens(payload.get("label", ""))
        best_code = None
        best_score = 0
        if dataset_tokens:
            for code, _idx in sorted(unit_indexes.items(), key=lambda kv: kv[1]):
                score = len(
                    dataset_tokens & self._label_affinity_tokens(unit_labels.get(code, ""))
                )
                if score > best_score:
                    best_code, best_score = code, score

        if best_code is None:
            # No affinity signal: preserve the long-standing first-unit
            # behavior rather than guessing.
            best_code = min(unit_indexes, key=unit_indexes.get)
        else:
            logger.info(
                "Eurostat %s: multi-unit dataset, selected unit %s (%s) by "
                "dataset-label affinity",
                dataset_code, best_code, unit_labels.get(best_code),
            )
        return unit_indexes[best_code], unit_labels.get(best_code)

    @staticmethod
    def _aggregate_member_score(value_id: str) -> int:
        """Score a JSON-stat category member by how 'aggregate/total' it is.

        Shared by the primary parser's default-member selection and the
        sparse-series fallback so both prefer the same TOTAL/aggregate members
        instead of blind index 0.
        """
        value_upper = str(value_id or "").upper()
        if value_upper in {"TOTAL", "T"} or value_upper.endswith("_TOTAL"):
            return 5
        return 0

    @classmethod
    def _select_default_member_index(cls, idx_to_value: Dict[int, str]) -> int:
        """Pick a dimension's member index when none was requested.

        Prefer the TOTAL/aggregate member; tie-break on the lowest index so the
        choice is deterministic and matches the prior index-0 bias when no
        aggregate member exists.
        """
        best_idx = 0
        best_score: Optional[int] = None
        for idx in sorted(idx_to_value.keys()):
            score = cls._aggregate_member_score(idx_to_value[idx])
            if best_score is None or score > best_score:
                best_score = score
                best_idx = idx
        return best_idx

    def _parse_json_stat(
        self,
        payload: Dict[str, Any],
        dataset_code: str,
        requested_members: Optional[Dict[str, str]] = None,
        strict_member_dims: Optional[set] = None,
        notes_sink: Optional[list] = None,
    ) -> list[Dict[str, Any]]:
        """Parse JSON-stat 2.0 format from Eurostat API with proper unit selection.

        For every non-unit/non-time dimension the response leaves multi-valued,
        select a member deliberately rather than blind index 0:
          * a requested member (caller filter or dataset default) is honored
            when present in the category list; a STRICT (caller-supplied) filter
            that the API ignored and whose member is absent FAILS CLOSED;
          * otherwise prefer the TOTAL/aggregate member and record the actual
            member (code + label) chosen into notes_sink for transparency.
        """
        from ..utils.retry import DataNotAvailableError

        requested_members = {
            str(k).strip().lower(): str(v)
            for k, v in (requested_members or {}).items()
            if v not in (None, "")
        }
        strict_member_dims = {str(d).strip().lower() for d in (strict_member_dims or set())}
        values = payload.get("value", {})
        if isinstance(values, list):
            values = {str(idx): val for idx, val in enumerate(values)}
        dimensions = payload.get("dimension", {})
        time_dim = dimensions.get("time", {})
        indexes = time_dim.get("category", {}).get("index", {})
        ordered = sorted(indexes.items(), key=lambda item: item[1])

        # Get dimension sizes to calculate positions
        sizes = payload.get("size", [])
        id_list = payload.get("id", [])

        unit_index, _unit_label = self._select_unit_choice(payload, dataset_code)

        data_points: list[Dict[str, Any]] = []

        structured = len(sizes) == len(id_list) and "time" in id_list
        if structured:
            time_pos = id_list.index("time")
            unit_pos = id_list.index("unit") if "unit" in id_list else -1

            # Choose a member index for every non-time, non-unit dimension ONCE.
            # (Time varies per observation; unit is chosen by _select_unit_choice.)
            dim_selected_index: Dict[int, int] = {}
            for pos, dim_id in enumerate(id_list):
                if pos == time_pos or pos == unit_pos:
                    continue
                size = sizes[pos] if pos < len(sizes) else 1
                category = dimensions.get(dim_id, {}).get("category", {})
                index_map = category.get("index", {}) or {}
                label_map = category.get("label", {}) or {}
                idx_to_value = {i: value_id for value_id, i in index_map.items()}
                if size <= 1:
                    # Constrained to a single member — nothing to choose/disclose.
                    dim_selected_index[pos] = 0
                    continue

                dim_key = str(dim_id).lower()
                requested = requested_members.get(dim_key)
                if requested:
                    selected_idx = next(
                        (
                            i
                            for i, value_id in idx_to_value.items()
                            if str(value_id).upper() == requested.upper()
                        ),
                        None,
                    )
                    if selected_idx is not None:
                        dim_selected_index[pos] = selected_idx
                        if notes_sink is not None:
                            selected_label = label_map.get(requested)
                            notes_sink.append(
                                f"Eurostat left dimension '{dim_id}' multi-valued "
                                f"despite the requested filter; selected requested "
                                f"member '{requested}'"
                                + (f" ({selected_label})" if selected_label else "")
                                + "."
                            )
                        continue
                    if dim_key in strict_member_dims:
                        # An EXPLICIT caller filter the API ignored, whose member
                        # is not offered — never silently slice something else.
                        available = ", ".join(
                            sorted(str(v) for v in idx_to_value.values())[:12]
                        )
                        raise DataNotAvailableError(
                            f"eurostat_filter_member_unavailable: dataset "
                            f"{dataset_code!r} dimension {dim_id!r} does not offer "
                            f"requested member {requested!r} (available: {available})"
                        )
                    # Non-strict (dataset default) member unavailable — fall back
                    # to the aggregate member below and disclose it.

                selected_idx = self._select_default_member_index(idx_to_value)
                dim_selected_index[pos] = selected_idx
                selected_code = idx_to_value.get(selected_idx, "")
                selected_label = label_map.get(selected_code, "")
                if notes_sink is not None:
                    prefix = (
                        f"requested member '{requested}' unavailable, "
                        if requested
                        else "no member requested; "
                    )
                    notes_sink.append(
                        f"Dimension '{dim_id}' had {size} members ({prefix}"
                        f"defaulted to '{selected_code}'"
                        + (f" ({selected_label})" if selected_label else "")
                        + ")."
                    )

            for label, idx in ordered:
                position = 0
                multiplier = 1
                for i in range(len(id_list) - 1, -1, -1):
                    if i == time_pos:
                        position += idx * multiplier
                    elif i == unit_pos:
                        position += unit_index * multiplier
                    else:
                        position += dim_selected_index.get(i, 0) * multiplier
                    if i > 0:
                        multiplier *= sizes[i]
                value = values.get(str(position))
                if value is None:
                    continue
                data_points.append(
                    {"date": self._normalize_time_label(label), "value": value}
                )
        else:
            for label, idx in ordered:
                value = values.get(str(idx))
                if value is None:
                    continue
                data_points.append(
                    {"date": self._normalize_time_label(label), "value": value}
                )

        if data_points:
            return data_points

        return self._parse_first_available_json_stat_series(
            values=values,
            dimensions=dimensions,
            id_list=id_list,
            sizes=sizes,
            notes_sink=notes_sink,
        )

    def _parse_first_available_json_stat_series(
        self,
        *,
        values: Dict[str, Any],
        dimensions: Dict[str, Any],
        id_list: list[str],
        sizes: list[int],
        notes_sink: Optional[list] = None,
    ) -> list[Dict[str, Any]]:
        """Parse the first viable non-default JSON-stat series.

        Many long-tail Eurostat datasets are sparse across their non-time
        dimensions.  The default tuple can be empty even when the requested
        country has public observations for another tuple.  When the primary
        parser finds no values, group non-null flattened observations by their
        non-time dimension coordinates and return the best-covered series.
        """
        if not values or len(id_list) != len(sizes) or "time" not in id_list:
            return []

        time_pos = id_list.index("time")
        time_dim = dimensions.get("time", {})
        time_indexes = time_dim.get("category", {}).get("index", {})
        if not time_indexes:
            return []
        time_label_by_index = {idx: label for label, idx in time_indexes.items()}

        dim_value_by_position: Dict[str, Dict[int, str]] = {}
        for dim_id in id_list:
            dim = dimensions.get(dim_id, {})
            category_index = dim.get("category", {}).get("index", {})
            dim_value_by_position[dim_id] = {
                idx: value_id for value_id, idx in category_index.items()
            }

        def decode_position(flat_index: int) -> Optional[list[int]]:
            coordinates: list[int] = []
            remaining = flat_index
            for size in reversed(sizes):
                if size <= 0:
                    return None
                coordinates.append(remaining % size)
                remaining //= size
            if remaining:
                return None
            return list(reversed(coordinates))

        def aggregate_preference(coordinates: list[int]) -> int:
            score = 0
            for pos, dim_id in enumerate(id_list):
                if pos == time_pos:
                    continue
                value_id = dim_value_by_position.get(dim_id, {}).get(coordinates[pos], "")
                score += self._aggregate_member_score(value_id)
                if pos == 0:
                    score += max(0, 3 - coordinates[pos])
            return score

        grouped: Dict[tuple[int, ...], Dict[str, Any]] = {}
        for flat_key, value in values.items():
            if value is None:
                continue
            try:
                flat_index = int(flat_key)
            except (TypeError, ValueError):
                continue
            coordinates = decode_position(flat_index)
            if not coordinates:
                continue
            time_index = coordinates[time_pos]
            time_label = time_label_by_index.get(time_index)
            if time_label is None:
                continue
            series_key = tuple(
                coordinate for pos, coordinate in enumerate(coordinates) if pos != time_pos
            )
            bucket = grouped.setdefault(
                series_key,
                {
                    "points": [],
                    "first_flat_index": flat_index,
                    "preference": aggregate_preference(coordinates),
                },
            )
            bucket["first_flat_index"] = min(bucket["first_flat_index"], flat_index)
            bucket["points"].append(
                {
                    "date": self._normalize_time_label(str(time_label)),
                    "value": value,
                }
            )

        if not grouped:
            return []

        best_key = max(
            grouped,
            key=lambda key: (
                len(grouped[key]["points"]),
                grouped[key]["preference"],
                -grouped[key]["first_flat_index"],
            ),
        )
        best = grouped[best_key]
        points = sorted(best["points"], key=lambda point: point["date"])
        logger.info(
            "Eurostat JSON-stat default tuple was empty; selected sparse series with %d points",
            len(points),
        )
        if notes_sink is not None:
            # best_key holds the non-time coordinates in id_list order; decode
            # them back to member codes so the returned slice is transparent.
            member_descs: list[str] = []
            key_iter = iter(best_key)
            for pos, dim_id in enumerate(id_list):
                if pos == time_pos:
                    continue
                coord = next(key_iter, None)
                if coord is None:
                    continue
                value_id = dim_value_by_position.get(dim_id, {}).get(coord, "")
                member_descs.append(f"{dim_id}={value_id}")
            notes_sink.append(
                "Eurostat default member tuple had no data; returned the "
                "best-covered available series ("
                + ", ".join(member_descs)
                + ")."
            )
        return points

    def _normalize_time_label(self, label: str) -> str:
        """Delegate to the shared SDMX period parser (Phase 3.2).

        The shared parser reproduces the start-of-quarter convention this
        method used for "YYYY" and "YYYY-Qn" labels, and additionally fixes
        the monthly "YYYY-MM" case which the old inline logic turned into a
        malformed "YYYY-MM-01-01" (it fell through both inner branches).
        """
        from ._sdmx import period_to_iso_date as _shared
        return _shared(label)

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

        # Only apply to genuine growth/change-RATE requests for INDEX data.  A bare
        # "change" substring matched "exchange rate" and "changes in inventories"
        # (real level series), silently corrupting their published values into a
        # YoY % transform; require word-boundaried, qualified phrasing instead.
        return bool(
            _EUROSTAT_RATE_INTENT_RE.search(indicator_lower)
            or _EUROSTAT_RATE_INTENT_RE.search(query_lower)
        )

    @staticmethod
    def _trim_points_to_year_range(
        data_points: list[dict], start_year, end_year
    ) -> list[dict]:
        """Keep only points whose year is within [start_year, end_year].

        Guarantees the returned window matches the request even when the API
        ignores since/untilTimePeriod. Unparseable dates are kept (never drop
        data on a formatting surprise). The year is the leading 4 chars of the
        date ("2010", "2010-Q1", "2010-03" all → 2010).
        """
        try:
            lo = int(start_year) if start_year is not None else None
            hi = int(end_year) if end_year is not None else None
        except (ValueError, TypeError):
            return data_points

        def _in_range(pt: dict) -> bool:
            try:
                y = int(str(pt.get("date", ""))[:4])
            except (ValueError, TypeError):
                return True
            if lo is not None and y < lo:
                return False
            if hi is not None and y > hi:
                return False
            return True

        return [pt for pt in data_points if _in_range(pt)]

    # Periods per year by frequency — the lag for a year-over-year change.
    _YOY_LAG_BY_FREQUENCY = {"monthly": 12, "quarterly": 4, "annual": 1, "yearly": 1}

    def _calculate_year_over_year_change(
        self, data: list[dict], frequency: str = "annual"
    ) -> list[dict]:
        """Calculate year-over-year percentage change from index/level values.

        The lag must span one YEAR for the series' frequency (12 monthly,
        4 quarterly, 1 annual). Diffing ADJACENT elements produced a
        month-over-month / quarter-over-quarter change mislabeled year-over-year
        — the number itself was wrong for any sub-annual dataset. Assumes data
        is in ascending time order (same assumption the old adjacent diff made).
        """
        lag = self._YOY_LAG_BY_FREQUENCY.get(str(frequency or "annual").lower(), 1)
        if not data or len(data) <= lag:
            # Not enough history to span a full year → no YoY is defined.
            return []

        result: list[dict] = []
        for i in range(lag, len(data)):
            prev_value = data[i - lag].get('value')
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

                if labels:
                    # Use the SAME unit choice the value parser used so the
                    # reported unit always describes the returned numbers.
                    _idx, selected_label = self._select_unit_choice(payload, dataset_code)
                    if selected_label:
                        return selected_label
                    return next(iter(labels.values()))

            currency_dim = dimensions.get("currency", {})
            if currency_dim:
                category = currency_dim.get("category", {})
                labels = category.get("label", {})
                if labels:
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

