"""ChinaMacro provider tests — offline, driven by RECORDED real payloads.

Fixtures: backend/tests/fixtures/chinamacro_live_samples.json — verbatim
upstream responses recorded by scripts/record_chinamacro_fixtures.py (project
rule: recorded real responses, never invented mocks). Re-record when upstream
drifts; the drift tests below cover the provider's behavior when it does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.providers.chinamacro import (
    SERIES_REGISTRY,
    ChinaMacroProvider,
    _SchemaDriftError,
    resolve_series,
)
from backend.utils.retry import DataNotAvailableError

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "chinamacro_live_samples.json").read_text()
)


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

def test_registry_entries_are_complete():
    seen = set()
    for series in SERIES_REGISTRY:
        for field in ("id", "name_en", "name_zh", "unit", "frequency", "source",
                      "source_org", "synonyms"):
            assert series.get(field) not in (None, ""), f"{series['id']}: missing {field}"
        assert series["id"] not in seen, f"duplicate id {series['id']}"
        seen.add(series["id"])
        kind = series["source"]["kind"]
        assert kind in {"eastmoney_v1", "eastmoney_treasury", "mofcom_shrzgm"}
        if kind == "eastmoney_v1":
            assert series["source"].get("report", "").startswith("RPT_ECONOMY_")
        assert series["source"].get("field")


def test_registry_fields_exist_in_recorded_payloads():
    """Every registry field mapping must match the recorded real payloads —
    this is the offline canary for upstream schema drift."""
    for series in SERIES_REGISTRY:
        src = series["source"]
        if src["kind"] == "eastmoney_v1":
            row = FIXTURES[src["report"]]["result"]["data"][0]
        elif src["kind"] == "eastmoney_treasury":
            row = FIXTURES["RPTA_WEB_TREASURYYIELD"]["result"]["data"][0]
        else:
            row = FIXTURES["MOFCOM_SHRZGM"][0]
        assert src["field"] in row, (
            f"{series['id']}: field {src['field']} absent from recorded payload"
        )


# ---------------------------------------------------------------------------
# Series resolution (id / English / Chinese)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("CN_PMI_MFG", "CN_PMI_MFG"),
    ("cn_cpi_yoy", "CN_CPI_YOY"),
    ("manufacturing pmi", "CN_PMI_MFG"),
    ("制造业PMI", "CN_PMI_MFG"),
    ("社会融资规模", "CN_SF_INCREMENT"),
    ("社融", "CN_SF_INCREMENT"),
    ("m2 growth china", "CN_M2_YOY"),
    ("china 10 year yield", "CN_10Y_YIELD"),
    ("新增人民币贷款", "CN_NEW_LOANS"),
])
def test_resolve_series(query, expected):
    series = resolve_series(query)
    assert series is not None, f"no match for {query!r}"
    assert series["id"] == expected


def test_resolve_series_rejects_unrelated_text():
    assert resolve_series("euro area inflation hicp") is None
    assert resolve_series("") is None


# ---------------------------------------------------------------------------
# Parsing recorded payloads
# ---------------------------------------------------------------------------

def test_parse_eastmoney_v1_rows():
    rows = FIXTURES["RPT_ECONOMY_PMI"]["result"]["data"]
    points = ChinaMacroProvider._parse_rows(rows, "MAKE_INDEX", "REPORT_DATE", "CN_PMI_MFG")
    assert points, "no points parsed"
    assert all(len(p["date"]) == 10 and p["date"][4] == "-" for p in points)
    dates = [p["date"] for p in points]
    assert dates == sorted(dates), "points must be date-ascending"
    assert all(isinstance(p["value"], (int, float)) for p in points)
    assert all(30 < p["value"] < 70 for p in points), "PMI outside plausible band"


def test_parse_treasury_rows():
    rows = FIXTURES["RPTA_WEB_TREASURYYIELD"]["result"]["data"]
    points = ChinaMacroProvider._parse_rows(rows, "EMM00166466", "SOLAR_DATE", "CN_10Y_YIELD")
    assert points and all(0 < p["value"] < 10 for p in points)


def test_parse_mofcom_rows_converts_yyyymm_dates():
    rows = FIXTURES["MOFCOM_SHRZGM"]
    points = ChinaMacroProvider._parse_rows(rows, "tiosfs", "date", "CN_SF_INCREMENT")
    assert points
    for p in points:
        assert len(p["date"]) == 10 and p["date"].endswith("-01")


def test_parse_missing_field_raises_schema_drift():
    rows = [dict(FIXTURES["RPT_ECONOMY_PMI"]["result"]["data"][0])]
    del rows[0]["MAKE_INDEX"]
    with pytest.raises(_SchemaDriftError):
        ChinaMacroProvider._parse_rows(rows, "MAKE_INDEX", "REPORT_DATE", "CN_PMI_MFG")


# ---------------------------------------------------------------------------
# Two-tier behavior: live failure/drift falls back to the curated snapshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_failure_falls_back_to_snapshot(monkeypatch):
    provider = ChinaMacroProvider()

    async def _boom(series):
        raise DataNotAvailableError("live endpoint down (test)")

    monkeypatch.setattr(provider, "_live_observations", _boom)
    result = await provider.fetch_indicator("CN_PMI_MFG")
    assert result.data, "snapshot tier should serve"
    assert "curated snapshot" in result.metadata.apiUrl
    assert result.metadata.country == "China"


@pytest.mark.asyncio
async def test_schema_drift_falls_back_to_snapshot(monkeypatch):
    provider = ChinaMacroProvider()

    async def _drift(series):
        raise _SchemaDriftError("field gone (test)")

    monkeypatch.setattr(provider, "_live_observations", _drift)
    result = await provider.fetch_indicator("制造业PMI")
    assert result.data
    assert "schema drifted" in result.metadata.apiUrl


@pytest.mark.asyncio
async def test_live_success_discles_live_provenance(monkeypatch):
    provider = ChinaMacroProvider()
    rows = FIXTURES["RPT_ECONOMY_PMI"]["result"]["data"]

    async def _fixture_rows(report):
        assert report == "RPT_ECONOMY_PMI"
        return rows

    monkeypatch.setattr(provider, "_rows_eastmoney_v1", _fixture_rows)
    result = await provider.fetch_indicator("CN_PMI_MFG")
    assert result.data
    assert "live:" in result.metadata.apiUrl
    assert "NBS" in result.metadata.source


@pytest.mark.asyncio
async def test_window_filtering_and_empty_window(monkeypatch):
    provider = ChinaMacroProvider()
    rows = FIXTURES["RPT_ECONOMY_PMI"]["result"]["data"]

    async def _fixture_rows(report):
        return rows

    monkeypatch.setattr(provider, "_rows_eastmoney_v1", _fixture_rows)
    all_points = await provider.fetch_indicator("CN_PMI_MFG")
    start = all_points.data[-1].date  # keep only the newest point
    windowed = await provider.fetch_indicator("CN_PMI_MFG", start_date=start)
    assert len(windowed.data) == 1
    with pytest.raises(DataNotAvailableError):
        await provider.fetch_indicator("CN_PMI_MFG", start_date="2999-01-01")


@pytest.mark.asyncio
async def test_unknown_indicator_is_honest():
    provider = ChinaMacroProvider()
    with pytest.raises(DataNotAvailableError):
        await provider.fetch_indicator("this matches nothing at all xyz")


# ---------------------------------------------------------------------------
# Dispatch integration: provider CHINAMACRO reaches the provider, params-first
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_reaches_chinamacro_params_first(monkeypatch):
    from backend.models import ParsedIntent
    from backend.services.data_fetcher import fetch_from_provider_dispatch

    calls = {}

    class _FakeChinaMacro:
        async def fetch_indicator(self, indicator, start_date=None, end_date=None):
            calls["indicator"] = indicator
            provider = ChinaMacroProvider()
            rows = FIXTURES["RPT_ECONOMY_PMI"]["result"]["data"]
            points = provider._parse_rows(rows, "MAKE_INDEX", "REPORT_DATE", "CN_PMI_MFG")
            from backend.models import Metadata, NormalizedData
            return NormalizedData(
                metadata=Metadata(
                    source="ChinaMacro (test)", indicator="pmi", country="China",
                    frequency="monthly", unit="index", lastUpdated=points[-1]["date"],
                ),
                data=points,
            )

    class _FakeSvc:
        chinamacro_provider = _FakeChinaMacro()

    from backend.models import ExecutionPlan

    intent = ParsedIntent(
        apiProvider="CHINAMACRO",
        indicators=["stale plan snapshot"],
        parameters={"indicator": "CN_PMI_MFG"},
        clarificationNeeded=False,
    )
    plan = ExecutionPlan(
        provider="CHINAMACRO",
        candidate_id="test",
        fetch_strategy="provider_dispatch",
        params=dict(intent.parameters),
    )
    result = await fetch_from_provider_dispatch(_FakeSvc(), intent, plan)
    # Params-first precedence: the resolved pick beats the plan snapshot.
    assert calls["indicator"] == "CN_PMI_MFG"
    # Dispatch contract: a LIST of NormalizedData, one entry per series.
    assert isinstance(result, list) and result[0].data
