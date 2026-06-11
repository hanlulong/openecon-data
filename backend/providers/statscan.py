from __future__ import annotations

from typing import Any, Dict, List, Optional
import csv
import io
import logging
import re
import zipfile

import httpx

from ..config import get_settings
from ..services.http_pool import get_http_client
from ..services.statscan_metadata import get_statscan_metadata_service
from ..models import Metadata, NormalizedData
from ..utils.geographies import canonicalize_canadian_region
from ..utils.retry import DataNotAvailableError
from ..services.rate_limiter import wait_for_provider, record_provider_request
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DIMENSION_MEMBER_VALUE_ALIASES: Dict[str, List[str]] = {
    # Mechanical normalization after a StatsCan product/table is already
    # selected.  These aliases map common user-facing dimension values to the
    # provider's own member labels; they must not select a product/vector/table.
    "male": ["men+", "men", "male"],
    "males": ["men+", "men", "male"],
    "men": ["men+", "men", "male"],
    "female": ["women+", "women", "female"],
    "females": ["women+", "women", "female"],
    "women": ["women+", "women", "female"],
    "youth": ["15 to 24 years", "15-24", "15 to 24", "youth"],
    "young": ["15 to 24 years", "15-24", "15 to 24"],
}


class StatsCanProvider(BaseProvider):
    """Statistics Canada Web Data Service (WDS) provider.

    Supports two methods:
    1. Vector API: For national-level aggregates (fast, simple)
    2. WDS Coordinate API: For provincial/dimensional data (requires metadata discovery)

    No API key required for basic access.
    """

    # Keep the exact-table CSV fallback bounded.  The fallback is for
    # provider-native full-table bundles that are unavailable/slow through WDS;
    # very large tables must fail closed rather than materializing arbitrary
    # provider payloads in memory.
    FULL_TABLE_CSV_MAX_BYTES = 200 * 1024 * 1024

    # Runtime cache for vector ID -> product ID mappings (populated by metadata search)
    # This cache is built dynamically when indicators are discovered via metadata search
    # Pre-populated with common indicators to avoid slow API resolution (2025-11-21)
    # IMPORTANT: Product IDs must be 10-digit numbers for table viewer URLs
    # Format: Subject(2) + Product type(2) + Specific(4) + Version(2) = 10 digits
    # Example: 14-10-0287-01 → 1410028701
    PRODUCT_ID_CACHE: Dict[int, str] = {
        # Demographics (Subject 17)
        1: "1710000501",  # Population → Population estimates by age and gender (quarterly)

        # GDP (Subject 36 - National accounts)
        65201210: "3610043401",  # GDP at basic prices, all industries
        65201211: "3610043401",  # GDP goods-producing industries
        65201212: "3610043401",  # GDP services-producing industries
        65201213: "3610043401",  # GDP business sector
        65201214: "3610043401",  # GDP non-business sector
        65201217: "3610043401",  # GDP industrial production
        65201218: "3610043401",  # GDP non-durable manufacturing
        65201219: "3610043401",  # GDP manufacturing
        65201220: "3610043401",  # GDP durable manufacturing

        # Labour (Subject 14 - Labour)
        2062815: "1410028701",  # Unemployment rate → Labour force characteristics, monthly (14-10-0287-01)
        14100239: "1410023901",  # Employment by industry
        14100287: "1410028701",  # Labour force characteristics

        # Prices (Subject 18 - Prices and price indexes)
        41690914: "1810000401",  # CPI all-items index → Consumer Price Index, monthly (18-10-0004-01)
        41690973: "1810000401",  # CPI inflation rate → Consumer Price Index (same product, different vector)

        # Housing (Subject 34 - Construction)
        52300157: "3410015801",  # Housing starts (all areas) → CMHC housing starts (34-10-0158-01)
        52299896: "3410015601",  # Housing starts (10k+) → CMHC housing starts in centres 10,000+ (34-10-0156-01)
        111955410: "1810020501",  # Housing price index → New housing price index (18-10-0205-01)

        # Immigration (Subject 17 - Population and demography)
        # NOTE: Vector 484 deprecated - dynamic discovery will use product directly
        # 484: "1710004001",  # Immigration → Components of international migration (17-10-0040-01)

        # Retail Trade (Subject 20 - Retail trade)
        7631665: "2010000801",  # Retail trade → Retail trade sales (20-10-0008-01)

        # Employment (Subject 14 - Labour)
        2062816: "1410028701",  # Employment → Labour force characteristics (14-10-0287-01)

        # Trade (Subject 12 - International trade)
        # NOTE: Trade vectors may be deprecated - dynamic discovery will use product
        38226235: "1210001101",  # Exports → International merchandise trade (12-10-0011-01)
        38226238: "1210001101",  # Imports → International merchandise trade (12-10-0011-01)

        # Labour by age group (Product 1410028702 - Labour force characteristics by age group)
        # Note: This table has dimensional structure requiring coordinate-based queries

        # Note: Provincial GDP queries should use metadata search to discover product 36100402 (Real GDP by province)
    }

    FREQUENCY_MAP: Dict[int, str] = {
        1: "daily",
        3: "weekly",
        6: "monthly",
        9: "quarterly",
        12: "annual",
    }

    SCALAR_FACTOR_MAP: Dict[int, str] = {
        0: "units",        # No multiplier
        1: "tens",         # 10
        2: "hundreds",     # 100
        3: "thousands",    # 1,000
        4: "ten thousands",     # 10,000
        5: "hundred thousands", # 100,000
        6: "millions",     # 1,000,000
        7: "ten millions",      # 10,000,000
        8: "hundred millions",  # 100,000,000
        9: "billions",     # 1,000,000,000
    }

    # Dimension member IDs for WDS coordinate-based queries
    # Based on product 17100005 (Population estimates by age and gender)

    # Dimension 0: Geography (provinces/territories)
    GEOGRAPHY_MEMBER_IDS: Dict[str, int] = {
        "CANADA": 1,
        "NEWFOUNDLAND AND LABRADOR": 2,
        "NEWFOUNDLAND": 2,
        "PRINCE EDWARD ISLAND": 3,
        "PEI": 3,
        "NOVA SCOTIA": 4,
        "NEW BRUNSWICK": 5,
        "QUEBEC": 6,
        "ONTARIO": 7,
        "MANITOBA": 8,
        "SASKATCHEWAN": 9,
        "ALBERTA": 10,
        "BRITISH COLUMBIA": 11,
        "BC": 11,
        "YUKON": 12,
        "NORTHWEST TERRITORIES": 13,
        "NWT": 13,
        "NUNAVUT": 14,
    }

    # Dimension 1: Gender/Sex
    GENDER_MEMBER_IDS: Dict[str, int] = {
        "TOTAL": 1,
        "BOTH": 1,
        "ALL": 1,
        "BOTH SEXES": 1,
        "MEN": 2,
        "MALE": 2,
        "MALES": 2,
        "MEN+": 2,
        "WOMEN": 3,
        "FEMALE": 3,
        "FEMALES": 3,
        "WOMEN+": 3,
    }

    # Dimension 2: Age groups
    # Note: There are 139 age group members. Listing key ones here.
    # For a complete list, query the cube metadata dynamically.
    AGE_GROUP_MEMBER_IDS: Dict[str, int] = {
        "ALL AGES": 1,
        "ALL": 1,
        "0 TO 4 YEARS": 2,
        "0-4": 2,
        "5 TO 9 YEARS": 3,
        "5-9": 3,
        "10 TO 14 YEARS": 4,
        "10-14": 4,
        "15 TO 19 YEARS": 5,
        "15-19": 5,
        "20 TO 24 YEARS": 6,
        "20-24": 6,
        "25 TO 29 YEARS": 7,
        "25-29": 7,
        "30 TO 34 YEARS": 8,
        "30-34": 8,
        "35 TO 39 YEARS": 9,
        "35-39": 9,
        "40 TO 44 YEARS": 10,
        "40-44": 10,
        "45 TO 49 YEARS": 11,
        "45-49": 11,
        "50 TO 54 YEARS": 12,
        "50-54": 12,
        "55 TO 59 YEARS": 13,
        "55-59": 13,
        "60 TO 64 YEARS": 14,
        "60-64": 14,
        "65 TO 69 YEARS": 15,
        "65-69": 15,
        "70 TO 74 YEARS": 16,
        "70-74": 16,
        "75 TO 79 YEARS": 17,
        "75-79": 17,
        "80 TO 84 YEARS": 18,
        "80-84": 18,
        "85 TO 89 YEARS": 19,
        "85-89": 19,
        "90 YEARS AND OVER": 20,
        "90+": 20,
    }

    # Product ID for population data by demographics
    POPULATION_DEMOGRAPHICS_PRODUCT = "17100005"

    # REMOVED: CMA_MAPPING - StatsCan WDS API does not support city-level queries
    # Statistics Canada only provides data at the province/territory level
    # City-level queries should be rejected with a helpful error message

    # Country/region name aliases for better matching
    GEOGRAPHY_ALIASES: Dict[str, str] = {
        "NFLD": "NEWFOUNDLAND AND LABRADOR",
        "NL": "NEWFOUNDLAND AND LABRADOR",
        "PE": "PRINCE EDWARD ISLAND",
        "NS": "NOVA SCOTIA",
        "NB": "NEW BRUNSWICK",
        "QC": "QUEBEC",
        "ON": "ONTARIO",
        "MB": "MANITOBA",
        "SK": "SASKATCHEWAN",
        "AB": "ALBERTA",
        "BC": "BRITISH COLUMBIA",
        "YT": "YUKON",
        "NT": "NORTHWEST TERRITORIES",
        "NU": "NUNAVUT",
        "CANADA": "CANADA",
        "CA": "CANADA",  # ISO 3166-1 alpha-2 code for Canada
        "CAN": "CANADA",  # ISO 3166-1 alpha-3 code for Canada
        "ALL": "CANADA",
        "NATIONAL": "CANADA",
    }

    @property
    def provider_name(self) -> str:
        return "StatsCan"

    def __init__(self, metadata_search_service=None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        settings = get_settings()
        self.base_url = settings.statscan_base_url.rstrip("/")
        self.metadata_search = metadata_search_service  # Optional: for intelligent indicator discovery
        self._statscan_metadata_service = get_statscan_metadata_service()
        self._cube_metadata_cache: Dict[str, Dict[str, Any]] = {}
        # Pre-populate cube metadata cache from local file for key tables.
        # This ensures dimension follow-ups work without runtime API calls.
        for _pid in ["14100287", "18100004", "36100434", "17100005", "34100156",
                      "18100205", "12100011", "14100355", "20100008", "14100017"]:
            _local = self._statscan_metadata_service.get_local_cube_metadata(_pid)
            if _local:
                self._cube_metadata_cache[_pid] = _local

    async def _fetch_data(self, **params) -> NormalizedData | List[NormalizedData]:
        """Implement BaseProvider interface by routing to fetch_indicator."""
        return await self.fetch_indicator(
            indicator=params.get("indicator", "GDP"),
            start_year=params.get("start_year"),
            end_year=params.get("end_year"),
        )

    def _get_table_viewer_url(self, product_id: str) -> str:
        """Convert 8-digit WDS product ID to 10-digit table viewer URL.

        Statistics Canada WDS API uses 8-digit product IDs (e.g., "17100005"),
        but the table viewer URLs require 10-digit IDs (e.g., "1710000501").

        The conversion appends "01" to get the default version of the table.

        Args:
            product_id: 8-digit product ID from WDS API

        Returns:
            Full table viewer URL with 10-digit product ID
        """
        # Ensure product_id is a string
        pid_str = str(product_id)

        # If already 10 digits, use as-is
        if len(pid_str) == 10:
            return f"https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid={pid_str}"

        # If 8 digits, append "01" for default version
        if len(pid_str) == 8:
            return f"https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid={pid_str}01"

        # For other lengths, return generic data search page
        return "https://www150.statcan.gc.ca/n1/en/type/data"

    @staticmethod
    def _normalize_metadata_product_id(product_id: str) -> str:
        """Normalize a product ID to the 8-digit WDS metadata form."""
        digits_only = "".join(ch for ch in str(product_id) if ch.isdigit())
        if len(digits_only) >= 10:
            return digits_only[:8]
        return digits_only

    @staticmethod
    def _extract_member_name(member: Dict[str, Any]) -> str:
        return str(member.get("memberNameEn", "")).strip()

    @staticmethod
    def _normalize_dimension_filters(dimensions: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Canonicalize loose dimension keys into provider-friendly hints."""
        normalized: Dict[str, Any] = {}
        for raw_key, raw_value in (dimensions or {}).items():
            key = str(raw_key or "").strip().lower()
            if not key:
                continue
            if "geogr" in key or "province" in key or "territor" in key:
                normalized["geography"] = raw_value
            elif "gender" in key or "sex" in key:
                normalized["gender"] = raw_value
            elif "age" in key:
                normalized["age"] = raw_value
            elif "labour force characteristic" in key or "labor force characteristic" in key:
                normalized["labour_characteristic"] = raw_value
            elif "characteristic" in key:
                normalized["characteristic"] = raw_value
            else:
                normalized[key] = raw_value
        return normalized

    @staticmethod
    def _dimension_has_aggregate_member(members: List[Dict[str, Any]]) -> bool:
        aggregate_names = {
            "all",
            "all ages",
            "all categories",
            "all names",
            "both sexes",
            "canada",
            "total",
        }
        for member in members:
            name = StatsCanProvider._extract_member_name(member).lower()
            if name in aggregate_names or name.startswith("total "):
                return True
        return False

    @staticmethod
    def _requires_explicit_dimension_member(dim_name: str, members: List[Dict[str, Any]]) -> bool:
        """Return whether defaulting this dimension would be arbitrary.

        High-cardinality dimensions with no aggregate member (for example a
        first-name list) are provider-native required dimensions.  Picking the
        first member would be a hidden default, so callers must supply a member
        or fail closed as supportability-blocked.
        """
        if len(members) <= 100:
            return False
        if StatsCanProvider._dimension_has_aggregate_member(members):
            return False
        return True

    def _find_member_id_by_keywords(self, members: List[Dict[str, Any]], keywords: List[str]) -> Optional[int]:
        """Find the best matching member ID using exact-first keyword scoring.

        Supports alias expansion: e.g. "youth" also searches for "15 to 24 years".
        """
        best_member_id: Optional[int] = None
        best_score = 0

        normalized_keywords = [kw.lower().strip() for kw in keywords if kw and kw.strip()]
        if not normalized_keywords:
            return None

        expanded_keywords = list(normalized_keywords)
        for kw in normalized_keywords:
            expanded_keywords.extend(_DIMENSION_MEMBER_VALUE_ALIASES.get(kw, []))
        expanded_keywords = list(dict.fromkeys(expanded_keywords))

        for member in members:
            member_name = self._extract_member_name(member)
            if not member_name:
                continue

            member_name_lower = member_name.lower()
            score = 0

            for keyword in expanded_keywords:
                if member_name_lower == keyword:
                    score = max(score, 120)
                elif member_name_lower.startswith(f"{keyword},") or member_name_lower.startswith(f"{keyword} "):
                    score = max(score, 100)
                elif keyword in member_name_lower:
                    score = max(score, 70 + min(len(keyword), 20))

            if score > best_score:
                best_score = score
                best_member_id = member.get("memberId")

        return best_member_id

    async def resolve_member_id(self, product_id: str, dim_keyword: str, search_term: str) -> int:
        """Dynamically find a member ID by searching actual table metadata.

        Instead of relying on hardcoded dictionaries (which assume member IDs are
        the same across all products), this method fetches the real metadata for the
        given product and searches the matching dimension for the requested term.

        Args:
            product_id: StatsCan product ID (e.g., "14100287" or "18100004").
                        Hyphens are stripped automatically.
            dim_keyword: Substring to identify the target dimension
                         (e.g., "geogr" for Geography, "sex" for Gender).
            search_term: Human-readable term to match against member names
                         (e.g., "Ontario", "Females", "15 to 24 years").

        Returns:
            The matching member ID, or 1 (the conventional "Total" / "All" default)
            if no match is found.
        """
        normalized_pid = self._normalize_metadata_product_id(product_id)
        metadata = await self._get_cube_metadata(normalized_pid)
        dim_keywords = self._decomposition_axis_keywords(dim_keyword)

        for keyword in dim_keywords:
            keyword_lower = keyword.lower()
            for dim in metadata.get("dimension", []):
                dim_name = dim.get("dimensionNameEn", "").lower()
                if keyword_lower not in dim_name:
                    continue
                members = dim.get("member", [])
                result = self._find_member_id_by_keywords(members, [search_term])
                if result is not None:
                    return result

        logger.debug(
            f"resolve_member_id: no match for '{search_term}' in dimension "
            f"'{dim_keyword}' of product {product_id}; defaulting to 1"
        )
        return 1  # Default to first member

    async def get_dimension_members(
        self, product_id: str, dim_keyword: str, parent_id: Optional[int] = None
    ) -> List[tuple]:
        """Get all members of a dimension (e.g., all provinces for Geography).

        Useful for "by province" or "by age group" queries where the caller
        needs every child of a parent node.

        Args:
            product_id: StatsCan product ID (e.g., "14100287").
            dim_keyword: Substring to identify the target dimension
                         (e.g., "geogr" for Geography).
            parent_id: If provided, only return members whose
                       ``parentMemberId`` equals this value. For example,
                       passing ``parent_id=1`` (Canada) returns only the
                       provinces directly under Canada, excluding sub-
                       regions or the national total itself.

        Returns:
            List of ``(member_id, member_name_en)`` tuples.
        """
        normalized_pid = self._normalize_metadata_product_id(product_id)
        metadata = await self._get_cube_metadata(normalized_pid)
        dim_keywords = self._decomposition_axis_keywords(dim_keyword)

        for keyword in dim_keywords:
            keyword_lower = keyword.lower()
            for dim in metadata.get("dimension", []):
                dim_name = dim.get("dimensionNameEn", "").lower()
                if keyword_lower not in dim_name:
                    continue
                members = dim.get("member", [])
                if parent_id is not None:
                    return [
                        (m["memberId"], m.get("memberNameEn", ""))
                        for m in members
                        if m.get("parentMemberId") == parent_id
                    ]
                return [
                    (m["memberId"], m.get("memberNameEn", ""))
                    for m in members
                ]
        return []

    @staticmethod
    def _decomposition_axis_keywords(axis_hint: str) -> List[str]:
        axis_lower = str(axis_hint or "").strip().lower()
        if "geogr" in axis_lower or "province" in axis_lower or "region" in axis_lower:
            return ["geogr"]
        if "age" in axis_lower:
            return ["age"]
        if "gender" in axis_lower or "sex" in axis_lower:
            return ["gender", "sex"]
        if "labour" in axis_lower or "labor" in axis_lower or "characteristic" in axis_lower:
            return ["labour force", "labor force", "characteristic"]
        return [axis_lower]

    @staticmethod
    def _is_aggregate_decomposition_member(axis_hint: str, member_name: str) -> bool:
        axis_lower = str(axis_hint or "").strip().lower()
        name_lower = str(member_name or "").strip().lower()
        if not name_lower:
            return True
        if "geogr" in axis_lower or "province" in axis_lower or "region" in axis_lower:
            return name_lower in {"canada", "total", "all"}
        if "age" in axis_lower:
            return name_lower in {
                "15 years and over",
                "all ages",
                "total",
                "25 years and over",
                "55 years and over",
            }
        if "gender" in axis_lower or "sex" in axis_lower:
            return name_lower in {"both sexes", "total - gender", "total", "all"}
        if "labour" in axis_lower or "labor" in axis_lower or "characteristic" in axis_lower:
            return name_lower in {"population", "labour force", "labor force", "employment", "unemployment"}
        return name_lower in {"total", "all"}

    def _select_default_member_id(
        self,
        dimension_name: str,
        members: List[Dict[str, Any]],
        indicator_lower: str,
    ) -> int:
        """Choose a semantically sensible default instead of blindly taking member 1."""
        if not members:
            return 1

        # Normalize underscores to spaces so "unemployment_rate" matches "unemployment rate"
        indicator_lower = indicator_lower.replace("_", " ")

        dimension_name_lower = dimension_name.lower()

        if "geogr" in dimension_name_lower:
            return self._find_member_id_by_keywords(members, ["canada", "total"]) or 1

        if any(term in dimension_name_lower for term in ["labour force characteristic", "labour force", "labor force"]):
            # IMPORTANT: Check "unemployment rate" BEFORE "employment rate" because
            # "unemployment rate" contains "employment rate" as a substring.
            if "unemployment rate" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["unemployment rate"]) or 1
            if "employment rate" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["employment rate"]) or 1
            if "employment-to-population" in indicator_lower or "employment to population" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["employment rate", "employment-to-population ratio"]) or 1
            if "participation rate" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["participation rate"]) or 1
            if "unemployment" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["unemployment"]) or 1
            if "full-time" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["full-time employment"]) or 1
            if "part-time" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["part-time employment"]) or 1
            if "employment" in indicator_lower or "employed" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["employment"]) or 1
            if "labour force" in indicator_lower or "labor force" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["labour force"]) or 1

        if any(term in dimension_name_lower for term in ["type of retail store", "kind of business", "industry", "sector"]):
            return self._find_member_id_by_keywords(
                members,
                ["total retail, all stores", "all industries", "all sectors", "total", "all"],
            ) or 1

        if "retail trade component" in dimension_name_lower:
            return self._find_member_id_by_keywords(members, ["all stores", "total", "all"]) or 1

        if any(term in dimension_name_lower for term in ["seasonal", "adjustment"]):
            if "unadjusted" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["unadjusted"]) or 1
            if "trend" in indicator_lower:
                return self._find_member_id_by_keywords(members, ["trend-cycle", "trend"]) or 1
            return self._find_member_id_by_keywords(members, ["seasonally adjusted", "adjusted"]) or 1

        if "basis" in dimension_name_lower:
            return self._find_member_id_by_keywords(members, ["balance of payments", "bop", "customs basis"]) or 1

        if "characteristics" in dimension_name_lower:
            if any(term in indicator_lower for term in ["rate", "ratio", "share", "percent", "%"]):
                return self._find_member_id_by_keywords(members, ["percent"]) or 1
            return self._find_member_id_by_keywords(members, ["percent", "number of persons", "number", "value"]) or 1

        if any(term in dimension_name_lower for term in ["statistics", "statistic"]):
            return self._find_member_id_by_keywords(members, ["estimate", "number", "value", "total"]) or 1

        if any(term in dimension_name_lower for term in ["gender", "sex"]):
            return self._find_member_id_by_keywords(members, ["total - gender", "both sexes", "total"]) or 1

        if "age group" in dimension_name_lower:
            return self._find_member_id_by_keywords(members, ["15 years and over", "all ages", "total"]) or 1

        return self._find_member_id_by_keywords(members, ["total", "all", "canada", "estimate"]) or (
            members[0].get("memberId") if members[0].get("memberId") is not None else 1
        )

    @staticmethod
    def _build_wds_coordinate(parts: List[int]) -> str:
        """Build a 10-position WDS coordinate string from discovered member IDs."""
        coordinate = ".".join(str(p) for p in parts[:10])
        while coordinate.count(".") < 9:
            coordinate += ".0"
        return coordinate

    def _coordinate_member_candidates(
        self,
        dimension_name: str,
        members: List[Dict[str, Any]],
        indicator_lower: str,
        selected_member_id: int,
        *,
        limit: int = 6,
    ) -> List[int]:
        """Return bounded alternate member IDs for fallback coordinate probing.

        Some StatsCan tables have no explicit national/total aggregate and WDS
        can reject the all-first-member coordinate even when nearby coordinates
        in the same product are valid.  Keep the chosen member first, then add
        aggregate-like and early product members so direct table-title queries
        can recover a valid provider-native series without hardcoding tables.
        """
        candidates: List[int] = []

        def add(value: Any) -> None:
            try:
                member_id = int(value)
            except (TypeError, ValueError):
                return
            if member_id not in candidates:
                candidates.append(member_id)

        add(selected_member_id)
        add(self._select_default_member_id(dimension_name, members, indicator_lower))
        aggregate_id = self._find_member_id_by_keywords(
            members,
            ["total", "all", "canada", "estimate", "all-items"],
        )
        add(aggregate_id)
        for member in members:
            add(member.get("memberId"))
            if len(candidates) >= limit:
                break
        return candidates[:limit] or [1]

    @staticmethod
    def _title_specialization_penalty(query_text: str, candidate_text: str) -> float:
        """Penalize specialized subgroup series when the query asked for a generic metric."""
        query_lower = str(query_text or "").lower()
        candidate_lower = str(candidate_text or "").lower()
        if not candidate_lower:
            return 0.0

        penalty = 0.0

        def query_mentions(*terms: str) -> bool:
            return any(term in query_lower for term in terms)

        def candidate_mentions(*terms: str) -> bool:
            return any(term in candidate_lower for term in terms)

        if not query_mentions("student", "students", "school months", "summer months") and candidate_mentions(
            "student",
            "students",
            "school months",
            "summer months",
        ):
            penalty += 15.0

        if not query_mentions("education", "educational attainment", "school") and candidate_mentions(
            "educational attainment",
            "education",
        ):
            penalty += 12.0

        if not query_mentions("fiscal") and candidate_mentions("fiscal"):
            penalty += 10.0

        if not query_mentions("indigenous") and candidate_mentions("indigenous"):
            penalty += 10.0

        if not query_mentions("youth", "age", "aged", "years old", "15-24", "15 to 24", "25-64", "25 to 64") and (
            re.search(r"\baged?\b", candidate_lower)
            or re.search(r"\b\d{1,2}\s*(?:to|-)\s*\d{1,2}\b", candidate_lower)
            or re.search(r"\b\d{1,2}\s+years?\s+(?:and|or)\s+over\b", candidate_lower)
        ):
            penalty += 8.0

        return penalty

    def _score_cube_metadata_relevance(
        self,
        metadata: Optional[Dict[str, Any]],
        search_terms: List[str],
    ) -> float:
        """Use dimension names and members to prefer cubes whose semantics match the query."""
        if not metadata:
            return 0.0

        score = 0.0
        normalized_terms = [term.lower().strip() for term in search_terms if term and term.strip()]

        for dimension in metadata.get("dimension", []):
            dimension_name = str(dimension.get("dimensionNameEn", "")).lower()
            if any(term in dimension_name for term in normalized_terms):
                score += 6.0

            best_member_score = 0.0
            for member in dimension.get("member", []):
                member_name = self._extract_member_name(member).lower()
                if not member_name:
                    continue

                for term in normalized_terms:
                    if member_name == term:
                        best_member_score = max(best_member_score, 25.0)
                    elif member_name.startswith(f"{term},") or member_name.startswith(f"{term} "):
                        best_member_score = max(best_member_score, 12.0)
                    elif term in member_name:
                        best_member_score = max(best_member_score, 4.0)

            score += best_member_score

        return score

    async def _vector_id(self, indicator: Optional[str], vector_id: Optional[int]) -> int:
        """
        Get vector ID from indicator name or direct vector ID.

        Natural-language indicator names are resolved only through provider
        metadata discovery.  Exact vector IDs may be supplied directly by the
        user or by an upstream LLM/evidence selection step.
        """
        if vector_id:
            return vector_id
        if not indicator:
            raise ValueError("Vector ID or indicator is required")

        # Check if indicator is a numeric string (LLM sometimes returns vector ID directly)
        indicator_stripped = indicator.strip()
        if indicator_stripped.isdigit():
            vector_id_int = int(indicator_stripped)
            # Provider-native vector IDs are exact mechanical inputs.
            if len(indicator_stripped) >= 7:
                logger.info(f"✅ Using exact numeric vector ID: {vector_id_int}")
                return vector_id_int

        # If not an exact vector, try intelligent metadata search (SDMX-first)
        if self.metadata_search:
            logger.info(f"🔍 Searching Statistics Canada metadata for '{indicator}' (SDMX-first)...")

            try:
                # Search metadata catalog (SDMX first, then StatsCan API)
                search_results = await self.metadata_search.search_with_sdmx_fallback(
                    provider="StatsCan",
                    indicator=indicator,
                )

                if search_results:
                    # Use LLM to select best match
                    discovery = await self.metadata_search.discover_indicator(
                        provider="StatsCan",
                        indicator_name=indicator,
                        search_results=search_results
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

                    if discovery and discovery.get("confidence", 0) > 0.6:
                        discovered_id = discovery["code"]
                        logger.info(
                            f"✅ Discovered indicator code {discovered_id} for '{indicator}' "
                            f"(confidence: {discovery['confidence']})"
                        )

                        # Check if discovered_id is numeric (vector ID) or alphanumeric (SDMX/product ID)
                        if isinstance(discovered_id, str) and not discovered_id.isdigit():
                            # SDMX-style alphanumeric code (e.g., 'TET00003', 'MED_AG50')
                            # Treat as product ID and skip caching as vector ID
                            logger.info(f"ℹ️ Code '{discovered_id}' is alphanumeric (SDMX product ID), not a numeric vector ID")
                            # Raise to trigger WDS dynamic discovery with this product ID
                            raise DataNotAvailableError(
                                f"SDMX code '{discovered_id}' found but needs WDS coordinate-based query. "
                                f"Using dynamic discovery..."
                            )
                        else:
                            # Numeric vector ID selected by metadata/LLM evidence.
                            return int(discovered_id)
                    else:
                        logger.warning(
                            f"⚠️ Low confidence match for '{indicator}' (confidence: {discovery.get('confidence', 0) if discovery else 0}). "
                            f"Falling back to WDS dynamic discovery..."
                        )

            except Exception as e:
                logger.warning(f"Error during SDMX metadata search: {e}. Falling back to WDS discovery...")

        raise DataNotAvailableError(
            f"Cannot determine vector ID for '{indicator}' with confidence. "
            f"Provide an exact Statistics Canada vector ID or product/table ID, "
            f"or enable metadata discovery with LLM/evidence adjudication."
        )

    def _resolve_geography(self, geography: Optional[str]) -> int:
        """
        Resolve a geography name to its member ID for Canadian provinces/territories only.

        Statistics Canada WDS API only supports province/territory-level data.
        City-level queries and non-Canadian countries are not supported.

        Args:
            geography: Geography name (province/territory abbreviation or full name, or None for Canada)

        Returns:
            Member ID for the geography (1 = Canada by default)

        Raises:
            ValueError: If geography is not a valid Canadian province/territory
        """
        if not geography:
            return 1  # Default to Canada

        geography_upper = geography.upper()

        # List of known city names that should be rejected with helpful message
        CANADIAN_CITIES = [
            "TORONTO", "VANCOUVER", "MONTREAL", "CALGARY", "EDMONTON",
            "WINNIPEG", "OTTAWA", "HALIFAX", "QUEBEC CITY", "VICTORIA",
            "REGINA", "SASKATOON", "MISSISSAUGA", "BRAMPTON", "HAMILTON",
            "KITCHENER", "LONDON", "MARKHAM", "VAUGHAN", "GATINEAU"
        ]

        # List of known non-Canadian country codes
        NON_CANADIAN_COUNTRIES = [
            "US", "USA", "UNITED STATES", "CN", "CHINA", "EU", "EUROPE",
            "UK", "UNITED KINGDOM", "JP", "JAPAN", "DE", "GERMANY",
            "FR", "FRANCE", "IT", "ITALY", "ES", "SPAIN", "MX", "MEXICO",
            "BR", "BRAZIL", "IN", "INDIA", "AU", "AUSTRALIA", "KR", "KOREA"
        ]

        # Reject city-level queries
        if geography_upper in CANADIAN_CITIES or geography_upper.replace("_", " ") in CANADIAN_CITIES:
            raise ValueError(
                f"City-level data not supported: '{geography}'. "
                f"Statistics Canada only provides province/territory-level data. "
                f"Please specify a province (e.g., 'Ontario' instead of 'Toronto', 'BC' instead of 'Vancouver'). "
                f"Available: {', '.join(sorted([k for k in self.GEOGRAPHY_MEMBER_IDS.keys() if k != 'CANADA'])[:8])}..."
            )

        # Reject non-Canadian countries
        if geography_upper in NON_CANADIAN_COUNTRIES:
            raise ValueError(
                f"Non-Canadian geography: '{geography}'. "
                f"Statistics Canada only provides data for Canadian provinces/territories. "
                f"For {geography} data, try providers like FRED (US), WorldBank (global), OECD, or Eurostat (EU)."
            )

        # Check geography aliases first (short forms like "ON", "QC")
        if geography_upper in self.GEOGRAPHY_ALIASES:
            canonical_name = self.GEOGRAPHY_ALIASES[geography_upper]
            geography_upper = canonical_name.upper()

        # Check province/territory mappings
        if geography_upper in self.GEOGRAPHY_MEMBER_IDS:
            return self.GEOGRAPHY_MEMBER_IDS[geography_upper]

        # Unknown geography - provide helpful error
        available_provinces = sorted(
            [k for k in self.GEOGRAPHY_MEMBER_IDS.keys() if k != "CANADA"]
        )
        raise ValueError(
            f"Unknown geography: '{geography}'. "
            f"Statistics Canada only supports Canadian provinces/territories. "
            f"Available: {', '.join(available_provinces)}. "
            f"You can also use 'Canada' for national data."
        )

    def _map_frequency(self, freq_code: int) -> str:
        return self.FREQUENCY_MAP.get(freq_code, "unknown")

    def _map_scalar_factor(self, scalar_code: int) -> str:
        return self.SCALAR_FACTOR_MAP.get(scalar_code, "")

    @staticmethod
    def _raise_for_status_or_data_unavailable(response, context: str) -> None:
        """Convert upstream StatsCan HTTP failures into data-unavailable errors.

        QueryService handles ``DataNotAvailableError`` as a provider/data
        availability failure (HTTP 200 with an explanatory error body).  Letting
        raw ``httpx.HTTPStatusError`` escape turns transient upstream WDS 5xx/429
        responses into internal ``processing_error`` 500s, which obscures
        certification evidence and user-facing provider status.
        """
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 429:
                raise DataNotAvailableError(
                    f"Statistics Canada API rate limit exceeded while {context}. "
                    "Please try again shortly."
                ) from exc
            if status_code >= 500:
                raise DataNotAvailableError(
                    f"Statistics Canada API temporarily unavailable while {context} "
                    f"(HTTP {status_code}). Please try again shortly."
                ) from exc
            raise DataNotAvailableError(
                f"Statistics Canada API request failed while {context} "
                f"(HTTP {status_code}): {exc}"
            ) from exc

    def _filter_by_date_range(
        self,
        data_points: List[Dict[str, any]],
        start_date: Optional[str],
        end_date: Optional[str]
    ) -> List[Dict[str, any]]:
        """Filter data points by date range.

        Args:
            data_points: List of data points with 'date' field
            start_date: Start date in ISO format (e.g., '2010-01-01') or None
            end_date: End date in ISO format (e.g., '2020-12-31') or None

        Returns:
            Filtered list of data points within the date range
        """
        if not start_date and not end_date:
            return data_points

        filtered = []
        for point in data_points:
            date_str = point.get("date", "")
            if not date_str:
                continue

            # Extract year-month from date (handles both 'YYYY-MM-DD' and 'YYYY-MM' formats)
            try:
                # StatsCan dates are typically 'YYYY-MM-01' or 'YYYY-MM'
                date_parts = date_str.split("-")
                point_year = int(date_parts[0])
                point_month = int(date_parts[1]) if len(date_parts) > 1 else 1
                point_day = int(date_parts[2]) if len(date_parts) > 2 else 1

                # Check start date
                if start_date:
                    start_parts = start_date.split("-")
                    start_year = int(start_parts[0])
                    start_month = int(start_parts[1]) if len(start_parts) > 1 else 1
                    start_day = int(start_parts[2]) if len(start_parts) > 2 else 1

                    if (point_year, point_month, point_day) < (start_year, start_month, start_day):
                        continue

                # Check end date
                if end_date:
                    end_parts = end_date.split("-")
                    end_year = int(end_parts[0])
                    end_month = int(end_parts[1]) if len(end_parts) > 1 else 12
                    end_day = int(end_parts[2]) if len(end_parts) > 2 else 31

                    if (point_year, point_month, point_day) > (end_year, end_month, end_day):
                        continue

                filtered.append(point)
            except (ValueError, IndexError):
                # If date parsing fails, include the point
                filtered.append(point)

        logger.info(
            f"📅 Date filter applied: {len(data_points)} → {len(filtered)} points "
            f"({start_date or 'start'} to {end_date or 'end'})"
        )
        return filtered

    def _get_unit_description(self, indicator_name: str, scalar_code: int) -> str:
        """
        Get a human-readable unit description based on indicator and scalar code.

        Args:
            indicator_name: Name of the indicator
            scalar_code: Scalar factor code from StatsCan

        Returns:
            Unit description string
        """
        scalar_unit = self._map_scalar_factor(scalar_code)

        # Check indicator type to provide better unit context
        indicator_upper = indicator_name.upper() if indicator_name else ""

        if any(x in indicator_upper for x in ["UNEMPLOYMENT", "RATE", "PERCENT"]):
            return "percent"
        elif any(x in indicator_upper for x in ["INDEX", "CPI", "PRICE"]):
            return "index (2007=100)" if "PRICE" in indicator_upper else "index (2002=100)"
        elif any(x in indicator_upper for x in ["POPULATION", "COUNT", "NUMBER"]):
            return scalar_unit or "persons"
        else:
            # For monetary/aggregate values, use scalar description
            return scalar_unit or "units"

    def _normalize_units(self, value: float | None, from_scalar_code: int, to_unit: str = "billions", indicator_name: Optional[str] = None) -> tuple[float | None, str]:
        """Convert values from StatsCan's scalar factor to a target unit.

        StatsCan API returns values with scalarFactorCode indicating the unit:
        - Code 0: units (1)
        - Code 1: tens (10)
        - Code 2: hundreds (100)
        - Code 3: thousands (1,000)
        - Code 4: ten thousands (10,000)
        - Code 5: hundred thousands (100,000)
        - Code 6: millions (1,000,000)
        - Code 7: ten millions (10,000,000)
        - Code 8: hundred millions (100,000,000)
        - Code 9: billions (1,000,000,000)

        For better UX, we convert large monetary values to billions.
        For example, if scalar_code=6 (millions) and value=2287214:
        - Value is 2,287,214 million dollars
        - Convert to billions: 2,287,214 / 1,000 = 2,287.214 billion
        - This makes GDP values human-readable

        Args:
            value: The scaled value from the API (e.g., 2287214)
            from_scalar_code: The scalar factor code from the API (e.g., 6 for millions)
            to_unit: Target unit ("billions", "millions", "thousands", etc.)
            indicator_name: Name of indicator for context-aware unit mapping

        Returns:
            Tuple of (converted_value, unit_label)
        """
        if value is None:
            return None, self._get_unit_description(indicator_name or "", from_scalar_code)

        # Define conversion factors - these match the scalar factor codes
        scale_factors = {
            "units": 1,
            "tens": 10,
            "hundreds": 100,
            "thousands": 1_000,
            "ten thousands": 10_000,
            "hundred thousands": 100_000,
            "millions": 1_000_000,
            "ten millions": 10_000_000,
            "hundred millions": 100_000_000,
            "billions": 1_000_000_000,
        }

        # Get source unit from scalar code
        source_unit = self._map_scalar_factor(from_scalar_code)
        if not source_unit or source_unit not in scale_factors:
            # Unknown scalar code, return as-is
            return value, self._get_unit_description(indicator_name or "", from_scalar_code)

        source_factor = scale_factors[source_unit]
        target_factor = scale_factors.get(to_unit, source_factor)

        # Convert: value * source_factor / target_factor
        # Example: 2,287,214 millions → billions
        #          2,287,214 * 1,000,000 / 1,000,000,000 = 2,287.214
        if source_factor != target_factor:
            converted_value = value * source_factor / target_factor
            return converted_value, to_unit
        else:
            # Source and target are same, use context-aware unit description
            final_unit = self._get_unit_description(indicator_name or "", from_scalar_code)
            return value, final_unit

    def _extract_detailed_metadata(
        self,
        cube_metadata: Dict[str, any],
        coordinate: Optional[str] = None
    ) -> Dict[str, any]:
        """Extract detailed metadata from cube metadata.

        Args:
            cube_metadata: Raw cube metadata from getCubeMetadata API
            coordinate: Optional coordinate string (e.g., "1.1.1.1.0.0.0.0.0.0") to get specific dimension values

        Returns:
            Dictionary with extracted metadata fields:
            - seasonalAdjustment: e.g., "Seasonally adjusted at annual rates"
            - priceType: e.g., "Chained (2017) dollars"
            - description: Full cube title
            - startDate, endDate: Data range
        """
        extracted = {}

        # Get cube title as description
        extracted["description"] = cube_metadata.get("cubeTitleEn", "")

        # Get date range
        extracted["startDate"] = cube_metadata.get("cubeStartDate")
        extracted["endDate"] = cube_metadata.get("cubeEndDate")

        # Parse dimensions to find seasonal adjustment, price type, etc.
        dimensions = cube_metadata.get("dimension", [])
        coord_parts = coordinate.split(".") if coordinate else []

        for dim_idx, dim_info in enumerate(dimensions):
            dim_name = dim_info.get("dimensionNameEn", "").upper()
            members = dim_info.get("member", [])

            # Get the selected member ID from coordinate
            selected_member_id = int(coord_parts[dim_idx]) if dim_idx < len(coord_parts) and coord_parts[dim_idx].isdigit() else 1

            # Find the member name for the selected ID
            selected_member_name = None
            for member in members:
                if member.get("memberId") == selected_member_id:
                    selected_member_name = member.get("memberNameEn", "")
                    break

            # Categorize based on dimension name
            if "SEASONAL" in dim_name or "ADJUSTMENT" in dim_name:
                extracted["seasonalAdjustment"] = selected_member_name or "Not specified"
            elif "PRICE" in dim_name or "DOLLAR" in dim_name or "VALUE" in dim_name:
                extracted["priceType"] = selected_member_name or "Not specified"
            elif "TYPE" in dim_name and "DATA" in dim_name:
                extracted["dataType"] = selected_member_name or "Level"

        # Default data type if not found
        if not extracted.get("dataType"):
            # Infer from title
            title_upper = extracted.get("description", "").upper()
            if "PERCENT CHANGE" in title_upper or "% CHANGE" in title_upper:
                extracted["dataType"] = "Percent change"
            elif "CHANGE" in title_upper:
                extracted["dataType"] = "Change"
            elif "INDEX" in title_upper:
                extracted["dataType"] = "Index"
            else:
                extracted["dataType"] = "Level"

        return extracted

    async def _get_cube_metadata(self, product_id: str) -> Dict[str, any]:
        """Get detailed metadata for a StatsCan cube/product.

        Uses the WDS getCubeMetadata endpoint (POST) to discover:
        - Available dimensions (geography, time period, etc.)
        - Member IDs for filtering
        - Data structure and hierarchy

        Args:
            product_id: Product ID (e.g., "14100287" for labour force data)

        Returns:
            Dictionary with cube metadata including dimensions and members
        """
        normalized_product_id = self._normalize_metadata_product_id(product_id)
        if not normalized_product_id:
            raise DataNotAvailableError(f"Invalid Statistics Canada product ID: {product_id}")

        cached_metadata = self._cube_metadata_cache.get(normalized_product_id)
        if cached_metadata:
            return cached_metadata

        local_metadata = self._statscan_metadata_service.get_local_cube_metadata(normalized_product_id)
        if local_metadata:
            self._cube_metadata_cache[normalized_product_id] = local_metadata
            logger.info(f"💾 Using local cube metadata for product {normalized_product_id}")
            return local_metadata

        try:
            # Use shared HTTP client pool for better performance
            client = get_http_client()
            logger.info(f"📊 Fetching metadata for product {normalized_product_id}")
            response = await client.post(
                f"{self.base_url}/getCubeMetadata",
                json=[{"productId": normalized_product_id}],
                headers={"Content-Type": "application/json"},
                timeout=30.0
            )
            response.raise_for_status()
            payload = response.json()

            # Response is an array with status and object
            if payload and len(payload) > 0:
                response_obj = payload[0]
                if response_obj.get("status") == "SUCCESS":
                    metadata = response_obj.get("object", {})
                    self._cube_metadata_cache[normalized_product_id] = metadata
                    logger.info(f"✅ Retrieved metadata for product {normalized_product_id}")
                    return metadata
                else:
                    raise ValueError(f"API error for product {normalized_product_id}: {response_obj.get('status')}")
            else:
                raise ValueError(f"Empty response for product {normalized_product_id}")

        except Exception as e:
            logger.warning(
                "Failed to get WDS metadata for product %s; trying full-table CSV metadata fallback: %s",
                normalized_product_id,
                e,
            )
            return await self._get_cube_metadata_via_full_table_csv(normalized_product_id)

    def _find_dimension_member(
        self,
        dimensions: List[Dict[str, any]],
        dimension_keywords: List[str],
        member_search: str
    ) -> tuple:
        """Find a dimension member that matches the search term.

        Searches through cube dimensions for one matching the keywords,
        then finds a member within that dimension matching the search term.

        Args:
            dimensions: List of dimension objects from cube metadata
            dimension_keywords: Keywords to identify the dimension (e.g., ["NAICS", "INDUSTRY"])
            member_search: Term to search for in member names (e.g., "goods-producing")

        Returns:
            Tuple of (dimension_index, member_id, member_name) or (None, None, None) if not found
        """
        search_terms = member_search.lower().replace("-", " ").replace("_", " ").split()

        for dim_idx, dim_info in enumerate(dimensions):
            dim_name = dim_info.get("dimensionNameEn", "").upper()

            # Check if this dimension matches our target
            if not any(kw.upper() in dim_name for kw in dimension_keywords):
                continue

            members = dim_info.get("member", [])
            best_match = None
            best_score = 0

            for member in members:
                member_name = member.get("memberNameEn", "")
                member_name_lower = member_name.lower()
                member_id = member.get("memberId")

                # Score based on how many search terms match
                score = sum(1 for term in search_terms if term in member_name_lower)

                # Exact phrase match gets bonus
                if member_search.lower().replace("-", " ").replace("_", " ") in member_name_lower:
                    score += 5

                if score > best_score:
                    best_score = score
                    best_match = (dim_idx, member_id, member_name)

            if best_match and best_score > 0:
                logger.info(f"✅ Found member match: '{best_match[2]}' (id={best_match[1]}) in dimension {dim_idx}")
                return best_match

        return (None, None, None)

    async def fetch_with_breakdown(
        self,
        params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch data with a specific industry/sector breakdown.

        This is a general method that dynamically finds the appropriate
        dimension member based on the breakdown parameter.

        Args:
            params: Dictionary containing:
                - indicator: Base indicator (e.g., "GDP")
                - breakdown: Industry/sector breakdown (e.g., "goods-producing", "services", "manufacturing")
                - startDate, endDate: Date range
                - periods: Number of periods

        Returns:
            NormalizedData with the breakdown data
        """
        indicator = params.get("indicator", "GDP")
        breakdown = params.get("breakdown") or params.get("industry")
        periods = params.get("periods", 240)
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        if not breakdown:
            # No breakdown specified, use regular fetch
            return await self.fetch_series(params)

        product_id = self._normalize_metadata_product_id(params.get("productId"))
        vector_id = params.get("vectorId")
        if not product_id and vector_id:
            try:
                product_id = self._normalize_metadata_product_id(
                    await self._get_product_id_from_vector(int(vector_id))
                )
            except Exception:
                product_id = None
        if not product_id and str(indicator or "").strip().isdigit():
            product_id = self._normalize_metadata_product_id(indicator)

        if not product_id:
            raise DataNotAvailableError(
                "StatsCan breakdown queries require an exact productId/table ID "
                "or vectorId selected upstream by metadata/LLM evidence."
            )

        # Normalize product ID to 8 digits (API requirement)
        # 10-digit IDs like 3610043401 need to be converted to 8-digit: 36100434
        product_id_str = str(product_id)
        if len(product_id_str) == 10:
            product_id_str = product_id_str[:8]  # Remove version suffix (last 2 digits)
        elif len(product_id_str) < 8:
            product_id_str = product_id_str.zfill(8)  # Pad with leading zeros

        logger.info(f"🔍 Fetching {indicator} with breakdown: {breakdown} (product: {product_id_str})")

        # Get cube metadata to find the dimension structure
        cube_meta = await self._get_cube_metadata(product_id_str)
        dimensions = cube_meta.get("dimension", [])

        if not dimensions:
            raise DataNotAvailableError(f"No dimensions found for product {product_id}")

        # Find the industry/sector dimension and matching member
        dim_idx, member_id, member_name = self._find_dimension_member(
            dimensions,
            dimension_keywords=["NAICS", "INDUSTRY", "SECTOR", "CLASSIFICATION"],
            member_search=breakdown
        )

        if dim_idx is None:
            # List available breakdowns
            available = []
            for dim in dimensions:
                dim_name = dim.get("dimensionNameEn", "").upper()
                if any(kw in dim_name for kw in ["NAICS", "INDUSTRY", "SECTOR"]):
                    for m in dim.get("member", [])[:10]:
                        available.append(m.get("memberNameEn", ""))

            raise DataNotAvailableError(
                f"Could not find breakdown '{breakdown}' for {indicator}. "
                f"Available breakdowns: {', '.join(available[:8])}..."
            )

        # Build coordinate: default member IDs (1) for all dimensions except the target
        coordinate_parts = []
        for i, dim in enumerate(dimensions):
            if i == dim_idx:
                coordinate_parts.append(str(member_id))
            else:
                coordinate_parts.append("1")  # Default to first member (usually "Total" or "All")

        # Pad to 10 dimensions
        while len(coordinate_parts) < 10:
            coordinate_parts.append("0")

        coordinate = ".".join(coordinate_parts[:10])
        logger.info(f"📊 Using coordinate: {coordinate} for {indicator} - {member_name}")

        # Fetch data using coordinate
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": int(product_id_str),  # Use normalized 8-digit product ID
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )

        self._raise_for_status_or_data_unavailable(
            response,
            f"fetching product {product_id} coordinate {coordinate}",
        )
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            error_msg = payload[0].get("object", "Unknown error") if payload else "Empty response"
            raise DataNotAvailableError(
                f"StatsCan query failed for {indicator} ({breakdown}): {error_msg}"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(
                f"No data found for {indicator} - {breakdown}"
            )

        # Build data points
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)
        frequency = self._map_frequency(freq_code)
        unit = self._map_scalar_factor(scalar_code) or "units"

        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date filter
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build indicator name
        indicator_name = f"Canadian {indicator} - {member_name}"

        # Get detailed metadata
        detailed_meta = self._extract_detailed_metadata(cube_meta, coordinate)

        source_url = self._get_table_viewer_url(product_id_str)

        metadata = Metadata(
            source="Statistics Canada",
            indicator=indicator_name,
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id_str}:{coordinate}",
            apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            sourceUrl=source_url,
            seasonalAdjustment=detailed_meta.get("seasonalAdjustment"),
            priceType=detailed_meta.get("priceType"),
            dataType=detailed_meta.get("dataType"),
            description=detailed_meta.get("description"),
            scaleFactor=self._map_scalar_factor(scalar_code),
            startDate=data_points[0]["date"] if data_points else None,
            endDate=data_points[-1]["date"] if data_points else None,
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def _get_product_id_from_vector(self, vector_id: int) -> str:
        """Query StatsCan API to get the product ID for a given vector ID.

        This is needed when metadata search discovers a vector ID but we need
        the product ID for batch API calls. Results are cached for future use.

        Args:
            vector_id: The vector ID to look up

        Returns:
            Product ID string (e.g., "17100005")

        Raises:
            ValueError: If product ID cannot be determined
        """
        # Check cache first
        if vector_id in self.PRODUCT_ID_CACHE:
            product_id = self.PRODUCT_ID_CACHE[vector_id]
            logger.debug(f"✅ Using cached product ID {product_id} for vector {vector_id}")
            return product_id

        # Query StatsCan API for vector metadata
        logger.info(f"🔍 Querying StatsCan API for product ID of vector {vector_id}")
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        try:
            response = await client.post(
                f"{self.base_url}/getSeriesInfoFromVector",
                json=[{"vectorId": vector_id}],
                headers={"Content-Type": "application/json"},
                timeout=30.0
            )
            response.raise_for_status()
            data = response.json()

            if data and len(data) > 0:
                product_id = str(data[0].get("productId", ""))
                if product_id:
                    # Cache for future use
                    self.PRODUCT_ID_CACHE[vector_id] = product_id
                    logger.info(f"✅ Discovered product ID {product_id} for vector {vector_id} (cached)")
                    return product_id

            raise ValueError(f"Product ID not found for vector {vector_id}")

        except Exception as e:
            logger.error(f"Failed to get product ID for vector {vector_id}: {e}")
            raise ValueError(f"Could not determine product ID for vector {vector_id}: {e}")

    async def fetch_by_coordinate(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch data using an explicit StatsCan product/coordinate query.

        Product and coordinate are provider-native execution parameters.  This
        method intentionally does not map natural-language indicator names to
        products or coordinates.

        Args:
            params: Dictionary containing:
                - productId: Exact StatsCan product/table ID
                - coordinate: Exact WDS coordinate
                - indicator: Optional display label
                - periods: Number of recent periods to fetch (default: 240)
                - startDate, endDate: Optional date range filters

        Returns:
            NormalizedData object with metadata and data points
        """
        indicator = params.get("indicator", "")
        periods = params.get("periods", 240)
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        product_id = self._normalize_metadata_product_id(params.get("productId"))
        coordinate = str(params.get("coordinate") or "").strip()
        if not product_id or not coordinate:
            raise DataNotAvailableError(
                "StatsCan coordinate fetch requires exact productId and coordinate."
            )

        description = str(params.get("indicatorLabel") or indicator or f"{product_id}:{coordinate}")
        logger.info(f"📊 Using exact coordinate query for {description}: product={product_id}, coord={coordinate}")

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )
        response.raise_for_status()
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            raise DataNotAvailableError(
                f"StatsCan coordinate query failed for {indicator} (product={product_id}, coord={coordinate})"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(f"No data found for {indicator} (product={product_id})")

        # Determine frequency and unit from first data point
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)
        frequency = self._map_frequency(freq_code)
        unit = self._get_unit_description(indicator, scalar_code)

        # Convert data points
        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date range filter if specified
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build source URL
        source_url = self._get_table_viewer_url(product_id)

        metadata = Metadata(
            source="Statistics Canada",
            indicator=f"Canadian {description}",
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id}:{coordinate}",
            apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            sourceUrl=source_url,
        )

        logger.info(f"✅ Retrieved {len(data_points)} data points for {indicator} via coordinate query")
        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_series(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch time series data from Statistics Canada.

        Args:
            params: Dictionary containing:
                - indicator: Natural-language label or exact vector ID
                - vectorId: Direct vector ID (optional, overrides indicator)
                - productId + coordinate: Direct coordinate query
                - periods: Number of recent periods to fetch (default: 120 for 10 years monthly)

        Returns:
            NormalizedData object with metadata and data points
        """
        indicator = params.get("indicator")
        indicator_key = indicator.upper().replace(" ", "_").replace("-", "_") if indicator else None

        if params.get("productId") and params.get("coordinate"):
            logger.info("🔄 Routing exact StatsCan product/coordinate request")
            return await self.fetch_by_coordinate(params)

        target_vector = await self._vector_id(
            indicator,
            params.get("vectorId")
        )
        periods = params.get("periods", 240)  # Default to 20 years of monthly data

        # Use extended timeout (300s = 5 minutes) to handle complex multi-province queries
        # StatsCan API can be slow, especially for batch coordinate queries
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        # Fetch data using the vector ID
        response = await client.post(
            f"{self.base_url}/getDataFromVectorsAndLatestNPeriods",
            json=[{"vectorId": target_vector, "latestN": periods}],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )
        self._raise_for_status_or_data_unavailable(
            response,
            f"fetching vector {target_vector}",
        )
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            raise DataNotAvailableError(f"StatsCan vector {target_vector} not found or error occurred")

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(f"No data found for vector {target_vector}")

        # Get indicator name from parameters or use vector ID
        indicator_name = params.get("indicator", f"Vector {target_vector}")
        series_title = str(indicator_name or f"Vector {target_vector}")

        # Determine frequency and unit from first data point
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)

        frequency = self._map_frequency(freq_code)

        # Determine if we should normalize units to billions (for monetary values)
        # GDP and similar monetary indicators should be in billions for readability
        should_normalize = indicator_name and any(
            term in indicator_name.upper()
            for term in ["GDP", "REVENUE", "EXPENDITURE", "DEBT", "DEFICIT", "SURPLUS"]
        )

        # Convert values if needed
        if should_normalize:
            # Convert data points to billions
            data_points = []
            target_unit = "billions"
            for point in vector_data:
                converted_value, final_unit = self._normalize_units(
                    point["value"],
                    scalar_code,
                    to_unit=target_unit,
                    indicator_name=indicator_name
                )
                data_points.append({
                    "date": point["refPer"],
                    "value": converted_value,
                })
            unit = final_unit
        else:
            # Keep original units (for percentages, indices, counts, etc.)
            unit = self._get_unit_description(indicator_name, scalar_code)
            data_points = [
                {
                    "date": point["refPer"],
                    "value": point["value"] if point["value"] is not None else None,
                }
                for point in vector_data
            ]

        # Apply date range filter if specified
        start_date = params.get("startDate")
        end_date = params.get("endDate")
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build API URL for reproducibility
        api_url = f"{self.base_url}/getDataFromVectorsAndLatestNPeriods (POST with vectorId={target_vector}, latestN={periods})"

        # Human-readable URL for data verification on Statistics Canada website
        # Try to get product ID from cache to build proper table viewer URL
        cached_product_id = self.PRODUCT_ID_CACHE.get(target_vector)
        if cached_product_id:
            # Use table viewer URL with product ID (auto-converts to 10-digit format)
            source_url = self._get_table_viewer_url(cached_product_id)
        else:
            # Fallback to StatsCan data search page
            source_url = "https://www150.statcan.gc.ca/n1/en/type/data"

        # Try to fetch detailed metadata for enhanced information
        detailed_meta = {}
        if cached_product_id:
            try:
                cube_meta = await self._get_cube_metadata(cached_product_id)
                # Get coordinate from data_object if available
                coordinate = data_object.get("coordinate")
                detailed_meta = self._extract_detailed_metadata(cube_meta, coordinate)
            except Exception as e:
                logger.warning(f"Could not fetch detailed metadata: {e}")

        # Determine scale factor from scalar code
        scale_factor = self._map_scalar_factor(scalar_code) if scalar_code else None

        metadata = Metadata(
            source="Statistics Canada",
            indicator=series_title,
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=str(target_vector),
            apiUrl=api_url,
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=detailed_meta.get("seasonalAdjustment"),
            priceType=detailed_meta.get("priceType"),
            dataType=detailed_meta.get("dataType"),
            description=detailed_meta.get("description"),
            notes=None,  # StatsCan doesn't provide detailed notes easily
            scaleFactor=scale_factor,
            startDate=detailed_meta.get("startDate") if detailed_meta.get("startDate") else (data_points[0]["date"] if data_points else None),
            endDate=detailed_meta.get("endDate") if detailed_meta.get("endDate") else (data_points[-1]["date"] if data_points else None),
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def search_vectors(
        self, keyword: str, limit: int = 10
    ) -> List[Dict[str, any]]:
        """Search for data cubes/vectors by keyword.

        This is a helper method to find vector IDs for indicators.
        Uses the WDS getAllCubesListLite endpoint to dynamically discover
        available tables without relying on provider-local semantic maps.

        Alternate search terms must be supplied by retrieval/LLM retry rather
        than provider-local semantic synonym maps.

        Args:
            keyword: Search term (e.g., "unemployment", "GDP", "employment")
            limit: Maximum number of results to return

        Returns:
            List of matching cubes with productId and titles
        """
        # Search with the user/selector-provided text only.  Do not expand with
        # curated semantic synonyms; alternate terms must come from retrieval or
        # LLM reject/search, not provider shortcut maps.
        search_terms = [keyword.lower()]

        def cube_relevance_score(cube: Dict[str, Any]) -> float:
            title = str(cube.get("title", "")).lower()
            metadata = cube.get("_metadata")
            score = 0.0

            if keyword.lower() in title:
                score += 10.0

            for term in search_terms:
                if title == term:
                    score += 14.0
                elif title.startswith(f"{term},") or title.startswith(f"{term} "):
                    score += 11.0
                elif term in title:
                    score += 7.0

            score += self._score_cube_metadata_relevance(metadata, search_terms)

            archived = cube.get("archived")
            if archived == "2":
                score += 2.0
            elif archived == "1":
                score -= 1.0

            end_date = str(cube.get("endDate", ""))
            if end_date[:4].isdigit():
                end_year = int(end_date[:4])
                if end_year >= 2024:
                    score += 2.0
                elif end_year >= 2020:
                    score += 1.0

            if "inactive" in title or "discontinued" in title:
                score -= 2.5

            if cube.get("metadataCached"):
                score += 3.0

            score -= self._title_specialization_penalty(keyword, title)

            return score

        matching: Dict[str, Dict[str, Any]] = {}

        local_catalog = getattr(self._statscan_metadata_service, "_local_cache", {})
        for product_id, metadata in local_catalog.items():
            title = str(metadata.get("cubeTitleEn", ""))
            title_lower = title.lower()
            metadata_score = self._score_cube_metadata_relevance(metadata, search_terms)
            if not any(term in title_lower for term in search_terms) and metadata_score <= 0:
                continue

            archived = "1" if "inactive" in title_lower else "2"
            matching[product_id] = {
                "productId": str(product_id),
                "title": title,
                "startDate": metadata.get("cubeStartDate"),
                "endDate": metadata.get("cubeEndDate"),
                "archived": archived,
                "frequency": metadata.get("frequencyCode"),
                "metadataCached": True,
                "_metadata": metadata,
            }

        try:
            client = get_http_client()
            logger.info(f"🔍 Searching StatsCan for: {keyword}")
            response = await client.get(
                f"{self.base_url}/getAllCubesListLite",
                timeout=30.0
            )
            response.raise_for_status()
            cubes = response.json()

            for cube in cubes:
                cube_title = cube.get("cubeTitleEn", "").lower()
                if not any(term in cube_title for term in search_terms):
                    continue

                product_id = str(cube["productId"])
                existing = matching.get(product_id)
                candidate = {
                    "productId": product_id,
                    "title": cube.get("cubeTitleEn", ""),
                    "startDate": cube.get("cubeStartDate"),
                    "endDate": cube.get("cubeEndDate"),
                    "archived": cube.get("archived", "1"),
                    "frequency": cube.get("frequencyCode"),
                    "metadataCached": bool(existing and existing.get("metadataCached")),
                    "_metadata": existing.get("_metadata") if existing else None,
                }

                if not existing or cube_relevance_score(candidate) > cube_relevance_score(existing):
                    matching[product_id] = candidate

        except Exception as e:
            if not matching:
                logger.error(f"Error searching StatsCan cubes: {e}")
                raise DataNotAvailableError(
                    f"Failed to search Statistics Canada for '{keyword}': {e}"
                )
            logger.warning(f"⚠️ Live StatsCan cube search failed, falling back to local catalog: {e}")

        ranked = sorted(matching.values(), key=cube_relevance_score, reverse=True)
        logger.info(f"✅ Found {len(ranked)} StatsCan cubes matching '{keyword}'")
        return [
            {key: value for key, value in cube.items() if not key.startswith("_")}
            for cube in ranked[:limit]
        ]

    async def fetch_categorical_data(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch categorical data using WDS coordinate-based queries.

        Dynamically discovers dimension structure from product metadata rather
        than assuming a fixed dimension order.  Dimension names are matched
        with fuzzy keywords (e.g. "sex" -> Gender, "age" -> Age group) so the
        method works for *any* product, not just the population table.

        Args:
            params: Dictionary containing:
                - productId: Product ID to query (e.g., "17100005" for population)
                - indicator: Human-readable indicator name (e.g., "Population")
                - periods: Number of recent periods to fetch (default: 20)
                - dimensions: Dict mapping dimension names to values, e.g.:
                    {
                        "geography": "Ontario",
                        "gender": "Men+",
                        "age": "25 to 29 years"
                    }
                  Any dimension can be None or omitted to use "all" (member ID 1)

        Returns:
            NormalizedData object with metadata and data points

        Raises:
            ValueError: If dimension value not recognized
            DataNotAvailableError: If data cannot be retrieved
        """
        product_id = params.get("productId", self.POPULATION_DEMOGRAPHICS_PRODUCT)
        indicator = params.get("indicator", "Population")
        display_indicator = str(params.get("indicatorLabel") or indicator or "").strip() or str(indicator or "")
        periods = params.get("periods", 20)
        dim_values = self._normalize_dimension_filters(params.get("dimensions", {}))
        indicator_lower = display_indicator.lower().replace("_", " ") if display_indicator else ""

        # Extract user-supplied dimension values
        geography = dim_values.get("geography")
        gender = dim_values.get("gender") or dim_values.get("sex")
        age = dim_values.get("age")

        # Validate geography early (rejects cities / non-Canadian countries)
        if geography:
            self._resolve_geography(geography)

        # ---------- Fetch actual dimension metadata for this product ----------
        normalized_pid = self._normalize_metadata_product_id(product_id)
        cube_metadata = await self._get_cube_metadata(normalized_pid)
        meta_dimensions = cube_metadata.get("dimension", [])

        if not meta_dimensions:
            raise DataNotAvailableError(
                f"Product {product_id} has no dimension metadata"
            )

        # ---------- Build coordinate dynamically ----------
        # For each dimension discovered in the metadata we decide the member ID
        # using keyword matching against the dimension name.
        coordinate_parts: List[int] = []
        for dim in meta_dimensions:
            dim_name_lower = dim.get("dimensionNameEn", "").lower()
            members = dim.get("member", [])

            if "geogr" in dim_name_lower:
                if geography:
                    mid = await self.resolve_member_id(normalized_pid, "geogr", geography)
                else:
                    mid = self._select_default_member_id(
                        dim.get("dimensionNameEn", ""), members, indicator_lower
                    )
                coordinate_parts.append(mid)

            elif any(kw in dim_name_lower for kw in ["gender", "sex"]):
                if gender:
                    mid = self._find_member_id_by_keywords(members, [gender])
                    if mid is None:
                        # List available members for a helpful error
                        available = [m.get("memberNameEn", "") for m in members[:15]]
                        raise ValueError(
                            f"Unknown gender/sex value: '{gender}'. "
                            f"Available: {', '.join(available)}"
                        )
                    coordinate_parts.append(mid)
                else:
                    coordinate_parts.append(
                        self._find_member_id_by_keywords(members, ["both sexes", "total"]) or 1
                    )

            elif "age" in dim_name_lower:
                if age:
                    mid = self._find_member_id_by_keywords(members, [age])
                    if mid is None:
                        available = [m.get("memberNameEn", "") for m in members[:20]]
                        raise ValueError(
                            f"Unknown age group: '{age}'. "
                            f"Available (partial): {', '.join(available)}..."
                        )
                    coordinate_parts.append(mid)
                else:
                    coordinate_parts.append(
                        self._find_member_id_by_keywords(members, ["15 years and over", "all ages", "total"]) or 1
                    )

            else:
                # Use semantic default selection for any other dimension
                coordinate_parts.append(
                    self._select_default_member_id(
                        dim.get("dimensionNameEn", ""), members, indicator_lower
                    )
                )

        # Pad to 10 dimensions
        while len(coordinate_parts) < 10:
            coordinate_parts.append(0)
        coordinate = ".".join(str(p) for p in coordinate_parts[:10])

        # Build human-readable description for logging and metadata
        description_parts = []
        if geography:
            description_parts.append(geography)
        if gender and gender.upper() not in ["TOTAL", "BOTH", "ALL", "BOTH SEXES"]:
            description_parts.append(gender)
        if age and age.upper() not in ["ALL AGES", "ALL"]:
            description_parts.append(f"aged {age}")

        description = " ".join(description_parts) if description_parts else "Canada (all categories)"

        logger.info(
            f"Fetching {indicator} for {description} "
            f"using WDS coordinate: {coordinate}"
        )

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": normalized_pid,
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )

        if response.status_code == 406:
            raise DataNotAvailableError(
                f"Invalid coordinate format for product {product_id}. "
                f"Coordinate: {coordinate}. "
                f"This may indicate the product structure has changed."
            )

        self._raise_for_status_or_data_unavailable(
            response,
            f"fetching product {product_id} coordinate {coordinate}",
        )
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            error_msg = payload[0].get("object", "Unknown error") if payload else "Empty response"
            raise DataNotAvailableError(
                f"StatsCan WDS query failed for {description}. "
                f"Error: {error_msg}"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(
                f"No data found for {description} {indicator}"
            )

        # Determine frequency and unit from first data point
        freq_code = vector_data[0].get("frequencyCode", 12)  # Default to annual
        scalar_code = vector_data[0].get("scalarFactorCode", 0)

        frequency = self._map_frequency(freq_code)
        unit = self._map_scalar_factor(scalar_code)

        # Build API URL for reproducibility
        api_url = (
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods "
            f"(POST with productId={product_id}, coordinate={coordinate}, latestN={periods})"
        )

        # Human-readable URL for data verification on Statistics Canada website
        # Use helper method to ensure 10-digit product ID format
        source_url = self._get_table_viewer_url(product_id)

        # Build indicator name
        indicator_name = (
            f"{description} {display_indicator}"
            if description != "Canada (all categories)"
            else display_indicator
        )

        # Build data points first (needed for startDate/endDate)
        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date range filter if specified
        start_date = params.get("startDate")
        end_date = params.get("endDate")
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Determine dataType from indicator name
        indicator_upper = indicator_name.upper()
        if "RATE" in indicator_upper or "PERCENT" in indicator_upper:
            data_type = "Rate"
        elif "INDEX" in indicator_upper:
            data_type = "Index"
        elif "CHANGE" in indicator_upper:
            data_type = "Percent Change"
        else:
            data_type = "Level"

        metadata = Metadata(
            source="Statistics Canada",
            indicator=indicator_name,
            country=geography or "Canada",
            frequency=frequency,
            unit=unit if unit else "persons",
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id}:{coordinate}",
            apiUrl=api_url,
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=None,  # Not available in categorical queries without metadata
            dataType=data_type,
            priceType=None,  # Not typically available for categorical queries
            description=indicator_name,
            notes=None,  # StatsCan doesn't provide detailed notes easily
            scaleFactor=self._map_scalar_factor(scalar_code) if scalar_code else None,
            startDate=data_points[0]["date"] if data_points else None,
            endDate=data_points[-1]["date"] if data_points else None,
        )

        return NormalizedData(metadata=metadata, data=data_points)

    def _metadata_from_full_table_csv_rows(
        self,
        product_id: str,
        rows: List[Dict[str, str]],
        metadata_rows: List[List[str]],
    ) -> Dict[str, Any]:
        """Build cube-style dimension metadata from a StatsCan full-table CSV zip.

        This is a generic provider-native fallback for archived/slow WDS
        ``getCubeMetadata`` calls.  Statistics Canada's public full-table CSV
        bundles include both data rows and a ``*_MetaData.csv`` member with
        dimension/member IDs; use that metadata instead of hardcoded product
        coordinates.
        """
        title = ""
        start_period = ""
        end_period = ""
        frequency = ""
        dimensions: Dict[int, Dict[str, Any]] = {}

        for index, record in enumerate(metadata_rows):
            if not record:
                continue
            if record[0] == "Cube Title" and index + 1 < len(metadata_rows):
                values = metadata_rows[index + 1]
                title = values[0] if len(values) > 0 else title
                frequency = values[6] if len(values) > 6 else frequency
                start_period = values[7] if len(values) > 7 else start_period
                end_period = values[8] if len(values) > 8 else end_period
            if len(record) > 1 and record[0] == "Dimension ID" and record[1] == "Dimension name":
                next_index = index + 1
                while next_index < len(metadata_rows):
                    dim_row = metadata_rows[next_index]
                    next_index += 1
                    if not dim_row:
                        continue
                    if dim_row[0] == "Dimension ID":
                        break
                    try:
                        dim_id = int(str(dim_row[0]).strip())
                    except (TypeError, ValueError):
                        continue
                    dimensions.setdefault(
                        dim_id,
                        {
                            "dimensionPositionId": dim_id,
                            "dimensionNameEn": str(dim_row[1] if len(dim_row) > 1 else "").strip(),
                            "member": [],
                        },
                    )
            if len(record) > 3 and record[0] != "Dimension ID":
                try:
                    dim_id = int(str(record[0]).strip())
                    member_id = int(str(record[3]).strip())
                except (TypeError, ValueError):
                    continue
                dim = dimensions.setdefault(
                    dim_id,
                    {
                        "dimensionPositionId": dim_id,
                        "dimensionNameEn": "",
                        "member": [],
                    },
                )
                if any(member.get("memberId") == member_id for member in dim["member"]):
                    continue
                parent = str(record[4]).strip() if len(record) > 4 else ""
                member: Dict[str, Any] = {
                    "memberId": member_id,
                    "memberNameEn": str(record[1] if len(record) > 1 else "").strip(),
                }
                if parent:
                    try:
                        member["parentMemberId"] = int(parent)
                    except ValueError:
                        member["parentMemberId"] = parent
                dim["member"].append(member)

        if (not dimensions or any(not dim.get("member") for dim in dimensions.values())) and rows:
            dim_columns = [
                col
                for col in rows[0]
                if col
                and col not in {
                    "REF_DATE", "GEO", "DGUID", "UOM", "UOM_ID", "SCALAR_FACTOR", "SCALAR_ID",
                    "VECTOR", "COORDINATE", "Coordinate", "VALUE", "STATUS", "SYMBOL", "TERMINATED", "DECIMALS",
                }
                and not str(col).startswith("Unnamed")
            ]
            for pos, column in enumerate(dim_columns, start=1):
                seen_names: Dict[str, int] = {}
                members: List[Dict[str, Any]] = []
                for row in rows:
                    name = str(row.get(column) or "").strip()
                    if not name or name in seen_names:
                        continue
                    member_id = len(seen_names) + 1
                    seen_names[name] = member_id
                    members.append({"memberId": member_id, "memberNameEn": name})
                dimensions.setdefault(
                    pos,
                    {
                        "dimensionPositionId": pos,
                        "dimensionNameEn": column,
                        "member": members,
                    },
                )

        return {
            "_source": "full_table_csv",
            "productId": self._normalize_metadata_product_id(product_id),
            "cubeTitleEn": title,
            "frequencyCode": self._frequency_code_from_text(frequency),
            "cubeStartDate": start_period,
            "cubeEndDate": end_period,
            "dimension": [dimensions[key] for key in sorted(dimensions)],
        }

    @staticmethod
    def _frequency_code_from_text(frequency: str) -> int:
        frequency_lower = str(frequency or "").lower()
        if "daily" in frequency_lower:
            return 1
        if "week" in frequency_lower:
            return 3
        if "month" in frequency_lower:
            return 6
        if "quarter" in frequency_lower:
            return 9
        return 12

    async def _download_full_table_csv_bundle(
        self,
        product_id: str,
    ) -> tuple[List[Dict[str, str]], List[List[str]], str]:
        normalized_product_id = self._normalize_metadata_product_id(product_id)
        if not normalized_product_id:
            raise DataNotAvailableError(f"Invalid Statistics Canada product ID: {product_id}")

        url = f"https://www150.statcan.gc.ca/n1/tbl/csv/{normalized_product_id}-eng.zip"
        client = get_http_client()
        if hasattr(client, "stream"):
            content = bytearray()
            async with client.stream("GET", url, timeout=30.0, follow_redirects=True) as response:
                self._raise_for_status_or_data_unavailable(
                    response,
                    f"downloading full-table CSV for product {normalized_product_id}",
                )
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > self.FULL_TABLE_CSV_MAX_BYTES:
                            raise DataNotAvailableError(
                                "Statistics Canada full-table CSV bundle exceeds the safe exact-table fallback size"
                            )
                    except ValueError:
                        pass
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.FULL_TABLE_CSV_MAX_BYTES:
                        raise DataNotAvailableError(
                            "Statistics Canada full-table CSV bundle exceeds the safe exact-table fallback size"
                        )
            response_content = bytes(content)
        else:
            # Test doubles and older clients may only expose get().
            response = await client.get(url, timeout=30.0, follow_redirects=True)
            self._raise_for_status_or_data_unavailable(
                response,
                f"downloading full-table CSV for product {normalized_product_id}",
            )
            response_content = response.content
            if len(response_content) > self.FULL_TABLE_CSV_MAX_BYTES:
                raise DataNotAvailableError(
                    "Statistics Canada full-table CSV bundle exceeds the safe exact-table fallback size"
                )
        try:
            zip_bytes = io.BytesIO(response_content)
            with zipfile.ZipFile(zip_bytes) as archive:
                data_name = next(
                    name for name in archive.namelist()
                    if name.endswith(".csv") and "MetaData" not in name
                )
                metadata_name = next(
                    name for name in archive.namelist()
                    if name.endswith(".csv") and "MetaData" in name
                )
                uncompressed_total = 0
                for name in (data_name, metadata_name):
                    uncompressed_total += archive.getinfo(name).file_size
                    if uncompressed_total > self.FULL_TABLE_CSV_MAX_BYTES:
                        raise DataNotAvailableError(
                            "Statistics Canada full-table CSV bundle exceeds the safe exact-table fallback size"
                        )
                with archive.open(data_name) as handle:
                    data_reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))
                    rows = [dict(row) for row in data_reader]
                with archive.open(metadata_name) as handle:
                    metadata_reader = csv.reader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))
                    metadata_rows = [list(row) for row in metadata_reader]
                return rows, metadata_rows, url
        except Exception as exc:
            raise DataNotAvailableError(
                f"Could not parse Statistics Canada full-table CSV for product {normalized_product_id}: {exc}"
            ) from exc

    async def _get_full_table_csv_rows(
        self,
        product_id: str,
    ) -> tuple[List[Dict[str, str]], str]:
        rows, metadata_rows, url = await self._download_full_table_csv_bundle(product_id)
        normalized_product_id = self._normalize_metadata_product_id(product_id)
        if normalized_product_id not in self._cube_metadata_cache:
            metadata = self._metadata_from_full_table_csv_rows(normalized_product_id, rows, metadata_rows)
            if metadata.get("dimension"):
                self._cube_metadata_cache[normalized_product_id] = metadata
        return rows, url

    async def _get_cube_metadata_via_full_table_csv(self, product_id: str) -> Dict[str, any]:
        normalized_product_id = self._normalize_metadata_product_id(product_id)
        rows, metadata_rows, _url = await self._download_full_table_csv_bundle(normalized_product_id)
        metadata = self._metadata_from_full_table_csv_rows(normalized_product_id, rows, metadata_rows)
        if not metadata.get("dimension"):
            raise DataNotAvailableError(
                f"Full-table CSV metadata for Statistics Canada product {normalized_product_id} has no dimensions"
            )
        self._cube_metadata_cache[normalized_product_id] = metadata
        logger.info("💾 Built local cube metadata for product %s from full-table CSV", normalized_product_id)
        return metadata

    def _data_value_column(self, row: Dict[str, str]) -> Optional[str]:
        for key in ("VALUE", "Value", "value"):
            if key in row:
                return key
        metadata_columns = {
            "REF_DATE", "GEO", "DGUID", "UOM", "UOM_ID", "SCALAR_FACTOR", "SCALAR_ID",
            "VECTOR", "COORDINATE", "Coordinate", "STATUS", "SYMBOL", "TERMINATED", "DECIMALS",
        }
        candidates = [key for key in row if key and key not in metadata_columns]
        for key in reversed(candidates):
            if self._parse_statscan_value(row.get(key)) is not None:
                return key
        return None

    @staticmethod
    def _parse_statscan_value(value: Any) -> Optional[float]:
        text = str(value or "").strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _select_full_table_rows(
        self,
        rows: List[Dict[str, str]],
        *,
        metadata: Dict[str, Any],
        indicator: str,
        geography: Optional[str],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> tuple[List[Dict[str, str]], str, Optional[str]]:
        if not rows:
            raise DataNotAvailableError("Statistics Canada full-table CSV contains no rows")

        value_column = self._data_value_column(rows[0])
        if not value_column:
            raise DataNotAvailableError("Statistics Canada full-table CSV has no value column")

        selected_coordinate = ""

        def value_is_present(row: Dict[str, str]) -> bool:
            return self._parse_statscan_value(row.get(value_column)) is not None

        coordinate_key = "COORDINATE" if "COORDINATE" in rows[0] else "Coordinate" if "Coordinate" in rows[0] else ""
        series_key = coordinate_key or ("VECTOR" if "VECTOR" in rows[0] else "")
        candidate_rows = [row for row in rows if value_is_present(row)]

        if geography:
            geography_lower = str(geography).strip().lower()
            geography_lower = {
                "ca": "canada",
                "can": "canada",
            }.get(geography_lower, geography_lower)
            candidate_rows = [
                row for row in candidate_rows
                if str(row.get("GEO") or "").lower() == geography_lower
                or str(row.get("GEO") or "").lower().startswith(f"{geography_lower},")
            ]

        if start_date or end_date:
            filtered_points = self._filter_by_date_range(
                [
                    {"date": str(row.get("REF_DATE") or ""), "value": row}
                    for row in candidate_rows
                ],
                start_date,
                end_date,
            )
            candidate_rows = [item["value"] for item in filtered_points]

        if not candidate_rows:
            raise DataNotAvailableError("No data rows found in Statistics Canada full-table CSV")

        # Exact table-title fallback must not use semantic keyword/member
        # selection.  Use the first provider-published coordinate/vector with
        # data, then keep only rows for that same provider-native series.  This
        # is deterministic file-order transport from the public table bundle,
        # not concept-to-code inference from query words.
        first_row = candidate_rows[0]
        selected_coordinate = str(first_row.get(series_key) or "").strip() if series_key else ""
        if series_key and selected_coordinate:
            matching_rows = [
                row for row in candidate_rows
                if str(row.get(series_key) or "").strip() == selected_coordinate
            ]
        else:
            matching_rows = candidate_rows
            selected_coordinate = value_column if not coordinate_key else selected_coordinate

        if not matching_rows:
            raise DataNotAvailableError("No data rows found in Statistics Canada full-table CSV")

        matching_rows.sort(key=lambda row: str(row.get("REF_DATE") or ""))
        return matching_rows, selected_coordinate, value_column

    async def fetch_full_table_csv_data(
        self,
        params: Dict[str, any],
    ) -> NormalizedData:
        product_id = self._normalize_metadata_product_id(params.get("productId") or params.get("indicator") or "")
        if not product_id:
            raise DataNotAvailableError("Statistics Canada full-table CSV fetch requires a product ID")
        display_indicator = str(params.get("indicatorLabel") or params.get("indicator") or product_id).strip() or product_id
        geography = params.get("geography")
        start_date = params.get("startDate")
        end_date = params.get("endDate")
        rows, csv_url = await self._get_full_table_csv_rows(product_id)
        metadata = self._cube_metadata_cache.get(product_id) or {}
        selected_rows, coordinate, value_column = self._select_full_table_rows(
            rows,
            metadata=metadata,
            indicator=display_indicator,
            geography=geography,
            start_date=start_date,
            end_date=end_date,
        )
        periods = params.get("periods")
        if periods is not None:
            try:
                selected_rows = selected_rows[-int(periods):]
            except (TypeError, ValueError):
                pass

        data_points = [
            {
                "date": str(row.get("REF_DATE") or ""),
                "value": self._parse_statscan_value(row.get(value_column)),
            }
            for row in selected_rows
        ]
        first = selected_rows[0]
        coordinate = coordinate or str(first.get("COORDINATE") or first.get("Coordinate") or "")
        frequency_text = str(metadata.get("frequency") or "")
        frequency_code = metadata.get("frequencyCode") or self._frequency_code_from_text(frequency_text)
        frequency = self._map_frequency(int(frequency_code) if str(frequency_code).isdigit() else 12)
        unit = str(first.get("UOM") or self._map_scalar_factor(int(str(first.get("SCALAR_ID") or 0) or 0)) or "units").strip()
        country = str(first.get("GEO") or geography or "Canada").strip() or "Canada"
        source_url = self._get_table_viewer_url(product_id)
        api_url = f"{csv_url} (direct full-table CSV fallback)"
        series_id = f"{product_id}:{coordinate}" if coordinate else product_id
        return NormalizedData(
            metadata=Metadata(
                source="Statistics Canada",
                indicator=display_indicator,
                country=country,
                frequency=frequency,
                unit=unit,
                lastUpdated="",
                seriesId=series_id,
                apiUrl=api_url,
                sourceUrl=source_url,
                description=display_indicator,
                dataType="Rate" if str(unit).lower() == "percent" or "percent" in display_indicator.lower() else "Level",
                scaleFactor=str(first.get("SCALAR_FACTOR") or "").strip() or None,
                startDate=data_points[0]["date"] if data_points else None,
                endDate=data_points[-1]["date"] if data_points else None,
            ),
            data=data_points,
        )

    async def fetch_dynamic_data(
        self, params: Dict[str, any]
    ) -> NormalizedData:
        """Fetch data dynamically using WDS metadata discovery.

        This is the main method for handling queries that don't match hardcoded
        vector IDs. It:
        1. Uses metadata search to find the right cube
        2. Gets cube metadata to understand dimensions
        3. Routes to appropriate fetch method based on structure

        Args:
            params: Dictionary containing:
                - indicator: Indicator name (e.g., "employment", "retail sales")
                - geography: Optional province/territory
                - period: Optional number of periods (default: 240 for 20 years)

        Returns:
            NormalizedData with the requested data
        """
        indicator = params.get("indicator", "")
        display_indicator = str(params.get("indicatorLabel") or indicator or "").strip() or str(indicator or "")
        geography = params.get("geography")
        periods = params.get("periods", 240)
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        # Validate geography if provided
        if geography:
            # This will raise ValueError for cities or non-Canadian countries
            self._resolve_geography(geography)

        logger.info(f"📡 Using dynamic metadata discovery for: {indicator} (geography: {geography})")

        # Normalize indicator name: convert "RETAIL_SALES" → "retail sales"
        # This ensures the search works with StatsCan's actual cube titles
        search_term = display_indicator.lower().replace("_", " ")
        logger.info(f"📊 Search term: '{search_term}' (normalized from '{indicator}')")

        exact_product_id = self._normalize_metadata_product_id(indicator)
        indicator_digits = "".join(ch for ch in str(indicator or "") if ch.isdigit())
        if exact_product_id and indicator_digits and len(indicator_digits) in {8, 10}:
            logger.info(f"📦 Treating {indicator} as an exact Statistics Canada product ID")
            if params.get("__exact_indicator_title_match") and not geography:
                try:
                    return await self.fetch_full_table_csv_data({
                        **params,
                        "productId": exact_product_id,
                        "indicatorLabel": display_indicator,
                        "periods": periods,
                        "startDate": start_date,
                        "endDate": end_date,
                    })
                except DataNotAvailableError as exc:
                    logger.warning(
                        "Exact StatsCan product %s full-table CSV path failed; trying WDS coordinate path: %s",
                        exact_product_id,
                        exc,
                    )
            try:
                metadata = await self._get_cube_metadata(exact_product_id)
                if metadata.get("_source") == "full_table_csv" and not geography:
                    return await self.fetch_full_table_csv_data({
                        **params,
                        "productId": exact_product_id,
                        "indicatorLabel": display_indicator,
                        "periods": periods,
                        "startDate": start_date,
                        "endDate": end_date,
                    })
                return await self.fetch_from_product_with_discovery(
                    product_id=exact_product_id,
                    indicator=display_indicator,
                    metadata=metadata,
                    geography=geography,
                    periods=periods,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as exc:
                logger.warning(
                    "Exact StatsCan product %s WDS coordinate path failed; trying full-table CSV fallback: %s",
                    exact_product_id,
                    exc,
                )
                return await self.fetch_full_table_csv_data({
                    **params,
                    "productId": exact_product_id,
                    "indicatorLabel": display_indicator,
                    "geography": geography,
                    "periods": periods,
                    "startDate": start_date,
                    "endDate": end_date,
                })

        # Step 1: Search for matching cubes
        matching_cubes = await self.search_vectors(search_term, limit=5)

        if not matching_cubes:
            raise DataNotAvailableError(
                f"No Statistics Canada data found for '{indicator}'. "
                f"Try a different indicator or use a different provider."
            )

        # Step 2: Try each matching cube until one works
        for cube in matching_cubes:
            product_id = cube.get("productId")
            cube_title = cube.get("title", "Unknown")

            try:
                logger.info(f"🔄 Trying {product_id}: {cube_title}")

                # Fetch metadata to understand structure
                metadata = await self._get_cube_metadata(product_id)

                # Determine which fetch method to use based on structure
                dimensions = metadata.get("dimension", [])
                has_geography = any(
                    "geogr" in d.get("dimensionNameEn", "").lower()
                    for d in dimensions
                )

                if geography and has_geography:
                    # Has geography dimension - use coordinate method
                    logger.info(f"✅ Using coordinate method for {product_id}")
                    result = await self.fetch_from_product_with_discovery(
                        product_id=product_id,
                        indicator=indicator,
                        metadata=metadata,
                        geography=geography,
                        periods=periods,
                        start_date=start_date,
                        end_date=end_date
                    )
                    return result
                elif geography and not has_geography:
                    # User requested geography but cube doesn't have it
                    logger.warning(f"⚠️ Product {product_id} doesn't have geography dimension")
                    continue
                else:
                    # No geography requested - use vector/simple method
                    logger.info(f"✅ Using vector method for {product_id}")
                    result = await self.fetch_from_product_with_discovery(
                        product_id=product_id,
                        indicator=indicator,
                        metadata=metadata,
                        geography=geography,
                        periods=periods,
                        start_date=start_date,
                        end_date=end_date
                    )
                    return result

            except Exception as e:
                logger.warning(f"⚠️ Product {product_id} failed: {e}")
                continue

        # All cubes failed
        raise DataNotAvailableError(
            f"Unable to retrieve data for '{indicator}' from Statistics Canada. "
            f"All matching datasets failed. Try a different indicator or provider."
        )

    async def fetch_from_product_with_discovery(
        self,
        product_id: str,
        indicator: str,
        metadata: Dict[str, any],
        geography: Optional[str],
        periods: int = 240,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> NormalizedData:
        """Fetch data from a specific product using discovered metadata.

        Args:
            product_id: Product ID to fetch from
            indicator: Indicator name for metadata
            metadata: Cube metadata from getCubeMetadata
            geography: Optional province/territory name
            periods: Number of periods to fetch
            start_date: Optional start date filter (ISO format)
            end_date: Optional end date filter (ISO format)

        Returns:
            NormalizedData with the results
        """
        # Validate geography if provided
        if geography:
            # This will raise ValueError for cities or non-Canadian countries
            self._resolve_geography(geography)

        # Extract available dimensions from metadata
        dimensions = metadata.get("dimension", [])
        indicator_lower = indicator.lower().replace("_", " ") if indicator else ""

        # Build coordinate by finding member IDs for each dimension
        # Use intelligent dimension matching based on indicator context
        coordinate_parts = []
        coordinate_candidate_parts: List[List[int]] = []
        for dim_info in dimensions:
            dim_name = dim_info.get("dimensionNameEn", "").upper()
            dim_name_lower = dim_name.lower()
            members = dim_info.get("member", [])

            def append_coordinate_part(member_id: int) -> None:
                coordinate_parts.append(member_id)
                coordinate_candidate_parts.append(
                    self._coordinate_member_candidates(
                        dim_name,
                        members,
                        indicator_lower,
                        member_id,
                    )
                )

            # Helper: Find best member by matching keywords
            def find_member_by_keywords(keywords: list) -> int | None:
                for kw in keywords:
                    for member in members:
                        member_name = member.get("memberNameEn", "").lower()
                        if kw.lower() in member_name:
                            return member.get("memberId")
                return None

            # 1. Geography dimension - match to specified geography or default to Canada
            if "geogr" in dim_name_lower:
                if geography:
                    geography_upper = geography.upper()
                    found_id = None
                    # Direct lookup
                    for member in members:
                        member_name = member.get("memberNameEn", "").upper()
                        if geography_upper == member_name or member_name.startswith(geography_upper):
                            found_id = member.get("memberId")
                            break
                    # Alias lookup
                    if not found_id:
                        canonical = self.GEOGRAPHY_ALIASES.get(geography_upper)
                        if canonical:
                            for member in members:
                                if canonical.upper() in member.get("memberNameEn", "").upper():
                                    found_id = member.get("memberId")
                                    break
                    append_coordinate_part(
                        found_id
                        if found_id
                        else self._select_default_member_id(dim_name, members, indicator_lower)
                    )
                else:
                    append_coordinate_part(
                        self._select_default_member_id(dim_name, members, indicator_lower)
                    )

            # 2. Trade dimension - match based on indicator (balance, export, import)
            elif "trade" in dim_name_lower:
                if "balance" in indicator_lower:
                    found = find_member_by_keywords(["balance", "net"])
                elif "export" in indicator_lower:
                    found = find_member_by_keywords(["export", "exports"])
                elif "import" in indicator_lower:
                    found = find_member_by_keywords(["import", "imports"])
                else:
                    found = None
                append_coordinate_part(found if found else 1)

            # 3. Component dimension (immigration, migration) - match based on indicator
            elif "component" in dim_name_lower:
                if any(x in indicator_lower for x in ["immigra", "immigrant", "permanent"]):
                    found = find_member_by_keywords(["immigrants", "immigration", "permanent"])
                elif "emigra" in indicator_lower:
                    found = find_member_by_keywords(["emigrants", "emigration"])
                else:
                    found = None
                append_coordinate_part(found if found else 1)

            # 4. Adjustment dimension - prefer seasonally adjusted
            elif any(x in dim_name_lower for x in ["seasonal", "adjustment"]):
                found = find_member_by_keywords(["seasonally adjusted", "adjusted"])
                append_coordinate_part(found if found else 1)

            # 5. Basis dimension - prefer balance of payments for trade data
            elif "basis" in dim_name_lower:
                found = find_member_by_keywords(["balance of payments", "bop"])
                append_coordinate_part(found if found else 1)

            # 6. Default to the best semantic aggregate rather than blindly taking member 1
            else:
                append_coordinate_part(
                    self._select_default_member_id(dim_name, members, indicator_lower)
                )

        coordinate = self._build_wds_coordinate(coordinate_parts)

        logger.info(f"📊 Fetching {product_id} with coordinate: {coordinate}")

        # Fetch data using coordinate
        # Use extended timeout (300s = 5 minutes) to handle complex multi-province queries
        # StatsCan API can be slow, especially for batch coordinate queries
        # Use shared HTTP client pool for better performance
        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )

        self._raise_for_status_or_data_unavailable(
            response,
            f"fetching product {product_id} coordinate {coordinate}",
        )
        payload = response.json()

        def successful_data_object(items: Any) -> Optional[Dict[str, Any]]:
            for item in items or []:
                if item.get("status") != "SUCCESS":
                    continue
                item_object = item.get("object", {})
                if not isinstance(item_object, dict):
                    continue
                if item_object.get("vectorDataPoint"):
                    return item_object
            return None

        data_object = successful_data_object(payload)
        if not data_object:
            error_msg = payload[0].get("object", "Unknown error") if payload else "Empty response"

            # If the semantic default coordinate is invalid, probe nearby
            # metadata-derived coordinates in one bounded batch and use the
            # first provider-successful series.  This fixes generic direct
            # table-title queries for products that have no aggregate member.
            fallback_coordinates: List[str] = []
            if coordinate_candidate_parts:
                def expand_candidates(index: int, parts: List[int]) -> None:
                    if len(fallback_coordinates) >= 48:
                        return
                    if index >= len(coordinate_candidate_parts):
                        candidate_coordinate = self._build_wds_coordinate(parts)
                        if candidate_coordinate != coordinate and candidate_coordinate not in fallback_coordinates:
                            fallback_coordinates.append(candidate_coordinate)
                        return
                    for member_id in coordinate_candidate_parts[index]:
                        expand_candidates(index + 1, [*parts, member_id])
                        if len(fallback_coordinates) >= 48:
                            break

                expand_candidates(0, [])

            if fallback_coordinates:
                logger.info(
                    "StatsCan coordinate %s failed for %s; probing %s metadata-derived alternatives",
                    coordinate,
                    product_id,
                    len(fallback_coordinates),
                )
                fallback_response = await client.post(
                    f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
                    json=[
                        {
                            "productId": product_id,
                            "coordinate": fallback_coordinate,
                            "latestN": periods,
                        }
                        for fallback_coordinate in fallback_coordinates
                    ],
                    headers={"Content-Type": "application/json"},
                    timeout=300.0,
                )
                self._raise_for_status_or_data_unavailable(
                    fallback_response,
                    f"probing fallback coordinates for product {product_id}",
                )
                fallback_payload = fallback_response.json()
                data_object = successful_data_object(fallback_payload)
                if data_object:
                    coordinate = str(data_object.get("coordinate") or "").strip() or coordinate
                    logger.info(
                        "✅ StatsCan fallback coordinate selected for %s: %s",
                        product_id,
                        coordinate,
                    )

            if not data_object:
                raise DataNotAvailableError(
                    f"StatsCan query failed for {indicator}: {error_msg}"
                )

        vector_data = data_object.get("vectorDataPoint", [])

        if not vector_data:
            raise DataNotAvailableError(
                f"No data found for {indicator} from Statistics Canada"
            )

        # Extract metadata from response
        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)

        frequency = self._map_frequency(freq_code)
        unit = self._map_scalar_factor(scalar_code) or "units"

        # Build data points
        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        # Apply date range filter if specified
        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build full indicator name
        indicator_name = indicator
        if geography:
            indicator_name = f"{geography} {indicator}"

        # Human-readable URL for data verification on Statistics Canada website
        # Use helper method to ensure 10-digit product ID format
        source_url = self._get_table_viewer_url(product_id)

        # Determine dataType from indicator name
        indicator_upper = indicator_name.upper()
        if "RATE" in indicator_upper or "PERCENT" in indicator_upper:
            data_type = "Rate"
        elif "INDEX" in indicator_upper:
            data_type = "Index"
        elif "CHANGE" in indicator_upper:
            data_type = "Percent Change"
        else:
            data_type = "Level"

        metadata_obj = Metadata(
            source="Statistics Canada",
            indicator=indicator_name,
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id}:{coordinate}",
            apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            sourceUrl=source_url,
            # Enhanced metadata fields
            seasonalAdjustment=None,  # Not available in dynamic discovery without metadata
            dataType=data_type,
            priceType=None,  # Not typically available in dynamic discovery
            description=indicator_name,
            notes=None,  # StatsCan doesn't provide detailed notes easily
            scaleFactor=self._map_scalar_factor(scalar_code) if scalar_code else None,
            startDate=data_points[0]["date"] if data_points else None,
            endDate=data_points[-1]["date"] if data_points else None,
        )

        return NormalizedData(metadata=metadata_obj, data=data_points)

    # ------------------------------------------------------------------
    # Dimension-modifier-aware fetch
    # ------------------------------------------------------------------

    async def fetch_with_dimensions(
        self,
        base_indicator: str,
        modifiers: Dict[str, str],
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        periods: int = 240,
        indicator_label: Optional[str] = None,
        product_id: Optional[str] = None,
    ) -> NormalizedData:
        """Fetch data with specific dimension values discovered from table metadata.

        This is the GENERAL mechanism for handling dimension modifiers like
        "male", "youth", "Ontario", "food" etc.  No modifier list is hardcoded;
        instead, the table's own metadata is fetched and each dimension's member
        list is searched for a match.

        Args:
            base_indicator: Display label or exact StatsCan product/vector ID.
            modifiers: Mapping of *dimension hint* -> *search term*.
                       The hint is a loose keyword used to identify the right
                       dimension (e.g., "geography", "sex", "age", "product",
                       "industry").  The search term is the user-facing value
                       (e.g., "Ontario", "male", "youth", "food").
                       If a hint does not match any dimension name the entry is
                       silently ignored.
            start_year: Optional start year for date filtering.
            end_year: Optional end year for date filtering.
            periods: Number of most-recent periods to request.

        Returns:
            NormalizedData with the dimension-filtered data.
        """
        # 1. Resolve product ID from exact upstream evidence only.
        resolved_product_id: Optional[str] = self._normalize_metadata_product_id(product_id)
        if not resolved_product_id and str(base_indicator or "").strip().isdigit():
            candidate = str(base_indicator).strip()
            if len(candidate) in {8, 10}:
                resolved_product_id = self._normalize_metadata_product_id(candidate)
            else:
                vector_id = int(candidate)
                cached_pid = self.PRODUCT_ID_CACHE.get(vector_id)
                if cached_pid:
                    resolved_product_id = self._normalize_metadata_product_id(cached_pid)
                else:
                    try:
                        resolved_product_id = self._normalize_metadata_product_id(
                            await self._get_product_id_from_vector(vector_id)
                        )
                    except Exception:
                        resolved_product_id = None
        if not resolved_product_id:
            raise DataNotAvailableError(
                f"Cannot determine Statistics Canada product for '{base_indicator}'. "
                f"Dimension modifiers require an exact productId/table ID or vectorId "
                f"selected upstream by metadata/LLM evidence."
            )
        product_id = resolved_product_id

        # 2. Fetch metadata for the product
        metadata = await self._get_cube_metadata(product_id)
        dimensions = metadata.get("dimension", [])
        if not dimensions:
            raise DataNotAvailableError(
                f"Product {product_id} has no dimension metadata"
            )

        indicator_lower = base_indicator.lower().replace("_", " ")

        # 3. Build coordinate by matching modifiers to dimensions
        coordinate_parts: list[int] = []
        matched_descriptions: list[str] = []

        for dim in dimensions:
            dim_name = dim.get("dimensionNameEn", "")
            dim_name_lower = dim_name.lower()
            members = dim.get("member", [])

            # Try to find a modifier whose hint matches this dimension
            matched = False
            for hint, search_term in modifiers.items():
                hint_lower = hint.lower()
                # Flexible matching: the hint is a substring of the dimension name
                # e.g., "geogr" matches "Geography", "sex"/"gender" matches "Sex"
                if hint_lower in dim_name_lower or dim_name_lower in hint_lower:
                    mid = self._find_member_id_by_keywords(members, [search_term])
                    if mid is not None:
                        coordinate_parts.append(mid)
                        # Find the member name for description
                        member_name = search_term
                        for m in members:
                            if m.get("memberId") == mid:
                                member_name = m.get("memberNameEn", search_term)
                                break
                        matched_descriptions.append(member_name)
                        matched = True
                        break

            if not matched:
                # Use semantic default
                coordinate_parts.append(
                    self._select_default_member_id(dim_name, members, indicator_lower)
                )

        # Pad to 10 dimensions
        while len(coordinate_parts) < 10:
            coordinate_parts.append(0)
        coordinate = ".".join(str(p) for p in coordinate_parts[:10])

        logger.info(
            f"📊 fetch_with_dimensions: {base_indicator} modifiers={modifiers} "
            f"-> product={product_id} coordinate={coordinate}"
        )

        # 4. Fetch data
        start_date = f"{start_year}-01-01" if start_year else None
        end_date = f"{end_year}-12-31" if end_year else None

        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=[{
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods,
            }],
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )
        response.raise_for_status()
        payload = response.json()

        if not payload or payload[0].get("status") != "SUCCESS":
            error_msg = payload[0].get("object", "Unknown error") if payload else "Empty response"
            raise DataNotAvailableError(
                f"StatsCan query failed for {base_indicator} with modifiers {modifiers}: {error_msg}"
            )

        data_object = payload[0]["object"]
        vector_data = data_object.get("vectorDataPoint", [])
        if not vector_data:
            raise DataNotAvailableError(
                f"No data found for {base_indicator} with modifiers {modifiers}"
            )

        freq_code = vector_data[0].get("frequencyCode", 6)
        scalar_code = vector_data[0].get("scalarFactorCode", 0)
        frequency = self._map_frequency(freq_code)
        unit = self._get_unit_description(base_indicator, scalar_code)

        data_points = [
            {
                "date": point["refPer"],
                "value": point["value"] if point["value"] is not None else None,
            }
            for point in vector_data
        ]

        if start_date or end_date:
            data_points = self._filter_by_date_range(data_points, start_date, end_date)

        # Build descriptive indicator name
        semantic_label = str(indicator_label or "").strip() or base_indicator.replace("_", " ").title()
        desc = ", ".join(matched_descriptions) if matched_descriptions else ""
        indicator_name = f"Canadian {semantic_label}"
        if desc:
            indicator_name = f"{indicator_name} - {desc}"

        detailed_meta = self._extract_detailed_metadata(metadata, coordinate)
        source_url = self._get_table_viewer_url(product_id)

        indicator_upper = indicator_name.upper()
        if "RATE" in indicator_upper or "PERCENT" in indicator_upper:
            data_type = "Rate"
        elif "INDEX" in indicator_upper:
            data_type = "Index"
        else:
            data_type = detailed_meta.get("dataType", "Level")

        metadata_obj = Metadata(
            source="Statistics Canada",
            indicator=indicator_name,
            country="Canada",
            frequency=frequency,
            unit=unit,
            lastUpdated=vector_data[-1].get("releaseTime", "") if vector_data else "",
            seriesId=f"{product_id}:{coordinate}",
            apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            sourceUrl=source_url,
            seasonalAdjustment=detailed_meta.get("seasonalAdjustment"),
            priceType=detailed_meta.get("priceType"),
            dataType=data_type,
            description=detailed_meta.get("description"),
            scaleFactor=self._map_scalar_factor(scalar_code) if scalar_code else None,
            startDate=data_points[0]["date"] if data_points else None,
            endDate=data_points[-1]["date"] if data_points else None,
        )

        return NormalizedData(metadata=metadata_obj, data=data_points)

    def extract_dimension_modifiers(
        self,
        query_text: str,
        base_indicator: str,
        product_id: Optional[str],
        cube_metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, str]:
        """Extract dimension modifiers from query text by matching against actual table metadata.

        This is the GENERAL, non-hardcoded approach: we look at the table's
        dimensions and their member names, then check whether any word/phrase
        in the user's query matches a non-default member.

        Args:
            query_text: The raw user query (e.g., "unemployment rate male Ontario").
            base_indicator: The resolved base indicator key (e.g., "UNEMPLOYMENT_RATE").
            product_id: The StatsCan product ID (8-digit), or None.
            cube_metadata: Pre-fetched cube metadata, or None (will skip if not available).

        Returns:
            Dict of dimension_hint -> matched_term, e.g.,
            {"geography": "Ontario", "sex": "male"}.
            Empty dict if no modifiers detected.
        """
        if not cube_metadata or not query_text:
            return {}

        def normalized_words(text: str) -> set[str]:
            return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))

        query_lower = query_text.lower()
        query_lower = re.sub(r"(\d+)\s*-\s*(\d+)", r"\1 to \2", query_lower)
        # Remove the base indicator words from the query to avoid false positives
        indicator_words = normalized_words(base_indicator.replace("_", " "))

        # Also remove common filler words
        filler_words = {
            "show", "me", "the", "for", "in", "of", "and", "a", "an",
            "data", "canada", "canadian", "statistics", "statscan",
            "rate", "index", "level", "last", "years", "year", "from",
            "to", "since", "until", "recent", "latest", "please",
            "get", "fetch", "what", "is", "are", "was", "were",
        }
        noise_words = indicator_words | filler_words

        modifiers: Dict[str, str] = {}
        dimensions = cube_metadata.get("dimension", [])

        for dim in dimensions:
            dim_name = dim.get("dimensionNameEn", "")
            dim_name_lower = dim_name.lower()
            members = dim.get("member", [])

            if not members:
                continue

            # Skip dimensions that are purely structural (e.g., "Statistics", "Adjustments")
            # We only want user-facing dimensions where the user might specify a value
            structural_dims = {"statistic", "statistics", "estimate", "adjustment", "adjustments"}
            if dim_name_lower in structural_dims:
                continue

            # Build a list of candidate member names
            # Skip the first member if it is a "Total"/"All"/"Canada" aggregate
            first_member_name = self._extract_member_name(members[0]).lower() if members else ""
            is_first_aggregate = any(
                tok in first_member_name
                for tok in ["total", "all ", "all-", "both", "canada"]
            ) or first_member_name.startswith("all")

            best_match_score = 0
            best_match_term: Optional[str] = None
            best_dim_hint: Optional[str] = None

            if any(kw in dim_name_lower for kw in ["geogr", "province", "territor", "region"]):
                geography_candidates: list[str] = []
                for name in self.GEOGRAPHY_MEMBER_IDS:
                    if name == "CANADA":
                        continue
                    geography_candidates.append(name)
                geography_candidates.extend(
                    alias
                    for alias, canonical in self.GEOGRAPHY_ALIASES.items()
                    if canonical != "CANADA"
                )
                geography_candidates = sorted(set(geography_candidates), key=len, reverse=True)
                for candidate in geography_candidates:
                    candidate_lower = candidate.lower()
                    pattern = r'(?<![a-z])' + re.escape(candidate_lower) + r'(?![a-z])'
                    if not re.search(pattern, query_lower):
                        continue
                    canonical = self.GEOGRAPHY_ALIASES.get(candidate.upper(), candidate)
                    best_match_score = 1000 + len(canonical)
                    best_match_term = canonical.title() if canonical.isupper() else canonical
                    best_dim_hint = "geography"
                    break

            if any(kw in dim_name_lower for kw in ["gender", "sex", "age"]):
                for alias in sorted(_DIMENSION_MEMBER_VALUE_ALIASES, key=len, reverse=True):
                    pattern = r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])'
                    if not re.search(pattern, query_lower):
                        continue
                    member_id = self._find_member_id_by_keywords(members, [alias])
                    if member_id is None:
                        continue
                    member_name = alias
                    for member in members:
                        if member.get("memberId") == member_id:
                            member_name = self._extract_member_name(member) or alias
                            break
                    score = 900 + len(alias)
                    if score > best_match_score:
                        best_match_score = score
                        best_match_term = member_name
                        best_dim_hint = "age" if "age" in dim_name_lower else "gender"
                    break

            for member in members:
                member_name = self._extract_member_name(member)
                if not member_name:
                    continue

                member_name_lower = member_name.lower()
                member_id = member.get("memberId")

                # Skip aggregate/total first member (the one representing "All" or "Total")
                if is_first_aggregate and member == members[0]:
                    continue

                # Skip members whose name substantially overlaps the base indicator.
                # e.g., if base_indicator is "UNEMPLOYMENT_RATE", skip:
                #   - "Unemployment rate" (exact match)
                #   - "Unemployment" (subset of indicator words)
                # These are handled by _select_default_member_id, not modifiers.
                indicator_normalized = base_indicator.lower().replace("_", " ")
                if member_name_lower == indicator_normalized or indicator_normalized == member_name_lower.rstrip("s"):
                    continue
                # Also skip if the member name consists entirely of indicator words
                member_name_words = normalized_words(member_name_lower)
                if member_name_words and member_name_words.issubset(indicator_words):
                    continue

                # Check if any form of this member name appears in the query
                # Strategy 1: exact member name match (word-boundary aware)
                # Use regex word boundaries to avoid substring false positives
                # e.g., "employment rate" must not match inside "unemployment rate"
                member_variants = {
                    member_name_lower,
                    member_name_lower.replace(" years", ""),
                    member_name_lower.replace(" year", ""),
                    re.sub(r"(\d+)\s+to\s+(\d+)", r"\1-\2", member_name_lower),
                }
                member_variants = {variant.strip() for variant in member_variants if variant and variant.strip()}
                matched_variant = None
                for variant in sorted(member_variants, key=len, reverse=True):
                    if variant not in query_lower:
                        continue
                    pattern = r'(?<![a-z0-9])' + re.escape(variant) + r'(?![a-z0-9])'
                    if re.search(pattern, query_lower):
                        matched_variant = variant
                        break
                if matched_variant:
                    # Guard: if the matched phrase is mostly noise/indicator words,
                    # this is not a real modifier (e.g., "consumer price index" is
                    # just the CPI indicator name, not a CPI sub-category).
                    match_words = normalized_words(member_name_lower)
                    non_noise_match_words = match_words - noise_words - {"and", "or", "the", "of", "to"}
                    if not non_noise_match_words:
                        continue  # match is entirely noise/indicator words

                    score = 100 + len(matched_variant)
                    if score > best_match_score:
                        best_match_score = score
                        best_match_term = member_name
                        best_dim_hint = dim_name_lower.split()[0]  # e.g., "geography" from "Geography"
                    continue

                # Strategy 2: check if individual significant words from member name
                # appear as query words (not just substrings)
                member_words = normalized_words(member_name_lower)
                significant_member_words = member_words - noise_words - {"and", "or", "the", "of", "to"}
                if not significant_member_words:
                    continue

                query_words = normalized_words(query_lower)

                for mw in significant_member_words:
                    if len(mw) < 3:
                        continue  # skip tiny words
                    if mw in query_words:
                        score = 50 + len(mw)
                        if score > best_match_score:
                            best_match_score = score
                            best_match_term = member_name
                            best_dim_hint = dim_name_lower.split()[0]


            if best_match_term and best_dim_hint:
                modifiers[best_dim_hint] = best_match_term

        return modifiers

    async def fetch_multi_province_data(
        self, params: Dict[str, any]
    ) -> List[NormalizedData]:
        """Fetch data for multiple provinces in a single batch API call.

        This method is optimized for multi-province queries by discovering the product's
        actual dimension structure and building proper coordinates for batch queries.

        Handles different product types (Population, Labour, Housing, etc.) by
        dynamically fetching metadata to determine dimension counts and member IDs.

        Args:
            params: Dictionary containing:
                - productId: Product ID or vector ID to query
                - indicator: Human-readable indicator name (e.g., "Population")
                - periods: Number of recent periods to fetch (default: 20)
                - provinces: List of province names or "all" for all provinces
                - dimensions: Dict for additional dimensions (labour_characteristic, gender, age, etc.)

        Returns:
            List of NormalizedData objects (one per province)

        Raises:
            ValueError: If product structure cannot be determined
            DataNotAvailableError: If no data can be retrieved
        """
        product_id_param = params.get("productId", self.POPULATION_DEMOGRAPHICS_PRODUCT)
        indicator = params.get("indicator", "Population")
        display_indicator = str(params.get("indicatorLabel") or indicator or "").strip() or str(indicator or "")
        periods = params.get("periods", 20)
        provinces_param = params.get("provinces", "all")
        dimensions = self._normalize_dimension_filters(params.get("dimensions", {}))
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        # Handle case where productId is actually a vector ID (from metadata search)
        # Vector IDs are integers, product IDs are strings like "17100005"
        if isinstance(product_id_param, int):
            logger.info(f"🔄 Parameter is vector ID {product_id_param}, resolving to product ID...")
            try:
                product_id = await self._get_product_id_from_vector(product_id_param)
                logger.info(f"✅ Resolved vector {product_id_param} → product {product_id}")
            except ValueError as e:
                logger.warning(f"⚠️ Could not resolve product ID from vector {product_id_param}: {e}")
                raise ValueError(f"Cannot use batch method: {e}")
        else:
            product_id = str(product_id_param)

        # IMPORTANT: Discover actual product structure to build correct coordinates
        logger.info(f"📊 Discovering dimension structure for product {product_id}...")
        try:
            metadata = await self._get_cube_metadata(product_id)
        except Exception as e:
            logger.warning(f"⚠️ Could not get metadata for {product_id}: {e}")
            raise ValueError(f"Cannot determine product structure: {e}")

        # Extract dimensions from metadata
        dimensions_list = metadata.get("dimension", [])
        if not dimensions_list:
            raise ValueError(f"Product {product_id} has no dimensions")

        logger.info(f"Product {product_id} has {len(dimensions_list)} dimensions")

        # Build a mapping of dimension names to their indices and member mappings
        dimension_mappings = {}
        for dim_idx, dim_info in enumerate(dimensions_list):
            dim_name = dim_info.get("dimensionNameEn", "").upper()
            dim_members = dim_info.get("member", [])

            # Create member ID mappings by name
            member_map = {}
            for member in dim_members:
                member_name = member.get("memberNameEn", "").upper()
                member_id = member.get("memberId")
                if member_name and member_id:
                    member_map[member_name] = member_id

            dimension_mappings[dim_name] = {
                "index": dim_idx,
                "member_map": member_map,
                "members": dim_members
            }

            logger.debug(f"  Dimension {dim_idx} ({dim_name}): {len(member_map)} members")

        # ---------- Discover Geography dimension from metadata ----------
        # Find the geography dimension index and its member list
        geo_dim_idx: Optional[int] = None
        geo_members: List[Dict[str, Any]] = []
        for dim_idx, dim_info in enumerate(dimensions_list):
            if "geogr" in dim_info.get("dimensionNameEn", "").lower():
                geo_dim_idx = dim_idx
                geo_members = dim_info.get("member", [])
                break

        if geo_dim_idx is None:
            raise ValueError(
                f"Product {product_id} has no Geography dimension; "
                f"cannot run a multi-province query."
            )

        explicit_geo_member_ids: Dict[str, int] = {}
        for member in geo_members:
            member_id = member.get("memberId")
            member_name = str(member.get("memberNameEn", "") or "").strip()
            if not member_name or member_id in (None, 1):
                continue
            normalized_name = canonicalize_canadian_region(member_name) or member_name
            explicit_geo_member_ids[" ".join(normalized_name.upper().split())] = member_id

        available_explicit_geographies = sorted(explicit_geo_member_ids)

        def _resolve_explicit_geography_member_id(raw_name: Any) -> tuple[int, str]:
            requested_name = str(raw_name or "").strip()
            normalized_name = canonicalize_canadian_region(requested_name) or requested_name
            lookup_key = " ".join(normalized_name.upper().split())
            member_id = explicit_geo_member_ids.get(lookup_key)
            if member_id is not None:
                return member_id, normalized_name

            available = ", ".join(available_explicit_geographies) if available_explicit_geographies else "none"
            raise ValueError(
                f"Product {product_id} does not expose geography '{requested_name}'. "
                f"Available geographies: {available}."
            )

        # Determine which provinces to query -- dynamically from metadata
        if provinces_param == "all" or provinces_param is None:
            # Use get_dimension_members to get provinces (children of Canada=1)
            province_tuples = await self.get_dimension_members(product_id, "geogr", parent_id=1)
            if not province_tuples:
                # Fallback: take all geography members except the first (usually "Canada")
                province_tuples = [
                    (m["memberId"], m.get("memberNameEn", ""))
                    for m in geo_members
                    if m.get("memberId", 0) != 1
                ]
            provinces_to_query_with_ids: List[tuple] = province_tuples
        elif isinstance(provinces_param, list):
            provinces_to_query_with_ids = []
            for pname in provinces_param:
                mid, normalized_name = _resolve_explicit_geography_member_id(pname)
                provinces_to_query_with_ids.append((mid, normalized_name))
        else:
            mid, normalized_name = _resolve_explicit_geography_member_id(provinces_param)
            provinces_to_query_with_ids = [(mid, normalized_name)]

        # Validate provinces (reject cities / non-Canadian)
        for _, province_name in provinces_to_query_with_ids:
            try:
                self._resolve_geography(province_name)
            except ValueError:
                # resolve_geography might not recognise dynamically-discovered names
                # that don't exist in the hardcoded list -- that's fine, we already
                # have the member ID from metadata.
                pass

        # ---------- Build coordinate requests for each province ----------
        indicator_lower = display_indicator.lower().replace("_", " ") if display_indicator else ""
        coordinate_requests = []
        province_map = {}  # Map coordinate -> province name for response parsing

        for geography_id, province_name in provinces_to_query_with_ids:
            coordinate_parts: List[str] = []

            for dim_idx, dim_info in enumerate(dimensions_list):
                dim_name_lower = dim_info.get("dimensionNameEn", "").lower()
                members = dim_info.get("member", [])

                if dim_idx == geo_dim_idx:
                    # Geography dimension -- use the province member ID
                    coordinate_parts.append(str(geography_id))

                elif any(kw in dim_name_lower for kw in ["gender", "sex"]):
                    gender = dimensions.get("gender") or dimensions.get("sex")
                    if gender:
                        mid = self._find_member_id_by_keywords(members, [gender]) or 1
                    else:
                        mid = self._find_member_id_by_keywords(members, ["both sexes", "total"]) or 1
                    coordinate_parts.append(str(mid))

                elif "age" in dim_name_lower:
                    age = dimensions.get("age")
                    if age:
                        mid = self._find_member_id_by_keywords(members, [age]) or 1
                    else:
                        mid = self._find_member_id_by_keywords(members, ["15 years and over", "all ages", "total"]) or 1
                    coordinate_parts.append(str(mid))

                elif any(kw in dim_name_lower for kw in ["labour force characteristic", "labour force", "labor force"]):
                    labour_char = dimensions.get("labour_characteristic") or dimensions.get("characteristic")
                    if labour_char:
                        mid = self._find_member_id_by_keywords(members, [labour_char]) or 1
                    else:
                        mid = self._select_default_member_id(
                            dim_info.get("dimensionNameEn", ""), members, indicator_lower
                        )
                    coordinate_parts.append(str(mid))

                else:
                    # For any other dimension, use semantic default selection
                    mid = self._select_default_member_id(
                        dim_info.get("dimensionNameEn", ""), members, indicator_lower
                    )
                    coordinate_parts.append(str(mid))

            # Pad coordinate to 10 dimensions (StatsCan requirement)
            while len(coordinate_parts) < 10:
                coordinate_parts.append("0")

            coordinate = ".".join(coordinate_parts[:10])
            coordinate_requests.append({
                "productId": product_id,
                "coordinate": coordinate,
                "latestN": periods
            })
            province_map[coordinate] = province_name

            logger.debug(f"  {province_name}: {coordinate}")

        if not coordinate_requests:
            raise ValueError(f"No valid provinces found in: {[n for _, n in provinces_to_query_with_ids]}")

        logger.info(
            f"Fetching {indicator} for {len(coordinate_requests)} provinces "
            f"in batch (single API call)"
        )

        # Make single batch API call with all coordinates
        # Use extended timeout for multi-province queries (300s = 5 minutes)
        # This prevents timeouts when StatsCan API is slow
        try:
            # Wait for rate limiter before making request
            wait_delay = await wait_for_provider("StatsCan")
            if wait_delay > 0:
                logger.info(f"⏳ StatsCan rate limiter applied {wait_delay:.1f}s delay")

            # Use shared HTTP client pool for better performance
            client = get_http_client()
            response = await client.post(
                f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
                json=coordinate_requests,  # Send array of coordinate requests
                headers={"Content-Type": "application/json"},
                timeout=300.0,
            )

            if response.status_code == 406:
                raise DataNotAvailableError(
                    f"Invalid coordinate format for product {product_id}"
                )

            response.raise_for_status()
            payload = response.json()

            # Record this request for rate limiting
            record_provider_request("StatsCan")

        except httpx.TimeoutException:
            raise DataNotAvailableError(
                f"StatsCan API timeout after 300 seconds for {len(coordinate_requests)} provinces. "
                f"The batch query took too long. Try reducing the time period or number of provinces."
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise DataNotAvailableError(
                    f"StatsCan API rate limit exceeded. Please try again in a few moments."
                )
            raise

        # Parse results for each province
        results = []
        failed_provinces = []
        expected_coordinates = set(province_map)
        returned_coordinates = set()

        for i, result_obj in enumerate(payload):
            # Extract coordinate from nested object (StatsCan API structure)
            data_object = result_obj.get("object", {})
            coordinate = data_object.get("coordinate", "")
            if coordinate:
                returned_coordinates.add(coordinate)
            province_name = province_map.get(coordinate, f"Province_{i+1}")

            if result_obj.get("status") != "SUCCESS":
                status_code = data_object.get("responseStatusCode", result_obj.get("responseStatusCode", "?"))
                error_msg = data_object if isinstance(data_object, str) else "Unknown error"
                logger.warning(
                    f"Province '{province_name}' query failed: Code {status_code} - {error_msg}"
                )
                failed_provinces.append(province_name)
                continue

            vector_data = data_object.get("vectorDataPoint", [])

            if not vector_data:
                logger.warning(f"⚠️ No data returned for {province_name}")
                failed_provinces.append(province_name)
                continue

            # Extract metadata
            frequency_code = vector_data[0].get("frequencyCode", 6)
            frequency = self.FREQUENCY_MAP.get(frequency_code, "unknown")
            scalar_factor = vector_data[0].get("scalarFactorCode", 0)
            unit = self.SCALAR_FACTOR_MAP.get(scalar_factor, "")

            # Human-readable URL for data verification on Statistics Canada website
            # Use helper method to ensure 10-digit product ID format
            source_url = self._get_table_viewer_url(product_id)

            # Build data points first (needed for startDate/endDate)
            data_points = [
                {
                    "date": point["refPer"],
                    "value": point["value"] if point["value"] is not None else None,
                }
                for point in vector_data
            ]

            # Apply date range filter if specified
            if start_date or end_date:
                data_points = self._filter_by_date_range(data_points, start_date, end_date)

            # Determine dataType from indicator name
            indicator_name = f"{province_name} {display_indicator}".strip()
            indicator_upper = indicator_name.upper()
            if "RATE" in indicator_upper or "PERCENT" in indicator_upper:
                data_type = "Rate"
            elif "INDEX" in indicator_upper:
                data_type = "Index"
            elif "CHANGE" in indicator_upper:
                data_type = "Percent Change"
            else:
                data_type = "Level"

            metadata = Metadata(
                source="Statistics Canada",
                indicator=indicator_name,
                country="Canada",
                frequency=frequency,
                unit=unit if unit else "persons",
                lastUpdated=(vector_data[-1].get("releaseTime") or "") if vector_data else "",
                seriesId=f"{product_id}:{coordinate}",
                apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
                sourceUrl=source_url,
                # Enhanced metadata fields
                seasonalAdjustment=None,  # Not available in multi-province batch queries
                dataType=data_type,
                priceType=None,  # Not typically available in batch queries
                description=indicator_name,
                notes=None,  # StatsCan doesn't provide detailed notes easily
                scaleFactor=self._map_scalar_factor(scalar_factor) if scalar_factor else None,
                startDate=data_points[0]["date"] if data_points else None,
                endDate=data_points[-1]["date"] if data_points else None,
            )

            results.append(NormalizedData(metadata=metadata, data=data_points))

        missing_coordinates = expected_coordinates - returned_coordinates
        for coordinate in sorted(missing_coordinates):
            province_name = province_map.get(coordinate, coordinate)
            logger.warning("⚠️ Missing StatsCan payload entry for %s", province_name)
            failed_provinces.append(province_name)

        if not results:
            provinces_str = ", ".join(failed_provinces) if failed_provinces else "all provinces"
            raise DataNotAvailableError(
                f"No data returned for any province. Failed: {provinces_str}. "
                f"The product {product_id} may not support the requested dimension combination. "
                f"Try using a single province or a different indicator."
            )

        if failed_provinces:
            provinces_str = ", ".join(failed_provinces[:5])
            raise DataNotAvailableError(
                f"StatsCan batch query returned incomplete province coverage for product {product_id}. "
                f"Failed or missing: {provinces_str}."
            )

        logger.info(f"✅ Successfully fetched data for {len(results)} provinces")

        return results

    async def fetch_multi_dimension_data(
        self, params: Dict[str, Any]
    ) -> List[NormalizedData]:
        """Fetch one series per member of a target StatsCan dimension axis.

        This generalizes decomposition beyond geography so follow-ups like
        "show by age group" can expand the current filtered slice instead of
        being misinterpreted as a scalar dimension filter.
        """
        product_id = self._normalize_metadata_product_id(
            params.get("productId", self.POPULATION_DEMOGRAPHICS_PRODUCT)
        )
        indicator = str(params.get("indicator") or "Population")
        display_indicator = str(params.get("indicatorLabel") or indicator).strip() or indicator
        axis_hint = str(params.get("axis") or "").strip()
        fixed_dimensions = self._normalize_dimension_filters(params.get("dimensions", {}) or {})
        geography_label = str(fixed_dimensions.get("geography") or "").strip()
        periods = params.get("periods", 20)
        start_date = params.get("startDate")
        end_date = params.get("endDate")

        if not axis_hint:
            raise ValueError("fetch_multi_dimension_data requires an axis hint")

        metadata = await self._get_cube_metadata(product_id)
        dimensions_list = metadata.get("dimension", [])
        if not dimensions_list:
            raise ValueError(f"Product {product_id} has no dimensions")

        axis_keywords = self._decomposition_axis_keywords(axis_hint)
        axis_dim_idx: Optional[int] = None
        axis_dim_info: Optional[Dict[str, Any]] = None
        for dim_idx, dim_info in enumerate(dimensions_list):
            dim_name_lower = str(dim_info.get("dimensionNameEn", "")).lower()
            if any(keyword in dim_name_lower for keyword in axis_keywords):
                axis_dim_idx = dim_idx
                axis_dim_info = dim_info
                break

        if axis_dim_idx is None or axis_dim_info is None:
            raise ValueError(f"Product {product_id} has no dimension matching axis '{axis_hint}'")

        axis_members_raw = axis_dim_info.get("member", [])
        axis_members = [
            (member.get("memberId"), member.get("memberNameEn", ""))
            for member in axis_members_raw
            if member.get("memberId") is not None
            and not self._is_aggregate_decomposition_member(axis_hint, member.get("memberNameEn", ""))
        ]
        if not axis_members:
            raise DataNotAvailableError(
                f"No non-aggregate members found for axis '{axis_hint}' in product {product_id}"
            )

        missing_required_dimensions: List[str] = []
        for dim_idx, dim_info in enumerate(dimensions_list):
            if dim_idx == axis_dim_idx:
                continue
            dim_name = str(dim_info.get("dimensionNameEn", "") or "")
            dim_name_lower = dim_name.lower()
            members = dim_info.get("member", [])
            has_supplied_member = False
            for hint, search_term in fixed_dimensions.items():
                hint_lower = str(hint or "").strip().lower()
                if not str(search_term or "").strip():
                    continue
                if hint_lower in dim_name_lower or dim_name_lower in hint_lower:
                    if self._find_member_id_by_keywords(members, [str(search_term)]) is not None:
                        has_supplied_member = True
                        break
            if has_supplied_member:
                continue
            if self._requires_explicit_dimension_member(dim_name, members):
                missing_required_dimensions.append(dim_name)

        if missing_required_dimensions:
            raise DataNotAvailableError(
                "fail-closed supportability block: "
                "reason=statscan_required_dimension_missing; "
                f"product={product_id}; "
                f"missing_dimensions={', '.join(missing_required_dimensions)}"
            )

        indicator_lower = display_indicator.lower().replace("_", " ")
        coordinate_requests: List[Dict[str, Any]] = []
        member_name_by_coordinate: Dict[str, str] = {}

        for member_id, member_name in axis_members:
            coordinate_parts: List[str] = []
            for dim_idx, dim_info in enumerate(dimensions_list):
                dim_name = str(dim_info.get("dimensionNameEn", "") or "")
                dim_name_lower = dim_name.lower()
                members = dim_info.get("member", [])

                if dim_idx == axis_dim_idx:
                    coordinate_parts.append(str(member_id))
                    continue

                matched = False
                for hint, search_term in fixed_dimensions.items():
                    hint_lower = str(hint or "").strip().lower()
                    if hint_lower in dim_name_lower or dim_name_lower in hint_lower:
                        mid = self._find_member_id_by_keywords(members, [str(search_term)])
                        if mid is not None:
                            coordinate_parts.append(str(mid))
                            matched = True
                            break

                if matched:
                    continue

                coordinate_parts.append(
                    str(self._select_default_member_id(dim_name, members, indicator_lower))
                )

            while len(coordinate_parts) < 10:
                coordinate_parts.append("0")

            coordinate = ".".join(coordinate_parts[:10])
            coordinate_requests.append(
                {"productId": product_id, "coordinate": coordinate, "latestN": periods}
            )
            member_name_by_coordinate[coordinate] = str(member_name or "").strip()

        client = get_http_client()
        response = await client.post(
            f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
            json=coordinate_requests,
            headers={"Content-Type": "application/json"},
            timeout=300.0,
        )
        response.raise_for_status()
        payload = response.json()

        results: List[NormalizedData] = []
        for idx, result_obj in enumerate(payload):
            data_object = result_obj.get("object", {})
            coordinate = data_object.get("coordinate", "")
            member_name = member_name_by_coordinate.get(coordinate, f"Member {idx + 1}")
            if result_obj.get("status") != "SUCCESS":
                continue

            vector_data = data_object.get("vectorDataPoint", [])
            if not vector_data:
                continue

            frequency_code = vector_data[0].get("frequencyCode", 6)
            frequency = self.FREQUENCY_MAP.get(frequency_code, "unknown")
            scalar_factor = vector_data[0].get("scalarFactorCode", 0)
            unit = self.SCALAR_FACTOR_MAP.get(scalar_factor, "")
            data_points = [
                {
                    "date": point["refPer"],
                    "value": point["value"] if point["value"] is not None else None,
                }
                for point in vector_data
            ]
            if start_date or end_date:
                data_points = self._filter_by_date_range(data_points, start_date, end_date)

            fixed_labels = [
                str(value).strip()
                for value in fixed_dimensions.values()
                if str(value or "").strip()
            ]
            label_parts = [part for part in [*fixed_labels, member_name] if part]
            indicator_name = display_indicator
            if label_parts:
                indicator_name = f"{display_indicator} - {', '.join(label_parts)}"

            metadata = Metadata(
                source="Statistics Canada",
                indicator=indicator_name,
                country=geography_label or "Canada",
                frequency=frequency,
                unit=unit if unit else "persons",
                lastUpdated=(vector_data[-1].get("releaseTime") or "") if vector_data else "",
                seriesId=f"{product_id}:{coordinate}",
                apiUrl=f"{self.base_url}/getDataFromCubePidCoordAndLatestNPeriods",
                sourceUrl=self._get_table_viewer_url(product_id),
                dataType="Rate" if "rate" in indicator_name.lower() or "percent" in indicator_name.lower() else "Level",
                description=indicator_name,
                scaleFactor=self._map_scalar_factor(scalar_factor) if scalar_factor else None,
                startDate=data_points[0]["date"] if data_points else None,
                endDate=data_points[-1]["date"] if data_points else None,
            )
            results.append(NormalizedData(metadata=metadata, data=data_points))

        if not results:
            raise DataNotAvailableError(
                f"No data returned for axis '{axis_hint}' on product {product_id}."
            )

        return results
