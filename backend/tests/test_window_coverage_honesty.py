"""Window misses must be scoped to the SERVED series, with its retrieved span.

User-reported retry loops (2026-11-07): a window miss told the user "the
latest available observation for <concept> is X" — scoped to the user's
CONCEPT + country, carrying only the latest date. Two failure shapes:

  * a series truncated/started late (ICE BofA indices FRED now serves only
    3 years of) asked for an earlier period: told "latest is 2026-11-05", the
    user rephrased 10+ times because the message hid that the window lies
    BEFORE the series' first observation;
  * a stale pick (an old mirror series) presented as the CONCEPT's latest
    although other series are current.

Mechanism: the window-marker exception carries a structured WindowCoverage
(provider, fetched series metadata, latest, earliest-from-the-UNWINDOWED-probe
only), and the renderer scopes to "series CODE (title)" when exactly one series
identity was fetched, naming the side of the span the window misses. The legacy
string marker is unchanged for its substring consumers.

The same fetched metadata feeds the subnational fail-closed on window misses
(a national series' window miss must not answer a provincial ask), and the
viability seam fails closed when EVERY menu option was fetched and none
references the requested region.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ExecutionPlan, Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.services.data_fetcher import (
    WindowCoverage,
    _probe_earliest_observation,
    _window_marker_error,
)
from backend.services.indicator_clarification import (
    build_all_options_region_uncovered_response,
    filter_viable_indicator_choice_options,
)
from backend.services.query import QueryService
from backend.utils.retry import DataNotAvailableError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _meta(indicator="Cost per training hour", country="IT", series_id="trng_cvt_20s",
          source="Eurostat", notes=None) -> Metadata:
    return Metadata(
        source=source, indicator=indicator, country=country,
        frequency="annual", unit="EUR", seriesId=series_id, notes=notes,
    )


def _series(points, **meta_kwargs) -> NormalizedData:
    return NormalizedData(
        metadata=_meta(**meta_kwargs),
        data=[{"date": d, "value": v} for d, v in points],
    )


def _fetch_intent(query="Italy cost per training hour from Eurostat 2021-2024") -> ParsedIntent:
    return ParsedIntent(
        apiProvider="EUROSTAT", indicators=["TRNG_CVT_20S"],
        parameters={
            "country": "IT", "indicator": "TRNG_CVT_20S",
            "startDate": "2021-01-01", "endDate": "2024-12-31",
            "__semantic_authority": "exact_user_input", "__time_scope_authority": "user",
        },
        originalQuery=query, clarificationNeeded=False,
    )


def _service(responses):
    replies = iter(responses)
    calls = []

    async def fetch(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        result = next(replies)
        if isinstance(result, BaseException):
            raise result
        return result

    svc = SimpleNamespace(
        _MAX_ALTERNATE_CODE_RETRIES=2,
        _preflight_geographic_split=AsyncMock(return_value=None),
        _maybe_expand_ranking_country_scope=lambda query, provider, params: params,
        _resolve_indicator_for_fetch=AsyncMock(side_effect=lambda provider, request, params: params),
        _get_from_cache=AsyncMock(return_value=None),
        _save_to_cache=AsyncMock(),
        _normalize_bis_metadata_labels=lambda result: None,
        # _fetch_data asks whether a fallback can follow (final provider stage).
        _cross_provider_fallback_refused=lambda intent: False,
        eurostat_provider=SimpleNamespace(fetch_indicator=AsyncMock(side_effect=fetch)),
    )
    return svc, calls


def _fetch_marker(responses) -> DataNotAvailableError:
    svc, _calls = _service(responses)
    with pytest.raises(DataNotAvailableError) as failure:
        asyncio.run(QueryService._fetch_data(svc, _fetch_intent()))
    assert "no_data_in_requested_window" in str(failure.value)
    return failure.value


def _tracker():
    return SimpleNamespace(to_list=lambda: [])


def _render_intent(language=None, indicators=("corporate bond yield",), region=None,
                   provider="FRED", country="US") -> ParsedIntent:
    intent = ParsedIntent(
        apiProvider=provider, indicators=list(indicators),
        parameters={"country": country, "startDate": "2015-01-01", "endDate": "2016-12-31"},
        clarificationNeeded=False, subnationalRegion=region,
    )
    intent.language = language
    return intent


def _coverage(metas, latest, earliest="", start=2015, end=2016, provider="FRED") -> WindowCoverage:
    return WindowCoverage(
        provider=provider, latest=latest, earliest=earliest,
        window_start_year=start, window_end_year=end, series_metadata=tuple(metas),
    )


def _marker(coverage: Optional[WindowCoverage], latest="2026-11-05") -> DataNotAvailableError:
    exc = DataNotAvailableError(
        f"no_data_in_requested_window: ... latest_available={latest}"
    )
    if coverage is not None:
        exc.window_coverage = coverage
    return exc


BAML = _meta(
    indicator="ICE BofA AA US Corporate Index Effective Yield", country="US",
    series_id="BAMLC0A2CAAEY", source="FRED",
)


# ---------------------------------------------------------------------------
# (1) data_fetcher: markers carry structured coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first", ["raised", "empty"])
def test_probe_marker_window_before_series_start_carries_earliest(first):
    initial = DataNotAvailableError("isolated") if first == "raised" else _series([])
    exc = _fetch_marker([initial, _series([("2025-01-01", 1.0), ("2026-01-01", 2.0)])])
    # Legacy token unchanged for the substring/regex consumers.
    assert "latest_available=2026-01-01" in str(exc)
    assert "earliest_available=2025-01-01" in str(exc)
    cov = exc.window_coverage
    assert cov.earliest == "2025-01-01" and cov.latest == "2026-01-01"
    assert cov.direction() == "before"
    assert cov.single_series().seriesId == "trng_cvt_20s"
    assert (cov.window_start_year, cov.window_end_year) == (2021, 2024)


def test_probe_marker_window_after_series_end():
    exc = _fetch_marker([_series([]), _series([("2012-01-01", 1.0), ("2019-01-01", 2.0)])])
    cov = exc.window_coverage
    assert (cov.earliest, cov.latest) == ("2012-01-01", "2019-01-01")
    assert cov.direction() == "after"


def test_post_filter_marker_never_claims_an_earliest_bound():
    # The provider ignored the window and returned only out-of-window points:
    # that payload came from a WINDOWED request, so its minimum is not the
    # series' first observation — earliest must stay unprovable.
    exc = _fetch_marker([_series([("2017-01-01", 1.0), ("2019-01-01", 2.0)])])
    assert "latest_available=2019-01-01" in str(exc)
    assert "earliest_available" not in str(exc)
    cov = exc.window_coverage
    assert cov.earliest == "" and cov.latest == "2019-01-01"
    assert cov.single_series().indicator == "Cost per training hour"


def test_probe_earliest_is_unprovable_when_any_point_is_inconclusive():
    assert _probe_earliest_observation([_series([("2019-01-01", 1.0), ("2003-01-01", None)])]) == "2019-01-01"
    assert _probe_earliest_observation([_series([("2019-01-01", 1.0), ("latest", 2.0)])]) == ""
    assert _probe_earliest_observation([_series([("2019-01-01", float("nan"))])]) == ""


def test_window_marker_error_keeps_text_and_copies_metadata():
    source = _series([("2019-01-01", 1.0)])
    exc = _window_marker_error(
        "no_data_in_requested_window: x latest_available=2019-01-01",
        provider="EUROSTAT", params={"start_year": 2021}, latest="2019-01-01", series=[source],
    )
    assert str(exc) == "no_data_in_requested_window: x latest_available=2019-01-01"
    meta = exc.window_coverage.series_metadata[0]
    assert meta == source.metadata and meta is not source.metadata


def test_single_series_identity_distinguishes_countries():
    cov = _coverage([_meta(country="IT"), _meta(country="FR")], latest="2019-01-01")
    assert cov.single_series() is None
    cov = _coverage([_meta(), _meta()], latest="2019-01-01")
    assert cov.single_series() is not None


# ---------------------------------------------------------------------------
# (1) rendering: served-series scope, both directions, zh, fallbacks
# ---------------------------------------------------------------------------

def test_render_before_start_names_served_series_and_its_span():
    cov = _coverage([BAML], latest="2026-11-05", earliest="2023-11-07")
    resp = QueryService._render_window_marker_response("c", _render_intent(), _tracker(), _marker(cov))
    msg = resp.message or ""
    assert resp.error == "no_data_in_requested_window"
    assert "BAMLC0A2CAAEY" in msg and "ICE BofA AA US Corporate Index Effective Yield" in msg
    assert "2023-11-07" in msg and "2026-11-05" in msg
    assert "earlier than 2023-11-07" in msg
    assert "No observations were found" in msg
    assert "published" not in msg
    # Scope is the SERVED series, never the user's concept wording.
    assert "corporate bond yield" not in msg
    assert resp.windowCoverage is cov


def test_render_after_end_zh():
    stale = _meta(indicator="CPI total, growth on same period previous year", country="US",
                  series_id="CPALTT01USM659N", source="FRED")
    cov = _coverage([stale], latest="2025-04-01", earliest="1956-01-01", start=2026, end=2026)
    resp = QueryService._render_window_marker_response(
        "c", _render_intent(language="zh"), _tracker(), _marker(cov, latest="2025-04-01"),
    )
    msg = resp.message or ""
    assert "CPALTT01USM659N" in msg and "1956-01-01" in msg and "2025-04-01" in msg
    assert "晚于 2025-04-01" in msg
    assert "未找到" in msg and "其他序列" in msg
    assert "发布" not in msg


def test_render_window_inside_span_has_no_direction_sentence():
    cov = _coverage([BAML], latest="2026-11-05", earliest="2023-11-07", start=2024, end=2024)
    msg = QueryService._render_window_marker_response(
        "c", _render_intent(), _tracker(), _marker(cov),
    ).message or ""
    assert "2023-11-07" in msg
    assert "earlier than" not in msg and "later than" not in msg


def test_render_without_provable_earliest_is_latest_only():
    cov = _coverage([BAML], latest="2019-01-01", earliest="", start=2026, end=2026)
    msg = QueryService._render_window_marker_response(
        "c", _render_intent(), _tracker(), _marker(cov, latest="2019-01-01"),
    ).message or ""
    assert "latest retrieved observation for series **BAMLC0A2CAAEY**" in msg
    assert "later than 2019-01-01" in msg
    assert " run from " not in msg and "earlier than" not in msg


def test_render_title_with_equals_and_cjk_is_verbatim():
    odd = _meta(indicator="社会融资规模存量=同比 (%)", country="China", series_id="TSF_YOY", source="ChinaMacro")
    cov = _coverage([odd], latest="2026-09-01", earliest="2017-01-01", start=2010, end=2012)
    msg = QueryService._render_window_marker_response(
        "c", _render_intent(language="zh"), _tracker(), _marker(cov, latest="2026-09-01"),
    ).message or ""
    assert "社会融资规模存量=同比 (%)" in msg and "TSF_YOY" in msg
    assert "早于 2017-01-01" in msg


def test_render_multi_series_falls_back_to_concept_scope():
    cov = _coverage([_meta(country="IT"), _meta(country="FR")], latest="2019-01-01")
    msg = QueryService._render_window_marker_response(
        "c", _render_intent(indicators=("training cost",)), _tracker(), _marker(cov, latest="2019-01-01"),
    ).message or ""
    assert "The latest available observation for training cost" in msg
    assert "2019-01-01" in msg


def test_render_string_marker_without_coverage_is_legacy():
    resp = QueryService._render_window_marker_response(
        "c", _render_intent(indicators=("GDP",)), _tracker(),
        "no_data_in_requested_window: ... latest_available=2019-08-01",
    )
    assert resp.error == "no_data_in_requested_window"
    assert "The latest available observation for GDP" in (resp.message or "")
    assert resp.windowCoverage is None


def test_window_coverage_is_never_serialized():
    cov = _coverage([BAML], latest="2026-11-05", earliest="2023-11-07")
    resp = QueryService._render_window_marker_response("c", _render_intent(), _tracker(), _marker(cov))
    assert "windowCoverage" not in resp.model_dump()
    assert "windowCoverage" not in resp.model_dump_json()


# ---------------------------------------------------------------------------
# (3a) subnational fail-closed on window misses — FETCHED metadata only
# ---------------------------------------------------------------------------

def _window_response(region, metas, language=None, provider="WorldBank", country="China"):
    cov = _coverage(metas, latest="2023-01-01", earliest="1970-01-01", start=2024, end=2025,
                    provider=provider)
    intent = _render_intent(language=language, region=region, provider=provider, country=country,
                            indicators=("per capita disposable income",))
    return QueryService._render_window_marker_response(
        "c", intent, _tracker(), _marker(cov, latest="2023-01-01"),
    )


WB_NATIONAL = _meta(
    indicator="Adjusted net national income per capita (current US$)", country="China",
    series_id="NY.ADJ.NNTY.PC.CD", source="World Bank",
)


def test_zh_province_window_miss_on_national_series_fails_closed():
    resp = _window_response("Liaoning", [WB_NATIONAL], language="zh")
    out = QueryService.__new__(QueryService)._enforce_subnational_fail_closed(resp)
    assert out.error == "subnational_data_unavailable"
    assert out.data is None
    assert "Liaoning" in out.message and "NY.ADJ.NNTY.PC.CD" in out.message
    assert "级别" in out.message
    assert "未发布" not in out.message  # no unverified "not published" claim


def test_en_window_miss_wording_never_claims_not_published():
    out = QueryService.__new__(QueryService)._enforce_subnational_fail_closed(
        _window_response("Liaoning", [WB_NATIONAL])
    )
    assert "No **Liaoning**-level series" in out.message
    assert "not published" not in out.message


@pytest.mark.parametrize("region,meta", [
    ("Texas", _meta(indicator="Unemployment Rate in Texas", country="US", series_id="TXUR", source="FRED")),
    ("Ontario", _meta(indicator="Canadian Unemployment Rate - Ontario", country="Canada",
                      series_id="v2063004", source="StatsCan")),
    ("Zhejiang", _meta(indicator="GDP", country="China", series_id="X",
                       notes=["Subnational series: Zhejiang province"])),
])
def test_window_miss_on_region_series_keeps_window_message(region, meta):
    resp = _window_response(region, [meta])
    before = resp.message
    out = QueryService.__new__(QueryService)._enforce_subnational_fail_closed(resp)
    assert out.error == "no_data_in_requested_window"
    assert out.message == before


def test_window_miss_without_fetched_metadata_is_untouched():
    intent = _render_intent(region="Liaoning", provider="WorldBank", country="China")
    resp = QueryService._render_window_marker_response(
        "c", intent, _tracker(), "no_data_in_requested_window: latest_available=2023-01-01",
    )
    before = resp.message
    out = QueryService.__new__(QueryService)._enforce_subnational_fail_closed(resp)
    assert out.error == "no_data_in_requested_window" and out.message == before


def test_window_miss_without_region_is_untouched():
    resp = _window_response(None, [WB_NATIONAL])
    out = QueryService.__new__(QueryService)._enforce_subnational_fail_closed(resp)
    assert out.error == "no_data_in_requested_window"


def test_window_miss_enforcement_is_idempotent():
    svc = QueryService.__new__(QueryService)
    once = svc._enforce_subnational_fail_closed(_window_response("Liaoning", [WB_NATIONAL]))
    message = once.message
    twice = svc._enforce_subnational_fail_closed(once)
    assert twice.error == "subnational_data_unavailable" and twice.message == message


def test_window_miss_fail_closed_does_not_trigger_re_adjudication():
    svc = QueryService.__new__(QueryService)
    calls: List[int] = []

    async def impl(self, query, **kwargs):  # noqa: ANN001, ARG001
        calls.append(1)
        return _window_response("Liaoning", [WB_NATIONAL])

    with patch.object(QueryService, "_process_query_impl", impl):
        out = asyncio.run(svc.process_query("辽宁省居民人均可支配收入 2024 2025"))
    assert len(calls) == 1, "no served code -> no extra adjudication round"
    assert out.error == "subnational_data_unavailable"


# ---------------------------------------------------------------------------
# (3b) viability seam: every fetched option fails the region check
# ---------------------------------------------------------------------------

class _FakeQS:
    def __init__(self, fetch_results: Dict[str, Any], region_hits: set) -> None:
        self._fetch_results = fetch_results
        self._region_hits = region_hits

    async def _fetch_data(self, intent: ParsedIntent):
        outcome = self._fetch_results.get(str((intent.parameters or {}).get("indicator") or ""))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _rerank_data_by_query_relevance(self, query, data):
        return data

    def _is_ranking_query(self, query):
        return False

    def _apply_ranking_projection(self, query, data):
        return data

    def _has_implausible_top_series(self, query, data):
        return False

    def _served_data_references_region(self, data, region):
        return str(data[0].metadata.indicator) in self._region_hits

    def _needs_indicator_clarification(self, query, data, intent, caller=""):
        return False


def _menu_intent(region="Liaoning", language=None) -> ParsedIntent:
    intent = ParsedIntent(
        apiProvider="WorldBank", indicators=["disposable income"], parameters={},
        clarificationNeeded=False, subnationalRegion=region,
    )
    intent.language = language
    return intent


OPT_A = "[WorldBank] National income per capita (AAA)"
OPT_B = "[IMF] Household income (BBB)"


def _nat(indicator):
    return [NormalizedData(metadata=_meta(indicator=indicator, country="China"),
                           data=[{"date": "2023-01-01", "value": 1.0}])]


async def _evidence(qs, intent):
    collector: Dict[str, Any] = {}
    viable = await filter_viable_indicator_choice_options(
        qs, query="q", intent=intent, options=[OPT_A, OPT_B], collector=collector,
    )
    return viable, collector


@pytest.mark.asyncio
async def test_menu_of_all_national_options_fails_closed():
    qs = _FakeQS({"AAA": _nat("National income"), "BBB": _nat("Household income")}, region_hits=set())
    intent = _menu_intent(language="zh")
    viable, evidence = await _evidence(qs, intent)
    resp = build_all_options_region_uncovered_response("c", intent, viable, evidence)
    assert resp is not None
    assert resp.error == "subnational_data_unavailable"
    assert resp.clarificationNeeded is False and not resp.clarificationOptions
    assert "Liaoning" in resp.message and "级别" in resp.message


@pytest.mark.asyncio
async def test_menu_with_one_region_option_is_not_failed_closed():
    qs = _FakeQS(
        {"AAA": _nat("Liaoning disposable income"), "BBB": _nat("Household income")},
        region_hits={"Liaoning disposable income"},
    )
    intent = _menu_intent()
    viable, evidence = await _evidence(qs, intent)
    assert viable  # the regional option survives -> sole-survivor / menu path
    assert build_all_options_region_uncovered_response("c", intent, viable, evidence) is None


@pytest.mark.asyncio
async def test_transient_drop_keeps_menu_behavior():
    qs = _FakeQS({"AAA": _nat("National income"), "BBB": RuntimeError("timeout")}, region_hits=set())
    intent = _menu_intent()
    viable, evidence = await _evidence(qs, intent)
    assert not viable
    assert build_all_options_region_uncovered_response("c", intent, viable, evidence) is None


def test_no_region_or_no_evidence_keeps_menu_behavior():
    dropped = {"dropped": {OPT_A: "substantive:region_not_covered"}}
    assert build_all_options_region_uncovered_response("c", _menu_intent(region=None), [], dropped) is None
    assert build_all_options_region_uncovered_response("c", _menu_intent(), [], {}) is None
    assert build_all_options_region_uncovered_response("c", _menu_intent(), [], None) is None
