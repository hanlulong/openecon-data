"""Content-Disposition must always survive Starlette's latin-1 header encoder.

Production 2026-11-05: /api/export returned 500/502 for 34 requests because the
old sanitizer (``[^\\w\\s\\-.]``) kept CJK (``\\w``) and CR/LF/U+2028/U+2029
(``\\s``), and Starlette encodes header values as latin-1. These tests pin the
MECHANISM (any filename -> encodable header carrying the Unicode name via RFC
5987), not one triggering string.
"""

from __future__ import annotations

import asyncio
import random
import re
from urllib.parse import unquote

import pytest
from starlette.responses import Response

from backend.models import ExportRequest, NormalizedData
from backend.services.export import content_disposition, export_service

_HEADER_SHAPE = re.compile(
    r"^attachment; filename=\"(?P<ascii>[A-Za-z0-9._-]+)\"; filename\*=UTF-8''(?P<enc>[A-Za-z0-9%._~-]+)$"
)


def _parse(value: str) -> tuple[str, str]:
    match = _HEADER_SHAPE.match(value)
    assert match, value
    return match.group("ascii"), unquote(match.group("enc"), encoding="utf-8")


def _assert_header_safe(value: str) -> None:
    value.encode("latin-1")  # Starlette's encoder; must never raise
    assert "\r" not in value and "\n" not in value
    # Constructing a real Response is the exact production failure point.
    Response(content=b"", headers={"Content-Disposition": value})


def _series(indicator: str, series_id: str = "CN_GOLD_RESERVES", country: str = "China") -> NormalizedData:
    return NormalizedData.model_validate(
        {
            "metadata": {
                "source": "ChinaMacro",
                "indicator": indicator,
                "country": country,
                "frequency": "monthly",
                "unit": "10k troy oz",
                "lastUpdated": "2026-11-01",
                "seriesId": series_id,
                "apiUrl": "https://example.com/x",
            },
            "data": [{"date": "2026-09-01", "value": 7406.0}, {"date": "2026-10-01", "value": 7409.0}],
        }
    )


@pytest.mark.parametrize(
    "name",
    [
        "中国黄金储备.csv",
        "China Gold Reserves / 中国黄金储备.csv",
        "report\r\nSet-Cookie: x=1.csv",
        "line\u2028sep\u2029para.csv",
        "tab\tnul\x00bell\x07.csv",
        "zero\u200bwidth\ufeff.csv",
        "lone\ud800surrogate.csv",
        "../../etc/passwd",
        "..\\..\\windows\\system32.dta",
        'quote"back\\slash.json',
        "Ünïcödé façade.csv",
        "emoji 📈 chart.csv",
    ],
)
def test_hostile_and_unicode_names_yield_encodable_header(name: str) -> None:
    value = content_disposition(name, "csv")
    _assert_header_safe(value)
    ascii_name, unicode_name = _parse(value)
    assert "/" not in unicode_name and "\\" not in unicode_name
    assert not any(ch in unicode_name for ch in "\r\n\t\x00\u2028\u2029\u200b\ufeff\ud800")
    assert ascii_name.rsplit(".", 1)[0]  # non-empty stem


def test_cjk_name_round_trips_through_rfc5987() -> None:
    ascii_name, unicode_name = _parse(content_disposition("中国黄金储备_20261107.csv", "csv"))
    assert unicode_name == "中国黄金储备_20261107.csv"
    assert ascii_name == "20261107.csv"


def test_name_with_no_ascii_falls_back_to_export_with_format() -> None:
    ascii_name, unicode_name = _parse(content_disposition("黄金储备", "dta"))
    assert ascii_name == "export.dta"
    assert unicode_name == "黄金储备.dta"


@pytest.mark.parametrize("name", ["", None, "   ", "  ", "..", "/", "...."])
def test_empty_result_uses_export_fallback(name) -> None:
    value = content_disposition(name, "json")
    _assert_header_safe(value)
    assert _parse(value) == ("export.json", "export.json")


def test_accented_latin_is_folded_not_dropped() -> None:
    ascii_name, unicode_name = _parse(content_disposition("Ünïcödé façade.csv", "csv"))
    assert ascii_name == "Unicode_facade.csv"
    assert unicode_name == "Ünïcödé façade.csv"


@pytest.mark.parametrize("name", ["name.", "name..", " name. ", ".name.", "name . "])
def test_edge_dots_never_double_the_extension(name) -> None:
    assert _parse(content_disposition(name, "csv")) == ("name.csv", "name.csv")


def test_plain_ascii_name_is_preserved() -> None:
    assert _parse(content_disposition("nama_10_gdp_DE_20261107.csv", "csv")) == (
        "nama_10_gdp_DE_20261107.csv",
        "nama_10_gdp_DE_20261107.csv",
    )


def test_random_unicode_never_breaks_header() -> None:
    rng = random.Random(20261107)
    planes = [(0x20, 0x7E), (0x80, 0x24F), (0x2000, 0x206F), (0x3000, 0x9FFF), (0xD800, 0xDFFF), (0x1F300, 0x1FAFF)]
    for _ in range(2000):
        chars = []
        for _ in range(rng.randint(0, 40)):
            lo, hi = rng.choice(planes)
            chars.append(chr(rng.randint(lo, hi)))
        value = content_disposition("".join(chars), rng.choice(["csv", "json", "dta"]))
        _assert_header_safe(value)
        _parse(value)


@pytest.mark.parametrize(
    "indicator",
    [
        "China Gold Reserves / 中国黄金储备",
        "中国社会融资规模增量",
        "GDP growth",
        "Exports\nYoY",
    ],
)
@pytest.mark.parametrize("fmt", ["csv", "json", "dta"])
def test_export_endpoint_returns_200_for_provider_metadata(indicator: str, fmt: str) -> None:
    from backend.main import export_endpoint

    request = ExportRequest(data=[_series(indicator)], format=fmt)
    response = asyncio.run(export_endpoint(request))
    assert response.status_code == 200
    _assert_header_safe(response.headers["content-disposition"])


def test_export_endpoint_sanitizes_client_supplied_filename() -> None:
    from backend.main import export_endpoint

    request = ExportRequest(data=[_series("China Gold Reserves")], format="csv", filename="../x\u2028 \u9ec4\u91d1.csv")
    response = asyncio.run(export_endpoint(request))
    assert response.status_code == 200
    ascii_name, unicode_name = _parse(response.headers["content-disposition"])
    assert ascii_name == "x.csv"
    assert unicode_name == "x 黄金.csv"


def test_generated_filename_for_cjk_metadata_is_header_safe() -> None:
    filename = export_service.generate_filename([_series("China Gold Reserves / 中国黄金储备")], "csv")
    _assert_header_safe(content_disposition(filename, "csv"))
