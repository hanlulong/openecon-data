import httpx
import pytest

from backend.providers.statscan import StatsCanProvider
from backend.utils.retry import DataNotAvailableError


class _FailingHttpClient:
    async def get(self, *args, **kwargs):
        raise httpx.ConnectError("offline")

    async def post(self, *args, **kwargs):
        raise httpx.ConnectError("offline")


class _MockResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://www150.statcan.gc.ca"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _MockPostClient:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    async def post(self, *args, **kwargs):
        return _MockResponse(self.payload, self.status_code)


@pytest.fixture
def statscan_provider():
    return StatsCanProvider()


# ---------- Labour force product metadata helper ----------
def _get_labour_metadata(statscan_provider):
    """Get locally-cached metadata for Labour Force Survey (14100287)."""
    return statscan_provider._statscan_metadata_service.get_local_cube_metadata("14100287")


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
    assert any("employment" in result["title"].lower() for result in results[:3])


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


def test_select_default_member_id_handles_underscored_indicator(statscan_provider):
    """When indicator has underscores (e.g., 'unemployment_rate'), it should still
    match 'Unemployment rate' member, not 'Unemployment' (count)."""
    metadata = statscan_provider._statscan_metadata_service.get_local_cube_metadata("14100287")
    labour_dimension = next(
        dim for dim in metadata["dimension"] if dim["dimensionNameEn"] == "Labour force characteristics"
    )

    # With underscored indicator labels, match the provider member exactly.
    member_underscored = statscan_provider._select_default_member_id(
        labour_dimension["dimensionNameEn"],
        labour_dimension["member"],
        "unemployment_rate",
    )
    # Should select "Unemployment rate" (member 7), NOT "Unemployment" (member 6)
    assert member_underscored == 7

    # Same for employment_rate
    member_emp = statscan_provider._select_default_member_id(
        labour_dimension["dimensionNameEn"],
        labour_dimension["member"],
        "employment_rate",
    )
    # Should select "Employment rate" (member 9), NOT "Employment" (member 3)
    assert member_emp == 9


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


def test_select_default_member_id_avoids_statistical_difference_characteristic(statscan_provider):
    members = [
        {"memberId": 1, "memberNameEn": "Number of persons"},
        {"memberId": 2, "memberNameEn": "Low 95% confidence interval, number of persons"},
        {"memberId": 3, "memberNameEn": "High 95% confidence interval, number of persons"},
        {"memberId": 4, "memberNameEn": "Percent"},
        {"memberId": 5, "memberNameEn": "Low 95% confidence interval, percent"},
        {"memberId": 6, "memberNameEn": "High 95% confidence interval, percent"},
        {"memberId": 7, "memberNameEn": "Statistically different from the Canada (excluding territories) rate"},
    ]

    member = statscan_provider._select_default_member_id(
        "Characteristics",
        members,
        "health indicator statistics, annual estimates",
    )

    assert member == 1


def test_extract_dimension_modifiers_ignores_exact_table_title_token_overlap(statscan_provider):
    metadata = {
        "dimension": [
            {
                "dimensionNameEn": "Commodities and commodity groups",
                "member": [
                    {"memberId": 2, "memberNameEn": "All-items"},
                    {"memberId": 299, "memberNameEn": "Core Consumer Price Index (CPI) (Bank of Canada definition)"},
                ],
            },
        ]
    }

    modifiers = statscan_provider.extract_dimension_modifiers(
        "Canada annual Consumer Price Index (CPI) 2001 basket content from Statistics Canada",
        "Consumer Price Index (CPI), 2001 basket content, annual",
        "18100009",
        metadata,
    )

    assert modifiers == {}


@pytest.mark.asyncio
async def test_fetch_multi_province_data_rejects_explicit_unsupported_geography_for_product(statscan_provider):
    with pytest.raises(ValueError, match="does not expose geography 'Yukon'"):
        await statscan_provider.fetch_multi_province_data(
            {
                "productId": "14100287",
                "indicator": "14100287",
                "indicatorLabel": "unemployment rate",
                "provinces": ["Yukon"],
                "periods": 20,
            }
        )


@pytest.mark.asyncio
async def test_fetch_multi_province_data_fails_closed_on_incomplete_batch_payload(monkeypatch, statscan_provider):
    payload = [
        {
            "status": "SUCCESS",
            "object": {
                "coordinate": "7.7.1.1.1.1.0.0.0.0",
                "vectorDataPoint": [
                    {
                        "refPer": "2026-03-01",
                        "value": 6.8,
                        "frequencyCode": 6,
                        "scalarFactorCode": 0,
                        "releaseTime": "2026-04-10T08:30",
                    }
                ],
            },
        }
    ]
    monkeypatch.setattr(
        "backend.providers.statscan.get_http_client",
        lambda: _MockPostClient(payload),
    )

    with pytest.raises(DataNotAvailableError, match="incomplete province coverage"):
        await statscan_provider.fetch_multi_province_data(
            {
                "productId": "14100287",
                "indicator": "14100287",
                "indicatorLabel": "unemployment rate",
                "provinces": ["Ontario", "Alberta"],
                "periods": 20,
            }
        )


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


@pytest.mark.asyncio
async def test_fetch_dynamic_data_exact_product_uses_metadata_geography_default(monkeypatch, statscan_provider):
    metadata = {
        "dimension": [
            {
                "dimensionNameEn": "Geography",
                "member": [
                    {"memberId": 2, "memberNameEn": "Canada"},
                    {"memberId": 3, "memberNameEn": "Ontario", "parentMemberId": 2},
                ],
            },
            {
                "dimensionNameEn": "Commodities and commodity groups",
                "member": [
                    {"memberId": 2, "memberNameEn": "All-items"},
                    {"memberId": 3, "memberNameEn": "Food", "parentMemberId": 2},
                ],
            },
        ]
    }
    captured_requests = []

    async def fake_get_cube_metadata(product_id):
        assert product_id == "18100009"
        return metadata

    class _MockResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{
                "status": "SUCCESS",
                "object": {
                    "coordinate": "2.2.0.0.0.0.0.0.0.0",
                    "vectorDataPoint": [
                        {
                            "refPer": "2006-01-01",
                            "value": 129.9,
                            "frequencyCode": 12,
                            "scalarFactorCode": 0,
                            "releaseTime": "2006-02-01",
                        }
                    ],
                },
            }]

    class _MockClient:
        async def post(self, url, json=None, **kwargs):
            captured_requests.append(json)
            return _MockResponse()

    monkeypatch.setattr(statscan_provider, "_get_cube_metadata", fake_get_cube_metadata)
    monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())

    result = await statscan_provider.fetch_dynamic_data(
        {
            "indicator": "18100009",
            "indicatorLabel": "Consumer Price Index (CPI), 2001 basket content, annual",
            "periods": 240,
        }
    )

    assert captured_requests[0][0]["coordinate"] == "2.2.0.0.0.0.0.0.0.0"
    assert result.data
    assert result.metadata.seriesId == "18100009:2.2.0.0.0.0.0.0.0.0"


@pytest.mark.asyncio
async def test_fetch_dynamic_data_exact_product_falls_back_to_valid_coordinate(monkeypatch, statscan_provider):
    metadata = {
        "dimension": [
            {
                "dimensionNameEn": "Geography",
                "member": [
                    {"memberId": 1, "memberNameEn": "Montreal"},
                    {"memberId": 2, "memberNameEn": "Toronto"},
                ],
            },
            {
                "dimensionNameEn": "Type of livestock",
                "member": [
                    {"memberId": 1, "memberNameEn": "Slaughter steers, good"},
                    {"memberId": 2, "memberNameEn": "Slaughter cows, good"},
                ],
            },
        ]
    }
    captured_requests = []

    async def fake_get_cube_metadata(product_id):
        assert product_id == "32100322"
        return metadata

    class _FallbackClient:
        async def post(self, url, json=None, **kwargs):
            captured_requests.append(json)
            if len(json) == 1:
                return _MockResponse([
                    {
                        "status": "FAILED",
                        "object": {
                            "responseStatusCode": 2,
                            "coordinate": "1.1.0.0.0.0.0.0.0.0",
                            "vectorDataPoint": [],
                        },
                    }
                ])
            assert any(item["coordinate"] == "2.1.0.0.0.0.0.0.0.0" for item in json)
            return _MockResponse([
                {
                    "status": "SUCCESS",
                    "object": {
                        "coordinate": "2.1.0.0.0.0.0.0.0.0",
                        "vectorDataPoint": [
                            {
                                "refPer": "1990-12-01",
                                "value": 93.58,
                                "frequencyCode": 6,
                                "scalarFactorCode": 0,
                                "releaseTime": "2000-02-18T19:41",
                            }
                        ],
                    },
                }
            ])

    monkeypatch.setattr(statscan_provider, "_get_cube_metadata", fake_get_cube_metadata)
    monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _FallbackClient())

    result = await statscan_provider.fetch_dynamic_data(
        {
            "indicator": "32100322",
            "indicatorLabel": "Average prices for selected classes and grades of cattle, monthly",
            "periods": 240,
        }
    )

    assert captured_requests[0][0]["coordinate"] == "1.1.0.0.0.0.0.0.0.0"
    assert result.data
    assert result.metadata.seriesId == "32100322:2.1.0.0.0.0.0.0.0.0"




@pytest.mark.asyncio
async def test_exact_product_falls_back_to_full_table_csv_when_wds_metadata_times_out(monkeypatch, statscan_provider):
    csv_payload = (
        'REF_DATE,GEO,DGUID,Age group,Sex,Characteristics,UOM,UOM_ID,SCALAR_FACTOR,SCALAR_ID,VECTOR,COORDINATE,VALUE,STATUS,SYMBOL,TERMINATED,DECIMALS\n'
        '2000,Canada,00,"Total, 12 years and over",Both sexes,Percent,Percent,239,units,0,v1,1.1.1,100.0,,,,0\n'
        '2001,Canada,00,"Total, 12 years and over",Both sexes,Percent,Percent,239,units,0,v1,1.1.1,98.0,,,,0\n'
    )
    metadata_payload = (
        'Cube Title,Product Id,CANSIM Id,URL,Cube Notes,Archive Status,Frequency,Start Reference Period,End Reference Period,Total number of dimensions\n'
        'Exact title table,13100071,,https://example.test,,,,2000,2001,3\n\n'
        'Dimension ID,Dimension name,Dimension Notes,Dimension Correction Notes,Dimension Definitions\n'
        '1,Geography,,,\n'
        '2,Age group,,,\n'
        '3,Sex,,,\n\n'
        'Dimension ID,Member Name,Classification Code,Member ID,Parent Member ID,Terminated,Member Notes,Member Correction Notes,Member Geo Attribute Keys,Member Definitions\n'
        '1,Canada,,1,,,,,,\n'
        '2,"Total, 12 years and over",,1,,,,,,\n'
        '3,Both sexes,,1,,,,,,\n'
    )

    import io
    import zipfile

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as archive:
        archive.writestr('13100071.csv', csv_payload)
        archive.writestr('13100071_MetaData.csv', metadata_payload)

    class _TimeoutResponse:
        status_code = 200
        content = zip_buffer.getvalue()

        def raise_for_status(self):
            pass

    class _FallbackClient:
        async def post(self, *args, **kwargs):
            raise TimeoutError('metadata read timed out')

        async def get(self, *args, **kwargs):
            return _TimeoutResponse()

    monkeypatch.setattr('backend.providers.statscan.get_http_client', lambda: _FallbackClient())

    result = await statscan_provider.fetch_dynamic_data({
        'indicator': '13100071',
        'indicatorLabel': 'Exact title table',
        'periods': 240,
    })

    assert result.metadata.source == 'Statistics Canada'
    assert result.metadata.seriesId == '13100071:1.1.1'
    assert result.metadata.apiUrl.endswith('(direct full-table CSV fallback)')
    assert [point.date for point in result.data] == ['2000', '2001']
    assert [point.value for point in result.data] == [100.0, 98.0]


@pytest.mark.asyncio
async def test_full_table_csv_exact_fallback_uses_first_provider_series_not_semantic_default(monkeypatch, statscan_provider):
    statscan_provider._cube_metadata_cache["13100071"] = {
        "dimension": [
            {
                "dimensionNameEn": "Geography",
                "member": [
                    {"memberId": 1, "memberNameEn": "Canada"},
                    {"memberId": 7, "memberNameEn": "Ontario"},
                ],
            },
            {
                "dimensionNameEn": "Sex",
                "member": [
                    {"memberId": 1, "memberNameEn": "Both sexes"},
                    {"memberId": 9, "memberNameEn": "Women+"},
                ],
            },
        ]
    }
    rows = [
        {
            "REF_DATE": "2000",
            "GEO": "Ontario",
            "UOM": "Percent",
            "SCALAR_FACTOR": "units",
            "SCALAR_ID": "0",
            "VECTOR": "v7",
            "COORDINATE": "7.9",
            "VALUE": "10.0",
        },
        {
            "REF_DATE": "2001",
            "GEO": "Ontario",
            "UOM": "Percent",
            "SCALAR_FACTOR": "units",
            "SCALAR_ID": "0",
            "VECTOR": "v7",
            "COORDINATE": "7.9",
            "VALUE": "11.0",
        },
        {
            "REF_DATE": "2000",
            "GEO": "Canada",
            "UOM": "Percent",
            "SCALAR_FACTOR": "units",
            "SCALAR_ID": "0",
            "VECTOR": "v1",
            "COORDINATE": "1.1",
            "VALUE": "99.0",
        },
    ]

    async def fake_rows(product_id):
        return rows, "https://www150.statcan.gc.ca/n1/tbl/csv/13100071-eng.zip"

    monkeypatch.setattr(statscan_provider, "_get_full_table_csv_rows", fake_rows)

    result = await statscan_provider.fetch_full_table_csv_data({
        "productId": "13100071",
        "indicatorLabel": "Exact provider table title",
        "periods": 10,
    })

    assert result.metadata.seriesId == "13100071:7.9"
    assert result.metadata.country == "Ontario"
    assert [point.date for point in result.data] == ["2000", "2001"]
    assert [point.value for point in result.data] == [10.0, 11.0]


@pytest.mark.asyncio
async def test_full_table_csv_respects_explicit_canada_geography(monkeypatch, statscan_provider):
    rows = [
        {
            "REF_DATE": "2000",
            "GEO": "Austria",
            "UOM": "Percent",
            "SCALAR_FACTOR": "units",
            "SCALAR_ID": "0",
            "VECTOR": "v_at",
            "COORDINATE": "1.1.1.1",
            "VALUE": "5.0",
        },
        {
            "REF_DATE": "2000",
            "GEO": "Canada",
            "UOM": "Percent",
            "SCALAR_FACTOR": "units",
            "SCALAR_ID": "0",
            "VECTOR": "v_ca",
            "COORDINATE": "2.1.1.1",
            "VALUE": "9.0",
        },
        {
            "REF_DATE": "2001",
            "GEO": "Canada",
            "UOM": "Percent",
            "SCALAR_FACTOR": "units",
            "SCALAR_ID": "0",
            "VECTOR": "v_ca",
            "COORDINATE": "2.1.1.1",
            "VALUE": "10.0",
        },
    ]

    async def fake_rows(product_id):
        return rows, "https://www150.statcan.gc.ca/n1/tbl/csv/13100287-eng.zip"

    monkeypatch.setattr(statscan_provider, "_get_full_table_csv_rows", fake_rows)

    result = await statscan_provider.fetch_full_table_csv_data({
        "productId": "13100287",
        "indicatorLabel": "Exact provider table title",
        "geography": "CA",
        "periods": 10,
    })

    assert result.metadata.seriesId == "13100287:2.1.1.1"
    assert result.metadata.country == "Canada"
    assert [point.date for point in result.data] == ["2000", "2001"]
    assert [point.value for point in result.data] == [9.0, 10.0]


@pytest.mark.asyncio
async def test_full_table_csv_fails_closed_when_explicit_geography_has_no_rows(monkeypatch, statscan_provider):
    rows = [
        {
            "REF_DATE": "2000",
            "GEO": "Austria",
            "UOM": "Percent",
            "SCALAR_FACTOR": "units",
            "SCALAR_ID": "0",
            "VECTOR": "v_at",
            "COORDINATE": "1.1.1.1",
            "VALUE": "5.0",
        },
    ]

    async def fake_rows(product_id):
        return rows, "https://www150.statcan.gc.ca/n1/tbl/csv/13100287-eng.zip"

    monkeypatch.setattr(statscan_provider, "_get_full_table_csv_rows", fake_rows)

    with pytest.raises(DataNotAvailableError):
        await statscan_provider.fetch_full_table_csv_data({
            "productId": "13100287",
            "indicatorLabel": "Exact provider table title",
            "geography": "Canada",
            "periods": 10,
        })


@pytest.mark.asyncio
async def test_full_table_csv_rejects_oversize_uncompressed_members(monkeypatch, statscan_provider):
    old_max = statscan_provider.FULL_TABLE_CSV_MAX_BYTES
    statscan_provider.FULL_TABLE_CSV_MAX_BYTES = 100
    try:
        import io
        import zipfile

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "13100071.csv",
                "REF_DATE,GEO,COORDINATE,VALUE\n2000,Canada,1.1,1\n" + ("#" * 200),
            )
            archive.writestr("13100071_MetaData.csv", "Cube Title\nOversize\n")

        class _OversizeResponse:
            status_code = 200
            content = zip_buffer.getvalue()

            def raise_for_status(self):
                pass

        class _OversizeClient:
            async def get(self, *args, **kwargs):
                return _OversizeResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _OversizeClient())

        with pytest.raises(DataNotAvailableError, match="exceeds the safe exact-table fallback size"):
            await statscan_provider._download_full_table_csv_bundle("13100071")
    finally:
        statscan_provider.FULL_TABLE_CSV_MAX_BYTES = old_max


@pytest.mark.asyncio
async def test_fetch_series_wraps_statscan_503_as_data_not_available(monkeypatch, statscan_provider):
    monkeypatch.setattr(
        "backend.providers.statscan.get_http_client",
        lambda: _MockPostClient({"error": "temporary outage"}, status_code=503),
    )

    with pytest.raises(DataNotAvailableError, match="temporarily unavailable"):
        await statscan_provider.fetch_series({"indicator": "32100095", "periods": 12})


@pytest.mark.asyncio
async def test_fetch_from_product_with_discovery_wraps_statscan_503_as_data_not_available(
    monkeypatch,
    statscan_provider,
):
    metadata = {
        "dimension": [
            {
                "dimensionNameEn": "Geography",
                "member": [{"memberId": 1, "memberNameEn": "Canada"}],
            }
        ]
    }
    monkeypatch.setattr(
        "backend.providers.statscan.get_http_client",
        lambda: _MockPostClient({"error": "temporary outage"}, status_code=503),
    )

    with pytest.raises(DataNotAvailableError, match="temporarily unavailable"):
        await statscan_provider.fetch_from_product_with_discovery(
            product_id="11100024",
            indicator="Low income entry and exit rates of tax filers in Canada",
            metadata=metadata,
            geography=None,
            periods=12,
        )


# =====================================================================
# Tests for extract_dimension_modifiers (metadata-driven, no hardcoded lists)
# =====================================================================


class TestExtractDimensionModifiers:
    """Tests that dimension modifiers are extracted from query text using actual table metadata."""

    def test_extract_geography_ontario(self, statscan_provider):
        """'Ontario' in query should match the Geography dimension."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="unemployment rate Ontario",
            base_indicator="UNEMPLOYMENT_RATE",
            product_id="14100287",
            cube_metadata=metadata,
        )
        assert "geography" in modifiers
        assert "ontario" in modifiers["geography"].lower()

    def test_extract_geography_alberta(self, statscan_provider):
        """'Alberta' in query should match the Geography dimension."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="employment rate in Alberta Canada",
            base_indicator="EMPLOYMENT",
            product_id="14100287",
            cube_metadata=metadata,
        )
        assert "geography" in modifiers
        assert "alberta" in modifiers["geography"].lower()

    def test_extract_geography_prefers_exact_province_over_longer_subregional_member(self, statscan_provider):
        metadata = {
            "dimension": [
                {
                    "dimensionNameEn": "Geography",
                    "member": [
                        {"memberId": 1, "memberNameEn": "Canada"},
                        {"memberId": 7, "memberNameEn": "Ontario"},
                        {"memberId": 701, "memberNameEn": "Ontario by Local Health Integration Network"},
                    ],
                }
            ]
        }
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="show only Ontario",
            base_indicator="EMPLOYMENT",
            product_id="14100287",
            cube_metadata=metadata,
        )
        assert modifiers.get("geography") == "ONTARIO" or modifiers.get("geography") == "Ontario"

    def test_extract_gender_male(self, statscan_provider):
        """Exact provider member 'Men+' should match the Gender/Sex dimension mechanically."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="unemployment rate Men+ Canada",
            base_indicator="UNEMPLOYMENT_RATE",
            product_id="14100287",
            cube_metadata=metadata,
        )
        # Should have detected a gender-related modifier
        gender_keys = [k for k in modifiers if k in ("gender", "sex")]
        assert len(gender_keys) > 0, f"Expected gender modifier, got: {modifiers}"

    def test_extract_gender_female(self, statscan_provider):
        """Exact provider member 'Women+' should match the Gender/Sex dimension mechanically."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="employment rate Women+ Canada",
            base_indicator="EMPLOYMENT",
            product_id="14100287",
            cube_metadata=metadata,
        )
        gender_keys = [k for k in modifiers if k in ("gender", "sex")]
        assert len(gender_keys) > 0, f"Expected gender modifier, got: {modifiers}"

    def test_extract_age_youth(self, statscan_provider):
        """Exact provider age member should match the Age group dimension mechanically."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="unemployment rate 15 to 24 years Canada",
            base_indicator="UNEMPLOYMENT_RATE",
            product_id="14100287",
            cube_metadata=metadata,
        )
        age_keys = [k for k in modifiers if k in ("age",)]
        assert len(age_keys) > 0, f"Expected age modifier, got: {modifiers}"

    def test_extract_age_numeric_range_alias(self, statscan_provider):
        """Hyphenated age ranges should match StatsCan age-group members generically."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="show only 25-54",
            base_indicator="EMPLOYMENT",
            product_id="14100287",
            cube_metadata=metadata,
        )
        assert modifiers.get("age") == "25 to 54 years"

    def test_extract_combined_modifiers(self, statscan_provider):
        """Multiple modifiers should be extracted simultaneously."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="unemployment rate Women+ Ontario Canada",
            base_indicator="UNEMPLOYMENT_RATE",
            product_id="14100287",
            cube_metadata=metadata,
        )
        # Should have geography AND gender
        assert "geography" in modifiers, f"Expected geography, got: {modifiers}"
        assert "ontario" in modifiers["geography"].lower()
        gender_keys = [k for k in modifiers if k in ("gender", "sex")]
        assert len(gender_keys) > 0, f"Expected gender modifier, got: {modifiers}"

    def test_no_modifiers_basic_query(self, statscan_provider):
        """A basic query without modifiers should return empty dict."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="unemployment rate Canada",
            base_indicator="UNEMPLOYMENT_RATE",
            product_id="14100287",
            cube_metadata=metadata,
        )
        # 'Canada' is the default/total member, so it should not be extracted as a modifier
        assert "geography" not in modifiers, f"'Canada' should not be a modifier, got: {modifiers}"

    def test_no_modifiers_empty_query(self, statscan_provider):
        """Empty query should return empty dict."""
        metadata = _get_labour_metadata(statscan_provider)
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="",
            base_indicator="UNEMPLOYMENT_RATE",
            product_id="14100287",
            cube_metadata=metadata,
        )
        assert modifiers == {}

    def test_no_modifiers_none_metadata(self, statscan_provider):
        """None metadata should return empty dict."""
        modifiers = statscan_provider.extract_dimension_modifiers(
            query_text="unemployment rate male Ontario",
            base_indicator="UNEMPLOYMENT_RATE",
            product_id="14100287",
            cube_metadata=None,
        )
        assert modifiers == {}


# =====================================================================
# Tests for fetch_with_dimensions coordinate building (offline)
# =====================================================================


class TestFetchWithDimensionsCoordinates:
    """Test that fetch_with_dimensions builds correct coordinates from modifiers."""

    @pytest.mark.asyncio
    async def test_geography_modifier_builds_correct_coordinate(self, monkeypatch, statscan_provider):
        """Ontario modifier should place the Ontario member ID in the geography dimension."""
        metadata = _get_labour_metadata(statscan_provider)

        # Mock HTTP to capture the coordinate
        captured_requests = []

        class _MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return [{
                    "status": "SUCCESS",
                    "object": {
                        "vectorDataPoint": [
                            {"refPer": "2024-01-01", "value": 5.5, "frequencyCode": 6,
                             "scalarFactorCode": 0, "releaseTime": "2024-02-01"},
                        ]
                    }
                }]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                captured_requests.append(json)
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        # Ensure metadata is in cache
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        result = await statscan_provider.fetch_with_dimensions(
            base_indicator="UNEMPLOYMENT_RATE",
            modifiers={"geography": "Ontario"},
            product_id="14100287",
        )

        assert result is not None
        assert len(captured_requests) == 1
        coord = captured_requests[0][0]["coordinate"]
        parts = coord.split(".")

        # Ontario is member ID 7 in product 14100287
        geo_dim_idx = next(
            i for i, d in enumerate(metadata["dimension"])
            if "geogr" in d["dimensionNameEn"].lower()
        )
        assert parts[geo_dim_idx] == "7", f"Ontario should be member 7, got {parts[geo_dim_idx]} in coordinate {coord}"

    @pytest.mark.asyncio
    async def test_gender_modifier_builds_correct_coordinate(self, monkeypatch, statscan_provider):
        """Male modifier should set the gender dimension correctly."""
        metadata = _get_labour_metadata(statscan_provider)

        captured_requests = []

        class _MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return [{
                    "status": "SUCCESS",
                    "object": {
                        "vectorDataPoint": [
                            {"refPer": "2024-01-01", "value": 5.5, "frequencyCode": 6,
                             "scalarFactorCode": 0, "releaseTime": "2024-02-01"},
                        ]
                    }
                }]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                captured_requests.append(json)
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        result = await statscan_provider.fetch_with_dimensions(
            base_indicator="UNEMPLOYMENT_RATE",
            modifiers={"gender": "Men+"},
            product_id="14100287",
        )

        assert result is not None
        coord = captured_requests[0][0]["coordinate"]
        parts = coord.split(".")

        # Gender dimension: Men+ is member 2
        gender_dim_idx = next(
            i for i, d in enumerate(metadata["dimension"])
            if "gender" in d["dimensionNameEn"].lower()
        )
        assert parts[gender_dim_idx] == "2", f"Male/Men+ should be member 2, got {parts[gender_dim_idx]} in coordinate {coord}"

    @pytest.mark.asyncio
    async def test_combined_modifiers_build_correct_coordinate(self, monkeypatch, statscan_provider):
        """Multiple modifiers should each affect their respective dimension."""
        metadata = _get_labour_metadata(statscan_provider)

        captured_requests = []

        class _MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return [{
                    "status": "SUCCESS",
                    "object": {
                        "vectorDataPoint": [
                            {"refPer": "2024-01-01", "value": 5.5, "frequencyCode": 6,
                             "scalarFactorCode": 0, "releaseTime": "2024-02-01"},
                        ]
                    }
                }]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                captured_requests.append(json)
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        result = await statscan_provider.fetch_with_dimensions(
            base_indicator="UNEMPLOYMENT_RATE",
            modifiers={"geography": "Ontario", "gender": "Women+", "age": "15 to 24 years"},
            product_id="14100287",
        )

        assert result is not None
        coord = captured_requests[0][0]["coordinate"]
        parts = coord.split(".")

        geo_idx = next(i for i, d in enumerate(metadata["dimension"]) if "geogr" in d["dimensionNameEn"].lower())
        gender_idx = next(i for i, d in enumerate(metadata["dimension"]) if "gender" in d["dimensionNameEn"].lower())
        age_idx = next(i for i, d in enumerate(metadata["dimension"]) if "age" in d["dimensionNameEn"].lower())

        assert parts[geo_idx] == "7", f"Ontario=7, got {parts[geo_idx]}"
        assert parts[gender_idx] == "3", f"Women+=3, got {parts[gender_idx]}"
        assert parts[age_idx] == "2", f"Youth (15 to 24)=2, got {parts[age_idx]}"

    @pytest.mark.asyncio
    async def test_coordinate_padded_to_10(self, monkeypatch, statscan_provider):
        """Coordinate should always have exactly 10 dot-separated parts."""
        metadata = _get_labour_metadata(statscan_provider)

        captured_requests = []

        class _MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return [{
                    "status": "SUCCESS",
                    "object": {
                        "vectorDataPoint": [
                            {"refPer": "2024-01-01", "value": 5.5, "frequencyCode": 6,
                             "scalarFactorCode": 0, "releaseTime": "2024-02-01"},
                        ]
                    }
                }]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                captured_requests.append(json)
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        await statscan_provider.fetch_with_dimensions(
            base_indicator="UNEMPLOYMENT_RATE",
            modifiers={"geography": "Alberta"},
            product_id="14100287",
        )

        coord = captured_requests[0][0]["coordinate"]
        parts = coord.split(".")
        assert len(parts) == 10, f"Coordinate should have 10 parts, got {len(parts)}: {coord}"

    @pytest.mark.asyncio
    async def test_defaults_used_for_unspecified_dimensions(self, monkeypatch, statscan_provider):
        """Unspecified dimensions should use semantic defaults, not blindly 1."""
        metadata = _get_labour_metadata(statscan_provider)

        captured_requests = []

        class _MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return [{
                    "status": "SUCCESS",
                    "object": {
                        "vectorDataPoint": [
                            {"refPer": "2024-01-01", "value": 5.5, "frequencyCode": 6,
                             "scalarFactorCode": 0, "releaseTime": "2024-02-01"},
                        ]
                    }
                }]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                captured_requests.append(json)
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        await statscan_provider.fetch_with_dimensions(
            base_indicator="UNEMPLOYMENT_RATE",
            modifiers={"geography": "Ontario"},
            product_id="14100287",
        )

        coord = captured_requests[0][0]["coordinate"]
        parts = coord.split(".")

        # Labour force characteristic dimension should default to "Unemployment rate" (member 7)
        labour_idx = next(
            i for i, d in enumerate(metadata["dimension"])
            if "labour force" in d["dimensionNameEn"].lower()
        )
        labour_dim = metadata["dimension"][labour_idx]
        unemp_member = statscan_provider._find_member_id_by_keywords(
            labour_dim["member"], ["unemployment rate"]
        )
        assert parts[labour_idx] == str(unemp_member), (
            f"Labour characteristic should default to unemployment rate (member {unemp_member}), "
            f"got {parts[labour_idx]}"
        )

    @pytest.mark.asyncio
    async def test_metadata_indicator_name_in_result(self, monkeypatch, statscan_provider):
        """Result metadata should include the dimension descriptions."""
        metadata = _get_labour_metadata(statscan_provider)

        class _MockResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return [{
                    "status": "SUCCESS",
                    "object": {
                        "vectorDataPoint": [
                            {"refPer": "2024-01-01", "value": 5.5, "frequencyCode": 6,
                             "scalarFactorCode": 0, "releaseTime": "2024-02-01"},
                        ]
                    }
                }]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        result = await statscan_provider.fetch_with_dimensions(
            base_indicator="UNEMPLOYMENT_RATE",
            modifiers={"geography": "Ontario"},
            product_id="14100287",
        )

        # Indicator name should mention Ontario
        assert "Ontario" in result.metadata.indicator, (
            f"Expected 'Ontario' in indicator name, got: {result.metadata.indicator}"
        )
        assert result.metadata.source == "Statistics Canada"


class TestFetchMultiDimensionData:
    @pytest.mark.asyncio
    async def test_age_group_decomposition_preserves_fixed_geography(self, monkeypatch, statscan_provider):
        metadata = _get_labour_metadata(statscan_provider)
        captured_requests = []

        class _MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                payload = []
                for idx, request in enumerate(captured_requests[0]):
                    payload.append(
                        {
                            "status": "SUCCESS",
                            "object": {
                                "coordinate": request["coordinate"],
                                "vectorDataPoint": [
                                    {
                                        "refPer": "2024-01-01",
                                        "value": 60.0 + idx,
                                        "frequencyCode": 6,
                                        "scalarFactorCode": 0,
                                        "releaseTime": "2024-02-01",
                                    }
                                ],
                            },
                        }
                    )
                return payload

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                captured_requests.append(json)
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        results = await statscan_provider.fetch_multi_dimension_data(
            {
                "productId": "14100287",
                "indicator": "EMPLOYMENT_RATE",
                "indicatorLabel": "employment rate",
                "axis": "Age group",
                "dimensions": {"Geography": "Ontario"},
                "periods": 24,
            }
        )

        assert len(results) >= 2
        coords = [request["coordinate"] for request in captured_requests[0]]
        geo_idx = next(i for i, d in enumerate(metadata["dimension"]) if "geogr" in d["dimensionNameEn"].lower())
        age_idx = next(i for i, d in enumerate(metadata["dimension"]) if "age" in d["dimensionNameEn"].lower())
        age_members = {coord.split(".")[age_idx] for coord in coords}
        assert all(coord.split(".")[geo_idx] == "7" for coord in coords)
        assert "1" not in age_members  # excludes aggregate "15 years and over"
        assert len(age_members) >= 2
        assert any("Ontario" in result.metadata.indicator for result in results)
        assert any("25 to 54 years" in result.metadata.indicator for result in results)
        assert {result.metadata.country for result in results} == {"Ontario"}

    @pytest.mark.asyncio
    async def test_multi_dimension_fetch_reports_fixed_geography_in_metadata_country(self, monkeypatch, statscan_provider):
        metadata = _get_labour_metadata(statscan_provider)
        captured_requests = []

        class _MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return [
                    {
                        "status": "SUCCESS",
                        "object": {
                            "coordinate": request["coordinate"],
                            "vectorDataPoint": [
                                {
                                    "refPer": "2025-01-01",
                                    "value": 100.0 + idx,
                                    "frequencyCode": 6,
                                    "scalarFactorCode": 0,
                                    "releaseTime": "2026-01-01",
                                }
                            ],
                        },
                    }
                    for idx, request in enumerate(captured_requests[0])
                ]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                captured_requests.append(json)
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        results = await statscan_provider.fetch_multi_dimension_data(
            {
                "productId": "14100287",
                "indicator": "EMPLOYMENT",
                "indicatorLabel": "employment",
                "axis": "Gender",
                "dimensions": {"geography": "Ontario"},
                "periods": 24,
            }
        )

        assert len(results) == 2
        assert {result.metadata.country for result in results} == {"Ontario"}
        assert all(result.metadata.indicator.startswith("employment - Ontario, ") for result in results)
        geo_idx = next(i for i, d in enumerate(metadata["dimension"]) if "geogr" in d["dimensionNameEn"].lower())
        assert {request["coordinate"].split(".")[geo_idx] for request in captured_requests[0]} == {"7"}

    @pytest.mark.asyncio
    async def test_high_cardinality_required_dimension_without_aggregate_fails_closed(self, monkeypatch, statscan_provider):
        metadata = {
            "productId": "17100147",
            "cubeTitleEn": "First names at birth by sex at birth, selected indicators",
            "dimension": [
                {
                    "dimensionNameEn": "Geography, place of residence of mother",
                    "member": [{"memberId": 1, "memberNameEn": "Canada"}],
                },
                {
                    "dimensionNameEn": "Sex at birth",
                    "member": [
                        {"memberId": 1, "memberNameEn": "Male"},
                        {"memberId": 2, "memberNameEn": "Female"},
                    ],
                },
                {
                    "dimensionNameEn": "First name at birth",
                    "member": [
                        {"memberId": idx, "memberNameEn": f"Name {idx}"}
                        for idx in range(1, 125)
                    ],
                },
                {
                    "dimensionNameEn": "Indicator",
                    "member": [
                        {"memberId": 1, "memberNameEn": "Frequency"},
                        {"memberId": 2, "memberNameEn": "Rank"},
                        {"memberId": 3, "memberNameEn": "Proportion"},
                    ],
                },
            ],
        }

        class _UnexpectedClient:
            async def post(self, *args, **kwargs):
                raise AssertionError("required-dimension supportability block should happen before WDS data request")

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _UnexpectedClient())
        statscan_provider._cube_metadata_cache["17100147"] = metadata

        with pytest.raises(DataNotAvailableError) as raised:
            await statscan_provider.fetch_multi_dimension_data(
                {
                    "productId": "17100147",
                    "indicator": "17100147",
                    "indicatorLabel": "First names at birth by sex at birth, selected indicators",
                    "axis": "Sex",
                    "dimensions": {},
                    "periods": 24,
                }
            )

        message = str(raised.value)
        assert "statscan_required_dimension_missing" in message
        assert "First name at birth" in message


class TestFetchCategoricalData:
    @pytest.mark.asyncio
    async def test_capitalized_dimension_keys_preserve_semantic_indicator_label(self, monkeypatch, statscan_provider):
        metadata = _get_labour_metadata(statscan_provider)

        class _MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return [{
                    "status": "SUCCESS",
                    "object": {
                        "vectorDataPoint": [
                            {
                                "refPer": "2024-01-01",
                                "value": 63.2,
                                "frequencyCode": 6,
                                "scalarFactorCode": 0,
                                "releaseTime": "2024-02-01",
                            }
                        ]
                    }
                }]

        class _MockClient:
            async def post(self, url, json=None, **kwargs):
                return _MockResponse()

        monkeypatch.setattr("backend.providers.statscan.get_http_client", lambda: _MockClient())
        statscan_provider._cube_metadata_cache["14100287"] = metadata

        result = await statscan_provider.fetch_categorical_data(
            {
                "productId": "14100287",
                "indicator": "14100287",
                "indicatorLabel": "employment rate",
                "dimensions": {"Geography": "Ontario", "Age group": "25 to 54 years"},
                "periods": 24,
            }
        )

        assert result.metadata.indicator == "Ontario aged 25 to 54 years employment rate"
        assert result.metadata.description == "Ontario aged 25 to 54 years employment rate"
        assert result.metadata.country == "Ontario"
