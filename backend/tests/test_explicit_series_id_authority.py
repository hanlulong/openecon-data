"""Explicit-code authority for the parse's dedicated ``seriesId`` slot.

User-reported defect class (2026-08..11): a provider-native code the user
literally named ("FRED series T10YIE", "WPSFD4 (not PPIACO)",
"NY.GDP.MKTP.CN", "HOUSTMW") was parsed into ``parameters.seriesId`` and then
ignored — the selector llm_picked a neighbour and dispatch silently dropped the
differing seriesId. These tests pin the MECHANISM (literal + code-shaped +
exists + not excluded + not a protected decision => exact_user_input lock;
otherwise an honest disclosure), across FRED, World Bank and IMF.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.providers.fred import FREDProvider
from backend.services import indicator_resolution as ir
from backend.services.data_fetcher import _attach_requested_series_disclosure
from backend.services.indicator_clarification import looks_like_provider_indicator_code
from backend.services.indicator_selector import SelectionResult

# Minimal fake catalog: provider -> code -> entry (frequency drives the FRED
# native-aggregation decision; category drives the IMF exact-code check).
_CATALOG: Dict[str, Dict[str, Dict[str, Any]]] = {
    "FRED": {
        "T10YIE": {"code": "T10YIE", "frequency": "Daily"},
        "DFII10": {"code": "DFII10", "frequency": "Daily"},
        "DGS10": {"code": "DGS10", "frequency": "Daily"},
        "GDP": {"code": "GDP", "frequency": "Quarterly"},
        "HOUSTMW": {"code": "HOUSTMW", "frequency": "Monthly"},
        "PPIACO": {"code": "PPIACO", "frequency": "Monthly"},
        "CPIAUCSL": {"code": "CPIAUCSL", "frequency": "Monthly"},
    },
    "WorldBank": {"NY.GDP.MKTP.CN": {"code": "NY.GDP.MKTP.CN"}},
    "IMF": {"GGXWDG_NGDP": {"code": "GGXWDG_NGDP", "category": "WEO"}},
}


def _fake_catalog(catalog_provider: str, code: str) -> Optional[Dict[str, Any]]:
    table = _CATALOG.get(catalog_provider, {})
    return table.get(code) or table.get(code.upper())


def _svc(lookup_result: tuple = ("unverified", None)) -> SimpleNamespace:
    lookup = AsyncMock(return_value=lookup_result)
    return SimpleNamespace(
        settings=SimpleNamespace(),
        fred_provider=SimpleNamespace(lookup_series_info=lookup),
        _looks_like_provider_indicator_code=looks_like_provider_indicator_code,
        _verify_semantic_discriminators=lambda *_a, **_k: True,
    )


def _intent(provider: str, query: str, indicators, params: dict, **extra) -> ParsedIntent:
    return ParsedIntent(
        apiProvider=provider,
        indicators=list(indicators),
        parameters=dict(params),
        clarificationNeeded=False,
        originalQuery=query,
        **extra,
    )


def _resolve(svc, provider: str, intent: ParsedIntent, pick: str = "DFII10"):
    select = AsyncMock(return_value=SelectionResult(code=pick, name=pick, source="llm_pick"))
    with patch.object(ir, "_catalog_series_entry", side_effect=_fake_catalog), patch(
        "backend.services.indicator_selector.IndicatorSelector.select", new=select
    ):
        params = asyncio.run(
            ir.resolve_indicator_for_fetch(svc, provider, intent, dict(intent.parameters or {}))
        )
    return params, select


# ---------------------------------------------------------------------------
# Literal, existing seriesId beats the selector — FRED / World Bank / IMF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "provider,query,indicators,series_id",
    [
        ("FRED", "FRED series T10YIE latest daily value. Use seriesId T10YIE only",
         ["10-Year Treasury Inflation-Indexed Security Yield"], "T10YIE"),
        ("FRED", "FRED series HOUSTMW from 2015", ["housing starts midwest"], "HOUSTMW"),
        ("FRED", "fred series t10yie", ["breakeven inflation"], "t10yie"),
        ("WORLDBANK", "China GDP current local currency CNY NY.GDP.MKTP.CN", ["GDP"], "NY.GDP.MKTP.CN"),
        ("IMF", "IMF GGXWDG_NGDP for Italy since 2010", ["government debt"], "GGXWDG_NGDP"),
    ],
)
def test_literal_series_id_beats_llm_pick(provider, query, indicators, series_id) -> None:
    intent = _intent(provider, query, indicators, {"seriesId": series_id, "country": "US"})
    params, select = _resolve(_svc(), provider, intent)

    expected = _fake_catalog(ir._SINGLE_SERIES_CODE_PROVIDERS[provider], series_id)["code"]
    assert params["indicator"] == expected
    assert params["__semantic_authority"] == "exact_user_input"
    assert params["__decision_source"] == "exact_code"
    assert ir.REQUESTED_SERIES_ID_KEY not in params
    select.assert_not_awaited()


def test_literal_series_id_overrides_prefetch_selector_pick() -> None:
    """A plain selector pick (llm_adjudication, e.g. stamped by the prefetch
    stage) is exactly the component that mis-picked — the user's literal code
    outranks it."""
    intent = _intent(
        "FRED", "US T10YIE latest", ["10-year Treasury yield"],
        {"seriesId": "T10YIE", "indicator": "DFII10",
         "__semantic_authority": "llm_adjudication", "__decision_source": "llm_pick"},
    )
    params, select = _resolve(_svc(), "FRED", intent)
    assert params["indicator"] == "T10YIE"
    assert params["__semantic_authority"] == "exact_user_input"
    select.assert_not_awaited()


def test_series_id_slot_beats_rejected_code_in_indicator_slot() -> None:
    """"WPSFD4 (not PPIACO)": both codes are literal; the LLM's seriesId slot
    decides which one is wanted (the resolver never parses "not"). WPSFD4 is
    absent from the catalog, so FRED's own /series answer validates it."""
    svc = _svc(lookup_result=("found", {"id": "WPSFD4", "frequency": "Monthly"}))
    intent = _intent(
        "FRED", "FRED series code WPSFD4 (not PPIACO) since 2020", ["producer price index"],
        {"seriesId": "WPSFD4", "indicator": "PPIACO"},
    )
    params, select = _resolve(svc, "FRED", intent, pick="PPIACO")
    assert params["indicator"] == "WPSFD4"
    svc.fred_provider.lookup_series_info.assert_awaited_once_with("WPSFD4")
    select.assert_not_awaited()


def test_transform_suffix_survives_the_lock() -> None:
    intent = _intent("FRED", "CPIAUCSL year over year", ["CPI"], {"seriesId": "CPIAUCSL:pc1"})
    params, _ = _resolve(_svc(), "FRED", intent)
    assert params["indicator"] == "CPIAUCSL"
    assert params["seriesId"] == "CPIAUCSL:pc1"


# ---------------------------------------------------------------------------
# Never lock what the user did not type / what this turn already proved dead
# ---------------------------------------------------------------------------

def test_carried_series_id_not_in_current_turn_text_never_locks() -> None:
    """Follow-up asking for a different concept: the previous turn's T10YIE is
    carried in params/resolvedQuery but NOT typed this turn."""
    intent = _intent(
        "FRED", "what about the 5-year one", ["5-year breakeven inflation"],
        {"seriesId": "T10YIE"},
        isFollowUp=True, resolvedQuery="FRED series T10YIE 5-year breakeven",
    )
    params, select = _resolve(_svc(), "FRED", intent, pick="T5YIE")
    assert params["indicator"] == "T5YIE"
    assert ir.REQUESTED_SERIES_ID_KEY not in params
    select.assert_awaited_once()


def test_excluded_code_is_not_relocked() -> None:
    intent = _intent(
        "FRED", "FRED series T10YIE latest", ["breakeven"],
        {"seriesId": "T10YIE", "__exclude_indicator_codes": ["T10YIE"]},
    )
    params, select = _resolve(_svc(), "FRED", intent, pick="T5YIE")
    assert params["indicator"] == "T5YIE"
    assert params[ir.REQUESTED_SERIES_ID_STATUS_KEY] == "excluded"
    select.assert_awaited_once()


def test_clarification_choice_is_not_overridden() -> None:
    intent = _intent(
        "FRED", "FRED series T10YIE latest", ["breakeven"],
        {"seriesId": "T10YIE", "indicator": "T5YIE",
         "__semantic_authority": "exact_user_input", "__decision_source": "user_choice"},
    )
    params, _ = _resolve(_svc(), "FRED", intent)
    assert params["indicator"] == "T5YIE"
    assert params[ir.REQUESTED_SERIES_ID_STATUS_KEY] == "protected"


def test_multi_indicator_requests_never_promote() -> None:
    intent = _intent(
        "FRED", "HOUSTNE and HOUSTMW", ["HOUSTNE", "HOUSTMW"], {"seriesId": "HOUSTMW"},
    )
    with patch.object(ir, "_catalog_series_entry", side_effect=_fake_catalog):
        assert asyncio.run(ir._literal_series_id_request(_svc(), "FRED", intent, intent.parameters)) is None


@pytest.mark.parametrize("provider", ["EUROSTAT", "STATSCAN", "BIS", "OECD"])
def test_dataset_code_providers_are_not_promoted(provider) -> None:
    intent = _intent(provider, "GOV_10A_TAXAG for Germany", ["taxes"], {"seriesId": "GOV_10A_TAXAG"})
    assert asyncio.run(ir._literal_series_id_request(_svc(), provider, intent, intent.parameters)) is None


def test_concept_query_without_series_id_is_unchanged() -> None:
    intent = _intent("FRED", "US M2 money supply", ["M2 money supply"], {"country": "US"})
    params, select = _resolve(_svc(), "FRED", intent, pick="M2SL")
    assert params["indicator"] == "M2SL"
    select.assert_awaited_once()


def test_stale_stamp_from_previous_turn_is_cleared() -> None:
    intent = _intent(
        "FRED", "US unemployment rate", ["unemployment rate"],
        {ir.REQUESTED_SERIES_ID_KEY: "WPSFD4", ir.REQUESTED_SERIES_ID_STATUS_KEY: "not_found"},
    )
    params, _ = _resolve(_svc(), "FRED", intent, pick="UNRATE")
    assert ir.REQUESTED_SERIES_ID_KEY not in params


# ---------------------------------------------------------------------------
# Nonexistent / unverifiable named codes: selector runs, honest disclosure
# ---------------------------------------------------------------------------

def test_nonexistent_fred_code_runs_selector_and_is_disclosed() -> None:
    svc = _svc(lookup_result=("not_found", None))
    intent = _intent("FRED", "FRED series ZZTOP99 inflation", ["inflation"], {"seriesId": "ZZTOP99"})
    params, select = _resolve(svc, "FRED", intent, pick="CPIAUCSL")
    assert params["indicator"] == "CPIAUCSL"
    assert params[ir.REQUESTED_SERIES_ID_KEY] == "ZZTOP99"
    assert params[ir.REQUESTED_SERIES_ID_STATUS_KEY] == "not_found"
    select.assert_awaited_once()

    served = [NormalizedData(
        metadata=Metadata(source="FRED", indicator="CPI", frequency="monthly", unit="Index",
                          seriesId="CPIAUCSL", notes=["FRED note"]),
        data=[{"date": "2025-01-01", "value": 1.0}],
    )]
    decorated = _attach_requested_series_disclosure(served, params, intent)
    assert "ZZTOP99" in decorated[0].metadata.notes[0]
    assert "not found" in decorated[0].metadata.notes[0]
    # The shared (cacheable) object is never mutated.
    assert served[0].metadata.notes == ["FRED note"]

    zh = _attach_requested_series_disclosure(served, params, SimpleNamespace(language="zh"))
    assert "未找到" in zh[0].metadata.notes[0]


def test_unverifiable_code_never_claims_not_found() -> None:
    svc = _svc(lookup_result=("unverified", None))
    intent = _intent("FRED", "FRED series ZZTOP99 inflation", ["inflation"], {"seriesId": "ZZTOP99"})
    params, _ = _resolve(svc, "FRED", intent, pick="CPIAUCSL")
    assert params[ir.REQUESTED_SERIES_ID_STATUS_KEY] == "unverified"
    served = [NormalizedData(
        metadata=Metadata(source="FRED", indicator="CPI", frequency="monthly", unit="Index", seriesId="CPIAUCSL"),
        data=[],
    )]
    note = _attach_requested_series_disclosure(served, params, intent)[0].metadata.notes[0]
    assert "ZZTOP99" in note and "not found" not in note


def test_disclosure_is_silent_when_requested_code_is_served() -> None:
    params = {ir.REQUESTED_SERIES_ID_KEY: "T10YIE", ir.REQUESTED_SERIES_ID_STATUS_KEY: "unverified"}
    served = [NormalizedData(
        metadata=Metadata(source="FRED", indicator="x", frequency="daily", unit="%", seriesId="T10YIE"),
        data=[],
    )]
    assert _attach_requested_series_disclosure(served, params, None) is served


# ---------------------------------------------------------------------------
# Frequency: provider-native aggregation of the NAMED code (not a sibling map)
# ---------------------------------------------------------------------------

def test_named_daily_code_with_coarser_request_gets_native_aggregation_params() -> None:
    intent = _intent("FRED", "FRED DGS10 monthly 2025 2026", ["10-year treasury"], {"seriesId": "DGS10"})
    params, select = _resolve(_svc(), "FRED", intent, pick="GS10")
    assert params["indicator"] == "DGS10"
    assert params["frequency"] == "monthly"
    assert params["aggregation_method"] == "avg"
    select.assert_not_awaited()


def test_named_code_at_native_frequency_adds_no_frequency_params() -> None:
    intent = _intent("FRED", "FRED series T10YIE daily", ["breakeven"], {"seriesId": "T10YIE"})
    params, _ = _resolve(_svc(), "FRED", intent)
    assert "frequency" not in params and "aggregation_method" not in params


def test_fred_native_frequency_request_only_aggregates_toward_coarser() -> None:
    fred = FREDProvider.__new__(FREDProvider)
    daily = {"id": "DGS10", "frequency": "Daily", "frequency_short": "D"}
    quarterly = {"id": "GDP", "frequency": "Quarterly", "frequency_short": "Q"}

    agg, note = fred._native_frequency_request({"frequency": "monthly", "aggregation_method": "avg"}, daily)
    assert agg == {"frequency": "m", "aggregation_method": "avg"}
    assert "DGS10" in note and "avg" in note

    # Finer than native: never sent to FRED (HTTP 400) — native data + note.
    agg, note = fred._native_frequency_request({"frequency": "monthly", "aggregation_method": "avg"}, quarterly)
    assert agg is None and "quarterly" in note

    # Not opted in by the exact-code lock: byte-identical to before.
    assert fred._native_frequency_request({"frequency": "monthly"}, daily) == (None, None)


def test_fred_lookup_series_info_distinguishes_missing_from_unanswered() -> None:
    fred = FREDProvider.__new__(FREDProvider)
    fred.base_url = "https://api.stlouisfed.org/fred"
    fred.api_key = "k"

    def _resp(status: int, payload: dict):
        return SimpleNamespace(status_code=status, json=lambda: payload)

    cases = [
        (_resp(200, {"seriess": [{"id": "WPSFD4"}]}), ("found", {"id": "WPSFD4"})),
        (_resp(400, {"error_message": "Bad Request.  The series does not exist."}), ("not_found", None)),
        (_resp(400, {"error_message": "Bad Request.  api_key is not registered."}), ("unverified", None)),
    ]
    for response, expected in cases:
        fred._get_with_retry = AsyncMock(return_value=response)
        assert asyncio.run(fred.lookup_series_info("WPSFD4")) == expected

    fred._get_with_retry = AsyncMock(side_effect=RuntimeError("429 cooldown"))
    assert asyncio.run(fred.lookup_series_info("WPSFD4")) == ("unverified", None)
