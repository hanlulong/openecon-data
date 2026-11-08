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
from backend.services import data_fetcher
from backend.services import indicator_resolution as ir
from backend.services.data_fetcher import (
    _attach_requested_series_disclosure,
    _provider_request_contract,
)
from backend.services.indicator_clarification import (
    build_prefetch_indicator_choice_clarification,
    looks_like_provider_indicator_code,
)
from backend.services.indicator_selector import SelectionResult

# Minimal fake catalog: provider -> code -> entry (category drives the IMF
# exact-code check).
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
    # The lock keeps a (silent-when-served) stamp so a later fallback serve
    # can still be disclosed.
    assert params[ir.REQUESTED_SERIES_ID_KEY] == expected
    assert params[ir.REQUESTED_SERIES_ID_STATUS_KEY] == "lock"
    assert params[ir.REQUESTED_SERIES_PROVIDER_KEY] == provider
    # The seriesId slot is canonicalized so dispatch keeps it.
    assert params["seriesId"] == expected
    # Never a frequency/aggregation side effect.
    assert "frequency" not in params and "aggregation_method" not in params
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


def test_rejected_code_locked_by_literal_indicator_path_is_overridden_by_series_id() -> None:
    """A second resolution pass: an earlier pass's literal-code path locked the
    REJECTED code from the indicator slot (exact_user_input / exact_code). That
    is a literal-code lock, not the user's clarification choice, so the
    seriesId slot still wins."""
    svc = _svc(lookup_result=("found", {"id": "WPSFD4"}))
    intent = _intent(
        "FRED", "FRED series code WPSFD4 (not PPIACO) since 2020", ["producer price index"],
        {"seriesId": "WPSFD4", "indicator": "PPIACO",
         "__semantic_authority": "exact_user_input", "__decision_source": "exact_code"},
    )
    params, _ = _resolve(svc, "FRED", intent, pick="PPIACO")
    assert params["indicator"] == "WPSFD4"


@pytest.mark.parametrize(
    "query,series_id,expected",
    [
        ("CPIAUCSL year over year", "CPIAUCSL:pc1", "CPIAUCSL:pc1"),
        ("cpiaucsl percent change from year ago", "cpiaucsl:pc1", "CPIAUCSL:pc1"),
    ],
)
def test_transform_suffix_survives_the_lock(query, series_id, expected) -> None:
    intent = _intent("FRED", query, ["CPI"], {"seriesId": series_id})
    params, _ = _resolve(_svc(), "FRED", intent)
    assert params["indicator"] == "CPIAUCSL"
    assert params["seriesId"] == expected
    # "year over year" is a transform, never a frequency request.
    assert "frequency" not in params and "aggregation_method" not in params


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
    assert "could not be verified" in note


def _served(source: str, series_id: str, indicator: str = "x") -> list:
    return [NormalizedData(
        metadata=Metadata(source=source, indicator=indicator, frequency="annual", unit="%", seriesId=series_id),
        data=[],
    )]


def test_locked_code_served_by_its_provider_is_never_disclosed() -> None:
    """A locked code is silent whenever its own provider served it, even if the
    provider echoes a decorated seriesId (dimension suffix, composite key)."""
    params = {ir.REQUESTED_SERIES_ID_KEY: "NY.GDP.MKTP.CN", ir.REQUESTED_SERIES_ID_STATUS_KEY: "lock",
              ir.REQUESTED_SERIES_PROVIDER_KEY: "WORLDBANK"}
    served = _served("World Bank", "NY.GDP.MKTP.CN.composite")
    assert _attach_requested_series_disclosure(served, params, None) is served


def test_locked_code_replaced_by_cross_provider_fallback_is_disclosed() -> None:
    params = {ir.REQUESTED_SERIES_ID_KEY: "T10YIE", ir.REQUESTED_SERIES_ID_STATUS_KEY: "lock",
              ir.REQUESTED_SERIES_PROVIDER_KEY: "FRED"}
    served = _served("World Bank", "FP.CPI.TOTL.ZG")
    note = _attach_requested_series_disclosure(served, params, None)[0].metadata.notes[0]
    assert "T10YIE" in note and "FP.CPI.TOTL.ZG" in note


def test_disclosure_placeholder_is_localized() -> None:
    params = {ir.REQUESTED_SERIES_ID_KEY: "ZZTOP99", ir.REQUESTED_SERIES_ID_STATUS_KEY: "not_found"}
    served = _served("FRED", "")
    served[0].metadata.indicator = ""
    note = _attach_requested_series_disclosure(served, params, SimpleNamespace(language="zh"))[0].metadata.notes[0]
    assert "最接近的可用序列" in note and "closest" not in note


def test_disclosure_is_silent_when_requested_code_is_served() -> None:
    params = {ir.REQUESTED_SERIES_ID_KEY: "T10YIE", ir.REQUESTED_SERIES_ID_STATUS_KEY: "unverified"}
    served = [NormalizedData(
        metadata=Metadata(source="FRED", indicator="x", frequency="daily", unit="%", seriesId="T10YIE"),
        data=[],
    )]
    assert _attach_requested_series_disclosure(served, params, None) is served


# ---------------------------------------------------------------------------
# Frequency: the lock serves the NAMED code at its native frequency. Raw-text
# frequency words are never turned into a hard FRED aggregation (deferred to a
# structured parse slot): these real-user phrasings name a maturity, a rate
# unit, a transform, an end-of-period value or a window — not a frequency.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query,series_id",
    [
        ("US 3-Month Treasury Constant Maturity Rate DGS3MO last value", "DGS3MO"),
        ("US nonfarm labor productivity percent change annual rate PRS85006092", "PRS85006092"),
        ("FRED GDPC1 real GDP seasonally adjusted annual rate since 2015", "GDPC1"),
        ("US PCE price index year-over-year, FRED series PCEPI, latest 3 months", "PCEPI"),
        ("CPIAUCSL percent change from year ago, latest months", "CPIAUCSL"),
        ("Fed balance sheet (WALCL) annual year-end values", "WALCL"),
        ("Show DGS10 for the year 2025", "DGS10"),
        ("FRED DGS10 monthly 2025 2026", "DGS10"),
        ("FRED DGS10 year to date", "DGS10"),
    ],
)
def test_named_code_lock_never_forces_a_frequency_transform(query, series_id) -> None:
    svc = _svc(lookup_result=("found", {"id": series_id}))
    intent = _intent("FRED", query, ["series"], {"seriesId": series_id})
    params, select = _resolve(svc, "FRED", intent, pick="GS10")
    assert params["indicator"] == series_id
    assert "frequency" not in params and "aggregation_method" not in params
    select.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dispatch + cache identity for a kept transform suffix
# ---------------------------------------------------------------------------

def test_fred_contract_keys_on_a_kept_transform_suffix() -> None:
    intent = _intent("FRED", "CPIAUCSL yoy", ["CPI"], {})
    levels = _provider_request_contract("FRED", intent, {"indicator": "CPIAUCSL"})
    yoy = _provider_request_contract("FRED", intent, {"indicator": "CPIAUCSL", "seriesId": "cpiaucsl:pc1"})
    other = _provider_request_contract("FRED", intent, {"indicator": "CPIAUCSL", "seriesId": "PPIACO:pc1"})
    assert "transform" not in levels
    assert yoy["transform"] == "pc1" and yoy != levels
    # A seriesId naming a DIFFERENT series is dropped at dispatch, so it must
    # not change the cache identity either.
    assert other == levels


@pytest.mark.parametrize(
    "series_id,sent",
    [("CPIAUCSL:pc1", "CPIAUCSL:pc1"), ("cpiaucsl:pc1", "CPIAUCSL:pc1"),
     ("CPIAUCSL", "CPIAUCSL"), ("PPIACO", None),
     # Only FRED's documented ``units`` values are sent; others are dropped.
     ("CPIAUCSL:PC1", "CPIAUCSL:pc1"), ("CPIAUCSL:ch1", "CPIAUCSL:ch1"),
     ("CPIAUCSL:yoy", "CPIAUCSL"), ("CPIAUCSL:lin", "CPIAUCSL"), ("CPIAUCSL:", "CPIAUCSL")],
)
def test_fred_dispatch_keeps_only_a_same_series_series_id(series_id, sent) -> None:
    captured: Dict[str, Any] = {}

    async def _fetch_series(fred_params):
        captured.update(fred_params)
        return NormalizedData(
            metadata=Metadata(source="FRED", indicator="CPI", frequency="monthly", unit="Index",
                              seriesId="CPIAUCSL"),
            data=[],
        )

    svc = SimpleNamespace(fred_provider=SimpleNamespace(fetch_series=_fetch_series))
    params = {"indicator": "CPIAUCSL", "seriesId": series_id, "country": "US"}
    plan = SimpleNamespace(provider="FRED", params=params, provider_request={"series_id": "CPIAUCSL"})
    intent = _intent("FRED", "CPI", ["CPI"], params)
    with patch.object(data_fetcher, "_assert_provider_map_authority", lambda *a, **k: None):
        asyncio.run(data_fetcher.fetch_from_provider_dispatch(svc, intent, plan))
    assert captured["indicator"] == "CPIAUCSL"
    # A different series is dropped (it would override the resolution); the
    # same series is sent in the resolved spelling with its transform intact.
    assert captured.get("seriesId") == sent


# ---------------------------------------------------------------------------
# Prefetch clarification must not pre-empt an unambiguous named code
# ---------------------------------------------------------------------------

def test_prefetch_menu_is_skipped_for_a_lockable_named_code() -> None:
    def _must_not_run(*_a, **_k):
        raise AssertionError("prefetch selector ran for an unambiguous named code")

    svc = _svc()
    svc._select_indicator_query_for_resolution = _must_not_run
    intent = _intent("FRED", "US T10YIE latest", ["10-year Treasury yield"], {"seriesId": "T10YIE"})
    with patch.object(ir, "_catalog_series_entry", side_effect=_fake_catalog):
        result = asyncio.run(build_prefetch_indicator_choice_clarification(
            svc, "conv", intent.originalQuery, intent, None, False,
        ))
    assert result is None


# ---------------------------------------------------------------------------
# FRED existence check
# ---------------------------------------------------------------------------

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
        fred._series_existence_memo = {}
        fred._get_with_retry = AsyncMock(return_value=response)
        assert asyncio.run(fred.lookup_series_info("WPSFD4")) == expected

    fred._series_existence_memo = {}
    fred._get_with_retry = AsyncMock(side_effect=RuntimeError("429 cooldown"))
    assert asyncio.run(fred.lookup_series_info("WPSFD4")) == ("unverified", None)


def test_fred_lookup_series_info_memoizes_only_definitive_answers() -> None:
    fred = FREDProvider.__new__(FREDProvider)
    fred.base_url = "https://api.stlouisfed.org/fred"
    fred.api_key = "k"
    fred._series_existence_memo = {}

    # A transport failure is retried on the next call, never remembered.
    fred._get_with_retry = AsyncMock(side_effect=RuntimeError("timeout"))
    assert asyncio.run(fred.lookup_series_info("WPSFD4")) == ("unverified", None)
    fred._get_with_retry = AsyncMock(
        return_value=SimpleNamespace(status_code=200, json=lambda: {"seriess": [{"id": "WPSFD4"}]})
    )
    assert asyncio.run(fred.lookup_series_info("wpsfd4")) == ("found", {"id": "WPSFD4"})
    # The definitive answer is reused by later resolution passes (one call).
    assert asyncio.run(fred.lookup_series_info("WPSFD4")) == ("found", {"id": "WPSFD4"})
    assert fred._get_with_retry.await_count == 1


# ---------------------------------------------------------------------------
# Named-code provenance is never data-cache identity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("with_plan_identity", [False, True])
@pytest.mark.parametrize(
    "stamps",
    [
        {ir.REQUESTED_SERIES_ID_KEY: "T10YIE", ir.REQUESTED_SERIES_ID_STATUS_KEY: "lock",
         ir.REQUESTED_SERIES_PROVIDER_KEY: "FRED"},
        {ir.REQUESTED_SERIES_ID_KEY: "T10YIE", ir.REQUESTED_SERIES_ID_STATUS_KEY: "not_found"},
        {ir.REQUESTED_SERIES_ID_KEY: "t10yie", ir.REQUESTED_SERIES_ID_STATUS_KEY: "unverified",
         ir.REQUESTED_SERIES_PROVIDER_KEY: "FRED"},
    ],
)
def test_requested_series_stamps_never_split_the_cache_key(stamps, with_plan_identity) -> None:
    from backend.services.query import QueryService

    svc = QueryService.__new__(QueryService)
    base = {"indicator": "T10YIE", "country": "US", "startDate": "2024-01-01"}
    if with_plan_identity:
        base["__execution_plan_identity"] = {"provider_request": {"provider": "FRED", "series_id": "T10YIE"}}
    stamped = {**base, **stamps}
    assert svc._build_cache_params("FRED", stamped) == svc._build_cache_params("FRED", dict(base))
    assert not set(stamps) & set(svc._build_cache_params("FRED", stamped))


# ---------------------------------------------------------------------------
# FRED ``units`` legality: the ``:suffix`` vocabulary is FRED's documented one
# ---------------------------------------------------------------------------

from backend.providers.fred import FRED_UNITS_TRANSFORMS, fred_units_transform  # noqa: E402


def test_fred_units_vocabulary_is_the_documented_one() -> None:
    assert set(FRED_UNITS_TRANSFORMS) == {"chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"}
    for value in FRED_UNITS_TRANSFORMS:
        assert fred_units_transform(value) == value
        assert fred_units_transform(f" {value.upper()} ") == value
    # Levels and anything outside the vocabulary are never sent.
    for value in ("lin", "LIN", "", None, "yoy", "pct", "mom", "pc1x", "pc 1"):
        assert fred_units_transform(value) is None


@pytest.mark.parametrize("suffix", ["yoy", "lin", "LIN", "garbage"])
def test_fred_contract_ignores_a_suffix_dispatch_would_drop(suffix) -> None:
    intent = _intent("FRED", "CPIAUCSL", ["CPI"], {})
    levels = _provider_request_contract("FRED", intent, {"indicator": "CPIAUCSL"})
    dropped = _provider_request_contract("FRED", intent, {"indicator": "CPIAUCSL", "seriesId": f"CPIAUCSL:{suffix}"})
    assert dropped == levels


def test_fred_contract_normalizes_the_kept_suffix_case() -> None:
    intent = _intent("FRED", "CPIAUCSL", ["CPI"], {})
    upper = _provider_request_contract("FRED", intent, {"indicator": "CPIAUCSL", "seriesId": "CPIAUCSL:PC1"})
    lower = _provider_request_contract("FRED", intent, {"indicator": "CPIAUCSL", "seriesId": "CPIAUCSL:pc1"})
    assert upper == lower and upper["transform"] == "pc1"


def _fred_with_payloads(observations_params: list) -> FREDProvider:
    fred = FREDProvider.__new__(FREDProvider)
    fred.base_url = "https://api.stlouisfed.org/fred"
    fred.api_key = "k"
    fred.timeout = 30.0
    fred._search_cache = {}
    fred._series_existence_memo = {}
    info = {"seriess": [{
        "id": "CPIAUCSL", "title": "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average",
        "units": "Index 1982-1984=100", "frequency": "Monthly", "seasonal_adjustment": "Seasonally Adjusted",
        "observation_start": "1947-01-01", "observation_end": "2026-09-01", "last_updated": "2026-10-15",
    }]}
    obs = {"observations": [{"date": "2026-08-01", "value": "1.2"}, {"date": "2026-09-01", "value": "1.3"}]}

    async def _get(_client, url, params=None, **_kw):
        if url.endswith("/series/observations"):
            observations_params.append(dict(params or {}))
            return SimpleNamespace(status_code=200, json=lambda: obs)
        return SimpleNamespace(status_code=200, json=lambda: info)

    fred._get_with_retry = _get
    return fred


@pytest.mark.parametrize(
    "series_id,units,unit_label,data_type",
    [
        ("CPIAUCSL:ch1", "ch1", "Change from Year Ago, Index 1982-1984=100", "Change"),
        ("CPIAUCSL:chg", "chg", "Change, Index 1982-1984=100", "Change"),
        ("CPIAUCSL:pca", "pca", "Compounded Annual Rate of Change", "Percent Change"),
        ("CPIAUCSL:pc1", "pc1", "Percent Change from Year Ago", "Percent Change"),
        ("CPIAUCSL:yoy", None, "Index 1982-1984=100", "Index"),
        ("CPIAUCSL:lin", None, "Index 1982-1984=100", "Index"),
    ],
)
def test_fred_provider_sends_only_documented_units_and_labels_them(series_id, units, unit_label, data_type) -> None:
    sent: list = []
    fred = _fred_with_payloads(sent)
    with patch("backend.providers.fred.get_http_client", return_value=SimpleNamespace()):
        series = asyncio.run(fred.fetch_series({"indicator": "CPIAUCSL", "seriesId": series_id}))
    assert sent and sent[0].get("units") == units
    assert sent[0]["series_id"] == "CPIAUCSL"
    assert series.metadata.unit == unit_label
    assert series.metadata.dataType == data_type
