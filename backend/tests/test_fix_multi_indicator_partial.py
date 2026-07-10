"""Guard tests for F6 — surface partial failures in multi-indicator fetches.

``fetch_multi_indicator_data`` previously raised only when EVERY indicator
failed; a partial success returned the resolved series with no indication that
others were dropped. It now attaches a transparency note (the same
``metadata.notes`` convention introduced by the StatsCan coordinate-substitution
fix) naming the indicators that could not be fetched, and — when all fail — the
raised error names them.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services import data_fetcher as DF
from backend.tests.utils import run
from backend.utils.retry import DataNotAvailableError


def _series(indicator: str) -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source="FRED",
            indicator=indicator,
            country="US",
            frequency="annual",
            unit="Billions",
            seriesId=indicator.upper(),
        ),
        data=[{"date": "2020-01-01", "value": 100.0}],
    )


def _svc() -> Mock:
    svc = Mock()
    svc._detect_explicit_provider = Mock(return_value="")
    svc._normalize_provider_alias = Mock(side_effect=lambda x: x or None)
    svc._select_routed_provider = AsyncMock(return_value="FRED")
    return svc


def _intent(indicators):
    return ParsedIntent(
        apiProvider="FRED",
        indicators=list(indicators),
        parameters={"country": "US", "startDate": "2000-01-01", "endDate": "2020-01-01"},
        clarificationNeeded=False,
        originalQuery=f"{', '.join(indicators)} for US",
    )


def _fake_fetch_factory(failing_token: str):
    async def _fake_fetch(_svc, single_intent):
        name = single_intent.indicators[0]
        if failing_token.lower() in name.lower():
            raise DataNotAvailableError(f"no data for {name}")
        return [_series(name)]

    return _fake_fetch


def test_partial_success_attaches_transparency_note():
    intent = _intent(["GDP", "inflation", "unobtainium index"])
    with patch.object(DF, "fetch_data", new=_fake_fetch_factory("unobtainium")):
        data = run(DF.fetch_multi_indicator_data(_svc(), intent))

    # Two of three indicators resolved.
    assert len(data) == 2
    notes = data[0].metadata.notes or []
    assert notes, "a partial failure must leave a transparency note"
    combined = " ".join(notes)
    assert "unobtainium index" in combined
    # The indicators that DID resolve are not listed as missing.
    assert "GDP" not in combined.replace("unobtainium index", "")


def test_full_success_leaves_no_note():
    intent = _intent(["GDP", "inflation"])
    with patch.object(DF, "fetch_data", new=_fake_fetch_factory("never-matches")):
        data = run(DF.fetch_multi_indicator_data(_svc(), intent))
    assert len(data) == 2
    assert not (data[0].metadata.notes or [])


def test_all_failed_raises_error_naming_indicators():
    intent = _intent(["unobtainium alpha", "unobtainium beta"])
    with patch.object(DF, "fetch_data", new=_fake_fetch_factory("unobtainium")):
        with pytest.raises(DataNotAvailableError) as exc_info:
            run(DF.fetch_multi_indicator_data(_svc(), intent))
    message = str(exc_info.value)
    assert "unobtainium alpha" in message
    assert "unobtainium beta" in message
