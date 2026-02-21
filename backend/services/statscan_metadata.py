"""Statistics Canada Metadata Discovery Service

Dynamically discovers product IDs and dimension mappings from StatsCan API
instead of relying on hardcoded values.
"""
import logging
from typing import Dict, List, Optional, Any
import httpx
import json
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


class StatsCanMetadataService:
    """Service for discovering Statistics Canada cube metadata dynamically"""

    def __init__(self):
        self.base_url = "https://www150.statcan.gc.ca/t1/wds/rest"
        self._cache: Dict[str, Any] = {}

        # Load local metadata cache
        self._local_cache_file = Path(__file__).parent.parent / 'data' / 'statscan_metadata_cache.json'
        self._local_cache = self._load_local_cache()

        # Known product IDs for common indicators
        self.KNOWN_PRODUCTS = {
            'Labour Force': '14100287',  # Labour Force Survey - unemployment by demographics
            'labour force': '14100287',
            'unemployment': '14100287',
            'employment': '14100287',
            'Population': '17100005',  # Population estimates by age and gender
            'population': '17100005',
            'GDP': '36100402',  # GDP by industry
            'gdp': '36100402',
            'Immigration': '17100040',  # Permanent residents by category
            'immigration': '17100040',
            'Wages': '14100063',  # Average hourly and weekly earnings
            'wages': '14100063',
        }

    async def discover_product_for_indicator(self, indicator_name: str) -> Optional[str]:
        """
        Search for a product ID matching the given indicator.

        Args:
            indicator_name: Human-readable indicator (e.g., "unemployment", "GDP")

        Returns:
            Product ID string (e.g., "14100287") or None if not found
        """
        # Try known products first (much faster and more reliable)
        if indicator_name in self.KNOWN_PRODUCTS:
            product_id = self.KNOWN_PRODUCTS[indicator_name]
            logger.info(f"📍 Using known product {product_id} for '{indicator_name}'")
            return product_id

        # Fallback to API search (slower, may timeout)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{self.base_url}/getAllCubesListLite")
                response.raise_for_status()
                cubes = response.json()

                # Search for matching cubes
                indicator_lower = indicator_name.lower()
                for cube in cubes:
                    title = cube.get("cubeTitleEn", "").lower()
                    if indicator_lower in title:
                        product_id = cube["productId"]
                        logger.info(
                            f"📍 Discovered product {product_id} for '{indicator_name}': "
                            f"{cube['cubeTitleEn']}"
                        )
                        return str(product_id)

                logger.warning(f"No product found matching '{indicator_name}'")
                return None

        except Exception as e:
            logger.warning(f"Error discovering product for '{indicator_name}': {e}")
            return None

    def _load_local_cache(self) -> Dict[str, Any]:
        """Load local metadata cache from JSON file"""
        try:
            if self._local_cache_file.exists():
                with open(self._local_cache_file, 'r') as f:
                    cache = json.load(f)
                products = cache.get('products', {})
                logger.info(f"📦 Loaded local metadata cache with {len(products)} products")
                return products
            else:
                logger.warning(f"Local metadata cache not found: {self._local_cache_file}")
                return {}
        except Exception as e:
            logger.error(f"Error loading local cache: {e}")
            return {}

    async def get_cube_metadata(self, product_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch full metadata for a given product ID.

        Args:
            product_id: Product ID (e.g., "14100287" for Labour Force Survey)

        Returns:
            Dictionary containing cube metadata including dimensions and members
        """
        # Check memory cache first
        cache_key = f"metadata_{product_id}"
        if cache_key in self._cache:
            logger.debug(f"Using cached metadata for product {product_id}")
            return self._cache[cache_key]

        # Check local file cache second
        if product_id in self._local_cache:
            metadata = self._local_cache[product_id]
            self._cache[cache_key] = metadata  # Store in memory cache too
            logger.info(f"💾 Using local cached metadata for product {product_id}")
            return metadata

        # Only fetch from API if not in local cache
        try:
            # Explicit timeout configuration for all stages
            timeout_config = httpx.Timeout(
                connect=30.0,  # 30 seconds for TLS/connection
                read=60.0,     # 60 seconds for reading response
                write=10.0,    # 10 seconds for writing request
                pool=10.0      # 10 seconds for pool acquire
            )
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.post(
                    f"{self.base_url}/getCubeMetadata",
                    json=[{"productId": int(product_id)}],
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()

                if not payload or payload[0].get("status") != "SUCCESS":
                    logger.error(f"Failed to fetch metadata for product {product_id}")
                    return None

                metadata = payload[0]["object"]

                # Cache for future use
                self._cache[cache_key] = metadata

                logger.info(
                    f"✅ Fetched metadata for product {product_id}: "
                    f"{metadata.get('cubeTitleEn', 'Unknown')}"
                )

                return metadata

        except Exception as e:
            logger.exception(f"Error fetching metadata for product {product_id}: {e}")
            return None

    def extract_dimension_mappings(self, metadata: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
        """
        Extract dimension member ID mappings from cube metadata.

        Args:
            metadata: Cube metadata from getCubeMetadata

        Returns:
            Dictionary mapping dimension names to member ID mappings
            Example: {
                "Geography": {"CANADA": 1, "ONTARIO": 7, ...},
                "Sex": {"Both sexes": 1, "Males": 2, "Females": 3},
                "Age group": {"15 to 19 years": 5, "20 to 24 years": 6, ...}
            }
        """
        result = {}

        dimensions = metadata.get("dimension", [])

        for dimension in dimensions:
            dimension_name = dimension.get("dimensionNameEn", "Unknown")
            dimension_id = dimension.get("dimensionPositionId")

            # Extract member mappings
            members = dimension.get("member", [])
            member_mapping = {}

            for member in members:
                member_id = member.get("memberId")
                member_name = member.get("memberNameEn", "").strip()

                if member_id and member_name:
                    # Store with original name
                    member_mapping[member_name] = member_id

                    # Also store uppercase version for case-insensitive lookup
                    member_mapping[member_name.upper()] = member_id

                    # Store abbreviated versions if applicable
                    if "TO" in member_name.upper():
                        # Age groups like "15 to 19 years" -> "15-19"
                        abbreviated = member_name.replace(" to ", "-").replace(" years", "").strip()
                        member_mapping[abbreviated] = member_id
                        member_mapping[abbreviated.upper()] = member_id

            result[dimension_name] = member_mapping

            logger.debug(
                f"Dimension {dimension_id} ({dimension_name}): "
                f"{len(member_mapping)} members"
            )

        return result

    async def get_dimension_mappings(self, product_id: str) -> Optional[Dict[str, Dict[str, int]]]:
        """
        Get dimension mappings for a product ID (fetches metadata if needed).

        Args:
            product_id: Product ID (e.g., "14100287")

        Returns:
            Dictionary of dimension mappings or None if error
        """
        metadata = await self.get_cube_metadata(product_id)
        if not metadata:
            return None

        return self.extract_dimension_mappings(metadata)

    async def discover_for_query(
        self,
        indicator: str,
        category: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Discover complete metadata for a query (indicator + optional category).

        Args:
            indicator: Main indicator (e.g., "unemployment", "population")
            category: Optional category dimension (e.g., "age", "gender", "province")

        Returns:
            Dictionary containing:
            - product_id: The discovered product ID
            - product_title: Human-readable title
            - dimensions: Full dimension mappings
            - relevant_dimension: The dimension most relevant to the category (if specified)
        """
        # Step 1: Find product ID
        product_id = await self.discover_product_for_indicator(indicator)
        if not product_id:
            return None

        # Step 2: Get metadata
        metadata = await self.get_cube_metadata(product_id)
        if not metadata:
            return None

        # Step 3: Extract dimension mappings
        dimension_mappings = self.extract_dimension_mappings(metadata)

        # Step 4: Find relevant dimension for category (if specified)
        relevant_dimension = None
        if category:
            category_lower = category.lower()
            for dim_name in dimension_mappings.keys():
                dim_name_lower = dim_name.lower()
                if category_lower in dim_name_lower or dim_name_lower in category_lower:
                    relevant_dimension = dim_name
                    break

        result = {
            "product_id": product_id,
            "product_title": metadata.get("cubeTitleEn", "Unknown"),
            "dimensions": dimension_mappings,
            "relevant_dimension": relevant_dimension,
            "dimension_count": len(dimension_mappings),
            "cube_start_date": metadata.get("cubeStartDate"),
            "cube_end_date": metadata.get("cubeEndDate"),
        }

        logger.info(
            f"✅ Discovered metadata for '{indicator}' "
            f"(product {product_id}, {len(dimension_mappings)} dimensions)"
        )

        return result


# Singleton instance
_metadata_service: Optional[StatsCanMetadataService] = None


def get_statscan_metadata_service() -> StatsCanMetadataService:
    """Get or create singleton instance"""
    global _metadata_service
    if _metadata_service is None:
        _metadata_service = StatsCanMetadataService()
    return _metadata_service
