"""Multi-series exports: linear time, no dropped series, no lost dates."""

import io
import time

import httpx
import pandas as pd

from backend.models import Metadata, NormalizedData
from backend.services.export import ExportService

svc = ExportService()


def _series(series_id, country, dates, indicator="GDP", start=0.0):
    return NormalizedData(
        metadata=Metadata(
            source="Test",
            indicator=indicator,
            country=country,
            frequency="monthly",
            unit="units",
            lastUpdated="",
            seriesId=series_id,
            apiUrl="",
        ),
        data=[{"date": d, "value": start + i} for i, d in enumerate(dates)],
    )


def _read_dta(content: bytes) -> pd.DataFrame:
    return pd.read_stata(io.BytesIO(content))


def test_multi_series_dta_is_linear_in_points():
    # The old builder scanned every series for every date, so its time grew
    # with the square of the points per series: 2 x 20,000 points (within the
    # 200,000-point cap) took about 4 s. A per-series date map is linear.
    dates = [f"{1000 + i // 12:04d}-{i % 12 + 1:02d}-01" for i in range(20_000)]
    data = [_series("A", "US", dates), _series("B", "US", dates, start=1.0)]
    started = time.perf_counter()
    df = _read_dta(svc.generate_dta(data))
    assert time.perf_counter() - started < 1.0
    assert len(df) == 20_000
    assert df["A_US_GDP"].iloc[-1] == 19_999.0
    assert df["B_US_GDP"].iloc[-1] == 20_000.0


async def test_export_endpoint_still_returns_each_format():
    import backend.main as m

    body = {
        "data": [
            _series("A", "US", ["2020-01-01", "2020-02-01"]).model_dump(),
            _series("B", "CA", ["2020-01-01"]).model_dump(),
        ],
    }
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for fmt, media_type in [("csv", "text/csv"), ("dta", "application/x-stata-dta"), ("json", "application/json")]:
            response = await client.post("/api/export", json={**body, "format": fmt})
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith(media_type)
            assert response.content
