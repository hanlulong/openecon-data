"""
Data Structure Definition (DSD) Cache Service.

Provides dynamic dimension discovery for SDMX-based providers (OECD, Eurostat, BIS).
Caches DSD responses to avoid repeated API calls.

General solution approach:
1. Query DSD endpoint to discover required dimensions
2. Build dimension keys with intelligent defaults
3. Cache results for performance
4. Persist cache to disk to survive server restarts
"""

from __future__ import annotations

from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from pathlib import Path
import httpx
import logging
import json

logger = logging.getLogger(__name__)

# Default cache file location
DEFAULT_CACHE_DIR = Path(__file__).parent.parent / "data"
DEFAULT_CACHE_FILE = "dsd_cache.json"


class DSDCache:
    """Cache for Data Structure Definitions from SDMX providers.

    Features:
    - In-memory caching with TTL
    - Disk persistence to survive server restarts
    - Automatic loading on initialization
    """

    def __init__(self, ttl_hours: int = 24, cache_dir: Optional[Path] = None):
        """
        Initialize DSD cache.

        Args:
            ttl_hours: Time-to-live for cached DSDs in hours (default: 24)
            cache_dir: Directory for cache file (default: backend/data)
        """
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = timedelta(hours=ttl_hours)
        self.ttl_hours = ttl_hours

        # Setup cache file path
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_file = self.cache_dir / DEFAULT_CACHE_FILE

        # Load cache from disk on startup
        self._load_from_disk()

    def _cache_key(self, provider: str, agency: str, dsd_id: str, version: str) -> str:
        """Generate cache key for DSD lookup."""
        return f"{provider}:{agency}:{dsd_id}:{version}"

    def _is_expired(self, cached_at: datetime) -> bool:
        """Check if cache entry has expired."""
        return datetime.now() - cached_at > self.ttl

    def _load_from_disk(self) -> None:
        """Load cache from disk file."""
        try:
            if not self.cache_file.exists():
                logger.debug(f"DSD cache file not found: {self.cache_file}")
                return

            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Restore cache entries, converting datetime strings back
            loaded_count = 0
            expired_count = 0
            for key, entry in data.items():
                cached_at = datetime.fromisoformat(entry["cached_at"])
                if not self._is_expired(cached_at):
                    self.cache[key] = {
                        "dsd": entry["dsd"],
                        "cached_at": cached_at,
                    }
                    loaded_count += 1
                else:
                    expired_count += 1

            logger.info(
                f"DSD cache loaded from disk: {loaded_count} entries "
                f"(skipped {expired_count} expired)"
            )

        except json.JSONDecodeError as e:
            logger.warning(f"DSD cache file corrupted, starting fresh: {e}")
        except Exception as e:
            logger.warning(f"Failed to load DSD cache from disk: {e}")

    def _save_to_disk(self) -> None:
        """Save cache to disk file."""
        try:
            # Ensure directory exists
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            # Convert cache to JSON-serializable format
            data = {}
            for key, entry in self.cache.items():
                data[key] = {
                    "dsd": entry["dsd"],
                    "cached_at": entry["cached_at"].isoformat(),
                }

            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)

            logger.debug(f"DSD cache saved to disk: {len(data)} entries")

        except Exception as e:
            logger.warning(f"Failed to save DSD cache to disk: {e}")

    async def get_dsd(
        self,
        provider: str,
        agency: str,
        dsd_id: str,
        version: str,
        base_url: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get Data Structure Definition, using cache if available.

        Args:
            provider: Provider name (OECD, EUROSTAT, BIS)
            agency: Agency identifier (e.g., OECD.SDD.TPS)
            dsd_id: Data structure ID (e.g., DSD_LFS)
            version: Version (e.g., 1.0)
            base_url: Base URL for SDMX API

        Returns:
            DSD metadata including dimensions, or None if not found
        """
        cache_key = self._cache_key(provider, agency, dsd_id, version)

        # Check cache
        if cache_key in self.cache:
            entry = self.cache[cache_key]
            if not self._is_expired(entry["cached_at"]):
                logger.debug(f"DSD cache hit: {cache_key}")
                return entry["dsd"]
            else:
                logger.debug(f"DSD cache expired: {cache_key}")
                del self.cache[cache_key]

        # Fetch from API
        logger.info(f"DSD cache miss: {cache_key}, fetching from API")
        dsd = await self._fetch_dsd(provider, agency, dsd_id, version, base_url)

        if dsd:
            self.cache[cache_key] = {
                "dsd": dsd,
                "cached_at": datetime.now(),
            }
            # Persist to disk for future server restarts
            self._save_to_disk()
            logger.info(f"DSD cached and saved to disk: {cache_key}")

        return dsd

    async def _fetch_dsd(
        self,
        provider: str,
        agency: str,
        dsd_id: str,
        version: str,
        base_url: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch DSD from SDMX API.

        Args:
            provider: Provider name
            agency: Agency ID
            dsd_id: DSD ID
            version: Version
            base_url: Base URL

        Returns:
            Parsed DSD structure or None on error
        """
        try:
            # Build DSD query URL based on provider
            if provider.upper() == "OECD":
                url = f"{base_url}/datastructure/{agency}/{dsd_id}/{version}"
                params = {"references": "children", "detail": "full"}
                headers = {"Accept": "application/vnd.sdmx.structure+json;version=1.0"}

            elif provider.upper() == "EUROSTAT":
                url = f"{base_url}/datastructure/ESTAT/{dsd_id}"
                params = {"references": "children", "detail": "full"}
                headers = {"Accept": "application/vnd.sdmx.structure+json;version=2.1"}

            elif provider.upper() == "BIS":
                url = f"{base_url}/dataflow/{agency}/{dsd_id}/{version}"
                params = {"references": "all", "detail": "full"}
                headers = {"Accept": "application/vnd.sdmx.structure+json;version=1.0"}

            else:
                logger.error(f"Unknown provider: {provider}")
                return None

            # OECD structure endpoints can be slow (30-60+ seconds)
            # Use 90-second timeout with retry logic
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=90.0) as client:
                        response = await client.get(url, params=params, headers=headers)
                        response.raise_for_status()
                        data = response.json()

                        # Parse DSD structure
                        return self._parse_dsd(data, provider)
                except httpx.TimeoutException:
                    if attempt < max_retries:
                        logger.warning(f"DSD fetch timeout (attempt {attempt + 1}/{max_retries + 1}), retrying...")
                        continue
                    raise

        except httpx.HTTPStatusError as e:
            logger.warning(f"DSD fetch failed (HTTP {e.response.status_code}): {url}")
            return None
        except httpx.TimeoutException:
            logger.error(f"DSD fetch timeout after {max_retries + 1} attempts: {url}")
            return None
        except Exception as e:
            logger.error(f"DSD fetch error: {e}")
            return None

    def _parse_dsd(self, data: Dict[str, Any], provider: str) -> Dict[str, Any]:
        """
        Parse DSD JSON response into simplified structure.

        Args:
            data: Raw SDMX-JSON response
            provider: Provider name

        Returns:
            Simplified DSD structure with dimensions list
        """
        try:
            dsd_list = data.get("data", {}).get("dataStructures", [])
            if not dsd_list:
                logger.warning("No dataStructures found in DSD response")
                return {"dimensions": []}

            dsd = dsd_list[0]
            components = dsd.get("dataStructureComponents", {})
            dim_list = components.get("dimensionList", {})
            dimensions = dim_list.get("dimensions", [])

            # Extract dimension metadata
            parsed_dims = []
            for idx, dim in enumerate(dimensions):
                # OECD doesn't always include position field, so use array index
                position = dim.get("position", idx)
                parsed_dims.append({
                    "id": dim.get("id"),
                    "position": position,
                    "name": dim.get("name", dim.get("id")),
                    "codelist": dim.get("localRepresentation", {}).get("enumeration"),
                })

            return {
                "dsd_id": dsd.get("id"),
                "name": dsd.get("name"),
                "dimensions": parsed_dims,
            }

        except Exception as e:
            logger.error(f"Error parsing DSD: {e}")
            return {"dimensions": []}


class DimensionKeyBuilder:
    """Build SDMX dimension keys with intelligent defaults."""

    # Common dimension defaults across providers
    # NOTE: For OECD SDMX, using wildcards (empty strings) is preferred over
    # guessing dimension values, as incorrect values cause 404 errors
    DEFAULT_DIMENSION_VALUES = {
        # Only specify defaults that are commonly correct across datasets
        # Most dimensions should use wildcards for maximum data coverage

        # Frequency - only if explicitly needed
        # "FREQ": "M",  # Commented out - let user or provider specify
        # "freq": "M",
    }

    def __init__(self, dsd_cache: DSDCache):
        """
        Initialize dimension key builder.

        Args:
            dsd_cache: DSD cache instance
        """
        self.dsd_cache = dsd_cache

    async def build_key(
        self,
        provider: str,
        agency: str,
        dsd_id: str,
        version: str,
        base_url: str,
        user_params: Dict[str, Any],
        custom_defaults: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """
        Build dimension key for SDMX data query.

        Args:
            provider: Provider name
            agency: Agency ID
            dsd_id: DSD ID
            version: Version
            base_url: Base URL
            user_params: User-provided parameter values (e.g., {"country": "DEU", "freq": "M"})
            custom_defaults: Provider/dataset-specific default values

        Returns:
            Dimension key string (e.g., "DEU........M") or None on error
        """
        # Get DSD
        dsd = await self.dsd_cache.get_dsd(provider, agency, dsd_id, version, base_url)
        if not dsd or not dsd.get("dimensions"):
            logger.warning(f"No DSD found for {provider}:{dsd_id}")
            return None

        dimensions = dsd["dimensions"]
        num_dims = len(dimensions)

        # Initialize key parts with wildcards
        key_parts = [""] * num_dims

        # Merge defaults: common < custom < user
        defaults = {**self.DEFAULT_DIMENSION_VALUES}
        if custom_defaults:
            defaults.update(custom_defaults)

        # Map common parameter names to dimension IDs
        param_mapping = {
            "country": ["REF_AREA", "geo", "COUNTRY"],
            "frequency": ["FREQ", "freq"],
            "startDate": ["TIME_PERIOD", "time"],
            "endDate": ["TIME_PERIOD", "time"],
        }

        # Fill dimension values
        for dim in dimensions:
            dim_id = dim["id"]
            position = dim["position"]

            # Try direct match first
            if dim_id in user_params:
                key_parts[position] = str(user_params[dim_id])
            # Try mapped parameter names
            elif dim_id in defaults:
                key_parts[position] = defaults[dim_id]
            else:
                # Check mapped names
                for param_name, dim_names in param_mapping.items():
                    if dim_id in dim_names and param_name in user_params:
                        key_parts[position] = str(user_params[param_name])
                        break
                else:
                    # Use wildcard
                    key_parts[position] = ""

        dimension_key = ".".join(key_parts)
        logger.debug(f"Built dimension key ({num_dims} dims): {dimension_key}")

        return dimension_key


# Global singleton instances
_dsd_cache = None
_dimension_key_builder = None


def get_dsd_cache() -> DSDCache:
    """Get global DSD cache instance."""
    global _dsd_cache
    if _dsd_cache is None:
        _dsd_cache = DSDCache(ttl_hours=24)
    return _dsd_cache


def get_dimension_key_builder() -> DimensionKeyBuilder:
    """Get global dimension key builder instance."""
    global _dimension_key_builder
    if _dimension_key_builder is None:
        _dimension_key_builder = DimensionKeyBuilder(get_dsd_cache())
    return _dimension_key_builder
