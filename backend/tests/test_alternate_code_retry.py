"""Same-provider alternate-code retry (re-adjudication on no-data).

When an LLM-picked indicator code returns no data, the fetch layer re-runs the
selector with that code EXCLUDED so the LLM picks the next-best EXECUTABLE
candidate, before surrendering to cross-provider fallback. General across
providers; bounded; scoped to fresh selector picks only.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import NormalizedData, Metadata, DataPoint
from backend.services.query import QueryService
from backend.utils.retry import DataNotAvailableError


def _make_service() -> QueryService:
    svc = QueryService.__new__(QueryService)
    return svc


def _ok_series(code: str) -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source="WorldBank", indicator=code, country="CN",
            frequency="annual", unit="%",
        ),
        data=[DataPoint(date="2024-01-01", value=4.9)],
    )


def _intent(code: str):
    from backend.models import ParsedIntent

    return ParsedIntent(
        apiProvider="WORLDBANK",
        indicators=["GDP growth rate"],
        clarificationNeeded=False,
        parameters={"indicator": code, "__decision_source": "llm_pick", "country": "CN"},
    )


@pytest.mark.asyncio
async def test_retries_past_dead_code_to_executable_one():
    """First (dead) code returns no data; the loop excludes it, the mocked
    re-resolution yields an executable code, and data is returned."""
    svc = _make_service()
    intent = _intent("NY.GDP.MKTP.ZG")  # the dead WB code
    calls = {"n": 0}

    async def fake_fetch(it, execution_plan=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # First pick is the dead code already in params → no data.
            raise DataNotAvailableError("no data for NY.GDP.MKTP.ZG")
        # The loop cleared 'indicator' and set the exclude set; simulate the
        # selector re-resolving to the executable code.
        assert "NY.GDP.MKTP.ZG" in (it.parameters.get("__exclude_indicator_codes") or [])
        it.parameters["indicator"] = "NY.GDP.MKTP.KD.ZG"
        it.parameters["__decision_source"] = "llm_pick"
        return [_ok_series("NY.GDP.MKTP.KD.ZG")]

    with patch.object(svc, "_fetch_data", side_effect=fake_fetch):
        data = await svc._fetch_data_with_alternate_codes(intent, None)

    assert calls["n"] == 2
    assert data and data[0].metadata.indicator == "NY.GDP.MKTP.KD.ZG"


@pytest.mark.asyncio
async def test_retry_is_bounded_and_then_raises():
    """Every alternate is also dead → loop stops after the bound and the
    DataNotAvailableError propagates (caller does cross-provider fallback)."""
    svc = _make_service()
    intent = _intent("DEAD0")
    seen = []

    async def always_dead(it, execution_plan=None):
        # Each retry must present a DISTINCT failed code (loop clears indicator;
        # simulate the selector picking a new-but-also-dead code each time).
        code = it.parameters.get("indicator") or f"DEAD{len(seen)+1}"
        seen.append(code)
        it.parameters["indicator"] = f"DEAD{len(seen)}"
        it.parameters["__decision_source"] = "llm_pick"
        raise DataNotAvailableError(f"no data for {code}")

    with patch.object(svc, "_fetch_data", side_effect=always_dead):
        with pytest.raises(DataNotAvailableError):
            await svc._fetch_data_with_alternate_codes(intent, None)

    # original attempt + _MAX_ALTERNATE_CODE_RETRIES re-adjudications
    assert len(seen) == svc._MAX_ALTERNATE_CODE_RETRIES + 1


@pytest.mark.asyncio
async def test_no_retry_for_non_selector_picks():
    """A user-exact / carried code (not __decision_source==llm_pick) must fail
    honestly, never silently swap to another series."""
    svc = _make_service()
    from backend.models import ParsedIntent

    intent = ParsedIntent(
        apiProvider="FRED", indicators=["CPIAUCSL"], clarificationNeeded=False,
        parameters={"indicator": "CPIAUCSL", "__decision_source": "exact_code"},
    )
    calls = {"n": 0}

    async def fail_once(it, execution_plan=None):
        calls["n"] += 1
        raise DataNotAvailableError("no data")

    with patch.object(svc, "_fetch_data", side_effect=fail_once):
        with pytest.raises(DataNotAvailableError):
            await svc._fetch_data_with_alternate_codes(intent, None)

    assert calls["n"] == 1  # no alternate retry attempted


@pytest.mark.asyncio
async def test_selector_excludes_codes_from_candidates():
    """select(exclude_codes=...) drops those codes before the LLM ever sees
    them, so the excluded code can never be re-picked."""
    from backend.services.indicator_selector import IndicatorSelector, SelectionResult

    sel = IndicatorSelector(settings=SimpleNamespace(
        indicator_telemetry_enabled=False, indicator_fusion="rrf",
    ))

    cand = [("NY.GDP.MKTP.ZG", "GDP dead"), ("NY.GDP.MKTP.KD.ZG", "GDP growth"),
            ("NY.GDP.PCAP.KD.ZG", "GDP per capita growth")]
    scores = [0.9, 0.88, 0.80]

    seen_candidates = {}

    async def fake_pick(query, candidates, provider, prefer_ask=False):
        seen_candidates["codes"] = [c[0] for c in candidates]
        return SelectionResult(code=candidates[0][0], name=candidates[0][1], source="llm_pick")

    with patch.object(sel, "_get_candidates_with_scores", return_value=(cand, scores)), \
         patch.object(sel, "_llm_pick", side_effect=fake_pick), \
         patch.object(sel, "_retry_if_metadata_conflict", new=AsyncMock(side_effect=lambda q, r, c, p: r)), \
         patch.object(sel, "_scores_are_ambiguous", return_value=False):
        result = await sel.select("GDP growth rate", "WORLDBANK",
                                  exclude_codes={"NY.GDP.MKTP.ZG"})

    assert "NY.GDP.MKTP.ZG" not in seen_candidates["codes"]
    assert result.code == "NY.GDP.MKTP.KD.ZG"
