import httpx
import pytest

from backend.providers.statscan import StatsCanProvider


class _FailingHttpClient:
    async def get(self, *args, **kwargs):
        raise httpx.ConnectError("offline")

    async def post(self, *args, **kwargs):
        raise httpx.ConnectError("offline")


@pytest.fixture
def statscan_provider():
    return StatsCanProvider()


@pytest.mark.asyncio
async def test_get_cube_metadata_uses_local_cache_for_known_product(monkeypatch, statscan_provider):
    monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _FailingHttpClient())

    metadata = await statscan_provider._get_cube_metadata("1410028701")

    assert str(metadata["productId"]) == "14100287"
    assert metadata["cubeTitleEn"].startswith("Labour force characteristics")


@pytest.mark.asyncio
async def test_search_vectors_falls_back_to_local_catalog(monkeypatch, statscan_provider):
    monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _FailingHttpClient())

    results = await statscan_provider.search_vectors("employment", limit=5)

    assert results
    assert any(result["productId"] == "14100287" for result in results[:3])


def test_select_default_member_id_prefers_employment_series(statscan_provider):
    metadata = statscan_provider._statscan_metadata_service.get_local_cube_metadata("14100287")
    dimensions = metadata["dimension"]
    labour_dimension = next(dim for dim in dimensions if dim["dimensionNameEn"] == "Labour force characteristics")
    statistic_dimension = next(dim for dim in dimensions if dim["dimensionNameEn"] == "Statistics")
    age_dimension = next(dim for dim in dimensions if dim["dimensionNameEn"] == "Age group")

    employment_member = statscan_provider._select_default_member_id(
        labour_dimension["dimensionNameEn"],
        labour_dimension["member"],
        "employment",
    )
    statistic_member = statscan_provider._select_default_member_id(
        statistic_dimension["dimensionNameEn"],
        statistic_dimension["member"],
        "employment",
    )
    age_member = statscan_provider._select_default_member_id(
        age_dimension["dimensionNameEn"],
        age_dimension["member"],
        "employment",
    )

    assert employment_member == 3
    assert statistic_member == 1
    assert age_member == 1


def test_select_default_member_id_prefers_employment_rate_member(statscan_provider):
    metadata = statscan_provider._statscan_metadata_service.get_local_cube_metadata("14100287")
    labour_dimension = next(
        dim for dim in metadata["dimension"] if dim["dimensionNameEn"] == "Labour force characteristics"
    )
    expected_member = statscan_provider._find_member_id_by_keywords(
        labour_dimension["member"],
        ["employment rate"],
    )

    member = statscan_provider._select_default_member_id(
        labour_dimension["dimensionNameEn"],
        labour_dimension["member"],
        "employment rate",
    )

    assert member == expected_member


def test_select_default_member_id_prefers_total_retail_all_stores(statscan_provider):
    metadata = statscan_provider._statscan_metadata_service.get_local_cube_metadata("20100031")
    dimensions = metadata["dimension"]
    store_dimension = next(dim for dim in dimensions if dim["dimensionNameEn"] == "Type of retail store")
    component_dimension = next(dim for dim in dimensions if dim["dimensionNameEn"] == "Retail trade components")
    adjustment_dimension = next(dim for dim in dimensions if dim["dimensionNameEn"] == "Adjustments")

    store_member = statscan_provider._select_default_member_id(
        store_dimension["dimensionNameEn"],
        store_dimension["member"],
        "retail sales",
    )
    component_member = statscan_provider._select_default_member_id(
        component_dimension["dimensionNameEn"],
        component_dimension["member"],
        "retail sales",
    )
    adjustment_member = statscan_provider._select_default_member_id(
        adjustment_dimension["dimensionNameEn"],
        adjustment_dimension["member"],
        "retail sales",
    )

    assert store_member == 1
    assert component_member == 3
    assert adjustment_member == 2


@pytest.mark.asyncio
async def test_fetch_dynamic_data_uses_exact_product_id_without_search(monkeypatch, statscan_provider):
    metadata = {
        "dimension": [
            {
                "dimensionNameEn": "Geography",
                "member": [{"memberId": 1, "memberNameEn": "Canada"}],
            }
        ]
    }

    async def fake_get_cube_metadata(product_id):
        assert product_id == "14100374"
        return metadata

    async def fake_fetch_from_product_with_discovery(**kwargs):
        assert kwargs["product_id"] == "14100374"
        assert kwargs["indicator"] == "employment rate"
        return "ok"

    monkeypatch.setattr(statscan_provider, "_get_cube_metadata", fake_get_cube_metadata)
    monkeypatch.setattr(
        statscan_provider,
        "fetch_from_product_with_discovery",
        fake_fetch_from_product_with_discovery,
    )

    async def fail_search_vectors(*args, **kwargs):
        raise AssertionError("search_vectors should not run for exact product IDs")

    monkeypatch.setattr(statscan_provider, "search_vectors", fail_search_vectors)

    result = await statscan_provider.fetch_dynamic_data(
        {"indicator": "14100374", "indicatorLabel": "employment rate"}
    )

    assert result == "ok"
