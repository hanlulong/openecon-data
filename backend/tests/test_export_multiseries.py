"""Multi-series exports: linear time, no dropped series, no lost dates."""

import io
import time
from datetime import datetime

import httpx
import pandas as pd
import pytest

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


def _csv_frame(content: str) -> pd.DataFrame:
    body = "\n".join(line for line in content.splitlines() if not line.startswith("#"))
    return pd.read_csv(io.StringIO(body))


LONG_ID = "A_VERY_LONG_SERIES_IDENTIFIER_XYZ"  # 33 chars: two countries share the first 32


def test_dta_keeps_series_whose_names_share_the_first_32_chars():
    data = [
        _series(LONG_ID, "US", ["2020-01-01", "2020-02-01"], start=1.0),
        _series(LONG_ID, "CA", ["2020-01-01", "2020-02-01"], start=10.0),
    ]
    df = _read_dta(svc.generate_dta(data))
    assert list(df.columns) == ["date", LONG_ID[:32], LONG_ID[:30] + "_2"]
    assert all(len(name) <= 32 for name in df.columns)
    assert df[LONG_ID[:32]].tolist() == [1.0, 2.0]
    assert df[LONG_ID[:30] + "_2"].tolist() == [10.0, 11.0]


def test_csv_keeps_series_with_duplicate_slugs():
    data = [
        _series("GDP", "US", ["2020", "2021"], start=1.0),
        _series("GDP", "US", ["2020", "2021"], start=5.0),
        _series("GDP", "US", ["2020", "2021"], start=9.0),
    ]
    df = _csv_frame(svc.generate_csv(data))
    assert list(df.columns) == ["date", "GDP_US_GDP", "GDP_US_GDP_2", "GDP_US_GDP_3"]
    assert df["GDP_US_GDP"].tolist() == [1.0, 2.0]
    assert df["GDP_US_GDP_2"].tolist() == [5.0, 6.0]
    assert df["GDP_US_GDP_3"].tolist() == [9.0, 10.0]


def test_a_series_slugged_date_does_not_overwrite_the_date_column():
    data = [_series("date", None, ["2020"], indicator=""), _series("B", "US", ["2020"], indicator="")]
    assert list(_csv_frame(svc.generate_csv(data)).columns) == ["date", "date_2", "B_US"]


def test_non_colliding_names_are_unchanged():
    data = [
        _series("NY.GDP.MKTP.CD", "US", ["2020-01-01"]),
        _series("NY.GDP.MKTP.CD", "CA", ["2020-01-01"]),
        _series(LONG_ID + "_MORE", "MX", ["2020-01-01"]),
    ]
    csv_columns = list(_csv_frame(svc.generate_csv(data)).columns)
    assert csv_columns == [
        "date",
        "NY_GDP_MKTP_CD_US_GDP",
        "NY_GDP_MKTP_CD_CA_GDP",
        f"{LONG_ID}_MORE_MX_GDP",
    ]
    dta_columns = list(_read_dta(svc.generate_dta(data)).columns)
    assert dta_columns == ["date", "NY_GDP_MKTP_CD_US_GDP", "NY_GDP_MKTP_CD_CA_GDP", LONG_ID[:32]]


def _dta_dates(labels, multi=True):
    data = [_series("A", "US", labels)]
    if multi:
        data.append(_series("B", "US", labels))
    return _read_dta(svc.generate_dta(data))["date"].tolist()


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["2020M01", "2020M02"], [datetime(2020, 1, 1), datetime(2020, 2, 1)]),
        (["2020-Q1", "2020Q4"], [datetime(2020, 1, 1), datetime(2020, 10, 1)]),
        (["2019", "2020"], [datetime(2019, 1, 1), datetime(2020, 1, 1)]),
        (["2020-01", "2020-02"], [datetime(2020, 1, 1), datetime(2020, 2, 1)]),
        (["2024/01/02"], [datetime(2024, 1, 2)]),
        # CoinGecko history timestamps are tz-aware; the old path raised
        # NotImplementedError (Stata has no time zones). Stored as UTC.
        (["2026-11-07T13:25:00.123000+00:00", "2026-11-07T09:00:00-05:00"],
         [datetime(2026, 11, 7, 13, 25, 0, 123000), datetime(2026, 11, 7, 14, 0)]),
    ],
)
def test_uniform_date_labels_become_stata_dates(labels, expected, multi):
    assert sorted(_dta_dates(labels, multi)) == sorted(pd.Timestamp(d) for d in expected)


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize(
    "labels",
    [
        ["2020", "2020-06"],          # mixed granularity: "2020-06" used to become NaT
        ["EUR", "GBP"],               # ExchangeRate's categorical "dates"
        ["2020-13", "2021-01"],       # not a real month
    ],
)
def test_other_date_labels_stay_strings(labels, multi):
    dates = _dta_dates(labels, multi)
    assert sorted(dates) == sorted(labels)
    assert not any(pd.isna(d) for d in dates)


@pytest.mark.parametrize("dates", [["1500-01-01", "1600-01-01"], ["2300-01-01", "2400-01-01"]])
def test_dta_keeps_labels_outside_pandas_datetime_range(dates):
    # Long historical series parse as dates but cannot be datetime64[ns]
    # (1677-09..2262-04); the export must keep the labels, not raise.
    for data in ([_series("A", "US", dates)], [_series("A", "US", dates), _series("B", "CA", dates)]):
        df = _read_dta(svc.generate_dta(data))
        assert len(df) == 2
        assert [str(v)[:4] for v in df["date"]] == [d[:4] for d in dates]
