"""
Tests for StatsCan dynamic metadata discovery and WDS API integration.

Tests verify that the provider can dynamically discover and fetch data for:
- Employment indicators by province
- Retail sales data
- Labour force characteristics
- Other indicators without hardcoded mappings
"""
import asyncio
import pytest
from backend.providers.statscan import StatsCanProvider


@pytest.fixture
def statscan_provider():
    """Create a StatsCan provider instance."""
    return StatsCanProvider()


@pytest.mark.asyncio
async def test_search_vectors_for_retail_sales(statscan_provider):
    """Test that search_vectors can find retail sales cubes."""
    results = await statscan_provider.search_vectors("retail sales", limit=5)

    assert len(results) > 0, "Should find retail sales cubes"
    assert any(
        "retail" in r.get("title", "").lower()
        for r in results
    ), "Should have retail-related cubes"

    # Verify structure of results
    for result in results[:2]:
        assert "productId" in result
        assert "title" in result
        assert "archived" in result
        assert result["archived"] in ["1", "2"]


@pytest.mark.asyncio
async def test_search_vectors_for_employment(statscan_provider):
    """Test that search_vectors can find employment-related cubes."""
    results = await statscan_provider.search_vectors("employment", limit=5)

    assert len(results) > 0, "Should find employment cubes"
    assert any(
        "employ" in r.get("title", "").lower()
        for r in results
    ), "Should have employment-related cubes"


@pytest.mark.asyncio
async def test_get_cube_metadata_labour_force(statscan_provider):
    """Test getCubeMetadata for labour force product."""
    metadata = await statscan_provider._get_cube_metadata("14100287")

    # Should have basic cube information
    assert "dimension" in metadata
    assert "productId" in metadata

    # Should have geography dimension
    dimensions = metadata.get("dimension", [])
    assert len(dimensions) > 0

    geo_dim = next(
        (d for d in dimensions if "geogr" in d.get("dimensionNameEn", "").lower()),
        None
    )
    assert geo_dim is not None, "Should have geography dimension"

    # Geography should have multiple members (provinces)
    members = geo_dim.get("member", [])
    assert len(members) >= 10, "Should have at least 10 geography members (provinces)"

    # Check for Alberta
    member_names = [m.get("memberNameEn", "").upper() for m in members]
    assert "ALBERTA" in member_names, "Should include Alberta"


@pytest.mark.asyncio
async def test_fetch_dynamic_data_alberta_employment(statscan_provider):
    """Test fetch_dynamic_data for Alberta employment."""
    result = await statscan_provider.fetch_dynamic_data({
        "indicator": "employment",
        "geography": "Alberta",
        "periods": 60
    })

    # Should have returned data
    assert result is not None
    assert result.metadata is not None
    assert result.data is not None
    assert len(result.data) > 0

    # Metadata checks
    assert "Alberta" in result.metadata.indicator or "Alberta" in str(result.metadata.indicator).lower()
    assert result.metadata.source == "Statistics Canada"
    assert result.metadata.country == "Canada"

    # Data should be time-series (DataPoint objects)
    assert result.data[0].date is not None
    assert result.data[0].value is not None
    assert result.data[-1].date is not None
    assert result.data[-1].value is not None

    # Verify data is sorted chronologically
    first_date = result.data[0].date
    last_date = result.data[-1].date
    assert first_date <= last_date, "first date should be <= last date"


@pytest.mark.asyncio
async def test_fetch_dynamic_data_retail_sales(statscan_provider):
    """Test fetch_dynamic_data for Canada retail sales."""
    result = await statscan_provider.fetch_dynamic_data({
        "indicator": "retail sales",
        "periods": 120
    })

    # Should have returned data
    assert result is not None
    assert result.metadata is not None
    assert result.data is not None
    assert len(result.data) > 0

    # Metadata checks
    assert "retail" in result.metadata.indicator.lower()
    assert result.metadata.source == "Statistics Canada"
    assert result.metadata.country == "Canada"

    # Data quality checks
    assert result.data[0].date is not None
    assert result.data[0].value is not None
    assert isinstance(result.data[0].value, (int, float))
    assert result.data[0].value > 0, "Retail sales should be positive"


@pytest.mark.asyncio
async def test_fetch_dynamic_data_with_geography_fallback(statscan_provider):
    """Test that fetch_dynamic_data skips products without geography when needed."""
    # Try to fetch employment for Ontario
    result = await statscan_provider.fetch_dynamic_data({
        "indicator": "labour force",
        "geography": "Ontario",
        "periods": 60
    })

    assert result is not None
    assert result.data is not None
    assert len(result.data) > 0
    assert "Ontario" in result.metadata.indicator


@pytest.mark.asyncio
async def test_coordinate_building(statscan_provider):
    """Test that coordinates are properly built for WDS queries."""
    metadata = await statscan_provider._get_cube_metadata("14100287")

    # Build coordinate as fetch_from_product_with_discovery would
    dimensions = metadata.get("dimension", [])
    coordinate_parts = []

    for dim_info in dimensions:
        dim_name = dim_info.get("dimensionNameEn", "").upper()
        members = dim_info.get("member", [])

        if "GEOGR" in dim_name:
            # Try to find Alberta
            found_id = None
            for member in members:
                if "ALBERTA" == member.get("memberNameEn", "").upper():
                    found_id = member.get("memberId")
                    break
            coordinate_parts.append(found_id if found_id else 1)
        else:
            coordinate_parts.append(1)

    # Build coordinate string
    coordinate = ".".join(str(p) for p in coordinate_parts[:10])
    while coordinate.count(".") < 9:
        coordinate += ".0"

    # Verify format
    parts = coordinate.split(".")
    assert len(parts) == 10, f"Coordinate should have 10 parts, got {len(parts)}"
    for part in parts:
        assert part.isdigit(), f"All coordinate parts should be digits, got {part}"

    # Alberta should be in there (ID = 10)
    assert "10" in parts[:2], "Alberta (ID 10) should be in first dimension"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
